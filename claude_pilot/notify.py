"""Pluggable notification: shell out to notify_command, always echo to stdout.

Identical text sent twice within 10 minutes is suppressed so a flapping
condition does not spam whatever notify_command delivers to.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from . import config

_DEDUP_WINDOW_S = 600


def _state_path() -> Path:
    return config.HOME / "notify.jsonl"


def _recently_sent(text: str) -> bool:
    p = _state_path()
    if not p.exists():
        return False
    cutoff = time.time() - _DEDUP_WINDOW_S
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("text") == text and row.get("at", 0) >= cutoff:
            return True
    return False


def _record_sent(text: str) -> None:
    config.HOME.mkdir(parents=True, exist_ok=True)
    p = _state_path()
    cutoff = time.time() - _DEDUP_WINDOW_S
    rows = []
    if p.exists():
        for line in p.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("at", 0) >= cutoff:
                rows.append(row)
    rows.append({"text": text, "at": time.time()})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def send(text: str) -> None:
    """Deliver a notification: run notify_command with text on stdin (if
    configured), and always print it to stdout. Skips delivery (but still
    prints) when the identical text was sent within the last 10 minutes.
    """
    print(f"notify: {text}")

    if _recently_sent(text):
        return
    _record_sent(text)

    cmd = config.settings().get("notify_command")
    if not cmd:
        return
    try:
        subprocess.run(cmd, shell=True, input=text, capture_output=True,
                        text=True, timeout=30)
    except Exception:
        pass
