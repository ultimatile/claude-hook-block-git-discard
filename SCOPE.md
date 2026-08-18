# Declared scope

The README states the guarantee this hook holds to:

> No shape it can read is under-refused, and a shape it cannot read is refused
> rather than passed.

That sentence quantifies over "shape it can read". Until that set is written down
the guarantee cannot be checked, and every newly imagined command arrives as the
goal moving rather than as a defect against a fixed target.

**The set lives in [`tests/scope_cases.py`](tests/scope_cases.py), as code.** This
file narrates the axes and the rule; it does not repeat the cells. A described
enumeration and a running one drift apart, and the described half is the one that
rots — so there is one enumeration, it is executable, and it is in the repository
where a reviewer can audit it and re-run it.

The set is **frozen**. A shape outside it is not a defect in this hook: it is a
scope extension, which is an issue and a deliberate later change, or a declared
exclusion, which is listed at the end of this file. Extending the enumeration is a
decision made on purpose, never a reaction to what a review round happened to find.

## How a cell's expected answer is decided

Not by judgement, and not by anything written down. Each cell builds a throwaway
repository whose at-risk bytes exist in no git object, asks the hook, **runs the
command for real**, and reads the bytes back:

- executing it destroys content that existed beforehand → the hook must **deny**
- executing it destroys nothing → the hook must **allow**, unless the shape is in
  `DECLARED_OVER_REFUSALS`, where a deny is the accepted answer

Content means content no git object holds: a tracked file's uncommitted change, an
untracked or ignored file, a repository nested inside the worktree. Anything still
reachable from a ref afterwards was not destroyed, which is why `stash` and a plain
`reset` are outside the tables.

Two things the rule deliberately does not grade, because neither is a loss and both
are the rest of the suite's business: whether a refusal names what is at stake, and
how its reason is worded.

## Running it

```sh
uv run pytest tests/test_scope.py
```

It is part of the ordinary suite — around 15 seconds for the whole enumeration —
rather than behind a marker. Behind one, whether the acceptance condition was
actually checked becomes unanswerable, which is the thing this arrangement exists
to end.

### Evidence it can fail

A check whose failure has never been observed is indistinguishable from no check.
Against the tip from before the three commits that closed the fail-opens it
reports **16 failures**; against the current tip, none. To reproduce:

```sh
git worktree add --detach /tmp/pre-fix 46623a3
cp tests/scope_cases.py tests/test_scope.py /tmp/pre-fix/tests/
cd /tmp/pre-fix && uv sync --frozen && uv run pytest -q tests/test_scope.py
```

The harness's own loss detector is tested directly in `tests/test_scope.py`, and
the consistency of the declaration is too: a `DECLARED_OVER_REFUSALS` entry naming
no cell fails the suite, so a stale exception cannot outlive the shape it excused.

## The axes

Each is swept against a baseline rather than crossed with the others, so the
enumeration is linear in the number of values. Pure per-axis sweeps would miss what
coupling in the code produces, so axis J enumerates the pairs that are actually
coupled — which is where every fail-open found so far has lived. **Interactions not
in J are outside the frozen set.**

| | axis | what it varies |
| --- | --- | --- |
| A | which command is a covered call | the five verbs, and the ways a name is or is not `git` — read by its neighbours, so one beginning no word still counts |
| B | forms that destroy nothing | dry runs, interactive selection, the staged-only and non-`--hard` spellings, branch creation |
| C | quoting and escaping | the three quoting styles, a backslash in each, and names holding characters git or the shell spell differently |
| D | separators and control flow | every separator, subshells, runs of punctuation that arrive glued, and the stages whose shell exits first |
| E | where the command runs | the directory-moving builtins and their wrappers, `git -C`, a target not yet created, a directory in no repository |
| F | git's own options | `-C`, `-c`, the relocating globals, an unrecognized one, and `GIT_*` riding in the line |
| G | what `clean` reaches | the force count, `-d`, `-x`, `-X`, `-e`, a pathspec or none, and the force waiver in argv |
| H | which paths the measurement covers | operands either side of `--`, a ref where a path could be, magic pathspecs, one the shell has still to expand |
| I | the override token | binding to the command and to the content, across a rewrite, a changed file set, a subdirectory, two verbs on one line |
| J | the coupled pairs | force count × reach, pathspec × the directory collapse, quoting × pathspec, escaping × the comment scan, wrapper × unreadable construct, non-repository × relocating global, subshell × movement, quoted name × fingerprint |

Axis I's cells need two invocations each and live in `tests/test_hook.py` with the
other token round-trips rather than in the single-invocation table.

## Declared exclusions

Outside the axes on purpose. A loss reached through one of these is not a defect in
this hook:

- plumbing that writes the worktree: `read-tree -u`, `checkout-index -f`
- `git rm -f`, and sequencer aborts
- `git stash` — everything it touches stays reachable from a ref
- a name that resolves to `git` only at run time: `$GIT`, an alias, a shell function
- `source` and `.`, which execute a file this hook does not read, so a `cd` inside
  one is not followed
- `clean.requireForce=false` set in a config FILE rather than in the command
- a configured `diff.external` (issue #3)
- a narrowed checkout or restore from a named tree-ish destroying an untracked or
  ignored file (issue #2)

## Accepted over-refusals

A deny is the correct answer for these even though they destroy nothing, because
telling them apart from a real call is the parsing this hook declines to attempt.
The list is `DECLARED_OVER_REFUSALS` in `tests/scope_cases.py` — in code, so it is
checked rather than believed — and the README carries the reasoning for each.
