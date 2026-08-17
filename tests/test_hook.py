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


def commit_all(repo: Path, message: str = "c1") -> None:
    git(repo, "add", ".")
    git(repo, "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One committed file, one sibling branch, and a clean working tree."""
    r = init(tmp_path / "r")
    (r / "a.txt").write_text("v1\n")
    (r / "k.txt").write_text("keep\n")
    commit_all(r)
    git(r, "branch", "other")
    return r


def dirty(repo: Path, name: str = "a.txt", text: str = "DIRTY\n") -> Path:
    (repo / name).write_text(text)
    return repo / name


def tracked_dirty(repo: Path, name: str, message: str = "add") -> Path:
    """Commit `name` into `repo`, then change it — a tracked file with work in it.

    Distinct from `dirty`, which changes a file the `repo` fixture already
    committed; this one brings its own file, for tests that need a particular
    name (a non-ASCII one, a `#` in it, a name that is just a digit).
    """
    target = repo / name
    target.write_text("v1\n")
    commit_all(repo, message)
    target.write_text("DIRTY\n")
    return target


def repo_holding_work(path: Path) -> Path:
    """A second repository with an uncommitted change, built at `path`.

    Several tests need one: the question they ask is whether the hook measured
    the directory the command will actually run in, and that only has an answer
    when the two candidates differ in what is at stake.
    """
    r = init(path)
    (r / "a.txt").write_text("v1\n")
    commit_all(r)
    dirty(r, text="PRECIOUS\n")
    return r


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
    """`-B` / `-C` read like force but only move a branch label.

    Folding them in with `-f` would deny every branch creation on a dirty tree,
    which is the common case rather than the dangerous one.
    """
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
    """The reader is the agent whose Bash call was just denied, and that call has
    no tty. An interactive suggestion — `restore -p`, `checkout -p`, `add -i` —
    aborts for it, so routing there would name a route it cannot take."""
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
    """Verbs outside COVERED, deliberately.

    Most of these reach the worktree — `rm -f`, `read-tree -u`,
    `checkout-index -f` and the sequencer aborts all overwrite content that was
    never hashed into the object store. `git stash` is here for a different
    reason: it moves content into a stash entry rather than destroying it, so it
    would not be denied even if it were covered.

    Covering the destructive ones means recognizing plumbing and sequencer verbs
    whose safe measurement needs git's own worktree/index normalization, and an
    agent does not type them. The limit is stated in the hook's header; this pins
    it so a later reader sees a decision rather than a gap, and so widening the
    list shows up as a change here.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


def test_staged_only_content_is_not_protected(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Staged content whose only copy outside the object store is the index.

    `git add` writes the blob into the object store, so losing the index entry
    leaves the content reachable via `git fsck` — recoverable, and not the
    irreversible loss this hook guards. The hook measures worktree-versus-index,
    which reports nothing in this state; that is the designed outcome, not an
    oversight, and the header says so.
    """
    (repo / "a.txt").write_text("STAGED\n")
    git(repo, "add", "a.txt")
    # `checkout -- <path>` restores from the INDEX, not from HEAD, so this leaves
    # the worktree matching the index with both ahead of HEAD. Restoring from
    # HEAD instead would make worktree and index differ, which the hook DOES
    # report — a different case from the one this test pins.
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
    """An env assignment or a wrapper puts `git` past argv[0].

    Recognizing only argv[0] would leave the guard off for an everyday idiom —
    the bare form denies while the prefixed one sails through, which is the worst
    shape a guard can have.
    """
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
    """The backstop reads the verb in SUBCOMMAND position, not anywhere in the
    line, so an ordinary commit message using one of the five words as English is
    not a covered call and is not treated as one."""
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
    """Quoting is not a reason to stop reading, and this is the third accepted
    false positive.

    "Inside quotes, so not executed" holds until the receiving command executes
    it — `sh -c`, `bash -lc`, `eval`, a pipe into a shell, `ssh host '...'` — and
    that set has no boundary to enumerate. Treating quoted text as unreadable
    rather than as inert refuses the `grep` and the `echo` alongside the real
    ones, which is a token; the other way round leaves a one-line bypass of the
    whole guard.
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
    """The other accepted false positive, and the price of the backstop.

    Unquoted, this is textually indistinguishable from the parse gaps the test
    exists to catch — a covered verb sitting in the line with no call read from
    it. Telling them apart is the parsing that kept going wrong, so the crude
    test wins and the refusal costs a token.
    """
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
    """The rest of the accepted false positives, pinned as a class.

    Each is `git` next to a covered verb with no call read from it — which is
    also the exact signature of a parse gap. Telling the two apart is the parsing
    that kept going wrong, so the backstop does not try.
    """
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
    """A `git <verb>` in the ARGUMENTS of a git call that does not run its
    arguments is text, not a call — most visibly in a commit message, which this
    hook's own subject invites people to write.

    The allowlist behind this is the safe direction: an omission from it costs a
    refusal, while listing the subcommands that DO run an argument would make an
    omission a hole — and `submodule foreach`, `bisect run` and
    `-c alias.x='!...'` were each confirmed by execution to discard that way.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "git submodule foreach 'git reset --hard'",
        "git bisect run git reset --hard",
        "git -c alias.zz='!git reset --hard' zz",
        # A separator ends the guard: what follows is its own call again.
        'git commit -m "x" && git reset --hard',
        'git commit -m "x"; git checkout -- a.txt',
    ],
)
def test_a_subcommand_that_runs_its_argument_is_still_refused(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """The exceptions the allowlist must not swallow. The first three were each
    run for real and observed to revert the tree."""
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
    """A newline ends a command as surely as a `;` does — and, being whitespace,
    never survives into a word for the separator test to find.

    Carried across lines, one inert git call disarmed the backstop for everything
    after it: `git commit -m "wip"` then `sh -c 'git reset --hard'` was allowed,
    and destroyed the tree. Every allowlist entry carried the same hole.
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
    """A backslash inside a comment is ordinary text to the shell.

    `echo one # note \\` then `echo two` prints both — verified in bash and zsh.
    Joining the lines first made the second one part of the comment and deleted
    it outright, so the discard was never seen at all: the whole line read as
    commented out, and the reset ran.
    """
    dirty(repo)
    command = f"{first} # note \\\ngit reset --hard"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_a_quoted_newline_does_not_end_the_inert_guard(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A newline inside quotes is part of a word, which is how a commit body is
    written — so it must not read as a command boundary. Otherwise one inert call
    answers two ways depending on whether its message has a body."""
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
    """The limit of the guarantee, pinned so it reads as known rather than missed.

    The backstop reads a covered call out of the words, so both halves have to be
    there as words. A command name that becomes `git` only at run time, and a
    verb that becomes `checkout` only at run time — which is what an alias is —
    are invisible to both parsers. Resolving either means reading the environment
    or the config the command will run under, and this hook reads neither.

    `$(echo git) reset --hard` is deliberately NOT on this list: the word `git`
    is literally present there, so peeling the clinging characters finds it. Only
    a name that is nowhere in the text falls through.
    """
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


@pytest.mark.parametrize(
    "command", ["git clean -fd -e keep.txt", "git clean -fd --exclude=keep.txt"]
)
def test_clean_with_an_exclude_drops_the_narrowing(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """`-e` narrows what clean removes in a way `ls-files` does not mirror.

    Both spellings must land the same way: with the attached value left on the
    token, `--exclude=keep.txt` would miss an `--exclude` test and the narrowing
    would silently stay in place for one spelling only.
    """
    (repo / "new.txt").write_text("work\n")
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_untracked_paths_are_listed_in_the_frame_the_reason_names(
    deny_reason: HookRunner, nested: Path
) -> None:
    """`ls-files --others` reports cwd-relative paths, `diff --name-only` reports
    root-relative ones, and the reason names a single frame for both. Run from a
    subdirectory, the untracked list has to come back root-relative or it sends
    the reader to a path that resolves nowhere."""
    (nested / "sub" / "fresh.txt").write_text("work\n")
    reason = deny_reason(
        HOOK, "git clean -fd", None, nested / "sub", payload_cwd=nested / "sub"
    )
    assert reason is not None
    assert "sub/fresh.txt" in reason, reason


def test_ignored_files_are_routed_to_the_stash_form_that_reaches_them(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`-u` takes untracked files but stops at ignored ones; only `--all` reaches
    those. Offering `-u` for `clean -fX` names a command that answers "No local
    changes to save" and changes nothing."""
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
    """A filename after `--` is a pathspec, whatever it starts with.

    Reading options from the whole argument list splits `-patch.txt` per
    character, and the `p` that falls out reads as `-p` — routing a real discard
    into the interactive branch, which allows it.
    """
    tracked_dirty(repo, "-patch.txt", "dash")
    reason = deny_reason(HOOK, "git checkout -- '-patch.txt'", payload_cwd=repo)
    assert reason is not None, "a dash-leading pathspec was read as -p"
    assert "-patch.txt" in reason


def test_clean_narrows_to_its_pathspec(deny_reason: HookRunner, repo: Path) -> None:
    """`ls-files` takes the same pathspec tail, so a narrowed clean stays narrow.

    Measuring the whole tree instead reports files the command cannot reach, and
    the deny then names work that was never at risk.
    """
    (repo / "build").mkdir()
    (repo / "keepdir").mkdir()
    (repo / "keepdir" / "precious.txt").write_text("keep me\n")
    assert deny_reason(HOOK, "git clean -fd build", payload_cwd=repo) is None
    # A .txt rather than a build-artifact extension: the user's global ignore
    # file is in effect here, and `--exclude-standard` honours it, so an ignored
    # name would make this pass for the wrong reason.
    (repo / "build" / "junk.txt").write_text("junk\n")
    reason = deny_reason(HOOK, "git clean -fd build", payload_cwd=repo)
    assert reason is not None
    assert "build/junk.txt" in reason
    assert "precious" not in reason


def test_separated_source_value_is_not_taken_as_a_pathspec(
    deny_reason: HookRunner, nested: Path
) -> None:
    """`--source HEAD~1` puts a ref where a naive scan reads a pathspec.

    The ref carries `~`, which trips the unexpanded-pathspec check and collapses
    the measurement to the whole tree — so the separated spelling would report an
    unrelated dirty file while the attached one narrows correctly.

    Both assertions run against a DIRTY `a.txt`. Against a clean one they hold
    whether or not the value is handled — the narrowing lands on an empty set
    either way — so the test would pass over a scan that swallowed the pathspec
    and one that read the ref as a path alike, and say nothing about either.
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
    """The other half: the ref must not widen the measurement to an unrelated
    dirty file, whichever spelling carries it."""
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
    """The names come from `git diff --name-only`, which is root-relative.

    Feeding them back as pathspecs in a subdirectory resolves to `sub/sub/file`,
    matches nothing, and exits 0 — so the hunk list empties with nothing to
    signal that it did.
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

    The untracked kinds have no patch to fingerprint, so this is the assertion
    that pins the fallback to something content-dependent. Each caller supplies a
    different way for that fallback to come apart — a subdirectory frame, a
    C-quoted name — while the sequence itself is one thing and lives here.
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


def test_two_covered_verbs_on_one_line_can_both_be_acked(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Reading only the first token on the line traps the second verb.

    Tokens accumulate as they are appended, so a lookup returning the earliest
    match keeps comparing the first block's token against the second block's
    expectation — the line then denies forever, appending one more token each
    round without ever converging.
    """
    # Two dirty files, so the narrowed verb and the whole-tree one measure
    # different things and therefore issue different tokens. With only one dirty
    # file both measurements coincide and a single token covers both — correct
    # behaviour, but it would not exercise the accumulation this pins.
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
    """`git diff --name-only` emits repo-root-relative paths wherever it runs, but
    the agent's next call runs in the payload cwd. Pasting `sub/s.txt` from inside
    `sub/` yields `sub/sub/s.txt`, which git rejects — so the suggestions have to
    carry the root rather than leaving the frame unstated."""
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
    """`-C` moves git's directory, not the shell's.

    Offered as an alternative frame, it sends `> keep.patch` to wherever the
    caller stands while the patch body carries root-relative paths — and
    `git apply keep.patch` from there exits 0 having restored NOTHING. A route
    that reports success and returns none of the content is worse than no route,
    this being the one reached for exactly when the content matters. So the
    message names one directory and every step runs in it.
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
    """Listed without a disjunction, and under "Both … Run them", the pair reads
    as two steps rather than two choices.

    Followed that way it breaks: the stash moves the content first, so the diff
    that follows writes a 0-byte patch and `git apply` ends at 128 "No valid
    patches in input". The check below runs the misreading to confirm it is a
    misreading, then pins the word that forecloses it.
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
    """A covered verb can move the BRANCH as well as the tree, and a copy taken
    against the old commit then refuses to go back on — `git stash pop` exits 1,
    `git apply` reports "patch does not apply". What survives either failure is
    the copy itself, and that is what the caller is deciding on here."""
    dirty(repo)
    reason = deny_reason(HOOK, "git checkout -f other", payload_cwd=repo)
    assert reason is not None
    assert "may not go back on cleanly" in reason, reason
    assert "Neither route discards what it could not place" in reason, reason


def test_the_patch_route_round_trips_binary_content(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The patch route has to be a copy, not a description of one.

    `git diff` without `--binary` writes `Binary files a/x and b/x differ` — a
    sentence — and `git apply` then refuses with "cannot apply binary patch …
    without full index line". It refuses the WHOLE patch, so a text file listed
    beside the binary one is not restored either. The route is followed here
    exactly as the message prints it, and both files checked afterwards.
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
    """`git stash push -- <untracked>` errors with "did not match any file(s)";
    only `-u` takes an untracked path. Routing to the plain form would hand the
    reader a command that cannot run."""
    (repo / "new.txt").write_text("work\n")
    reason = deny_reason(HOOK, "git clean -fd", payload_cwd=repo)
    assert reason is not None
    assert "stash push -u" in reason, reason


def test_reason_lists_paths_usable_as_git_operands(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The suggested commands take a `<path>`, so one has to be recoverable from
    the message. `git diff --stat` abbreviates a long path to `.../tail`, which
    git will not accept as an operand."""
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
    """`a{1,2}.txt` reaches the hook unexpanded, so it cannot be forwarded as
    written; measuring everything over-detects rather than letting it through."""
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
    """A file the command visibly does not name, sitting on the at-stake list,
    reads two ways — an over-wide report, or a hook that measured the wrong
    thing. Only one of those is worth acting on, so the message distinguishes
    them rather than leaving the reader to guess."""
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


def test_non_repository_cwd_denies(deny_reason: HookRunner, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert deny_reason(HOOK, "git checkout -- a.txt", payload_cwd=plain) is not None


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
    """Each redirects the command away from what would be measured here.

    `--git-dir` / `--work-tree` move the tree itself; `--namespace` shifts ref
    resolution, so a measurement against the local refs describes a different
    starting point. Neither can be followed cheaply, and an agent does not type
    them, so they are refused rather than guessed at.
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
    """Each of these carries a `where` this hook refuses to guess at — a bare
    `cd`, a `cd` the shell may not have run, a relocated tree, a moved
    environment. Each also carries a verb form that destroys nothing wherever it
    lands, so asking `where` at all would refuse it for a reason unrelated to
    what it does. Settling the form first is what keeps that from happening.
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
    """Without a token of its own an unmeasurable case has no exit: re-running
    reproduces the same refusal, so a genuine intent to discard would be stuck.
    This token binds the command, which is all that is knowable once the
    measurement itself has failed."""
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
    only. Run from a subdirectory the measurement then misses every change above
    it, finds nothing at stake, and lets the discard through — with a
    clean-looking report as the only trace."""
    git(nested, "config", "diff.relative", "true")
    (nested / "a.txt").write_text("CHANGED\n")
    assert deny_reason(HOOK, "git reset --hard", payload_cwd=nested / "sub") is not None


def test_a_non_ascii_untracked_path_still_fingerprints_its_content(
    deny_reason: HookRunner, repo: Path
) -> None:
    """git C-quotes a path holding a non-ASCII byte, and `"\\303\\251.txt"` names
    no file on disk. Every stat then misses, every file marks `gone`, and the
    fingerprint stops depending on content — so one token unlocks whatever the
    file is later rewritten to hold."""
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
    """`pushd` moves the shell exactly as `cd` does. Following only `cd` measures
    the directory the command never ran in — which reports nothing at stake and
    lets the other repository's uncommitted work go."""
    other = repo_holding_work(tmp_path / "other")
    assert (
        deny_reason(HOOK, f"pushd {other} && git reset --hard", payload_cwd=repo)
        is not None
    )


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
    """A subshell's `cd` is undone when that subshell exits.

    Replaying it regardless measured a directory the git command never ran in:
    `(cd /clean && git status); git reset --hard` came back clean and allowed,
    while the reset took the payload's own tree. A pipeline stage and a
    backgrounded command each get their own subshell the same way.
    """
    dirty(repo)  # the payload cwd has work at stake
    other = init(tmp_path / "other")  # ... and the subshell's target does not
    (other / "k.txt").write_text("v\n")
    commit_all(other)
    command = line.format(other=other) + "git reset --hard"
    denied = deny_reason(HOOK, command, payload_cwd=repo) is not None
    assert denied is not moves, command


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
    """git takes each of these values as a separate token. Consuming only the
    flag leaves the value standing in the subcommand position, where it hides the
    verb and the whole invocation slips past recognition."""
    dirty(repo)
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_an_unknown_global_option_does_not_hide_the_verb(
    deny_reason: HookRunner, repo: Path
) -> None:
    """An option git does not know makes git exit 129 before the subcommand runs,
    so the value-taking list stays closed: nothing outside it may consume a
    following token, or the verb behind it disappears."""
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
    """A subcommand's options go through git's parse-options, which accepts any
    unambiguous abbreviation: `git reset --har` really does reset. Matching by
    equality reads these as carrying no flag at all and lets the discard
    through."""
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
def test_an_abbreviated_harmless_option_still_passes(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """Reading abbreviations only for the destructive flags would refuse these,
    which destroy nothing."""
    dirty(repo)
    (repo / "u.txt").write_text("untracked\n")
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


# --- what a forced checkout actually reaches --------------------------------


def test_a_forced_checkout_still_narrows_to_its_pathspec(
    deny_reason: HookRunner, nested: Path
) -> None:
    """`-f` waives git's refusal to act on a dirty tree; it does not widen what
    the command opens. Measured whole, `git checkout -f -- a.txt` would be
    refused over a dirty `b.txt` it never touches."""
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
    """`-b` takes the new branch's NAME as its operand. Read as a pathspec it
    narrows the measurement to a file that does not exist and reports nothing at
    stake — while `-f` discards the tree for real."""
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
def test_a_directory_this_line_creates_is_left_alone(
    deny_reason: HookRunner, repo: Path, creator: str
) -> None:
    """A path holding nothing when the hook decides can only come to hold what
    the rest of the line puts there, which is not content this hook was ever
    protecting.

    Parametrized over unrelated producers to pin the contract that the reading
    does NOT come from recognizing them: that set has no boundary, and a rule
    built on it would refuse whichever spelling had not been listed yet.
    """
    dirty(repo)  # so passing cannot be an artefact of an empty payload directory
    command = f"{creator} && cd repo && git checkout other"
    assert deny_reason(HOOK, command, payload_cwd=repo) is None, command


def test_the_two_spellings_of_a_branch_switch_agree(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`switch` clears without the repository and `checkout` cannot, so the two
    used to disagree on the same intent purely by spelling."""
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
    directory the shell was already in — which is right here, and measurable. The
    answer names the files actually at risk instead of refusing blind."""
    dirty(repo)
    reason = deny_reason(HOOK, "cd typo; git reset --hard", payload_cwd=repo)
    assert reason is not None
    assert "a.txt" in reason, reason


def test_a_cd_that_will_fail_passes_when_that_directory_holds_nothing(
    deny_reason: HookRunner, repo: Path
) -> None:
    assert deny_reason(HOOK, "cd typo; git reset --hard", payload_cwd=repo) is None


def test_git_dash_c_into_a_missing_directory_is_left_alone(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`git -C <missing>` exits 128 without opening anything."""
    dirty(repo)
    assert (
        deny_reason(HOOK, "git -C no-such-dir reset --hard", payload_cwd=repo) is None
    )


def test_content_moved_in_by_the_same_line_is_not_protected(
    deny_reason: HookRunner, repo: Path, tmp_path: Path
) -> None:
    """Pinned so this reads as a decision rather than a gap.

    The work does exist when the hook decides, but at a path nothing here can
    connect to the one the command names. Closing it needs an enumeration of
    content-moving commands — the same unbounded set, approached from the other
    side, that the test above refuses to build.
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
    """`git -C` names a directory exactly as `cd` does, so it goes through the
    same gate. Held apart, the two drifted: `cd $REPO && git reset --hard` was
    refused while `git -C $REPO reset --hard` — the same command, said the other
    way — went through."""
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
    """ "Does not exist" is read as "holds nothing", and that reading is only about
    the path as WRITTEN. An unexpanded target never matches a directory, so
    without this the reading would be applied to a spelling the shell is about to
    turn into some other path entirely — handing a pass to whatever it names.

    A glob is on this list though it is deliberately off the pathspec one: git's
    own glob matches at least as widely as the shell's, so forwarding a pathspec
    over-detects, while a directory glob resolves to one path the shell picks and
    the hook cannot.
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
    elsewhere = init(tmp_path / "elsewhere")
    (elsewhere / "k.txt").write_text("v\n")
    commit_all(elsewhere)
    reason = deny_reason(HOOK, command, {"HOME": str(home)}, payload_cwd=elsewhere)
    assert reason is not None, command
    assert "a.txt" in reason, reason


def test_a_tilde_cd_target_is_expanded_rather_than_refused(
    deny_reason: HookRunner, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~` is the one expansion this hook shares with the shell, so it resolves
    instead of refusing — and having resolved it, it measures what is there."""
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
    """An accepted false positive, pinned so it reads as a decision.

    Writing a script destroys nothing, and setting the body apart to say so takes
    a delimiter rule. Every attempt at one here misread something — a here-string
    takes a word rather than a delimiter, a `<<-` marker travels attached to it,
    an unterminated body swallows the rest of the line — and each misread showed
    up as a covered verb this hook never saw. The refusal costs an override
    token; the misreads cost the guarantee.
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
        # A here-STRING takes a word, not a delimiter. Hunting for that word as
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
    """Skipping to end-of-line on a MISREAD `<<` is how the guard switches itself
    off: no terminator was found, so nothing was a body, and the tokens are split
    as the ordinary commands they are."""
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
        # A backslash-escaped newline is a line JOIN to the shell; shlex leaves a
        # bare newline, which reads as a command boundary and drops the pathspec.
        "git checkout \\\n  -- a.txt",
        "git reset \\\n  --hard",
    ],
)
def test_a_redirection_is_not_a_command_boundary(
    deny_reason: HookRunner, repo: Path, command: str
) -> None:
    """A redirection splits nothing and its file descriptor is not an argument.

    Read as a boundary, `git clean -fd 2>/dev/null` left `2` behind as clean's
    pathspec — narrowing the measurement to nothing — and `git 2>&1 reset --hard`
    lost the verb entirely. Both were allowed, and both destroy content.
    """
    dirty(repo)
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None, command
    assert "a.txt" in reason, reason


@pytest.mark.parametrize("name", ["2", "2024"])
def test_a_digit_pathspec_is_not_read_as_a_file_descriptor(
    deny_reason: HookRunner, repo: Path, name: str
) -> None:
    """`2>log` and `2 > log` tokenize identically, so whether the digit is a
    descriptor or a pathspec survives only as ADJACENCY in the raw text.

    Deciding it after tokenizing has to guess, and guessing "a digit before a
    redirection is a descriptor" throws the pathspec away: `git checkout -- 2 >
    log` then measured nothing and was allowed, over a repository holding a file
    named `2`.
    """
    tracked_dirty(repo, name, "digit")
    reason = deny_reason(HOOK, f"git checkout -- {name} > log", payload_cwd=repo)
    assert reason is not None
    assert name in reason, reason


@pytest.mark.parametrize("joiner", [";", "&&"])
def test_a_brace_group_cd_is_followed(
    deny_reason: HookRunner, repo: Path, tmp_path: Path, joiner: str
) -> None:
    """A brace group runs in the CURRENT shell, so unlike a subshell its `cd`
    persists into what follows.

    `{` is a word to the tokenizer — deliberately, so `{}` in `find -exec` and a
    `{a,b}` expansion stay whole — which left it sitting in command position with
    the `cd` behind it unread. The measurement then stayed where the payload
    pointed while the discard happened somewhere else.
    """
    other = repo_holding_work(tmp_path / "other")
    command = f"{{ cd {other}; }}{joiner} git reset --hard"
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None, command
    assert "a.txt" in reason, reason


def test_a_brace_expansion_operand_is_left_whole(
    deny_reason: HookRunner, repo: Path
) -> None:
    """Only a LONE brace is a group keyword. `{}` is one token and an argument."""
    dirty(repo)
    assert (
        deny_reason(HOOK, "find . -name '*.py' -exec ls {} \\;", payload_cwd=repo)
        is None
    )


def test_a_redirection_target_is_not_read_as_a_pathspec(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The narrowing has to survive too: reading `2` or the target as a pathspec
    would collapse the measurement rather than merely widen it."""
    (repo / "junk").mkdir()
    (repo / "junk" / "j.txt").write_text("x\n")
    reason = deny_reason(HOOK, "git clean -fd 2>/dev/null", payload_cwd=repo)
    assert reason is not None
    assert "junk/j.txt" in reason, reason


def test_a_pathspec_holding_a_hash_is_not_truncated(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The shell opens a comment at a `#` that STARTS a word; shlex opens one at
    a `#` anywhere in it. Under the latter `f#1.txt` measures as `f`, matches
    nothing, and the discard goes through."""
    tracked_dirty(repo, "f#1.txt", "hash")
    reason = deny_reason(HOOK, "git checkout -- f#1.txt", payload_cwd=repo)
    assert reason is not None
    assert "f#1.txt" in reason, reason


@pytest.mark.parametrize("spelling", ["'x #1.txt'", '"x #1.txt"', "x\\ \\#1.txt"])
def test_a_quoted_hash_in_a_pathspec_survives_comment_stripping(
    deny_reason: HookRunner, repo: Path, spelling: str
) -> None:
    """A `#` inside quotes is literal to the shell, so cutting there leaves an
    unbalanced quote — the tokenizer falls back to a whitespace split, the
    pathspec becomes `'x`, it matches nothing, and the discard goes through. The
    three spellings name one file and must be answered alike."""
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
    """`git clean -f` does not descend into an untracked directory, but
    `ls-files --others` does. Reporting its output names files the command will
    not touch — and presents them as exact."""
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
    """A substitution's contents are EXECUTED, so hiding them is backwards.

    Collapsing `$(...)` to one word keeps the tokenizer from splitting the line
    at its parens, and that is worth doing — but the same collapse applied to the
    mention test took the verb out of view, and `echo $(git reset --hard)` was
    allowed while discarding the tree. The tokenizer needs the mask; the mention
    test splits on whitespace and peels the clinging characters, so it does not.
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
    fragments — `git -C $`, then the inner command, then `reset --hard` — and the
    covered verb is in none of them. Collapsing it to one word first keeps the
    line whole, and the `$` that survives marks the value as one only the shell
    can supply."""
    dirty(repo)
    (repo / "u.txt").write_text("untracked\n")
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_one_unread_call_among_read_ones_still_refuses(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The backstop counts rather than matches.

    Asking only "was anything recognized?" lets a line carrying one readable call
    vouch for an unreadable one beside it — and the unreadable one is the whole
    point.
    """
    dirty(repo)
    command = "git checkout -- k.txt && sh -c 'git reset --hard'"
    reason = deny_reason(HOOK, command, payload_cwd=repo)
    assert reason is not None, command
    assert "could not read as a call" in reason, reason


def test_an_unread_call_can_be_overridden(deny_reason: HookRunner, repo: Path) -> None:
    """A refusal with no way through would strand a genuine intent, and this one
    fires on shapes the hook cannot read rather than on shapes it judged."""
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
    `--reset` both carry one — and both are plumbing this hook documents as
    passing through. The backstop matches whole words instead."""
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
    """A bundle ends at a short option that takes a value; what follows is the
    value. Reading past it turns the value's letters into flags, and the letters
    that turn up there are the ones that decide everything."""
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
    """Restoring a deleted file puts content back rather than taking any away:
    what returns comes from the index, and whatever it held before the delete was
    already gone by then. Refusing here trains its reader to reach for the
    override on a command that only undoes a removal."""
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
        ("git clean -fd", "`git status --short` lists them all"),
        # `-x` and `-X` reach ignored files, which a plain `git status` does not
        # report at all — for `clean -fX` it would show NONE of what the cap
        # stands in for.
        ("git clean -fdx", "`git status --short --ignored` lists them all"),
        ("git checkout -- .", "`git status --short` lists them all"),
    ],
)
def test_the_at_stake_list_is_capped(
    deny_reason: HookRunner, repo: Path, command: str, listing: str
) -> None:
    """This reason lands in the caller's transcript. A `clean -fdx` over a
    `node_modules` names tens of thousands of files, which arrives as megabytes
    displacing the context needed to act on the message. The count stays exact;
    the listing does not — and the command offered in its place has to be one
    that can actually show the kind of content at stake."""

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
    # times the content must not make a longer message. Asserting a line count
    # instead would pass on a message that still grew, just more slowly — and the
    # diffstat is per-file too, so capping only the path list does exactly that.
    small = len(reason_for(120).splitlines())
    large = len(reason_for(480).splitlines())
    assert small == large, (small, large)


def test_an_unrelated_conditional_does_not_refuse(
    deny_reason: HookRunner, repo: Path
) -> None:
    """`||` puts the directory in doubt only when what it guards MOVES the
    shell. Refusing on any earlier one blames a `cd` that is not on the line, and
    answers with no at-stake list where a full measurement was available."""
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
    """These move the tree exactly as `--git-dir` / `--work-tree` do, and they
    ride in through the very step that keeps `LC_ALL=C git ...` recognized.

    Measured from a CLEAN payload directory on purpose: the answer must come from
    the assignment, not from work that happens to sit where the hook was pointed.
    """
    elsewhere = repo_holding_work(tmp_path / "elsewhere")
    command = f"{name}={elsewhere} git reset --hard"
    assert deny_reason(HOOK, command, payload_cwd=repo) is not None, command


def test_an_ordinary_env_assignment_does_not_refuse_a_clean_tree(
    deny_reason: HookRunner, repo: Path
) -> None:
    """The refusal is keyed to `GIT_`, so a locale or pager prefix still gets the
    measured answer rather than an unmeasurable one."""
    assert deny_reason(HOOK, "LC_ALL=C git reset --hard", payload_cwd=repo) is None


def test_a_git_env_assignment_still_yields_to_a_harmless_form(
    deny_reason: HookRunner, repo: Path
) -> None:
    """A dry run destroys nothing wherever it is pointed, so `where` never comes
    up for it."""
    dirty(repo)
    assert deny_reason(HOOK, "GIT_DIR=/x git clean -n", payload_cwd=repo) is None
