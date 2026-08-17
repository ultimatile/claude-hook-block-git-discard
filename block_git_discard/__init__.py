"""A Claude Code PreToolUse hook that refuses git commands which would discard
uncommitted work.

The decision is a measurement, not a pattern match: the hook runs the same
read-only query git itself would, and denies on what that query reports. See
`hook` for the reasoning behind the scope and the fail-closed posture.
"""

from __future__ import annotations

from .hook import main

__all__ = ["main"]
