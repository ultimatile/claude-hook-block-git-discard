"""Negative controls: break the hook on purpose and check the sweeps notice.

A green sweep is worth exactly what its ability to go red is worth. Without this
file, "A1 found no fail-opens" and "A1 is not looking" produce identical output.

NOT mutation testing, and the distinction decides where this belongs. Mutation
testing GENERATES mutants mechanically and reports a score; the score is noisy,
equivalent mutants are unavoidable, and no pass/fail line can be drawn across it.
What is here is a fixed, hand-written set of breaks, each naming the sweep that
must catch it -- a control group, deterministic, and read by a person rather than
compared against a threshold. Run it when the hook or a sweep changes, which is
when the answer can change; it is deliberately NOT part of `pytest -m
acceptance`, because its anchors are verbatim lines of `hook.py` and a
whitespace-only edit to one of them reddens it on a correct change.

WHICH sweep catches a mutant is part of the claim, not bookkeeping. A1 sends
well-formed commands and asks whether the measurement behind them is right; A2
wraps the same calls in shell constructions and asks whether they are found at
all. Disabling the backstop is invisible to A1 by construction -- every command
it sends parses cleanly, so the backstop has nothing to do. Point that mutant at
A1 and it survives every real discard A1 produces, which reads as a hole in A1
and is the division of labour working. Naming the owning sweep per mutant is
what keeps the two apart.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "block_git_discard"

# (label, sweep, anchor, replacement, verbs) — `verbs` narrows A1 only.
MUTANTS = [
    (
        "drop `clean` from the covered set",
        "a1",
        'COVERED = frozenset({"checkout", "switch", "restore", "reset", "clean"})',
        'COVERED = frozenset({"checkout", "switch", "restore", "reset"})',
        ["clean"],
    ),
    (
        "`forced` never fires",
        "a1",
        'return "f" in flags or bool(flags & {"--force", "--discard-changes"})',
        "return False",
        ["checkout", "switch"],
    ),
    (
        "a hard reset is read as harmless",
        "a1",
        'return ("worktree", [], False) if "--hard" in flags else None',
        "return None",
        ["reset"],
    ),
    (
        "an untracked-file discard is read as harmless",
        "a1",
        'ignored = "-ignored-only" if "X" in flags else "-all" if "x" in flags else ""',
        'ignored = "-ignored-only" if "X" in flags'
        ' else "-all" if "x" in flags else ""\n'
        "        return None",
        ["clean"],
    ),
    (
        "the backstop never fires",
        "a2",
        "    if unread:",
        "    if False:",
        [],
    ),
    (
        "the backstop counts nothing",
        "a2",
        "    for line in logical_lines(prepared(command, mask=False)):",
        "    for line in []:",
        [],
    ),
    # `NAME_CHAR` is widened rather than narrowed, because widening is the
    # direction that costs: a character wrongly IN the class glues the name to
    # what precedes it and the call goes unread, while one wrongly out of it
    # splits a word and costs a single refusal. The header over `NAME_CHAR` says
    # exactly that, and this is what holds it to it.
    #
    # The obvious alternative -- requiring the name to begin a whitespace word --
    # breaks every row the backstop is the only reader of, `sh -c`, `bash -c` and
    # `eval` among them, which is the set "the backstop never fires" already
    # owns. Widening the class reaches only the rows whose covered call sits
    # INSIDE a substitution, which is the set this reading adds. No count is
    # given for either: `WRAPPERS` is a list that grows, and a number written
    # here has nothing to bring it back into agreement with it.
    (
        "an opening bracket counts as part of the command name",
        "a2",
        r'NAME_CHAR = r"A-Za-z0-9_.+\-"',
        r'NAME_CHAR = r"A-Za-z0-9_.+\-=$("',
        [],
    ),
    (
        "a separator behind the name stops clearing the inert guard",
        "a2b",
        'SEPARATORS = ";&|()`"',
        'SEPARATORS = ";&|"',
        [],
    ),
]

# (script, the lines carrying a violation count, the line carrying the size of
# the space it was counted over). Read out of the sweep's own report rather than
# off its exit status, because a sweep that fell over exits non-zero too --
# "the mutant was caught" and "the sweep crashed" have to stay distinguishable,
# or a broken sweep reads as a working one. The counts differ per sweep because
# what a violation IS differs: A1 and A2 count content that really went, A2b
# counts the two ways its two counters can disagree.
SWEEPS = {
    "a1": ("a1_flags.py", ["FAIL-OPEN"], "actually destroyed"),
    "a2": ("a2_grammar.py", ["FAIL-OPEN"], "actually destroyed"),
    "a2b": (
        "a2b_counting.py",
        ["recognized > mentions", "mentions under the truth"],
        "commands checked",
    ),
}

# A hang backstop, not a performance bound. A1 narrowed to a single verb still
# builds hundreds of repositories, so this sits far above any real run; what it
# rules out is a wedged sweep taking the whole gate with it, which would leave
# the mutant neither caught nor reported.
SWEEP_TIMEOUT = 1800


def sweep(name: str, pkg_parent: Path, verbs: list[str]) -> tuple[str, str]:
    argv = [
        sys.executable,
        "-c",
        f"import sys; sys.path.insert(0, {str(pkg_parent)!r}); "
        "import block_git_discard.hook as h; h.main()",
    ]
    # Two ways of reaching the same broken copy, because the sweeps reach the
    # hook two different ways: A1 and A2 run it as a subprocess, A2b imports it.
    env = {
        **os.environ,
        "ORACLE_HOOK_ARGV": json.dumps(argv),
        "ORACLE_PKG_PARENT": str(pkg_parent),
    }
    if verbs:
        env["ORACLE_VERBS"] = json.dumps(verbs)
    proc = subprocess.run(
        [sys.executable, str(HERE / SWEEPS[name][0])],
        env=env,
        capture_output=True,
        text=True,
        timeout=SWEEP_TIMEOUT,
    )
    return proc.stdout, proc.stderr


def field(out: str, prefix: str) -> str:
    for ln in out.splitlines():
        if ln.strip().startswith(prefix):
            return ln.split(":", 1)[1].strip().split()[0]
    return "?"


def main() -> int:
    unaccounted = 0
    for label, name, anchor, replacement, verbs in MUTANTS:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            shutil.copytree(
                SRC,
                parent / "block_git_discard",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            hook = parent / "block_git_discard" / "hook.py"
            text = hook.read_text()
            if text.count(anchor) != 1:
                print(
                    f"  INCONCLUSIVE [{name}] {label}: "
                    f"anchor matched {text.count(anchor)}x, so nothing was mutated"
                )
                unaccounted += 1
                continue
            hook.write_text(text.replace(anchor, replacement))
            try:
                out, err = sweep(name, parent, verbs)
            except subprocess.TimeoutExpired:
                print(
                    f"  INCONCLUSIVE [{name}] {label}: "
                    f"no verdict — still running after {SWEEP_TIMEOUT}s"
                )
                unaccounted += 1
                continue
            _script, violation_lines, size_line = SWEEPS[name]
            violations = [field(out, prefix) for prefix in violation_lines]
            size = field(out, size_line)
            if "?" in violations:
                tail = (err.strip().splitlines() or ["(no stderr)"])[-1]
                print(f"  INCONCLUSIVE [{name}] {label}: no verdict — {tail}")
                unaccounted += 1
                continue
            caught = any(count != "0" for count in violations)
            print(
                f"  {'CAUGHT      ' if caught else 'MISSED      '}"
                f"[{name}] {label}: violations={'+'.join(violations)} over {size}"
            )
            if not caught:
                unaccounted += 1
    print()
    print(
        "Every mutant was caught by the sweep that owns its layer."
        if not unaccounted
        else f"{unaccounted} mutant(s) unaccounted for."
    )
    return 1 if unaccounted else 0


if __name__ == "__main__":
    sys.exit(main())
