# Declared scope

The README states the guarantee this hook holds to:

> No shape it can read is under-refused, and a shape it cannot read is refused
> rather than passed.

That sentence quantifies over "shape it can read", and until that set is written
down the guarantee cannot be checked — every newly imagined command reads as the
goal moving rather than as a defect against a fixed target. This file is that set,
and it is **frozen**: a shape outside it is not a defect in this hook. It is either
a scope extension, which is an issue and a later change, or a declared exclusion,
which belongs in the list at the end of this file.

## How a cell's expected answer is decided

Not by judgement, and not by anything written here. For every cell of the
enumeration below the answer comes from **running the command in a throwaway
repository and comparing the content before and after**:

- executing it destroys content that existed beforehand → the hook must **deny**
- executing it destroys nothing → the hook must **allow**, unless the shape is on
  the declared over-refusal list, where a deny is the accepted answer

Content means content no git object holds: a tracked file's uncommitted change, an
untracked or ignored file, the whole of a repository nested inside the worktree.
Anything reachable from a ref afterwards was not destroyed, which is why `stash`
and a plain `reset` are outside these tables.

Two things this rule deliberately does not grade, because neither is a loss and
both are the unit suite's business: whether a refusal names what is at stake
(measured versus blind), and how the reason is worded.

## The axes

Each axis is swept against a fixed baseline rather than crossed with every other,
so the enumeration is linear in the number of values and not exponential. Pure
per-axis sweeps would miss what coupling in the code produces, so the pairs that
are actually coupled are enumerated too, and they are listed as their own axis.
**Interactions not named in axis J are outside the frozen set.**

### A. Which command is a covered call

The five verbs `checkout`, `switch`, `restore`, `reset`, `clean`. The name is read
by its neighbours, so a `git` beginning no word still counts: `x=$(git …)`,
`cat <(git …)`, backticks. A name that only becomes `git` when the shell expands it
(`$GIT`, an alias, a shell function) is outside — see exclusions.

### B. Forms that destroy nothing

`-n` / `--dry-run`, `-p` / `--patch`, `restore --staged` without `--worktree`,
`reset` without `--hard`, branch creation (`-b`, `-B`, `--orphan`) without force,
an unforced `switch`.

### C. Quoting and escaping

Single, double and `$'…'` (ANSI-C) quoting; a backslash escaping outside quotes and
inside double quotes, and taken literally inside single ones. Names holding a space,
`"`, `\`, a tab, a newline, a `#`, a non-ASCII byte, or a ` 2>` sequence. A `#` at a
word's start, mid-word, after an escaped space, and inside quotes. A line
continuation, and an escaped backslash before a newline.

### D. Separators and control flow

`;`, `&&`, `||`, `|`, `&`, a newline; a subshell `( )`; separators that arrive glued
into one run (`)&&`, `&&(`); a pipeline stage or a backgrounded command, whose shell
has exited by the time the git call runs.

### E. Where the command runs

`cd`, `pushd`, `popd`, a bare `cd`, `cd -`; the `builtin` and `command` wrappers and
their option set; `git -C`; a target the line has still to create; a directory in no
repository; a payload `cwd` that disagrees with the process's own.

### F. git's own options

`-C`, `-c <name>=<value>`, `--git-dir`, `--work-tree`, `--namespace`, and a global
this hook does not recognise. `GIT_*` assignments riding in the line.

### G. What `clean` reaches

Force given once versus twice; `-d`; `-x`; `-X`; `-e`; a pathspec present or absent;
`clean.requireForce` set false in the command's own argv.

### H. Which paths the measurement covers

Operands after `--` and before it; an operand that resolves as a ref rather than a
path; `--source`; a magic pathspec (`:(icase)`, `--icase-pathspecs`); a pathspec
holding a character the shell expands after this hook has seen it;
`--pathspec-from-file`.

### I. The override token

Bound to the command and to the content at stake: the same command with the content
rewritten, with the file set changed, presented from a subdirectory, presented for
one of two covered verbs on one line, and issued for a directory-shaped stake.

### J. The coupled pairs

Where the code makes one axis's answer depend on another, and where every fail-open
found so far has lived:

1. G's force count × G's pathspec-or-`-d` (what reaches a nested repository)
2. G's pathspec × the collapse of an untracked directory
3. C's quoting × H's pathspec (a rewrite firing inside a quoted path)
4. C's escaping × the comment scan
5. E's wrappers × E's unreadable constructs (`builtin eval …`)
6. E's non-repository directory × F's relocating globals
7. D's subshell × E's directory movement
8. C's quoted or escaped names × I's fingerprint

## Declared exclusions

Outside the tables above, on purpose. A loss reached through one of these is not a
defect in this hook:

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
The README carries the list with its reasoning; it is part of the frozen set.
