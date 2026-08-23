"""A Claude Code PreToolUse hook that refuses git commands which would discard
uncommitted work.
"""

from __future__ import annotations

from .hook import main

__all__ = ["main"]
