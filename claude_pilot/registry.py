"""Pilot records and the orchestrator -> pilot inbox.

A pilot is identified by its `name`, derived from the working directory it
runs in. Records live as one JSON file per pilot under config.PILOTS; the
inbox is a JSONL file of queued instructions next to it.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from pathlib import Path

from . import config

STATES = (
    "active", "done", "blocked", "expired", "exhausted", "error", "stopped", "paused",
)


def now() -> datetime.datetime:
    """Current UTC time."""
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt: datetime.datetime) -> str:
    """ISO-8601 string with second precision, matching how records store time."""
    return dt.isoformat(timespec="seconds")


def name_for_cwd(cwd: str) -> str:
    """Derive a pilot name from a working directory.

    Lowercased basename with non-alphanumeric characters replaced by "-". If
    a different, already-registered pilot has a different cwd but the same
    derived name, a short hash suffix is appended to keep names unique.
    """
    base = re.sub(r"[^a-z0-9]+", "-", Path(cwd).name.lower()).strip("-") or "pilot"
    path, rec = load(base)
    if rec is None or rec.get("cwd") == cwd:
        return base
    suffix = hashlib.sha1(cwd.encode()).hexdigest()[:6]
    return f"{base}-{suffix}"


def _record_path(name: str) -> Path:
    return config.PILOTS / f"{name}.json"


def load(name: str):
    """(path, record) for a pilot, or (path, None) if absent or unreadable."""
    p = _record_path(name)
    if not p.exists():
        return p, None
    try:
        return p, json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return p, None


def save(rec: dict) -> None:
    """Write a pilot record, keyed by rec['name']."""
    config.PILOTS.mkdir(parents=True, exist_ok=True)
    _record_path(rec["name"]).write_text(json.dumps(rec, indent=1))


def all_records() -> list[dict]:
    """Every readable pilot record, sorted by name."""
    if not config.PILOTS.exists():
        return []
    out = []
    for f in sorted(config.PILOTS.glob("*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def active_names(exclude: str | None = None) -> list[str]:
    """Names of every pilot currently in state 'active', for the cap check.

    A non-persistent pilot past its own deadline does not count: a stale
    record should never block a fresh start.
    """
    out = []
    for rec in all_records():
        name = rec.get("name")
        if name == exclude:
            continue
        if rec.get("state") != "active":
            continue
        if not rec.get("persistent") and rec.get("deadline"):
            try:
                if now() > datetime.datetime.fromisoformat(rec["deadline"]):
                    continue
            except ValueError:
                pass
        out.append(name)
    return out


def cap_block(name: str, allow_more: bool, cap: int) -> str:
    """Empty string if starting `name` stays within `cap`; otherwise a
    refusal message naming the pilots already active."""
    if allow_more:
        return ""
    active = active_names(exclude=name)
    if len(active) < cap:
        return ""
    return (
        f"{len(active)} pilots are already active within their bounds "
        f"({', '.join(active)}), cap is {cap}, refusing to start {name}. "
        "Stop one first, or pass --allow-more."
    )


def find_by_cwd(cwd: str):
    """The record whose cwd exactly matches, or the nearest ancestor's, or None."""
    here = Path(cwd).resolve()
    best = None
    best_len = -1
    for rec in all_records():
        rec_cwd = rec.get("cwd")
        if not rec_cwd:
            continue
        try:
            rec_path = Path(rec_cwd).resolve()
        except OSError:
            continue
        if here == rec_path or rec_path in here.parents:
            parts = len(rec_path.parts)
            if parts > best_len:
                best_len = parts
                best = rec
    return best


# --- inbox -------------------------------------------------------------

def inbox_path(name: str) -> Path:
    """Path to a pilot's inbox JSONL file."""
    return config.PILOTS / f"{name}.inbox.jsonl"


def inbox_add(name: str, text: str) -> None:
    """Queue an instruction for a pilot's next tick."""
    config.PILOTS.mkdir(parents=True, exist_ok=True)
    with inbox_path(name).open("a") as fh:
        fh.write(json.dumps({"at": iso(now()), "text": text, "consumed": False}) + "\n")


def _rows(name: str) -> list[dict]:
    p = inbox_path(name)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def inbox_pending(name: str) -> list[dict]:
    """Instructions not yet consumed, oldest first."""
    return [r for r in _rows(name) if not r.get("consumed")]


def inbox_drain(name: str) -> list[dict]:
    """Return pending instructions and mark them consumed on disk."""
    rows = _rows(name)
    pending = [r for r in rows if not r.get("consumed")]
    if pending:
        for r in rows:
            r["consumed"] = True
        inbox_path(name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return pending
