# PreToolUse hook: measure what a git command would discard, and refuse it when
# the loss is one no `git fsck` reaches. Fail-closed after the verb is recognized.

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .shell_tokens import is_separator, tokenize

# The override token, read from the raw command string: the comment carrying it is
# stripped before tokenizing, so the token list never holds it.
ACK_RE = re.compile(r"#\s*ack:([0-9a-f]{16})\b")


# A backslash-escaped newline, which the shell joins before reading anything.
# shlex leaves a bare newline, which `is_separator` reads as a command boundary:
# `git checkout \` + newline + `-- a.txt` then arrives without its pathspec.
CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\r?\n")

# A command substitution, innermost first. `(` and `)` are separator tokens, so
# uncollapsed, `git -C $(git rev-parse --show-toplevel) reset --hard` splits with
# the covered verb in no fragment. `$SUBST` keeps the `$`, marking a value only
# the shell can supply.
SUBSTITUTION = re.compile(r"\$\([^()]*\)|`[^`]*`")

# Characters that glue to a word without being part of it, stripped off the words
# after a command name in `mentions`: `sh -c 'git reset'` presents the verb as
# `reset'`. The name itself needs none of it: `COMMAND_GIT` reads the characters
# on either side of it.
CLINGING = "'\"()$`\\"

# Characters that may appear inside a command name: `COMMAND_GIT` reads a `git` as
# a name when neither neighbour is one, so `x=$(git`, `a$(git` and `<(git` are all
# read. Spelled from what a name may hold, so an omission costs one refusal; a
# list of the separators that end a name would let a missing one hide a call.
NAME_CHAR = r"A-Za-z0-9_.+\-"

# `/` differs on the two sides, which is the basename rule spelled as neighbours:
# in front it path-qualifies, so `/usr/bin/git` is read; behind it makes the `git`
# a directory component, so `git/lfs` is not. The other exclusions come from
# `NAME_CHAR` on the side the character sits: `gitk` from the right, `.gitignore`
# from the left.
COMMAND_GIT = re.compile(rf"(?<![{NAME_CHAR}])git(?![{NAME_CHAR}/])")

# Characters that end the simple command an inert-subcommand guard belonged to.
# The parens and the backtick because what is inside a substitution runs on its
# own: `git commit -am "$(git reset --hard)"` discards before the commit begins.
SEPARATORS = ";&|()`"

# The end of the whitespace token a candidate sits in: a separator behind the name
# in the same word still ends the previous command.
WHITESPACE = re.compile(r"\s")

# The file-descriptor number in front of a redirection, and only written against
# it. Adjacency survives only here: `2>log` and `2 > log` reach the tokenizer as
# the same three tokens, and `git checkout -- 2 > log` may name a file called `2`.
FD_PREFIX = re.compile(r"(?:(?<=\s)|\A)\d+(?=[<>])")

# The stand-in `mask_quoted` writes over quoted and escaped characters. NUL,
# because nothing a caller looks for through the mask can match it.
MASK = "\0"


def mask_quoted(text: str) -> str:
    """`text` with every quoted or escaped character replaced by `MASK`, same
    length, so a caller can match on the mask and apply the spans to the original.
    """
    out: list[str] = []
    quote = ""
    ansi_c = False
    dollar = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            dollar = False
            out.append(MASK)
            continue
        if (quote != "'" or ansi_c) and ch == "\\":
            escaped = True
            dollar = False
            out.append(MASK)
            continue
        if quote:
            if ch == quote:
                quote = ""
                ansi_c = False
            dollar = False
            out.append(MASK)
            continue
        if ch in "'\"":
            ansi_c = ch == "'" and dollar
            quote = ch
            dollar = False
            out.append(MASK)
            continue
        dollar = ch == "$"
        out.append(ch)
    return "".join(out)


def strip_fd_prefixes(command: str) -> str:
    """Blank a descriptor number written against a redirection, quoting respected."""
    masked = mask_quoted(command)
    out = list(command)
    for found in FD_PREFIX.finditer(masked):
        out[found.start() : found.end()] = " " * (found.end() - found.start())
    return "".join(out)


# git subcommands that take their arguments as data, never as a command to run.
# A `git <verb>` written inside one is text and not a call -- most visibly a commit
# message. An allowlist, because the opposite set (`submodule foreach`,
# `bisect run`, `rebase -x`) is open at the dangerous end, and an omission from
# this list costs one refusal.
#
# Nothing joins the list on reasoning: each entry was run with `git <verb> --hard`
# inside its argument and the worktree checked afterwards.
INERT_SUBCOMMANDS = frozenset(
    {"commit", "log", "config", "grep", "tag", "stash", "branch", "show", "notes"}
)


def prepared(command: str, *, mask: bool = True) -> str:
    """The command as the shell's own word-splitting would first see it. `mask`
    collapses command substitutions, which only the tokenizing path wants.
    """
    # Comments come off before lines are joined, because a backslash inside a
    # comment is ordinary text to the shell -- `echo one # note \` / `echo two`
    # prints both, verified in bash and zsh. Joining first would delete the second
    # line along with the comment, and the reset on it would still run.
    command = strip_comments(command)
    command = CONTINUATION.sub(r"\1 ", command)
    if mask:
        previous = ""
        while previous != command:
            previous = command
            command = SUBSTITUTION.sub("$SUBST", command)
    return strip_fd_prefixes(command)


def logical_lines(text: str) -> list[str]:
    """Split on the newlines that end a command, leaving quoted ones in place."""
    masked = mask_quoted(text)
    out: list[str] = []
    start = 0
    for i, seen in enumerate(masked):
        if seen == "\n":
            out.append(text[start:i])
            start = i + 1
    out.append(text[start:])
    return out


def mentions(command: str) -> int:
    """Covered calls the raw text names, read as crudely as this hook can manage.

    A second parser and a dumber one, so what made the real parse lose a verb has no
    purchase on it. `main` compares the two counts.
    """
    found = 0
    # Line by line, because a newline ends a command as surely as a `;` does and,
    # being whitespace, never survives into a word for the separator test below to
    # find. Carried across lines, one `git log -1` would disarm the guard for
    # everything after it.
    for line in logical_lines(prepared(command, mask=False)):
        inert = ""
        previous = 0
        for candidate in COMMAND_GIT.finditer(line):
            start = candidate.start()
            # To the end of this candidate's own token, not to the name: in
            # `git log -1 git)` the `)` sits behind the second name inside the
            # same token, and it is what ends the previous command.
            edge = WHITESPACE.search(line, start)
            through = edge.start() if edge else len(line)
            if any(ch in line[previous:through] for ch in SEPARATORS):
                inert = ""
            previous = candidate.end()
            if inert:
                continue  # an argument of `git <inert>`, not a call of its own
            # From the name onwards, so the word it may be buried in contributes
            # only what follows the name. Its own remainder is dropped with it.
            raws = line[start:].split()[1:]
            words = [w.strip(CLINGING) for w in raws]
            _, rest, _ = strip_global_opts(words)
            if not rest:
                continue
            if rest[0] in COVERED:
                found += 1
            elif rest[0] in INERT_SUBCOMMANDS:
                inert = rest[0]
            else:
                # The subcommand position holds a word that is neither, which
                # means the whitespace split tore a quoted option value in two:
                # `-c user.name="John Doe"` leaves `-c` consuming `user.name="John`
                # and the verb one position further along. So look ahead rather
                # than give up, at the price of over-counting `git bisect reset`.
                for j, raw in enumerate(raws):
                    if any(ch in raw for ch in SEPARATORS):
                        break
                    if words[j] in COVERED:
                        found += 1
                        break
    return found


def strip_comments(command: str) -> str:
    """Drop `#`-comments the way the shell delimits them: only at a `#` that starts
    a word, and never inside quotes. Read through `mask_quoted`.
    """
    masked = mask_quoted(command)
    out: list[str] = []
    prev = " "  # the start of the line counts as a word boundary
    skipping = False
    # `strict` states `mask_quoted`'s invariant -- one character out per character
    # in -- so a break in it raises where `zip` would truncate the command
    # silently. At run time this sits inside `main`'s pre-recognition handler,
    # which allows, so the raise is visible under test and nowhere else.
    for original, seen in zip(command, masked, strict=True):
        if skipping:
            # A comment runs to the end of its line, and this hook is handed
            # multi-line commands, so the rest of the line is not the rest of it.
            if original == "\n":
                skipping = False
                out.append(original)
                prev = seen
            continue
        if seen == "#" and prev.isspace():
            skipping = True
            continue
        out.append(original)
        # From the MASK, so a quoted or escaped space does not read as the word
        # boundary a comment has to open at.
        prev = seen
    return "".join(out)


# Subcommands the hook itself may run, checked at the single subprocess entry, so
# an edit reaching for a mutating query fails loudly.
READ_ONLY = frozenset({"diff", "ls-files", "rev-parse"})

# Characters the shell expands after this hook has seen the text, so a pathspec
# containing one cannot be forwarded to git as written. Globs are excluded on
# purpose: git's own glob matches at least as much as the shell's, so forwarding
# one over-detects.
UNEXPANDED = re.compile(r"[{}$`~]")

# For a directory the glob exemption inverts, so it gets its own pattern. A
# pathspec carrying `*` is handed to git, which matches at least as widely. A
# directory carrying `*` is one the shell picks and the hook cannot: testing the
# written spelling finds nothing there and reads that as nothing at stake.
GLOB = re.compile(r"[*?\[]")


def shell_path(raw: str, what: str) -> Path:
    """`raw` as a path this hook can test, or `Unmeasurable` if only the shell can.
    `~` resolves here, this hook and that shell sharing a home.
    """
    path = Path(raw).expanduser()
    if UNEXPANDED.search(str(path)) or GLOB.search(str(path)):
        raise Unmeasurable(f"the {what} is resolved by the shell after this runs")
    return path


class Unmeasurable(Exception):
    """A recognized shape could not be measured. `main`'s handler turns it into a
    deny.
    """


def git(cwd: str, *args: str) -> str:
    if not args or args[0] not in READ_ONLY:
        raise Unmeasurable(f"hook attempted a non-read-only git query: {args!r}")
    try:
        proc = subprocess.run(
            # `core.quotepath=false` because git otherwise C-quotes a path holding
            # a non-ASCII byte: `é.txt` as `"\303\251.txt"`, which the reader
            # cannot pass back to git and the untracked fingerprint cannot stat.
            ["git", "-c", "core.quotepath=false", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise Unmeasurable(f"could not run git: {exc}") from exc
    if proc.returncode != 0:
        raise Unmeasurable(f"`git {args[0]}` failed: {proc.stderr.strip()}")
    return proc.stdout


def git_line(cwd: str, *args: str) -> str:
    """A query whose whole answer is one line, with the newline off."""
    return git(cwd, *args).strip()


# Commands that precede the real one without changing what it is. Stepping over a
# wrapper's own options needs per-wrapper knowledge, so `sudo -u x git reset
# --hard` lands on the mention test.
WRAPPERS = frozenset(
    {"env", "sudo", "doas", "nice", "nohup", "time", "command", "stdbuf"}
)
ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def git_args(argv: list[str]) -> tuple[list[str], bool] | None:
    """(arguments after `git`, a GIT_* assignment precedes them), or None for
    non-git.

    An assignment or a known wrapper is stepped over; anything else in command
    position ends the search. The test is the `GIT_` prefix, because git's
    environment surface grows.
    """
    relocating_env = False
    for i, tok in enumerate(argv):
        name = PurePosixPath(tok).name
        if name == "git":
            return argv[i + 1 :], relocating_env
        if ENV_ASSIGN.match(tok):
            relocating_env = relocating_env or tok.startswith("GIT_")
            continue
        if name in WRAPPERS:
            continue
        return None
    return None


def simple_commands(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """Split a token list into (preceding separator, argv) simple commands.

    A here-document's body is not set apart: newlines are separator tokens, so its
    lines arrive as commands of their own.
    """
    out: list[tuple[str, list[str]]] = []
    sep, cur = "", []
    skip_target = False
    for tok in tokens:
        if skip_target:
            skip_target = False
            continue
        if is_separator(tok):
            # A redirection is not a command boundary: read as one, `git clean -fd
            # 2>/dev/null` leaves `2` behind as clean's pathspec and `git 2>&1 reset
            # --hard` loses the verb. So the operator takes its target with it. Its
            # descriptor is already gone, `prepared` having removed it where
            # adjacency to the operator was still visible.
            if "<" in tok or ">" in tok:
                skip_target = True
                continue
            if cur:
                out.append((sep, cur))
                sep, cur = tok, []
            else:
                # Consecutive separators are kept, not overwritten. `(cd x) && git
                # reset --hard` puts `)` and `&&` back to back with nothing
                # between them; dropping the first loses the subshell's close,
                # and the `cd` inside it then follows the git command out.
                sep += tok
        else:
            cur.append(tok)
    if cur:
        out.append((sep, cur))
    return [(sep, argv) for sep, argv in (ungrouped(c) for c in out) if argv]


def ungrouped(command: tuple[str, list[str]]) -> tuple[str, list[str]]:
    """A simple command with its brace-group keywords peeled off. `{` and `}` are
    words to the tokenizer, so `{` otherwise sits in command position.
    """
    sep, argv = command
    if argv and argv[0] == "{":
        argv = argv[1:]
    if argv and argv[-1] == "}":
        argv = argv[:-1]
    return sep, argv


# git's own options that take their value as a separate following token, which
# consuming only the flag would leave sitting in the subcommand position, hiding
# the verb. The long ones also take `--opt=value`; `-C` and `-c` do not, so the
# `=` test below only ever spares a long option.
#
# Closed, and safe as closed: an option git does not know makes it exit 129 before
# the subcommand runs. Measured against git 2.50, and `--exec-path` is deliberately
# absent: bare, it prints the path and exits.
GIT_VALUE_OPTS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--config-env",
        "--attr-source",
    }
)

# git globals that change nothing this hook measures. Anything else in git's
# global position moves the ground under the measurement.
#
# Spelled this way round because git's global surface grows: the list of globals
# that matter would go quiet on whichever was added last, as `--icase-pathspecs`
# makes `readme.MD` reach `README.md` at run time. This one goes quiet in the
# other direction, at one override token.
#
# `-C` and `-c` are here because neither is stepped over blindly: `-C` has its
# directories resolved, `-c` is read for the setting that waives git's refusal.
GIT_INERT_GLOBALS = frozenset(
    {"-C", "-c", "-p", "-P", "--paginate", "--no-pager", "--no-optional-locks"}
)


def strip_global_opts(argv: list[str]) -> tuple[list[str], list[str], str | None]:
    """Returns (`-C` dirs, subcommand argv, the first global outside
    `GIT_INERT_GLOBALS`).

    Never raises: refusing before the verb is recognized is indistinguishable from
    not covering the shape. Globals are matched by exact spelling, git's own not
    going through parse-options.
    """
    cdirs: list[str] = []
    unknown: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        name = tok.split("=", 1)[0]
        if unknown is None and tok.startswith("-") and name not in GIT_INERT_GLOBALS:
            unknown = tok
        if name in GIT_VALUE_OPTS and "=" not in tok:
            if name == "-C" and i + 1 < len(argv):
                cdirs.append(argv[i + 1])
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            break
    return cdirs, argv[i:], unknown


# Subcommands this hook covers at all. Recognition turns on the verb alone, with
# no repository access, so that everything after it can fail closed.
COVERED = frozenset({"checkout", "switch", "restore", "reset", "clean"})


def covered_verb(argv: list[str]) -> str | None:
    """The covered subcommand named here, if any. Never raises, never runs git."""
    _, rest, _ = strip_global_opts(argv)
    return rest[0] if rest and rest[0] in COVERED else None


def split_at_ddash(rest: list[str]) -> tuple[list[str], list[str] | None]:
    """Return (tokens before `--`, tokens after `--` or None when absent)."""
    if "--" in rest:
        idx = rest.index("--")
        return rest[:idx], rest[idx + 1 :]
    return rest, None


# The long options this hook expands abbreviations for; `--orphan` is resolved at
# its one call site. A subcommand's options go through parse-options,
# which takes any unambiguous abbreviation, so `git reset --har` really does reset
# and matching by equality alone reads it as carrying no flag.
LONG_OPTS = frozenset(
    {
        "--patch",
        "--pathspec-from-file",
        "--force",
        "--discard-changes",
        "--staged",
        "--worktree",
        "--source",
        "--hard",
        "--dry-run",
        "--exclude",
    }
)

# Of those, the ones whose presence makes a command harmless, matched by exact
# spelling only: `--p` expands to `--patch`, which `switch` does not have, while
# git resolves it to `--progress` and discards the tree. Toward a destructive flag
# an over-expansion only adds a refusal, so that direction keeps abbreviations.
HARMLESS_LONG = frozenset({"--patch", "--dry-run", "--staged"})

# Short options that take a value, across the covered verbs: `-e` is clean's
# exclude, `-s` restore's source, `-b`/`-B`/`-c`/`-C` name a new branch. A bundle
# ends at the first of them, or the value's letters read as flags: `-e"*.pyc"`
# yields a `p`, `-fdxenode_modules` an `n`, `-bfix-thing` an `f`.
SHORT_VALUE = frozenset("esbBcC")

# How many at-stake paths the reason prints before it stops naming them.
AT_STAKE_LIMIT = 50


def expand_flags(flags: list[str]) -> set[str]:
    """Flag names named here, bundled short ones split and long ones un-abbreviated.

    `-SW` -> {'S', 'W'}, `--exclude=pat` -> `--exclude`, and an abbreviation expands
    to every `LONG_OPTS` entry it prefixes except the harmless ones, which take
    their full spelling. A bundle stops at the first `SHORT_VALUE` letter,
    everything after it being that option's value.
    """
    return set(flag_occurrences(flags))


def flag_occurrences(flags: list[str]) -> list[str]:
    """The same names `expand_flags` reports, in order and keeping repeats, which is
    what `forced_twice` needs.
    """
    out: list[str] = []
    for tok in flags:
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            out.append(name)
            if len(name) > 2:
                out.extend(
                    o
                    for o in LONG_OPTS - HARMLESS_LONG
                    if o.startswith(name) and o != name
                )
        elif tok.startswith("-") and len(tok) > 1:
            for ch in tok[1:]:
                out.append(ch)
                if ch in SHORT_VALUE:
                    break
    return out


def option_tokens(opts: list[str]) -> list[str]:
    """The option tokens in `opts`, with the `--` separator left out."""
    return [a for a in opts if a.startswith("-") and a != "--"]


def certainly_harmless(argv: list[str]) -> bool:
    """True when a covered verb is in a form that cannot destroy anything, decided
    without touching the repository and before anything about where it runs.
    """
    _, rest, _ = strip_global_opts(argv)
    if not rest:
        return True
    verb, args = rest[0], rest[1:]
    opts, _ = split_at_ddash(args)
    flags = expand_flags(option_tokens(opts))

    if given(flags, "p", "--patch"):
        return True
    if verb == "clean":
        return given(flags, "n", "--dry-run")
    if verb == "reset":
        return "--hard" not in flags
    if verb == "restore":
        staged = given(flags, "S", "--staged")
        worktree = given(flags, "W", "--worktree")
        return staged and not worktree
    if verb == "switch":
        return not forced(flags)
    if verb == "checkout":
        # Full spelling only for `--orphan`: this side answers "measure nothing",
        # so a wrong guess here drops the measurement.
        return not forced(flags) and (bool(flags & {"b", "B"}) or "--orphan" in flags)
    return False


# What git reads as false in a boolean config value. Measured: `=false`, `=0`,
# `=no` and `=off` each let `git clean -d` delete untracked content, while
# `=true` and a bare `-c clean.requireForce` (no value) leave git refusing.
CONFIG_FALSE = frozenset({"false", "0", "no", "off"})


def force_waived(argv: list[str]) -> bool:
    """Whether `-c clean.requireForce=<false>` rides in the command's own argv,
    which is what lets a bare `git clean -d` delete.

    Only the separate spelling exists, and the key compares case-insensitively as
    git compares it. The same setting in a config file stays uncovered.
    """
    for i, tok in enumerate(argv):
        if tok != "-c" or i + 1 >= len(argv):
            continue
        name, sep, value = argv[i + 1].partition("=")
        if (
            sep
            and name.strip().lower() == "clean.requireforce"
            and value.strip().lower() in CONFIG_FALSE
        ):
            return True
    return False


def abbreviates(flags: set[str], full: str) -> bool:
    """Whether any long flag present could be an abbreviation of `full`. Its one
    caller resolves `--orphan`, which `LONG_OPTS` deliberately does not carry.
    """
    return any(f.startswith("--") and len(f) > 2 and full.startswith(f) for f in flags)


def given(flags: set[str], short: str, long: str) -> bool:
    """Whether a flag is present, under either spelling git accepts for it."""
    return short in flags or long in flags


def forced(flags: set[str]) -> bool:
    """Whether these flags waive git's own refusal to act on a dirty tree — `switch`
    says `--discard-changes` for the same thing.
    """
    return "f" in flags or bool(flags & {"--force", "--discard-changes"})


def forced_twice(opts: list[str]) -> bool:
    """Whether `clean`'s force is given twice, which with `-d` or a pathspec is what
    reaches a directory holding its own `.git`. Takes raw option tokens, a set being
    unable to say twice.
    """
    names = flag_occurrences(option_tokens(opts))
    return sum(1 for n in names if n in ("f", "--force")) >= 2


def operands(opts: list[str], value_taking: set[str]) -> list[str]:
    """Non-option tokens from before `--`, skipping any option's separate value.

    Only the separate spelling consumes a following token: `--source=HEAD~1` and
    `-sHEAD~1` carry their value inside it. `value_taking` holds names as
    `expand_flags` reports them.
    """
    out: list[str] = []
    skip = False
    for tok in opts:
        if skip:
            skip = False
            continue
        if tok.startswith("--"):
            skip = "=" not in tok and bool(expand_flags([tok]) & value_taking)
        elif tok.startswith("-") and len(tok) == 2:
            skip = tok[1:] in value_taking
        if skip:
            continue
        if not tok.startswith("-"):
            out.append(tok)
    return out


def in_repository(cwd: str) -> bool:
    """Whether a command run here would find a repository, walking upwards."""
    try:
        git(cwd, "rev-parse", "--show-toplevel")
    except Unmeasurable:
        return False
    return True


def is_ref(cwd: str, name: str) -> bool:
    try:
        git(cwd, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
    except Unmeasurable:
        return False
    return True


def paths_of(cwd: str, rest: list[str]) -> list[str] | None:
    """Pathspecs a checkout-like invocation targets, or None if it targets no path.

    Without `--`, git resolves each operand as a ref first and only falls back to a
    path, so the same order is used here.
    """
    before, after = split_at_ddash(rest)
    if after is not None:
        return after or None
    operands = [t for t in before if not t.startswith("-")]
    return [t for t in operands if not is_ref(cwd, t)] or None


class Stake(NamedTuple):
    """What a covered invocation would destroy."""

    kind: str
    pathspecs: list[str]
    narrowing_dropped: bool
    # A directory holding its own `.git`, which git will not enumerate. Separate
    # from `kind` because it is a property of the flags: two invocations asking
    # `ls-files` the very same question differ on it.
    reaches_nested: bool


def widened(kind: str, reaches_nested: bool = False) -> Stake:
    """A stake measured without the narrowing the command itself carries; the reason
    says so when it is set.
    """
    return Stake(kind, [], True, reaches_nested)


def narrowed(kind: str, paths: list[str], reaches_nested: bool = False) -> Stake:
    """A stake narrowed to the pathspecs the command carries, where it can be. A
    pathspec `UNEXPANDED` matches is dropped.
    """
    if any(UNEXPANDED.search(p) for p in paths):
        return widened(kind, reaches_nested)
    return Stake(kind, paths, False, reaches_nested)


def stake_for(cwd: str, argv: list[str]) -> Stake | None:
    """What this invocation would destroy, as a `Stake`.

    Returns None when the shape is outside the list this hook covers, or when it
    is one of the covered verbs in a form that destroys nothing.
    """
    _, rest, unknown = strip_global_opts(argv)
    if not rest:
        return None
    if unknown is not None:
        raise Unmeasurable(
            f"`{unknown}` is a git global this hook does not recognize, and one "
            "can move what the command acts on or how its pathspecs match"
        )
    verb, args = rest[0], rest[1:]
    # Options are read only from before `--`. Everything after it is a pathspec,
    # and a filename opening with a dash would otherwise be split per character
    # by expand_flags: `-patch.txt` yields `p`, which reads as `-p` and sends a
    # real discard down the "interactive, no collateral" branch below.
    opts, after_ddash = split_at_ddash(args)
    flags = expand_flags(option_tokens(opts))

    # Interactive forms pick hunks, so they never take collateral.
    if given(flags, "p", "--patch"):
        return None
    if "--pathspec-from-file" in flags:
        raise Unmeasurable("pathspecs come from a file this hook cannot read")

    if verb in ("checkout", "switch"):
        force = forced(flags)
        if verb == "switch":
            # A forced switch rewrites the whole worktree and takes no pathspec.
            # Unforced, git itself refuses to lose work. `-C` is force-create and
            # leaves the tree alone, so `forced` must not read it as force.
            return narrowed("worktree", []) if force else None
        # The operand of these is the new branch's name, not a pathspec, and must
        # not reach `paths_of`: an unborn branch resolves as no ref, and read as a
        # path it narrows the measurement to a file that does not exist.
        names_a_branch = bool(flags & {"b", "B"}) or "--orphan" in flags
        if force and (names_a_branch or abbreviates(flags, "--orphan")):
            # An abbreviation may guess here and nowhere else: wrong on this side
            # it widens to the worktree a forced checkout reaches anyway, wrong on
            # the unforced side below it answers "nothing at stake" for `--ou`.
            return narrowed("worktree", [])
        if names_a_branch:
            return None
        paths = paths_of(cwd, args)
        if force:
            # `-f` waives git's own refusal; it does not widen what the command
            # reaches. With a pathspec the reach is still that pathspec, so
            # measuring the whole tree would deny `git checkout -f -- a.txt` over
            # an unrelated dirty `b.txt`. Without one, `[]` is the whole worktree.
            return narrowed("worktree", paths if paths is not None else [])
        return narrowed("worktree", paths) if paths is not None else None

    if verb == "restore":
        staged = given(flags, "S", "--staged")
        worktree = given(flags, "W", "--worktree")
        # `--staged` alone rewrites the index and leaves the file on disk, so the
        # content survives; only a worktree write can take it away.
        if staged and not worktree:
            return None
        paths = (
            after_ddash
            if after_ddash is not None
            else operands(opts, {"s", "--source"})
        )
        return narrowed("worktree", paths) if paths else None

    if verb == "reset":
        return narrowed("worktree", []) if "--hard" in flags else None

    if verb == "clean":
        if given(flags, "n", "--dry-run"):
            return None
        if not given(flags, "f", "--force") and not force_waived(argv):
            # `argv`, not `rest`: the setting sits among git's own options, which
            # `strip_global_opts` has already peeled off by this point.
            return None
        ignored = "-ignored-only" if "X" in flags else "-all" if "x" in flags else ""
        # Without `-d` and without a pathspec, clean leaves an untracked directory
        # whole while `ls-files --others` recurses regardless. A pathspec naming
        # the directory removes it either way -- measured on `git clean -f
        # newmodule` -- so the pathspec reaches `measure`, which then drops the
        # collapse.
        deep = "-deep" if "d" in flags else ""
        kind = f"untracked{ignored}{deep}"
        twice = forced_twice(opts)
        # `-e <pattern>` narrows what clean removes, and the report widens rather
        # than following it: `ls-files` takes the same `--exclude`, but forwarding
        # it means collecting a repeatable option's values across three spellings.
        if given(flags, "e", "--exclude"):
            # `reaches_nested` on the force alone, because this arm does not read
            # the operands and so cannot say whether a pathspec is among them.
            # Over-refusing a `-ff -e pat` costs one override token.
            return widened(kind, twice)
        paths = after_ddash if after_ddash is not None else operands(opts, set())
        return narrowed(kind, paths, twice and bool(deep or paths))

    return None


LS_SELECT = {
    "untracked": ["--others", "--exclude-standard"],
    "untracked-all": ["--others"],
    "untracked-ignored-only": ["--others", "--ignored", "--exclude-standard"],
}

# `--directory` collapses a wholly-untracked directory to its own name, so what is
# left un-collapsed is the set a bare `clean` without `-d` removes.
#
# A `/`-terminated entry means two unrelated things -- the collapse produced it, or
# it is a directory holding its own `.git`, which `ls-files` reports that way
# regardless -- and `forced_twice` is what separates them.
LS_COLLAPSE = ["--directory", "--no-empty-directory"]

# Every `git diff` this hook runs carries these. `diff.relative=true` in a user's
# config makes the command answer about the current directory only, so from a
# subdirectory the measurement misses every change above it; it also breaks the
# single frame the reason promises.
#
# `--diff-filter=d` (lower case excludes) drops a path whose only change is that it
# was deleted from the worktree: restoring one puts content back, not away.
DIFF_FRAME = ("--no-relative", "--diff-filter=d")


# How many files under a directory-shaped stake the fingerprint reads before it
# stops. Such a directory holds a whole repository, and the walk runs inside a
# PreToolUse hook, so the worst case needs a bound.
TREE_MARK_LIMIT = 2000


def tree_mark(path: Path) -> str:
    """Size and mtime of the files under `path`, as one string, sorted so that two
    runs agree. The walk stops at `TREE_MARK_LIMIT` and records that it did.
    """
    parts: list[str] = []
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda _: None):
        dirnames.sort()
        for name in sorted(filenames):
            if len(parts) >= TREE_MARK_LIMIT:
                return "\0".join([*parts, f"capped-at-{TREE_MARK_LIMIT}"])
            f = Path(dirpath) / name
            rel = f.relative_to(path)
            try:
                st = f.stat()
            except OSError:
                parts.append(f"{rel}\0gone")
                continue
            parts.append(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}")
    return "\0".join(parts)


def dedup(entries: list[str]) -> list[str]:
    """The non-empty entries, first occurrence only, order kept: `-z` output ends
    with a trailing NUL, and `diff --name-only` names an unmerged path once per
    stage it compares.
    """
    seen: set[str] = set()
    out: list[str] = []
    for entry in entries:
        if entry and entry not in seen:
            seen.add(entry)
            out.append(entry)
    return out


def measure(
    cwd: str, kind: str, pathspecs: list[str], reaches_nested: bool = False
) -> tuple[list[str], str, str]:
    """Return (affected paths, a display summary, a content fingerprint).

    The fingerprint is what the override token binds, so it has to move with the
    content: the raw patch for the worktree kind, size and mtime for the untracked
    ones, which have no patch. The summary is a diffstat, and empty for those.
    """
    # Whether the pathspecs could be forwarded at all was settled in `stake_for`:
    # what arrives here is the command's own narrowing, or nothing.
    tail = ["--", *pathspecs] if pathspecs else []
    if kind == "worktree":
        # `-z`, because a name carrying `"`, `\`, a tab or a newline is C-quoted
        # whatever `core.quotepath` says, that setting covering only non-ASCII
        # bytes. Quoted, `hunk_headers` forwards a pathspec that matches nothing;
        # a newline in a name also splits it in two under `splitlines`.
        names = dedup(
            git(cwd, "diff", *DIFF_FRAME, "--name-only", "-z", *tail).split("\0")
        )
        if not names:
            return [], "", ""
        # `--stat-count` because the summary is per-file too, so a cap on the path
        # list alone leaves this one growing. Past the cap it is cut to one path
        # line and the totals, the reason having printed those paths already. `1`,
        # git reading `0` as no limit at all.
        stat_count = 1 if len(names) > AT_STAKE_LIMIT else AT_STAKE_LIMIT
        summary = git(
            cwd, "diff", *DIFF_FRAME, "--stat", f"--stat-count={stat_count}", *tail
        ).rstrip("\n")
        return names, summary, git(cwd, "diff", *DIFF_FRAME, *tail)
    # `ls-files` takes the same `-- <pathspec>` tail, so a narrowed clean stays
    # narrowed here. `--full-name` because it otherwise reports paths relative to
    # the current directory while the worktree branch's `diff --name-only` reports
    # them relative to the root -- and the reason names one frame for both lists.
    deep = kind.endswith("-deep")
    select = LS_SELECT[kind.removesuffix("-deep")]
    # Collapsing is right only for the shape that leaves untracked directories
    # whole: a clean with neither `-d` nor a pathspec. `git clean -f newmodule`
    # removes `newmodule/impl.py`, which the collapsed entry named as one thing
    # that then read as out of reach.
    collapse = [] if deep or pathspecs else LS_COLLAPSE
    names = [
        ln
        for ln in dedup(
            git(cwd, "ls-files", "--full-name", "-z", *select, *collapse, *tail).split(
                "\0"
            )
        )
        # A trailing `/` is either a directory the collapse stood in for, whose
        # files the command leaves alone, or one holding its own `.git`, which
        # `-ff` removes outright. `reaches_nested` is the flags' answer to which,
        # so it decides whether the entry counts.
        if reaches_nested or not ln.endswith("/")
    ]
    # Size and mtime, which is what moves with the content without reading every
    # byte of every untracked file.
    #
    # Resolved against the repository root, `--full-name` above reporting
    # root-relative names: joined onto `cwd` from a subdirectory every stat misses,
    # every file marks `gone`, and the fingerprint stops depending on content.
    root = Path(git_line(cwd, "rev-parse", "--show-toplevel"))
    marks = []
    for n in names:
        try:
            if n.endswith("/"):
                # Its content is still content, so the mark has to come from
                # the filesystem.
                marks.append(f"{n}\0{tree_mark(root / n)}")
            else:
                st = (root / n).stat()
                marks.append(f"{n}\0{st.st_size}\0{st.st_mtime_ns}")
        except OSError:
            marks.append(f"{n}\0gone")
    # No summary for the untracked kinds: there is no diffstat to give, and the
    # path list the caller already has is the whole story.
    return names, "", "\n".join(marks)


def hunk_headers(cwd: str, paths: list[str]) -> list[str]:
    """`@@` headers for the first few affected files. Binary files yield none."""
    out: list[str] = []
    for path in paths[:3]:
        try:
            diff = git(cwd, "diff", *DIFF_FRAME, "-U0", "--", path)
        except Unmeasurable:
            continue
        heads = [ln for ln in diff.splitlines() if ln.startswith("@@")][:4]
        if heads:
            out.append(f"  {path}")
            out.extend(f"    {h}" for h in heads)
    return out


# Words that run what follows them in the current shell, so a `cd` behind one moves
# the shell exactly as a bare `cd` does. Neither opens a subshell: `builtin` skips
# any function of that name, `command` skips functions and aliases, and both then
# run the builtin.
SHELL_WRAPPERS = ("builtin", "command")

# Text this hook does not read, executed in the current shell, so a `cd` inside it
# is invisible here and moves the shell anyway.
#
# `source` and `.` are deliberately out: what they run resolves at run time, and
# refusing every one taxes `. .venv/bin/activate && ...`. A `cd` inside such a
# file is not followed; that is the cost.
RUNS_TEXT = ("eval",)

# `command`'s options are a closed set, which is what makes them safe to read:
# POSIX gives it `-p`, `-v` and `-V`, and only `-p` still runs the command.
# `builtin` takes none.
WRAPPER_OPTS_THAT_RUN = ("-p",)
WRAPPER_OPTS_THAT_REPORT = ("-v", "-V")


def unwrapped(argv: list[str]) -> list[str]:
    """`argv` with any `builtin` / `command` prefix peeled off.

    A reporting option (`command -v cd`) comes back unpeeled, so the caller reads it
    as no directory change; an option outside the closed set is refused, how many
    words it takes being unknown.
    """
    out = list(argv)
    while len(out) > 1 and out[0] in SHELL_WRAPPERS:
        rest = out[1:]
        while rest and rest[0].startswith("-"):
            if rest[0] in WRAPPER_OPTS_THAT_REPORT:
                return argv
            if rest[0] not in WRAPPER_OPTS_THAT_RUN:
                raise Unmeasurable(
                    f"`{out[0]} {rest[0]}` is a form this hook does not read"
                )
            rest = rest[1:]
        if not rest:
            return argv
        out = rest
    return out


def only_and(sep: str) -> bool:
    """Whether `sep` joins with `&&` and carries no weaker operator.

    A separator is a run of punctuation, so operators that ran together arrive
    glued: parentheses and a newline after the `&&` are dropped before the
    comparison.
    """
    return sep.translate(str.maketrans("", "", "()\n")) == "&&"


def nearest_existing(path: Path) -> Path | None:
    """The closest ancestor of `path` that exists and sits inside a repository, or
    None.

    What a command run in a directory this line creates can reach is whatever
    repository encloses that ancestor. Outside one, git resolves upwards and stops.
    """
    existing = path
    while not existing.is_dir() and existing != existing.parent:
        existing = existing.parent
    if not existing.is_dir() or not in_repository(str(existing)):
        return None
    return existing


def resolve_cwd(
    payload_cwd: str, commands: list[tuple[str, list[str]]], idx: int
) -> tuple[str, bool] | None:
    """(where `commands[idx]` runs, whether that is wider than its reach), or None
    when nowhere holds protected content.

    Applies the directory changes of the preceding simple commands. What lands
    somewhere this hook cannot follow — `popd`, a bare `cd`, a target only the shell
    can expand — is refused; `~` is expanded, this hook and that shell sharing a
    home.

    A target that does not exist yet holds nothing, so it is measured as nothing,
    and the separator decides whether the git call lands there or here.
    """
    cwd = Path(payload_cwd)
    saved: list[Path] = []
    for j in range(idx + 1):
        for ch in commands[j][0]:
            if ch == "(":
                saved.append(cwd)
            elif ch == ")" and saved:
                cwd = saved.pop()
        if j == idx:
            break  # its separator is applied; its own argv is the git call

        sep, argv = commands[j]
        if not argv:
            continue
        # Peeled before the word is tested: tested first,
        # `builtin eval "cd <repo>"` reads as `builtin`, which is neither a
        # directory change nor an unreadable one, and the line is stepped over.
        argv = unwrapped(argv)
        if argv[0] not in ("cd", "pushd", "popd", *RUNS_TEXT):
            # Only a command that moves the shell leaves the directory in doubt.
            # Refusing on any earlier `||` blames a `cd` that is not there, and
            # costs the at-stake list over a line that never moved.
            continue
        # A `cd` reached through any conditional -- `&&` as much as `||` -- may or
        # may not have run, and is safe to follow only when the same condition
        # guards the git call: under `mkdir x && cd x && git clean -n` the branch
        # that runs the git command is the one that ran the `cd`, while under
        # `test -d dist && cd dist; git reset --hard` both readings survive.
        #
        # Substring, a separator carrying every operator that ran together: `)&&`
        # is one of these.
        conditional = "&&" in sep or "||" in sep
        # `only_and` for the same reason, and the two have to agree: read one by
        # substring and the other by equality, and a separator carrying a `(` is
        # conditional-but-not-chained, which costs the measured refusal.
        chained = all(only_and(commands[k][0]) for k in range(j + 1, idx + 1))
        if conditional and not chained:
            raise Unmeasurable(
                f"a conditional `{argv[0]}` leaves the directory ambiguous"
            )
        follows = commands[j + 1][0]
        if set(follows) <= {"|", "&"} and follows not in ("||", "&&"):
            # A pipeline stage and a backgrounded command each get their own
            # subshell, so this `cd` moved a shell that has already exited by the
            # time the git command runs.
            continue
        if argv[0] in RUNS_TEXT:
            if saved:
                # Inside an unclosed `(`, so whatever it did to the directory left
                # with that subshell. Refusing here would trade a measured refusal
                # for a blind one over a move that cannot have reached the git call.
                continue
            # The `cd` would be inside text this hook does not read, and moves the
            # shell all the same. An unknown directory is a refusal.
            #
            # Placed after the skips above: a form they step over has moved nothing.
            raise Unmeasurable(
                f"`{argv[0]}` runs text this hook cannot read, and a `cd` inside "
                "it moves where the git command lands"
            )
        if argv[0] == "popd":
            raise Unmeasurable("`popd` returns to a directory only the shell knows")
        targets = [a for a in argv[1:] if not a.startswith("-")]
        if not targets:
            raise Unmeasurable(
                f"`{argv[0]}` with no usable target moves somewhere "
                "this hook cannot follow"
            )
        # The absence test below answers about the path as written, so a target
        # only the shell can resolve is refused: the miss would read as "nothing
        # here" for a directory about to hold a whole repository.
        target = shell_path(targets[0], f"`{argv[0]}` target")
        moved = target if target.is_absolute() else (cwd / target).resolve()
        if moved.is_dir():
            cwd = moved
        elif chained:
            # `chained` is every separator from here to the git call, `&&` guarding
            # only what immediately follows it: after
            # `cd nosuchdir && echo x; git reset --hard` the `;` runs the discard
            # where the shell never left.
            #
            # The shell moves here whether or not the directory exists yet, so the
            # walk follows and keeps going: where it ends is the only directory
            # worth answering about.
            cwd = moved
        else:
            # The `cd` will fail and something after it still reaches the git
            # call, which therefore runs right here. Measure that, and step over
            # the move.
            continue
    if cwd.is_dir():
        return (str(cwd), False)
    # The walk ended somewhere this line has still to create, and "holds nothing"
    # is about that directory alone: git resolves upwards, so
    # `mkdir -p d && cd d && git reset --hard` takes the enclosing tree.
    existing = nearest_existing(cwd)
    if existing is None:
        return None
    # Measured, but wider than the command reaches: whatever the line puts at the
    # target may well be a repository of its own, and then none of what is named
    # here is inside it. The reason has to say so, or a list of the parent's
    # files reads as the exact set at stake.
    return (str(existing), True)


def stash_form(kind: str) -> str:
    """The stash spelling that reaches this kind of content: `-u` stops at ignored
    files and only `--all` takes them. The `-deep` suffix does not bear on the
    choice.
    """
    kind = kind.removesuffix("-deep")
    if kind in ("untracked-all", "untracked-ignored-only"):
        return "git stash push --all"
    if kind == "untracked":
        return "git stash push -u"
    return "git stash push"


# What each spelling is for, in the order a reader picks between them. Kept beside
# `stash_form`, so the mapping from content to spelling stays one rule: picking
# wrong gets "No local changes to save" and leaves the file where it was.
STASH_ROUTES = (
    ("worktree", "a tracked change"),
    ("untracked", "an untracked file, which a plain push refuses"),
    ("untracked-all", "an ignored file, which -u still skips"),
)


def stash_routes() -> str:
    """The routes as one sentence fragment: spelling and what each one reaches."""
    return ", ".join(f"`{stash_form(k)}` for {what}" for k, what in STASH_ROUTES)


# The way back from a stash, as a complete clause: the sites have their own
# sentences, and shared text that arrives already grammatical cannot be spliced
# wrong. `stash_routes()` is the same pattern.
POP_BACK = "`git stash pop` brings them back, and keeps the entry if it cannot"


def operand(path: str) -> str:
    """A listed path in the spelling the printed routes can be given: each asks the
    reader to substitute these into `-- <path>...`.
    """
    return shlex.quote(path)


def listing_for(kind: str) -> str:
    """The command that prints the names the cap stood in for -- all of them.

    `--ignored` because a plain `git status` reports no ignored file at all, and
    `-uall` because it otherwise collapses an untracked directory to one entry.
    """
    ignored = " --ignored" if "-all" in kind or "-ignored-only" in kind else ""
    untracked = " -uall" if kind.startswith("untracked") else ""
    return f"git status --short{ignored}{untracked}"


def build_reason(
    kind: str,
    names: list[str],
    summary: str,
    hunks: list[str],
    token: str,
    root: str,
    narrowing_dropped: bool,
    wider_than_reach: bool,
) -> str:
    what = (
        "untracked file(s)" if kind.startswith("untracked") else "uncommitted change(s)"
    )
    # The count is always exact; the list is capped. `git clean -fdx` over a
    # `node_modules` names tens of thousands of files, which reaches the caller's
    # transcript as megabytes displacing the context needed to act on it.
    shown = names[:AT_STAKE_LIMIT]
    lines = [
        f"Blocked: this would discard {what} that cannot be recovered afterwards.",
        "",
        # Full paths first, then the summary. `git diff --stat` abbreviates a
        # long path to `.../tail.txt`, which is not usable as a git operand --
        # and these paths are exactly what the suggestions below ask for.
        f"At stake ({len(names)} file(s)), relative to {root}:",
        *(f"  {operand(n)}" for n in shown),
    ]
    if len(names) > len(shown):
        lines.append(
            f"  ... and {len(names) - len(shown)} more; "
            f"`{listing_for(kind)}` lists them all"
        )
    # Said plainly, a file the command visibly does not name reading two ways:
    # over-wide report, or broken hook. The two causes get their own sentences,
    # since a line carrying no narrowing must not be told its narrowing was
    # dropped, and both can hold at once -- two `if`s, not an `elif`.
    if narrowing_dropped:
        lines += [
            "",
            "The narrowing this command carries was not forwarded to the query",
            "behind this list, so the whole tree was measured: some of the files",
            "above may lie outside what it would actually reach.",
        ]
    if wider_than_reach:
        lines += [
            "",
            "The directory this command moves to does not exist yet, so the",
            "nearest one that does was measured instead. git resolves upwards,",
            "so a command run in a directory this line creates still reaches",
            "this tree -- but it may reach less of it than is listed above.",
        ]
    if kind == "worktree" and summary:
        lines += ["", summary]
    if hunks:
        lines += ["", "Hunks:", *hunks]
    # Every route runs from `cd <root>`. `-C` moves git's directory and not the
    # shell's, so under it the patch route writes keep.patch wherever the caller
    # stands while the patch body names root-relative paths.
    #
    # Neither route promises the restore succeeds, so what the message states is
    # that the copy outlives the failure. Without `EITHER` the pair reads as two
    # steps, and the diff then runs on a tree the stash has already cleaned.
    if kind == "worktree":
        lines += [
            "",
            "To keep any of it, make a recoverable copy BEFORE discarding.",
            "Take EITHER route, not both — the first one moves the content, so",
            "the second would find nothing left to copy. Both run without a",
            "terminal. Run whichever you pick from the root named above (`cd`",
            "there first — the paths listed are relative to it, and the patch",
            "route writes and re-reads a file there):",
            f"  {stash_form(kind)} -- <path>...",
            "      sets them aside; `git stash pop` brings them back",
            # `--binary` or the route silently is not one. Without it `git diff`
            # writes `Binary files a/x and b/x differ` -- a sentence, not a copy
            # -- and `git apply` refuses the whole patch, so a text file listed
            # beside a binary one is not restored either.
            "  git diff --binary -- <path>... > keep.patch",
            "      writes a patch; after discarding, `git apply keep.patch`",
            "      restores it, and editing the patch first restores only",
            "      the hunks you still want (--binary keeps any binary file",
            "      in the list restorable; without it `git apply` refuses the",
            "      whole patch, text files included)",
            "",
            "If this also moves the branch, the copy may not go back on cleanly.",
            "Neither route discards what it could not place: the stash entry is",
            "kept, and keep.patch stays where it was written.",
        ]
    else:
        # The spelling comes from `stash_form`, and so does the reason it is this
        # one. "reaches" and not "is for": a `clean -fdx` list holds untracked and
        # ignored files, and naming only the escalation reads as its whole scope.
        picked = next(
            what for k, what in STASH_ROUTES if stash_form(k) == stash_form(kind)
        )
        lines += [
            "",
            "To keep any of these, move them out of the repository, or stash",
            "them with the one spelling below — it covers every path listed,",
            f"and it is the spelling that reaches {picked}. Run it from the",
            "root named above (`cd` there first; the paths listed are relative",
            "to it):",
            f"  {stash_form(kind)} -- <path>...",
            f"      sets them aside; {POP_BACK}",
        ]
    lines += ["", *override(token)]
    return "\n".join(lines)


def override(token: str) -> list[str]:
    """The closing paragraph both messages end on.

    Only the token is echoed, never the command: echoing it reads as "run this
    again", and re-running a compound line changes what is at stake. What to append
    it to is named, a token binding the whole line.
    """
    return [
        "If discarding all of it is intended, append this to the command you",
        "ran -- the whole line, otherwise unchanged -- and re-run it:",
        f"  # ack:{token}",
        "",
        "The token is an override, not a confirmation that the list above was read.",
    ]


def emit_deny(reason: str) -> None:
    # `json.dumps` escapes every non-ASCII code point, lone surrogates included,
    # so what reaches `print` is pure ASCII and cannot fail to encode whatever
    # the reason interpolated. That is why the encoding hazard this file guards
    # against lives at the token hash and not here.
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


# The refusal of last resort, held as a constant because it interpolates
# nothing: it is emitted exactly when composing a refusal out of the input is
# what failed, so touching the input again is the one thing it must not do.
LAST_RESORT = (
    "Blocked: this command can discard uncommitted work, and the hook failed "
    "while composing the refusal that would have said what is at stake.\n"
    "\n"
    "Denied rather than allowed, because the loss would be irreversible and "
    "nothing here managed to measure it.\n"
    "\n"
    "No override token is offered. A token is derived from the command text, "
    "and deriving one is among the steps that just failed -- so there is no "
    "token to present, and re-running the line unchanged reaches this same "
    "refusal.\n"
    "\n"
    # Every route to this message refuses on the shape of the command, before and
    # without consulting what the tree holds, so a clean tree is refused here just
    # the same. What changes the outcome is giving the hook a command it can read.
    "This refusal does not depend on what the tree holds, so committing or "
    "stashing will not lift it. Re-issue the command in a form that can be "
    "read instead: write the directory out rather than reaching it with "
    "`popd`, a bare `cd`, or `cd -`; drop a `GIT_DIR` / `--work-tree` "
    "override; split a compound line so the git call stands on its own. A "
    "command the hook can follow gets a measured answer -- which names what "
    "is at stake, and carries an override token if it still refuses."
)


def hashable(text: str) -> bytes:
    """Bytes for the token hash, for any str the payload can carry.

    JSON can spell a lone surrogate no UTF-8 encoder takes. `surrogatepass` takes it
    and stays injective, where `replace` would let one command's override authorise
    another.
    """
    return text.encode("utf-8", "surrogatepass")


def bound_to(command: str) -> str:
    """The command as an override token binds it: acks removed, spacing flattened,
    so that where the ack was written stops mattering.
    """
    return " ".join(ACK_RE.sub("", command).split())


def clipped(text: str, limit: int = 400) -> str:
    """A reason fragment cut to something a transcript can carry — git answers an
    unusable invocation with its usage screen.
    """
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def deny_unmeasured(command: str, cwd: str, why: str, posture: str) -> bool:
    """Refuse a covered shape that was not measured. False when already overridden.

    Raises for no input the payload can carry: this function is the refusal, so
    nothing above it catches what escapes, and the handler at the end holds it.

    `posture` is the caller's to write, the backstop firing on text this hook could
    not read, some of which destroys nothing. `cwd` is a starting point for the
    reader, not the tree at stake.
    """
    try:
        token = hashlib.sha256(
            hashable(f"unmeasured\0{bound_to(command)}")
        ).hexdigest()[:16]
        if token in set(ACK_RE.findall(command)):
            return False
        why = clipped(why)
        # Where to run the listing. "The tree that command targets" fails exactly
        # where this branch bites: a `popd` returns to a directory only the shell's
        # stack knows. So the message anchors on what is knowable, dropping the
        # anchor when the payload carried no directory.
        where = (
            f"The shell was in {operand(cwd)} when this was checked; if the line "
            "moves from there (`cd`, `pushd`, `popd`, or a `GIT_DIR` / "
            "`--work-tree` override) the tree at stake is wherever it lands, and "
            "this hook could not follow it.\n\nIn that tree, run "
            if cwd
            else "This hook was given no directory to start from, so the tree at "
            "stake has to come from the command itself. In it, run "
        )
        emit_deny(
            f"Blocked: {why}\n"
            "\n"
            f"{posture}\n"
            "\n"
            + where
            # The same spelling `listing_for` picks, taken from it. Widest kind,
            # because this branch measured nothing and so cannot rule out an
            # ignored file.
            + f"`{listing_for('untracked-all')}` and set aside anything worth "
            f"keeping: {stash_routes()}, each with `-- <path>...`.\n"
            "\n"
            # Said per push, because unlike the measured messages this one offers
            # three spellings at once: a tree holding both a tracked change and an
            # ignored file takes two pushes, and one pop then strands an entry.
            f"Each push makes its own stash entry, so pop once per push: "
            f"{POP_BACK}.\n"
            "\n" + "\n".join(override(token))
        )
        return True
    except BaseException:  # noqa: BLE001
        # The net needs a net: everywhere else a raise becomes a refusal, and this
        # function is the refusal. A fixed string is the only one trustworthy here,
        # every ingredient of a composed one being implicated.
        emit_deny(LAST_RESORT)
        return True


def main() -> None:
    # A payload this hook cannot parse is allowed through: it is not evidence of
    # a discard, and refusing on it would refuse every command the harness sends.
    # After recognition the polarity flips to fail-closed.
    try:
        data = json.load(sys.stdin)
        command = data.get("tool_input", {}).get("command", "")
        if not command:
            return
        payload_cwd = data.get("cwd") or ""
    except Exception:  # noqa: BLE001
        return

    # A command that cannot be tokenized is a different case: the text is in hand
    # and may name a covered verb, which is the signature the count below refuses
    # on. So the failure leaves the list empty and falls through to it.
    try:
        commands = simple_commands(tokenize(prepared(command), comments=False))
    except Exception:  # noqa: BLE001
        commands = []

    recognized = 0
    for idx, (_, argv) in enumerate(commands):
        found = git_args(argv)
        if found is None:
            continue
        rest, relocating_env = found
        # Recognition turns on the verb alone and touches no repository, which is
        # what puts the handler below it: a failure inside is "a covered shape we
        # could not measure" and can deny, a failure here is "a shape we do not
        # cover" and must not.
        if covered_verb(rest) is None:
            continue
        recognized += 1

        try:
            # Settled before any question about where, so that a form which
            # destroys nothing is not refused over a directory or an environment
            # that has no bearing on it.
            if certainly_harmless(rest):
                continue
            if not payload_cwd:
                raise Unmeasurable("the payload carried no working directory")
            if relocating_env:
                raise Unmeasurable(
                    "a GIT_* assignment moves what git resolves, so a "
                    "measurement taken here would describe a different tree"
                )
            resolved = resolve_cwd(payload_cwd, commands, idx)
            if resolved is None:
                continue  # nowhere holding content that existed when this ran
            cwd, wider_than_reach = resolved
            cdirs, _, relocating_global = strip_global_opts(rest)
            for d in cdirs:
                # Through the same gate as a `cd` target: the two ways of saying
                # "run it over there" have to answer alike.
                cwd = str(Path(cwd) / shell_path(d, "`git -C` target"))
            if not Path(cwd).is_dir():
                # `git -C <missing>` exits 128 when the directory really is absent,
                # but `mkdir -p d && git -C d reset --hard` has git resolving
                # upwards into the repository above, as `cd d` does.
                existing = nearest_existing(Path(cwd))
                if existing is None:
                    continue
                cwd, wider_than_reach = str(existing), True
            elif relocating_global is None and not in_repository(cwd):
                # Nothing here to lose: git resolves upwards and stops, so the
                # command exits without touching a file. Checked after the `git -C`
                # folding above, which can move the answer either way.
                #
                # `relocating_global` keeps the answer honest, here being exactly
                # what those options move: answering would reach allow before
                # `stake_for`, the only thing that refuses an unknown global.
                continue
            stake = stake_for(cwd, rest)
            if stake is None:
                continue  # a covered verb, in a form that discards nothing
            kind, pathspecs, narrowing_dropped, reaches_nested = stake
            names, summary, fingerprint = measure(cwd, kind, pathspecs, reaches_nested)
            if not names:
                continue  # nothing at stake; let it run
            payload = "\0".join(
                [
                    git_line(cwd, "rev-parse", "--absolute-git-dir"),
                    bound_to(command),
                    fingerprint,
                ]
            )
            token = hashlib.sha256(hashable(payload)).hexdigest()[:16]
            # Every token on the line, not just the first: a line holding two
            # covered verbs needs one token each, and the tokens accumulate while
            # `search` keeps returning the earliest.
            if token in set(ACK_RE.findall(command)):
                continue  # override presented, and still describes this content
            root = git_line(cwd, "rev-parse", "--show-toplevel")
            # Run the per-file diffs from the root, because the names came from
            # `git diff --name-only`, which reports root-relative paths wherever it
            # ran: passed back in a subdirectory they resolve to `sub/sub/file`,
            # match nothing, and exit 0.
            hunks = hunk_headers(root, names) if kind == "worktree" else []
            emit_deny(
                build_reason(
                    kind,
                    names,
                    summary,
                    hunks,
                    token,
                    root,
                    narrowing_dropped,
                    wider_than_reach,
                )
            )
            return
        except BaseException as exc:  # noqa: BLE001
            # An unmeasurable case still needs an exit, re-running producing the
            # identical refusal.
            #
            # Interpolating `exc` runs an arbitrary `__str__`, and the f-string is
            # built as an argument, so it is evaluated in this frame, outside the
            # refusal's own handler -- hence the try of its own.
            try:
                why = (
                    "this command can discard uncommitted work, and the hook "
                    f"could not determine what is at stake ({exc})."
                )
            except BaseException:  # noqa: BLE001
                why = (
                    "this command can discard uncommitted work, and the hook "
                    "could not determine what is at stake."
                )
            if deny_unmeasured(
                command,
                payload_cwd,
                why,
                "Denied rather than allowed, because the loss would be irreversible.",
            ):
                return
            continue

    # Where the parse fails closed. A covered verb it could not read is answered
    # nowhere above, and silence reaches the caller as permission; every parser gap
    # has the one signature, a covered verb in the text with no recognized call to
    # account for it. It counts, one unread call among read ones having to refuse
    # too. A count that could not be taken is not zero.
    try:
        unread = mentions(command) > recognized
    except BaseException:  # noqa: BLE001
        unread = True
    if unread:
        deny_unmeasured(
            command,
            payload_cwd,
            "this command names a git verb that can discard uncommitted work, "
            "in a form the hook could not read as a call -- so what is at "
            "stake was never measured.",
            "It may well discard nothing: a line that writes a script or echoes "
            "a command reaches here too, because unread text is unread whatever "
            "it turns out to say. Refusing is the only answer that cannot be "
            "wrong in the expensive direction.",
        )
