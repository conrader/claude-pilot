"""The second-brain connector: context in before a turn, a verdict after it.

Both hooks are optional shell commands from the settings, time-boxed and
never fatal: the loop must keep working when the operator's own system is
slow, broken or absent, so every failure degrades to "no context" or "ok".
Used by the tick loop and by the Codex Stop-hook controller alike, so a
brain plugged in once sees every turn of every pilot.
"""
from __future__ import annotations

import json
import subprocess

TIMEOUT = 30


def fetch_context(rec: dict, settings: dict) -> str:
    """Run context_command with the record on stdin; return its stdout or ""."""
    cmd = settings.get("context_command") or ""
    if not cmd:
        return ""
    try:
        r = subprocess.run(cmd, shell=True, input=json.dumps(rec), capture_output=True,
                           text=True, timeout=TIMEOUT)
        return (r.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"  context_command failed: {type(exc).__name__}: {exc}")
        return ""


def run_judge(rec: dict, reply: str, settings: dict) -> dict:
    """Run judge_command on {record, reply}; return {"verdict": "ok"|"pause", ...}.

    Anything other than a JSON object with a verdict of ok or pause counts as
    ok and is reported on stdout, so a misbehaving judge can only ever fail
    to pause, never break a tick.
    """
    cmd = settings.get("judge_command") or ""
    if not cmd:
        return {"verdict": "ok"}
    try:
        r = subprocess.run(cmd, shell=True, input=json.dumps({"record": rec, "reply": reply}),
                           capture_output=True, text=True, timeout=TIMEOUT)
        data = json.loads((r.stdout or "").strip() or "{}")
        if isinstance(data, dict) and data.get("verdict") in ("ok", "pause"):
            return data
        print("  judge_command: invalid output, treating as ok")
    except Exception as exc:  # noqa: BLE001
        print(f"  judge_command failed: {type(exc).__name__}: {exc}")
    return {"verdict": "ok"}
