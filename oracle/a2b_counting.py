"""A2b: the counting comparison that makes the backstop mean anything.

`main` ends on `if mentions(command) > recognized: refuse`. That test is the
entire reason the shell parsing upstream is allowed to stay shallow: a hole in
the real parser lowers `recognized`, trips this, and comes out as a false
positive rather than a fail-open.

TWO PROPERTIES, and the weaker one alone leaves the sweep able to pass on a
destroyed tree.

  1. `recognized <= mentions`. Necessary: one over-count on the recognized side
     absorbs an unread call beside it and the backstop goes quiet.
  2. `mentions >= the number of covered calls that will actually run`. This is
     the one that matters, and totals-agree does not imply it. A score of (1, 1)
     is what a fail-open of this shape looks like: `git -c user.name="John Doe"
     reset --hard; sh -c 'git clean -fdx'` runs two covered calls, and the quoted
     value tears the line exactly where a reading that reduces whitespace words
     looks for the verb. First call recognized and measured (nothing at stake, so
     no refusal), second neither recognized nor counted, totals equal, backstop
     silent, tree destroyed. Only the declared count below can tell that apart
     from a line that really holds one call.

The true count cannot be derived -- it is what a shell would do -- so it is
declared per case below. That makes this table the same kind of artifact as
`a2_grammar.WRAPPERS`: a specification, extended by adding a row.

The cases run against a REAL repository, and that is load-bearing rather than
tidy. Run against `/tmp`, which is in no repository, every covered call fails to
measure and refuses on the spot, so `main` returns after the first one and
`recognized` can never exceed 1 -- leaving the sweep unable to observe the
over-count it exists to detect.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Where the package under test is imported from. `negative_controls.py` points this at a
# deliberately broken copy, the way `ORACLE_HOOK_ARGV` points the other two
# sweeps at one. This sweep imports the hook rather than shelling out to it, so
# without an override it would load the real module whatever was mutated -- and
# would then report green on every mutant, which is indistinguishable from a
# sweep that is not looking.
sys.path.insert(
    0,
    os.environ.get("ORACLE_PKG_PARENT", str(Path(__file__).resolve().parent.parent)),
)

import a2_grammar as g

import block_git_discard.hook as h
from oracle import st_everything

# Taken from `a2_grammar`, which this file already imports for `CORES` and
# `WRAPPERS`. They are spelled from pieces there for the reason stated there:
# so the file can be edited in a session where the hook is installed and
# watching.
R, CO, CL = g.R, g.CO, g.CL

# (command, covered calls a shell would actually run). Zero is a real answer:
# text that merely NAMES a verb runs nothing.
DECLARED: list[tuple[str, int]] = [
    (f"git {R} --hard", 1),
    (f"git {R} --hard; git {R} --hard", 2),
    (f"git {R} --hard; git {R} --hard; git {R} --hard", 3),
    (f"git {CO} -- a && git {CL} -fd", 2),
    (f"sh -c 'git {R} --hard'", 1),
    (f'eval "git {CL} -fdx"', 1),
    # The quoting shapes that tore `mentions` apart. Each runs two covered calls.
    (f"git -c user.name=\"John Doe\" {R} --hard; sh -c 'git {CL} -fdx'", 2),
    (f"git -c user.name='John Doe' {R} --hard; sh -c 'git {CL} -fdx'", 2),
    (f"git -C 'a dir' {R} --hard; sh -c 'git {CL} -fdx'", 2),
    (f"\\git {R} --hard; sh -c 'git {CL} -fdx'", 2),
    (f'git -c core.editor="vim -f" {R} --hard', 1),
    # An escaped apostrophe inside an inert subcommand's argument. One covered
    # call runs: the `commit` is inert, the `clean` on the next line is not.
    (f"git commit -q --allow-empty -m don\\'t\nsh -c 'git {CL} -fdx'", 1),
    (f"git commit -q --allow-empty -m \"don't\"\nsh -c 'git {CL} -fdx'", 1),
    # The call written INSIDE a substitution. The name does not begin its word,
    # and the real parse masks the substitution away, so `mentions` is the only
    # counter that can see any of these -- the comparison in `main` is `0 > 0`
    # the moment it stops reading them, and the line runs.
    (f"x=$(git {R} --hard)", 1),
    (f"export OUT=$(git {CL} -fdx)", 1),
    (f"echo a$(git {R} --hard)", 1),
    (f"cat <(git {R} --hard)", 1),
    # A substitution inside an INERT subcommand's argument. `commit` does not run
    # its arguments, but what is written inside the substitution runs before the
    # commit has begun, so the `(` has to clear the inert guard even though it
    # sits mid-word. Separated by `;` instead, this is the row above it.
    (f"git commit -q --allow-empty -m x$(git {R} --hard)", 1),
    # A quote or a backslash that must NOT swallow the newline after an inert
    # call. One covered call runs on the second line; the `commit` and the `log`
    # are inert.
    (f"git commit -q --allow-empty -m $'don\\'t'\nsh -c 'git {CL} -fdx'", 1),
    (f"git log -1 \\\\\nsh -c 'git {CL} -fdx'", 1),
    # Text that names a verb without running one.
    (f"echo git {R} --hard", 0),
    (f'git commit -m "git {CL} -fdx" --allow-empty -q', 0),
    (f"man git {CO}", 0),
    (f"grep 'git {CO} -- a.txt' log.txt", 0),
    (f"git bisect {R}", 0),
    (f"git tag -d {CO}", 0),
    (f"git log -S 'git {CO}'", 0),
]


def counts(command: str, cwd: str) -> tuple[int, int]:
    """(recognized, mentions) for one command, against a real repository."""
    seen = 0
    original = h.covered_verb

    def counting(argv):
        nonlocal seen
        got = original(argv)
        if got is not None:
            seen += 1
        return got

    h.covered_verb = counting
    stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(
            json.dumps({"tool_input": {"command": command}, "cwd": cwd})
        )
        with redirect_stdout(io.StringIO()):
            h.main()
    finally:
        sys.stdin = stdin
        h.covered_verb = original
    return seen, h.mentions(command)


def main() -> int:
    over: list[tuple[str, str]] = []
    under: list[tuple[str, str]] = []

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        st_everything(repo)
        (repo / "a dir").mkdir(exist_ok=True)

        corpus = [g.render(shape, core) for core in g.CORES for _, shape in g.WRAPPERS]
        for command in corpus:
            try:
                recognized, mentioned = counts(command, str(repo))
            except BaseException as exc:  # noqa: BLE001 -- the hook must not raise
                over.append((command, f"raised {type(exc).__name__}: {exc}"))
                continue
            if recognized > mentioned:
                over.append(
                    (command, f"recognized={recognized} > mentions={mentioned}")
                )

        for command, expected in DECLARED:
            try:
                recognized, mentioned = counts(command, str(repo))
            except BaseException as exc:  # noqa: BLE001
                over.append((command, f"raised {type(exc).__name__}: {exc}"))
                continue
            if recognized > mentioned:
                over.append(
                    (command, f"recognized={recognized} > mentions={mentioned}")
                )
            if mentioned < expected:
                under.append(
                    (command, f"mentions={mentioned} < {expected} calls that run")
                )

    checked = len(corpus) + len(DECLARED)
    print("A2b — the backstop can still fire")
    print(f"  commands checked        : {checked}")
    print(f"  recognized > mentions   : {len(over)}")
    print(f"  mentions under the truth: {len(under)}")
    print()
    for command, why in over + under:
        print(f"  VIOLATION  {why}")
        print(f"             {command!r}")
    return 1 if (over or under) else 0


if __name__ == "__main__":
    sys.exit(main())
