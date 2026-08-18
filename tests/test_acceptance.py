"""The acceptance gate, as tests. See `oracle/README.md` for what it claims.

Marked `acceptance` and excluded from the default run, for the cost
`oracle/README.md` states under "Running them".

Each test shells out to the sweep rather than importing it, so a sweep stays
runnable on its own — the full listing of what was destroyed and what was
refused is what you want when one goes red, and pytest's assertion output is not
that.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from conftest import PROJECT_ROOT

ORACLE = PROJECT_ROOT / "oracle"

pytestmark = pytest.mark.acceptance


def sweep(script: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(ORACLE / script)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        pytest.fail(f"{script} reported failures:\n{proc.stdout}\n{proc.stderr}")


def test_a1_measurement_over_the_flag_surface() -> None:
    """Every command that really destroyed content was refused."""
    sweep("a1_flags.py")


def test_a2_the_parse_over_the_declared_grammar() -> None:
    """Same claim, with the call wrapped in each construction we declare we see."""
    sweep("a2_grammar.py")


def test_a2b_the_backstop_can_still_fire() -> None:
    """`recognized <= mentions`, without which the backstop is structurally mute."""
    sweep("a2b_counting.py")
