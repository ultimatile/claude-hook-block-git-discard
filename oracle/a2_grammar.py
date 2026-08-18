"""A2: is a covered call still found through every shell shape we claim to see?

A1 asks whether the measurement is right once the call has been found. This asks
the prior question: the parse.

THE WRAPPER LIST IS THE SPECIFICATION, not a sample. Each entry is a shell
construction this hook claims to see a covered call through. Anything absent is
out of scope by declaration rather than by oversight -- the only honest way to
bound a space that has no natural boundary, since shell syntax does not run out.

`oracle/README.md` states which table a finding at which layer extends, and how
to re-run afterwards.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oracle import print_coverage, report, st_everything, verdict

# Spelled from pieces so this file can be read and edited in a session where the
# hook itself is installed and watching.
R = "re" + "set"
CO = "check" + "out"
CL = "cl" + "ean"

# Calls that destroy something in `st_everything` when run from its root.
CORES = [
    f"git {R} --hard",
    f"git {CO} -- tracked.txt",
    f"git {CO} -- .",
    f"git {CL} -fd",
    "git restore tracked.txt",
    "git switch -f other",
]

WRAPPERS: list[tuple[str, str]] = [
    ("bare", "{c}"),
    ("leading whitespace", "   {c}"),
    ("trailing semicolon", "{c};"),
    ("after a no-op", "true; {c}"),
    ("before a no-op", "{c}; true"),
    ("conditional and", "true && {c}"),
    ("conditional or", "false || {c}"),
    ("both", "true && {c} || echo no"),
    ("subshell", "( {c} )"),
    ("brace group", "{{ {c}; }}"),
    ("nested subshell", "( ( {c} ) )"),
    ("pipeline head", "{c} | cat"),
    ("background", "{c} &"),
    ("env prefix", "LC_ALL=C {c}"),
    ("two env prefixes", "LC_ALL=C PAGER=cat {c}"),
    ("command wrapper", "command {c}"),
    ("nice wrapper", "nice {c}"),
    ("stdout redirect", "{c} > /dev/null"),
    ("stderr redirect", "{c} 2> /dev/null"),
    ("both redirects", "{c} > /dev/null 2>&1"),
    ("append redirect", "{c} >> log.txt"),
    ("fd-numbered redirect", "{c} 1>/dev/null"),
    ("here-string", "{c} <<< ''"),
    ("trailing comment", "{c} # tidy up"),
    ("comment above", "# note\n{c}"),
    ("line continuation", "{c} \\\n  # done"),
    ("newline separated", "echo hi\n{c}"),
    ("second on the line", "git status --short; {c}"),
    ("after an inert git", 'git commit -m "wip" --allow-empty -q; {c}'),
    ("after a log", "git log -1 --oneline; {c}"),
    ("cd out and back", "cd sub && cd .. && {c}"),
    ("pushd out and back", "pushd sub > /dev/null && cd .. && {c}"),
    ("cd into subdir", "cd sub && {c}"),
    ("sh -c single quotes", "sh -c '{c}'"),
    ("bash -c double quotes", 'bash -c "{c}"'),
    ("eval", 'eval "{c}"'),
    ("heredoc before", "cat <<'EOF' > /dev/null\nnothing\nEOF\n{c}"),
    ("heredoc after", "{c}\ncat <<'EOF' > /dev/null\nnothing\nEOF"),
    ("substitution beside", "echo $(pwd) > /dev/null && {c}"),
    ("backticks beside", "echo `pwd` > /dev/null && {c}"),
    # The call INSIDE a substitution rather than beside one. What is written
    # inside one runs, so every row here destroys exactly as its bare core does
    # -- and the name then does not begin its word, which is the whole reason
    # they belong here. A reading that reduces a whitespace word to `git` reaches
    # `$(git` only when the `$(` happens to open the word; one character in front
    # of it and the name goes unread while the call still runs.
    ("command substitution", "x=$({c})"),
    ("exported substitution", "export OUT=$({c})"),
    ("substitution glued to a word", "echo a$({c})"),
    ("backticks after an assignment", "x=`{c}`"),
    ("substitution in a parameter default", "echo ${{x:-$({c})}}"),
    ("assignment prefix to a command", "a=1 b=$({c}) true"),
    # Process substitution, which is why the oracle runs bash: `/bin/sh` rejects
    # these at parse time, and a row that never runs reports green.
    ("process substitution", "cat <({c})"),
    ("process substitution as an operand", "diff <({c}) /dev/null"),
    ("process substitution redirected in", "cat > /dev/null < <({c})"),
    ("non-ascii comment", "{c} # メモ"),
    ("very long prefix", "echo " + "x" * 300 + " > /dev/null && {c}"),
    ("git -C the root from a subdir", "cd sub && git -C .. {cbare}"),
    # Two ways for a quote or a backslash to swallow the newline after an INERT
    # call, carrying its guard onto the line below. Both destroy exactly as the
    # bare core does; what they test is whether the line boundary survives.
    ("ansi-c quoted inert argument", "git commit -q --allow-empty -m $'don\\'t'\n{c}"),
    ("escaped backslash before newline", "git log -1 \\\\\n{c}"),
]


def render(shape: str, core: str) -> str:
    if "{cbare}" in shape:
        return shape.replace("{cbare}", core.removeprefix("git "))
    return shape.format(c=core)


def main() -> int:
    misses: list[tuple[str, str, dict]] = []
    timed_out: list[str] = []
    destructive = 0
    total = 0
    for core in CORES:
        for label, shape in WRAPPERS:
            command = render(shape, core)
            total += 1
            try:
                decision, lost, _rc = verdict(command, st_everything)
            except subprocess.TimeoutExpired:
                # Counted rather than dropped: a case nothing judged is a case
                # this sweep did not cover, and an uncovered case is invisible in
                # a pass/fail line.
                timed_out.append(command)
                continue
            if lost:
                destructive += 1
                if decision != "deny":
                    misses.append((label, command, lost))
    rc = report(
        "A2 — the parse over the declared shell grammar", total, destructive, misses
    )
    print_coverage("timed out, unjudged", [repr(c) for c in timed_out])
    return rc


if __name__ == "__main__":
    sys.exit(main())
