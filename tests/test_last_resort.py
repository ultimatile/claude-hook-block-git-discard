"""The handlers that turn an internal fault into a refusal instead of an exit.

No bad command reaches these paths any more, which is why they need tests of
their own: a path reachable only by a bug nobody has found yet stops working
silently. So each test runs the real `main()` in a subprocess with one internal
broken on purpose, and asserts it still exits 0 and still prints a deny.
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
    escapes into a non-zero exit unless something catches it there.
    """
    code, out = run_broken(BREAK_TOKEN, "popd; git reset --hard", dirty_repo)
    assert code == 0, out
    reason = decision(out)
    assert reason is not None
    assert "failed" in reason and "composing the refusal" in reason, reason


def test_the_last_resort_offers_no_token_it_could_not_derive(
    dirty_repo: Path,
) -> None:
    """The hash is what broke, so a token printed anyway would never match."""
    _, out = run_broken(BREAK_TOKEN, "popd; git reset --hard", dirty_repo)
    reason = decision(out)
    assert reason is not None
    assert "No override token is offered" in reason, reason
    assert "ack:" not in reason, reason


def test_a_broken_backstop_count_refuses_rather_than_passes(dirty_repo: Path) -> None:
    """Nothing follows the count, so a fault there is silence, and silence is
    consent.
    """
    code, out = run_broken(BREAK_MENTIONS, "sh -c 'git reset --hard'", dirty_repo)
    assert code == 0, out
    assert decision(out) is not None, out


def test_an_exception_that_cannot_be_rendered_still_refuses(
    dirty_repo: Path,
) -> None:
    """The unmeasured reason interpolates the failure, running an arbitrary
    `__str__` while building the argument — outside the refusal's own handler.
    """
    code, out = run_broken(BREAK_EXC_STR, "git reset --hard", dirty_repo)
    assert code == 0, out
    reason = decision(out)
    assert reason is not None
    assert "could not determine what is at stake" in reason, reason


def test_a_command_that_cannot_be_tokenized_reaches_the_backstop(
    dirty_repo: Path,
) -> None:
    """Unrecognized is allowed, which is right for a payload this hook cannot parse
    and wrong for a command it cannot tokenize: the text is in hand and may name a
    covered verb.
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
