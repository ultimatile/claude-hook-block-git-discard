"""The acceptance condition, run rather than described.

One test per cell of the enumeration in `scope_cases`, and the assertion is the rule
that file states: the hook must deny exactly when executing the command destroys
content that existed beforehand. No expected verdict is written anywhere; each cell
runs the command and reads the bytes back.

This runs in the ordinary suite rather than behind a marker. Behind one, whether the
acceptance condition was actually checked becomes unanswerable again, which is the
failure this file exists to end.

The harness's own detector is tested first, below. A loss oracle that cannot report a
loss would pass every cell while checking nothing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import scope_cases
from conftest import VENV_BIN, child_env
from scope_cases import CELLS, DECLARED_OVER_REFUSALS, build, execute, name_of

HOOK = "block-git-discard"


def verdict(command: str, payload: Path) -> tuple[str, str]:
    """(ALLOW | DENY | ERROR, and for a deny whether it named what is at stake)."""
    script = VENV_BIN / HOOK
    if not script.exists():
        pytest.fail(f"{script} is missing -- run `uv sync`.")
    proc = subprocess.run(
        [str(script)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "cwd": str(payload),
                "tool_input": {"command": command},
            }
        ),
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        cwd=str(payload),
    )
    if proc.returncode != 0:
        return "ERROR", proc.stderr[:300]
    if not proc.stdout.strip():
        return "ALLOW", ""
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    return "DENY", "measured" if "At stake" in reason else "blind"


# --- the detector, before anything that relies on it ------------------------


def test_the_loss_oracle_reports_a_removed_file(tmp_path: Path) -> None:
    """A registered file that disappears has to come back as lost."""
    fixture = build(tmp_path / "t", "untracked")
    assert fixture.lost() == []
    victim = next(iter(fixture.at_risk))
    victim.unlink()
    assert any("gone" in entry for entry in fixture.lost())


def test_the_loss_oracle_reports_a_rewritten_file(tmp_path: Path) -> None:
    """And so does one whose bytes are replaced -- a `checkout` restores rather
    than deletes, so a detector watching only for absence would miss every
    tracked-file case in the tables."""
    fixture = build(tmp_path / "t", "tracked")
    assert fixture.lost() == []
    victim = next(iter(fixture.at_risk))
    victim.write_bytes(b"something else entirely\n")
    assert any("changed" in entry for entry in fixture.lost())


def test_the_harness_cannot_reach_outside_its_fixture(tmp_path: Path) -> None:
    """A cell is a real destructive command in a real shell, so where it lands is
    part of the harness's contract rather than a detail.

    `cd` with no operand goes to `$HOME`, and one cell is `cd && git reset --hard`.
    With the ambient value that hard reset runs in the home directory, and on a
    home-as-repository setup it discards the user's own uncommitted work -- running
    the suite would cause the exact loss this project exists to prevent. This pins
    the redirection that stops it, because nothing else about the suite would go
    red if it were removed.
    """
    fixture = build(tmp_path / "cell", "tracked")
    execute("cd && pwd > landed.txt", fixture.payload)
    landed = Path((fixture.payload / "landed.txt").read_text().strip())
    assert landed.resolve() == fixture.payload.resolve()


def test_every_declared_over_refusal_names_a_cell() -> None:
    """A declaration for a cell that does not exist quietly stops meaning anything,
    and is the way a stale exception outlives the shape it excused."""
    known = {name_of(axis, label) for axis, label, _, _ in CELLS}
    assert known >= DECLARED_OVER_REFUSALS.keys(), DECLARED_OVER_REFUSALS.keys() - known


def test_the_enumeration_has_no_duplicate_cells() -> None:
    """Two cells under one name would let one hide the other's result."""
    names = [name_of(axis, label) for axis, label, _, _ in CELLS]
    assert len(names) == len(set(names)), [n for n in names if names.count(n) > 1]


# --- the enumeration -------------------------------------------------------


@pytest.mark.parametrize(
    ("axis", "label", "template", "kind"),
    CELLS,
    ids=[f"{axis}-{label}" for axis, label, _, _ in CELLS],
)
def test_the_hook_denies_exactly_what_destroys_content(
    tmp_path: Path, axis: str, label: str, template: str, kind: str
) -> None:
    fixture = build(tmp_path / "cell", kind)
    command = template.replace("{other}", str(fixture.root / "other"))
    # Registered rather than merely written: these are untracked files, so a `clean`
    # that removes them HAS destroyed content, and leaving them out of the at-risk
    # set turns a correct refusal into a reported over-refusal.
    fixture.risk(fixture.payload / "activate", b"export X=1\n")
    fixture.risk(fixture.payload / "log.txt", b"nothing\n")

    said, shade = verdict(command, fixture.payload)
    assert said != "ERROR", (
        f"the hook exited non-zero, which the harness allows: {shade}"
    )

    execute(command, fixture.payload)
    lost = fixture.lost()
    name = name_of(axis, label)

    if lost:
        assert said == "DENY", (
            f"{name}: running `{command}` destroyed {lost}, and the hook allowed it"
        )
    elif said == "DENY":
        assert name in DECLARED_OVER_REFUSALS, (
            f"{name}: `{command}` destroyed nothing, and the hook refused it. "
            f"Either the refusal is wrong or the shape belongs in "
            f"DECLARED_OVER_REFUSALS, with its reason beside it there."
        )


def test_every_declared_over_refusal_is_needed(tmp_path: Path) -> None:
    """An entry only does work when its cell destroys nothing.

    With a loss the rule demands a deny outright, so the exemption never gates
    anything and the entry is dead — present, reviewable, and inert. Twelve entries
    were in that state before this test existed, which is how many a list checked
    only for live names can accumulate: `test_every_declared_over_refusal_names_a_cell`
    holds the name, and nothing held the exemption.
    """
    by_name = {name_of(axis, label): (t, k) for axis, label, t, k in CELLS}
    dead = []
    for entry in sorted(DECLARED_OVER_REFUSALS):
        template, kind = by_name[entry]
        fixture = build(tmp_path / entry.replace(":", "").replace(" ", "-"), kind)
        command = template.replace("{other}", str(fixture.root / "other"))
        fixture.risk(fixture.payload / "activate", b"export X=1\n")
        fixture.risk(fixture.payload / "log.txt", b"nothing\n")
        execute(command, fixture.payload)
        if fixture.lost():
            dead.append(f"{entry}: `{command}` destroys {fixture.lost()}")
    assert not dead, (
        "these entries exempt a refusal the rule already demands, so they gate "
        "nothing:\n  " + "\n  ".join(dead)
    )


def test_every_declared_over_refusal_states_a_reason() -> None:
    """The reason is what makes an exemption reviewable, and it lives beside the
    entry because there is nowhere else that holds one."""
    missing = [k for k, v in DECLARED_OVER_REFUSALS.items() if not v.strip()]
    assert not missing, missing


def test_the_enumeration_covers_every_axis() -> None:
    """An axis that loses its last cell leaves the sweep it names unchecked."""
    axes = {axis for axis, _, _, _ in CELLS}
    assert axes == set("ABCDEFGHJ"), axes.symmetric_difference(set("ABCDEFGHJ"))
    assert scope_cases.CASE_SHELL.endswith("bash")
