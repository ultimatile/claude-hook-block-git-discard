"""Splitting a shell command line into simple commands.

The Bash tool hands over a whole command line, which may chain several commands.
Deciding which arguments belong to which command is the first thing this hook has
to do, and getting the boundary wrong makes it read the next command's flags as
its target's.

`shlex.split()` cannot draw that boundary: it splits on whitespace only, so
`git checkout -- f; rm -rf dir` yields `'f;'` as one token with no `;` token at
all, and a newline separator disappears as plain whitespace. Either way a walk
that stops at a separator token never finds one and runs off the end of the
command.

`tokenize()` emits separators as standalone tokens so that walk terminates.
"""

from __future__ import annotations

import io
import shlex

# Characters that separate one simple command from the next. shlex emits a run of
# these as its own token (`;`, `&&`, `>>`, ...), so a token made only of them ends
# the current command's argument list. '\n' is included and removed from the
# lexer's whitespace set, so a newline-separated command list splits too.
# Brace groups (`{ cmd; }`) are covered by the `;` their body requires, so `{`
# and `}` are deliberately left out: making them punctuation would also split
# `{}` in `find -exec ... {} \;` and brace expansions like `{a,b}`.
PUNCT = "();<>|&\n"


def tokenize(command: str, *, comments: bool = True) -> list[str]:
    """Split a shell command line, keeping separators as standalone tokens.

    Quoting is honored, so a metacharacter inside an argument (`git commit -m
    'a;b'`) stays part of its token. In posix mode an argument that is nothing but
    a metacharacter (`git commit -m ';'`) is indistinguishable from a real
    separator and ends that invocation's argument list early; the effect is a
    narrower scope, never a wider one.

    `comments=False` stops shlex ending a token at `#`. Its default fires on a
    `#` anywhere in a word, where the shell only starts a comment at a word's
    beginning: `git checkout -- f#1.txt` truncates to `f`, and a caller reading
    that as the filename is reading one the command never names. A caller that
    turns this off owes its own comment handling, since the whole rest of the line
    then arrives as arguments -- see `strip_comments` in `hook`.

    On unbalanced quotes this falls back to a whitespace split so a malformed
    command still gets inspected rather than skipped. Separators stay glued to
    their neighbours in that path, so an invocation's argument list can run long
    -- fail-closed for this hook's deny check, at the cost of over-reach on a
    command that was already syntactically broken.
    """
    lex = shlex.shlex(io.StringIO(command), posix=True, punctuation_chars=PUNCT)
    lex.whitespace_split = True
    lex.whitespace = " \t\r"
    if not comments:
        lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return command.split()


def is_separator(tok: str) -> bool:
    """True if a token is a command separator rather than an argument."""
    return bool(tok) and all(ch in PUNCT for ch in tok)
