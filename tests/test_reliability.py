"""Regressions for concurrent operator commands and durable instruction delivery."""
import datetime
import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from claude_pilot import cli, config, registry, storage, tick, wake


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(tmp_path / "absent.json"))
    config.reload()
    config.ensure_dirs()
    rec = dict(name="demo", cwd=str(tmp_path), session_id="sid", agent="claude",
               host="", state="active", goal="original goal", ticks=0, max_ticks=5,
               deadline=registry.iso(registry.now() + datetime.timedelta(hours=1)),
               quiet_ticks=0, history=[])
    registry.save(rec)
    adapter = SimpleNamespace(newest_transcript_mtime=lambda *a: 0,
                              transcript_is_gone=lambda *a: False,
                              estimate_tokens=lambda *a: 0,
                              turn_in_progress=lambda *a: False)
    monkeypatch.setattr(tick.agents, "for_record", lambda r: adapter)
    monkeypatch.setattr(tick.sessions, "interactive_agent_pid", lambda *a, **k: None)
    monkeypatch.setattr(tick.notify, "send", lambda *a: None)
    return rec, adapter


@pytest.mark.parametrize("command,expected", [(["stop", "demo"], "stopped"),
                                            (["goal", "demo", "new goal"], "active")])
def test_operator_change_survives_inflight_completion(pilot, monkeypatch, command, expected):
    rec, _ = pilot
    def resume(*args):
        assert cli.main(command) == 0
        return True, "PILOT-DONE\nfinished the old goal"
    monkeypatch.setattr(tick.driver, "resume", resume)
    tick.tick(config.settings())
    saved = registry.load("demo")[1]
    assert saved["state"] == expected
    assert saved["ticks"] == 1  # The spent turn survives the operator edit too.
    if command[0] == "goal":
        assert saved["goal"] == "new goal"


def test_stop_during_context_fetch_prevents_launch(pilot, monkeypatch):
    def context(*args):
        cli.main(["stop", "demo"])
        return "context"
    monkeypatch.setattr(tick.brain, "fetch_context", context)
    monkeypatch.setattr(tick.driver, "resume", lambda *a: pytest.fail("resumed after stop"))
    tick.tick(config.settings())
    assert registry.load("demo")[1]["state"] == "stopped"
    assert registry.load("demo")[1]["ticks"] == 0


def test_stale_record_cannot_overwrite_newer_revision(pilot):
    rec, _ = pilot
    newer = registry.load("demo")[1]
    newer["state"] = "stopped"
    registry.save(newer)
    rec["state"] = "done"
    with pytest.raises(registry.ConflictError):
        registry.save(rec)
    assert registry.load("demo")[1]["state"] == "stopped"


@pytest.mark.parametrize("ok,reply,state", [
    (True, "Cannot claim PILOT-DONE; unfinished.", "active"),
    (True, "The instructions mention PILOT-BLOCKED.", "active"),
    (False, "PILOT-DONE\nold transcript", "error"),
    (False, "PILOT-BLOCKED\nold transcript", "error"),
    (True, "PILOT-DONE\nverified", "done"),
])
def test_only_successful_explicit_status_changes_state(pilot, monkeypatch, ok, reply, state):
    monkeypatch.setattr(tick.driver, "resume", lambda *a: (ok, reply))
    tick.tick(config.settings())
    assert registry.load("demo")[1]["state"] == state


@pytest.mark.parametrize("agent,host", [("claude", ""), ("codex", ""), ("codex", "remote")])
def test_tell_does_not_bypass_live_transcript(pilot, monkeypatch, agent, host):
    rec, adapter = pilot
    rec.update(agent=agent, host=host)
    registry.save(rec)
    adapter.newest_transcript_mtime = lambda *a: registry.now().timestamp()
    cli.main(["tell", "demo", "next turn only"])
    monkeypatch.setattr(tick.driver, "resume", lambda *a: pytest.fail("raced a live turn"))
    tick.tick(config.settings())
    assert registry.load("demo")[1]["ticks"] == 0
    assert len(registry.inbox_pending("demo")) == 1
    assert not (config.WAKE / "demo.wake").exists()


@pytest.mark.parametrize("sid,should_resume", [("sid", True), ("unrelated-session", False)])
def test_only_matching_stop_bypasses_claude_quiet_window(pilot, monkeypatch, sid, should_resume):
    rec, adapter = pilot
    adapter.newest_transcript_mtime = lambda *a: registry.now().timestamp()
    wake.send("demo", "stop", sid)
    calls = []
    monkeypatch.setattr(tick.driver, "resume", lambda *a: (calls.append(1) is None, "working"))
    tick.tick(config.settings())
    assert bool(calls) == should_resume


def test_inbox_delivery_ack_preserves_new_instructions(pilot, monkeypatch):
    registry.inbox_add("demo", "first")
    delivered = registry.inbox_pending("demo")
    def resume(rec, prompt, settings):
        assert "first" in prompt
        assert registry.inbox_pending("demo")[0]["id"] == delivered[0]["id"]
        registry.inbox_add("demo", "arrived during the turn")
        return True, "working"
    monkeypatch.setattr(tick.driver, "resume", resume)
    tick.tick(config.settings())
    assert [r["text"] for r in registry.inbox_pending("demo")] == ["arrived during the turn"]


def test_interrupted_resume_keeps_durable_input(pilot, monkeypatch):
    registry.inbox_add("demo", "must survive a crash")
    before = registry.inbox_pending("demo")
    def interrupted(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(tick.driver, "resume", interrupted)
    with pytest.raises(KeyboardInterrupt):
        tick.tick(config.settings())
    assert registry.inbox_pending("demo") == before


def test_append_from_another_process_survives_drain(pilot, monkeypatch):
    registry.inbox_add("demo", "first")
    rewriting, release = threading.Event(), threading.Event()
    original = registry._write_rows
    def delayed_write(*args):
        rewriting.set()
        assert release.wait(5)
        original(*args)
    monkeypatch.setattr(registry, "_write_rows", delayed_write)
    drain = threading.Thread(target=registry.inbox_drain, args=("demo",))
    drain.start()
    assert rewriting.wait(5)
    code = "from claude_pilot import registry; print('ready', flush=True); registry.inbox_add('demo', 'second')"
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        release.set()
        drain.join(5)
        _, errors = child.communicate(timeout=5)
        assert child.returncode == 0, errors
        assert not drain.is_alive()
        assert [r["text"] for r in registry.inbox_pending("demo")] == ["second"]
    finally:
        release.set()
        drain.join(5)
        if child.poll() is None:
            child.kill()
            child.wait()


def test_atomic_write_failure_leaves_record_readable(pilot, monkeypatch):
    rec, _ = pilot
    before = registry.load("demo")[1]
    monkeypatch.setattr(storage.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk error")))
    rec["state"] = "stopped"
    with pytest.raises(OSError):
        registry.save(rec)
    assert registry.load("demo")[1] == before


def test_legacy_inbox_ids_survive_repeated_snapshots(pilot):
    registry.inbox_path("demo").write_text(json.dumps({"text": "legacy", "consumed": False}) + "\n")
    pending = registry.inbox_pending("demo")
    assert registry.inbox_pending("demo") == pending
    registry.inbox_ack("demo", [pending[0]["id"]])
    assert registry.inbox_pending("demo") == []
