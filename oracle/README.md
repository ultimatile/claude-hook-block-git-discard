# The acceptance condition

These sweeps exist because "reviewers stopped finding things" is not a place this
hook can arrive at.

The hook reads shell text from outside the shell and decides whether a git
command will destroy content. Both halves of that admit an unbounded supply of
findings: shell syntax does not run out, and neither does prose making claims
about behaviour. Over an unbounded space, "nobody found anything this time" is a
fact about how long the looking went on, not about what is left to find — and a
fix aimed at one finding is itself new text in that same space.

So the standard is not "no findings". It is these sweeps, and the property they
check is one sentence:

> Whenever running the command for real leaves content that is in neither the
> worktree nor the object store, the hook must have refused.

That loss — content the object store never held, now gone from the worktree — is
the only thing this hook guards. It has no reflog entry, no dangling blob, and
nothing for `git fsck` to reach.

## The sweeps

| | asks | bounded by |
| --- | --- | --- |
| `a1_flags.py` | once the call is found, is the measurement right? | git's own CLI surface for five verbs, read from `git <verb> -h` at run time |
| `a2_grammar.py` | is the call found at all? | the wrapper list in that file, which is a **declaration** |
| `a2b_counting.py` | can the backstop still fire? | the hook's own two counters |

`a1` and `a2` are both oracle-backed: they run the command in a throwaway
repository and compare content before and after. That is not ceremony — an
assertion with no oracle here reports the hook being *right* as a failure, and
`oracle.py`'s module docstring works the example.

`a2b` needs no oracle, because its property is about the hook's own counters and
not about git.

`negative_controls.py` is what makes the three above mean anything: without it,
"A1 found no fail-opens" and "A1 is not looking" print the same thing. It is
**not** part of the gate, and the reason is in its own header — it is a control
group read by a person, not a score with a pass mark, and its anchors are
verbatim lines of `hook.py`, so a whitespace-only edit reddens it on a change
that is correct. Run it when the hook or a sweep changes:

```sh
uv run python oracle/negative_controls.py
```

## Running them

```sh
uv run pytest -m acceptance      # all three, as tests
uv run python oracle/a1_flags.py # or individually, for the full listing
```

They are excluded from the default `uv run pytest`, which stays a fast check of
the unit suite. A1 alone builds a throwaway repository for every case in its
matrix and runs a real git command in each; folded into the default suite it
would make that suite something nobody runs.

## What is NOT covered

Stating this is part of the condition, because a bound nobody wrote down is
indistinguishable from a bound nobody noticed.

- **Shell constructions outside `WRAPPERS`.** Out of scope by declaration. The
  hook's own guarantee is narrower than "never under-refuses": a name that
  resolves to `git` only at run time (`$GIT reset --hard`), or a verb that does
  (a `git co` alias), is invisible to it and to these sweeps alike.
- **A flag whose value placeholder has no sample in `VALUES`.** A1 builds
  value-taking flags out of git's own usage output, substituting a sample for the
  placeholder; one with no sample cannot be built. Every skipped spelling is
  printed on the run's `spellings not built` line, so that line is the bound —
  read it off the run rather than off a list written here, which is the whole
  reason it prints on green runs too.
- **Prose.** No sweep here reads a comment or a deny message. The claims the hook
  makes about itself, and the instructions it gives a denied agent, are left to
  review and to the unit suite — so a false claim in either can stand while every
  sweep here reports green.
- **Repository configuration no fixture sets** — `assume-unchanged`,
  `skip-worktree`, a lossy clean filter. This is not a limit of the oracle: it
  hashes the worktree itself and asks `git cat-file -e` whether the object store
  holds the result, so content such a setting hides from `git diff` is still
  content it would report GONE. The gap is in `STATES`, which builds no
  repository configured that way, and closing it means adding a state rather than
  changing how loss is measured.
- **Constructions only a shell other than bash parses.** `a1` and `a2` run each
  case under bash, because that is the grammar `WRAPPERS` is written in: under
  `/bin/sh` a row holding `cat <(git reset --hard)` is a parse error, so the
  command never runs, nothing is destroyed, and the row reports green having
  measured nothing. A shape that only some other shell accepts is neither run
  nor claimed here.

### Known fail-opens, left open on purpose

The bullets above are bounds on what is asked. These two are cases where the
answer is already known and is wrong — the hook allows a command that destroys
content — and they are named here so that the sweeps reporting green is not read
as covering them. Both are filed, with reproductions and acceptance conditions,
and are follow-up work rather than unknowns.

- **A narrowed checkout or restore from a named tree-ish**
  ([#2](https://github.com/ultimatile/claude-hook-block-git-discard/issues/2)).
  `git checkout other -- shared.txt` overwrites the worktree file with the
  version `other` holds, and an untracked or ignored file there is in neither the
  index nor the object store. The `worktree` measurement comes from
  `git diff --name-only`, which cannot report a path git has never seen, so the
  command runs unmeasured. No sweep here builds a repository where a source tree
  and the worktree hold the same path, so no row can go red on it.
- **A configured `diff.external` or `textconv` driver**
  ([#3](https://github.com/ultimatile/claude-hook-block-git-discard/issues/3)).
  Every measurement runs `git diff`, which honours both: a slow external diff
  pushes a decision past the point where anyone is still waiting for it, and one
  that prints nothing empties the patch the `worktree` fingerprint is computed
  from, so an override token stays valid across the content change its binding
  exists to invalidate. The sweeps set no such configuration, and each case asks
  the hook once, so neither could observe the token binding in any case.

## How to spend a review finding

Every sweep's bound is a hand-written table, and a finding is spent by extending
the one that owns its layer — not by patching the parser for that one shape:

| the finding | the table to extend |
| --- | --- |
| a shell construction the hook misses | `a2_grammar.WRAPPERS` |
| a line whose true count of covered calls the backstop reads wrong | `a2b_counting.DECLARED` |
| a flag spelling the matrix never builds | `a1_flags.COMBOS`, or `VALUES` for its placeholder |
| a break none of the above would notice | `negative_controls.MUTANTS` |

Then re-run the whole gate, not only the sweep you extended — a row added to one
table can change what another sweep is asked:

```sh
uv run pytest -m acceptance
```

The difference is what survives. A row in a declaration is a check that keeps
holding as the parser changes; a patched special case is a claim that held once,
on the day someone thought of it, and nothing afterwards re-asks it.
