"""Desktop and rollout lifecycle guards: an open turn must never receive a tick."""
import datetime
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_pilot import cli, config, registry, tick, wake
from claude_pilot.agents import codex


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(tmp_path / "pilot"))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(tmp_path / "config.json"))
    config.reload()
    config.ensure_dirs()
    return tmp_path


def record(root):
    rec = dict(name="demo", cwd=str(root / "repo"), session_id="session", agent="codex",
               state="active", goal="finish", ticks=0, max_ticks=5, quiet_ticks=0,
               history=[], deadline=registry.iso(registry.now() + datetime.timedelta(hours=1)))
    registry.save(rec)
    return rec


def history(root, status="completed", turn_id="previous"):
    home = root / "codex"
    home.mkdir(parents=True)
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT,cwd TEXT,updated_at_ms INTEGER,history_mode TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?,?,?)", ("session", str(root / "repo"), 125000, "paginated"))
    with sqlite3.connect(home / "thread_history_1.sqlite") as db:
        db.execute("CREATE TABLE thread_turns(thread_id TEXT,status TEXT,rollout_ordinal INTEGER,turn_id TEXT)")
        db.execute("INSERT INTO thread_turns VALUES(?,?,?,?)", ("session", status, 1, turn_id))


@pytest.mark.parametrize("status,busy", [("inProgress", True), ("completed", False), ("interrupted", False)])
def test_desktop_activity_uses_explicit_status(env, status, busy):
    history(env, status)
    assert codex.turn_in_progress(str(env / "repo"), "session", "") is busy
    mtime = codex.newest_transcript_mtime(str(env / "repo"), "session", "")
    assert mtime > 125 if busy else mtime == 125


def test_other_project_does_not_claim_desktop_activity(env):
    history(env, "inProgress")
    assert codex.newest_transcript_mtime(str(env / "other"), "session", "") is None


def test_absent_desktop_history_has_no_activity(env):
    assert codex.newest_transcript_mtime(str(env / "repo"), "session", "") is None


def test_unreadable_paginated_history_defers_resume(env, monkeypatch):
    history(env)
    (env / "codex/thread_history_1.sqlite").unlink()
    monkeypatch.setattr(codex.transport, "run", lambda *a, **k: pytest.fail("launched with unknown activity"))
    ok, reply = codex.resume(record(env), "continue", {})
    assert not ok and "background agent" in reply


def test_active_desktop_turn_blocks_even_stop_wake(env, monkeypatch):
    history(env, "inProgress")
    record(env)
    wake.send("demo", "stop", "session")
    monkeypatch.setattr(tick.sessions, "interactive_agent_pid", lambda *a, **k: None)
    monkeypatch.setattr(codex.transport, "run", lambda *a, **k: pytest.fail("queued into an active turn"))
    tick.tick(config.settings())
    assert registry.load("demo")[1]["ticks"] == 0


def test_desktop_queue_is_not_duplicated_and_ack_waits_for_delivery(env, monkeypatch):
    history(env)
    record(env)
    registry.inbox_add("demo", "operator instruction")
    calls = []
    def run(host, argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="queued", stderr="")
    monkeypatch.setattr(codex.transport, "run", run)
    monkeypatch.setattr(tick.sessions, "interactive_agent_pid", lambda *a, **k: None)
    tick.tick(config.settings())
    assert calls[0][:4] == ["codex", "queue", "--thread", "session"]
    assert len(registry.inbox_pending("demo")) == 1
    cli.main(["tell", "demo", "arrived after queueing"])
    tick.tick(config.settings())
    assert len(calls) == 1
    assert registry.load("demo")[1]["ticks"] == 1
    assert len(registry.inbox_pending("demo")) == 2
    # A real subsequent Stop acknowledges the first delivery, then reports done.
    monkeypatch.setattr(codex.notify, "send", lambda *a: None)
    codex.stop_decision({"session_id": "session", "last_assistant_message": "PILOT-DONE"}, {})
    assert [q["text"] for q in registry.inbox_pending("demo")] == ["arrived after queueing"]
    assert registry.load("demo")[1]["state"] == "done"


def test_newer_completed_row_does_not_hide_an_open_desktop_turn(env):
    history(env, "inProgress")
    with sqlite3.connect(env / "codex/thread_history_1.sqlite") as db:
        db.execute("INSERT INTO thread_turns VALUES('session','completed',2,'other')")
    assert codex.turn_in_progress(str(env / "repo"), "session", "") is True


@pytest.mark.parametrize("goal_changed,expected", [(False, "done"), (True, "exhausted")])
def test_desktop_completion_is_read_before_exhausting_budget(env, monkeypatch, goal_changed, expected):
    history(env, "completed", "delivered-turn")
    rec = record(env)
    rec.update(ticks=5, _queued_continuation={"after_turn_id": "previous", "goal": "finish"})
    if goal_changed:
        rec["goal"] = "different goal"
    registry.save(rec)
    with sqlite3.connect(env / "codex/thread_history_1.sqlite") as db:
        db.execute("ALTER TABLE thread_turns ADD COLUMN final_agent_item_id TEXT")
        db.execute("UPDATE thread_turns SET final_agent_item_id='final'")
        db.execute("CREATE TABLE thread_items(thread_id TEXT,item_id TEXT,item_json TEXT)")
        db.execute("INSERT INTO thread_items VALUES(?,?,?)",
                   ("session", "final", json.dumps({"type": "agentMessage", "text": "PILOT-DONE\nverified"})))
    monkeypatch.setattr(tick.notify, "send", lambda *a: None)
    monkeypatch.setattr(tick.driver, "resume", lambda *a: pytest.fail("unexpected extra turn"))
    tick.tick(config.settings())
    assert registry.load("demo")[1]["state"] == expected


@pytest.mark.parametrize("end,expected", [(None, True), ("task_complete", False), ("turn_aborted", False)])
def test_old_rollout_with_open_turn_stays_busy(env, end, expected):
    path = env / "codex/sessions/rollout-session.jsonl"
    path.parent.mkdir(parents=True)
    events = [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t"}}]
    if end:
        events.append({"type": "event_msg", "payload": {"type": end, "turn_id": "t"}})
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    os.utime(path, (100, 100))
    assert codex.turn_in_progress(str(env / "repo"), "session", "") is expected


def test_remote_probe_uses_remote_desktop_database(env, monkeypatch):
    history(env, "inProgress")
    def python_on(host, script, *args, **kwargs):
        assert host == "remote"
        return subprocess.run([sys.executable, "-c", script, *args[:-1], str(env / "codex")],
                              capture_output=True, text=True)
    monkeypatch.setattr(codex.transport, "python_on", python_on)
    assert codex.turn_in_progress(str(env / "repo"), "session", "remote") is True


def test_real_hook_and_controller_keep_json_clean_and_input_durable(env, monkeypatch):
    record(env)
    config.CONFIG_FILE.write_text(json.dumps({"judge_command": "printf not-json"}))
    registry.inbox_add("demo", "must be delivered")
    hook_config = env / ".config/claude-pilot/codex-hook.json"
    hook_config.parent.mkdir(parents=True)
    hook_config.write_text(json.dumps({"hook_url": "http://127.0.0.1:1/hook",
                                      "controller": [sys.executable, "-m", "claude_pilot.cli"]}))
    monkeypatch.setenv("HOME", str(env))
    script = Path(__file__).parents[1] / "contrib/codex/claude_pilot_codex_hook.py"
    def stop(message):
        result = subprocess.run([sys.executable, str(script)], text=True, capture_output=True,
                                input=json.dumps({"session_id": "session", "hook_event_name": "Stop",
                                                  "last_assistant_message": message}), timeout=10)
        assert result.returncode == 0
        return json.loads(result.stdout)
    decision = stop("partial progress")
    assert decision["decision"] == "block" and "must be delivered" in decision["reason"]
    assert len(registry.inbox_pending("demo")) == 1
    assert stop("PILOT-DONE\nverified") == {}
    assert registry.load("demo")[1]["state"] == "done"
    assert registry.inbox_pending("demo") == []
