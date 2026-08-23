# claude-hook-block-git-discard

A Claude Code `PreToolUse` hook that blocks Git commands when they would
irreversibly discard uncommitted work.

It protects changes that Git itself cannot recover because they were never
committed, staged, or stored as objects.

## Install

```sh
uv tool install git+https://github.com/ultimatile/claude-hook-block-git-discard
```

Register the command as a Claude Code `PreToolUse` hook:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "block-git-discard" }]
      }
    ]
  }
}
```

Requires Python 3.11+, Git, and Claude Code. The installed hook has no
third-party Python dependencies.

## Usage

Once registered, the hook runs automatically before each Bash tool call.

For covered Git commands, it uses read-only Git queries to determine whether
the command would discard content. Safe commands continue silently. Unsafe
commands are denied with:

- the affected paths
- a diff summary and hunk locations, for tracked changes
- a preservation command chosen for the content at risk
- an override token

The override token is bound to the command and to the content at risk. Changing
either invalidates it. Re-spacing the same command does not, so the token still
matches wherever on the line you append it.

What "the content" means differs by kind. For a tracked change it is the patch.
For untracked and ignored content it is each file's size and modification time,
which a same-length replacement that preserves the timestamp leaves unchanged,
and which a symlink contributes from its target rather than from itself
([issue #10](https://github.com/ultimatile/claude-hook-block-git-discard/issues/10)).

Where nothing could be measured the token binds the command alone, with its
whitespace flattened, so two commands that differ only inside a quoted word
share one ([issue #11](https://github.com/ultimatile/claude-hook-block-git-discard/issues/11)).

## Scope

The hook covers destructive forms of these Git commands:

- `checkout`
- `switch`
- `restore`
- `reset`
- `clean`

It inspects effects rather than blocking these command names unconditionally.
For example, a `git checkout` that would not discard changes is allowed.

`git stash` is not blocked because it preserves its input in a Git ref.

## Limitations

The hook reads shell command text; it does not reproduce shell execution.
Runtime indirection such as `$GIT reset --hard`, Git aliases such as `git co`,
shell functions, and commands loaded through `source` or `.` are outside its
readable scope.

Git plumbing commands, sequencer aborts, and `git rm -f` are also outside the
covered command set.

A forced switch is measured against the working tree, but not against the branch
it is switching to. An untracked file that the target branch also carries is
overwritten, and the hook does not stop it.

Known gaps include:

- `clean.requireForce=false` supplied through a Git config file;
- narrowed `checkout` or `restore` operations that replace untracked or ignored files from a named tree ([issue #2](https://github.com/ultimatile/claude-hook-block-git-discard/issues/2));
- external diff commands and `textconv` drivers ([issue #3](https://github.com/ultimatile/claude-hook-block-git-discard/issues/3));
- `git reset --hard <commit>`, which overwrites an untracked file the named commit tracks ([issue #4](https://github.com/ultimatile/claude-hook-block-git-discard/issues/4));
- a `cd` placed behind a shell keyword — `then`, `else`, `do`, `time` — which the directory walk steps over, measuring the tree the command started in rather than the one it moves to ([issue #5](https://github.com/ultimatile/claude-hook-block-git-discard/issues/5));
- a subshell closed immediately before a redirection, as in `(cd d)>/dev/null`, whose close is dropped with the redirection and so never ends the subshell ([issue #6](https://github.com/ultimatile/claude-hook-block-git-discard/issues/6));
- `GIT_DIR` put into the environment by `export` rather than as an inline prefix, which the inline spelling's refusal does not reach ([issue #7](https://github.com/ultimatile/claude-hook-block-git-discard/issues/7));
- a behaviour-affecting `git -c <key>=<value>`, which reaches the command but not the measurement, so `-c core.excludesFile=/dev/null clean -fd` deletes a file the measurement never listed ([issue #8](https://github.com/ultimatile/claude-hook-block-git-discard/issues/8));
- an assignment prefix in front of a directory change, as in `X=1 cd d`, which the directory walk steps over ([issue #5](https://github.com/ultimatile/claude-hook-block-git-discard/issues/5));
- `pushd -n d`, which adds a directory to the stack without moving to it while the walk reads it as a move ([issue #13](https://github.com/ultimatile/claude-hook-block-git-discard/issues/13)).

The parser fails closed. If covered Git syntax appears but cannot be assigned to
a command the parser understands, the hook denies it. This can produce false
positives, including:

```sh
echo git reset --hard >> notes.md
man git checkout
git bisect reset
git status --short reset
git clean -i
```

`git clean -i` is on that list because `-i` makes git ignore
`clean.requireForce`: whether the menu deletes turns on what answers it, and
that comes from stdin rather than from the command text.

Measurement produces one of its own: a submodule whose worktree is dirty is
reported by the parent's `git diff`, so a plain `git reset --hard` is denied over
content that reset does not reach ([issue #9](https://github.com/ultimatile/claude-hook-block-git-discard/issues/9)).

Such commands can be continued with the generated override token. The complete
readable-command scope is tested in
[`tests/scope_cases.py`](tests/scope_cases.py).

## Development

```sh
uv run pytest
```
