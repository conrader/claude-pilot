"""The loop: one pass over every enrolled pilot, advancing each by one step.

Order matters here more than anywhere else in the package: the wake marker is
consumed before any state check (a stray marker with no matching record would
otherwise hold a systemd path unit in a restart loop forever), the interactive
guard outranks a wake marker (a human at a terminal in that tree outranks any
automated signal), and idle backoff is evaluated only after both of those.
"""
from __future__ import annotations

import datetime
import fcntl
import re

from . import config, driver, instruction, notify, registry, sessions

_LIMIT_RE = re.compile(r"usage limit|rate limit|session limit|resets \d", re.I)


def _save(rec: dict) -> None:
    registry.save(rec)


def _tick_one(rec: dict, settings: dict) -> bool:
    """Advance one record by a tick. Returns True if a turn was spent."""
    name = rec["name"]
    config.WAKE.mkdir(parents=True, exist_ok=True)
    woken = config.WAKE / f"{name}.wake"
    just_stopped = woken.exists()
    if just_stopped:
        woken.unlink(missing_ok=True)

    # A pilot that died on a usage limit is waiting, not broken.
    if rec.get("state") == "error" and rec.get("history"):
        last = rec["history"][-1]
        if _LIMIT_RE.search(last.get("reply", "")):
            try:
                age_min = (
                    registry.now() - datetime.datetime.fromisoformat(last["at"])
                ).total_seconds() / 60
            except ValueError:
                age_min = 0
            if age_min >= 60:
                rec["state"] = "active"
                try:
                    if registry.now() > datetime.datetime.fromisoformat(rec["deadline"]):
                        rec["deadline"] = registry.iso(
                            registry.now() + datetime.timedelta(hours=settings.get("default_hours", 12))
                        )
                except (KeyError, ValueError):
                    pass
                rec.setdefault("history", []).append(
                    {"at": registry.iso(registry.now()), "ok": True,
                     "reply": "auto-revived after a usage limit"}
                )

    if rec.get("state") != "active":
        _save(rec)
        return False

    if rec.get("persistent"):
        if rec["ticks"] >= rec["max_ticks"]:
            rec["ticks"] = 0
        try:
            if registry.now() > datetime.datetime.fromisoformat(rec["deadline"]):
                rec["deadline"] = registry.iso(
                    registry.now() + datetime.timedelta(hours=settings.get("default_hours", 12))
                )
        except (KeyError, ValueError):
            pass
    else:
        try:
            if registry.now() > datetime.datetime.fromisoformat(rec["deadline"]):
                rec["state"] = "expired"
                notify.send(f"pilot {name} hit its deadline after {rec['ticks']} tick(s). "
                            f"Goal: {rec['goal'][:150]}")
                print(f"  {name}: deadline reached, pilot stopped")
                _save(rec)
                return False
        except (KeyError, ValueError):
            pass
        if rec["ticks"] >= rec["max_ticks"]:
            rec["state"] = "exhausted"
            notify.send(f"pilot {name} used all {rec['max_ticks']} ticks without finishing. "
                        f"Goal: {rec['goal'][:150]}")
            print(f"  {name}: tick budget exhausted, pilot stopped")
            _save(rec)
            return False

    if sessions.interactive_agent_pid(rec["cwd"]):
        print(f"  {name}: interactive agent is open in this tree, leaving it to the human")
        rec["quiet_ticks"] = 0
        _save(rec)
        return False

    if not just_stopped:
        mtime = sessions.newest_transcript_mtime(rec["cwd"], rec.get("session_id"))
        if mtime is not None:
            quiet = (registry.now().timestamp() - mtime) / 60
            if quiet < settings.get("idle_minutes", 8):
                print(f"  {name}: alive, transcript {quiet:.0f} min old, leaving it to work")
                return False

    if rec.get("session_id") and sessions.transcript_is_gone(rec["cwd"], rec["session_id"]):
        newer = sessions.newest_session(rec["cwd"])
        if newer and newer != rec["session_id"]:
            rec["session_id"] = newer

    pending = registry.inbox_pending(name)
    if not pending and not just_stopped:
        rec["quiet_ticks"] = rec.get("quiet_ticks", 0) + 1
        backoff = settings.get("idle_backoff", 6)
        if rec["quiet_ticks"] > 1 and (rec["quiet_ticks"] - 1) % backoff != 0:
            print(f"  {name}: idle backoff (quiet {rec['quiet_ticks']}), staying open")
            _save(rec)
            return False
    else:
        rec["quiet_ticks"] = 0

    queued = registry.inbox_drain(name)
    prompt = instruction.build_instruction(rec, queued)
    ok, reply = driver.resume(rec, prompt, settings)
    if not ok and queued:
        for q in queued:
            registry.inbox_add(name, q.get("text", ""))
        print(f"  {name}: {len(queued)} instruction(s) put back, the turn did not happen")

    rec["ticks"] = rec.get("ticks", 0) + 1
    rec.setdefault("history", []).append(
        {"at": registry.iso(registry.now()), "ok": ok, "reply": reply[:400]}
    )

    if instruction.DONE in reply:
        rec["state"] = "done"
        notify.send(f"pilot {name} DONE after {rec['ticks']} tick(s)\n{reply[:600]}")
        print(f"  {name}: reported done")
    elif instruction.BLOCKED in reply:
        rec["state"] = "blocked"
        notify.send(f"pilot {name} BLOCKED, needs you\n{reply[:600]}")
        print(f"  {name}: blocked, needs a human")
    elif not ok and "background agent" in reply:
        rec["ticks"] -= 1
        rec["history"].append(
            {"at": registry.iso(registry.now()), "ok": True,
             "reply": "session held by a background agent, alive, not resuming"}
        )
        print(f"  {name}: session runs as a background agent, leaving it to work")
    elif not ok:
        rec["state"] = "error"
        notify.send(f"pilot {name} could not be resumed\n{reply[:400]}")
        print(f"  {name}: resume failed, {reply[:120]}")
    else:
        print(f"  {name}: tick {rec['ticks']} done")
        est = sessions.estimate_tokens(rec["cwd"], rec["session_id"])
        if est >= settings.get("compact_tokens", 150000):
            rec["last_estimate"] = est
            if driver.compact(rec, settings):
                print(f"  {name}: compacted to a fresh session")
            else:
                print(f"  {name}: handoff refused or too thin, keeping the session")
        elif est:
            rec["last_estimate"] = est

    _save(rec)
    return True


def tick(settings: dict) -> int:
    """Advance every active pilot by one step. Returns a process exit code."""
    config.ensure_dirs()
    lock_fh = config.TICK_LOCK.open("w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("claude-pilot: another tick is in flight, skipping this one")
        return 0

    try:
        if config.WAKE.exists():
            for w in config.WAKE.glob("*.wake"):
                if not (config.PILOTS / f"{w.stem}.json").exists():
                    w.unlink(missing_ok=True)

        acted = 0
        for rec in sorted(registry.all_records(), key=lambda r: r.get("name", "")):
            if _tick_one(rec, settings):
                acted += 1

        if not acted:
            print("claude-pilot: no pilot needed a nudge")
        return 0
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()
