"""Claude Code as a pilot agent: local or over SSH on a configured host.

Locally this is a thin wrapper around claude_pilot.sessions, which already
reads Claude Code's own transcript files. Remotely the same information is
gathered by shipping a small python3 script over SSH via transport.python_on
that walks the equivalent ~/.claude/projects/<slug> tree on the far side.
"""
from __future__ import annotations

import json
import re

from .. import config, sessions, transport

_LIST_SCRIPT = (
    "import json,os,sys\n"
    "from pathlib import Path\n"
    "d = Path(os.path.expanduser('~/.claude/projects')) / sys.argv[1]\n"
    "rows = []\n"
    "if d.exists():\n"
    "    for p in d.rglob('*.jsonl'):\n"
    "        try:\n"
    "            m = p.stat().st_mtime\n"
    "        except OSError:\n"
    "            continue\n"
    "        rows.append((m, str(p.relative_to(d))))\n"
    "print(json.dumps(rows))\n"
)


def _remote_rows(cwd: str, host: str) -> list[tuple[float, str]]:
    """(mtime, path-relative-to-project-dir) for every transcript remotely."""
    slug = sessions.transcript_slug(cwd)
    r = transport.python_on(host, _LIST_SCRIPT, slug, timeout=60)
    if r.returncode != 0 or not (r.stdout or "").strip():
        return []
    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return [(float(m), p) for m, p in rows]


def transcript_ids(cwd: str, host: str = "") -> list[str]:
    """Session ids with a transcript file for `cwd`, newest first."""
    if not host:
        return sessions.transcript_ids(cwd)
    rows = [(m, p) for m, p in _remote_rows(cwd, host) if "/" not in p]
    rows.sort(key=lambda t: t[0], reverse=True)
    out = []
    for _, p in rows:
        stem = p[:-6] if p.endswith(".jsonl") else p
        if sessions.UUID_RX.match(stem):
            out.append(stem)
    return out


def newest_session(cwd: str, host: str = "") -> str | None:
    """Most recently written resumable session id for `cwd`, or None."""
    ids = transcript_ids(cwd, host)
    return ids[0] if ids else None


def newest_transcript_mtime(
    cwd: str, session_id: str | None = None, host: str = ""
) -> float | None:
    """Epoch seconds of the newest transcript activity for `cwd`."""
    if not host:
        return sessions.newest_transcript_mtime(cwd, session_id)
    rows = _remote_rows(cwd, host)
    if not rows:
        return None
    if session_id:
        own = [
            m
            for m, p in rows
            if p == f"{session_id}.jsonl" or p.startswith(f"{session_id}/")
        ]
        if own:
            return max(own)
    mtimes = [m for m, _ in rows]
    return max(mtimes) if mtimes else None


def transcript_is_gone(cwd: str, session_id: str, host: str = "") -> bool:
    """True when a recorded session's transcript file no longer exists."""
    if not host:
        return sessions.transcript_is_gone(cwd, session_id)
    rows = _remote_rows(cwd, host)
    return not any(p == f"{session_id}.jsonl" for _, p in rows)


def _argv(settings: dict, claude_bin: str, tail: list[str]) -> list[str]:
    """The claude command line: binary, the turn, then non-interactive flags."""
    argv = [claude_bin, *tail]
    mode = settings.get("permission_mode") or ""
    if mode:
        argv += ["--permission-mode", mode]
    argv += ["--output-format", "text"]
    model = settings.get("model") or ""
    if model:
        argv += ["--model", model]
    return argv


def _last_assistant_text_remote(
    cwd: str, session_id: str, host: str, settings: dict | None = None, max_chars: int = 4000
) -> str:
    slug = sessions.transcript_slug(cwd)
    path = f"~/.claude/projects/{slug}/{session_id}.jsonl"
    r = transport.run(host, ["cat", path], timeout=60, settings=settings)
    if r.returncode != 0:
        return ""
    last = ""
    for line in (r.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    last = text
    return last[:max_chars]


def last_assistant_text(cwd: str, session_id: str, host: str = "", settings: dict | None = None) -> str:
    """Final assistant text in the transcript, or "" if none is found."""
    if not host:
        return sessions.last_assistant_text(cwd, session_id)
    return _last_assistant_text_remote(cwd, session_id, host, settings)


def estimate_tokens(cwd: str, session_id: str, host: str = "", settings: dict | None = None) -> int:
    """Rough token count of a session's transcript."""
    if not host:
        return sessions.estimate_tokens(cwd, session_id)
    slug = sessions.transcript_slug(cwd)
    path = f"~/.claude/projects/{slug}/{session_id}.jsonl"
    r = transport.run(host, ["cat", path], timeout=60, settings=settings)
    if r.returncode != 0:
        return 0
    n = 0
    for line in (r.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (d.get("message") or {}).get("content")
        if isinstance(content, str):
            n += len(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    n += len(block.get("text", ""))
                elif btype == "tool_use":
                    n += len(json.dumps(block.get("input", {})))
                elif btype == "tool_result":
                    result = block.get("content")
                    n += len(result if isinstance(result, str) else json.dumps(result))
    return n // 4


def resume(rec: dict, instruction: str, settings: dict) -> tuple[bool, str]:
    """Resume the piloted Claude session headlessly. Returns (ok, reply)."""
    host = rec.get("host", "") or ""
    if host:
        claude_bin = config.host_config(host, settings).get("claude", "claude")
    else:
        claude_bin = settings.get("agent_command", "claude")
    argv = _argv(settings, claude_bin, ["-r", rec["session_id"], "-p", instruction])
    timeout = settings.get("resume_timeout_s", 3600)
    try:
        r = transport.run(host, argv, cwd=rec["cwd"], timeout=timeout, settings=settings)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    ok = r.returncode == 0
    reply = (r.stdout or "").strip()
    if not reply:
        reply = last_assistant_text(rec["cwd"], rec["session_id"], host, settings)
    if ok:
        return True, reply
    if not reply:
        reply = (r.stderr or "").strip()[-2000:]
    return False, reply
