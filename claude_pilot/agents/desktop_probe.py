"""Read-only activity probe for Codex desktop's paginated history.

This module is also sent to remote Python interpreters, so it depends only on
the standard library and never writes to the desktop's databases.
"""
import json
import sqlite3
import sys
from pathlib import Path


def probe(cwd: str, session_id: str, root: str) -> dict:
    root = Path(root).expanduser() if root else Path.home() / ".codex"
    info = {"mode": None, "active": None, "mtime": None, "turn_id": None, "reply": ""}
    try:
        with sqlite3.connect((root / "state_5.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM threads WHERE id=?", (session_id,)).fetchone()
        if not row or Path(row["cwd"]).resolve() != Path(cwd).resolve():
            return info
        row = dict(row)
        info["mode"] = row.get("history_mode")
        if row.get("updated_at_ms") is not None:
            info["mtime"] = float(row["updated_at_ms"]) / 1000
        elif row.get("updated_at") is not None:
            info["mtime"] = float(row["updated_at"])
        with sqlite3.connect((root / "thread_history_1.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            turn = db.execute("SELECT * FROM thread_turns WHERE thread_id=? "
                              "ORDER BY rollout_ordinal DESC LIMIT 1", (session_id,)).fetchone()
            busy = db.execute("SELECT 1 FROM thread_turns WHERE thread_id=? "
                              "AND status='inProgress' LIMIT 1", (session_id,)).fetchone()
            if turn and not busy and turn["status"] == "completed":
                item_id = dict(turn).get("final_agent_item_id")
                if item_id:
                    item = db.execute("SELECT item_json FROM thread_items WHERE thread_id=? "
                                      "AND item_id=?", (session_id, item_id)).fetchone()
                    if item:
                        message = json.loads(item[0])
                        if message.get("type") == "agentMessage":
                            info["reply"] = message.get("text") or ""
        if turn:
            info["active"] = bool(busy)
            info["turn_id"] = dict(turn).get("turn_id")
    except (sqlite3.Error, OSError, ValueError, TypeError):
        # A known desktop session with unreadable history is not safe to nudge.
        if info["mode"] == "paginated":
            info["active"] = True
    return info


if __name__ == "__main__":
    print(json.dumps(probe(*sys.argv[1:])))
