"""Splitting a shell command line into simple commands.

`shlex.split()` cannot draw the boundary: it splits on whitespace only, so
`git checkout -- f; rm -rf dir` yields `'f;'` with no `;` token at all, and a
newline separator disappears as plain whitespace. `tokenize()` emits separators
as standalone tokens instead, so a walk that stops at one terminates.
"""

from __future__ import annotations

import io
import shlex

# Characters that separate one simple command from the next. shlex emits a run of
# these as its own token (`;`, `&&`, `>>`, ...), so a token made only of them ends
# the current command's argument list. '\n' is included and removed from the
# lexer's whitespace set, so a newline-separated command list splits too. `{` and
# `}` are left out: making them punctuation would split `{}` in
# `find -exec ... {} \;` and brace expansions like `{a,b}`.
PUNCT = "();<>|&\n"


def tokenize(command: str, *, comments: bool = True) -> list[str]:
    """Split a shell command line, keeping separators as standalone tokens.

    Quoting is honored, so a metacharacter inside an argument stays part of its
    token. An argument that is nothing but a metacharacter (`git commit -m ';'`) is
    indistinguishable from a real separator and ends that argument list early, which
    narrows the scope rather than widening it.

    `comments=False` stops shlex ending a token at a `#` anywhere in a word, where
    the shell opens a comment only at a word's beginning; a caller that turns it off
    owes its own comment handling.

    On unbalanced quotes this falls back to a whitespace split, where separators
    stay glued to their neighbours and an argument list can run long — fail-closed
    for this hook.
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
