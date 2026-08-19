"""The frozen readable-command scope, as code.

Each cell is swept along one axis against a baseline; the axes are not crossed with
each other. Axis J holds the pairs the code couples, so an interaction outside J is
outside the set.

**No expected verdict is written down.** Each cell's answer comes from running the
command in a throwaway repository and comparing the bytes:

    executing it destroys content that existed beforehand -> the hook must deny
    executing it destroys nothing                         -> the hook must allow,
        unless the shape is in `DECLARED_OVER_REFUSALS`, where a deny is accepted

Content means content no git object holds -- a tracked file's uncommitted change, an
untracked or ignored file, a repository nested inside the worktree. Anything still
reachable from a ref was not destroyed.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

AUTHOR = ["-c", "user.email=t@t", "-c", "user.name=t"]

# A pager would wait for a terminal that is not there, and `man git checkout` is one
# of the declared over-refusals this enumeration executes.
CASE_ENV = {
    **os.environ,
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "MANPAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
}

# `shell=True` would run /bin/sh, where `<(...)` is a syntax error -- the process
# substitution case would then never execute, and its loss would read as no loss at
# all. The shapes this hook is handed come from a Bash tool, so the shell has to
# have the syntax they are written in.
CASE_SHELL = "/bin/bash"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=CASE_ENV,
    ).stdout


@dataclass
class Fixture:
    """A tree, and exactly which bytes in it are unrecoverable if they move.

    `at_risk` is the oracle. Registering a file is the claim that its current bytes
    exist in no git object, so any change to them is a destruction -- which is why
    every helper file a case needs is registered too rather than merely written: a
    `clean` really does remove them, and an unregistered loss reads as no loss and
    turns a correct refusal into a reported over-refusal.
    """

    root: Path
    payload: Path
    at_risk: dict[Path, bytes] = field(default_factory=dict)

    def risk(self, path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        self.at_risk[path] = body

    def lost(self) -> list[str]:
        gone = []
        for path, body in self.at_risk.items():
            try:
                if path.read_bytes() != body:
                    gone.append(f"{path.name} (changed)")
            except OSError:
                gone.append(f"{path.name} (gone)")
        return gone


def base_repo(where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    git(where, "init", "-q", ".")
    (where / "a.txt").write_bytes(b"v1\n")
    (where / "sub").mkdir(exist_ok=True)
    (where / "sub" / "c.txt").write_bytes(b"v1\n")
    (where / ".gitignore").write_bytes(b"ignored/\n")
    git(where, "add", "-A")
    git(where, *AUTHOR, "commit", "-qm", "base")
    git(where, "branch", "other")
    return where


WEIRD_NAMES = ('q"1.txt', "q 2>3.txt", "q #4.txt", "q\t5.txt", "qé.txt")


def build(where: Path, kind: str) -> Fixture:
    """The tree a cell runs against, by the shape of stake it needs."""
    where.mkdir(parents=True, exist_ok=True)

    if kind == "plain":
        holder = where / "dir"
        holder.mkdir()
        f = Fixture(where, holder)
        f.risk(holder / "notes.txt", b"outside any repository\n")
        # Cells here name `{other}`: a relocating global reaches a tree the launch
        # directory says nothing about, and with no such tree the command exits
        # having touched nothing, which would read as a correct allow.
        f.risk(base_repo(where / "other") / "a.txt", b"PRECIOUS elsewhere\n")
        return f

    main = base_repo(where / "main")
    f = Fixture(where, main)

    if kind == "clean-payload":
        # Nothing at risk in the payload tree on purpose: an allow here must not be
        # explainable by the hook having measured the right tree and found it empty.
        f.risk(base_repo(where / "other") / "a.txt", b"PRECIOUS elsewhere\n")
        return f
    if kind == "tracked":
        f.risk(main / "a.txt", b"DIRTY, never committed\n")
        f.risk(main / "sub" / "c.txt", b"DIRTY in a subdirectory\n")
        return f
    if kind == "untracked":
        f.risk(main / "loose.txt", b"PRECIOUS untracked\n")
        f.risk(main / "newmodule" / "impl.py", b"PRECIOUS in an untracked dir\n")
        return f
    if kind == "ignored":
        f.risk(main / "ignored" / "art.o", b"PRECIOUS but ignored\n")
        return f
    if kind == "nested":
        inner = main / "nested"
        inner.mkdir()
        git(inner, "init", "-q", ".")
        (inner / "page.md").write_bytes(b"a chapter\n")
        git(inner, "add", "page.md")
        git(inner, *AUTHOR, "commit", "-qm", "inner")
        # Committed inside the nested repo, and still at risk: its whole object
        # store sits in the directory a `-ff` removes, so nothing survives to
        # restore it from.
        f.at_risk[inner / "page.md"] = (inner / "page.md").read_bytes()
        return f
    if kind == "weird-tracked":
        for name in WEIRD_NAMES:
            (main / name).write_bytes(b"v1\n")
        git(main, "add", "-A")
        git(main, *AUTHOR, "commit", "-qm", "weird")
        for name in WEIRD_NAMES:
            f.risk(main / name, b"DIRTY " + name.encode() + b"\n")
        return f
    if kind == "weird-untracked":
        for name in WEIRD_NAMES:
            f.risk(main / name, b"PRECIOUS " + name.encode() + b"\n")
        return f
    raise AssertionError(f"no fixture kind {kind!r}")


# Cells whose command destroys nothing and which the hook denies anyway. Each entry
# carries its reason here, beside the name, because an entry's reason is what makes
# the exemption reviewable and there is nowhere else that holds one.
#
# An entry does work only when its cell destroys nothing and the hook refuses it:
# a loss makes the deny the rule's own answer, and an allow leaves no refusal to
# exempt. Either way the entry is dead -- present, reviewable, and inert.
# `test_every_declared_over_refusal_is_needed` asks the hook and then runs the
# cell, so both halves are measured rather than assumed.
DECLARED_OVER_REFUSALS = {
    "A: echoed verb": (
        "the text names a verb; reading it as a call is the parse this hook declines"
    ),
    "A: grep for a verb": "a pattern argument that happens to spell a call",
    "A: man page": "the verb is the subject of another command, not the command",
    "A: docker wrapper": "the call runs in a container this hook cannot measure",
    "A: bisect reset": "a two-word subcommand whose second word is a covered verb",
    "A: status with a verb operand": "a covered verb sitting in an operand position",
    "F: GIT_DIR assignment": (
        "the assignment moves the tree, so a measurement here describes another one"
    ),
    "H: pathspec from file": "the pathspecs live in a file this hook does not read",
}

# (axis, label, command, fixture kind). `{other}` is filled in with the second
# repository's path.
CELLS: list[tuple[str, str, str, str]] = [
    # A -- which command is a covered call
    ("A", "plain checkout", "git checkout -- a.txt", "tracked"),
    ("A", "plain restore", "git restore a.txt", "tracked"),
    ("A", "hard reset", "git reset --hard", "tracked"),
    ("A", "forced switch", "git switch -f other", "tracked"),
    ("A", "clean", "git clean -fd", "untracked"),
    ("A", "substitution capture", "x=$(git reset --hard)", "tracked"),
    ("A", "process substitution", "cat <(git reset --hard)", "tracked"),
    ("A", "backticks", "echo `git reset --hard`", "tracked"),
    ("A", "gitk is not git", "gitk --all", "tracked"),
    ("A", "git-lfs is not git", "git-lfs env", "tracked"),
    ("A", "echoed verb", "echo git reset --hard >> notes.md", "tracked"),
    ("A", "man page", "man git checkout", "tracked"),
    ("A", "grep for a verb", "grep 'git checkout -- a.txt' log.txt", "tracked"),
    ("A", "docker wrapper", "docker run --rm alpine git clean -fdx", "tracked"),
    ("A", "sh -c payload", "sh -c 'git clean -fdx'", "untracked"),
    ("A", "bisect reset", "git bisect reset", "tracked"),
    ("A", "status with a verb operand", "git status --short reset", "tracked"),
    (
        "A",
        "verb in a commit message body",
        'git commit -q --allow-empty -m "clean up the mess"',
        "tracked",
    ),
    ("A", "indirect name", "$GIT reset --hard", "tracked"),
    # B -- forms that destroy nothing
    ("B", "dry run", "git clean -n", "untracked"),
    ("B", "dry run long", "git clean --dry-run -d", "untracked"),
    ("B", "patch checkout", "git checkout -p -- a.txt", "tracked"),
    ("B", "restore staged only", "git restore --staged a.txt", "tracked"),
    ("B", "soft reset", "git reset --soft HEAD", "tracked"),
    ("B", "mixed reset", "git reset HEAD", "tracked"),
    ("B", "branch creation", "git checkout -b fresh", "tracked"),
    ("B", "unforced switch", "git switch other", "tracked"),
    # C -- quoting and escaping
    ("C", "quoted quote name", 'git checkout -- "q\\"1.txt"', "weird-tracked"),
    ("C", "quoted redirection name", 'git checkout -- "q 2>3.txt"', "weird-tracked"),
    (
        "C",
        "clean quoted redirection",
        'git clean -f -- "q 2>3.txt"',
        "weird-untracked",
    ),
    ("C", "escaped hash name", "git checkout -- q\\ #4.txt", "weird-tracked"),
    ("C", "clean escaped hash", "git clean -f -- q\\ #4.txt", "weird-untracked"),
    ("C", "quoted tab name", 'git checkout -- "q\t5.txt"', "weird-tracked"),
    ("C", "non-ascii name", "git checkout -- qé.txt", "weird-tracked"),
    ("C", "hash inside quotes", "git checkout -- 'q #4.txt'", "weird-tracked"),
    ("C", "real trailing comment", "git reset --hard # tidy up", "tracked"),
    ("C", "real redirection", "git log -1 2>err.txt", "tracked"),
    (
        "C",
        "ansi-c quoting then verb",
        "git commit -q --allow-empty -m $'don\\'t'\ngit reset --hard",
        "tracked",
    ),
    (
        "C",
        "escaped backslash then verb",
        "git log -1 \\\\\ngit reset --hard",
        "tracked",
    ),
    ("C", "line continuation", "git reset \\\n--hard", "tracked"),
    # The tokenizer falls back to a whitespace split here, which glues separators to
    # their neighbours and runs an argument list long. Measured: the long list
    # narrows the query to nothing, the hook allows, and the shell rejects the line
    # anyway -- so nothing is destroyed and no exemption is needed.
    ("C", "unbalanced quote", "git checkout -- 'a.txt", "tracked"),
    # D -- separators and control flow
    ("D", "semicolon", "true ; git reset --hard", "tracked"),
    ("D", "and-and", "true && git reset --hard", "tracked"),
    ("D", "or-or", "false || git reset --hard", "tracked"),
    ("D", "newline", "true\ngit reset --hard", "tracked"),
    ("D", "subshelled call", "true && cd sub && (git checkout -- c.txt)", "tracked"),
    ("D", "verb in a pipeline", "git reset --hard | cat", "tracked"),
    ("D", "backgrounded verb", "git reset --hard &", "tracked"),
    # E -- where the command runs
    ("E", "cd into a subdirectory", "cd sub && git checkout -- .", "tracked"),
    ("E", "cd elsewhere", "cd {other} && git reset --hard", "clean-payload"),
    ("E", "pushd elsewhere", "pushd {other} && git reset --hard", "clean-payload"),
    (
        "E",
        "builtin cd elsewhere",
        "builtin cd {other} && git reset --hard",
        "clean-payload",
    ),
    (
        "E",
        "command cd elsewhere",
        "command cd {other} && git reset --hard",
        "clean-payload",
    ),
    (
        "E",
        "eval before a verb",
        'eval "cd {other}" && git reset --hard',
        "clean-payload",
    ),
    (
        "E",
        "builtin eval",
        'builtin eval "cd {other}" && git reset --hard',
        "clean-payload",
    ),
    (
        "E",
        "command -v runs nothing",
        "command -v rg && git checkout -- a.txt",
        "tracked",
    ),
    ("E", "source is excluded", ". activate && git checkout -- a.txt", "tracked"),
    ("E", "bare cd", "cd && git reset --hard", "tracked"),
    ("E", "conditional cd", "cd nowhere || cd elsewhere; git reset --hard", "tracked"),
    ("E", "git -C elsewhere", "git -C {other} reset --hard", "clean-payload"),
    ("E", "verb outside a repository", "git checkout -- notes.txt", "plain"),
    # F -- git's own options
    ("F", "dash C", "git -C sub checkout -- c.txt", "tracked"),
    (
        "F",
        "config force waiver",
        "git -c clean.requireForce=false clean -d",
        "untracked",
    ),
    (
        "F",
        "relocating global",
        "git --git-dir={other}/.git --work-tree={other} reset --hard",
        "clean-payload",
    ),
    ("F", "unknown global", "git --namespace ns checkout -- a.txt", "tracked"),
    (
        "F",
        "GIT_DIR assignment",
        "GIT_DIR={other}/.git git reset --hard",
        "clean-payload",
    ),
    # G -- what clean reaches
    ("G", "force once", "git clean -f", "untracked"),
    ("G", "force once with dir flag", "git clean -fd", "untracked"),
    ("G", "force once with pathspec", "git clean -f newmodule", "untracked"),
    ("G", "force once dot", "git clean -f .", "untracked"),
    ("G", "ignored needs x", "git clean -fd", "ignored"),
    ("G", "ignored with x", "git clean -fdx", "ignored"),
    ("G", "ignored with X", "git clean -fdX", "ignored"),
    ("G", "nested needs ff", "git clean -fd", "nested"),
    ("G", "nested with ffd", "git clean -ffd", "nested"),
    ("G", "nested with ff pathspec", "git clean -ff nested", "nested"),
    ("G", "exclude widens", "git clean -fd -e keep.txt", "untracked"),
    # H -- which paths the measurement covers
    ("H", "after dashdash", "git checkout -- a.txt", "tracked"),
    ("H", "before dashdash", "git checkout a.txt", "tracked"),
    ("H", "ref operand", "git checkout other", "tracked"),
    ("H", "narrowed away from the stake", "git checkout -- sub/c.txt", "tracked"),
    ("H", "icase pathspec", "git clean -fd ':(icase)NEWMODULE'", "untracked"),
    ("H", "unexpanded pathspec", "git checkout -- $HOME/a.txt", "tracked"),
    (
        "H",
        "pathspec from file",
        "git checkout --pathspec-from-file=list.txt",
        "tracked",
    ),
    # I -- the override token: each cell needs two invocations, so those live in
    # tests/test_hook.py with the other token round-trips.
    # J -- the pairs the code couples, where every fail-open so far has lived
    ("J", "force count x pathspec", "git clean -ff newmodule", "untracked"),
    ("J", "pathspec x collapsed dir", "git clean -f newmodule", "untracked"),
    ("J", "quoting x pathspec", 'git clean -f -- "q 2>3.txt"', "weird-untracked"),
    ("J", "escaping x comment scan", "git clean -f -- q\\ #4.txt", "weird-untracked"),
    (
        "J",
        "wrapper x unreadable",
        'command eval "cd {other}" && git reset --hard',
        "clean-payload",
    ),
    (
        "J",
        "non-repo x relocating global",
        "git --git-dir={other}/.git --work-tree={other} reset --hard",
        "plain",
    ),
    ("J", "subshell x movement", "(cd {other}) ; git reset --hard", "clean-payload"),
]


def name_of(axis: str, label: str) -> str:
    return f"{axis}: {label}"


def execute(command: str, where: Path) -> None:
    """Run the command for real. Its effect on the fixture is the oracle.

    `HOME` is pointed at the fixture, and that is not hygiene -- it is what keeps
    this enumeration from destroying the machine it runs on. The cells are real
    destructive commands run in a real shell, and one of them is `cd && git reset
    --hard`: a bare `cd` goes to `$HOME`, so with the ambient value the hard reset
    lands in the home directory. On a home-as-repository setup -- a dotfiles tree
    checked out at `$HOME` is the common one -- `pytest` would then discard the
    user's own uncommitted work, which is the exact loss this project exists to
    prevent. Pointed at the fixture, the same cell lands in a throwaway tree.

    `GIT_CONFIG_GLOBAL` goes with it: git resolves the global config under `HOME`,
    so moving one without the other reads config from a path that now holds a
    fixture. Silenced explicitly rather than left to follow `HOME`, so a cell's
    answer cannot turn on whatever the machine's own git config happens to say.
    """
    subprocess.run(
        command,
        shell=True,
        executable=CASE_SHELL,
        cwd=str(where),
        capture_output=True,
        env={
            **CASE_ENV,
            "HOME": str(where),
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
        timeout=30,
        stdin=subprocess.DEVNULL,
        check=False,
    )
