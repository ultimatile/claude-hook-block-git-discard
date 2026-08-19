"""The handlers that turn an internal fault into a refusal instead of an exit.

These paths cannot be reached by feeding the hook a bad command, because the one
input known to reach them has been fixed -- which is exactly why they need tests
of their own. A path reachable only by a bug nobody has found yet is a path that
silently stops working.

So the fault is injected: each test runs the real `main()` in a real subprocess
with one internal broken on purpose, and asserts the process still exits 0 and
still prints a deny. In-process these faults would surface as test errors;
through a process boundary they surface as what they
actually are, since a hook that exits non-zero is reported by the harness as a
non-blocking error and the command then runs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import repo_holding_work, run_hook_process

# Injected before `main()` runs. Each string is the body of the break, applied to
# the imported module as `h`.
BREAK_TOKEN = """
def boom(*a, **k):
    raise RuntimeError("injected: token derivation")
h.bound_to = boom
"""

BREAK_MENTIONS = """
def boom(*a, **k):
    raise RuntimeError("injected: backstop count")
h.mentions = boom
"""

BREAK_TOKENIZE = """
def boom(*a, **k):
    raise RuntimeError("injected: tokenizer")
h.tokenize = boom
"""

BREAK_EXC_STR = """
class Unprintable(Exception):
    def __str__(self):
        raise RuntimeError("injected: exception rendering")
def boom(*a, **k):
    raise Unprintable()
h.measure = boom
"""


def run_broken(break_src: str, command: str, cwd: Path) -> tuple[int, str]:
    """Run `main()` with one internal broken; return (exit code, stdout)."""
    script = (
        "import sys\nimport block_git_discard.hook as h\n" + break_src + "h.main()\n"
    )
    proc = run_hook_process([sys.executable, "-c", script], command, cwd)
    return proc.returncode, proc.stdout


def decision(stdout: str) -> str | None:
    if not stdout.strip():
        return None
    return json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.fixture
def dirty_repo(tmp_path: Path) -> Path:
    """A repository with a tracked change in it -- something to actually lose."""
    return repo_holding_work(tmp_path / "r")


def test_a_broken_token_still_produces_a_refusal(dirty_repo: Path) -> None:
    """Deriving the override token happens inside the refusal, so a fault there
    escapes the refusal itself unless something catches it there. A lone
    surrogate in the command text is such a fault, and what it escapes into is
    a non-zero exit, which the harness reports as a non-blocking error before
    running the very command being refused."""
    code, out = run_broken(BREAK_TOKEN, "popd; git reset --hard", dirty_repo)
    assert code == 0, out
    reason = decision(out)
    assert reason is not None
    assert "failed" in reason and "composing the refusal" in reason, reason


def test_the_last_resort_offers_no_token_it_could_not_derive(
    dirty_repo: Path,
) -> None:
    """An override token is a hash of the command, and the hash is what broke.
    Printing one anyway would hand back a token that never matches, leaving a
    refusal with an exit that does not work."""
    _, out = run_broken(BREAK_TOKEN, "popd; git reset --hard", dirty_repo)
    reason = decision(out)
    assert reason is not None
    assert "No override token is offered" in reason, reason
    assert "ack:" not in reason, reason


def test_a_broken_backstop_count_refuses_rather_than_passes(dirty_repo: Path) -> None:
    """The count is the last thing the hook does and nothing follows it, so a
    fault there is silence -- which reaches the harness as consent. A count that
    could not be taken is not a count of zero."""
    code, out = run_broken(BREAK_MENTIONS, "sh -c 'git reset --hard'", dirty_repo)
    assert code == 0, out
    assert decision(out) is not None, out


def test_an_exception_that_cannot_be_rendered_still_refuses(
    dirty_repo: Path,
) -> None:
    """The unmeasured reason interpolates the failure, which runs an arbitrary
    `__str__`. That happens while building the argument, so it is outside the
    refusal's own handler and needs one of its own."""
    code, out = run_broken(BREAK_EXC_STR, "git reset --hard", dirty_repo)
    assert code == 0, out
    reason = decision(out)
    assert reason is not None
    assert "could not determine what is at stake" in reason, reason


def test_a_command_that_cannot_be_tokenized_reaches_the_backstop(
    dirty_repo: Path,
) -> None:
    """The tokenizer runs before anything is recognized, and what is unrecognized
    is allowed -- which is right for a payload this hook cannot parse and wrong
    for a command it cannot tokenize. The text is in hand and names a covered
    verb; a parse that raised read no call from it, which is the signature the
    backstop refuses on. Returning instead would skip the backstop and let the
    discard run.
    """
    code, out = run_broken(BREAK_TOKENIZE, "git reset --hard", dirty_repo)
    assert code == 0, out
    assert decision(out) is not None, "a covered verb was allowed after a parse fault"


def test_a_payload_that_cannot_be_parsed_is_still_allowed() -> None:
    """The other half of the same boundary, and it must not move: refusing on a
    payload this hook cannot read refuses every command the harness sends."""
    proc = subprocess.run(
        [sys.executable, "-c", "import block_git_discard.hook as h\nh.main()\n"],
        input="not json at all",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", proc.stdout
