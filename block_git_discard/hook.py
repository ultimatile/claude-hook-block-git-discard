# PreToolUse hook: bounce a git command that would discard uncommitted work.
#
# `git checkout -- <file>` restores the file from the index and takes every other
# uncommitted change in that file with it, with no warning and nothing for
# `git fsck` to find afterwards. That loss is what this guards.
#
# It does not pattern-match dangerous-looking commands: it runs the same
# read-only query git would and denies on what that query reports.
#
# Where the command's reach cannot be narrowed exactly, the report widens and the
# deny follows the wider report. That is a rule and not a list -- a list would
# have to grow with every such site and would go quiet on whichever one was added
# without it.
#
# The five verbs in COVERED are the ones an agent actually types, which is the
# whole of why the scope is that narrow.
#
# What "cannot be measured" means is narrow too: this guards content that exists
# when the hook decides. A tree that
# is there but hidden from the query is refused. A path that does not exist yet
# is not hidden; it holds nothing of its own, and reading that as "unknown"
# refuses a line while protecting nothing.
#
# "Of its own" is the qualification, and it was learned the hard way: git
# resolves upwards, so a command run in a directory the same line creates still
# reaches the repository above it. `mkdir -p d && cd d && git reset --hard`
# destroyed an enclosing tree on the unqualified reading. The measurement
# therefore falls back to the nearest directory that already exists.
#
# Fail-closed. A PreToolUse hook that cannot make sense of its input normally
# lets the command through:
# being wrong that way costs a redo. This one guards a loss that no redo reaches,
# so it refuses instead. A false positive here costs one override token.
#
# The floor under that: a hook that exits non-zero is reported as a non-blocking
# error and the command then runs, so every path after recognition must reach
# print() rather than raise. That includes the paths that do the refusing --
# `deny_unmeasured` carries its own handler for exactly this, after a command
# holding one unencodable character raised inside the override-token hash and
# turned a decided refusal into a discard. Failures before main() -- import,
# syntax, an unusable interpreter -- cannot be caught from inside this file.
#
# The parse fails closed too. A measurement failure is loud: the hook knows
# which call it could not answer for. A recognition failure is silent -- a
# covered verb this hook cannot read as a call is indistinguishable from a
# shape it does not cover, and silence reaches the caller as permission. Every
# parser gap has that one signature: a covered verb in the text with no
# recognized call to account for it. `main` counts the two and refuses when the
# text names more than the parse read. That count is why the shell parsing here
# stays shallow, and telling a gap from a mention is precisely the parsing this
# hook declines to attempt.
#
# The backstop's own floor: both halves of the call have to be written in the
# text. The name is read off the characters around it, so it need not be a word
# of its own -- `x=$(git reset --hard)` and `cat <(git reset --hard)` are read,
# and so is `$(echo git) reset --hard`, because the word `git` is written down.
# What stays out of reach is a name that is nowhere in the text (`$GIT`), and a
# verb that is the same, which is what a `git co` alias is: resolving either means
# reading the environment or the config the command will run under, and this hook
# reads neither.
#
# So "never under-refuse" is not a guarantee it can make. What it holds to is
# narrower: no shape it can read is under-refused, and a shape it cannot
# read is refused rather than passed.

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

# The override token, read from the raw command string. It cannot be read from
# the token list: the comment carrying it is stripped before tokenizing, so
# `git checkout -- f # ack:...` tokenizes without the token entirely.
ACK_RE = re.compile(r"#\s*ack:([0-9a-f]{16})\b")


# A backslash-escaped newline, which the shell joins into one line before it
# reads anything. shlex does not: it leaves a bare newline, `is_separator` reads
# that as a command boundary, and `git checkout \` + newline + `-- a.txt` arrives
# as `git checkout` with the pathspec dropped -- a whole-tree discard measured as
# a narrowed one, which is to say allowed.
CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\r?\n")

# A command substitution, innermost first. The tokenizer treats `(` and `)` as
# separators, so `git -C $(git rev-parse --show-toplevel) reset --hard` arrives
# split into fragments with the covered verb in none of them. It is one word to
# the shell, so it is collapsed to one here; `$SUBST` keeps the `$`, which marks
# it as a value only the shell can supply, and the existing handling for those
# takes it from there.
SUBSTITUTION = re.compile(r"\$\([^()]*\)|`[^`]*`")

# Characters that glue to a word without being part of it. Stripped off the ends
# of the words following a command name in `mentions`, and what it buys is narrow
# and worth naming exactly: it is the verb abutting a closer. `sh -c 'git reset'`
# and `x=$(git reset)` present the verb as `reset'` and `reset)`, and neither is
# the word `reset` until the closer comes off. The same payloads with an option
# after the verb (`sh -c 'git reset --hard'`) need none of this, because the
# closer lands on the option instead.
#
# The command name needs none of it either: `COMMAND_GIT` below finds the name by
# its neighbours rather than by reducing a word to it.
CLINGING = "'\"()$`\\"

# Characters that may appear inside a command name. `COMMAND_GIT` reads a `git`
# as a name when neither neighbour is one of these, so the name does not have to
# arrive as a whole word -- which is the difference that matters, because
# `x=$(git`, `a$(git` and `<(git` all carry a command name that no amount of
# stripping the ends of a word reduces to `git`. Each of those lines discards a
# worktree, and is a line this hook refuses on the strength of this class alone.
#
# The class is spelled from what a name may hold rather than from the separators
# that end one, and that direction is the whole point. A separator missing from
# such a list would leave a `git` glued to what precedes it and invisible here --
# a call that runs unmeasured. A name character missing from this list splits a
# word that should have stayed whole, and costs one refusal. The cheap mistake is
# the one to be exposed to.
NAME_CHAR = r"A-Za-z0-9_.+\-"

# `/` is treated differently on the two sides, and that asymmetry is the basename
# rule -- the same one `PurePosixPath(...).name` states, spelled as neighbours.
# A `/` in front means the name is path-qualified, so `/usr/bin/git` is still the
# git this hook covers --
# the guard on the left lets it through. A `/` behind means the `git` is a
# directory component and the name is something else, so `git/lfs` and `a/git/b`
# are not read as `git`, exactly as their basenames are not.
#
# The other exclusions come from `NAME_CHAR` on the side the character sits:
# `git-lfs` and `gitk` from the right, `foo.git` and `.gitignore` from the left.
COMMAND_GIT = re.compile(rf"(?<![{NAME_CHAR}])git(?![{NAME_CHAR}/])")

# Characters that end the simple command an inert-subcommand guard belonged to.
# Spelled once because `mentions` reads it at two points -- clearing the guard,
# and bounding the look-ahead -- and two copies drift.
SEPARATORS = ";&|()`"

# The end of the whitespace token a candidate sits in. `mentions` needs it
# because a separator behind the name in the same word still ends the previous
# command, and a scan that stops at the name would not have seen it.
WHITESPACE = re.compile(r"\s")

# The file-descriptor number in front of a redirection, and only when it is
# written against the operator. Adjacency is the whole signal, and it survives
# only here: `2>log` and `2 > log` reach the tokenizer as the same three tokens,
# so a rule applied there has to guess -- and guessing "digit before a
# redirection is a descriptor" throws away the pathspec in
# `git checkout -- 2 > log`, over a repository holding a file named `2`.
FD_PREFIX = re.compile(r"(?:(?<=\s)|\A)\d+(?=[<>])")

# The stand-in `mask_quoted` writes over quoted and escaped characters. NUL,
# because nothing a caller looks for through the mask can match it: it is not
# whitespace, not a digit, not a `#`, not a newline, and not a redirection.
MASK = "\0"


def mask_quoted(text: str) -> str:
    """`text` with every quoted or escaped character replaced by `MASK`, same length.

    One quote model, shared by every rewrite that has to respect quoting. Written
    apart they drift, and a drift here is a rewrite firing inside a quoted
    pathspec: the name it leaves behind is one the repository does not have, the
    measurement narrows to nothing, and nothing at stake is an allow.

    Length-preserving on purpose: a caller runs its own regex over the mask and
    applies the spans it finds to the original text, so no offset arithmetic is
    needed and no caller has to re-derive where a quote began.

    What the shell does, and what this follows: single quotes take a backslash
    literally, double quotes and `$'...'` (ANSI-C) let it escape, and outside
    quotes it escapes whatever comes next. The quote characters and the
    backslashes are masked too, because they are syntax rather than content.

    ANSI-C is tracked rather than folded into the ordinary single quote because
    the two close in different places. In `$'don\\'t'` the `\\'` escapes and the
    word ends after `t`; read as an ordinary quote it closes there and the `'`
    after `t` opens a new one that runs past the end of the line.
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
    """Blank a descriptor number written against a redirection, quoting respected.

    ` 2>` is a redirection between words and part of the filename inside quotes.
    The match therefore runs over the mask, and the span is overwritten in the
    original with as many spaces, which keeps every later match's offsets valid.
    Quoting also settles a digit that only looks unquoted because of what precedes
    it: in `"x"2>log` the shell reads the word as `x2`, and through the mask the
    digit has a masked character in front of it rather than whitespace, so it does
    not match.
    """
    masked = mask_quoted(command)
    out = list(command)
    for found in FD_PREFIX.finditer(masked):
        out[found.start() : found.end()] = " " * (found.end() - found.start())
    return "".join(out)


# git subcommands that do not run a command string handed to them as an argument,
# so a `git <verb>` sitting inside one of these is text and not a call. Without
# this, `git commit -m "block git clean when untracked files exist"` is refused --
# an ordinary commit message, and one this very hook invites people to write.
#
# An allowlist. Listing the subcommands that do run their argument would make
# every omission a hole, and that set is open at
# the dangerous end: `submodule foreach`, `bisect run`, `rebase -x` and
# `-c alias.x='!...'` each run one, and all four were confirmed by execution to
# discard a tree that way. Listing the ones that do not makes every omission a
# refusal instead, which costs a token.
#
# Nothing joins this list on reasoning. Each entry was run with `git <verb>
# --hard` inside its argument and the worktree checked afterwards.
INERT_SUBCOMMANDS = frozenset(
    {"commit", "log", "config", "grep", "tag", "stash", "branch", "show", "notes"}
)


def prepared(command: str, *, mask: bool = True) -> str:
    """The command as the shell's own word-splitting would first see it.

    `mask` collapses command substitutions, and only the tokenizing path wants
    it. The contents of a substitution are executed, so hiding them from the
    mention test is backwards: `echo $(git reset --hard)` discards the tree, and
    masking is the one thing that could hide it. The mention test reads a name off the
    characters around it rather than off word boundaries, so the `git` inside
    `$(...)`, `` `...` `` or `<(...)` is there to be found whether or not the
    bracket begins a word -- it needs none of the protection the tokenizer needs,
    and pays none of its cost.
    """
    # Comments come off before lines are joined, because a backslash inside a
    # comment is ordinary text to the shell -- `echo one # note \` / `echo two`
    # prints both, verified in bash and zsh. Joining first would make the second
    # line part of the comment and delete it, so `git log -1 # note \` followed by
    # `git reset --hard` would arrive as one commented-out line -- nothing left to
    # read, and a reset that still runs.
    command = strip_comments(command)
    command = CONTINUATION.sub(r"\1 ", command)
    if mask:
        previous = ""
        while previous != command:
            previous = command
            command = SUBSTITUTION.sub("$SUBST", command)
    return strip_fd_prefixes(command)


def logical_lines(text: str) -> list[str]:
    """Split on the newlines that end a command, leaving quoted ones in place.

    A newline inside quotes is part of a word — a commit body is written exactly
    that way — so treating it as a command boundary splits one argument into
    several, and the guard in `mentions` is cleared halfway through a message the
    shell never executes. That refused `git commit -am "subject` + blank line +
    `body naming git reset --hard"` while the single-line spelling passed: two
    readings of one inert call.

    Which newlines those are is `mask_quoted`'s answer, not this function's: a
    newline it masked is inside quotes or escaped, and only an unmasked one ends a
    command.
    """
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

    Deliberately a second parser, and a dumber one. It reads the raw text, so
    what made the real parse lose a verb has no purchase on it. Compared in
    `main` against what that parse recognized, a shortfall is a covered verb
    nothing accounted for.

    The name is found by its neighbours rather than by reducing a word to it,
    which is what lets it read a name buried mid-word: in `x=$(git reset --hard)`
    one character in front of the `$(` stops any reduction, and the real parse
    masks the substitution away and recognizes nothing there either.

    A `git` in the arguments of a call that does not run its arguments is
    skipped: see `INERT_SUBCOMMANDS`. That is the only position a covered verb
    goes uncounted in. Everywhere else the look-ahead below counts one anywhere
    before the next separator, an option's value included, so `git diff -S clean`
    and `git blame -L 1,2 reset` are refused while `git log -S clean` is not --
    measured, and the difference is `log` being inert.
    """
    found = 0
    # line by line, because a newline ends a command as surely as a `;` does and,
    # being whitespace, never survives into a word for the separator test below
    # to find. Carried across lines, one `git log -1` above would disarm the guard
    # for everything after it, and `git commit -m "wip"` followed by
    # `sh -c 'git reset --hard'` would go uncounted while the reset runs.
    for line in logical_lines(prepared(command, mask=False)):
        inert = ""
        previous = 0
        for candidate in COMMAND_GIT.finditer(line):
            start = candidate.start()
            # A separator ends the call the guard belonged to. So does a
            # substitution or a subshell, for a different reason: what is written
            # inside one runs, so `git commit -am "$(git reset --hard)"` discards
            # the tree before the commit it is quoted into has begun.
            #
            # The evidence is the span since the last candidate, because neither
            # name need be a whole word. It runs to the end of this candidate's
            # own token rather than to the name, so a separator sitting behind the
            # name in the same word still clears the guard: `git log -1 git)`
            # followed by a covered verb is two commands, and the `)` is the only
            # thing that says so.
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
                # means this crude split lost the boundary rather than found a
                # subcommand nothing here covers: splitting on whitespace tears a
                # quoted option value in two -- `-c user.name="John Doe"` becomes
                # `-c`, `user.name="John`, `Doe"` -- and `-c` consumes only the
                # first half, so the verb lands one position further along than
                # the subcommand test looks.
                #
                # So look ahead instead of giving up: a covered verb before the
                # next separator is counted. That over-counts `git status --short
                # reset` and `git bisect reset`, which discard nothing and which an
                # agent does type -- one refusal each, and the alternative is the
                # fail-open above.
                for j, raw in enumerate(raws):
                    if any(ch in raw for ch in SEPARATORS):
                        break
                    if words[j] in COVERED:
                        found += 1
                        break
    return found


def strip_comments(command: str) -> str:
    """Drop `#`-comments the way the shell delimits them, not the way shlex does.

    shlex ends a token at a `#` anywhere inside it, so `f#1.txt` arrives as `f`:
    a pathspec matching nothing, a measurement finding nothing at stake, and a
    real discard let through. The shell instead opens a comment only at a `#`
    that starts a word, and never inside quotes.

    Quoting is the half a regex cannot hold: cutting at the `#` in
    `git checkout -- 'x #1.txt'` leaves an unbalanced
    quote, tokenizing falls back to a whitespace split, and the pathspec becomes
    `'x`. Escapes matter as much and for the same reason -- `a\\ #b.txt` is one
    word, the shell hands git `a #b.txt`, and the `#` in it starts no comment.

    Read through `mask_quoted`, which settles both halves at once.
    """
    masked = mask_quoted(command)
    out: list[str] = []
    prev = " "  # the start of the line counts as a word boundary
    skipping = False
    # `strict` states the invariant `mask_quoted` holds -- one character out per
    # character in -- so a disagreement stops here instead of silently truncating
    # the command. It is not a loud failure at run time: this runs inside `main`'s
    # pre-recognition handler, which returns and therefore allows, and which the
    # `mentions` backstop is not reached from. So the length is the mask's
    # invariant to keep, and this only makes a break in it visible under test.
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


# Subcommands the hook itself may run. Checked at the single subprocess entry
# rather than asserted in a comment, so an edit reaching for a mutating query
# fails loudly instead of quietly mutating the repository this hook protects.
READ_ONLY = frozenset({"diff", "ls-files", "rev-parse"})

# Characters the shell expands after this hook has seen the text, so a pathspec
# containing one cannot be forwarded to git as written. Globs are excluded on
# purpose: git's own glob matches at least as much as the shell's, so forwarding
# one over-detects rather than under-detects.
UNEXPANDED = re.compile(r"[{}$`~]")

# For a directory the glob exemption inverts, so it gets its own pattern. A
# pathspec carrying `*` is handed to git, which matches at least as widely. A
# directory carrying `*` is one the shell picks and the hook cannot: testing the
# written spelling finds nothing there and reads that as nothing at stake.
GLOB = re.compile(r"[*?\[]")


def shell_path(raw: str, what: str) -> Path:
    """`raw` as a path this hook can test, or `Unmeasurable` if only the shell can.

    `~` resolves here, this hook and that shell sharing a home. Nothing else
    does, and every caller that turns a written word into a directory goes
    through this -- `cd`, `pushd`, and `git -C` alike. Checked at each site
    instead, the same question is answered several times and the answers drift:
    one site without the check lets `git -C $REPO reset --hard` through while
    `cd $REPO && git reset --hard`, which asks exactly the same thing, is
    refused.
    """
    path = Path(raw).expanduser()
    if UNEXPANDED.search(str(path)) or GLOB.search(str(path)):
        raise Unmeasurable(f"the {what} is resolved by the shell after this runs")
    return path


class Unmeasurable(Exception):
    """A recognized shape could not be measured.

    Reaching `main`'s handler it becomes a deny, unless the command already
    carries that handler's own override token. Three callers catch it on purpose
    before it gets there, each because the failure it names is an answer at that
    site rather than a gap in one:

    - `in_repository`, where a rev-parse that fails means there is no repository
      here, which is the question being asked
    - `is_ref`, where a failed rev-parse is the negative answer
    - `hunk_headers`, where a per-file diff that fails costs only that file's
      display line, and the refusal it would otherwise cause has already been
      decided on without it

    Anywhere else, a catch would be turning "could not measure" into "nothing at
    stake", which is the fail-open this class exists to prevent -- so a fourth
    site is a change of policy, not a change of code.
    """


def git(cwd: str, *args: str) -> str:
    if not args or args[0] not in READ_ONLY:
        raise Unmeasurable(f"hook attempted a non-read-only git query: {args!r}")
    try:
        proc = subprocess.run(
            # `core.quotepath=false` because git otherwise C-quotes any path
            # holding a non-ASCII byte: `é.txt` is reported as `"\303\251.txt"`.
            # That spelling reaches the reason as a name the reader cannot pass
            # back to git, and the untracked fingerprint stats it, misses, and
            # marks the file `gone` -- which pins the fingerprint to the file
            # set and lets an override token outlive the content it described.
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
    """A query whose whole answer is one line, with the newline off.

    Every `rev-parse` whose output this hook reads answers that way -- the two
    that only test an exit code, `in_repository` and `is_ref`, call `git`
    directly and want none of this. At the three callers that do read it, the
    strip is not cosmetic. As a path it feeds `Path`, where a
    trailing newline makes a directory that compares unequal to the same
    directory and exists nowhere; one of those paths goes on to be a subprocess
    `cwd`, which fails outright. Spliced into an override token's payload it
    changes the token, so a refusal would mint one hash and the re-run would
    look for another.
    """
    return git(cwd, *args).strip()


# Commands that precede the real one without changing what it is. A wrapper's own
# options are not stepped over -- that needs per-wrapper knowledge, and the
# prefixes that actually show up here take none. `sudo -u x git reset --hard` is
# therefore not recognized as a call at all -- it lands on the mention test in
# `main` and is refused there, which is what makes leaving the per-wrapper
# knowledge out affordable.
WRAPPERS = frozenset(
    {"env", "sudo", "doas", "nice", "nohup", "time", "command", "stdbuf"}
)
ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def git_args(argv: list[str]) -> tuple[list[str], bool] | None:
    """(arguments after `git`, a GIT_* assignment precedes them), or None for non-git.

    `argv[0]` alone is too narrow: `LC_ALL=C git ...` and `env git ...` are
    everyday idioms, and reading them as "not a git call" would switch the guard
    off exactly when someone reaches for one. Scanning the whole command is too
    wide in the other direction -- `echo git reset --hard >> notes.md` names a
    covered verb, and the verb check downstream would let that reach a deny. So
    only an assignment or a known wrapper is stepped over; anything else in
    command position ends the search.

    Stepping over an assignment is what makes the second half of the return
    necessary: a `GIT_*` assignment moves the tree, the index or the config the
    measurement depends on, and read as ordinary prefix noise it leaves the hook
    measuring the directory it was handed while the command works on another one.

    The test is the `GIT_` prefix rather than the variables that matter, because
    git's environment surface grows and a list goes quiet on whichever member was
    not added to it. Erring wide costs an override on `GIT_PAGER=cat`.
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

    A here-document's body is not set apart. Newlines are separator tokens, so
    `cat > setup.sh <<'EOF'` / `git reset --hard` / `EOF` yields the middle line
    as a simple command, and a script that only mentions a discard is measured
    as though it performed one -- so it is refused wherever the tree holds
    something to lose, and allowed where it does not.

    That false positive is accepted rather than parsed away. Recognizing the
    body needs a delimiter rule, and every attempt at one here misread something
    -- a here-string (`<<<`) takes a word rather than a delimiter, a `<<-` marker
    travels attached to the delimiter, an unterminated body swallows the rest of
    the line -- with each misread showing up as a covered verb this hook never
    saw. The cost of the refusal is one override token on a script-writing line;
    the cost of the misreads was the guard switching itself off.
    """
    out: list[tuple[str, list[str]]] = []
    sep, cur = "", []
    skip_target = False
    for tok in tokens:
        if skip_target:
            skip_target = False
            continue
        if is_separator(tok):
            # A redirection is not a command boundary, and reading it as one is
            # how `git clean -fd 2>/dev/null` came to be allowed: the line split
            # after `2`, which stayed behind as clean's pathspec and narrowed the
            # measurement to nothing. `git 2>&1 reset --hard` lost the verb the
            # same way. So the operator takes its target with it and the command
            # carries on. Its file descriptor is already gone -- `prepared`
            # removes that, where being written against the operator is still
            # visible and a digit standing alone is still a pathspec.
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
    """A simple command with its brace-group keywords peeled off.

    `{` and `}` are words to the tokenizer, deliberately: making them punctuation
    would split `{}` in `find -exec ... {} \\;` and a `{a,b}` brace expansion. So
    `{ cd elsewhere; }` arrives with `{` sitting in command position, where the
    `cd` behind it goes unread -- and a brace group, unlike a subshell, runs in
    the current shell, so that `cd` is one the git command afterwards really does
    inherit. Only a lone brace is peeled; `{}` is one token and stays whole.
    """
    sep, argv = command
    if argv and argv[0] == "{":
        argv = argv[1:]
    if argv and argv[-1] == "}":
        argv = argv[:-1]
    return sep, argv


# git's own options that take their value as a separate following token. That
# spelling is what has to be enumerated, because consuming only the flag there
# leaves the value sitting in the subcommand position, where it hides the verb
# and the whole invocation passes unexamined. The long ones also accept the
# attached `--opt=value` form; `-C` and `-c` do not (`git -C=/tmp status` exits
# 129 on `unknown option`), which is why the `=` test below only ever has to
# spare a long option from consuming a second token.
#
# The list is closed, and safe to treat as closed: an option git does not know
# makes git print `unknown option` and exit 129 before the subcommand runs, so a
# spelling missing from here names a command that never executes. Measured
# against git 2.50: `--exec-path` is deliberately absent, since bare it prints the
# path and exits rather than consuming what follows.
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
# global position is treated as moving the ground under the measurement.
#
# The direction is the whole point, and it is the one `git_args` already takes
# for the `GIT_` environment prefix. git's global surface grows, so a list of the
# globals that matter goes quiet on whichever was added last: `--icase-pathspecs`
# makes the pathspec `readme.MD` reach `README.md` at run time, while this hook's
# `git diff --name-only -- readme.MD` matches nothing and reports an empty tree
# at stake. A list of the globals that are harmless goes quiet in the other
# direction, and costs one override token.
#
# `-C` and `-c` are here because neither is stepped over blindly: `-C` is
# collected and its directories resolved, and `-c` is read for the one setting
# that waives git's own refusal. What the rest of a `-c` carries is config, which
# this hook does not read.
GIT_INERT_GLOBALS = frozenset(
    {"-C", "-c", "-p", "-P", "--paginate", "--no-pager", "--no-optional-locks"}
)


def strip_global_opts(argv: list[str]) -> tuple[list[str], list[str], str | None]:
    """Peel git's own options off the front.

    Returns (`-C` dirs, subcommand argv, unknown). `unknown` is the first global
    option here that is not in `GIT_INERT_GLOBALS`, or None -- the token itself
    rather than a flag, so the caller can name it. Anything outside that set may
    move what the subcommand acts on or how its pathspecs match, either of which
    makes a measurement taken here describe something other than what runs.
    This function never raises: the caller decides, because
    refusing has to happen after the verb is recognized, not before -- otherwise
    the refusal is indistinguishable from "not a shape we cover" and the command
    would be let through.

    git's globals are matched by exact spelling on purpose: unlike a
    subcommand's options they do not go through parse-options, so git rejects
    `--git-di=<path>` outright and an abbreviation-tolerant match here would
    accept what git does not.
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


# The long options this hook expands abbreviations for. `--orphan` is tested for
# elsewhere and is deliberately not here -- `abbreviates` says why -- so adding a
# newly tested option to this set is the usual move, not the invariable one.
#
# A subcommand's options go
# through git's parse-options, which accepts any unambiguous abbreviation, so
# `git reset --har` really does reset and `git clean --forc -d` really does
# delete. Matching by equality alone reads those as unflagged and lets them
# through -- the one failure this hook exists to prevent.
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
# spelling only. An abbreviation is expanded against `LONG_OPTS`, a union
# over all five verbs rather than any verb's real option table, so it can name a
# flag the verb does not have.
# `git switch --p -f other` is the case that fixes the rule: `--p` expands to
# `--patch`, which `switch` does not have, so reading it as harmless cancels the
# measurement while git resolves `--p` to `--progress` and discards the tree.
#
# Over-expanding toward a destructive flag only ever adds a refusal, so that
# direction keeps its abbreviations. Toward a harmless one it removes the
# measurement entirely, which is the whole guard.
HARMLESS_LONG = frozenset({"--patch", "--dry-run", "--staged"})

# Short options that take a value, across the covered verbs: `-e` is clean's
# exclude, `-s` restore's source, and `-b`/`-B`/`-c`/`-C` name a new branch. A
# bundle ends at the first of them, because what follows is the value.
#
# Reading past it turns the value's letters into flags, and the letters that
# happen to appear there are the dangerous ones: `-e"*.pyc"` yields a `p` that
# reads as `--patch`, sending a real `clean -f` down the "interactive, no
# collateral" branch, and `-fdxenode_modules` yields an `n` that reads as a dry
# run. It goes wrong in the other direction too -- `-bfix-thing` yields an `f`
# that reads as force, refusing a branch creation that keeps the tree whole.
SHORT_VALUE = frozenset("esbBcC")

# How many at-stake paths the reason prints before it stops naming them.
AT_STAKE_LIMIT = 50


def expand_flags(flags: list[str]) -> set[str]:
    """Flag names named here, bundled short ones split and long ones un-abbreviated.

    `-SW` -> {'S', 'W'}. A long flag drops any attached value, so `--exclude=pat`
    lands as `--exclude`; without that the whole token misses every `in flags`
    test and a check written for `--exclude` fails to see the attached spelling
    git accepts just as readily.

    An abbreviated long flag expands to every entry of `LONG_OPTS` it prefixes
    except the harmless ones, which take their full spelling. Resolving an
    abbreviation the way git does would mean carrying git's per-subcommand option
    table; expanding against a union of all five verbs' flags needs none of it,
    but it can name a flag the verb in hand does not have. Where that lands on a
    destructive flag it adds a refusal; where it lands on a harmless one it
    cancels the measurement, so only the first direction is allowed to guess.

    A short bundle stops at the first `SHORT_VALUE` letter, that letter included:
    everything after it is the option's value, and the letters in a value are not
    flags.
    """
    return set(flag_occurrences(flags))


def flag_occurrences(flags: list[str]) -> list[str]:
    """The same names `expand_flags` reports, in order and keeping repeats.

    `clean` reads its force twice as a different instruction than once -- `-ff`
    reaches a directory holding its own `.git`, `-f` never does -- so a caller
    asking that question cannot use a set. It shares this body rather than
    counting `f` in the raw tokens, because the letters after a `SHORT_VALUE`
    letter belong to that option's value: `-efpat` carries the pattern `fpat`
    and no force at all, and a count taken over the token would read one.
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
    """The option tokens in `opts`, with the `--` separator left out.

    Here for the reason `given` and `forced` are here: `certainly_harmless` and
    `stake_for` have to read the same flags, and a selection written twice is a
    step the two can diverge on. `forced_twice` takes the same tokens for its own
    count.

    `--` is a separator and not an option, and it is dropped here rather than at
    each caller for the same reason.
    """
    return [a for a in opts if a.startswith("-") and a != "--"]


def certainly_harmless(argv: list[str]) -> bool:
    """True when a covered verb is in a form that cannot destroy anything.

    Decided without touching the repository, and checked before anything about
    where the command runs. Resolving first would refuse `cd $x && git clean -n`
    or `GIT_DIR=/x git clean -n` -- neither of which this hook can place, and
    neither of which destroys anything wherever it lands. A dry run is a dry run
    in every directory, so the form is settled before the place. Only the
    ref-versus-path question genuinely needs the repository, so everything
    decidable without it is settled here.
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
        # Full spelling only for `--orphan`, unlike the forced arm in
        # `stake_for`: this side answers "measure nothing", so a wrong guess
        # here drops the measurement rather than widening it.
        return not forced(flags) and (bool(flags & {"b", "B"}) or "--orphan" in flags)
    return False


# What git reads as false in a boolean config value. Measured: `=false`, `=0`,
# `=no` and `=off` each let `git clean -d` delete untracked content, while
# `=true` and a bare `-c clean.requireForce` (no value) leave git refusing.
CONFIG_FALSE = frozenset({"false", "0", "no", "off"})


def force_waived(argv: list[str]) -> bool:
    """Whether `-c clean.requireForce=<false>` rides in the command's own argv.

    `git clean` refuses without `-f` -- unless that setting says otherwise, and
    then a bare `git clean -d` deletes untracked directories. The setting is a
    token in the argv this hook already tokenizes, so reading it here costs no
    query and no normalization.

    What stays uncovered is the same setting in a config file. Reading that needs
    a repository query this function does not make.

    Only the separate spelling is checked, because only the separate spelling
    exists: `git -cclean.requireForce=false` exits 129 as an unknown option. The
    key is compared case-insensitively, as git compares it -- `requireforce`
    waives the refusal just as `requireForce` does.
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
    """Whether any long flag present could be an abbreviation of `full`.

    Subcommand options are parse-options, so git accepts any unambiguous prefix
    -- `--orph` really does create an orphan branch. Callers use this only where
    guessing wrong widens the measurement, never where it narrows one: the
    asymmetry `expand_flags` describes at length, applied to a flag whose full
    spelling `LONG_OPTS` deliberately does not carry.
    """
    return any(f.startswith("--") and len(f) > 2 and full.startswith(f) for f in flags)


def given(flags: set[str], short: str, long: str) -> bool:
    """Whether a flag is present, under either spelling git accepts for it.

    Separate from `forced`, which keeps its own body: `--force` is not a single
    long spelling but a set, because `switch` says `--discard-changes` for the
    same thing. A flag whose spellings are one-to-one belongs here; one whose
    long form is a set of synonyms does not fit and is better named.
    """
    return short in flags or long in flags


def forced(flags: set[str]) -> bool:
    """Whether these flags waive git's own refusal to act on a dirty tree.

    One definition, because the two callers have to agree: `certainly_harmless`
    deciding a form is safe while `stake_for` reads it as a discard would let the
    discard past without ever being measured. Written apart, they drift, and a
    branch missing `--discard-changes` is exactly that drift.
    """
    return "f" in flags or bool(flags & {"--force", "--discard-changes"})


def forced_twice(opts: list[str]) -> bool:
    """Whether `clean`'s force is given twice, in any spelling that repeats it.

    Measured against git: a directory holding its own `.git` is removed by
    `-ff` together with either `-d` or a pathspec that names it, and by nothing
    weaker -- `-f`, `-fd`, `-f .` and `-f nested` all leave it whole, and `-ff`
    on its own does too. The pair is what `measure` needs, because such a
    directory is the one stake git will not enumerate: `ls-files --others`
    reports it as a single `nested/` entry and refuses to look inside.

    Takes the raw option tokens rather than a flag set, since a set cannot say
    whether force arrived once or twice.
    """
    names = flag_occurrences(option_tokens(opts))
    return sum(1 for n in names if n in ("f", "--force")) >= 2


def operands(opts: list[str], value_taking: set[str]) -> list[str]:
    """Non-option tokens from before `--`, skipping any option's separate value.

    `--source HEAD~1 a.txt` puts a ref where a naive scan reads a pathspec; the
    ref then carries `~`, which trips the unexpanded-pathspec check and collapses
    the measurement to the whole tree. The attached `--source=HEAD~1` spelling
    never had the problem, so without this the two spellings disagree.

    `value_taking` holds flag names as `expand_flags` reports them, so an
    abbreviation git accepts -- `--sou HEAD~1` -- consumes its value here too.

    Only the separate spelling consumes a following token. `--source=HEAD~1` and
    `-sHEAD~1` carry their value inside the token, and skipping after them eats
    the pathspec instead: the measurement then narrows to nothing, finds nothing
    at stake, and lets a real discard through. That is why a long option is
    tested for `=` and a short one for length -- `-s` is the flag, `-sHEAD~1` is
    the flag with its value already attached.
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

    With `--` present the answer is exactly what follows it. Without, git resolves
    each operand as a ref first and only falls back to a path, so the same order
    is used here; an invocation whose operands are all refs is a branch switch,
    which preserves uncommitted work and is not this hook's business.
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
    # from `kind` because it is a property of the flags rather than of the query:
    # two invocations asking `ls-files` the very same question differ on it.
    reaches_nested: bool


def widened(kind: str, reaches_nested: bool = False) -> Stake:
    """A stake measured without the narrowing the command itself carries.

    Every site that cannot forward a narrowing to git funnels through here, so
    the fact that the report will name more than the command reaches is decided
    once and travels with the stake. The reason says so when it is set: a caller
    reading an untouched file on the at-stake list has no way, from the list
    alone, to tell an over-wide report from a hook that is simply wrong.
    """
    return Stake(kind, [], True, reaches_nested)


def narrowed(kind: str, paths: list[str], reaches_nested: bool = False) -> Stake:
    """A stake narrowed to the pathspecs the command carries, where it can be.

    A pathspec `UNEXPANDED` matches is dropped rather than guessed at:
    over-detecting costs one extra round trip, forwarding a spelling git reads
    differently costs the work itself.

    Widening keeps `reaches_nested` as it arrived: the command still carries the
    pathspec that reaches such a directory, and the wider list is a superset of
    what the narrowing would have named.
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
            # A forced switch rewrites the whole worktree and takes no pathspec,
            # so there is nothing to narrow to. Unforced, git itself refuses to
            # lose work. `-C` is force-create: it moves a branch label and leaves
            # the tree alone, so reading it as force would deny every branch
            # creation on a dirty tree.
            return narrowed("worktree", []) if force else None
        # The operand of these is the new branch's name, not a pathspec, so it
        # must not reach `paths_of` -- an unborn branch resolves as no ref, and
        # reading it as a path narrows the measurement to a file that does not
        # exist and reports nothing at stake. Unforced, branch creation keeps the
        # working tree; forced, it discards all of it. Both measured.
        #
        # `--orphan` belongs in this set for that reason: leave it out and
        # `git checkout -f --orphan fresh` goes straight through, taking the
        # whole worktree with it.
        names_a_branch = bool(flags & {"b", "B"}) or "--orphan" in flags
        if force and (names_a_branch or abbreviates(flags, "--orphan")):
            # An abbreviation may guess here and nowhere else. Guessing wrong on
            # this side widens to the whole worktree, which a forced checkout
            # reaches anyway; guessing wrong on the unforced side below would
            # answer "nothing at stake" for `--ou`, which is far more likely to
            # be `--ours` -- and `--ours` does write the worktree.
            return narrowed("worktree", [])
        if names_a_branch:
            return None
        paths = paths_of(cwd, args)
        if force:
            # `-f` waives git's own refusal; it does not widen what the command
            # reaches. With a pathspec present the reach is still that pathspec,
            # so measuring the whole tree would deny `git checkout -f -- a.txt`
            # over an unrelated dirty `b.txt` -- a file the command never opens.
            # Without one the target is the whole worktree, and `[]` says so.
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
        # Without `-d` and without a pathspec, clean does not descend into an
        # untracked directory: it removes untracked files sitting beside tracked
        # ones and leaves the directory whole. `ls-files --others` recurses
        # regardless, so listing its output would name `newdir/j.txt` for a bare
        # `git clean -f` that leaves `newdir/` exactly as it found it.
        #
        # A pathspec that names the directory removes it, `-d` or no `-d`:
        # measured, `git clean -f newmodule` and `git clean -f .` both print
        # `Removing newmodule/`. That is why the pathspec reaches `measure`
        # below, which drops the collapse whenever one is present -- what is
        # under a matching pathspec is the same untracked content this hook
        # already refuses, and only the listing was hiding it.
        deep = "-deep" if "d" in flags else ""
        kind = f"untracked{ignored}{deep}"
        twice = forced_twice(opts)
        # `-e <pattern>` narrows what clean removes, and the report is widened
        # rather than following it. `ls-files` does take the same `--exclude`, so
        # this is a cost decision, not an impossibility: forwarding it means
        # collecting a repeatable option's values across `-e p`, `-epat` and
        # `--exclude=pat`. Widening costs a wider list, which `widened` marks and
        # the refusal states. Both spellings reach the test below:
        # expand_flags strips an attached value, so `--exclude=pat` reads as
        # `--exclude`.
        if given(flags, "e", "--exclude"):
            # `reaches_nested` on the force alone here, because this arm does not
            # read the operands and so cannot say whether a pathspec is among
            # them. Over-refusing a `-ff -e pat` that carries neither a pathspec
            # nor `-d` costs one refusal with an override; guessing the other way
            # costs the repository.
            return widened(kind, twice)
        paths = after_ddash if after_ddash is not None else operands(opts, set())
        return narrowed(kind, paths, twice and bool(deep or paths))

    return None


LS_SELECT = {
    "untracked": ["--others", "--exclude-standard"],
    "untracked-all": ["--others"],
    "untracked-ignored-only": ["--others", "--ignored", "--exclude-standard"],
}

# `--directory` collapses a wholly-untracked directory to its own name, so what
# is left un-collapsed is exactly the set a bare `clean` without `-d` removes.
# `measure` uses it only for that shape: the `-deep` kinds drop it because `-d`
# reaches the whole recursive listing, and a command carrying a pathspec drops it
# because a pathspec naming a directory removes it whether or not `-d` is there.
#
# A `/`-terminated entry survives the collapse for two unrelated reasons, so it
# cannot be read as one: the collapse produced it, or it is a directory holding
# its own `.git`, which `ls-files` reports that way with and without this option
# because git will not descend into another repository. `measure` keeps such an
# entry when the flags reach it and drops it otherwise; `forced_twice` is what
# separates the two.
LS_COLLAPSE = ["--directory", "--no-empty-directory"]

# Every `git diff` this hook runs carries these, because `diff.relative=true` in
# a user's config silently rewrites what the command answers: it reports only
# what lies under the current directory, and reports it relative to that
# directory. Run from a subdirectory the measurement then misses every change
# above it, finds nothing at stake, and lets the discard through -- the exact
# failure this hook exists to prevent, arriving without a symptom. It also
# breaks the frame the reason promises, since the untracked half is pinned to
# the repository root by `--full-name`.
# `--diff-filter=d` (lower case excludes) drops a path whose only change is that
# it was deleted from the worktree. Restoring one puts content back rather than
# taking any away: what returns comes from the index, and whatever the file held
# before the delete was already gone when the delete happened. Counting it as at
# stake refuses `git checkout -- a.txt` after `rm a.txt` -- a command that only
# undoes the removal -- and every such refusal trains its reader to reach for the
# override token.
DIFF_FRAME = ("--no-relative", "--diff-filter=d")


# How many files under a directory-shaped stake the fingerprint reads before it
# stops. Such a directory holds a whole repository, and the walk runs inside a
# PreToolUse hook, so the worst case needs a bound.
TREE_MARK_LIMIT = 2000


def tree_mark(path: Path) -> str:
    """Size and mtime of the files under `path`, as one string.

    A directory's own `stat` does not move when a file inside it is rewritten, so
    a token bound to that would keep unlocking a tree whose content has since
    changed -- the same failure the per-file marks below exist to prevent.

    Sorted at every level, because the mark has to come out identical on the run
    that issues the token and the run that presents it. `os.walk` does not follow
    symlinks, so a link pointing at an ancestor cannot spin here.

    The walk stops at `TREE_MARK_LIMIT` files and records that it did, which
    keeps the result reproducible while bounding the cost. Content past the cap
    is outside what the token is bound to.
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
    """The non-empty entries, first occurrence only, order kept.

    `-z` output ends with a trailing NUL, so a split leaves one empty entry that
    is not a path. And `diff --name-only` names an unmerged path once per stage it
    compares, so one conflicted file arrives twice: the count then reads
    `2 file(s)` for a single file, its hunks print twice, and the doubled length
    reaches `AT_STAKE_LIMIT` at half the real number of files.
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

    The summary is a diffstat for the worktree kind and empty for the untracked
    ones, which have no stat to show.

    The fingerprint is what the override token is bound to, and it has to change
    whenever the content at stake changes. A `--stat` summary does not: editing a
    line leaves `a.txt | 2 +-` byte-for-byte identical, which would keep an old
    token valid over different content. The worktree kind uses the raw patch
    instead -- which also covers binaries, whose `index <old>..<new>` line moves
    with the content even though no hunk text is produced. The untracked kinds
    have no patch, so they use each file's size and mtime.
    """
    # Whether the pathspecs could be forwarded at all was settled in `stake_for`,
    # which is where the widening travels from. What arrives here is already
    # either the command's own narrowing or nothing.
    tail = ["--", *pathspecs] if pathspecs else []
    if kind == "worktree":
        # `-z`, and NUL rather than newline, because a name carrying `"`, `\`, a
        # tab or a newline is C-quoted whatever `core.quotepath` says -- that
        # setting only covers non-ASCII bytes. Quoted, the name reaches the reason
        # in a spelling git will not take back: `hunk_headers` forwards it as a
        # pathspec, matches nothing, and the hunk list empties with nothing to
        # notice. A name holding a newline also splits into two entries under
        # `splitlines`, so the count goes wrong as well.
        names = dedup(
            git(cwd, "diff", *DIFF_FRAME, "--name-only", "-z", *tail).split("\0")
        )
        if not names:
            return [], "", ""
        # `--stat-count` because the summary is per-file too: capping the path
        # list while letting this one run to 5000 lines moves the size problem
        # rather than solving it. git prints `...` and keeps the totals line, so
        # the scale still reaches the reader.
        #
        # Once the path list is itself capped, the stat is cut to one path line
        # and the totals: the reason already printed those paths, and repeating
        # all of them with a churn column beside them spends the budget twice on
        # one list. `1` and not `0`, which git reads as no limit at all.
        stat_count = 1 if len(names) > AT_STAKE_LIMIT else AT_STAKE_LIMIT
        summary = git(
            cwd, "diff", *DIFF_FRAME, "--stat", f"--stat-count={stat_count}", *tail
        ).rstrip("\n")
        return names, summary, git(cwd, "diff", *DIFF_FRAME, *tail)
    # `ls-files` takes the same `-- <pathspec>` tail, so a narrowed clean stays
    # narrowed here; without it `git clean -fd build` reports files under paths
    # it will never touch.
    #
    # `--full-name` because `ls-files` otherwise reports paths relative to the
    # current directory, while the worktree branch's `diff --name-only` reports
    # them relative to the root. The reason names one frame for both lists, so
    # the two have to agree or the untracked half sends the reader to a path
    # that resolves nowhere.
    deep = kind.endswith("-deep")
    select = LS_SELECT[kind.removesuffix("-deep")]
    # Collapsing is right only for the shape that leaves untracked directories
    # whole, which is a clean with neither `-d` nor a pathspec. With a pathspec
    # the recursive listing is what the command reaches: `git clean -f newmodule`
    # removes `newmodule/impl.py`, and the collapsed `newmodule/` entry named it
    # as one thing that then read as out of reach.
    collapse = [] if deep or pathspecs else LS_COLLAPSE
    names = [
        ln
        for ln in dedup(
            git(cwd, "ls-files", "--full-name", "-z", *select, *collapse, *tail).split(
                "\0"
            )
        )
        # A trailing `/` is either a directory the collapse stood in for -- whose
        # files the command leaves alone, so naming it would name content out of
        # reach -- or a directory holding its own `.git`, which git reports this
        # way whatever options it is given and which `-ff` removes outright,
        # history included. `reaches_nested` is the flags' answer to which one is
        # in front of us, so it decides whether the entry counts.
        if reaches_nested or not ln.endswith("/")
    ]
    # The fingerprint has to move when the content moves, not only when the file
    # set does: a token issued for one body must stop matching once that body has
    # been rewritten. Size and mtime are what is reachable without reading every
    # byte of every untracked file.
    #
    # Resolved against the repository root, because `--full-name` above reports
    # root-relative names. Joining them onto `cwd` instead works only when the
    # two coincide: from a subdirectory every stat misses, every file marks
    # `gone`, and the fingerprint stops depending on content at all -- so an
    # override token issued once keeps unlocking whatever the file is rewritten
    # to hold.
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
# `source` and `.` are deliberately not here. They read a file, so what they run
# resolves at run time, and refusing every one of them taxes
# `. .venv/bin/activate && ...`, a line agents type constantly. A `cd` inside such
# a file is not followed; that is the cost.
RUNS_TEXT = ("eval",)

# `command`'s options are a closed set, which is what makes them safe to read
# rather than refuse: POSIX gives it `-p`, `-v` and `-V`, and only `-p` still runs
# the command. `builtin` takes none.
WRAPPER_OPTS_THAT_RUN = ("-p",)
WRAPPER_OPTS_THAT_REPORT = ("-v", "-V")


def unwrapped(argv: list[str]) -> list[str]:
    """`argv` with any `builtin` / `command` prefix peeled off.

    Recognizing the move by the first word alone stepped over every one of these
    spellings, and each leaves the shell in the new directory: the measurement was
    then taken in a tree the git call never ran in, which reports nothing at stake.

    A reporting option comes back unpeeled, which the caller reads as "not a
    directory change" -- `command -v cd` says where the name resolves and runs
    nothing, so following it would read a move that does not happen. An option
    outside the closed set is refused instead of skipped: an option whose
    value-taking is guessed wrong leaves the wrong word in the command position,
    and the move is missed either way.
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
    glued into one token: `)&&` closes a subshell and then chains, `&&(` chains
    and then opens one. Grouping does not weaken the guard -- a failure before it
    still stops what follows -- so parentheses are dropped before the comparison,
    as is a newline directly after the `&&`, which continues the same conditional
    onto the next line. What is left has to be the `&&` itself: a run carrying a
    `;`, a `|` or a lone `&` reaches the git call on a branch where the `cd` may
    not have run, which is the ambiguity the caller refuses.
    """
    return sep.translate(str.maketrans("", "", "()\n")) == "&&"


def nearest_existing(path: Path) -> Path | None:
    """The closest ancestor of `path` that exists and sits inside a repository.

    A directory this line has still to create holds nothing now, but git resolves
    upwards out of it, so what a command run there can reach is whatever
    repository encloses the nearest directory that does exist. None when there is
    no such directory, or when it is in no repository -- there, whatever the line
    puts at `path` is either a repository of its own, whose content the line just
    created, or not a repository at all, and git stops.

    Shared by the two spellings of "run it over there", `cd` and `git -C`, which
    have to answer alike: the same intent written two ways, decided two ways, is
    a fail-open behind whichever way is not looked at.
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
    """(where `commands[idx]` runs, whether that is wider than its reach).

    None when nowhere holding protected content.

    Applies the directory changes from the simple commands preceding it. `pushd`
    moves the shell exactly as `cd` does and is followed the same way; reading
    only `cd` leaves `pushd <other-repo> && git reset --hard` measured against a
    directory the command never ran in, which reports nothing at stake. `popd`, a
    bare `pushd` and `cd -` land wherever the shell's own stack or `OLDPWD`
    points, which the payload does not carry, so they are refused rather than
    guessed at. So is anything after `||`, where the shell may not have run the
    `cd` at all.

    A `cd` target that does not exist yet is a measurement rather than a failure
    to measure, and what it measures is nothing: this hook protects content that
    exists when it decides, and a path holding none can only come to hold what
    the rest of this same line puts there. The separator decides which reading
    applies, and nothing else does -- notably not what the earlier commands were,
    since the set of ways to produce a directory has no boundary to enumerate.
    Under `&&` the git command runs only if the `cd` succeeded, so either the
    directory holds just what this line put there or the command never runs.
    Under `;` a failed `cd` stops nothing and the git command runs where the
    shell already was, which is right here and can be measured.

    Reading an absence as "nothing here" is only sound for the path the shell
    will use, so a target the shell has still to expand is refused. `~` is
    expanded here because this hook and that shell share a home; `$VAR`, a
    command substitution and a brace expansion are not.

    A `cd` inside a subshell is undone when the subshell ends, so it is undone
    here too -- parens are tracked as a stack, and a `cd` in a pipeline or
    backgrounded with `&` gets its own subshell and is skipped. Letting one
    outlive its parens measures a tree the line never touches.

    The residue left unguarded is content the same line moves into the target,
    as in `mv <dirty-repo> new && cd new && git reset --hard`. Closing it needs
    the enumeration ruled out above, of content-moving commands this time.
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
        # Peeled before the word is tested. Tested first, `builtin eval "cd <repo>"`
        # reads as `builtin` -- neither a directory change nor an unreadable one --
        # and the line is stepped over, measured in a tree the git call never ran
        # in, and allowed.
        argv = unwrapped(argv)
        if argv[0] not in ("cd", "pushd", "popd", *RUNS_TEXT):
            # Only a command that moves the shell can leave the directory in
            # doubt. Refusing on any earlier `||` blames a `cd` that is not
            # there: `test -d x || echo no; git checkout -- a.txt` gets a blind
            # refusal and no at-stake list, over a line whose directory never
            # moved at all.
            continue
        # A `cd` reached through any conditional -- `&&` as much as `||` -- may or
        # may not have run, so where the git call lands depends on something this
        # hook cannot evaluate. It is still safe to follow when the same condition
        # guards the git call: under `mkdir x && cd x && git clean -n`, a failure
        # anywhere stops the git command too, and the only branch where it runs is
        # the one where the `cd` did. Break that chain with anything else and both
        # readings survive -- `test -d dist && cd dist; git reset --hard` runs the
        # reset either in `dist` or in the tree the shell never left, and it was
        # the second one that got destroyed while this measured the first.
        #
        # Substring rather than equality, because a separator now carries every
        # operator that ran together: `)&&` is one of these.
        conditional = "&&" in sep or "||" in sep
        # `only_and` for the same reason, and the two have to agree: read one by
        # substring and the other by equality, and a separator carrying a `(` is
        # conditional-but-not-chained, which raises. The line then degrades from a
        # measured refusal to a blind one -- it still denies, but the at-stake list
        # and the content-bound token are gone.
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
            # The `cd` would be inside text this hook does not read, and it moves
            # the shell all the same: `eval "cd <repo>" && git reset --hard` runs
            # the reset in that repository, measured here as the payload's own tree,
            # found clean, and allowed. Unknown rather than unchanged is a refusal.
            #
            # Placed after the skips above rather than at recognition, because a
            # form the rest of this function would have stepped over cannot have
            # moved anything, and refusing it costs the at-stake list for nothing.
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
        # only the shell can resolve is refused instead: the miss would read as
        # "nothing here" for a directory about to hold a whole repository.
        target = shell_path(targets[0], f"`{argv[0]}` target")
        moved = target if target.is_absolute() else (cwd / target).resolve()
        if moved.is_dir():
            cwd = moved
        elif chained:
            # `chained` is every separator from here to the git call, not just
            # the next one.
            # `&&` guards only what immediately follows it: after
            # `cd nosuchdir && echo x; git reset --hard` the failed `cd` stops
            # `echo`, and the `;` then runs the discard in the directory the
            # shell never left. Reading the first `&&` as covering the whole line
            # lets that through.
            #
            # The shell moves here whether or not the directory exists yet, so
            # this follows it and keeps walking. Answering at this point instead
            # abandons every later move on the same line: under
            # `mkdir -p d && cd d && cd <other-repo> && git reset --hard` the
            # answer would describe the tree the payload named, which may hold
            # nothing, while the command lands in <other-repo> and takes its
            # uncommitted work. Where the walk ends is the only directory worth
            # answering about.
            cwd = moved
        else:
            # The `cd` will fail and something after it still reaches the git
            # call, which therefore runs right here. Measure that, and step over
            # the move.
            continue
    if cwd.is_dir():
        return (str(cwd), False)
    # The walk ended somewhere this line has still to create, and "the directory
    # holds nothing" is about the directory alone. git resolves upwards, so a
    # command run in a path this line creates still reaches the repository above
    # it: `mkdir -p d && cd d && git reset --hard` takes the enclosing tree, whose
    # content is exactly the kind that predates the line. So the measurement
    # falls back to the nearest directory that already exists.
    #
    # Only when that nearest existing directory is itself inside a repository is
    # there anything above to reach. Outside one, whatever the line puts there is
    # either a repository of its own -- a fresh clone, whose content the line
    # just created -- or not a repository at all, and git stops.
    existing = nearest_existing(cwd)
    if existing is None:
        return None
    # Measured, but wider than the command reaches: whatever the line puts at the
    # target may well be a repository of its own, and then none of what is named
    # here is inside it. The reason has to say so, or a list of the parent's
    # files reads as the exact set at stake.
    return (str(existing), True)


def stash_form(kind: str) -> str:
    """The stash spelling that actually covers this kind of content.

    `-u` takes untracked files but stops at ignored ones; only `--all` reaches
    those. Routing every untracked kind to `-u` hands `clean -fX`, and the
    ignored half of `clean -fdx`, a command that answers "No local changes to
    save" and leaves the files exactly where they were.

    The `-deep` suffix says how far the command descends, which does not bear on
    which content the stash has to reach, so it is dropped before the choice.
    """
    kind = kind.removesuffix("-deep")
    if kind in ("untracked-all", "untracked-ignored-only"):
        return "git stash push --all"
    if kind == "untracked":
        return "git stash push -u"
    return "git stash push"


# What each spelling is for, in the order a reader picks between them. Kept
# beside `stash_form` rather than written into the messages, because the mapping
# from content to spelling is one rule: a message restating it in prose is a
# second copy that can be edited alone, and the two would then disagree about
# which command reaches an ignored file -- the case where picking wrong gets
# "No local changes to save" and leaves the file where it was.
STASH_ROUTES = (
    ("worktree", "a tracked change"),
    ("untracked", "an untracked file, which a plain push refuses"),
    ("untracked-all", "an ignored file, which -u still skips"),
)


def stash_routes() -> str:
    """The routes as one sentence fragment: spelling and what each one reaches."""
    return ", ".join(f"`{stash_form(k)}` for {what}" for k, what in STASH_ROUTES)


# The way back from a stash, as a complete clause. Shared by the two messages
# that offer a stash and end on the same promise; the worktree message makes a
# different, joint claim covering `keep.patch` as well and writes its own.
#
# Complete rather than the bare predicate `"keeps the entry if it cannot"`,
# because the sites do not share a sentence: a fragment leaves each to supply the
# subject and verb the ellipsis was written for, and one that supplies the wrong
# one emits "the stash keeps the entry if it cannot" --
# the subject moved off the pop it describes -- or "A pop that cannot place its
# entry keeps the entry if it cannot", which says nothing at all. Shared text
# that arrives already grammatical cannot be spliced wrong; `stash_routes()` is
# the same pattern.
POP_BACK = "`git stash pop` brings them back, and keeps the entry if it cannot"


def operand(path: str) -> str:
    """A listed path in the spelling the routes below can actually be given.

    The listing is not a display: every route printed under it asks the reader to
    substitute these into `-- <path>...`. A name carrying a space or a `#` is not
    a shell word, and pasting `x #1.txt` verbatim gets
    `pathspec ':(prefix:0)x' did not match any file(s)` -- nothing saved, on the
    step taken to save something. `core.quotepath=false` was set for the same
    reason on the non-ASCII spelling; this is the other half of it.
    """
    return shlex.quote(path)


def listing_for(kind: str) -> str:
    """The command that prints the names the cap stood in for -- all of them.

    Two axes. `--ignored`, because a plain
    `git status` does not report an ignored file at all: for `clean -fX` it shows
    none of the list. And `-uall`, because it otherwise collapses an untracked
    directory to one entry: over 60 files under `bigdir/` it prints `?? bigdir/`
    and nothing else, which is not "lists them all" by any reading.
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
    # `node_modules` names tens of thousands of files, and this reason is
    # delivered into the caller's transcript, where that arrives as megabytes
    # displacing the context it needs to act on the message. The count carries
    # the scale, and the tail is one command away.
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
    # Said plainly, because the list is otherwise indistinguishable from a hook
    # that measured the wrong thing. A reader who sees a file the command visibly
    # does not name has two readings available -- over-wide report, or broken
    # hook -- and only one of them is worth acting on.
    #
    # The two causes get their own sentences, and are not collapsed into one
    # flag's worth of prose. A single boolean prints the pathspec wording for
    # both, which tells `mkdir -p d && cd d && git reset --hard` -- a line
    # carrying no narrowing at all -- that its narrowing was not forwarded. Both
    # can hold at once, so this is two `if`s and not an `elif`.
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
    # `cd <root>` rather than `-C <root>`, for every route offered. `-C` moves
    # git's directory and not the shell's, so under it the patch route writes
    # keep.patch wherever the caller happens to stand while the patch body names
    # root-relative paths -- and `git apply keep.patch` from there exits 0 having
    # restored nothing. One directory for everything is also one rule to hold:
    # the paths listed above are relative to that same root.
    #
    # Neither route promises the restore succeeds. A covered verb may move the
    # branch as well as the tree (`git checkout -f <branch>`), and a copy taken
    # against the old commit can then refuse to go back on: `git stash pop`
    # exits 1 on conflict and `git apply` reports "patch does not apply". What
    # both do guarantee is that the copy outlives the failure -- the stash entry
    # is kept, the patch file stays on disk -- which is what the message states.
    # Listed without `EITHER`, with "Both ... Run them" as the only quantifier on
    # offer, the pair reads as two steps: stash first, then diff -- which diffs a
    # tree the stash has already cleaned, writes a 0-byte keep.patch, and ends at
    # `git apply` exiting 128 on "No valid patches in input".
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
            # -- and `git apply` refuses the lot with "cannot apply binary patch
            # ... without full index line". Refuses the lot: apply is atomic, so
            # a text file listed beside a binary one is not restored either. The
            # caller is left holding a patch that describes the loss instead of
            # undoing it, on the one route reached for when the content matters.
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
        # spelling: restating "a plain push refuses an untracked path" here would
        # put the selection rule in a second place, free to drift from the one
        # that picks it.
        #
        # "reaches" and not "is for": a `clean -fdx` list holds untracked and
        # ignored files, and naming only the escalation reads as the spelling's
        # scope -- sending the reader off for a second, narrower run over the
        # untracked half this one already covers.
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

    Only the token is echoed, never the command it came from. Echoing the whole
    command reads as "run this again", which for a compound line re-runs the
    earlier parts too -- and any of those that touch the tree change what is at
    stake, invalidating the very token being offered.

    The object of "append" is named, because the two available readings do not
    agree. A token binds the whole command line, so appending it to the line
    passes while appending it to the git call alone -- the more natural reading
    of "append this" beside a single command -- yields a second refusal with a
    different token, and a caller that keeps re-running.
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
    # The way out has to be the one that actually exists on this path, and
    # emptying the tree is not it: every route to this message refuses on the
    # shape of the command, before and without consulting what the tree holds,
    # so a clean tree is refused here just the same. Saying otherwise sent the
    # reader to do work that changes nothing and returns them to an identical
    # refusal with no exit. What does change the outcome is giving the hook a
    # command it can read.
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

    The command arrives as JSON, and JSON can spell a lone surrogate (`\\ud800`)
    that no UTF-8 encoder will take. A plain `.encode()` on one raises
    `UnicodeEncodeError` -- and it raises inside the refusal, after the decision
    to deny is already made, so the hook exits non-zero and the harness runs the
    command. A command carrying one unencodable character was a fail-open, on a
    guard whose entire purpose is that there is no such thing.

    `surrogatepass` is the encoder that takes them, and it is the right one for
    the further reason that it stays injective: `replace` would map every
    unencodable command onto the same bytes, so an override minted for one
    command would authorise a different one. A token that stops binding to its
    command is not a smaller bug than the crash it fixed.
    """
    return text.encode("utf-8", "surrogatepass")


def bound_to(command: str) -> str:
    """The command as an override token binds it: acks removed, spacing flattened.

    Flattened because removing the ack leaves the whitespace it sat beside, and
    the token is a hash of what is left. An ack appended anywhere but the very
    end therefore changed the base and minted a new token on every retry -- a
    refusal with an override that never converges, which is a refusal with no way
    through at all. Collapsing runs of whitespace makes where the ack was written
    stop mattering, which is the only thing about it that should not.
    """
    return " ".join(ACK_RE.sub("", command).split())


def clipped(text: str, limit: int = 400) -> str:
    """A reason fragment cut to something a transcript can carry.

    This one interpolates whatever a failed git query said, and git answers an
    unusable invocation with its usage screen: a non-repository working directory
    produced 136 lines of `git diff` syntax inside a deny. The measured reason
    caps its file list for exactly this reason; the unmeasured one had no bound
    at all.
    """
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def deny_unmeasured(command: str, cwd: str, why: str, posture: str) -> bool:
    """Refuse a covered shape that was not measured. False when already overridden.

    Raises for no input the payload can carry, and that is a guarantee rather
    than an observation: this function is itself the refusal, so there is nothing
    above it to catch what escapes. The handler at the end holds it.

    One raise source survives that handler and is left unhandled: `emit_deny`
    writes to stdout, and a stdout that has gone away raises `BrokenPipeError`
    from inside the handler itself. The decision reaches the harness through
    stdout, so with stdout gone a refusal and a crash are the same event.
    Handling it would only make the guarantee read as broader than it is.

    Two callers reach this -- a measurement that could not be taken, and a
    covered verb never read as a call. Both leave the same thing unknown, so both
    offer a token binding the command alone, which is all there is to bind to.

    They differ in how much they are entitled to claim, which is why `posture` is
    the caller's to write: the backstop fires on text this hook could not read,
    and some of what it catches destroys nothing. One sentence asserting
    irreversible loss for both would be false half the time it printed.

    `cwd` is the payload's working directory: a starting point for the reader,
    not the tree at stake, and the message says which is which.
    """
    try:
        token = hashlib.sha256(
            hashable(f"unmeasured\0{bound_to(command)}")
        ).hexdigest()[:16]
        if token in set(ACK_RE.findall(command)):
            return False
        why = clipped(why)
        # where to run the listing. Naming "the tree that command targets" would
        # be no help here: it covers a `GIT_DIR=` or a written-out `cd`, and
        # fails exactly where this branch is reached most sharply. A `popd`
        # returns to a directory only the shell's stack knows; this hook refuses
        # because it cannot settle which tree, and the caller reading the message
        # holds the same text and can settle it no better.
        #
        # So the message anchors on the directory that is knowable: where the
        # shell stood when the harness handed this over. When even that is
        # missing the anchor is dropped rather than printed empty -- one of the
        # ways into this branch is a payload carrying no working directory, and
        # an anchor printed anyway reads `The shell was in '' when this was
        # checked` directly beneath its own reason saying there was no directory.
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
            # The same spelling `listing_for` picks, and taken from it rather
            # than written out again: which status flags reveal the content at
            # stake is one rule, and a second copy here is free to drift from the
            # one the measured messages print. Widest kind, because this branch
            # measured nothing and so cannot rule out an ignored file.
            + f"`{listing_for('untracked-all')}` and set aside anything worth "
            f"keeping: {stash_routes()}, each with `-- <path>...`.\n"
            "\n"
            # The way back, which a message telling the reader to set content
            # aside owes them. Said per push, because
            # unlike the measured messages this one offers three spellings at
            # once: a tree holding both a tracked change and an ignored file
            # takes two pushes, and one pop then strands an entry.
            f"Each push makes its own stash entry, so pop once per push: "
            f"{POP_BACK}.\n"
            "\n" + "\n".join(override(token))
        )
        return True
    except BaseException:  # noqa: BLE001
        # The net needs a net, and this is the net: everywhere else a raise is
        # caught and turned into a refusal, and this function is the refusal. Not
        # hypothetical -- a lone surrogate in the command text raises inside the
        # token hash, and with nothing here the `git reset --hard` it was refusing
        # runs.
        #
        # A fixed string is the only refusal trustworthy here, because every
        # ingredient of a composed one is implicated: the command that would not
        # encode, the directory that could not be read, the token derived from
        # either. `LAST_RESORT` interpolates none of them.
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

    # A command that cannot be tokenized is a different case, and returning here
    # would be the fail-open the backstop below exists to close: the text is in
    # hand, it may name a covered verb, and a parse that raised read no call from
    # it -- which is the exact signature the count refuses on. So the failure
    # leaves the command list empty and falls through to it.
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
        # Recognition turns on the verb alone and touches no repository, so that
        # a failure anywhere below is unambiguously "a covered shape we could not
        # measure" rather than "a shape we do not cover", and can deny.
        if covered_verb(rest) is None:
            continue
        recognized += 1

        # Everything from here on runs inside the handler, because from here on
        # the shape is one of the covered ones. Recognition itself stays outside
        # -- a failure there is "a shape we do not cover", which has no business
        # denying.
        try:
            # Settle what can be settled without the repository. A dry run or an
            # interactive form denies nothing, and it denies nothing wherever it
            # runs, so it is answered before any question about where -- which
            # keeps a form that destroys nothing from being refused over an
            # environment or a directory that has no bearing on it.
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
                # Through the same gate as a `cd` target: `git -C` names a
                # directory the shell may still have to resolve, and the two ways
                # of saying "run it over there" have to answer alike.
                cwd = str(Path(cwd) / shell_path(d, "`git -C` target"))
            if not Path(cwd).is_dir():
                # `git -C <missing>` exits 128 when the directory really is
                # absent. But a line that creates it first --
                # `mkdir -p d && git -C d reset --hard` -- has git resolving
                # upwards out of the new directory into the repository above it,
                # which is the same thing `cd d` does and is measured the same
                # way. Answering "reaches nothing" here decided one intent two
                # ways depending on which spelling it was written in.
                existing = nearest_existing(Path(cwd))
                if existing is None:
                    continue
                cwd, wider_than_reach = str(existing), True
            elif relocating_global is None and not in_repository(cwd):
                # Nothing here to lose: git resolves upwards and stops, so the
                # command exits without touching a file. Checked on this side too,
                # not only inside `nearest_existing`, and after the `git -C`
                # folding above, which is what can move the answer either way.
                #
                # `relocating_global` is what keeps the answer honest, because here
                # is exactly what those options move: under `--git-dir` /
                # `--work-tree` the command acts on a tree the launch directory says
                # nothing about, and it discards that tree's work. Answering here
                # would reach allow before `stake_for` -- the only thing that
                # refuses an unrecognized global -- ever runs.
                # Without it a plain directory gets a refusal carrying git's own
                # usage screen as its explanation, while the same command one
                # directory-that-does-not-exist away is allowed -- one intent,
                # two answers, and the one it gives is over a tree that holds
                # nothing.
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
            # covered verbs needs one token each, and matching only the first
            # would leave the second block unable to ever see its own -- the
            # tokens accumulate while `search` keeps returning the earliest.
            if token in set(ACK_RE.findall(command)):
                continue  # override presented, and still describes this content
            root = git_line(cwd, "rev-parse", "--show-toplevel")
            # Run the per-file diffs from the root, because the names came from
            # `git diff --name-only`, which reports root-relative paths wherever
            # it ran. Passing them back as pathspecs in a subdirectory resolves
            # to `sub/sub/file`, matches nothing, exits 0 -- and the hunk list
            # silently empties with no error to notice.
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
            # An unmeasurable case still needs an exit. Most of the ways this is
            # reached -- a relocated worktree, a conditional `cd`, a payload with
            # no directory -- produce the identical refusal on a re-run, so
            # without a token of its own a genuine intent to discard would have
            # nowhere to go.
            #
            # Interpolating `exc` runs an arbitrary `__str__`, and the f-string
            # is built as an argument -- so it is evaluated in this frame, not
            # inside the refusal's own handler. `deny_unmeasured` cannot catch
            # what raised before it was entered, which is why the argument list
            # sits inside a try of its own.
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

    # Where the parse fails closed. Everything above answers about calls this hook
    # read; a covered verb it failed to read is not answered at all, and silence
    # reaches the caller as permission. Parser gaps differ in the mistake and
    # agree in the signature: a covered verb in the text with no recognized call
    # to account for it.
    #
    # So the signature is the test, and it counts rather than matches, because one
    # unread call among several read ones has to refuse too. It is cruder than the
    # parse it backstops, which is what makes `echo git reset --hard >> log` cost a
    # token.
    #
    # `mentions` re-reads the raw command, so it can fail on the same input the
    # parse above did -- and nothing runs after it to notice. A count that could
    # not be taken is not a count of zero: it is one more covered verb this hook
    # could not read.
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
