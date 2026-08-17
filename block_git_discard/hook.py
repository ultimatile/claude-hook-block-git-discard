#!/usr/bin/env -S uv run --no-project --script
# /// script
# requires-python = ">=3.11"
# ///
# PreToolUse hook: bounce a git command that would discard uncommitted work.
#
# `git checkout -- <file>` restores the file from the index, and takes every
# OTHER uncommitted change in that file with it. The revert's granularity is the
# file; the intent's is almost always one hunk. Nothing warns, and the loss reads
# as though the edit never applied. That has cost real work more than once.
#
# This does not pattern-match "dangerous-looking" commands. It runs the same
# read-only query git would and denies on what that query reports, so a checkout
# with nothing to lose is not in the way.
#
# Where the command's reach cannot be narrowed exactly, the report widens rather
# than narrows, and the deny follows the wider report. That is the rule, not a
# list -- a list would have to grow with every such site and would go quiet on
# whichever one was added without it. Representative sites: a pathspec the shell
# has not expanded yet, which cannot be forwarded to git and so falls back to the
# whole tree; `clean -e`, whose repeatable exclude values are not worth the
# spelling surface it would take to forward them; a `cd` whose target the shell
# has still to expand; and any covered verb whose measurement fails outright.
# When a report is widened, the reason says so -- a file the command visibly does
# not name otherwise reads as a hook that measured the wrong thing.
#
# SCOPE IS DELIBERATELY NARROW. The five verbs in COVERED are the ones an agent
# actually types. Covering every git command that can destroy content -- plumbing
# such as `read-tree -u` and `checkout-index -f`, sequencer aborts, `git rm -f`,
# `clean.requireForce=false` -- means reproducing git's own worktree/index
# normalization (filters, eol, symlinks, gitlinks, sparse entries). That buys
# nothing here, because agents do not type those. Shapes outside the verb list
# PASS THROUGH by design, not by oversight.
#
# A covered verb that cannot be measured is refused, not passed: a
# `--git-dir`/`--work-tree` override moves the tree the command acts on, so any
# measurement taken here would describe a different one. A `GIT_*` assignment in
# front of the command is refused on the same ground and reaches the guard by a
# different door -- assignments are stepped over so that `LC_ALL=C git ...` stays
# recognized, and `GIT_DIR=<other> git reset --hard` rides in on that.
#
# What "cannot be measured" means is narrow, and the narrowing is the invariant:
# this guards content that EXISTS WHEN THE HOOK DECIDES. A refusal is for a tree
# that is there but hidden from the query -- relocated, reached through a `cd`
# the shell may not have run, named by a payload carrying no directory. A path
# that does not exist yet is not hidden; it holds nothing, and reading that as
# "unknown" refuses a line while protecting nothing. So `cd repo && git checkout
# <branch>` is left alone whether `repo` was produced by `git clone`, `ghq get`,
# `gh repo clone`, `git worktree add` or anything else -- the reading follows
# from the separator, and never from a list of directory-producing commands,
# which has no boundary to enumerate. The residue is content the same line MOVES
# into the target (`mv <dirty-repo> new && cd new && git reset --hard`): it
# exists at decision time, at a path nothing here can connect to the one named.
#
# A forced switch is measured against the whole worktree, but an untracked file
# that the target branch would write over is not. Catching that needs the target
# tree, and the cheap substitute -- measuring every untracked file -- would deny
# on any tree carrying one, which is most of them.
#
# Two further kinds of content stay unprotected under a covered verb. Content git
# does not report as changed -- `assume-unchanged`, `skip-worktree`, a lossy
# clean filter -- is invisible to the query asked here. And content that exists
# only in the index, staged and then reverted in the worktree, is left alone on
# purpose: `git add` wrote that blob into the object store, so `git fsck` can
# still reach it and losing the index entry is not the irreversible kind of loss
# this guards against.
#
# The reason is the real payload. It names what is at stake, offers a way to keep
# a recoverable copy FIRST, and only then the override -- because the agent, not
# the human who would see a permission prompt, is the one who knows which hunk
# was its own throwaway edit. Those routes are whole-file (`stash push`, a saved
# patch) rather than hunk-level: hunk-level means `-p`, which wants a terminal
# the denied caller does not have.
#
# FAIL-CLOSED, deliberately against the convention of every other hook here. The
# others let malformed input through because being wrong costs a redo; this one
# guards an irreversible loss. The guarantee has a floor worth stating: a hook
# that exits non-zero is reported as a non-blocking error and the command then
# runs, so every path after recognition must reach print() rather than raise.
# Failures before main() -- import, syntax, an unusable interpreter -- cannot be
# caught from inside this file at all.
#
# THE PARSE FAILS CLOSED TOO, which is the part that took three review rounds to
# arrive at. Measurement failures were refused from the start; RECOGNITION
# failures were not, and a covered verb this hook could not read as a call was
# indistinguishable from a shape it does not cover -- so it passed, silently.
# Every parser gap found here surfaced that way, each a different mistake with
# one signature: a covered verb in the text with no recognized call to account
# for it. `main` counts the two and refuses when they disagree.
#
# That test is why the shell parsing here stays shallow. A here-document body is
# read as ordinary commands and a script that merely writes one is refused; so is
# `echo git reset --hard >> log`, `man git checkout`, `docker run IMG git clean
# -fdx`, and a commit message that spells `git <verb>` inside its quotes. Every
# one of those is `git` beside a covered verb with no call read from it, which is
# also the exact signature of a parse gap -- telling them apart is the parsing
# that kept going wrong. The refusals cost an override token.
#
# The backstop's own floor: it reads a covered call out of the WORDS, so both
# halves have to be there as words. A command whose NAME resolves to `git` only
# at run time -- `$GIT reset --hard`, `$(echo git) reset --hard` -- and a VERB
# that does the same, which is what a `git co` alias is, are invisible to both
# parsers alike. Resolving either means reading the environment or the config the
# command will run under, and this hook reads neither. So "never under-refuse" is
# not a guarantee it can make; what it holds to is narrower and true: no shape it
# can READ is under-refused, and a shape it cannot read is refused rather than
# passed.

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

# A script's own directory is normally on sys.path, but not under
# PYTHONSAFEPATH=1, where the sibling import below would raise. The harness
# reports that as a non-blocking error and runs the command anyway, so a silently
# disabled guard is the worst outcome. Put the directory back explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shell_tokens import is_separator, tokenize  # noqa: E402

# The override token, read from the RAW command string. It cannot be read from
# the token list: the comment carrying it is stripped before tokenizing, so
# `git checkout -- f # ack:...` tokenizes without the token entirely.
ACK_RE = re.compile(r"#\s*ack:([0-9a-f]{16})\b")


# A backslash-escaped newline, which the shell joins into one line before it
# reads anything. shlex does not: it leaves a bare newline, `is_separator` reads
# that as a command boundary, and `git checkout \` + newline + `-- a.txt` arrives
# as `git checkout` with the pathspec dropped -- a whole-tree discard measured as
# a narrowed one, which is to say allowed.
CONTINUATION = re.compile(r"\\\r?\n")

# A command substitution, innermost first. The tokenizer treats `(` and `)` as
# separators, so `git -C $(git rev-parse --show-toplevel) reset --hard` arrives
# split into fragments with the covered verb in none of them. It is one WORD to
# the shell, so it is collapsed to one here; `$SUBST` keeps the `$`, which marks
# it as a value only the shell can supply, and the existing handling for those
# takes it from there.
SUBSTITUTION = re.compile(r"\$\([^()]*\)|`[^`]*`")

# Characters that glue to a word without being part of it. Stripped for the
# mention test only, so a quoted payload (`sh -c 'git reset --hard'`) is read as
# the words it holds rather than as opaque text.
CLINGING = "'\"()$`"

# The file-descriptor number in front of a redirection, and ONLY when it is
# written against the operator. Adjacency is the whole signal, and it survives
# only here: `2>log` and `2 > log` reach the tokenizer as the same three tokens,
# so a rule applied there has to guess -- and guessing "digit before a
# redirection is a descriptor" throws away the pathspec in
# `git checkout -- 2 > log`, over a repository holding a file named `2`.
FD_PREFIX = re.compile(r"(?:(?<=\s)|\A)\d+(?=[<>])")

# git subcommands that do NOT run a command string handed to them as an argument,
# so a `git <verb>` sitting inside one of these is text and not a call. Without
# this, `git commit -m "block git clean when untracked files exist"` is refused --
# an ordinary commit message, and one this very hook invites people to write.
#
# An ALLOWLIST, and the direction is the point. Listing the subcommands that DO
# run their argument would make every omission a hole, and that set is open at
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
    it. The contents of a substitution ARE executed, so hiding them from the
    mention test is backwards: `echo $(git reset --hard)` was allowed by exactly
    that, and it discards the tree. The mention test splits on whitespace and
    peels the characters that cling to a word, so `$(git` gives up its `git`
    without any masking at all -- it needs none of the protection the tokenizer
    needs, and pays none of its cost.
    """
    # Comments come off BEFORE lines are joined, because a backslash inside a
    # comment is ordinary text to the shell -- `echo one # note \` / `echo two`
    # prints both, verified in bash and zsh. Joining first makes the second line
    # part of the comment and deletes it, so `git log -1 # note \` followed by
    # `git reset --hard` reached the hook as one commented-out line and was
    # allowed while the reset ran.
    command = strip_comments(command)
    command = CONTINUATION.sub(" ", command)
    if mask:
        previous = ""
        while previous != command:
            previous = command
            command = SUBSTITUTION.sub("$SUBST", command)
    return FD_PREFIX.sub(" ", command)


def logical_lines(text: str) -> list[str]:
    """Split on the newlines that end a COMMAND, leaving quoted ones in place.

    A newline inside quotes is part of a word — a commit body is written exactly
    that way — so treating it as a command boundary splits one argument into
    several, and the guard in `mentions` is cleared halfway through a message the
    shell never executes. That refused `git commit -am "subject` + blank line +
    `body naming git reset --hard"` while the single-line spelling passed: two
    readings of one inert call.
    """
    out: list[str] = []
    current: list[str] = []
    quote = ""
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            out.append("".join(current))
            current = []
            continue
        current.append(ch)
    out.append("".join(current))
    return out


def mentions(command: str) -> int:
    """Covered calls the raw text names, read as crudely as this hook can manage.

    Deliberately a SECOND parser, and a dumber one. It splits on whitespace and
    strips the characters that cling to a word, so the things that made the real
    parse lose a verb -- a `(` the tokenizer reads as a separator, a
    here-document delimiter, a payload sitting inside `sh -c '...'` -- have no
    purchase on it. Compared in `main` against what that parse recognized, a
    shortfall is a covered verb nothing accounted for.

    It reads the verb in SUBCOMMAND position rather than anywhere in the line.
    Anywhere-in-the-line refuses `git bisect reset`, `git log -S clean` and
    `git tag -d checkout`, none of which discards anything, and all of which an
    agent types.

    And a `git` standing in the ARGUMENTS of a git call that does not run its
    arguments is skipped: see `INERT_SUBCOMMANDS`.
    """
    found = 0
    # LINE BY LINE, because a newline ends a command as surely as a `;` does and,
    # being whitespace, never survives into a word for the separator test below
    # to find. Carried across lines, one `git log -1` on the line above disarmed
    # the guard for everything after it -- `git commit -m "wip"` then
    # `sh -c 'git reset --hard'` was allowed, and destroyed the tree.
    for line in logical_lines(prepared(command, mask=False)):
        inert = ""
        raws = line.split()
        words = [w.strip(CLINGING) for w in raws]
        for i, raw in enumerate(raws):
            # A separator ends the call the guard belonged to. So does a
            # substitution or a subshell, for a different reason: what is written
            # inside one RUNS, so `git commit -am "$(git reset --hard)"` discards
            # the tree before the commit it is quoted into has begun. The bracket
            # is read off the raw word, since stripping is what would take the
            # evidence away.
            if any(ch in raw for ch in ";&|()`"):
                inert = ""
            if PurePosixPath(words[i]).name != "git":
                continue
            if inert:
                continue  # an argument of `git <inert>`, not a call of its own
            _, rest, _ = strip_global_opts(words[i + 1 :])
            if not rest:
                continue
            if rest[0] in COVERED:
                found += 1
            elif rest[0] in INERT_SUBCOMMANDS:
                inert = rest[0]
    return found


def strip_comments(command: str) -> str:
    """Drop `#`-comments the way the SHELL delimits them, not the way shlex does.

    shlex ends a token at a `#` anywhere inside it, so `f#1.txt` arrives as `f`:
    a pathspec matching nothing, a measurement finding nothing at stake, and a
    real discard let through. The shell instead opens a comment only at a `#`
    that STARTS a word, and never inside quotes.

    Both halves of that are load-bearing, and quoting is the half a regex cannot
    hold. Cutting at the `#` in `git checkout -- 'x #1.txt'` leaves an unbalanced
    quote; tokenizing then falls back to a whitespace split, the pathspec becomes
    `'x`, and the same discard goes through by the other door. So the scan tracks
    quote state and leaves anything inside it alone.
    """
    out: list[str] = []
    quote = ""
    prev = " "  # the start of the line counts as a word boundary
    skipping = False
    for ch in command:
        if skipping:
            # A comment runs to the end of its LINE, and this hook is handed
            # multi-line commands, so the rest of the line is not the rest of it.
            if ch == "\n":
                skipping = False
                out.append(ch)
                prev = ch
            continue
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            prev = ch
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "#" and prev.isspace():
            skipping = True
            continue
        out.append(ch)
        prev = ch
    return "".join(out)


# Subcommands the HOOK itself may run. Checked at the single subprocess entry
# rather than asserted in a comment, so an edit reaching for a mutating query
# fails loudly instead of quietly mutating the repository this hook protects.
READ_ONLY = frozenset({"diff", "ls-files", "rev-parse"})

# Characters the shell expands after this hook has seen the text, so a pathspec
# containing one cannot be forwarded to git as written. Globs are excluded on
# purpose: git's own glob matches at least as much as the shell's, so forwarding
# one over-detects rather than under-detects.
UNEXPANDED = re.compile(r"[{}$`~]")

# For a DIRECTORY the glob exemption inverts, so it gets its own pattern. A
# pathspec carrying `*` is handed to git, which matches at least as widely. A
# directory carrying `*` is one the shell picks and the hook cannot: testing the
# written spelling finds nothing there and reads that as nothing at stake.
GLOB = re.compile(r"[*?\[]")


def shell_path(raw: str, what: str) -> Path:
    """`raw` as a path this hook can test, or `Unmeasurable` if only the shell can.

    `~` resolves here, this hook and that shell sharing a home. Nothing else
    does, and every caller that turns a written word into a directory goes
    through this -- `cd`, `pushd`, and `git -C` alike. They had the check
    separately once, and the one that went without it let
    `git -C $REPO reset --hard` through while `cd $REPO && git reset --hard` was
    correctly refused: the same question, answered twice, drifting apart.
    """
    path = Path(raw).expanduser()
    if UNEXPANDED.search(str(path)) or GLOB.search(str(path)):
        raise Unmeasurable(f"the {what} is resolved by the shell after this runs")
    return path


class Unmeasurable(Exception):
    """A recognized shape could not be measured.

    Reaching `main`'s handler it becomes a deny, unless the command already
    carries that handler's own override token. Two callers catch it on purpose
    before it gets there: `is_ref`, where a failed rev-parse IS the negative
    answer rather than an error, and `hunk_headers`, where a per-file diff that
    fails costs only that file's display line.
    """


def git(cwd: str, *args: str) -> str:
    if not args or args[0] not in READ_ONLY:
        raise Unmeasurable(f"hook attempted a non-read-only git query: {args!r}")
    try:
        proc = subprocess.run(  # noqa: S603
            # `core.quotepath=false` because git otherwise C-quotes any path
            # holding a non-ASCII byte: `é.txt` is reported as `"\303\251.txt"`.
            # That spelling reaches the reason as a name the reader cannot pass
            # back to git, and the untracked fingerprint stats it, misses, and
            # marks the file `gone` -- which pins the fingerprint to the file
            # SET and lets an override token outlive the content it described.
            ["git", "-c", "core.quotepath=false", *args],  # noqa: S607
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


# Commands that precede the real one without changing what it is. A wrapper's own
# options are NOT stepped over -- that needs per-wrapper knowledge, and the
# prefixes that actually show up here take none. `sudo -u x git reset --hard` is
# therefore not recognized as a call, and used to pass on that account; it now
# lands on the mention test in `main` and is refused, which is what makes leaving
# the per-wrapper knowledge out affordable.
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
    necessary. `GIT_DIR` and `GIT_WORK_TREE` move the tree exactly as the
    `--git-dir` / `--work-tree` options do, and `GIT_INDEX_FILE`,
    `GIT_NAMESPACE`, `GIT_OBJECT_DIRECTORY` and the `GIT_CONFIG_*` family each
    move some other input the measurement here depends on. Read as ordinary
    prefix noise they would leave the hook measuring the directory it was handed
    while the command works on another one -- and reporting nothing at stake is
    the worst answer available.

    The test is the `GIT_` prefix, not a list of the variables that matter: git's
    environment surface is large and grows, and a list would go quiet on
    whichever member had not been added yet. Erring wide costs an override on
    `GIT_PAGER=cat git reset --hard`, which is neither common nor destructive to
    refuse.
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

    A here-document's body is NOT set apart. Newlines are separator tokens, so
    `cat > setup.sh <<'EOF'` / `git reset --hard` / `EOF` yields the middle line
    as a simple command, and writing a script that only mentions a discard is
    refused as though it performed one.

    That false positive is accepted rather than parsed away. Recognizing the
    body needs a delimiter rule, and every attempt at one here misread something
    -- a here-STRING (`<<<`) takes a word rather than a delimiter, a `<<-` marker
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
            # A REDIRECTION is not a command boundary, and reading it as one is
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
                # Consecutive separators are KEPT, not overwritten. `(cd x) && git
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
    the CURRENT shell, so that `cd` is one the git command afterwards really does
    inherit. Only a lone brace is peeled; `{}` is one token and stays whole.
    """
    sep, argv = command
    if argv and argv[0] == "{":
        argv = argv[1:]
    if argv and argv[-1] == "}":
        argv = argv[:-1]
    return sep, argv


# git's OWN options that take their value as a separate following token. That
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

# Of those, the ones that move what the subcommand acts on.
GIT_RELOCATING = frozenset({"--git-dir", "--work-tree", "--namespace"})


def strip_global_opts(argv: list[str]) -> tuple[list[str], list[str], bool]:
    """Peel git's own options off the front.

    Returns (`-C` dirs, subcommand argv, relocated). `relocated` marks
    `--git-dir` / `--work-tree` / `--namespace`, which move what the subcommand
    acts on, so a measurement taken in the resolved cwd would describe a
    different tree. This function never raises: the caller decides, because
    refusing has to happen after the verb is recognized, not before -- otherwise
    the refusal is indistinguishable from "not a shape we cover" and the command
    would be let through.

    git's globals are matched by exact spelling on purpose: unlike a
    subcommand's options they do not go through parse-options, so git rejects
    `--git-di=<path>` outright and an abbreviation-tolerant match here would
    accept what git does not.
    """
    cdirs: list[str] = []
    relocated = False
    i = 0
    while i < len(argv):
        tok = argv[i]
        name = tok.split("=", 1)[0]
        if name in GIT_RELOCATING:
            relocated = True
        if name in GIT_VALUE_OPTS and "=" not in tok:
            if name == "-C" and i + 1 < len(argv):
                cdirs.append(argv[i + 1])
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            break
    return cdirs, argv[i:], relocated


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


# Every long option this hook tests a subcommand for. A subcommand's options go
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

# Short options that take a VALUE, across the covered verbs: `-e` is clean's
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

    An abbreviated long flag expands to EVERY entry of `LONG_OPTS` it prefixes,
    not to a single unambiguous one. Resolving it the way git does would mean
    carrying git's full per-subcommand option table here; expanding to all of
    them needs none of it and cannot under-detect, because a token this treats as
    two options is one git itself rejects as ambiguous, leaving a command that
    never runs.

    A short bundle stops at the first `SHORT_VALUE` letter, that letter included:
    everything after it is the option's value, and the letters in a value are not
    flags.
    """
    out: set[str] = set()
    for tok in flags:
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            out.add(name)
            if len(name) > 2:
                out.update(o for o in LONG_OPTS if o.startswith(name))
        elif tok.startswith("-") and len(tok) > 1:
            for ch in tok[1:]:
                out.add(ch)
                if ch in SHORT_VALUE:
                    break
    return out


def certainly_harmless(argv: list[str]) -> bool:
    """True when a covered verb is in a form that cannot destroy anything.

    Decided WITHOUT touching the repository, and checked before anything about
    WHERE the command runs. Resolving first would refuse `cd $x && git clean -n`
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
    flags = expand_flags([a for a in opts if a.startswith("-") and a != "--"])

    if "p" in flags or "--patch" in flags:
        return True
    if verb == "clean":
        return "n" in flags or "--dry-run" in flags
    if verb == "reset":
        return "--hard" not in flags
    if verb == "restore":
        staged = "S" in flags or "--staged" in flags
        worktree = "W" in flags or "--worktree" in flags
        return staged and not worktree
    if verb == "switch":
        return not forced(flags)
    if verb == "checkout":
        return not forced(flags) and bool(flags & {"b", "B"})
    return False


def forced(flags: set[str]) -> bool:
    """Whether these flags waive git's own refusal to act on a dirty tree.

    One definition, because the two callers have to agree: `certainly_harmless`
    deciding a form is safe while `stake_for` reads it as a discard would let the
    discard past without ever being measured. They were written apart and had
    already drifted -- one branch omitted `--discard-changes`.
    """
    return "f" in flags or bool(flags & {"--force", "--discard-changes"})


def operands(opts: list[str], value_taking: set[str]) -> list[str]:
    """Non-option tokens from before `--`, skipping any option's separate value.

    `--source HEAD~1 a.txt` puts a ref where a naive scan reads a pathspec; the
    ref then carries `~`, which trips the unexpanded-pathspec check and collapses
    the measurement to the whole tree. The attached `--source=HEAD~1` spelling
    never had the problem, so without this the two spellings disagree.

    `value_taking` holds flag names as `expand_flags` reports them, so an
    abbreviation git accepts -- `--sou HEAD~1` -- consumes its value here too.

    Only the SEPARATE spelling consumes a following token. `--source=HEAD~1` and
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


def widened(kind: str) -> tuple[str, list[str], bool]:
    """A stake measured without the narrowing the command itself carries.

    Every site that cannot forward a narrowing to git funnels through here, so
    the fact that the report will name more than the command reaches is decided
    once and travels with the stake. The reason says so when it is set: a caller
    reading an untouched file on the at-stake list has no way, from the list
    alone, to tell an over-wide report from a hook that is simply wrong.
    """
    return (kind, [], True)


def narrowed(kind: str, paths: list[str]) -> tuple[str, list[str], bool]:
    """A stake narrowed to the pathspecs the command carries, where it can be.

    A pathspec holding a character the shell expands after this hook has seen it
    cannot be forwarded to git as written, so the narrowing is dropped instead of
    guessed at: over-detecting costs one extra round trip, forwarding a spelling
    git reads differently costs the work itself. Globs are deliberately outside
    `UNEXPANDED` -- git's own glob matches at least as much as the shell's, so
    forwarding one over-detects rather than under-detects.
    """
    if any(UNEXPANDED.search(p) for p in paths):
        return widened(kind)
    return (kind, paths, False)


def stake_for(cwd: str, argv: list[str]) -> tuple[str, list[str], bool] | None:
    """What this invocation would destroy, as (query kind, pathspecs, widened).

    Returns None when the shape is outside the list this hook covers, or when it
    is one of the covered verbs in a form that destroys nothing.
    """
    _, rest, relocated = strip_global_opts(argv)
    if not rest:
        return None
    if relocated:
        raise Unmeasurable("--git-dir/--work-tree relocates the tree to measure")
    verb, args = rest[0], rest[1:]
    # Options are read only from BEFORE `--`. Everything after it is a pathspec,
    # and a filename opening with a dash would otherwise be split per character
    # by expand_flags: `-patch.txt` yields `p`, which reads as `-p` and sends a
    # real discard down the "interactive, no collateral" branch below.
    opts, after_ddash = split_at_ddash(args)
    flags = expand_flags([a for a in opts if a.startswith("-") and a != "--"])

    # Interactive forms pick hunks, so they never take collateral.
    if "p" in flags or "--patch" in flags:
        return None
    if "--pathspec-from-file" in flags:
        raise Unmeasurable("pathspecs come from a file this hook cannot read")

    if verb in ("checkout", "switch"):
        force = forced(flags)
        if verb == "switch":
            # A forced switch rewrites the whole worktree and takes no pathspec,
            # so there is nothing to narrow to. Unforced, git itself refuses to
            # lose work. `-C` is force-CREATE: it moves a branch label and leaves
            # the tree alone, so reading it as force would deny every branch
            # creation on a dirty tree.
            return ("worktree", [], False) if force else None
        if flags & {"b", "B"}:
            # The operand here is the new branch's name, not a pathspec, so it
            # must not reach `paths_of` -- an unborn branch resolves as no ref,
            # and reading it as a path would narrow the measurement to a file
            # that does not exist and report nothing at stake. Unforced, branch
            # creation keeps the working tree; forced, it discards all of it.
            return ("worktree", [], False) if force else None
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
        staged = "S" in flags or "--staged" in flags
        worktree = "W" in flags or "--worktree" in flags
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
        return ("worktree", [], False) if "--hard" in flags else None

    if verb == "clean":
        if "n" in flags or "--dry-run" in flags:
            return None
        if not (flags & {"f"} or "--force" in flags):
            return None
        ignored = "-ignored-only" if "X" in flags else "-all" if "x" in flags else ""
        # Without `-d`, clean does not descend into an untracked DIRECTORY: it
        # removes untracked files sitting beside tracked ones and leaves the
        # directory whole. `ls-files --others` recurses regardless, so listing
        # its output would name `newdir/j.txt` for a `git clean -f` that leaves
        # `newdir/` exactly as it found it.
        deep = "-deep" if "d" in flags else ""
        kind = f"untracked{ignored}{deep}"
        # `-e <pattern>` narrows what clean removes, and the report is widened
        # rather than following it. `ls-files` does take the same `--exclude`, so
        # this is a cost decision, not an impossibility: forwarding it means
        # collecting a REPEATABLE option's values across `-e p`, `-epat` and
        # `--exclude=pat`, which is the same value-spelling surface that has
        # already produced two fail-opens here. Widening costs a wider list,
        # which `over_wide` announces. Both spellings reach the test below:
        # expand_flags strips an attached value, so `--exclude=pat` reads as
        # `--exclude`.
        if "e" in flags or "--exclude" in flags:
            return widened(kind)
        paths = after_ddash if after_ddash is not None else operands(opts, set())
        return narrowed(kind, paths)

    return None


LS_SELECT = {
    "untracked": ["--others", "--exclude-standard"],
    "untracked-all": ["--others"],
    "untracked-ignored-only": ["--others", "--ignored", "--exclude-standard"],
}

# `--directory` collapses a wholly-untracked directory to its own name, so what
# is left un-collapsed is exactly the set a `clean` without `-d` removes. The
# `-deep` kinds drop it and take the recursive listing, which is what `-d` does
# reach. Entries the collapse produced end in `/` and are dropped by `measure`:
# they name a directory the command will not open.
LS_COLLAPSE = ["--directory", "--no-empty-directory"]

# Every `git diff` this hook runs carries these, because `diff.relative=true` in
# a user's config silently rewrites what the command answers: it reports only
# what lies UNDER the current directory, and reports it relative to that
# directory. Run from a subdirectory the measurement then misses every change
# above it, finds nothing at stake, and lets the discard through -- the exact
# failure this hook exists to prevent, arriving without a symptom. It also
# breaks the frame the reason promises, since the untracked half is pinned to
# the repository root by `--full-name`.
# `--diff-filter=d` (lower case excludes) drops a path whose only change is that
# it was DELETED from the worktree. Restoring one puts content back rather than
# taking any away: what returns comes from the index, and whatever the file held
# before the delete was already gone when the delete happened. Counting it as at
# stake refuses `git checkout -- a.txt` after `rm a.txt` -- a command that only
# undoes the removal -- and every such refusal trains its reader to reach for the
# override token.
DIFF_FRAME = ("--no-relative", "--diff-filter=d")


def measure(cwd: str, kind: str, pathspecs: list[str]) -> tuple[list[str], str, str]:
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
        names = [
            ln
            for ln in git(cwd, "diff", *DIFF_FRAME, "--name-only", *tail).splitlines()
            if ln
        ]
        if not names:
            return [], "", ""
        # `--stat-count` because the summary is per-file too: capping the path
        # list while letting this one run to 5000 lines moves the size problem
        # rather than solving it. git prints `...` and keeps the totals line, so
        # the scale still reaches the reader.
        #
        # Once the path list is itself capped, the stat is cut to its totals: the
        # reason already printed those paths, and printing them again with a
        # churn column beside them spends the budget twice on one list. `1` and
        # not `0`, which git reads as no limit at all.
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
    # CURRENT directory, while the worktree branch's `diff --name-only` reports
    # them relative to the root. The reason names one frame for both lists, so
    # the two have to agree or the untracked half sends the reader to a path
    # that resolves nowhere.
    deep = kind.endswith("-deep")
    select = LS_SELECT[kind.removesuffix("-deep")]
    collapse = [] if deep else LS_COLLAPSE
    names = [
        ln
        for ln in git(
            cwd, "ls-files", "--full-name", *select, *collapse, *tail
        ).splitlines()
        # A trailing `/` is a directory the collapse stood in for, and a clean
        # without `-d` leaves it alone. Listing it would name content the command
        # does not reach; dropping it leaves the files beside it, which it does.
        if ln and not ln.endswith("/")
    ]
    # The fingerprint has to move when the CONTENT moves, not only when the file
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
    root = Path(git(cwd, "rev-parse", "--show-toplevel").strip())
    marks = []
    for n in names:
        try:
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


def resolve_cwd(
    payload_cwd: str, commands: list[tuple[str, list[str]]], idx: int
) -> str | None:
    """Where `commands[idx]` will run, or None if nowhere holding protected content.

    Applies the directory changes from the simple commands preceding it. `pushd`
    moves the shell exactly as `cd` does and is followed the same way; reading
    only `cd` leaves `pushd <other-repo> && git reset --hard` measured against
    the directory the command never ran in, which reports nothing at stake and
    lets that repository's uncommitted work go -- fail-OPEN, against this hook's
    whole posture. `popd` and a bare `pushd` land wherever the shell's own
    directory stack points, and `cd -` wherever `OLDPWD` does; the payload
    carries neither, so all three are refused rather than guessed at. So is
    anything after `||`, where the shell may or may not have run the `cd` at
    all.

    A `cd` target that does not exist YET is not a failure to measure -- it IS a
    measurement, and what it measures is nothing. This hook protects content that
    exists when it decides; a path holding none can only come to hold what the
    rest of this same line puts there. Which of the two readings applies is
    decided by the separator joining that `cd` to what follows, and by nothing
    else -- notably not by what the earlier commands were, since the set of ways
    to produce a directory (`git clone`, `ghq get`, `gh repo clone`,
    `git worktree add`, `mkdir`, `tar x`, ...) has no boundary to enumerate:

      `&&` -- either the directory exists by then, holding only what this line
              just put there, or it does not and the `&&` stops the git command
              from running at all. Neither branch endangers anything that existed
              when this hook decided, so the whole line is left alone.

      `;`  -- a failed `cd` stops nothing: the git command runs in the directory
              the shell was already in. That directory is right here and can be
              measured, so `cd typo; git reset --hard` is answered with the
              files it will actually destroy instead of a blind refusal.

    Reading an absence as "nothing here" is only sound for the path the SHELL
    will use, so a target the shell has still to expand is refused instead. `~`
    is expanded here, because this hook and that shell share a home; `$VAR`,
    a command substitution and a brace expansion are not, and testing the
    unexpanded spelling would find no directory and hand `cd $repo && git reset
    --hard` a pass over whatever the variable actually names.

    A `cd` the shell runs in a SUBSHELL is undone when that subshell ends, so it
    is undone here too: `(cd /other && git status); git reset --hard` reset the
    payload's own tree while this measured `/other` and found it clean. Parens
    are tracked as a stack, restoring the directory saved on the way in; a `cd`
    in a pipeline or backgrounded with `&` gets its own subshell the same way and
    is skipped outright.

    The residue this leaves unguarded is content the same line MOVES into the
    target, as in `mv <dirty-repo> new && cd new && git reset --hard`: it exists
    when the hook decides, but at a path the hook has no way to connect to the
    one the command names. Closing that needs the enumeration ruled out above,
    of content-moving commands this time.
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
        if argv[0] not in ("cd", "pushd", "popd"):
            # Only a command that MOVES the shell can leave the directory in
            # doubt. Refusing on any earlier `||` blames a `cd` that is not
            # there: `test -d x || echo no; git checkout -- a.txt` gets a blind
            # refusal and no at-stake list, over a line whose directory never
            # moved at all.
            continue
        # Substring rather than equality throughout, because a separator now
        # carries every operator that ran together: `)&&` is one of these.
        if "||" in sep:
            raise Unmeasurable(
                f"a conditional `{argv[0]}` leaves the directory ambiguous"
            )
        follows = commands[j + 1][0]
        if set(follows) <= {"|", "&"} and follows not in ("||", "&&"):
            # A pipeline stage and a backgrounded command each get their own
            # subshell, so this `cd` moved a shell that has already exited by the
            # time the git command runs.
            continue
        if argv[0] == "popd":
            raise Unmeasurable("`popd` returns to a directory only the shell knows")
        targets = [a for a in argv[1:] if not a.startswith("-")]
        if not targets:
            raise Unmeasurable(
                f"`{argv[0]}` with no usable target moves somewhere "
                "this hook cannot follow"
            )
        # The absence test below answers about the path as WRITTEN, so a target
        # only the shell can resolve is refused instead: the miss would read as
        # "nothing here" for a directory about to hold a whole repository.
        target = shell_path(targets[0], f"`{argv[0]}` target")
        moved = target if target.is_absolute() else (cwd / target).resolve()
        if moved.is_dir():
            cwd = moved
        elif "&&" in follows:
            return None
        # Under any other separator the failed `cd` changes nothing, so `cwd`
        # stays where it was and the loop carries on measuring from there.
    return str(cwd) if cwd.is_dir() else None


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


# What each spelling is FOR, in the order a reader picks between them. Kept
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


def build_reason(
    kind: str,
    names: list[str],
    summary: str,
    hunks: list[str],
    token: str,
    root: str,
    over_wide: bool,
) -> str:
    what = (
        "untracked file(s)" if kind.startswith("untracked") else "uncommitted change(s)"
    )
    # The count is always exact; the LIST is capped. `git clean -fdx` over a
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
        *(f"  {n}" for n in shown),
    ]
    if len(names) > len(shown):
        # `--ignored` when the kind includes ignored content, because plain
        # `git status` does not report an ignored file at all: for `clean -fX`
        # it would show NONE of the list this line is standing in for.
        listing = (
            "git status --short --ignored"
            if kind.startswith(("untracked-all", "untracked-ignored-only"))
            else "git status --short"
        )
        lines.append(
            f"  ... and {len(names) - len(shown)} more; `{listing}` lists them all"
        )
    if over_wide:
        # Said plainly, because the list is otherwise indistinguishable from a
        # hook that measured the wrong thing. A reader who sees a file the
        # command visibly does not name has two readings available -- over-wide
        # report, or broken hook -- and only one of them is worth acting on.
        lines += [
            "",
            "The narrowing this command carries was not forwarded to the query",
            "behind this list, so the whole tree was measured: some of the files",
            "above may lie outside what it would actually reach.",
        ]
    if kind == "worktree" and summary:
        lines += ["", summary]
    if hunks:
        lines += ["", "Hunks:", *hunks]
    # `cd <root>` rather than `-C <root>`, for every route offered. `-C` moves
    # git's directory and not the shell's, so under it the patch route writes
    # keep.patch wherever the caller happens to stand while the patch body names
    # root-relative paths -- and `git apply keep.patch` from there exits 0 having
    # restored NOTHING. A route that reports success and returns none of the
    # content is worse than no route at all, this being the one the caller
    # reaches for precisely when the content matters. One directory for
    # everything is also one rule to hold: the paths listed above are relative to
    # that same root.
    #
    # Neither route promises the restore SUCCEEDS. A covered verb may move the
    # branch as well as the tree (`git checkout -f <branch>`), and a copy taken
    # against the old commit can then refuse to go back on: `git stash pop`
    # exits 1 on conflict and `git apply` reports "patch does not apply". What
    # both do guarantee is that the copy outlives the failure -- the stash entry
    # is kept, the patch file stays on disk -- and that is the part the caller is
    # deciding on here, so it is the part the message states.
    # EITHER, and the word is load-bearing. Listed without it, with "Both ... Run
    # them" as the only quantifier on offer, the pair reads as two steps: stash
    # first, then diff -- which diffs a tree the stash has already cleaned, writes
    # a 0-byte keep.patch, and ends at `git apply` exiting 128 on "No valid
    # patches in input". The reader following the message exactly is the reader
    # this message exists for.
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
            # ... without full index line". Refuses the LOT: apply is atomic, so
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
            "kept and keep.patch stays where it was written.",
        ]
    else:
        # The spelling comes from `stash_form`, and so does the reason it is THIS
        # spelling: restating "a plain push refuses an untracked path" here would
        # put the selection rule in a second place, free to drift from the one
        # that picks it.
        #
        # "reaches" and not "is for": a `clean -fdx` list holds untracked AND
        # ignored files, and naming only the escalation reads as the spelling's
        # SCOPE -- sending the reader off for a second, narrower run over the
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
            "      sets them aside; `git stash pop` brings them back, and keeps",
            "      the entry if it cannot",
        ]
    # Only the token is echoed, never the command it came from. Echoing the whole
    # command reads as "run this again", which for a compound line re-runs the
    # earlier parts too -- and any of those that touch the tree change what is at
    # stake, invalidating the very token being offered.
    lines += [
        "",
        "If discarding all of it is intended, append this and re-run:",
        f"  # ack:{token}",
        "",
        "The token is an override, not a confirmation that the list above was read.",
    ]
    return "\n".join(lines)


def emit_deny(reason: str) -> None:
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


def deny_unmeasured(command: str, why: str, posture: str) -> bool:
    """Refuse a covered shape that was not measured. False when already overridden.

    Two callers reach this, and they fail in different places: a measurement that
    could not be taken, and a covered verb that was never read as a call at all.
    Both leave the same thing unknown -- what is at stake -- so both offer the
    same token, which binds the command alone because that is all that is
    knowable once no measurement exists to bind to.

    What they do NOT share is how much is being claimed, which is why `posture`
    is the caller's to write. A measurement that failed was taken over a command
    this hook did read as a discard; the backstop fires on text it could not read
    at all, and some of what it catches -- a script being written, a line echoing
    a command -- destroys nothing. One sentence asserting irreversible loss for
    both would be false half the time it printed.
    """
    token = hashlib.sha256(
        f"unmeasured\0{ACK_RE.sub('', command).strip()}".encode()
    ).hexdigest()[:16]
    if token in set(ACK_RE.findall(command)):
        return False
    emit_deny(
        f"Blocked: {why}\n"
        "\n"
        f"{posture}\n"
        "\n"
        # `--ignored`, because a plain `git status` does not report an ignored
        # file at all -- and the route right after this one asks the reader to
        # decide whether the content IS ignored. Naming a command that cannot
        # show them leaves that choice unmakeable from what this message gave.
        #
        # "in the tree that command targets" rather than a path, because these
        # are exactly the refusals where the target is NOT the caller's cwd -- a
        # `GIT_*` assignment, a relocated worktree, a `cd` that may not have run.
        # No path is knowable here, but the caller holds the command that names
        # it, so the extent can still be stated.
        "Run `git status --short --ignored` in the tree that command targets, "
        f"and set aside anything worth keeping: {stash_routes()}.\n"
        "\n"
        "To proceed without a measurement, append this and re-run:\n"
        f"  # ack:{token}"
    )
    return True


def main() -> None:
    # Before a destructive shape is recognized, anything unexpected is allowed
    # through, matching the other hooks here. After recognition the polarity
    # flips: see the header.
    try:
        data = json.load(sys.stdin)
        command = data.get("tool_input", {}).get("command", "")
        if not command:
            return
        payload_cwd = data.get("cwd") or ""
        commands = simple_commands(tokenize(prepared(command), comments=False))
    except Exception:  # noqa: BLE001
        return

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
        # the shape IS one of the covered ones and the header's guarantee has
        # taken effect: a raise would exit non-zero, which the harness reports as
        # a non-blocking error and then runs the command. Recognition itself
        # stays outside -- a failure there is "a shape we do not cover", which
        # has no business denying.
        try:
            # Settle what can be settled without the repository. A dry run or an
            # interactive form denies nothing, and it denies nothing wherever it
            # runs, so it is answered before any question about WHERE -- which
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
            cwd = resolved
            cdirs, _, _ = strip_global_opts(rest)
            for d in cdirs:
                # Through the same gate as a `cd` target: `git -C` names a
                # directory the shell may still have to resolve, and the two ways
                # of saying "run it over there" have to answer alike.
                cwd = str(Path(cwd) / shell_path(d, "`git -C` target"))
            if not Path(cwd).is_dir():
                # `git -C <missing>` exits 128 without opening anything, so this
                # reaches nothing either. A directory that exists but cannot be
                # reached from here lands in the same branch and is treated the
                # same way, because git runs as this hook does and stops where it
                # stops.
                continue
            stake = stake_for(cwd, rest)
            if stake is None:
                continue  # a covered verb, in a form that discards nothing
            kind, pathspecs, over_wide = stake
            names, summary, fingerprint = measure(cwd, kind, pathspecs)
            if not names:
                continue  # nothing at stake; let it run
            payload = "\0".join(
                [
                    git(cwd, "rev-parse", "--absolute-git-dir").strip(),
                    ACK_RE.sub("", command).strip(),
                    fingerprint,
                ]
            )
            token = hashlib.sha256(payload.encode()).hexdigest()[:16]
            # Every token on the line, not just the first: a line holding two
            # covered verbs needs one token each, and matching only the first
            # would leave the second block unable to ever see its own -- the
            # tokens accumulate while `search` keeps returning the earliest.
            if token in set(ACK_RE.findall(command)):
                continue  # override presented, and still describes this content
            root = git(cwd, "rev-parse", "--show-toplevel").strip()
            # Run the per-file diffs from the root, because the names came from
            # `git diff --name-only`, which reports root-relative paths wherever
            # it ran. Passing them back as pathspecs in a subdirectory resolves
            # to `sub/sub/file`, matches nothing, exits 0 -- and the hunk list
            # silently empties with no error to notice.
            hunks = hunk_headers(root, names) if kind == "worktree" else []
            emit_deny(build_reason(kind, names, summary, hunks, token, root, over_wide))
            return
        except BaseException as exc:  # noqa: BLE001
            # An unmeasurable case still needs an exit. Most of the ways this is
            # reached -- a relocated worktree, a conditional `cd`, a payload with
            # no directory -- produce the identical refusal on a re-run, so
            # without a token of its own a genuine intent to discard would have
            # nowhere to go.
            if deny_unmeasured(
                command,
                "this command can discard uncommitted work, and the hook could "
                f"not determine what is at stake ({exc}).",
                "Denied rather than allowed, because the loss would be irreversible.",
            ):
                return
            continue

    # THE PARSE ITSELF FAILS CLOSED, and this is where that happens. Everything
    # above answers about calls this hook managed to read; a covered verb it
    # failed to read is not answered at all, and "not answered" reaches the
    # caller as silence -- the command runs. Every parser gap found here has
    # surfaced that way: a `$(...)` splitting the line at the tokenizer's `(`, a
    # here-document delimiter misread, a `<<<` swallowing the tail. Each was a
    # different mistake with one signature, which is a covered verb sitting in
    # the text with no recognized call to account for it.
    #
    # So that signature is the test. It counts rather than matches, because one
    # unread call among several read ones has to refuse too. It is deliberately
    # cruder than the parse it backstops: quoted runs come out first, and a
    # `git` that is merely NEAR a verb word -- `echo git reset --hard >> log` --
    # is refused. That is the cost of the guarantee, and it is a token, not a
    # loss.
    if mentions(command) > recognized:
        deny_unmeasured(
            command,
            "this command names a git verb that can discard uncommitted work, "
            "in a form the hook could not read as a call -- so what is at "
            "stake was never measured.",
            "It may well discard nothing: a line that writes a script or echoes "
            "a command reaches here too, because unread text is unread whatever "
            "it turns out to say. Refusing is the only answer that cannot be "
            "wrong in the expensive direction.",
        )


if __name__ == "__main__":
    main()
