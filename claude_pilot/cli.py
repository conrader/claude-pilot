"""Command-line entry point: `claude-pilot <command> ...`."""
from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sys
from pathlib import Path

from . import config, registry, sessions, tick as tick_mod

_MANIFESTS = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Makefile")


def _looks_like_a_project(cwd: str) -> bool:
    p = Path(cwd)
    if (p / ".git").exists():
        return True
    return any((p / m).exists() for m in _MANIFESTS)


def cmd_start(args) -> int:
    """Enrol the session running in this directory as a pilot."""
    cwd = str(Path(args.cwd or os.getcwd()).resolve())

    if not _looks_like_a_project(cwd) and not args.force:
        print(f"claude-pilot: {cwd} does not look like a project tree.")
        print("  No git repo and no manifest. Run from the project's own directory,")
        print("  or pass --force if this is deliberate.")
        return 2

    known_ids = sessions.transcript_ids(cwd)
    sid = args.session_id
    if sid and sid not in known_ids:
        print(f"claude-pilot: {sid} is not a recorded session for {cwd}.")
        return 2
    if not sid:
        sid = sessions.newest_session(cwd)
    if not sid:
        print(f"claude-pilot: no transcript recorded for {cwd} yet.")
        print("  Run this from inside the session you want piloted, after it has")
        print("  made at least one tool call.")
        return 2

    name = args.name or registry.name_for_cwd(cwd)
    _, prior = registry.load(name)
    if prior and prior.get("state") == "stopped" and not args.force:
        print(f"claude-pilot: {name} was stopped at {prior.get('stopped_at', '?')} "
              f"by {prior.get('stopped_by', 'a human')} , a stop is final.")
        print("  Re-arm deliberately: claude-pilot start --force ...")
        return 3

    settings = config.settings()
    msg = registry.cap_block(name, args.allow_more, settings.get("cap", 3))
    if msg:
        print(msg)
        return 3

    now = registry.now()
    rec = {
        "name": name, "cwd": cwd, "session_id": sid, "goal": args.goal,
        "state": "active",
        "started_at": registry.iso(now),
        "active_since": registry.iso(now),
        "deadline": registry.iso(now + datetime.timedelta(hours=args.hours)),
        "max_ticks": args.ticks, "ticks": 0, "history": [],
        "persistent": bool(args.persistent),
        "quiet_ticks": 0,
        "previous_sessions": [],
        "compactions": 0,
    }
    registry.save(rec)

    print(f"claude-pilot: {name} enrolled")
    print(f"  session   {sid}")
    print(f"  goal      {args.goal}")
    print(f"  bounds    {args.ticks} ticks, until {rec['deadline']}")
    print("\n  The loop resumes this session when it goes quiet. To finish, say")
    print(f"  PILOT-DONE in the session, or run: claude-pilot stop {name}")
    return 0


def cmd_tick(args) -> int:
    """Drive every active pilot one step."""
    return tick_mod.tick(config.settings())


def cmd_status(args) -> int:
    """Print every enrolled pilot, or one by name."""
    records = registry.all_records()
    if args.name:
        records = [r for r in records if r.get("name") == args.name]
    if not records:
        print("claude-pilot: nothing enrolled")
        return 0
    for r in sorted(records, key=lambda r: r.get("name", "")):
        print(f"  {r['name']:<24} {r['state']:<10} tick {r.get('ticks', 0)}/{r.get('max_ticks', 0)} "
              f"until {r.get('deadline', '?')[:16]}")
        print(f"    goal: {r.get('goal', '')[:96]}")
        if r.get("history"):
            print(f"    last: {r['history'][-1].get('reply', '')[:96]}")
    return 0


def cmd_stop(args) -> int:
    """End a pilot. Final unless a later start/revive passes --force."""
    path, rec = registry.load(args.name)
    if rec is None:
        print(f"claude-pilot: {args.name} is not enrolled")
        return 1
    rec["state"] = "stopped"
    rec["stopped_at"] = registry.iso(registry.now())
    rec["stopped_by"] = os.environ.get("USER") or "human"
    registry.save(rec)
    print(f"claude-pilot: {args.name} stopped after {rec.get('ticks', 0)} tick(s)")
    return 0


def cmd_revive(args) -> int:
    """Put an error/expired/exhausted/blocked pilot back to active."""
    path, rec = registry.load(args.name)
    if rec is None:
        print(f"claude-pilot: {args.name} is not enrolled")
        return 1
    if rec.get("state") == "stopped" and not args.force:
        print(f"claude-pilot: {args.name} was stopped by {rec.get('stopped_by', 'a human')} "
              ", a stop is final; use --force to re-arm")
        return 3
    settings = config.settings()
    msg = registry.cap_block(args.name, args.allow_more, settings.get("cap", 3))
    if msg:
        print(msg)
        return 3
    was = rec.get("state")
    rec["state"] = "active"
    rec.pop("paused_reason", None)
    if was == "exhausted":
        # A revived pilot that kept its spent counter would be exhausted again on
        # its very next tick; reviving from exhaustion means a fresh budget.
        rec["ticks"] = 0
    rec["active_since"] = registry.iso(registry.now())
    rec["quiet_ticks"] = 0
    try:
        if registry.now() > datetime.datetime.fromisoformat(rec["deadline"]):
            rec["deadline"] = registry.iso(
                registry.now() + datetime.timedelta(hours=args.hours or settings.get("default_hours", 12))
            )
    except (KeyError, ValueError):
        pass
    if args.persistent:
        rec["persistent"] = True
    rec.setdefault("history", []).append(
        {"at": registry.iso(registry.now()), "ok": True, "reply": f"revived from {was}"}
    )
    registry.save(rec)
    print(f"claude-pilot: {args.name} {was} -> active"
          f"{' (persistent)' if rec.get('persistent') else ''}, until {rec['deadline']}")
    return 0


def cmd_goal(args) -> int:
    """Replace a running pilot's goal without resetting its tick counter."""
    path, rec = registry.load(args.name)
    if rec is None:
        print(f"claude-pilot: {args.name} is not enrolled")
        return 2
    old = rec.get("goal", "")
    rec["goal"] = args.goal
    rec.setdefault("goal_history", []).append({"at": registry.iso(registry.now()), "was": old})
    if rec.get("state") in ("done", "blocked", "error", "expired", "exhausted"):
        was_state = rec["state"]
        rec["state"] = "active"
        print(f"  (was {was_state}; a new goal reactivates it)")
    registry.save(rec)
    print(f"claude-pilot: {args.name} goal replaced\n  was: {old[:90]}\n  now: {args.goal[:90]}")
    return 0


def cmd_tell(args) -> int:
    """Queue an instruction a pilot picks up on its next tick."""
    text = (args.text or "").strip()
    if not text:
        print("claude-pilot tell: empty instruction")
        return 2
    registry.inbox_add(args.name, text)
    n = len(registry.inbox_pending(args.name))
    print(f"claude-pilot: queued for {args.name} ({n} pending)")
    config.WAKE.mkdir(parents=True, exist_ok=True)
    (config.WAKE / f"{args.name}.wake").touch()
    return 0


def cmd_inbox(args) -> int:
    """Read (optionally drain) queued instructions for a pilot."""
    items = registry.inbox_drain(args.name) if args.drain else registry.inbox_pending(args.name)
    if not items:
        print("(no new instructions)")
        return 0
    for it in items:
        print(f"  [{it.get('at', '?')}] {it.get('text', '')}")
    return 0


_TIMER_UNIT = """[Unit]
Description=claude-pilot periodic tick

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
"""

_SERVICE_UNIT = """[Unit]
Description=claude-pilot tick

[Service]
Type=oneshot
ExecStart={exe} tick
"""

_WAKE_PATH_UNIT = """[Unit]
Description=claude-pilot wake on session Stop

[Path]
PathExistsGlob={wake}/*.wake

[Install]
WantedBy=default.target
"""

_WAKE_SERVICE_UNIT = """[Unit]
Description=claude-pilot wake tick

[Service]
Type=oneshot
ExecStart={exe} tick
"""

_HOOKD_UNIT = """[Unit]
Description=claude-pilot hook receiver

[Service]
Type=simple
ExecStart={exe} hookd
Restart=on-failure

[Install]
WantedBy=default.target
"""


def _executable() -> str:
    return shutil.which("claude-pilot") or str(Path(sys.argv[0]).resolve())


def cmd_install_units(args) -> int:
    """Render (and optionally write) the four systemd user units."""
    exe = _executable()
    wake = str(config.WAKE)
    units = {
        "claude-pilot.timer": _TIMER_UNIT,
        "claude-pilot.service": _SERVICE_UNIT.format(exe=exe),
        "claude-pilot-wake.path": _WAKE_PATH_UNIT.format(wake=wake),
        "claude-pilot-wake.service": _WAKE_SERVICE_UNIT.format(exe=exe),
        "claude-pilot-hookd.service": _HOOKD_UNIT.format(exe=exe),
    }
    for fname, body in units.items():
        print(f"# {fname}")
        print(body)

    if args.write:
        target_dir = Path("~/.config/systemd/user").expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname, body in units.items():
            (target_dir / fname).write_text(body)
        import subprocess
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        print(f"claude-pilot: units written to {target_dir}")
    return 0


def cmd_hookd(args) -> int:
    """Delegate to the hook receiver's own entry point."""
    from . import hookd
    argv = ["claude-pilot-hookd"]
    if args.port is not None:
        argv += ["--port", str(args.port)]
    if args.bind:
        argv += ["--bind", args.bind]
    old_argv = sys.argv
    sys.argv = argv
    try:
        return hookd.main()
    finally:
        sys.argv = old_argv


def main(argv=None) -> int:
    """Parse argv and dispatch to the matching cmd_* function."""
    ap = argparse.ArgumentParser(prog="claude-pilot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("goal")
    s.add_argument("--cwd")
    s.add_argument("--session-id", dest="session_id")
    s.add_argument("--ticks", type=int, default=config.settings().get("default_ticks", 40))
    s.add_argument("--hours", type=int, default=config.settings().get("default_hours", 12))
    s.add_argument("--persistent", action="store_true")
    s.add_argument("--allow-more", dest="allow_more", action="store_true")
    s.add_argument("--force", action="store_true")
    s.add_argument("--name")
    s.set_defaults(fn=cmd_start)

    sub.add_parser("tick").set_defaults(fn=cmd_tick)

    st = sub.add_parser("status")
    st.add_argument("name", nargs="?")
    st.set_defaults(fn=cmd_status)

    sp = sub.add_parser("stop")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_stop)

    rv = sub.add_parser("revive")
    rv.add_argument("name")
    rv.add_argument("--persistent", action="store_true")
    rv.add_argument("--hours", type=int, default=None)
    rv.add_argument("--force", action="store_true")
    rv.add_argument("--allow-more", dest="allow_more", action="store_true")
    rv.set_defaults(fn=cmd_revive)

    g = sub.add_parser("goal")
    g.add_argument("name")
    g.add_argument("goal")
    g.set_defaults(fn=cmd_goal)

    tl = sub.add_parser("tell")
    tl.add_argument("name")
    tl.add_argument("text")
    tl.set_defaults(fn=cmd_tell)

    ib = sub.add_parser("inbox")
    ib.add_argument("name")
    ib.add_argument("--drain", action="store_true")
    ib.set_defaults(fn=cmd_inbox)

    hd = sub.add_parser("hookd")
    hd.add_argument("--port", type=int, default=None)
    hd.add_argument("--bind", default=None)
    hd.set_defaults(fn=cmd_hookd)

    iu = sub.add_parser("install-units")
    iu.add_argument("--write", action="store_true")
    iu.set_defaults(fn=cmd_install_units)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
