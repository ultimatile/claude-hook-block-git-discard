# claude-hook-block-git-discard

A Claude Code `PreToolUse` hook that refuses git commands which would
irreversibly discard uncommitted work.

## The problem

`git checkout -- <file>` restores the file from the index — and takes every
*other* uncommitted change in that file with it. The revert's granularity is the
file; the intent's is almost always one hunk. Nothing warns you, and the loss
reads as though the edit never applied, so the usual reaction is to make the edit
again rather than to go looking for what vanished.

An agent reverting its own scratch edit has no way to see that it also threw away
work you made minutes earlier in the same file. `git reset --hard`, `git clean
-fd`, `git switch -f` and `git restore` lose content the same way.

None of it is recoverable. The content never entered the object store, so there
is no `reflog` entry, no dangling blob, nothing for `git fsck` to find.

## What this does

It does not pattern-match commands that look dangerous. **It runs the same
read-only query git itself would run, and denies on what that query reports.** A
`git checkout` with nothing to lose is allowed through silently; the hook is only
in the way when there is something to lose.

When it denies, the message names what is at stake, shows the hunks, and offers a
route that keeps a recoverable copy *first* — the override comes last, after
there is a way to not need it. Abridged, with the repository root generalized:

```
Blocked: this would discard uncommitted change(s) that cannot be recovered afterwards.

At stake (2 file(s)), relative to /home/you/project:
  src/parser.py
  tests/test_parser.py

 src/parser.py        | 5 ++++-
 tests/test_parser.py | 2 +-
 2 files changed, 5 insertions(+), 2 deletions(-)

Hunks:
  src/parser.py
    @@ -0,0 +1,3 @@
    @@ -2 +5 @@ def parse():
  tests/test_parser.py
    @@ -2 +2 @@ def test_parse():

To keep any of it, make a recoverable copy BEFORE discarding.
Take EITHER route, not both — the first one moves the content, so
the second would find nothing left to copy. …
  git stash push -- <path>...
      sets them aside; `git stash pop` brings them back
  git diff --binary -- <path>... > keep.patch
      writes a patch; after discarding, `git apply keep.patch`
      restores it, and editing the patch first restores only
      the hunks you still want …

If discarding all of it is intended, append this to the command you
ran -- the whole line, otherwise unchanged -- and re-run it:
  # ack:13128c3b927422f4

The token is an override, not a confirmation that the list above was read.
```

The override is a stateless token bound to the exact command *and* the exact
content at stake. Change either and the token stops matching. It is a deny rather
than a permission prompt on purpose: the agent is the one who knows which hunk
was its own throwaway edit, and a human staring at a prompt does not.

## Install

```sh
uv tool install git+https://github.com/ultimatile/claude-hook-block-git-discard
```

Then register it in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "block-git-discard" }
        ]
      }
    ]
  }
}
```

Requires Python 3.11+ and no third-party dependencies.

## What it covers, and what it does not

Five verbs: `checkout`, `switch`, `restore`, `reset`, `clean`. These are the ones
an agent actually types. Plumbing (`read-tree -u`, `checkout-index -f`),
sequencer aborts and `git rm -f` are **not** covered — reaching them would mean
reproducing git's own worktree normalization for no practical gain.

`git stash` is not covered either, and that is a deliberate consequence of the
guarantee: everything stash touches stays reachable from a ref, so it is not the
irreversible kind of loss this guards against.

**The guarantee is narrower than "never lets a destructive command through", and
the difference matters.** The hook reads the command as text; a name that only
resolves to `git` at run time (`$GIT reset --hard`) or a verb that does the same
(a `git co` alias) is invisible to it. What it does hold to:

> No shape it can read is under-refused, and a shape it cannot read is refused
> rather than passed.

That second half is why it fails **closed**. A covered verb sitting in the text
with no call the parser could account for is the exact signature of a parser gap,
so it is denied rather than allowed. The cost is a set of false positives that
are worth knowing about up front:

| Command | Result |
| --- | --- |
| `echo git reset --hard >> notes.md` | denied |
| `man git checkout` | denied |
| `grep 'git checkout -- a.txt' log.txt` | denied |
| `docker run --rm alpine git clean -fdx` | denied |
| a heredoc that *writes* a script containing `git reset --hard` | denied |
| `git bisect reset` | denied |
| `git status --short reset` | denied |

Each costs one override token, and none costs data. Telling these apart from a
real call is precisely the parsing the hook declines to attempt, because a wrong
answer there costs data rather than a token.

The last two differ only in how they get there: the hook reads the call — `git
bisect`, `git status` — but not its subcommand, and rather than give up on a
position it cannot read it scans ahead for a covered verb, a refusal bought
deliberately to close a measured fail-open.

## Development

```sh
uv sync
uv run pytest
```

The suite runs the installed console script as a subprocess against real
repositories built under `tmp_path`, and needs to keep doing so: an escaping
raise is a non-zero exit, which the harness reports as a non-blocking error
before running the command — a fail-open that an in-process test would show as
an ordinary test error.

To run against a working checkout while developing:

```sh
uv tool install -e .
```

## License

MIT
