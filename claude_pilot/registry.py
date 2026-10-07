"""Pilot records and the orchestrator -> pilot inbox.

A pilot is identified by its `name`, derived from the working directory it
runs in. Records live as one JSON file per pilot under config.PILOTS; the
inbox is a JSONL file of queued instructions next to it.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from . import config, storage

STATES = (
    "active", "done", "blocked", "expired", "exhausted", "error", "stopped", "paused",
)


class ConflictError(RuntimeError):
    """A record changed since the caller read it; reload before retrying."""


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
    """Atomically save unless another writer changed the loaded revision.

    Updates the caller's revision on success. Legacy records start at zero.
    Never hold this lock while running an agent or an operator command.
    """
    path = _record_path(rec["name"])
    with storage.locked(path):
        _, current = load(rec["name"])
        revision = (current or {}).get("_revision", 0)
        if revision != rec.get("_revision", 0):
            raise ConflictError(f"pilot {rec['name']} changed during this operation")
        updated = dict(rec, _revision=revision + 1)
        storage.atomic_write(path, json.dumps(updated, indent=1))
        rec["_revision"] = updated["_revision"]


def is_current(rec: dict) -> bool:
    """Whether a long-running action still belongs to the current record."""
    _, current = load(rec["name"])
    return current is not None and current.get("_revision", 0) == rec.get("_revision", 0)


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
    path = inbox_path(name)
    with storage.locked(path):
        with path.open("a") as fh:
            fh.write(json.dumps({"id": str(uuid.uuid4()), "at": iso(now()),
                                 "text": text, "consumed": False}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


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


def _inbox_rows(name: str) -> list[dict]:
    """Assign durable identities to legacy rows while holding the inbox lock."""
    rows = _rows(name)
    changed = False
    for row in rows:
        if not row.get("id"):
            row["id"] = str(uuid.uuid4())
            changed = True
    if changed:
        _write_rows(name, rows)
    return rows


def _write_rows(name: str, rows: list[dict]) -> None:
    storage.atomic_write(inbox_path(name), "".join(json.dumps(r) + "\n" for r in rows))


def inbox_pending(name: str) -> list[dict]:
    """Snapshot unacknowledged instructions without consuming them."""
    with storage.locked(inbox_path(name)):
        return [r for r in _inbox_rows(name) if not r.get("consumed")]


def inbox_ack(name: str, ids: list[str]) -> None:
    """Acknowledge only delivered IDs, preserving concurrently appended input."""
    if not ids:
        return
    with storage.locked(inbox_path(name)):
        rows = _inbox_rows(name)
        for row in rows:
            if row["id"] in ids:
                row["consumed"] = True
        _write_rows(name, rows)


def inbox_drain(name: str) -> list[dict]:
    """Return pending instructions and mark them consumed on disk."""
    with storage.locked(inbox_path(name)):
        rows = _inbox_rows(name)
        pending = [r for r in rows if not r.get("consumed")]
        if pending:
            for r in rows:
                r["consumed"] = True
            _write_rows(name, rows)
        return pending
