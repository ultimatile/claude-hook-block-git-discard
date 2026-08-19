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

The override token is bound to both the exact command and the content at risk.
Changing either invalidates the token.

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

Known gaps include:

- `clean.requireForce=false` supplied through a Git config file;
- narrowed `checkout` or `restore` operations that replace untracked or ignored files from a named tree ([issue #2](https://github.com/ultimatile/claude-hook-block-git-discard/issues/2)).
- external diff commands and `textconv` drivers ([issue #3](https://github.com/ultimatile/claude-hook-block-git-discard/issues/3));

The parser fails closed. If covered Git syntax appears but cannot be assigned to
a command the parser understands, the hook denies it. This can produce false
positives, including:

```sh
echo git reset --hard >> notes.md
man git checkout
git bisect reset
git status --short reset
```

Such commands can be continued with the generated override token. The complete
readable-command scope is tested in
[`tests/scope_cases.py`](tests/scope_cases.py).

## Development

```sh
uv run pytest
```
