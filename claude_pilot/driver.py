"""Headless resume and compaction of a piloted session.

A headless `-p` resume has no TTY to answer a permission prompt: without a
permission mode every write blocks and the pilot reports a false blocker for
a purely mechanical reason. A pilot is an autonomous loop the operator chose
to run, so it runs with the same non-interactive permission stance every
tick.
"""
from __future__ import annotations

import os
import subprocess

from . import agents, instruction as instruction_mod, sessions
from .agents import claude as claude_agent


def _run(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_PILOT_TICK"] = "1"
    env["PILOT_HEADLESS"] = "1"
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env,
    )


def _argv(settings: dict, tail: list[str]) -> list[str]:
    """The claude command line: binary, the turn, then the non-interactive flags.

    Kept for backward compatibility; agents/claude.py has its own copy used
    on the dispatch path.
    """
    argv = [settings.get("agent_command", "claude"), *tail]
    mode = settings.get("permission_mode") or ""
    if mode:
        argv += ["--permission-mode", mode]
    argv += ["--output-format", "text"]
    model = settings.get("model") or ""
    if model:
        argv += ["--model", model]
    return argv


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
    ok, handoff = resume(rec, instruction_mod.handoff_request(), settings)
    if not ok or len(handoff.strip()) < 200:
        return False

    opening = instruction_mod.handoff_opening(handoff, rec)
    argv = _argv(settings, ["-p", opening])
    timeout = settings.get("resume_timeout_s", 3600)
    try:
        r = _run(argv, rec["cwd"], timeout)
    except Exception:
        return False
    if r.returncode != 0:
        return False

    new_sid = sessions.newest_session(rec["cwd"])
    if not new_sid or new_sid == rec.get("session_id"):
        return False
    rec["previous_sessions"] = rec.get("previous_sessions", []) + [rec["session_id"]]
    rec["session_id"] = new_sid
    rec["compactions"] = rec.get("compactions", 0) + 1
    return True
