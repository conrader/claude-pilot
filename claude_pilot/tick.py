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

from . import agents, brain, config, driver, instruction, notify, registry, sessions, wake

_LIMIT_RE = re.compile(r"usage limit|rate limit|session limit|resets \d", re.I)


def _save(rec: dict) -> None:
    registry.save(rec)


def _tick_one(rec: dict, settings: dict) -> bool:
    """Advance one record by a tick. Returns True if a turn was spent."""
    try:
        return _advance(rec, settings)
    except registry.ConflictError:
        print(f"  {rec['name']}: state changed, preserving the newer record")
        return False


def _desktop_delivery(rec: dict, agent, host: str) -> bool:
    """Return True if waiting or terminal; inspect receipts before new bounds."""
    delivery = rec.get("_queued_continuation")
    if not delivery:
        return False
    name = rec["name"]
    activity = agent.desktop_info(rec["cwd"], rec["session_id"], host)
    if (activity.get("active") is not False or not activity.get("turn_id")
            or activity["turn_id"] == delivery.get("after_turn_id")):
        print(f"  {name}: a desktop continuation is outstanding, waiting for delivery")
        return True
    registry.inbox_ack(name, delivery.get("inbox_ids", []))
    rec.pop("_queued_continuation")
    reply = activity.get("reply", "")
    outcome = instruction.outcome(reply) if delivery.get("goal") == rec["goal"] else None
    if outcome:
        rec["state"] = outcome
        rec.setdefault("history", []).append(
            {"at": registry.iso(registry.now()), "ok": True, "reply": reply[:400]})
    _save(rec)
    if outcome:
        notify.send(f"pilot {name} {outcome.upper()} after desktop continuation\n{reply[:600]}")
        return True
    return False


def _advance(rec: dict, settings: dict) -> bool:
    name = rec["name"]
    host = rec.get("host", "")
    agent = agents.for_record(rec)
    event = wake.consume(name)
    just_stopped = (event.get("kind") == "stop"
                    and rec.get("agent", "claude") == "claude"
                    and bool(rec.get("session_id"))
                    and event.get("session_id") == rec["session_id"])

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

    if _desktop_delivery(rec, agent, host):
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
                _save(rec)
                notify.send(f"pilot {name} hit its deadline after {rec['ticks']} tick(s). "
                            f"Goal: {rec['goal'][:150]}")
                print(f"  {name}: deadline reached, pilot stopped")
                return False
        except (KeyError, ValueError):
            pass
        if rec["ticks"] >= rec["max_ticks"]:
            rec["state"] = "exhausted"
            _save(rec)
            notify.send(f"pilot {name} used all {rec['max_ticks']} ticks without finishing. "
                        f"Goal: {rec['goal'][:150]}")
            print(f"  {name}: tick budget exhausted, pilot stopped")
            return False

    # Remote TTYs are not visible from here; the interactive guard only
    # makes sense for a local record, where /proc actually tells the truth.
    interactive = None
    if not host:
        if rec.get("agent") == "codex":
            interactive = sessions.interactive_agent_pid(rec["cwd"], agent="codex")
        else:
            interactive = sessions.interactive_agent_pid(rec["cwd"])
    if interactive:
        print(f"  {name}: interactive agent is open in this tree, leaving it to the human")
        rec["quiet_ticks"] = 0
        _save(rec)
        return False

    if rec.get("agent") == "codex" and agent.turn_in_progress(
            rec["cwd"], rec.get("session_id"), host):
        print(f"  {name}: Codex has an unfinished turn, leaving it to work")
        return False

    # A codex record is normally driven by the Stop hook's synchronous
    # continuation; the tick only steps in as a fallback once the rollout
    # file has gone quiet, so this check keeps it from ever racing that
    # loop, exactly as it keeps a claude tick from interrupting live work.
    if not just_stopped:
        mtime = agent.newest_transcript_mtime(rec["cwd"], rec.get("session_id"), host)
        if mtime is not None:
            quiet = (registry.now().timestamp() - mtime) / 60
            if quiet < settings.get("idle_minutes", 8):
                print(f"  {name}: alive, transcript {quiet:.0f} min old, leaving it to work")
                return False

    if rec.get("session_id") and agent.transcript_is_gone(rec["cwd"], rec["session_id"], host):
        newer = agent.newest_session(rec["cwd"], host)
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

    # Ask the judge before spending a turn: a supervisor that already knows
    # the session is off goal should stop it here, not one turn later.
    last_reply = rec["history"][-1].get("reply", "") if rec.get("history") else ""
    gate = brain.run_judge(rec, last_reply, settings, phase="before")
    if gate.get("verdict") == "pause":
        rec["state"] = "paused"
        rec["paused_reason"] = gate.get("reason", "")
        _save(rec)
        notify.send(f"pilot {name} paused by judge: {gate.get('reason', '')}")
        print(f"  {name}: paused by judge before the turn, {gate.get('reason', '')[:120]}")
        return False

    queued = registry.inbox_pending(name)
    context = brain.fetch_context(rec, settings)
    prompt = instruction.build_instruction(rec, queued, context)
    # Reserve the budget before launching. A concurrent stop/goal update is
    # either seen here or preserved when the eventual result is committed.
    rec["ticks"] = rec.get("ticks", 0) + 1
    _save(rec)
    ok, reply = driver.resume(rec, prompt, settings)
    queued_to_desktop = ok and "_queued_continuation" in rec
    if queued_to_desktop:
        rec["_queued_continuation"]["inbox_ids"] = [q["id"] for q in queued]
    elif ok:
        registry.inbox_ack(name, [q["id"] for q in queued])
    elif queued:
        print(f"  {name}: {len(queued)} instruction(s) remain pending")

    if not registry.is_current(rec):
        print(f"  {name}: state changed during the turn, preserving the newer record")
        return True

    rec.setdefault("history", []).append(
        {"at": registry.iso(registry.now()), "ok": ok, "reply": reply[:400]}
    )

    if queued_to_desktop:
        _save(rec)
        print(f"  {name}: continuation queued; waiting for the desktop turn")
        return True

    outcome = instruction.outcome(reply) if ok else None
    note = ""
    if outcome == "done":
        rec["state"] = "done"
        note = f"pilot {name} DONE after {rec['ticks']} tick(s)\n{reply[:600]}"
        print(f"  {name}: reported done")
    elif outcome == "blocked":
        rec["state"] = "blocked"
        note = f"pilot {name} BLOCKED, needs you\n{reply[:600]}"
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
        note = f"pilot {name} could not be resumed\n{reply[:400]}"
        print(f"  {name}: resume failed, {reply[:120]}")
    else:
        verdict = brain.run_judge(rec, reply, settings)
        if verdict.get("verdict") == "pause":
            rec["state"] = "paused"
            rec["paused_reason"] = verdict.get("reason", "")
            _save(rec)
            notify.send(f"pilot {name} paused by judge: {verdict.get('reason', '')}")
            print(f"  {name}: paused by judge, {verdict.get('reason', '')[:120]}")
            return True
        print(f"  {name}: tick {rec['ticks']} done")
        est = agent.estimate_tokens(rec["cwd"], rec["session_id"], host)
        if est >= settings.get("compact_tokens", 150000):
            rec["last_estimate"] = est
            _save(rec)
            if driver.compact(rec, settings):
                print(f"  {name}: compacted to a fresh session")
            else:
                print(f"  {name}: handoff refused or too thin, keeping the session")
        elif est:
            rec["last_estimate"] = est

    _save(rec)
    if note:
        notify.send(note)
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
