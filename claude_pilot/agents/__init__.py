"""Agent dispatch: pick the claude or codex module for a pilot record.

Codex support is imported lazily so this package (and anything that only
needs the claude side) works before agents/codex.py exists.
"""
from __future__ import annotations

from . import claude as claude


def for_record(rec: dict):
    """Return the agent module (claude or codex) that drives `rec`."""
    agent = rec.get("agent", "claude")
    if agent == "codex":
        from . import codex

        return codex
    return claude
