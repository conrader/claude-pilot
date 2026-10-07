"""Headless resume and compaction of a piloted session.

A headless `-p` resume has no TTY to answer a permission prompt: without a
permission mode every write blocks and the pilot reports a false blocker for
a purely mechanical reason. A pilot is an autonomous loop the operator chose
to run, so it runs with the same non-interactive permission stance every
tick.
"""
from __future__ import annotations

from . import agents, instruction as instruction_mod, registry
from .agents import claude as claude_agent


def resume(rec: dict, instruction: str, settings: dict) -> tuple[bool, str]:
    """Resume the piloted session headlessly. Returns (ok, reply).

    Dispatches to the agent module (claude or codex) matching `rec`'s
    "agent" field.
    """
    return agents.for_record(rec).resume(rec, instruction, settings)


def compact(rec: dict, settings: dict) -> bool:
    """Ask for a handoff, then start a fresh session carrying it.

    Returns True and mutates `rec` in place (previous_sessions, session_id,
    compactions) if a new session was found; False (keeping the old session)
    if the handoff was refused or the fresh session's id could not be
    located, because losing the thread is worse than one skipped compaction.
    """
    if rec.get("agent", "claude") != "claude":
        return False
    if "_revision" in rec and not registry.is_current(rec):
        return False
    ok, handoff = resume(rec, instruction_mod.handoff_request(), settings)
    if not ok or len(handoff.strip()) < 200:
        return False
    if "_revision" in rec and not registry.is_current(rec):
        return False

    opening = instruction_mod.handoff_opening(handoff, rec)
    try:
        new_sid = claude_agent.start(rec, opening, settings)
    except Exception:
        return False
    if not new_sid or new_sid == rec.get("session_id"):
        return False
    rec["previous_sessions"] = rec.get("previous_sessions", []) + [rec["session_id"]]
    rec["session_id"] = new_sid
    rec["compactions"] = rec.get("compactions", 0) + 1
    return True
