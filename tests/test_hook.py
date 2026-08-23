"""Behaviour of the block-git-discard hook, against real repositories under
`tmp_path`: it decides by running git, so the fixtures run git too.

The harness sends a working directory in the payload and also launches the hook
somewhere; those are separate inputs here, the hook resolving paths from the
payload and having to keep working when the two disagree.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from conftest import HookRunner, commit_all, dirty, git, init, repo_holding_work

# The console script `[project.scripts]` installs, which is what settings.json
# invokes. `conftest` resolves it in this checkout's venv.
HOOK = "block-git-discard"
ACK = re.compile(r"ack:([0-9a-f]{16})")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One committed file, one sibling branch, and a clean working tree."""
    r = init(tmp_path / "r")
    (r / "a.txt").write_text("v1\n")
    (r / "k.txt").write_text("keep\n")
    commit_all(r)
    git(r, "branch", "other")
    return r


def untracked(repo: Path, name: str = "untracked.txt") -> Path:
    """A file git has never seen, reachable only through `ls-files --others`, unlike
    `dirty`.
    """
    target = repo / name
    target.write_text("PRECIOUS\n")
    return target


def tracked_dirty(repo: Path, name: str, message: str = "add") -> Path:
    """Commit `name` into `repo`, then change it — for tests that need a particular
    name.
    """
    target = repo / name
    target.write_text("v1\n")
    commit_all(repo, message)
    target.write_text("DIRTY\n")
    return target


def clean_repo(path: Path) -> Path:
    """A second repository with nothing uncommitted: an allow means something only
    if the tree the hook must not have measured holds nothing to lose.
    """
    r = init(path)
    (r / "k.txt").write_text("v\n")
    commit_all(r)
    return r


# --- nothing at stake -------------------------------------------------------


def test_clean_tree_is_not_interrupted(deny_reason: HookRunner, repo: Path) -> None:
    """A checkout with nothing to lose must pass, which is the whole point of
    measuring.
    """
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
        # Forced switches overwrite the worktree wholesale.
        "git checkout -f other",
        "git switch -f other",
        "git switch --discard-changes other",
        "git checkout --force other",
    ],
)
def test_discarding_shapes_are_denied(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    dirty(repo)
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None, command
    assert "a.txt" in reason


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("git switch -C fresh", "force-create moves a label, not the worktree"),
        ("git checkout -B fresh", "same, in checkout's spelling"),
        ("git switch other", "git refuses a plain switch that would lose work"),
        ("git switch -c fresh", "branch creation keeps the tree"),
    ],
)
def test_force_create_is_not_force_discard(
    deny_reason: HookRunner, repo: Path, command: str, why: str
) -> None:
    """`-B` / `-C` read like force but only move a branch label."""
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, why


def test_reason_names_the_alternatives_before_the_override(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The routing is the payload; the override is the last resort, not the first."""
    dirty(repo)
    reason = deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=repo)
    assert reason is not None
    assert "stash push" in reason
    assert reason.index("stash push") < reason.index("ack:")


def test_alternatives_are_runnable_without_a_terminal(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The denied caller has no tty, so an interactive route is one it cannot take."""
    dirty(repo)
    reason = deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=repo)
    assert reason is not None
    for interactive in ("restore -p", "checkout -p", "add -i", "add -p"):
        assert interactive not in reason, interactive


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
    """Verbs outside `COVERED` pass, including ones that reach the worktree.
    Widening the list has to show up as a change here.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


def test_staged_only_content_is_not_protected(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`git add` writes the blob into the object store, so the loss is recoverable;
    the hook measures worktree-versus-index, which reports nothing here.
    """
    (repo / "a.txt").write_text("STAGED\n")
    git(repo, "add", "a.txt")
    # `checkout -- <path>` restores from the index, not from HEAD, so this leaves
    # the worktree matching the index with both ahead of HEAD. Restoring from HEAD
    # instead would make worktree and index differ, which the hook does report.
    git(repo, "checkout", "--", "a.txt")
    assert git(repo, "diff", "--name-only") == "", "fixture did not reach the state"
    assert deny_reason(HOOK, "git reset --hard", payload_cwd=repo) is None


@pytest.mark.parametrize(
    "command",
    [
        "LC_ALL=C git checkout -- a.txt",
        "GIT_PAGER=cat LANG=C git checkout -- a.txt",
        "env git checkout -- a.txt",
        "sudo git reset --hard",
        "nice git restore a.txt",
    ],
)
def test_a_prefixed_git_call_is_still_recognized(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """An env assignment or a wrapper puts `git` past argv[0]."""
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "clean up the mess"',
        "git commit -m 'restore the old layout'",
        'git commit -m "reset expectations with the team"',
        "git log --grep clean",
    ],
)
def test_a_verb_word_without_git_in_front_is_left_alone(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """The backstop reads the verb in subcommand position, not anywhere in the line."""
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'git reset --hard'",
        'bash -lc "git clean -fdx"',
        'eval "git reset --hard"',
        "grep 'git checkout -- a.txt' log.txt",
        'echo "git reset --hard $(date)" >> notes.md',
    ],
)
def test_a_quoted_git_command_is_refused(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """Quoted text is read as unreadable, because the receiving command may execute
    it and that set has no boundary. The `grep` and the `echo` are the price.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        "echo git reset --hard >> notes.md",
        "printf 'x' && echo git clean -fdx >> notes.md",
    ],
)
def test_an_unquoted_mention_of_git_is_refused(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """Unquoted, this is textually indistinguishable from a parse gap."""
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        "man git checkout",
        "tldr git checkout",
        "docker run --rm alpine git clean -fdx",
    ],
)
def test_git_beside_a_verb_under_an_unread_wrapper_is_refused(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """The rest of the accepted false positives, pinned as a class."""
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "block git clean when untracked files exist"',
        'git commit -am "note: git reset --hard someday"',
        'git log --grep "git reset --hard"',
        'git config note.text "git checkout -- ."',
        'git tag -a v9 -m "after git reset --hard"',
        'git stash push -m "before git checkout -- ." -- a.txt',
        'git notes add -m "git switch -f other broke this"',
    ],
)
def test_a_verb_inside_an_inert_subcommands_arguments_is_left_alone(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """A `git <verb>` in the arguments of a call that does not run its arguments is
    text. The allowlist is the safe direction, an omission from it costing a refusal
    and not a hole.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "git submodule foreach 'git reset --hard'",
        "git bisect run git reset --hard",
        "git -c alias.zz='!git reset --hard' zz",
        "git rebase -x 'git reset --hard' HEAD~1",
        # A separator ends the guard: what follows is its own call again.
        'git commit -m "x" && git reset --hard',
        'git commit -m "x"; git checkout -- a.txt',
    ],
)
def test_a_subcommand_that_runs_its_argument_is_still_refused(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """The exceptions the allowlist must not swallow, each run for real and observed
    to destroy content.

    An unstaged change makes `rebase -x` refuse to start, so it was confirmed on a
    clean tracked tree.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "first",
    [
        'git commit -m "wip"',
        "git log -1",
        "git branch -a",
        "git stash list",
        "git config user.name",
    ],
)
def test_the_inert_guard_does_not_cross_a_line_break(
    deny_reason: HookRunner, repo: Path, first: str
) -> None:
    """A newline ends a command as a `;` does and, being whitespace, never survives
    into a word for the separator test to find.
    """
    dirty(repo)
    command = f"{first}\nsh -c 'git reset --hard'"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "first", ["git log -1", "git config user.name", 'git commit -m "wip"']
)
def test_a_backslash_inside_a_comment_does_not_join_the_next_line(
    deny_reason: HookRunner, repo: Path, first: str
) -> None:
    """A backslash inside a comment is ordinary text to the shell — `echo one # note
    \\` then `echo two` prints both, verified in bash and zsh.
    """
    dirty(repo)
    command = f"{first} # note \\\ngit reset --hard"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_a_quoted_newline_does_not_end_the_inert_guard(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A newline inside quotes is part of a word, which is how a commit body is
    written.
    """
    dirty(repo)
    body = 'git commit -am "subject line\n\nbody mentions git reset --hard here"'
    assert deny_reason(HOOK, body, payload_cwd=repo) is None, body


@pytest.mark.parametrize(
    "command",
    [
        "$GIT reset --hard",
        "${GIT} clean -fdx",
        "git co -- a.txt",  # a `checkout` alias: the VERB resolves at run time
    ],
)
def test_a_name_that_resolves_only_at_run_time_is_the_backstop_floor(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """The limit of the guarantee. Both halves of the call have to be written in the
    text, and neither parser reads a name or a verb that resolves at run time.

    `$(echo git) reset --hard` is deliberately off the list, the word `git` being
    written there.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


@pytest.mark.parametrize(
    "command", ["git clean -fd -e keep.txt", "git clean -fd --exclude=keep.txt"]
)
def test_clean_with_an_exclude_drops_the_narrowing(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """`-e` narrows what clean removes in a way `ls-files` does not mirror, and both
    spellings have to land the same way.
    """
    (repo / "new.txt").write_text("work\n")
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_untracked_paths_are_listed_in_the_frame_the_reason_names(
    deny_reason: HookRunner, nested: Path
) -> None:
    """`ls-files --others` reports cwd-relative paths and `diff --name-only`
    root-relative ones, while the reason names a single frame for both.
    """
    (nested / "sub" / "fresh.txt").write_text("work\n")
    reason = deny_reason(
        HOOK, "git clean -fd", None, nested / "sub", payload_cwd=nested / "sub"
    )
    assert reason is not None
    assert "sub/fresh.txt" in reason, reason


def test_ignored_files_are_routed_to_the_stash_form_that_reaches_them(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`-u` stops at ignored files; only `--all` reaches them."""
    (repo / ".gitignore").write_text("skip.txt\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore")
    (repo / "skip.txt").write_text("ignored\n")
    reason = deny_reason(HOOK, "git clean -fX", payload_cwd=repo)
    assert reason is not None
    assert "stash push --all" in reason, reason


def test_a_dash_leading_pathspec_is_not_read_as_a_flag(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A filename after `--` is a pathspec, whatever it starts with. Read as
    options, `-patch.txt` splits per character and the `p` routes a real discard
    into the interactive branch.
    """
    tracked_dirty(repo, "-patch.txt", "dash")
    reason = deny_reason(HOOK, "git checkout -- '-patch.txt'", payload_cwd=repo)
    assert reason is not None, "a dash-leading pathspec was read as -p"
    assert "-patch.txt" in reason


def test_clean_narrows_to_its_pathspec(deny_reason: HookRunner, repo: Path) -> None:
    """`ls-files` takes the same pathspec tail, so a narrowed clean stays narrow."""
    (repo / "build").mkdir()
    (repo / "keepdir").mkdir()
    (repo / "keepdir" / "precious.txt").write_text("keep me\n")
    assert deny_reason(HOOK, "git clean -fd build", payload_cwd=repo) is None
    # A `.txt`, the user's global ignore file being in effect here and
    # `--exclude-standard` honouring it: an ignored name would make this pass for
    # the wrong reason.
    (repo / "build" / "junk.txt").write_text("junk\n")
    reason = deny_reason(HOOK, "git clean -fd build", payload_cwd=repo)
    assert reason is not None
    assert "build/junk.txt" in reason
    assert "precious" not in reason


def nested_repo(parent: Path, name: str = "inner") -> Path:
    """A repository inside `parent`'s working tree, which `parent` does not track.

    `ls-files --others` reports it as a single `inner/` entry and will not look
    inside — the one untracked stake the listing cannot break into files, and the
    heaviest, since a `-ff` takes the object store with it.
    """
    r = init(parent / name)
    (r / "page.md").write_text("a chapter\n")
    commit_all(r)
    return r


def test_a_pathspec_reaches_into_an_untracked_directory(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A pathspec naming a wholly-untracked directory removes it, `-d` or not:
    measured, `git clean -f newmodule` and `git clean -f .` both print `Removing
    newmodule/`. `--directory` collapses it to one name.
    """
    (repo / "newmodule").mkdir()
    (repo / "newmodule" / "impl.py").write_text("work worth keeping\n")
    for command in ("git clean -f newmodule", "git clean -f ."):
        reason = deny_reason(HOOK, command, payload_cwd=repo)
        assert reason is not None, command
        assert "newmodule/impl.py" in reason, reason


def test_a_clean_with_no_pathspec_leaves_an_untracked_directory_alone(
    deny_reason: HookRunner, repo: Path
) -> None:
    """With neither `-d` nor a pathspec git leaves the directory whole."""
    (repo / "newmodule").mkdir()
    (repo / "newmodule" / "impl.py").write_text("work worth keeping\n")
    assert deny_reason(HOOK, "git clean -f", payload_cwd=repo) is None


def test_a_doubly_forced_clean_is_refused_over_a_nested_repository(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`-ff` deletes a directory holding its own `.git`, history included, and
    measured against git it takes either `-d` or a pathspec to reach it.
    """
    nested_repo(repo, "inner")
    for command in ("git clean -ffd", "git clean -ff inner"):
        reason = deny_reason(HOOK, command, payload_cwd=repo)
        assert reason is not None, command
        assert "inner/" in reason, reason


def test_a_singly_forced_clean_is_not_refused_over_a_nested_repository(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Measured: `-fd`, `-f .`, `-f inner` and a bare `-ff` all leave the directory
    whole.
    """
    nested_repo(repo, "inner")
    for command in (
        "git clean -fd",
        "git clean -f .",
        "git clean -f inner",
        "git clean -ff",
    ):
        assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


def test_force_is_counted_from_flag_names_not_from_raw_letters() -> None:
    """An `f` inside an option's value is not a second force: `-efpat` carries the
    pattern `fpat`.
    """
    from block_git_discard.hook import forced_twice

    assert forced_twice(["-ff"])
    assert forced_twice(["-f", "-f"])
    assert forced_twice(["-f", "--force"])
    assert forced_twice(["--force", "--force"])
    assert not forced_twice(["-f"])
    assert not forced_twice(["-fd"])
    assert not forced_twice(["--force"])
    assert not forced_twice(["-f", "-efpat"])


def test_separated_source_value_is_not_taken_as_a_pathspec(
    deny_reason: HookRunner, nested: Path
) -> None:
    """`--source HEAD~1` puts a ref where a naive scan reads a pathspec, and its `~`
    then collapses the measurement to the whole tree.

    Both assertions need the dirty `a.txt`: against a clean one they hold either
    way.
    """
    (nested / "a.txt").write_text("CHANGED\n")
    for command in (
        "git restore --source HEAD~1 a.txt",
        "git restore --source=HEAD~1 a.txt",
        "git restore -s HEAD~1 a.txt",
        "git restore -sHEAD~1 a.txt",
    ):
        reason = deny_reason(HOOK, command, payload_cwd=nested)
        assert reason is not None, command
        assert "a.txt" in reason, reason


def test_a_clean_file_is_not_denied_over_the_source_value(
    deny_reason: HookRunner, nested: Path
) -> None:
    """The other half: the ref must not widen the measurement to an unrelated dirty
    file.
    """
    (nested / "b.txt").write_text("CHANGED\n")
    for command in (
        "git restore --source HEAD~1 a.txt",
        "git restore --source=HEAD~1 a.txt",
        "git restore -s HEAD~1 a.txt",
    ):
        assert deny_reason(HOOK, command, payload_cwd=nested) is None, command


def test_hunks_survive_being_run_from_a_subdirectory(
    deny_reason: HookRunner, nested: Path
) -> None:
    """The names are root-relative, so fed back as pathspecs in a subdirectory they
    resolve to `sub/sub/file`, match nothing, and exit 0.
    """
    (nested / "sub" / "s.txt").write_text("CHANGED\n")
    reason = deny_reason(
        HOOK, "git checkout -- .", None, nested / "sub", payload_cwd=nested / "sub"
    )
    assert reason is not None
    assert "@@" in reason, reason


def assert_untracked_ack_expires(
    deny_reason: HookRunner, workdir: Path, target: Path
) -> None:
    """A token issued for `target`'s body must stop matching once it is rewritten.

    The untracked kinds have no patch to fingerprint, and each caller supplies a
    different way for that fallback to come apart.
    """
    target.write_text("first\n")
    token = issue_token(deny_reason, workdir, "git clean -fd")
    target.write_text("second, longer than the first\n")
    assert (
        deny_reason(HOOK, f"git clean -fd # ack:{token}", payload_cwd=workdir)
        is not None
    )


def test_untracked_ack_expires_when_the_file_is_rewritten(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A fingerprint of only the path list keeps an ack valid over new content."""
    assert_untracked_ack_expires(deny_reason, repo, repo / "new.txt")


# --- the override token -----------------------------------------------------


def issue_token(deny_reason: HookRunner, repo: Path, command: str) -> str:
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None
    found = ACK.search(reason)
    assert found is not None, reason
    return found.group(1)


def test_a_token_for_a_nested_repository_follows_the_content_inside_it(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A directory's own `stat` does not move when a file inside it is rewritten."""
    inner = nested_repo(repo, "inner")
    token = issue_token(deny_reason, repo, "git clean -ffd")
    acked = f"git clean -ffd # ack:{token}"
    assert deny_reason(HOOK, acked, payload_cwd=repo) is None
    (inner / "page.md").write_text("a chapter, rewritten after the token was issued\n")
    assert deny_reason(HOOK, acked, payload_cwd=repo) is not None


def test_two_covered_verbs_on_one_line_can_both_be_acked(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Tokens accumulate as they are appended, so a lookup returning the earliest
    match never converges.
    """
    # Two dirty files, so the narrowed verb and the whole-tree one measure
    # different things and issue different tokens. With one, both measurements
    # coincide and a single token covers them -- correct, but not this.
    dirty(repo, "a.txt")
    dirty(repo, "k.txt", "ALSO DIRTY\n")
    line = "git checkout -- a.txt && git reset --hard"
    first = issue_token(deny_reason, repo, line)
    second_reason = deny_reason(HOOK, f"{line} # ack:{first}", payload_cwd=repo)
    assert second_reason is not None, "the second verb should still be denied"
    second = ACK.search(second_reason)
    assert second is not None
    assert second.group(1) != first
    assert (
        deny_reason(
            HOOK, f"{line} # ack:{first} # ack:{second.group(1)}", payload_cwd=repo
        )
        is None
    )


def test_suggestions_carry_the_frame_the_listed_paths_are_relative_to(
    deny_reason: HookRunner, nested: Path
) -> None:
    """The paths are repo-root-relative and the agent's next call runs in the
    payload cwd, where `sub/s.txt` pasted from inside `sub/` yields `sub/sub/s.txt`.
    """
    (nested / "sub" / "s.txt").write_text("CHANGED\n")
    reason = deny_reason(
        HOOK, "git checkout -- .", None, nested / "sub", payload_cwd=nested / "sub"
    )
    assert reason is not None
    assert str(nested) in reason, reason
    assert "relative to" in reason, reason


def test_the_recovery_route_names_one_directory_for_every_step(
    deny_reason: HookRunner, nested: Path
) -> None:
    """`-C` moves git's directory, not the shell's. Offered as a frame it sends `>
    keep.patch` wherever the caller stands, and `git apply` from there exits 0
    having restored nothing.
    """
    sub = nested / "sub"
    (sub / "s.txt").write_text("PRECIOUS\n")
    reason = deny_reason(HOOK, "git checkout -- .", None, sub, payload_cwd=sub)
    assert reason is not None
    assert "keep.patch" in reason, reason
    assert str(nested) in reason, reason
    assert "-C" not in reason, reason


def test_the_two_recovery_routes_are_marked_as_alternatives(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Read as two steps, the stash moves the content first and the diff writes a
    0-byte patch. The misreading is run below to confirm it is one.
    """
    target = dirty(repo)
    reason = deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=repo)
    assert reason is not None
    assert "EITHER route, not both" in reason, reason

    # The consequence the word exists to prevent, executed in the printed order.
    git(repo, "stash", "push", "--", "a.txt")
    patch = repo / "keep.patch"
    patch.write_text(git(repo, "diff", "--", "a.txt"))
    assert patch.read_text() == "", "the stash already moved it"
    applied = subprocess.run(
        ["git", "apply", "keep.patch"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode != 0, applied.stdout
    git(repo, "stash", "pop")
    assert target.read_text() == "DIRTY\n"


def test_the_recovery_route_does_not_promise_the_restore_succeeds(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A covered verb can move the branch as well as the tree, and a copy taken
    against the old commit may refuse to go back on.
    """
    dirty(repo)
    reason = deny_reason(HOOK, "git checkout -f other", payload_cwd=repo)
    assert reason is not None
    assert "may not go back on cleanly" in reason, reason
    assert "Neither route discards what it could not place" in reason, reason


def test_the_patch_route_round_trips_binary_content(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Without `--binary` the patch is a sentence rather than a copy, and `git
    apply` then refuses the whole thing, text files included.
    """
    (repo / "bin.dat").write_bytes(b"\x00\x01binary-v1")
    commit_all(repo, "bin")
    (repo / "a.txt").write_text("PRECIOUS TEXT\n")
    (repo / "bin.dat").write_bytes(b"\x00\x01binary-PRECIOUS")

    reason = deny_reason(HOOK, "git checkout -- .", payload_cwd=repo)
    assert reason is not None
    assert "git diff --binary -- <path>... > keep.patch" in reason, reason

    (repo / "keep.patch").write_text(git(repo, "diff", "--binary", "--", "."))
    git(repo, "checkout", "-f", "--", ".")
    git(repo, "apply", "keep.patch")
    assert (repo / "a.txt").read_text() == "PRECIOUS TEXT\n"
    assert (repo / "bin.dat").read_bytes() == b"\x00\x01binary-PRECIOUS"


def test_the_untracked_route_names_the_way_back_too(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Naming only the outbound half leaves the reader with content set aside and
    no stated way to retrieve it."""
    (repo / "new.txt").write_text("work\n")
    reason = deny_reason(HOOK, "git clean -fd", payload_cwd=repo)
    assert reason is not None
    assert "stash pop" in reason, reason


def test_untracked_route_uses_the_flag_that_actually_works(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`git stash push -- <untracked>` errors with "did not match any file(s)"."""
    (repo / "new.txt").write_text("work\n")
    reason = deny_reason(HOOK, "git clean -fd", payload_cwd=repo)
    assert reason is not None
    assert "stash push -u" in reason, reason


def test_reason_lists_paths_usable_as_git_operands(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The suggested commands take a `<path>`, and `git diff --stat` abbreviates a
    long one to `.../tail`, which git will not accept.
    """
    deep = "a/very/deeply/nested/directory/tree"
    (repo / deep).mkdir(parents=True)
    target = tracked_dirty(
        repo, f"{deep}/some_source_file_with_a_long_name.txt", "deep"
    )
    reason = deny_reason(HOOK, "git checkout -- .", payload_cwd=repo)
    assert reason is not None
    relative = str(target.relative_to(repo))
    assert relative in reason, f"{relative} not recoverable from:\n{reason}"


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
    """An edit that keeps the line count leaves a `--stat` summary byte-identical."""
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
    """shlex treats `#` as a comment and drops the rest of the line."""
    from block_git_discard.shell_tokens import tokenize

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
    commit_all(r)
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
    commit_all(r)
    return r


def test_pathspec_narrows_to_the_named_file(
    deny_reason: HookRunner, nested: Path
) -> None:
    (nested / "b.txt").write_text("CHANGED\n")
    # a.txt is untouched, so discarding it loses nothing.
    assert deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=nested) is None
    assert deny_reason(HOOK, "git checkout -- b.txt", payload_cwd=nested) is not None


def test_git_pathspec_magic_is_forwarded(deny_reason: HookRunner, nested: Path) -> None:
    """The hook hands pathspecs to git, so exclude magic keeps working."""
    (nested / "a.txt").write_text("CHANGED\n")
    assert (
        deny_reason(HOOK, "git checkout -- ':(exclude)a.txt' .", payload_cwd=nested)
        is None
    )
    assert deny_reason(HOOK, "git checkout -- '*.txt'", payload_cwd=nested) is not None


def test_shell_expandable_pathspec_falls_back_to_the_whole_tree(
    deny_reason: HookRunner, nested: Path
) -> None:
    """`a{1,2}.txt` reaches the hook unexpanded, so it cannot be forwarded as
    written; measuring everything over-detects.
    """
    (nested / "sub" / "s.txt").write_text("CHANGED\n")
    assert (
        deny_reason(HOOK, "git checkout -- 'a{1,2}.txt'", payload_cwd=nested)
        is not None
    )


@pytest.mark.parametrize(
    ("command", "widened"),
    [
        ("git checkout -- 'a{1,2}.txt'", True),
        ("git clean -fd -e keep.txt", True),
        ("git checkout -- sub", False),
        ("git clean -fd", False),
    ],
)
def test_an_over_wide_report_says_so(
    deny_reason: HookRunner, nested: Path, command: str, widened: bool
) -> None:
    """A file the command visibly does not name reads two ways — over-wide report,
    or a hook that measured the wrong thing.
    """
    (nested / "sub" / "s.txt").write_text("CHANGED\n")
    (nested / "sub" / "u.txt").write_text("untracked\n")
    reason = deny_reason(HOOK, command, payload_cwd=nested)
    assert reason is not None, command
    assert ("may lie outside" in reason) is widened, reason


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


def test_a_cwd_in_no_repository_has_nothing_to_lose(
    deny_reason: HookRunner, tmp_path: Path
) -> None:
    """`in_repository` asks git the same upward-walk question the real command will
    answer, from the same directory and environment.

    The pair is pinned together. A `cd` into a directory that does not exist yet is
    answered the same way, and it was their disagreement that was the defect.
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    assert deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=plain) is None
    # The pair is pinned together, their disagreement having been the defect.
    missing = tmp_path / "not-created-yet"
    assert deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=missing) is None


def test_a_conflicted_file_is_counted_once(deny_reason: HookRunner, repo: Path) -> None:
    """`diff --name-only` names an unmerged path once per stage it compares, so one
    conflicted file reads `2 file(s)` and reaches `AT_STAKE_LIMIT` at half the real
    count.
    """
    (repo / "f.txt").write_text("base\n")
    commit_all(repo, "base")
    git(repo, "checkout", "-q", "-b", "theirs")
    (repo / "f.txt").write_text("theirs\n")
    commit_all(repo, "theirs")
    git(repo, "checkout", "-q", "-")
    (repo / "f.txt").write_text("ours\n")
    commit_all(repo, "ours")
    subprocess.run(["git", "merge", "theirs"], cwd=repo, capture_output=True)
    assert "UU f.txt" in git(repo, "status", "--short")
    reason = deny_reason(HOOK, "git checkout -- .", payload_cwd=repo)
    assert reason is not None
    assert "At stake (1 file(s))" in reason, reason
    at_stake = reason.split("At stake", 1)[1].split("\n\n", 1)[0]
    assert at_stake.count("f.txt") == 1, at_stake


@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("double quote", 'a"b.txt'),
        ("backslash", "a\\b.txt"),
        ("tab", "a\tb.txt"),
        ("newline", "a\nb.txt"),
    ],
)
def test_a_token_follows_content_under_a_name_git_quotes(
    deny_reason: HookRunner, repo: Path, label: str, name: str
) -> None:
    """`core.quotepath=false` covers non-ASCII bytes and these four characters
    anyway; `-z` is what removes the quoting. C-quoted, the name stats nothing and
    the fingerprint stops depending on content.
    """
    victim = repo / name
    victim.write_text("PRECIOUS\n")
    token = issue_token(deny_reason, repo, "git clean -fd")
    acked = f"git clean -fd # ack:{token}"
    assert deny_reason(HOOK, acked, payload_cwd=repo) is None, label
    victim.write_text("REWRITTEN AFTER THE TOKEN WAS ISSUED\n")
    assert deny_reason(HOOK, acked, payload_cwd=repo) is not None, label


def test_a_subshell_around_the_call_keeps_the_measurement(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A separator is a run of punctuation, so a `(` arrives glued to the `&&`, and
    read by equality the line is conditional-but-not-chained, so the refusal
    survives blind.
    """
    (repo / "sub").mkdir()
    (repo / "sub" / "c.txt").write_text("v1\n")
    commit_all(repo, "with sub")
    (repo / "sub" / "c.txt").write_text("PRECIOUS\n")
    for command in (
        "true && cd sub && git checkout -- c.txt",
        "true && cd sub && (git checkout -- c.txt)",
    ):
        reason = deny_reason(HOOK, command, payload_cwd=repo)
        assert reason is not None, command
        assert "At stake" in reason, command
        assert "sub/c.txt" in reason, (command, reason)


@pytest.mark.parametrize(
    "command",
    [
        "git --git-dir=/x --work-tree=/y checkout -- a.txt",
        # git accepts the value as a separate token too. Consuming only the flag
        # leaves the value in the subcommand position, where it hides the verb
        # and the whole invocation slips past recognition.
        "git --git-dir /x --work-tree /y checkout -- a.txt",
        "git --work-tree /y checkout -- a.txt",
        "git --namespace ns checkout -- a.txt",
    ],
)
def test_relocated_worktree_denies(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """`--git-dir` / `--work-tree` move the tree itself and `--namespace` shifts ref
    resolution, none of which can be followed cheaply.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


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


@pytest.mark.parametrize(
    "command",
    [
        "cd && git clean -n",
        "cd && git checkout -p -- a.txt",
        "cd nowhere || cd elsewhere; git reset --soft HEAD",
        "git --work-tree=/y restore --staged a.txt",
        "GIT_DIR=/x git reset --soft HEAD",
    ],
)
def test_harmless_forms_are_decided_before_where_they_run(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """Each carries a `where` this hook refuses to guess at, and a verb form that
    destroys nothing wherever it lands.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


def test_bare_cd_denies(deny_reason: HookRunner, repo: Path) -> None:
    """A `cd` with no operand goes home, which the payload cannot resolve to."""
    dirty(repo)
    assert deny_reason(HOOK, "cd && git reset --hard", payload_cwd=repo) is not None


def test_dash_c_scopes_the_measurement(deny_reason: HookRunner, nested: Path) -> None:
    """`git -C <dir>` relocates the command without a `cd`, so the measurement has
    to follow it the same way."""
    (nested / "sub" / "s.txt").write_text("CHANGED\n")
    assert deny_reason(HOOK, "git -C sub checkout -- .", payload_cwd=nested) is not None
    git(nested, "checkout", "--", "sub/s.txt")
    (nested / "a.txt").write_text("CHANGED\n")
    # The change is outside sub/, so a checkout scoped there discards nothing.
    assert deny_reason(HOOK, "git -C sub checkout -- .", payload_cwd=nested) is None


def test_unmeasurable_cases_still_offer_a_way_through(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Re-running reproduces the same refusal, so the token binds the command — all
    that is knowable once the measurement failed.
    """
    dirty(repo)
    blocked = "git --work-tree=/y checkout -- a.txt"
    token = issue_token(deny_reason, repo, blocked)
    assert deny_reason(HOOK, f"{blocked} # ack:{token}", payload_cwd=repo) is None


def test_the_unmeasurable_token_does_not_unlock_a_measured_deny(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The two tokens bind different things, so one must not stand in for the
    other."""
    dirty(repo)
    blind = issue_token(deny_reason, repo, "git --work-tree=/y checkout -- a.txt")
    assert (
        deny_reason(HOOK, f"git checkout -- a.txt # ack:{blind}", payload_cwd=repo)
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


# --- what the local configuration must not be able to move ------------------


def test_diff_relative_does_not_hide_changes_above_the_cwd(
    deny_reason: HookRunner, nested: Path
) -> None:
    """`diff.relative=true` makes `git diff` answer about the current directory
    only, so from a subdirectory the measurement misses every change above it.
    """
    git(nested, "config", "diff.relative", "true")
    (nested / "a.txt").write_text("CHANGED\n")
    assert deny_reason(HOOK, "git reset --hard", payload_cwd=nested / "sub") is not None


def test_a_non_ascii_untracked_path_still_fingerprints_its_content(
    deny_reason: HookRunner, repo: Path
) -> None:
    """git C-quotes a path holding a non-ASCII byte, and `"\\303\\251.txt"` names no
    file on disk, so every stat misses and the fingerprint stops depending on
    content.
    """
    assert_untracked_ack_expires(deny_reason, repo, repo / "é.txt")


def test_a_non_ascii_path_is_named_as_the_reader_can_pass_it_back(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The listed paths are what the suggested commands take as operands, so the
    quoted spelling would hand the reader a name git does not accept back."""
    (repo / "é.txt").write_text("v\n")
    reason = deny_reason(HOOK, "git clean -fd", payload_cwd=repo)
    assert reason is not None
    assert "é.txt" in reason, reason


def test_untracked_ack_expires_when_measured_from_a_subdirectory(
    deny_reason: HookRunner, nested: Path
) -> None:
    """`ls-files --full-name` reports root-relative names, so joining them onto
    the cwd resolves to `sub/sub/new.txt` from a subdirectory and misses."""
    sub = nested / "sub"
    assert_untracked_ack_expires(deny_reason, sub, sub / "new.txt")


# --- directory changes the payload does not carry ---------------------------


def test_pushd_is_followed_like_cd(
    deny_reason: HookRunner, repo: Path, tmp_path: Path
) -> None:
    """`pushd` moves the shell exactly as `cd` does."""
    other = repo_holding_work(tmp_path / "other")
    assert (
        deny_reason(HOOK, f"pushd {other} && git reset --hard", payload_cwd=repo)
        is not None
    )


@pytest.mark.parametrize(
    "spelling", ['eval "cd {other}"', "builtin cd {other}", "command cd {other}"]
)
def test_a_cd_written_under_another_word_is_still_a_cd(
    deny_reason: HookRunner, repo: Path, tmp_path: Path, spelling: str
) -> None:
    """`eval`, `builtin` and `command` run in the current shell, so the directory
    they change stays changed. The `repo` fixture is clean, so a pass cannot be
    explained by having measured the right tree.
    """
    other = repo_holding_work(tmp_path / "other")
    line = f"{spelling.format(other=other)} && git reset --hard"
    assert deny_reason(HOOK, line, payload_cwd=repo) is not None, line


def test_a_relocating_global_denies_even_from_outside_a_repository(
    deny_reason: HookRunner, tmp_path: Path
) -> None:
    """"Nothing here to lose" is about here, and these globals move where that is.
    `stake_for` refuses an unrecognized global, but only when this check declines to
    answer first.
    """
    other = repo_holding_work(tmp_path / "other")
    plain = tmp_path / "plain"
    plain.mkdir()
    line = f"git --git-dir={other}/.git --work-tree={other} reset --hard"
    assert deny_reason(HOOK, line, payload_cwd=plain) is not None, line


@pytest.mark.parametrize(
    "spelling",
    ['builtin eval "cd {other}"', 'command eval "cd {other}"'],
)
def test_a_wrapper_around_an_unreadable_cd_is_peeled_first(
    deny_reason: HookRunner, repo: Path, tmp_path: Path, spelling: str
) -> None:
    """Tested before peeling, `builtin eval "cd <repo>"` reads as `builtin`, which
    is neither a directory change nor an unreadable one.
    """
    other = repo_holding_work(tmp_path / "other")
    line = f"{spelling.format(other=other)} && git reset --hard"
    assert deny_reason(HOOK, line, payload_cwd=repo) is not None, line


def test_a_reporting_wrapper_runs_nothing_and_is_not_refused(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`command -v <name>` runs nothing, so it moves no directory. POSIX gives
    `command` a closed option set, `-p` being the only one that still runs it.
    """
    assert (
        deny_reason(HOOK, "command -v rg && git checkout -- a.txt", payload_cwd=repo)
        is None
    )
    dirty(repo)
    reason = deny_reason(
        HOOK, "command -v rg && git checkout -- a.txt", payload_cwd=repo
    )
    assert reason is not None
    # Measured, not blind: the wrapper is stepped over.
    assert "At stake" in reason, reason


def test_an_unreadable_command_inside_a_subshell_does_not_blind_the_measurement(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A pipeline stage and a subshell each get their own shell, so an `eval` in one
    cannot move the shell the git call runs in.
    """
    dirty(repo)
    for command in (
        'eval "cd /tmp" | cat ; git checkout -- a.txt',
        '(eval "cd /tmp") ; git checkout -- a.txt',
    ):
        reason = deny_reason(HOOK, command, payload_cwd=repo)
        assert reason is not None, command
        assert "At stake" in reason, (command, reason)
        assert "a.txt" in reason, (command, reason)


def test_sourcing_a_file_is_a_declared_exclusion_not_a_refusal(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`.` and `source` read a file this hook does not, and refusing every one taxes
    `. .venv/bin/activate && ...`. A `cd` inside such a file is not followed.
    """
    (repo / "activate").write_text("export X=1\n")
    assert (
        deny_reason(HOOK, ". activate && git checkout -- a.txt", payload_cwd=repo)
        is None
    )
    dirty(repo)
    reason = deny_reason(HOOK, ". activate && git checkout -- a.txt", payload_cwd=repo)
    assert reason is not None
    assert "At stake" in reason, reason


def test_a_peeled_cd_is_followed_rather_than_only_refused(
    deny_reason: HookRunner, repo: Path, tmp_path: Path
) -> None:
    """Refusing on the sight of a wrapper would satisfy the assertion above while
    measuring nothing, so this one requires the at-stake list. `eval` stays blind,
    its argument being text this hook does not read.
    """
    other = repo_holding_work(tmp_path / "other")
    for spelling in (f"cd {other}", f"builtin cd {other}", f"command cd {other}"):
        reason = deny_reason(HOOK, f"{spelling} && git reset --hard", payload_cwd=repo)
        assert reason is not None, spelling
        assert "At stake" in reason, spelling
        assert "a.txt" in reason, (spelling, reason)
    blind = deny_reason(
        HOOK, f'eval "cd {other}" && git reset --hard', payload_cwd=repo
    )
    assert blind is not None
    assert "At stake" not in blind, blind


def test_a_redirection_shape_inside_a_quoted_pathspec_survives(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`FD_PREFIX` rewrites the raw command text, so it cannot see quotes, and ` 2>`
    is part of a filename inside them. Both branches of `measure` are asked.
    """
    tracked_dirty(repo, "a 2>3.txt")
    assert (
        deny_reason(HOOK, 'git checkout -- "a 2>3.txt"', payload_cwd=repo) is not None
    )
    (repo / "j 2>1.txt").write_text("PRECIOUS\n")
    assert (
        deny_reason(HOOK, 'git clean -f -- "j 2>1.txt"', payload_cwd=repo) is not None
    )


@pytest.mark.parametrize("name", ["a b.txt", "a>b.txt", "x2>3.txt"])
def test_names_beside_the_redirection_shape_keep_denying(
    deny_reason: HookRunner, repo: Path, name: str
) -> None:
    """The boundary of that fix: it takes a space, then digits, then `>`."""
    tracked_dirty(repo, name)
    assert deny_reason(HOOK, f'git checkout -- "{name}"', payload_cwd=repo) is not None


def test_a_hash_after_an_escaped_space_is_not_a_comment(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The shell starts a comment only at a word's beginning, and `a\\ #b.txt` is
    one word — `printf` hands git `a #b.txt`.
    """
    tracked_dirty(repo, "a #b.txt")
    assert deny_reason(HOOK, "git checkout -- a\\ #b.txt", payload_cwd=repo) is not None
    (repo / "j #1.txt").write_text("PRECIOUS\n")
    assert deny_reason(HOOK, "git clean -f -- j\\ #1.txt", payload_cwd=repo) is not None


@pytest.mark.parametrize(
    ("line", "moves"),
    [
        ("(cd {other} && git status); ", False),
        ("(cd {other}) && ", False),
        ("cd {other} | cat; ", False),
        ("cd {other} && ", True),
        ("(cd {other} && ", True),  # the git call is INSIDE, so the cd reaches it
    ],
)
def test_a_cd_confined_to_a_subshell_does_not_move_the_measurement(
    deny_reason: HookRunner, repo: Path, tmp_path: Path, line: str, moves: bool
) -> None:
    """A subshell's `cd` is undone when that subshell exits, and replaying it
    measured a directory the git command never ran in.
    """
    dirty(repo)  # the payload cwd has work at stake
    # ... and the subshell's target does not
    other = clean_repo(tmp_path / "other")
    command = line.format(other=other) + "git reset --hard"
    denied = deny_reason(HOOK, command, payload_cwd=repo) is not None
    assert denied is not moves, command


@pytest.mark.parametrize(
    ("line", "denied"),
    [
        # The `cd` may not run, and a later separator reaches the git call
        # anyway — so it lands either there or here, and here holds work.
        ("test -d nowhere && cd {other}; ", True),
        ("false && cd {other}; ", True),
        # Same condition guarding both: the only branch where the git call runs
        # is the one where the `cd` did.
        ("mkdir -p {other} && cd {other} && ", False),
    ],
)
def test_a_conditional_cd_is_followed_only_when_it_guards_the_git_call(
    deny_reason: HookRunner, repo: Path, tmp_path: Path, line: str, denied: bool
) -> None:
    """`&&` makes a `cd` conditional exactly as `||` does, and `test -d dist && cd
    dist; git reset --hard` runs the reset in whichever tree the shell is left in.
    """
    dirty(repo)
    # clean, so a pass would have to come from here
    other = clean_repo(tmp_path / "other")
    command = line.format(other=other) + "git reset --hard"
    assert (deny_reason(HOOK, command, payload_cwd=repo) is not None) is denied, command


def test_a_measurement_of_the_enclosing_repository_announces_itself(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The ancestor fallback measures a tree the command may not reach at all. The
    caveat has to be the one about where this was measured, the command carrying no
    pathspec.
    """
    dirty(repo)
    command = "git clone -q https://example.invalid/x.git r && cd r && git reset --hard"
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None
    assert "nearest one that does was measured" in reason, reason
    assert "narrowing this command carries" not in reason, reason


def test_an_ack_converges_wherever_it_is_written(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The token is a hash of what is left once the ack is removed, so an ack
    written anywhere but the end shifted the base and minted a fresh token every
    retry.
    """
    dirty(repo)
    command = "git checkout -- a.txt\ngit status"
    token = issue_token(deny_reason, repo, command)
    # Appended to the offending line rather than to the end of the whole thing.
    acked = f"git checkout -- a.txt # ack:{token}\ngit status"
    assert deny_reason(HOOK, acked, payload_cwd=repo) is None, acked


def test_popd_denies(deny_reason: HookRunner, repo: Path) -> None:
    """Where `popd` lands is held in the shell's own directory stack, which the
    payload does not carry."""
    dirty(repo)
    assert deny_reason(HOOK, "popd && git reset --hard", payload_cwd=repo) is not None


# --- git's own options ------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git --attr-source HEAD reset --hard",
        "git --config-env core.pager=PAGER_ENV reset --hard",
        "git -c core.pager=cat reset --hard",
    ],
)
def test_a_global_options_value_does_not_hide_the_verb(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """git takes each of these values as a separate token, which consuming only the
    flag leaves standing in the subcommand position.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_an_unknown_global_option_does_not_hide_the_verb(
    deny_reason: HookRunner, repo: Path
) -> None:
    """An option git does not know makes git exit 129 before the subcommand runs, so
    the value-taking list stays closed.
    """
    dirty(repo)
    assert (
        deny_reason(HOOK, "git --bogus-option reset --hard", payload_cwd=repo)
        is not None
    )


# --- abbreviated long options -----------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git reset --har",
        "git clean --forc -d",
        "git checkout --forc other",
        "git switch --disc other",
        "git restore --worktre a.txt",
    ],
)
def test_an_abbreviated_long_option_still_reads_as_the_option_it_names(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """git's parse-options accepts any unambiguous abbreviation, so `git reset
    --har` really does reset.
    """
    dirty(repo)
    (repo / "u.txt").write_text("untracked\n")  # so `clean` has something to take
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        "git clean --d -f",
        "git restore --stag a.txt",
        "git checkout --patc -- a.txt",
    ],
)
def test_an_abbreviated_harmless_option_is_refused(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """Abbreviations expand against a union over all five verbs, so one can name a
    flag the verb lacks. Toward a destructive flag that adds a refusal, toward a
    harmless one it cancels the measurement.

    `git switch --p -f other` was read as `--patch` while git resolved `--p` to
    `--progress` and discarded the tree.
    """
    dirty(repo)
    (repo / "u.txt").write_text("untracked\n")
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        "git clean --dry-run -f",
        "git restore --staged a.txt",
        "git checkout --patch -- a.txt",
    ],
)
def test_a_fully_spelled_harmless_option_still_passes(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """The other half: spelled out, they are read and they pass."""
    dirty(repo)
    (repo / "u.txt").write_text("untracked\n")
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


@pytest.mark.parametrize(
    "command", ["git switch --p -f other", "git switch --p --discard-changes other"]
)
def test_an_abbreviation_naming_a_flag_the_verb_lacks_is_measured(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """`switch` has no `--patch`; git resolves `--p` to `--progress` and, with
    `-f`, discards. Both shapes were run for real and reverted the tree."""
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


# --- what a forced checkout actually reaches --------------------------------


def test_a_forced_checkout_still_narrows_to_its_pathspec(
    deny_reason: HookRunner, nested: Path
) -> None:
    """`-f` waives git's refusal to act on a dirty tree; it does not widen what the
    command opens.
    """
    (nested / "b.txt").write_text("CHANGED\n")
    assert deny_reason(HOOK, "git checkout -f -- a.txt", payload_cwd=nested) is None
    assert deny_reason(HOOK, "git checkout -f -- b.txt", payload_cwd=nested) is not None


def test_a_forced_checkout_without_a_pathspec_takes_the_whole_tree(
    deny_reason: HookRunner, repo: Path
) -> None:
    dirty(repo)
    assert deny_reason(HOOK, "git checkout -f other", payload_cwd=repo) is not None


def test_a_forced_branch_creation_is_measured_whole(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`-b` takes the new branch's name as its operand, which read as a pathspec
    narrows the measurement to a file that does not exist.
    """
    dirty(repo)
    assert deny_reason(HOOK, "git checkout -f -b fresh", payload_cwd=repo) is not None


# --- a directory that does not exist yet ------------------------------------


@pytest.mark.parametrize(
    "creator",
    [
        "git clone -q https://example.invalid/x.git repo",
        "ghq get example.invalid/x",
        "gh repo clone example/x repo",
        "git worktree add repo other",
        "mkdir repo",
        "tar xf repo.tar",
    ],
)
def test_a_directory_this_line_creates_outside_a_repository_is_left_alone(
    deny_reason: HookRunner, tmp_path: Path, creator: str
) -> None:
    """A path holding nothing when the hook decides can only come to hold what the
    rest of the line puts there — provided nothing above it is a repository either.

    Parametrized over unrelated producers to pin that the reading does not come from
    recognizing them: that set has no boundary, and a rule built on it would refuse
    whichever spelling had not been listed yet.
    """
    workspace = tmp_path / "workspace"  # an ordinary directory, no repository
    workspace.mkdir()
    command = f"{creator} && cd repo && git reset --hard"
    assert deny_reason(HOOK, command, payload_cwd=workspace) is None, command


@pytest.mark.parametrize(
    "creator", ["mkdir -p d", "git clone -q URL d", "tar xf d.tar"]
)
def test_a_directory_this_line_creates_inside_a_repository_is_measured(
    deny_reason: HookRunner, repo: Path, creator: str
) -> None:
    """"This path holds nothing" is about the path alone, and git resolves upwards.

    A command run in a directory this line creates still reaches the repository
    above it, so reading the new path alone lets `mkdir -p d && cd d && git reset
    --hard` take the enclosing tree. The measurement falls back to the nearest
    directory that already exists, and the cost is a refusal when a clone lands
    inside a dirty repository.
    """
    dirty(repo)
    command = f"{creator} && cd d && git reset --hard"
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None, command
    assert "a.txt" in reason, reason


def test_the_two_spellings_of_a_branch_switch_agree(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`switch` clears without the repository and `checkout` cannot, so nothing but
    this test keeps one intent from being decided two ways by spelling.
    """
    dirty(repo)
    for verb in ("checkout", "switch"):
        command = (
            f"git clone -q https://example.invalid/x.git r && cd r && git {verb} other"
        )
        assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


def test_a_cd_that_will_fail_is_measured_where_the_command_lands(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`;` stops nothing, so a failed `cd` leaves the git command running in the
    directory the shell was already in — which is right here, and measurable.
    """
    dirty(repo)
    reason = deny_reason(HOOK, "cd typo; git reset --hard", payload_cwd=repo)
    assert reason is not None
    assert "a.txt" in reason, reason


def test_a_cd_that_will_fail_passes_when_that_directory_holds_nothing(
    deny_reason: HookRunner, repo: Path
) -> None:
    assert deny_reason(HOOK, "cd typo; git reset --hard", payload_cwd=repo) is None


@pytest.mark.parametrize(
    ("situation", "cd_form", "dash_c_form"),
    [
        pytest.param(
            "never created",
            "cd no-such-dir && git reset --hard",
            "git -C no-such-dir reset --hard",
            id="never-created",
        ),
        pytest.param(
            "created by this same line",
            "mkdir -p d && cd d && git reset --hard",
            "mkdir -p d && git -C d reset --hard",
            id="created-on-this-line",
        ),
        pytest.param(
            "already there",
            "mkdir -p d; cd d && git reset --hard",
            "mkdir -p d; git -C d reset --hard",
            id="already-there",
        ),
    ],
)
def test_the_two_ways_of_running_it_over_there_answer_alike(
    deny_reason: HookRunner, repo: Path, situation: str, cd_form: str, dash_c_form: str
) -> None:
    """`cd <dir> && git <verb>` and `git -C <dir> <verb>` are one intent, and a hook
    deciding them differently is fail-open behind whichever spelling it does not
    look at.

    The middle row is the one measured going wrong. With the directory created
    earlier on the same line, git resolves upwards into the enclosing repository and
    takes the worktree, which the `cd` spelling refused and the `-C` spelling did
    not. The first row is the price — a directory that will never exist cannot be
    told apart from one this line is about to create.
    """
    dirty(repo)
    cd_reason = deny_reason(HOOK, cd_form, payload_cwd=repo)
    dash_c_reason = deny_reason(HOOK, dash_c_form, payload_cwd=repo)
    assert (cd_reason is None) == (dash_c_reason is None), (
        f"{situation}: cd={cd_reason!r} -C={dash_c_reason!r}"
    )
    assert cd_reason is not None, situation
    # Not just that both refuse, but that they refuse about the same thing: the
    # widening caveat is what differs between a directory measured exactly and one
    # measured through its nearest existing ancestor, so two spellings that
    # disagree on it are answering about different trees.
    caveat = "nearest one that does was measured"
    assert (caveat in cd_reason) == (caveat in dash_c_reason), (
        f"{situation}: the two spellings disagree on how wide the answer is"
    )
    assert ("a.txt" in cd_reason) == ("a.txt" in dash_c_reason), (
        f"{situation}: the two spellings name different content at stake"
    )


@pytest.mark.parametrize(
    ("cd_form", "dash_c_form"),
    [
        pytest.param(
            "mkdir -p r && cd r && git reset --hard",
            "mkdir -p r && git -C r reset --hard",
            id="created-on-this-line",
        )
    ],
)
def test_running_it_over_there_from_outside_any_repository_is_left_alone(
    deny_reason: HookRunner, tmp_path: Path, cd_form: str, dash_c_form: str
) -> None:
    """The other end of the fallback, and the reason it is not simply "refuse".

    Outside any repository there is nothing above for git to resolve up into, so
    whatever this line puts at the target holds no content that existed when the
    hook decided. Without this row the fallback's None branch is reached by no test
    at all.
    """
    outside = tmp_path / "plain"
    outside.mkdir()
    assert deny_reason(HOOK, cd_form, payload_cwd=outside) is None, cd_form
    assert deny_reason(HOOK, dash_c_form, payload_cwd=outside) is None, dash_c_form


def test_a_later_move_on_the_same_line_is_still_followed(
    deny_reason: HookRunner, repo: Path, tmp_path: Path
) -> None:
    """The walk answers about where it ends, not where it first met a directory that
    does not exist yet. The payload repository here is clean and the work is in
    another one, so an answer given at `cd d` describes a tree with nothing at
    stake.
    """
    other = init(tmp_path / "other")
    (other / "f.txt").write_text("v1\n")
    commit_all(other)
    dirty(other, "f.txt")
    command = f"mkdir -p d && cd d && cd {other} && git reset --hard"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_content_moved_in_by_the_same_line_is_not_protected(
    deny_reason: HookRunner, repo: Path, tmp_path: Path
) -> None:
    """Pinned as a decision: the work does exist when the hook decides, but at a
    path nothing here can connect to the one the command names. Closing it needs the
    same unbounded enumeration the test above refuses to build.
    """
    dirty(repo)
    command = f"mv {repo} moved && cd moved && git reset --hard"
    assert deny_reason(HOOK, command, payload_cwd=tmp_path) is None


@pytest.mark.parametrize(
    "target",
    ["$repo", "${repo}", "$(ghq root)/x", "`pwd`/x", "{a,b}", "r*", "r?", "r[12]"],
)
def test_a_dash_c_target_the_shell_still_resolves_is_refused(
    deny_reason: HookRunner, repo: Path, target: str
) -> None:
    """`git -C` names a directory exactly as `cd` does, so it goes through the same
    gate. Held apart, the two drifted: `cd $REPO && git reset --hard` was refused
    while `git -C $REPO reset --hard` went through.
    """
    dirty(repo)
    command = f"git -C {target} reset --hard"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "target",
    ["$repo", "${repo}", "$(ghq root)/x", "`pwd`/x", "{a,b}", "r*", "r?", "r[12]"],
)
def test_a_cd_target_the_shell_still_expands_is_refused(
    deny_reason: HookRunner, repo: Path, target: str
) -> None:
    """"Does not exist" is read as "holds nothing", and that reading is only about
    the path as written: an unexpanded target never matches a directory, so the
    reading would be applied to a spelling the shell is about to turn into some
    other path.

    A glob is on this list though it is deliberately off the pathspec one: git's own
    glob matches at least as widely as the shell's, so forwarding a pathspec
    over-detects, while a directory glob resolves to one path the hook cannot pick.
    """
    dirty(repo)
    command = f"cd {target} && git reset --hard"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    ["cd ~/myrepo && git reset --hard", "git -C ~/myrepo reset --hard"],
)
def test_a_tilde_resolves_the_same_way_in_both_spellings(
    deny_reason: HookRunner, tmp_path: Path, command: str
) -> None:
    """`~` is the one expansion this hook shares with the shell, so both ways of
    naming a directory resolve it and then measure what is actually there."""
    home = tmp_path / "home"
    repo_holding_work(home / "myrepo")
    # Clean, so a deny cannot be coming from the directory the payload names.
    elsewhere = clean_repo(tmp_path / "elsewhere")
    reason = deny_reason(HOOK, command, {"HOME": str(home)}, payload_cwd=elsewhere)
    assert reason is not None, command
    assert "a.txt" in reason, reason


def test_a_tilde_cd_target_is_expanded_rather_than_refused(
    deny_reason: HookRunner, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~` is the one expansion this hook shares with the shell, so it resolves and
    then measures what is there.
    """
    monkeypatch.setenv("HOME", str(repo.parent))
    dirty(repo)
    command = f"cd ~/{repo.name} && git reset --hard"
    reason = deny_reason(
        HOOK, command, {"HOME": str(repo.parent)}, payload_cwd=repo.parent
    )
    assert reason is not None, command
    assert "a.txt" in reason, reason


@pytest.mark.parametrize("redirect", ["<<'EOF'", "<<EOF", "<<-EOF"])
def test_a_here_document_body_is_refused_rather_than_parsed(
    deny_reason: HookRunner, repo: Path, redirect: str
) -> None:
    """An accepted false positive: setting the body apart takes a delimiter rule,
    and every attempt at one here misread something — a here-string takes a word, a
    `<<-` marker travels attached, an unterminated body swallows the line.
    """
    dirty(repo)
    command = f"cat > setup.sh {redirect}\ngit reset --hard\nEOF"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_a_real_command_after_a_here_document_is_still_seen(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Skipping the body must end at the delimiter, or the rest of the line goes
    unexamined with it."""
    dirty(repo)
    command = "cat > setup.sh <<'EOF'\necho hi\nEOF\ngit reset --hard"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        # A here-string takes a word, not a delimiter. Hunting for that word as
        # one swallows the rest of the line, git verb included.
        'grep foo <<< "$v" && git reset --hard',
        # A `<<` whose delimiter never recurs is not a here-document either.
        "cat > setup.sh <<-EOF\ngit reset --hard",
        "echo x << MISSING\ngit reset --hard",
    ],
)
def test_an_unterminated_body_does_not_swallow_the_rest_of_the_line(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """Skipping to end-of-line on a misread `<<` is how the guard switches itself
    off.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        "git 2>&1 reset --hard",
        "git reset --hard 2>/dev/null",
        "git reset --hard > out.log",
        "git checkout 2>/dev/null -- a.txt",
        "git checkout -- a.txt 1>out 2>&1",
        "git restore 2>/dev/null a.txt",
        # A backslash-escaped newline is a line join to the shell; shlex leaves a
        # bare newline, which reads as a command boundary and drops the pathspec.
        "git checkout \\\n  -- a.txt",
        "git reset \\\n  --hard",
    ],
)
def test_a_redirection_is_not_a_command_boundary(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """Read as one, `git clean -fd 2>/dev/null` leaves `2` behind as clean's
    pathspec and `git 2>&1 reset --hard` loses the verb.
    """
    dirty(repo)
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None, command
    assert "a.txt" in reason, reason


@pytest.mark.parametrize("name", ["2", "2024"])
def test_a_digit_pathspec_is_not_read_as_a_file_descriptor(
    deny_reason: HookRunner, repo: Path, name: str
) -> None:
    """`2>log` and `2 > log` tokenize identically, so the digit's role survives only
    as adjacency in the raw text.
    """
    tracked_dirty(repo, name, "digit")
    reason = deny_reason(HOOK, f"git checkout -- {name} > log", payload_cwd=repo)
    assert reason is not None
    assert name in reason, reason


@pytest.mark.parametrize("joiner", [";", "&&"])
def test_a_brace_group_cd_is_followed(
    deny_reason: HookRunner, repo: Path, tmp_path: Path, joiner: str
) -> None:
    """A brace group runs in the current shell, unlike a subshell. `{` is a word to
    the tokenizer — so `{}` and `{a,b}` stay whole — which left it in command
    position with the `cd` behind it unread.
    """
    other = repo_holding_work(tmp_path / "other")
    command = f"{{ cd {other}; }}{joiner} git reset --hard"
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None, command
    assert "a.txt" in reason, reason


def test_a_brace_expansion_operand_is_left_whole(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Only a lone brace is a group keyword. `{}` is one token and an argument."""
    dirty(repo)
    assert (
        deny_reason(HOOK, "find . -name '*.py' -exec ls {} \\;", payload_cwd=repo)
        is None
    )


def test_a_redirection_target_is_not_read_as_a_pathspec(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The narrowing has to survive too: reading `2` or the target as a pathspec
    would collapse the measurement.
    """
    (repo / "junk").mkdir()
    (repo / "junk" / "j.txt").write_text("x\n")
    reason = deny_reason(HOOK, "git clean -fd 2>/dev/null", payload_cwd=repo)
    assert reason is not None
    assert "junk/j.txt" in reason, reason


def test_a_pathspec_holding_a_hash_is_not_truncated(
    deny_reason: HookRunner, repo: Path
) -> None:
    """shlex opens a comment at a `#` anywhere in a word, so `f#1.txt` measures as
    `f` and matches nothing.
    """
    tracked_dirty(repo, "f#1.txt", "hash")
    reason = deny_reason(HOOK, "git checkout -- f#1.txt", payload_cwd=repo)
    assert reason is not None
    assert "f#1.txt" in reason, reason


@pytest.mark.parametrize("spelling", ["'x #1.txt'", '"x #1.txt"', "x\\ \\#1.txt"])
def test_a_quoted_hash_in_a_pathspec_survives_comment_stripping(
    deny_reason: HookRunner, repo: Path, spelling: str
) -> None:
    """Cutting at a quoted `#` leaves an unbalanced quote, the tokenizer falls back
    to a whitespace split, and the pathspec becomes `'x`. The three spellings name
    one file.
    """
    tracked_dirty(repo, "x #1.txt", "hashy")
    reason = deny_reason(HOOK, f"git checkout -- {spelling}", payload_cwd=repo)
    assert reason is not None, spelling
    assert "x #1.txt" in reason, reason


def test_a_trailing_comment_is_still_dropped(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Turning shlex's rule off means owning the shell's: a `#` opening a word
    still starts a comment, so the words after it are not read as pathspecs."""
    (repo / "b.txt").write_text("untracked\n")
    dirty(repo)
    reason = deny_reason(
        HOOK, "git checkout -- a.txt # tidy up b.txt", payload_cwd=repo
    )
    assert reason is not None
    assert "b.txt" not in reason, reason


def test_clean_without_d_leaves_untracked_directories_alone(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`git clean -f` does not descend into an untracked directory, but `ls-files
    --others` does.
    """
    (repo / "newdir").mkdir()
    (repo / "newdir" / "j.txt").write_text("x\n")
    assert deny_reason(HOOK, "git clean -f", payload_cwd=repo) is None
    reason = deny_reason(HOOK, "git clean -fd", payload_cwd=repo)
    assert reason is not None
    assert "newdir/j.txt" in reason, reason


def test_clean_without_d_still_sees_a_file_beside_the_tracked_ones(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The collapse must not take the files `clean -f` does remove with it."""
    (repo / "loose.txt").write_text("x\n")
    reason = deny_reason(HOOK, "git clean -f", payload_cwd=repo)
    assert reason is not None
    assert "loose.txt" in reason, reason


@pytest.mark.parametrize(
    "command",
    [
        "echo $(git reset --hard)",
        'git commit -am "$(git reset --hard)"',
        "echo `git checkout -- a.txt`",
    ],
)
def test_a_verb_inside_a_substitution_is_not_masked_away(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """A substitution's contents are executed, so hiding them is backwards. The
    tokenizer needs the mask to keep the line whole; the mention test reads the name
    off the characters around it and does not.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        "git -C $(git rev-parse --show-toplevel) reset --hard",
        "git -C `git rev-parse --show-toplevel` clean -fdx",
        "cd $(git rev-parse --show-toplevel) && git reset --hard",
        "git checkout -- $(ls *.txt)",
    ],
)
def test_a_command_substitution_does_not_hide_the_verb(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """`(` and `)` are separator tokens, so a substitution splits the line into
    fragments with the covered verb in none of them.
    """
    dirty(repo)
    (repo / "u.txt").write_text("untracked\n")
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_one_unread_call_among_read_ones_still_refuses(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The backstop counts: asking only "was anything recognized?" lets a readable
    call vouch for an unreadable one beside it.
    """
    dirty(repo)
    command = "git checkout -- k.txt && sh -c 'git reset --hard'"
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None, command
    assert "could not read as a call" in reason, reason


def test_an_unread_call_can_be_overridden(deny_reason: HookRunner, repo: Path) -> None:
    """A refusal with no way through would strand a genuine intent, and this one
    fires on shapes the hook cannot read.
    """
    dirty(repo)
    command = "sh -c 'git reset --hard'"
    token = issue_token(deny_reason, repo, command)
    assert deny_reason(HOOK, f"{command} # ack:{token}", payload_cwd=repo) is None


@pytest.mark.parametrize(
    "command",
    [
        "git read-tree -u --reset HEAD",
        "git checkout-index -f -a",
        "git rev-parse --show-toplevel",
    ],
)
def test_a_hyphenated_neighbour_is_not_read_as_the_verb(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """A word boundary sits either side of a hyphen, so `checkout-index` and
    `--reset` both carry one; the backstop matches whole words.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


@pytest.mark.parametrize(
    ("command", "denied", "why"),
    [
        ('git clean -f -e"*.pyc"', True, "the `p` in the value read as -p"),
        ("git clean -fdxenode_modules", True, "the `n` in the value read as -n"),
        ("git clean -fdx -eout", True, "the `u` and `t` are value, not flags"),
    ],
)
def test_an_attached_short_option_value_is_not_read_as_flags(
    deny_reason: HookRunner, repo: Path, command: str, denied: bool, why: str
) -> None:
    """A bundle ends at a short option that takes a value, and the letters that turn
    up in one are the ones that decide everything.
    """
    (repo / "untracked.txt").write_text("x\n")
    assert (deny_reason(HOOK, command, payload_cwd=repo) is not None) is denied, why


def test_an_attached_branch_name_is_not_read_as_force(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The same rule in the other direction: `-bfix-thing` carries an `f` that
    read as force and refused a branch creation which keeps the tree whole."""
    dirty(repo)
    assert deny_reason(HOOK, "git checkout -bfix-thing", payload_cwd=repo) is None


def test_a_worktree_deletion_is_not_a_loss(deny_reason: HookRunner, repo: Path) -> None:
    """What a restore returns comes from the index, and whatever the file held
    before the delete was already gone by then.
    """
    (repo / "a.txt").unlink()
    assert deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=repo) is None
    assert deny_reason(HOOK, "git reset --hard", payload_cwd=repo) is None


def test_a_deletion_beside_a_real_change_still_names_the_change(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Dropping deletions must not drop the file next to them."""
    (repo / "a.txt").unlink()
    (repo / "k.txt").write_text("CHANGED\n")
    reason = deny_reason(HOOK, "git reset --hard", payload_cwd=repo)
    assert reason is not None
    assert "k.txt" in reason, reason
    assert "a.txt" not in reason, reason


@pytest.mark.parametrize(
    ("command", "listing"),
    [
        # `-uall`, because `git status` otherwise collapses an untracked
        # directory to one entry: over 60 files under `bigdir/` it prints
        # `?? bigdir/` and nothing else, which is not "lists them all".
        ("git clean -fd", "`git status --short -uall` lists them all"),
        # `--ignored` too, because a plain `git status` does not report an
        # ignored file at all — for `clean -fX` it would show none of them.
        ("git clean -fdx", "`git status --short --ignored -uall` lists them all"),
        # Tracked changes are never collapsed, so the worktree kind needs neither.
        ("git checkout -- .", "`git status --short` lists them all"),
    ],
)
def test_the_at_stake_list_is_capped(
    deny_reason: HookRunner, repo: Path, command: str, listing: str
) -> None:
    """This reason lands in the caller's transcript, where a `clean -fdx` over a
    `node_modules` arrives as megabytes. The count stays exact; the listing does
    not, and the command offered in its place has to show the kind of content at
    stake.
    """

    def reason_for(count: int) -> str:
        for i in range(count):
            (repo / f"u{i}.txt").write_text("x\n")
        if command.startswith("git checkout"):
            commit_all(repo, f"many{count}")
            for i in range(count):
                (repo / f"u{i}.txt").write_text("CHANGED\n")
        got = deny_reason(HOOK, command, payload_cwd=repo)
        assert got is not None
        assert f"At stake ({count} file(s))" in got, got
        assert f"more; {listing}" in got, got
        return got

    # Boundedness is the contract, so it is measured as one: past the cap, four
    # times the content must not make a longer message. A line-count assertion
    # would pass on a message that still grew, just more slowly -- and the diffstat
    # is per-file too, so capping only the path list does exactly that.
    small = len(reason_for(120).splitlines())
    large = len(reason_for(480).splitlines())
    assert small == large, (small, large)


def test_an_unrelated_conditional_does_not_refuse(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`||` puts the directory in doubt only when what it guards moves the shell."""
    dirty(repo)
    reason = deny_reason(
        HOOK, "test -d x || echo no; git checkout -- a.txt", payload_cwd=repo
    )
    assert reason is not None
    assert "a.txt" in reason, reason
    assert "could not determine" not in reason, reason


# --- environment assignments in front of the command ------------------------


@pytest.mark.parametrize(
    "name", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE"]
)
def test_a_git_env_assignment_is_refused_like_the_option_it_mirrors(
    deny_reason: HookRunner, repo: Path, tmp_path: Path, name: str
) -> None:
    """These move the tree exactly as `--git-dir` / `--work-tree` do, and ride in
    through the step that keeps `LC_ALL=C git ...` recognized. Measured from a clean
    payload directory, so the answer comes from the assignment.
    """
    elsewhere = repo_holding_work(tmp_path / "elsewhere")
    command = f"{name}={elsewhere} git reset --hard"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_an_ordinary_env_assignment_does_not_refuse_a_clean_tree(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The refusal is keyed to `GIT_`, so a locale or pager prefix still gets the
    measured answer.
    """
    assert deny_reason(HOOK, "LC_ALL=C git reset --hard", payload_cwd=repo) is None


def test_a_git_env_assignment_still_yields_to_a_harmless_form(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A dry run destroys nothing wherever it is pointed, so `where` never comes
    up for it."""
    dirty(repo)
    assert deny_reason(HOOK, "GIT_DIR=/x git clean -n", payload_cwd=repo) is None


# --- the refusal itself must not be able to fail ----------------------------
#
# Every test above asks what the hook decides. These ask whether it can say so. A
# hook that exits non-zero is reported by the harness as a non-blocking error and
# the command then runs, so a raise on the refusal path is a fail-open -- and the
# one shape of it the counting backstop cannot catch, the count having already
# said "refuse".

# Command text a payload can legally carry that the refusal has to survive. The
# lone surrogate is not hypothetical: JSON can spell it, no UTF-8 encoder will
# take it, and .encode() inside the override-token hash raised on it -- after the
# decision to deny, so the discard ran. These name a covered call, so the answer
# is settled: refuse.
HOSTILE_COVERED = [
    pytest.param("git reset --hard \ud800", id="lone-surrogate-operand"),
    pytest.param("git checkout -- \ud800", id="lone-surrogate-pathspec"),
    pytest.param("sh -c 'git reset --hard' \ud800", id="lone-surrogate-past-backstop"),
    pytest.param("git reset --hard \x00a.txt", id="nul-byte"),
    pytest.param("git reset --hard " + "x" * 40000, id="very-long-operand"),
    pytest.param("git reset --hard \x1b[2J\x07", id="control-characters"),
]

# These do not, and not by omission: with the surrogate glued to the verb the word
# is `\udcfeclean`, which is no more a covered call than `git frobnicate` is, and
# allowing them is correct. What is owed is that the hook reach that answer instead
# of raising on the way to it.
HOSTILE_UNCOVERED = [
    pytest.param("git \udcfeclean -fd", id="surrogate-glued-to-verb"),
    pytest.param("git \ud800 reset --hard", id="surrogate-in-subcommand-position"),
]


@pytest.mark.parametrize("command", HOSTILE_COVERED)
def test_a_refusal_survives_whatever_the_command_text_holds(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """`deny_reason` asserts the exit code is 0 before it looks at the output, so
    this pins both halves: the process ended cleanly and it said deny.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize("command", HOSTILE_COVERED + HOSTILE_UNCOVERED)
def test_hostile_text_over_a_clean_tree_still_decides(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """Which way each goes is not the claim; that a decision is reached at all is."""
    deny_reason(HOOK, command, payload_cwd=repo)


@pytest.mark.parametrize("command", HOSTILE_UNCOVERED)
def test_unencodable_text_around_no_covered_call_still_decides(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """The dirty-tree half of the above: the encoding hazard is in the command text,
    and the text is read before the verb is.
    """
    dirty(repo)
    deny_reason(HOOK, command, payload_cwd=repo)


def test_an_override_still_binds_to_its_own_unencodable_command(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Mapping every unencodable byte onto one replacement would close the crash and
    open an override minted for one command authorising another.
    """
    dirty(repo)
    first = issue_token(deny_reason, repo, "git reset --hard \ud800")
    second = issue_token(deny_reason, repo, "git reset --hard \ud801")
    assert first != second


def test_the_unmeasured_refusal_names_a_directory_the_reader_can_start_from(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The message sends the reader to run `git status` in the tree at stake, but a
    `popd` returns to a directory only the shell's stack knows — which is why the
    hook refused. So it names the one directory that is known.
    """
    reason = deny_reason(HOOK, "popd; git reset --hard", payload_cwd=repo)
    assert reason is not None
    assert str(repo) in reason, reason
    assert "popd" in reason, reason


def test_the_unmeasured_refusal_pops_once_per_push(
    deny_reason: HookRunner, repo: Path
) -> None:
    """This message offers three stash spellings at once, and a tree holding both a
    tracked change and an ignored file takes two pushes.
    """
    reason = deny_reason(HOOK, "popd; git reset --hard", payload_cwd=repo)
    assert reason is not None
    assert "once per push" in reason, reason


CL = "cl" + "ean"


# --- shapes measured against git, in both directions ------------------------
#
# Every command below was run for real in a throwaway repository, with the content
# compared before and after, so each row asserts what git actually did. The
# converse rows are here for the same reason: only running a shape says whether it
# destroys anything.


def test_a_forced_orphan_checkout_is_measured(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`--orphan` takes a new branch name, as `-b` and `-B` do: read as a pathspec
    it resolves as no ref and narrows the measurement to a file that does not exist.
    """
    dirty(repo)
    assert (
        deny_reason(HOOK, "git checkout -f --orphan fresh", payload_cwd=repo)
        is not None
    )
    assert (
        deny_reason(HOOK, "git checkout --force --orphan fresh", payload_cwd=repo)
        is not None
    )


def test_an_abbreviated_orphan_is_read_as_orphan(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Measured: `git checkout -f --orph fresh` exits 0 and takes the tree. The only
    thing that exercises `abbreviates`, `--orphan` being kept out of `LONG_OPTS`.
    """
    dirty(repo)
    assert (
        deny_reason(HOOK, "git checkout -f --orph fresh", payload_cwd=repo) is not None
    )


def test_an_unforced_orphan_checkout_keeps_the_tree_and_is_allowed(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Measured: `git checkout --orphan fresh` leaves every uncommitted change in
    place, so the fix cannot be "refuse --orphan".
    """
    dirty(repo)
    assert deny_reason(HOOK, "git checkout --orphan fresh", payload_cwd=repo) is None


@pytest.mark.parametrize(
    "first",
    [
        pytest.param(
            'git -c user.name="John Doe" gitreset --hard', id="quoted-c-value"
        ),
        pytest.param("git -c user.name='John Doe' gitreset --hard", id="single-quoted"),
        pytest.param("git -C 'a dir' gitreset --hard", id="quoted-C-operand"),
        pytest.param("\\git gitreset --hard", id="escaped-command-name"),
    ],
)
def test_a_quoted_global_does_not_hide_the_next_call_from_the_backstop(
    deny_reason: HookRunner, repo: Path, first: str
) -> None:
    """A quoted option value splits into two words, `-c` consumes only the first,
    and the verb lands one position past where the subcommand test looks — scoring
    exactly what the real parser measures. The look-ahead is what this pins.

    The tracked tree is clean on purpose, or the first call refuses on its own.
    """
    (repo / "a dir").mkdir()
    untracked(repo)
    command = first.replace("gitreset", "git reset") + f"; sh -c 'git {CL} -fdx'"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "first",
    [
        pytest.param("git commit -q --allow-empty -m $'don\\'t'", id="ansi-c-quote"),
        pytest.param("git log -1 \\\\", id="escaped-backslash"),
        pytest.param("git log -1 \\\\\\\\", id="two-escaped-backslashes"),
    ],
)
def test_a_line_boundary_survives_an_escape_the_shell_reads_differently(
    deny_reason: HookRunner, repo: Path, first: str
) -> None:
    """Two ways to lose the newline after an inert call, and the guard with it:
    `$'...'` read as an ordinary quote spans the newline, and an escaped `\\\\` read
    as a continuation joins the lines.

    The bare-newline spellings of these same lines are refused, which places the bug
    at the line boundary.
    """
    dirty(repo)
    untracked(repo)
    command = f"{first}\nsh -c 'git {CL} -fdx'"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize("run", [1, 3, 5])
def test_an_odd_backslash_run_really_does_join_and_costs_no_refusal(
    deny_reason: HookRunner, repo: Path, run: int
) -> None:
    """An odd run escapes the newline, so the shell joins the lines and the second
    becomes arguments to the first command — measured at runs of 1, 3 and 5, the
    `clean` never runs.

    A regex that joins on any backslash refuses these; one that never joins lets the
    even runs destroy.
    """
    dirty(repo)
    untracked(repo)
    command = "git log -1 " + "\\" * run + f"\nsh -c 'git {CL} -fdx'"
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


def test_a_continuation_keeps_the_backslashes_that_are_not_the_continuation() -> None:
    """An odd run of three is one escaped backslash plus the one that joins, so the
    joined line still holds a literal `\\`. No decision depends on it; this is the
    only place it is observable.
    """
    from block_git_discard.hook import prepared

    assert prepared("echo a\\\\\\\nb", mask=False) == "echo a\\\\ b"
    assert prepared("echo a\\\nb", mask=False) == "echo a b"
    assert prepared("echo a\\\\\nb", mask=False) == "echo a\\\\\nb"


def test_a_git_global_this_hook_does_not_know_is_unmeasurable(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`--icase-pathspecs` makes `readme.MD` reach `README.md` at run time while
    this hook's own query matches nothing. Measured: the file is discarded and the
    command exits 0.

    The set is spelled the other way round because git's global surface grows.
    """
    tracked_dirty(repo, "README.md", "readme")
    command = "git --icase-pathspecs checkout -- readme.MD"
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None, command
    # The reason names the flag actually present, so the reader knows which token
    # to look at.
    assert "--icase-pathspecs" in reason, reason


@pytest.mark.parametrize("flag", ["--no-pager", "--paginate", "-p", "-P"])
def test_a_git_global_known_to_change_nothing_still_measures(
    deny_reason: HookRunner, repo: Path, flag: str
) -> None:
    """The bound on that widening: these four change what git prints and nothing it
    acts on.
    """
    dirty(repo)
    reason = deny_reason(HOOK, f"git {flag} checkout -- a.txt", payload_cwd=repo)
    assert reason is not None
    assert "a.txt" in reason, reason


def test_an_escaped_apostrophe_does_not_merge_the_next_line(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Read as an opening quote it never closes, so the next line merges into the
    `commit` argument, carrying the inert guard with it. The double-quoted spelling
    carries no such escape.
    """
    untracked(repo)
    command = "git commit -q --allow-empty -m don\\'t\n" + f"sh -c 'git {CL} -fdx'"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None


def test_the_clean_force_config_is_read_out_of_the_argv(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`git clean` refuses without `-f` unless this setting says otherwise, and it
    is a token in the argv the hook already tokenizes.
    """
    untracked(repo)
    command = f"git -c clean.requireForce=false {CL} -d"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_every_false_spelling_of_the_clean_force_config_counts(
    deny_reason: HookRunner, repo: Path, value: str
) -> None:
    """Measured: each of these lets `git clean -d` delete untracked content. The
    key is compared case-insensitively too, as git compares it."""
    untracked(repo)
    command = f"git -c clean.requireforce={value} {CL} -d"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_a_true_clean_force_config_leaves_the_refusal_in_place(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The other direction, so the waiver is not read as "any -c mentioning the
    key".
    """
    untracked(repo)
    command = f"git -c clean.requireForce=true {CL} -d"
    assert deny_reason(HOOK, command, payload_cwd=repo) is None


# --- the command name does not have to be a word of its own -----------------

R = "re" + "set"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"x=$(git {R} --hard)", id="assignment"),
        pytest.param(f"export OUT=$(git {CL} -fdx)", id="export-assignment"),
        pytest.param(f"readonly x=$(git {R} --hard)", id="readonly-assignment"),
        pytest.param(f"a=1 b=$(git {R} --hard) true", id="assignment-prefix"),
        pytest.param(f"echo a$(git {R} --hard)", id="glued-to-a-word"),
        pytest.param(f"x=`git {R} --hard`", id="backticks-after-assignment"),
        pytest.param(f"echo ${{x:-$(git {R} --hard)}}", id="parameter-default"),
        pytest.param(f"cat <(git {R} --hard)", id="process-substitution"),
        pytest.param(f"diff <(git {CL} -fdx) /dev/null", id="process-subst-operand"),
        pytest.param(f"cat > /dev/null < <(git {R} --hard)", id="redirected-in"),
    ],
)
def test_a_name_that_does_not_begin_its_word_is_still_read(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """Each of these takes a worktree, and `mentions` is the only counter that can
    see it: one character in front of the `$(` stops any word-peeling reduction,
    while the real parse masks the substitution away.
    """
    dirty(repo)
    untracked(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_a_path_qualified_name_is_still_read(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A `/` in front of the name is a path qualifier, so `/usr/bin/git` is the same
    git.

    Wrapped in a substitution on purpose: bare, the real parser reads it and refuses
    on the measurement, so the assertion would hold with the backstop's guard
    broken.
    """
    dirty(repo)
    for command in (f"x=$(/usr/bin/git {R} --hard)", f"x=$(./git {R} --hard)"):
        assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"mygit {R} --hard", id="suffix-of-another-name"),
        pytest.param(f"legit {R} --hard", id="contains-the-letters"),
        pytest.param(f"digit {R} --hard", id="ends-with-the-letters"),
        pytest.param(f"git-lfs {R} --hard", id="hyphenated-sibling"),
        pytest.param(f"gitk {R} --hard", id="prefix-of-another-name"),
        pytest.param(f"cat .gitignore && echo {R}", id="dotfile-name"),
        # The rest of `NAME_CHAR`, one member per case: a member left unexercised
        # is one that could be dropped from the class with every test still green,
        # which is how `mygit` becomes a covered call.
        pytest.param(f"git2 {R} --hard", id="digit-behind"),
        pytest.param(f"2git {R} --hard", id="digit-in-front"),
        pytest.param(f"git_x {R} --hard", id="underscore-behind"),
        pytest.param(f"x_git {R} --hard", id="underscore-in-front"),
        pytest.param(f"git+x {R} --hard", id="plus-behind"),
        pytest.param(f"x+git {R} --hard", id="plus-in-front"),
        # A `/` behind the name makes the `git` a directory component, so the name
        # is whatever follows it -- the other half of the basename rule the
        # path-qualified test above pins.
        pytest.param(f"git/lfs {R} --hard", id="git-as-a-directory"),
        pytest.param(f"a/git/b {R} --hard", id="git-as-an-inner-directory"),
    ],
)
def test_a_name_that_merely_contains_the_letters_is_not_read_as_git(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """The bound on the widening: finding the name by its neighbours would read
    `mygit` as `git` if the class were wrong, so it is spelled from the characters a
    command name may hold.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


def test_a_quoted_covered_verb_stays_inert(deny_reason: HookRunner, repo: Path) -> None:
    """`commit` does not run its arguments, and nothing separates the two names
    here. Paired with the test below: the two lines differ by one `(`.
    """
    dirty(repo)
    command = f'git commit -q --allow-empty -m "git {R} --hard"'
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


def test_a_substitution_inside_an_inert_argument_is_not_inert(
    deny_reason: HookRunner, repo: Path
) -> None:
    """What is inside a substitution runs before the commit it is quoted into
    begins, and the `(` between the two names ends the call the guard belonged to.
    """
    dirty(repo)
    command = f"git commit -q --allow-empty -m x$(git {R} --hard)"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"git log -1 git) {R} --hard", id="after-a-log"),
        pytest.param(
            f"git commit -q --allow-empty -m m git) {R} --hard", id="after-a-commit"
        ),
        pytest.param("git log -1 git) checkout -- a.txt", id="checkout-verb"),
    ],
)
def test_a_separator_behind_the_name_still_ends_the_previous_command(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """A `)` behind a bare `git` sits inside the same token, so a span measured only
    up to the name never contains it and the guard survives into a call that is not
    its own.

    These lines are malformed shell, and refusing one the shell would reject costs
    nothing.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_the_first_name_on_a_line_needs_no_previous_candidate(
    deny_reason: HookRunner, repo: Path
) -> None:
    """For the first name the span runs from the start of the line, so a subshell
    opened just before it is read like one opened between two names.
    """
    dirty(repo)
    assert deny_reason(HOOK, f"( git {R} --hard )", payload_cwd=repo) is not None
    assert deny_reason(HOOK, f"(git {R} --hard)", payload_cwd=repo) is not None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"git bisect {R}", id="bisect"),
        pytest.param(f"git status --short {R}", id="status-then-a-verb"),
    ],
)
def test_an_unreadable_subcommand_position_costs_a_refusal(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """The price of the look-ahead. Neither line discards anything, and the first is
    one an agent types; they are refused because the subcommand position holds a
    word this hook neither covers nor knows to be inert — which is also what a
    torn-apart option value looks like.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_a_tree_mark_survives_a_file_it_cannot_stat(tmp_path: Path) -> None:
    """A broken symlink is walked and cannot be stat'ed. The walk runs while a token
    is being derived, and a raise there reaches `LAST_RESORT`, which carries no
    token.
    """
    from block_git_discard.hook import tree_mark

    (tmp_path / "kept.txt").write_text("x")
    (tmp_path / "dangling").symlink_to(tmp_path / "absent.txt")

    mark = tree_mark(tmp_path)

    assert "dangling\0gone" in mark
    assert "kept.txt\0" in mark


def test_a_capped_tree_mark_is_stable_over_what_it_did_not_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token is minted on one run and checked on the next, so a mark that reads
    part of a tree has to read the same part both times. Editing a file past the cap
    leaves the token unlocking the tree.
    """
    import block_git_discard.hook as h

    monkeypatch.setattr(h, "TREE_MARK_LIMIT", 3)
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text("x")

    before = h.tree_mark(tmp_path)
    assert before.endswith("capped-at-3")
    assert "f0.txt\0" in before
    assert "f3.txt\0" not in before

    (tmp_path / "f5.txt").write_text("changed")
    (tmp_path / "f9.txt").write_text("new")
    assert h.tree_mark(tmp_path) == before


def test_a_wrapper_is_peeled_only_where_the_option_still_runs_the_command() -> None:
    """`unwrapped` has four exits and the shallow parse rests on all of them:
    peeling one that runs nothing invents a move, and an option in neither set is
    refused, since skipping the wrong number of words leaves another word in command
    position.
    """
    from block_git_discard.hook import Unmeasurable, unwrapped

    assert unwrapped(["builtin", "cd", "x"]) == ["cd", "x"]
    assert unwrapped(["command", "-p", "cd", "x"]) == ["cd", "x"]
    assert unwrapped(["command", "builtin", "cd", "x"]) == ["cd", "x"]
    assert unwrapped(["cd", "x"]) == ["cd", "x"]

    for reporting in (["command", "-v", "cd"], ["command", "-V", "cd"]):
        assert unwrapped(reporting) == reporting

    assert unwrapped(["command", "-p"]) == ["command", "-p"]

    with pytest.raises(Unmeasurable):
        unwrapped(["command", "-z", "cd", "x"])


def test_a_reason_fragment_is_cut_to_something_a_transcript_can_carry() -> None:
    """git answers an unusable invocation with its usage screen, which uncut
    displaces the context the caller needs.
    """
    from block_git_discard.hook import clipped

    assert clipped("a  b\n c") == "a b c"
    assert clipped("short") == "short"

    long = clipped("x" * 500)
    assert len(long) == 400
    assert long.endswith("…")


def test_the_query_helper_refuses_a_verb_that_is_not_read_only(tmp_path: Path) -> None:
    """Every measurement runs through one subprocess call, and this is the guard on
    it.

    `init` rather than a discarding verb: if the guard is removed this test runs
    what it passed.
    """
    from block_git_discard.hook import Unmeasurable, git

    with pytest.raises(Unmeasurable):
        git(str(tmp_path), "init", "-q", ".")
    with pytest.raises(Unmeasurable):
        git(str(tmp_path))

    # And the guard is not simply rejecting everything.
    repo = init(tmp_path / "r")
    assert git(str(repo), "rev-parse", "--is-inside-work-tree").strip() == "true"


def test_an_untracked_entry_that_cannot_be_stat_ed_still_fingerprints(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A dangling symlink is listed by git and cannot be stat'ed. The walk runs
    while the override token is derived, and a raise there reaches `LAST_RESORT`,
    which carries no token.
    """
    (repo / "dangling.link").symlink_to(repo / "absent.txt")
    reason = deny_reason(HOOK, "git clean -f", payload_cwd=repo)
    assert reason is not None

    # A measured refusal. An unmeasured one interpolates the exception, whose text
    # carries the very filename, so naming the file would be satisfied by the
    # failure this excludes. "At stake" is printed only where the walk finished.
    assert "At stake" in reason, reason
    assert "dangling.link" in reason
