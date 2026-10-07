"""Wake reasons: queued input is not evidence that an agent has stopped."""
from __future__ import annotations

import json

from . import config, storage


def send(name: str, kind: str, session_id: str = "") -> None:
    path = config.WAKE / f"{name}.wake"
    with storage.locked(path):
        storage.atomic_write(path, json.dumps({"kind": kind, "session_id": session_id}))


def consume(name: str) -> dict:
    path = config.WAKE / f"{name}.wake"
    with storage.locked(path):
        if not path.exists():
            return {}
        try:
            event = json.loads(path.read_text())
            return event if isinstance(event, dict) else {"kind": "unknown"}
        except (ValueError, OSError):
            return {"kind": "unknown"}
        finally:
            path.unlink(missing_ok=True)
