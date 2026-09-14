"""Codex agent adapter: transcript discovery, resume, and the Stop-hook contract.

Codex writes its transcripts to `~/.codex/sessions/**/*.jsonl` (or
`$CODEX_HOME/sessions`), one rollout file per session, named with the
session id embedded in the filename. Unlike Claude Code there is no
per-project directory to scope a scan by, so id discovery reads the first
JSON line of every rollout file to find its `cwd` and session id, and
liveness needs the session id up front (there is nothing else to scope a
glob by).

Codex normally drives itself through its own Stop hook, synchronously,
without this module spawning a process at all: `stop_decision` answers that
hook's question ("should this turn continue?") in-process. `resume` is the
timer-driven fallback used only when that loop has gone idle (the
underlying `codex exec` process exited and nothing is left to fire the
hook).
"""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

from .. import brain, instruction, notify, registry, transport

UUID_RX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

_SCAN_SCRIPT = """import json,pathlib,sys
cwd=sys.argv[1]
root=pathlib.Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 and sys.argv[2] else (pathlib.Path.home()/'.codex'/'sessions')
out=[]
for p in (root.rglob('*.jsonl') if root.exists() else []):
    try:
        with p.open(errors='replace') as fh:
            first=json.loads(next(fh))
        payload=first.get('payload') or {}
        pcwd=payload.get('cwd') or ''
        sid=payload.get('session_id') or payload.get('id') or ''
        if pcwd==cwd or pcwd.startswith(cwd.rstrip('/')+'/'):
            out.append((p.stat().st_mtime,sid))
    except Exception:
        pass
for _,sid in sorted(out,reverse=True):
    print(sid)
"""


_MTIME_SCRIPT = """import pathlib,sys
sid=sys.argv[1]
root=pathlib.Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 and sys.argv[2] else (pathlib.Path.home()/'.codex'/'sessions')
m=[p.stat().st_mtime for p in (root.rglob('*'+sid+'*.jsonl') if root.exists() else [])]
print(max(m) if m else '')
"""


def _sessions_root() -> Path:
    """Local root of the Codex sessions tree, honouring CODEX_HOME."""
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "sessions"


def transcript_ids(cwd: str, host: str) -> list[str]:
    """Codex session ids recorded for `cwd`, newest first.

    Reads only the first JSON line of each rollout file (the session's own
    start-of-file metadata), matching `cwd` exactly or as an ancestor. The
    same scan script runs locally and over SSH via transport.python_on.
    """
    root_arg = str(_sessions_root()) if not host else ""
    r = transport.python_on(host, _SCAN_SCRIPT, cwd, root_arg, timeout=180)
    return [line.strip() for line in (r.stdout or "").splitlines() if UUID_RX.match(line.strip())]


def newest_session(cwd: str, host: str) -> str | None:
    """Most recently written session id for `cwd`, or None."""
    ids = transcript_ids(cwd, host)
    return ids[0] if ids else None


def newest_transcript_mtime(cwd: str, session_id: str | None, host: str) -> float | None:
    """Epoch seconds of the newest rollout activity for this session.

    Codex has no per-project directory to scope by, so a session id is
    required; without one this returns None rather than guessing across
    every rollout file on the host. On a remote host the same lookup runs
    over SSH, so the tick can tell a live remote Codex turn from an idle one.
    """
    if not session_id:
        return None
    if host:
        r = transport.python_on(host, _MTIME_SCRIPT, session_id, "", timeout=120)
        out = (r.stdout or "").strip()
        try:
            return float(out) if out else None
        except ValueError:
            return None
    root = _sessions_root()
    if not root.exists():
        return None
    mtimes = [f.stat().st_mtime for f in root.rglob(f"*{session_id}*.jsonl") if f.exists()]
    return max(mtimes) if mtimes else None


def transcript_is_gone(cwd: str, session_id: str, host: str) -> bool:
    """Whether no rollout file for this session id exists any more."""
    if not host:
        root = _sessions_root()
        return not any(root.rglob(f"*{session_id}*.jsonl")) if root.exists() else True
    return session_id not in transcript_ids(cwd, host)


def estimate_tokens(cwd: str, session_id: str, host: str) -> int:
    """Codex transcripts do not carry a usable token estimate."""
    return 0


def last_assistant_text(cwd: str, session_id: str, host: str) -> str:
    """Codex resume replies come back on stdout already; nothing to reread."""
    return ""


def _resume_argv(rec: dict, instruction_text: str, settings: dict) -> list[str]:
    codex_bin = settings.get("codex_command", "codex")
    argv = [codex_bin, "exec", "resume", rec["session_id"], instruction_text,
            "--json", "-c", "mcp_servers={}"]
    sandbox = rec.get("sandbox") or settings.get("codex_sandbox") or ""
    if sandbox:
        argv += ["-c", f'sandbox_mode="{sandbox}"',
                 "-c", "sandbox_workspace_write.network_access=true"]
        roots = rec.get("writable_roots") or []
        if roots:
            argv += ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps(roots)]
    return argv


def _last_agent_message(stdout: str) -> str:
    messages = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            messages.append(item.get("text") or "")
    return messages[-1] if messages else ""


def resume(rec: dict, instruction_text: str, settings: dict) -> tuple[bool, str]:
    """Resume a Codex session headlessly. Returns (ok, reply).

    This is the timer-driven fallback, used by tick only when the Stop-hook
    loop has gone idle. Remote resume is allowed (Linux file-based auth is
    fine over SSH); the macOS keychain caveat that blocks it there is a
    README note, not a refusal enforced here.
    """
    argv = _resume_argv(rec, instruction_text, settings)
    timeout = settings.get("resume_timeout_s", 3600)
    host = rec.get("host") or ""
    try:
        r = transport.run(host, argv, cwd=rec["cwd"], timeout=timeout, settings=settings)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    reply = _last_agent_message(r.stdout or "")
    if not reply:
        reply = (r.stderr or r.stdout or "").strip()
    return r.returncode == 0, reply


def stop_instruction(rec: dict, queued: list[dict], context: str = "") -> str:
    """The continuation prompt handed back through the Codex Stop hook.

    States the goal, the continuation count, tells the model to take the
    next concrete step and verify by reading state back rather than merely
    describing it, to preserve unrelated user changes, and restates the
    PILOT-DONE / PILOT-BLOCKED contract. Any instructions queued in the
    pilot's inbox since the last turn are appended, matching tick's own
    instruction.build_instruction.
    """
    inbox_block = ""
    if queued:
        lines = "\n".join(f"- {q.get('text', '')}" for q in queued)
        inbox_block = f"Instructions from your operator since the last tick:\n{lines}\n\n"
    context_block = ""
    if context.strip():
        context_block = f"Context from your operator's system:\n{context.strip()}\n\n"

    return (
        "You are running under claude-pilot, continuing autonomously toward "
        "the user's stated goal.\n\n"
        f"GOAL: {rec['goal']}\n\n"
        f"This is continuation {rec['ticks']} of at most {rec['max_ticks']}.\n\n"
        "Continue the work. Take the next concrete step and verify it by "
        "reading state back. Do not merely describe what remains. Preserve "
        "unrelated user changes.\n\n"
        f"{context_block}{inbox_block}"
        f"If the goal is fully met AND verified, put {instruction.DONE} on "
        "its own line in your final response, followed by a concise "
        f"delivery summary. Never use {instruction.DONE} for partial work. "
        "If only a human can unblock you, put "
        f"{instruction.BLOCKED} on its own line and state exactly what is "
        "needed."
    )


def stop_decision(payload: dict, settings: dict) -> dict:
    """Answer a Codex Stop hook: continue this turn, or let it end.

    Looks up the active record whose session_id matches the payload (any
    host). Never raises: any exception on the way in is swallowed and an
    empty dict returned, since a broken controller must never block a
    Codex turn.
    """
    try:
        return _stop_decision(payload, settings)
    except Exception:
        return {}


def _stop_decision(payload: dict, settings: dict) -> dict:
    sid = payload.get("session_id") or ""
    if not sid:
        return {}

    target = None
    rec: dict = {}
    for candidate in registry.all_records():
        if (candidate.get("agent") == "codex"
                and candidate.get("session_id") == sid
                and candidate.get("state") == "active"):
            rec = candidate
            target = rec["name"]
            break
    if not target:
        return {}

    name = rec["name"]
    message = payload.get("last_assistant_message") or ""
    rec.setdefault("history", []).append({
        "at": registry.iso(registry.now()),
        "ok": True,
        "turn_id": payload.get("turn_id"),
        "reply": message[:400],
    })

    def _finish(state: str, note: str = "") -> dict:
        rec["state"] = state
        if note:
            notify.send(note)
        registry.save(rec)
        return {}

    if re.search(r"(?m)^\s*" + re.escape(instruction.DONE) + r"\s*(?:$|\n)", message):
        return _finish(
            "done",
            f"pilot {name} DONE after {rec['ticks']} continuation(s)\n{message[:600]}",
        )

    if re.search(r"(?m)^\s*" + re.escape(instruction.BLOCKED) + r"\b", message):
        return _finish(
            "blocked",
            f"pilot {name} BLOCKED, needs you\n{message[:600]}",
        )

    if rec.get("persistent"):
        if rec["ticks"] >= rec["max_ticks"]:
            rec["ticks"] = 0
        try:
            if registry.now() > datetime.datetime.fromisoformat(rec["deadline"]):
                rec["deadline"] = registry.iso(
                    registry.now()
                    + datetime.timedelta(hours=settings.get("default_hours", 12))
                )
        except (KeyError, ValueError):
            pass
    else:
        try:
            if registry.now() > datetime.datetime.fromisoformat(rec["deadline"]):
                return _finish(
                    "expired",
                    f"pilot {name} hit its deadline after {rec['ticks']} "
                    f"continuation(s). Goal: {rec['goal'][:150]}",
                )
        except (KeyError, ValueError):
            pass
        if rec["ticks"] >= rec["max_ticks"]:
            return _finish(
                "exhausted",
                f"pilot {name} used all {rec['max_ticks']} continuations "
                f"without finishing. Goal: {rec['goal'][:150]}",
            )

    verdict = brain.run_judge(rec, message, settings, phase="before")
    if verdict.get("verdict") == "pause":
        rec["paused_reason"] = verdict.get("reason", "")
        return _finish(
            "paused",
            f"pilot {name} paused by judge: {verdict.get('reason', '')}",
        )

    rec["ticks"] = rec.get("ticks", 0) + 1
    queued = registry.inbox_drain(name)
    reason = stop_instruction(rec, queued, brain.fetch_context(rec, settings))
    registry.save(rec)
    return {"decision": "block", "reason": reason}
