"""Local Claude Code transcript discovery, liveness and the interactive guard.

Everything here reads state Claude Code itself writes: transcript files under
its projects directory, and /proc for a live agent process. Nothing here talks
to a remote host; a pilot only ever manages a local session.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

UUID_RX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def transcript_slug(cwd: str) -> str:
    """Claude Code's transcript directory name for a working directory.

    Every character that is not a letter or digit becomes a dash, not just
    path separators: a directory containing a dot, space or underscore needs
    the same treatment as `/`, or the computed slug never matches anything on
    disk and the session looks unrecorded when it is not.
    """
    return "-" + re.sub(r"[^A-Za-z0-9]", "-", cwd.strip("/"))


def projects_dir() -> Path:
    """Root directory Claude Code stores per-project transcripts under."""
    return Path(os.environ.get("CLAUDE_PROJECTS_DIR") or "~/.claude/projects").expanduser()


def transcript_ids(cwd: str) -> list[str]:
    """Session ids with an actual transcript file for `cwd`, newest first.

    A session id is only resumable if its file exists; some ids are visible
    elsewhere (a subagent shares its parent's cwd but has no transcript of its
    own) without ever being usable here.
    """
    d = projects_dir() / transcript_slug(cwd)
    if not d.exists():
        return []
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files if UUID_RX.match(p.stem)]


def newest_session(cwd: str) -> str | None:
    """Most recently written resumable session id for `cwd`, or None."""
    ids = transcript_ids(cwd)
    return ids[0] if ids else None


def newest_transcript_mtime(cwd: str, session_id: str | None = None) -> float | None:
    """Epoch seconds of the newest transcript activity for `cwd`.

    With a session id, only that session's own file and its subagents/
    subtree count as life for it; a build waiting on subagents can go quiet at
    the top level while its subagents write every few seconds, so this looks
    recursively rather than only at the top-level file. Without a session id,
    any transcript under the project directory counts.
    """
    d = projects_dir() / transcript_slug(cwd)
    if not d.exists():
        return None
    if session_id:
        own = [d / f"{session_id}.jsonl"]
        sub_dir = d / session_id
        if sub_dir.is_dir():
            own += list(sub_dir.rglob("*.jsonl"))
        mtimes = [f.stat().st_mtime for f in own if f.exists()]
        if mtimes:
            return max(mtimes)
    mtimes = [f.stat().st_mtime for f in d.rglob("*.jsonl")]
    return max(mtimes) if mtimes else None


def transcript_is_gone(cwd: str, session_id: str) -> bool:
    """True when a recorded session's transcript file no longer exists."""
    d = projects_dir() / transcript_slug(cwd)
    return not (d / f"{session_id}.jsonl").exists()


def interactive_agent_pid(cwd: str, agent: str = "claude") -> int | None:
    """PID of a human-driven agent process working in `cwd`, or None.

    Only a process attached to a terminal counts, so a pilot's own headless
    `claude -r <id> -p` never blocks its next tick: an open interactive
    session is not quiet, it is somebody else's turn, and running a headless
    resume beside it would mean two agents editing the same working tree
    under one session id. Linux only; returns None elsewhere.
    """
    proc = Path("/proc")
    if not proc.exists():
        return None
    want = cwd.rstrip("/")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.readlink(entry / "cwd").rstrip("/") != want:
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if not argv or Path(argv[0].decode(errors="replace")).name != agent:
                continue
            if b"-p" in argv or b"--print" in argv:
                continue  # headless: a pilot's own turn, not a person
            tty = (entry / "stat").read_text().rsplit(")", 1)[1].split()[4]
            if tty != "0":
                return int(entry.name)
        except (OSError, ValueError, IndexError):
            continue
    return None


def estimate_tokens(cwd: str, session_id: str) -> int:
    """Rough token count of a session, from the text actually in its transcript.

    File size is a bad proxy: a transcript is mostly JSON scaffolding, and
    prose alone under-counts because tool input/output also occupies the
    context window. This is deliberately a proxy, not an exact count; it only
    needs to be good enough to decide when to hand off to a fresh session.
    """
    path = projects_dir() / transcript_slug(cwd) / f"{session_id}.jsonl"
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(errors="replace").splitlines():
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


def last_assistant_text(cwd: str, session_id: str, max_chars: int = 4000) -> str:
    """Final assistant text in the transcript, or "" if none is found.

    Used to recover a reply when a headless invocation's own stdout is empty
    but the transcript shows the session actually answered.
    """
    path = projects_dir() / transcript_slug(cwd) / f"{session_id}.jsonl"
    if not path.exists():
        return ""
    last = ""
    for line in path.read_text(errors="replace").splitlines():
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
