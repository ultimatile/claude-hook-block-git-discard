"""Behaviour of the block-git-discard hook.

The hook decides by running read-only git queries against a real repository, so
these tests build real repositories under `tmp_path` rather than stubbing git.
The harness sends the working directory in the JSON payload and also launches the
hook somewhere; those are separate inputs here, because the hook resolves paths
from the payload and has to keep working when the two disagree.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from conftest import HookRunner

HOOK = "block-git-discard.py"
ACK = re.compile(r"ack:([0-9a-f]{16})")


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout


def init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", ".")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One committed file, one sibling branch, and a clean working tree."""
    r = init(tmp_path / "r")
    (r / "a.txt").write_text("v1\n")
    (r / "k.txt").write_text("keep\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "c1")
    git(r, "branch", "other")
    return r


def dirty(repo: Path, name: str = "a.txt", text: str = "DIRTY\n") -> Path:
    (repo / name).write_text(text)
    return repo / name


# --- nothing at stake -------------------------------------------------------


def test_clean_tree_is_not_interrupted(deny_reason: HookRunner, repo: Path) -> None:
    """The whole point of measuring: a checkout with nothing to lose must pass."""
    assert deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=repo) is None


def test_empty_command_is_allowed(deny_reason: HookRunner) -> None:
    assert deny_reason(HOOK, "") is None


def test_unrelated_command_is_allowed(deny_reason: HookRunner, repo: Path) -> None:
    assert deny_reason(HOOK, "ls -la", payload_cwd=repo) is None


# --- the shape the hook exists for -----------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git checkout -- a.txt",
        "git checkout a.txt",
        "git checkout HEAD -- a.txt",
        "git checkout -- .",
        "git restore a.txt",
        "git restore -SW a.txt",
        "git restore --worktree a.txt",
        "git reset --hard",
    ],
)
def test_discarding_shapes_are_denied(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    dirty(repo)
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None, command
    assert "a.txt" in reason


def test_reason_names_the_alternatives_before_the_override(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The routing is the payload; the override is the last resort, not the first."""
    dirty(repo)
    reason = deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=repo)
    assert reason is not None
    assert "restore -p" in reason
    assert reason.index("restore -p") < reason.index("ack:")


# --- shapes that must pass --------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git checkout other",  # branch switch preserves the working tree
        "git checkout -b fresh",
        "git switch -C fresh",
        "git checkout -B fresh",
        "git restore --staged a.txt",  # index only; the file keeps its content
        "git restore -S a.txt",
        "git reset --soft HEAD",
        "git reset --keep HEAD",
        "git reset HEAD",
        "git checkout -p -- a.txt",  # picks hunks, so it takes no collateral
        "git restore -p a.txt",
        "git clean -n",
        "git clean --dry-run -d",
        "git rm --cached a.txt",
        "git status",
        "git diff",
    ],
)
def test_non_discarding_shapes_pass(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "git rm -f a.txt",
        "git read-tree -u --reset HEAD",
        "git checkout-index -f -a",
        "git merge --abort",
        "git stash",
    ],
)
def test_shapes_outside_the_covered_list_pass_through(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """These do destroy uncommitted content, and are deliberately not covered.

    Covering them means recognizing plumbing and sequencer verbs whose safe
    measurement needs git's own worktree/index normalization, and an agent does
    not type them. The limit is stated in the hook's header; this pins it so a
    later reader sees it as a decision rather than a gap, and so widening the
    list is a visible change here.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


# --- the override token -----------------------------------------------------


def issue_token(deny_reason: HookRunner, repo: Path, command: str) -> str:
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None
    found = ACK.search(reason)
    assert found is not None, reason
    return found.group(1)


def test_matching_ack_passes(deny_reason: HookRunner, repo: Path) -> None:
    dirty(repo)
    token = issue_token(deny_reason, repo, "git checkout -- a.txt")
    assert (
        deny_reason(HOOK, f"git checkout -- a.txt # ack:{token}", payload_cwd=repo)
        is None
    )


def test_wrong_ack_denies(deny_reason: HookRunner, repo: Path) -> None:
    dirty(repo)
    reason = deny_reason(
        HOOK, "git checkout -- a.txt # ack:0000000000000000", payload_cwd=repo
    )
    assert reason is not None


def test_ack_expires_when_the_content_changes(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Binding to a --stat summary would not catch this: an edit that keeps the
    line count leaves the summary byte-identical, so the old token would live on
    over content it never described."""
    dirty(repo, text="first\n")
    token = issue_token(deny_reason, repo, "git checkout -- a.txt")
    dirty(repo, text="second\n")  # same line count, different content
    assert (
        deny_reason(HOOK, f"git checkout -- a.txt # ack:{token}", payload_cwd=repo)
        is not None
    )


def test_ack_is_read_from_the_raw_command_not_the_tokens(
    deny_reason: HookRunner, repo: Path
) -> None:
    """shlex treats `#` as a comment and drops the rest of the line, so a
    token-based read would never see the ack and the override could never be
    exercised. This pins the raw-string read."""
    from shell_tokens import tokenize

    assert "ack" not in " ".join(tokenize("git checkout -- a.txt # ack:abc"))
    dirty(repo)
    token = issue_token(deny_reason, repo, "git checkout -- a.txt")
    assert (
        deny_reason(HOOK, f"git checkout -- a.txt # ack:{token}", payload_cwd=repo)
        is None
    )


# --- untracked files --------------------------------------------------------


def test_clean_denies_when_untracked_files_exist(
    deny_reason: HookRunner, repo: Path
) -> None:
    (repo / "new.txt").write_text("work\n")
    reason = deny_reason(HOOK, "git clean -fd", payload_cwd=repo)
    assert reason is not None
    assert "new.txt" in reason


def test_clean_passes_on_a_clean_tree(deny_reason: HookRunner, repo: Path) -> None:
    assert deny_reason(HOOK, "git clean -fd", payload_cwd=repo) is None


def test_clean_ignores_ignored_files_without_x(
    deny_reason: HookRunner, repo: Path
) -> None:
    (repo / ".gitignore").write_text("skip.txt\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore")
    (repo / "skip.txt").write_text("ignored\n")
    assert deny_reason(HOOK, "git clean -fd", payload_cwd=repo) is None
    assert deny_reason(HOOK, "git clean -fdx", payload_cwd=repo) is not None
    assert deny_reason(HOOK, "git clean -fX", payload_cwd=repo) is not None


# --- ref versus path --------------------------------------------------------


@pytest.fixture
def ambiguous(tmp_path: Path) -> Path:
    """`shared` names both a branch and a tracked file, which is where git's own
    resolution order becomes observable."""
    r = init(tmp_path / "amb")
    (r / "shared").write_text("v1\n")
    (r / "onlyfile").write_text("v1\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "c1")
    git(r, "branch", "shared")
    (r / "shared").write_text("DIRTY\n")
    (r / "onlyfile").write_text("DIRTY\n")
    return r


def test_ref_wins_without_a_double_dash(
    deny_reason: HookRunner, ambiguous: Path
) -> None:
    # git resolves `shared` as the branch, so this is a switch, not a discard.
    assert deny_reason(HOOK, "git checkout shared", payload_cwd=ambiguous) is None


def test_double_dash_forces_the_path_reading(
    deny_reason: HookRunner, ambiguous: Path
) -> None:
    assert (
        deny_reason(HOOK, "git checkout -- shared", payload_cwd=ambiguous) is not None
    )


def test_a_name_that_is_only_a_path_is_a_discard(
    deny_reason: HookRunner, ambiguous: Path
) -> None:
    assert deny_reason(HOOK, "git checkout onlyfile", payload_cwd=ambiguous) is not None


# --- pathspecs --------------------------------------------------------------


@pytest.fixture
def nested(tmp_path: Path) -> Path:
    r = init(tmp_path / "nest")
    (r / "sub").mkdir()
    (r / "a.txt").write_text("a\n")
    (r / "b.txt").write_text("b\n")
    (r / "sub" / "s.txt").write_text("s\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "c1")
    return r


def test_pathspec_narrows_to_the_named_file(
    deny_reason: HookRunner, nested: Path
) -> None:
    (nested / "b.txt").write_text("CHANGED\n")
    # a.txt is untouched, so discarding it loses nothing.
    assert deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=nested) is None
    assert deny_reason(HOOK, "git checkout -- b.txt", payload_cwd=nested) is not None


def test_git_pathspec_magic_is_forwarded(deny_reason: HookRunner, nested: Path) -> None:
    """The hook hands pathspecs to git rather than interpreting them, so exclude
    magic has to keep working."""
    (nested / "a.txt").write_text("CHANGED\n")
    assert (
        deny_reason(HOOK, "git checkout -- ':(exclude)a.txt' .", payload_cwd=nested)
        is None
    )
    assert deny_reason(HOOK, "git checkout -- '*.txt'", payload_cwd=nested) is not None


def test_shell_expandable_pathspec_falls_back_to_the_whole_tree(
    deny_reason: HookRunner, nested: Path
) -> None:
    """`{a,b}.txt` reaches the hook unexpanded, so it cannot be forwarded as
    written; measuring everything over-detects rather than letting it through."""
    (nested / "sub" / "s.txt").write_text("CHANGED\n")
    assert (
        deny_reason(HOOK, "git checkout -- 'a{1,2}.txt'", payload_cwd=nested)
        is not None
    )


def test_cd_is_followed_and_scopes_the_measurement(
    deny_reason: HookRunner, nested: Path
) -> None:
    (nested / "sub" / "s.txt").write_text("CHANGED\n")
    assert (
        deny_reason(HOOK, "cd sub && git checkout -- .", payload_cwd=nested) is not None
    )
    git(nested, "checkout", "--", "sub/s.txt")
    (nested / "a.txt").write_text("CHANGED\n")
    # The change is outside sub/, so a checkout scoped there discards nothing.
    assert deny_reason(HOOK, "cd sub && git checkout -- .", payload_cwd=nested) is None


# --- fail-closed ------------------------------------------------------------


def test_missing_payload_cwd_denies(deny_reason: HookRunner, repo: Path) -> None:
    dirty(repo)
    assert deny_reason(HOOK, "git checkout -- a.txt") is not None


def test_non_repository_cwd_denies(deny_reason: HookRunner, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=plain) is not None


def test_relocated_worktree_denies(deny_reason: HookRunner, repo: Path) -> None:
    """`--work-tree` moves what the command acts on, so the measurement taken in
    the resolved directory would describe a different tree."""
    dirty(repo)
    assert (
        deny_reason(
            HOOK, "git --git-dir=/x --work-tree=/y checkout -- a.txt", payload_cwd=repo
        )
        is not None
    )


def test_conditional_cd_denies(deny_reason: HookRunner, repo: Path) -> None:
    """After `||` the shell may or may not have changed directory, so replaying
    it would measure somewhere the command never reached."""
    dirty(repo)
    assert (
        deny_reason(
            HOOK, "cd nowhere || cd elsewhere; git reset --hard", payload_cwd=repo
        )
        is not None
    )


def test_pathspec_from_file_denies(deny_reason: HookRunner, repo: Path) -> None:
    dirty(repo)
    assert (
        deny_reason(
            HOOK, "git checkout --pathspec-from-file=list.txt", payload_cwd=repo
        )
        is not None
    )


def test_payload_cwd_wins_over_the_process_directory(
    deny_reason: HookRunner, repo: Path, tmp_path: Path
) -> None:
    """The harness picks where the hook runs; the payload says where the command
    will run. Only the latter describes what is at stake."""
    dirty(repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert (
        deny_reason(HOOK, "git checkout -- a.txt", None, elsewhere, payload_cwd=repo)
        is not None
    )


# --- binary content ---------------------------------------------------------


def test_binary_change_is_denied_without_hunk_headers(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`git diff -U0` emits no `@@` for binaries, so a reason built only from
    hunk headers would come out empty and the deny would look unfounded."""
    (repo / "bin.dat").write_bytes(b"\x00\x01binary-v1")
    git(repo, "add", "bin.dat")
    git(repo, "commit", "-qm", "bin")
    (repo / "bin.dat").write_bytes(b"\x00\x01binary-EDITED")
    reason = deny_reason(HOOK, "git checkout -- bin.dat", payload_cwd=repo)
    assert reason is not None
    assert "bin.dat" in reason
