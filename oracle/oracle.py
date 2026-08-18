"""Shared machinery for the acceptance sweeps: fixtures, the oracle, the hook.

THE ORACLE IS GIT. Every sweep here works the same way: build a throwaway
repository holding content that exists only in the worktree, ask the hook what it
would decide, then run the command for real and look at what survived. Content
that afterwards is in neither the worktree nor the object store is GONE, and GONE
is the one loss this hook exists to prevent.

Running the command is the whole point, and it is not a formality. No amount of
reading tells you which spellings reach the worktree: `git switch -C` looks
destructive and is not, `git checkout -f -b new` looks narrowed and is not, and
`git clean --force` destroys nothing at all without `-d`. Every one of those was
established by running it.

An assertion with no oracle does worse than nothing here. Asserting instead that
every wrapped covered call must be refused reports failures that are the hook
being right: `cd sub && git checkout -- tracked.txt` resolves its pathspec
against `sub/`, matches nothing, and exits 1 having destroyed nothing. Acting on
such a report means breaking a correct hook to satisfy a false claim.

NOTHING here runs outside a temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

# The shell a case is actually run under. `shell=True` on its own means
# `/bin/sh`, and there `cat <(git reset --hard)` is a syntax error: the command
# never runs, nothing is destroyed, and the row reports green having measured
# nothing. A check that cannot go red is worse than an absent one, so the shell
# whose grammar `a2_grammar.WRAPPERS` is written in has to be the one that reads
# it. Missing bash is raised rather than worked around, for the same reason.
#
# Resolved here but only REQUIRED in `verdict`, which is the one thing that runs
# a command. `a2b_counting` imports this module for its fixtures and runs no
# shell at all, so a raise at import would fail a sweep on a dependency it does
# not have.
SHELL = shutil.which("bash")

# The hook under test, as an argv. Overridable because `negative_controls.py` points the
# sweeps at deliberately broken copies: a sweep that cannot report red proves
# nothing by reporting green.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_ARGV: list[str] = json.loads(
    os.environ.get(
        "ORACLE_HOOK_ARGV",
        json.dumps([str(PROJECT_ROOT / ".venv" / "bin" / "block-git-discard")]),
    )
)


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check
    )


# --- fixtures ---------------------------------------------------------------


def base_repo(repo: Path) -> None:
    """Two commits, a branch to switch to, and an ignore rule to hide behind."""
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text("ignored/\n*.ign\n")
    (repo / "tracked.txt").write_text("committed\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "nested.txt").write_text("committed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    # A second commit so `HEAD~1` resolves: several destructive spellings reach
    # the worktree only when they are moving to a different commit.
    (repo / "tracked.txt").write_text("committed\nsecond\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "second")
    git(repo, "branch", "other")


def st_worktree(repo: Path) -> None:
    """A tracked file edited but never staged: the case this hook was built for."""
    base_repo(repo)
    (repo / "tracked.txt").write_text("committed\nPRECIOUS-worktree\n")
    (repo / "sub" / "nested.txt").write_text("committed\nPRECIOUS-nested\n")


def st_untracked(repo: Path) -> None:
    base_repo(repo)
    (repo / "new.txt").write_text("PRECIOUS-untracked\n")
    (repo / "sub" / "new2.txt").write_text("PRECIOUS-untracked2\n")


def st_ignored(repo: Path) -> None:
    """Ignored content, which `-u` does not reach and `--all` does."""
    base_repo(repo)
    (repo / "secret.ign").write_text("PRECIOUS-ignored\n")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "x.txt").write_text("PRECIOUS-ignored-dir\n")


def st_mixed(repo: Path) -> None:
    base_repo(repo)
    (repo / "tracked.txt").write_text("committed\nPRECIOUS-worktree\n")
    (repo / "new.txt").write_text("PRECIOUS-untracked\n")
    (repo / "secret.ign").write_text("PRECIOUS-ignored\n")


def st_staged_then_dirty(repo: Path) -> None:
    """Staged content plus a later worktree edit.

    Only the edit can go GONE. `git add` wrote the staged blob into the object
    store, so losing the index entry leaves it reachable by `git fsck` -- which
    is why this hook does not guard index-only content, and why this state exists
    to check that the sweep agrees.
    """
    base_repo(repo)
    (repo / "tracked.txt").write_text("committed\nSTAGED\n")
    git(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_text("committed\nSTAGED\nPRECIOUS-after-add\n")


def st_everything(repo: Path) -> None:
    """One tree carrying every kind at once, for sweeps that vary the command."""
    base_repo(repo)
    (repo / "tracked.txt").write_text("committed\nPRECIOUS\n")
    (repo / "sub" / "nested.txt").write_text("committed\nPRECIOUS\n")
    (repo / "untracked.txt").write_text("PRECIOUS\n")
    (repo / "sub" / "u.txt").write_text("PRECIOUS\n")


STATES = {
    "worktree": st_worktree,
    "untracked": st_untracked,
    "ignored": st_ignored,
    "mixed": st_mixed,
    "staged+dirty": st_staged_then_dirty,
}


# --- the oracle -------------------------------------------------------------


def worktree_blobs(repo: Path) -> dict[str, set[Path]]:
    """git blob id -> the paths currently holding that content."""
    out: dict[str, set[Path]] = {}
    for p in repo.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            data = p.read_bytes()
            h = hashlib.sha1(b"blob %d\0" % len(data) + data)
            out.setdefault(h.hexdigest(), set()).add(p.relative_to(repo))
    return out


def in_store(repo: Path, blob: str) -> bool:
    return git(repo, "cat-file", "-e", blob, check=False).returncode == 0


def at_risk(repo: Path) -> dict[str, set[Path]]:
    """Worktree content the object store has no copy of -- what can be lost."""
    return {b: ps for b, ps in worktree_blobs(repo).items() if not in_store(repo, b)}


def gone(repo: Path, before: dict[str, set[Path]]) -> dict[str, set[Path]]:
    """Of the at-risk content, what is now in neither the worktree nor the store."""
    now = worktree_blobs(repo)
    return {b: ps for b, ps in before.items() if b not in now and not in_store(repo, b)}


def ask_hook(command: str, repo: Path) -> str:
    """What the hook decides: 'deny', 'allow', or 'error' for a non-zero exit.

    'error' is not a third opinion -- the harness reports it as its own row
    because a hook that exits non-zero is reported by Claude Code as a
    non-blocking error and the command then runs. It is a fail-open wearing a
    crash's clothes.
    """
    payload = json.dumps({"tool_input": {"command": command}, "cwd": str(repo)})
    proc = subprocess.run(HOOK_ARGV, input=payload, capture_output=True, text=True)
    if proc.returncode != 0:
        return "error"
    return "deny" if proc.stdout.strip() else "allow"


def verdict(command: str, build, cwd_suffix: str = "") -> tuple[str, dict, int]:
    """(decision, what went GONE, the command's exit code) for one case."""
    import tempfile

    if SHELL is None:
        raise RuntimeError(
            "the acceptance sweeps need bash: the shell constructions they "
            "declare are bash's, and /bin/sh rejects several of them at parse "
            "time -- a row that never runs destroys nothing and reports green"
        )

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        build(repo)
        # The hook is told the directory the command is ACTUALLY run from, which
        # is why `cwd_suffix` reaches both. Let the payload name one directory
        # and the command run in another, and a hook that measures the wrong tree
        # reads as correct: the sweep is then asking about a tree nothing
        # touched.
        run_in = repo / cwd_suffix if cwd_suffix else repo
        decision = ask_hook(command, run_in)
        before = at_risk(repo)
        proc = subprocess.run(
            command,
            cwd=run_in,
            shell=True,
            executable=SHELL,
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
        return decision, gone(repo, before), proc.returncode


def print_coverage(label: str, items: list) -> None:
    """A count this sweep did not judge, and then what it did not judge.

    Printed on every run, green or red, and the count comes first so a nonzero
    one is visible without reading the list. Coverage a sweep did not attempt is
    the half of its result a pass/fail line cannot carry: a case nothing looked
    at and a case that passed are the same blank space in the report otherwise.
    """
    print(f"  {label:20}: {len(items)}")
    for item in items:
        print(f"      {item}")


def report(name: str, total: int, destructive: int, misses: list) -> int:
    print(f"{name}")
    print(f"  cases               : {total}")
    print(f"  actually destroyed  : {destructive}")
    print(f"  FAIL-OPEN (missed)  : {len(misses)}")
    print()
    for label, command, lost in misses:
        print(f"  MISS [{label}] {command!r}")
        for _blob, paths in list(lost.items())[:3]:
            print(f"       lost: {sorted(str(p) for p in paths)}")
    return 1 if misses else 0
