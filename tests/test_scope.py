"""The acceptance condition, run rather than described.

One test per cell of the enumeration in `scope_cases`, asserting the rule that
file states. No expected verdict is written anywhere: each cell runs the command
and reads the bytes back. It runs in the ordinary suite, since behind a marker
whether the condition was checked becomes unanswerable again.

The harness's own detector is tested first, a loss oracle that cannot report a
loss passing every cell while checking nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import scope_cases
from conftest import VENV_BIN, run_hook_process
from scope_cases import CELLS, DECLARED_OVER_REFUSALS, build, execute, name_of

HOOK = "block-git-discard"


def verdict(command: str, payload: Path) -> tuple[str, str]:
    """(allow | deny | error, and for a deny whether it named what is at stake)."""
    script = VENV_BIN / HOOK
    if not script.exists():
        pytest.fail(f"{script} is missing -- run `uv sync`.")
    proc = run_hook_process([str(script)], command, payload, cwd=payload)
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
    """A `checkout` restores rather than deletes, so a detector watching only for
    absence would miss every tracked-file case.
    """
    fixture = build(tmp_path / "t", "tracked")
    assert fixture.lost() == []
    victim = next(iter(fixture.at_risk))
    victim.write_bytes(b"something else entirely\n")
    assert any("changed" in entry for entry in fixture.lost())


def test_the_harness_cannot_reach_outside_its_fixture(tmp_path: Path) -> None:
    """A bare `cd` goes to `$HOME`, and one cell is `cd && git reset --hard`: with
    the ambient value, on a home-as-repository setup, running the suite would
    discard the user's own uncommitted work.

    Removing the redirection also turns two cells red, but as verdict mismatches.
    This one names where the command landed.
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
    # that removes them has destroyed content, and leaving them out of the at-risk
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
    """An entry does work only when its cell destroys nothing and is refused. With a
    loss the rule already demands a deny; with an allow there is no refusal to
    exempt — and that half is what a list checked only for live names cannot see.
    """
    by_name = {name_of(axis, label): (t, k) for axis, label, t, k in CELLS}
    dead = []
    for entry in sorted(DECLARED_OVER_REFUSALS):
        template, kind = by_name[entry]
        fixture = build(tmp_path / entry.replace(":", "").replace(" ", "-"), kind)
        command = template.replace("{other}", str(fixture.root / "other"))
        fixture.risk(fixture.payload / "activate", b"export X=1\n")
        fixture.risk(fixture.payload / "log.txt", b"nothing\n")
        said, _ = verdict(command, fixture.payload)
        execute(command, fixture.payload)
        if fixture.lost():
            dead.append(f"{entry}: `{command}` destroys {fixture.lost()}")
        elif said != "DENY":
            dead.append(f"{entry}: `{command}` is not refused; the hook said {said}")
    assert not dead, (
        "these entries exempt nothing, because the rule already demands the "
        "answer the hook gives:\n  " + "\n  ".join(dead)
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
