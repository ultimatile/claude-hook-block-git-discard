"""A1: does the hook refuse every command that actually destroys content?

The claim is about MEASUREMENT. Given a command the parser reads perfectly, does
the hook work out what is at stake correctly enough to refuse when it should?

The space is bounded, and that is why this sweep can finish. It is indexed by
git's own CLI surface for five verbs -- read off `git <verb> -h` at run time
rather than written down, so it follows the installed git -- crossed with whether
a pathspec is present and with the kinds of content a tree can hold. Nothing
about it depends on anyone thinking of the right case.

What it does NOT cover is the parse. Every command here is well-formed, so the
backstop never has anything to do; disabling it entirely leaves this sweep green.
That is `a2_grammar.py`'s layer, and `negative_controls.py` is what keeps the boundary
honest.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oracle import STATES, git, print_coverage, report, verdict

VERBS = json.loads(
    os.environ.get(
        "ORACLE_VERBS", '["checkout", "switch", "restore", "reset", "clean"]'
    )
)


# Sample values for the placeholders git's usage output uses, so that a flag
# taking one can still be built into a command.
#
# Supplying a value is per-flag work, and skipping every value-taking option
# instead is the cheap alternative -- a silent one, which is what makes it worse
# than the work. `--orphan <new-branch>` goes with such a skip, and
# `git checkout -f --orphan fresh` destroys an entire worktree, so the sweep
# reports green over every case it does build while never generating the one that
# would go red. A skipped spelling is indistinguishable from a covered one in the
# report; that is the whole hazard, and this table is the fix.
VALUES = {
    "<style>": "merge",
    "<new-branch>": "fresh",
    "<branch>": "fresh",
    "<tree-ish>": "HEAD~1",
    "<commit>": "HEAD~1",
    "<pattern>": "keep",
    "<file>": "pathspecs.txt",
}

# git's usage lines come in three shapes, and all three have to be read:
#
#     -f, --[no-]force      forced checkout
#         --[no-]orphan <new-branch>
#     -d                    remove whole directories
#
# The third is the one that bites. `clean`'s `-d`, `-x` and `-X` have no long
# spelling at all, so a pattern requiring `--` drops exactly the flags that make
# `clean` recurse and reach ignored files -- a larger hole in the sweep than any
# this parser closes. Both halves of the alternation are therefore optional.
USAGE_OPTION = re.compile(
    r"^\s+(?:-([a-zA-Z0-9])(?:,\s*)?)?(?:--(?:\[no-\])?([a-z][a-z0-9-]*))?"
)


def flags_for(verb: str) -> tuple[list[str], list[str]]:
    """(spellings this sweep can build, placeholders it could not).

    The second half is returned rather than discarded. A bound nobody wrote down
    cannot be told apart from a bound nobody noticed, and that is not a
    hypothetical here: it is how the `--orphan` fail-open stayed invisible.

    SHORT flags are collected too, and not for symmetry: `clean`'s destructive
    axes are `-d` and `-x`, which have no long spelling at all. A long-only sweep
    reports four options for `clean` and never once recurses into a directory.
    """
    out = git(Path("/tmp"), verb, "-h", check=False)
    built: list[str] = []
    unbuildable: list[str] = []
    for line in (out.stdout + out.stderr).splitlines():
        m = USAGE_OPTION.match(line)
        if not m:
            continue
        short, long = m.group(1), m.group(2)
        if not short and not long:
            continue
        bare = [
            s for s in (f"--{long}" if long else "", f"-{short}" if short else "") if s
        ]
        rest = line[m.end() :]
        placeholder = re.match(r"\s*\[?=?\s*(<[a-z-]+>|\([^)]*\))", rest)
        if not placeholder or rest.lstrip().startswith("["):
            # No value, or an OPTIONAL one -- either way the bare flag is a
            # spelling in its own right.
            for s in bare:
                if s not in built:
                    built.append(s)
            continue
        token = placeholder.group(1)
        if token in VALUES:
            for s in bare:
                spelled = f"{s} {VALUES[token]}"
                if spelled not in built:
                    built.append(spelled)
        else:
            for s in bare:
                if f"{s} {token}" not in unbuildable:
                    unbuildable.append(f"{s} {token}")
    return built, unbuildable


# Spellings a single-flag sweep cannot reach, because the combination is what
# touches the worktree and no member of it does so alone. `clean` destroys
# nothing without `-f` and reaches no directory without `-d`; a forced checkout
# that also creates a branch destroys, while `switch -C` -- which reads the same
# way -- does not.
COMBOS = {
    "clean": ["-f", "-fd", "-fdx", "-fdX", "-fx", "-fX", "-n", "-nd", "-fd -e keep"],
    "checkout": ["-f", "-f -b fresh", "-B other", "-f other", "other", "HEAD~1"],
    "switch": [
        "-f other",
        "-C fresh",
        "--discard-changes other",
        "other",
        "-f -c fresh",
    ],
    "restore": ["--staged --worktree", "--source HEAD~1", "-s HEAD~1 -SW"],
    "reset": [
        "--hard",
        "--hard HEAD~1",
        "--merge HEAD~1",
        "--keep HEAD~1",
        "-q --hard",
    ],
}


def matrix() -> tuple[list[tuple[str, str, str]], list[str]]:
    """((label, command, state) for every case, spellings that could not be built)."""
    cases: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    for verb in VERBS:
        built, unbuildable = flags_for(verb)
        skipped += [f"{verb} {u}" for u in unbuildable]
        options = [""] + built + COMBOS.get(verb, [])
        if verb == "switch":
            pathspecs = [""]  # switch takes no pathspec
        elif verb in ("reset", "clean"):
            pathspecs = ["", "-- tracked.txt"]
        else:
            pathspecs = ["", "-- tracked.txt", "-- sub", "-- ."]
        for opt in options:
            for ps in pathspecs:
                command = " ".join(x for x in ("git", verb, opt, ps) if x)
                for state in STATES:
                    cases.append((f"{verb}/{state}", command, state))
    return cases, skipped


def main() -> int:
    cases, skipped = matrix()
    misses: list[tuple[str, str, dict]] = []
    timed_out: list[str] = []
    destructive = 0
    for i, (label, command, state) in enumerate(cases):
        if i and i % 200 == 0:
            print(f"  .. {i}/{len(cases)}", file=sys.stderr)
        try:
            decision, lost, _rc = verdict(command, STATES[state])
        except subprocess.TimeoutExpired:
            # An interactive spelling with no terminal to prompt at, so far. That
            # reading is not checked per case, and a case nothing judged is a
            # case this sweep did not cover -- the same hazard the line below
            # prints for, so it is counted the same way rather than dropped.
            timed_out.append(command)
            continue
        if lost:
            destructive += 1
            if decision != "deny":
                misses.append((label, command, lost))
    rc = report(
        "A1 — measurement over git's flag surface", len(cases), destructive, misses
    )
    # Printed on every run, green or red. Coverage this sweep did not attempt is
    # the half of its result a pass/fail line cannot carry, and leaving it unsaid
    # is how a spelling that destroys the whole worktree stays out of the matrix
    # with the report still reading green.
    print_coverage("spellings not built", skipped)
    print_coverage("timed out, unjudged", [repr(c) for c in timed_out])
    return rc


if __name__ == "__main__":
    sys.exit(main())
