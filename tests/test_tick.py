"""Tests for claude_pilot.tick."""
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from claude_pilot import config, registry, tick as tick_mod


def _fake_agent(tmp_path, name="claude"):
    script = tmp_path / "bin" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/sh\n"
        'if [ -n "$PILOT_FAKE_FAIL" ]; then\n'
        '  echo "background agent" 1>&2\n'
        "  exit 1\n"
        "fi\n"
        'echo "$PILOT_FAKE_REPLY"\n'
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(home))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(tmp_path / "nope.json"))
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    config.reload()
    config.ensure_dirs()
    return tmp_path


def _make_project(tmp_path, name):
    cwd = tmp_path / "proj" / name
    cwd.mkdir(parents=True, exist_ok=True)
    return str(cwd)


def _make_transcript(tmp_path, cwd, session_id, mtime):
    from claude_pilot import sessions
    slug = sessions.transcript_slug(cwd)
    d = tmp_path / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{session_id}.jsonl"
    f.write_text(json.dumps({"type": "assistant", "message": {"content": "hi"}}) + "\n")
    os.utime(f, (mtime, mtime))
    return f


def _base_rec(name, cwd, session_id, **overrides):
    now = registry.now()
    rec = {
        "name": name, "cwd": cwd, "session_id": session_id, "goal": "do stuff",
        "state": "active",
        "started_at": registry.iso(now), "active_since": registry.iso(now),
        "deadline": registry.iso(now + __import__("datetime").timedelta(hours=1)),
        "max_ticks": 10, "ticks": 0, "history": [], "persistent": False,
        "quiet_ticks": 0,
    }
    rec.update(overrides)
    return rec


def _settings(agent_path, **overrides):
    s = {
        "agent_command": agent_path, "model": "", "idle_minutes": 8,
        "idle_backoff": 6, "default_hours": 12, "compact_tokens": 150000,
        "resume_timeout_s": 30,
    }
    s.update(overrides)
    return s


def test_stray_wake_marker_removed(env):
    config.WAKE.mkdir(parents=True, exist_ok=True)
    (config.WAKE / "ghost.wake").touch()
    tick_mod.tick(_settings("claude"))
    assert not (config.WAKE / "ghost.wake").exists()


def test_wake_marker_bypasses_idle_check_even_for_non_active(env, tmp_path, monkeypatch):
    agent = _fake_agent(tmp_path)
    monkeypatch.setenv("PILOT_FAKE_REPLY", "still working")
    cwd = _make_project(tmp_path, "p1")
    _make_transcript(tmp_path, cwd, "sess-1", time.time())
    rec = _base_rec("p1", cwd, "sess-1", state="blocked")
    registry.save(rec)
    config.WAKE.mkdir(parents=True, exist_ok=True)
    marker = config.WAKE / "p1.wake"
    marker.touch()
    tick_mod.tick(_settings(agent))
    # marker is consumed even though the record stayed blocked (state != active)
    assert not marker.exists()
    _, saved = registry.load("p1")
    assert saved["state"] == "blocked"


def test_deadline_expiry(env, tmp_path, monkeypatch):
    agent = _fake_agent(tmp_path)
    cwd = _make_project(tmp_path, "p2")
    _make_transcript(tmp_path, cwd, "sess-2", time.time() - 3600)
    past = registry.iso(registry.now() - __import__("datetime").timedelta(hours=1))
    rec = _base_rec("p2", cwd, "sess-2", deadline=past)
    registry.save(rec)
    tick_mod.tick(_settings(agent))
    _, saved = registry.load("p2")
    assert saved["state"] == "expired"


def test_tick_exhaustion(env, tmp_path):
    agent = _fake_agent(tmp_path)
    cwd = _make_project(tmp_path, "p3")
    _make_transcript(tmp_path, cwd, "sess-3", time.time() - 3600)
    rec = _base_rec("p3", cwd, "sess-3", ticks=10, max_ticks=10)
    registry.save(rec)
    tick_mod.tick(_settings(agent))
    _, saved = registry.load("p3")
    assert saved["state"] == "exhausted"


def test_persistent_renews_instead_of_exhausting(env, tmp_path, monkeypatch):
    agent = _fake_agent(tmp_path)
    monkeypatch.setenv("PILOT_FAKE_REPLY", "nothing new")
    cwd = _make_project(tmp_path, "p4")
    _make_transcript(tmp_path, cwd, "sess-4", time.time() - 3600)
    rec = _base_rec("p4", cwd, "sess-4", ticks=10, max_ticks=10, persistent=True)
    registry.save(rec)
    tick_mod.tick(_settings(agent))
    _, saved = registry.load("p4")
    assert saved["state"] == "active"
    assert saved["ticks"] < 10 or saved["ticks"] == 1


def test_done_reply_sets_state_done(env, tmp_path, monkeypatch):
    agent = _fake_agent(tmp_path)
    monkeypatch.setenv("PILOT_FAKE_REPLY", "PILOT-DONE\nall good")
    cwd = _make_project(tmp_path, "p5")
    _make_transcript(tmp_path, cwd, "sess-5", time.time() - 3600)
    rec = _base_rec("p5", cwd, "sess-5")
    registry.save(rec)
    tick_mod.tick(_settings(agent))
    _, saved = registry.load("p5")
    assert saved["state"] == "done"


def test_blocked_reply_sets_state_blocked(env, tmp_path, monkeypatch):
    agent = _fake_agent(tmp_path)
    monkeypatch.setenv("PILOT_FAKE_REPLY", "PILOT-BLOCKED need input")
    cwd = _make_project(tmp_path, "p6")
    _make_transcript(tmp_path, cwd, "sess-6", time.time() - 3600)
    rec = _base_rec("p6", cwd, "sess-6")
    registry.save(rec)
    tick_mod.tick(_settings(agent))
    _, saved = registry.load("p6")
    assert saved["state"] == "blocked"


def test_idle_backoff_resumes_on_first_and_every_nth(env, tmp_path, monkeypatch):
    agent = _fake_agent(tmp_path)
    monkeypatch.setenv("PILOT_FAKE_REPLY", "nothing new, quiet tick")
    cwd = _make_project(tmp_path, "p7")
    _make_transcript(tmp_path, cwd, "sess-7", time.time() - 3600)
    rec = _base_rec("p7", cwd, "sess-7", quiet_ticks=0)
    registry.save(rec)
    settings = _settings(agent, idle_backoff=3)

    # tick 1: quiet_ticks becomes 1 -> resumes (quiet_ticks==1 rule)
    tick_mod.tick(settings)
    _, saved = registry.load("p7")
    assert saved["ticks"] == 1
    assert saved["quiet_ticks"] == 1

    # tick 2: quiet_ticks becomes 2 -> (2-1)%3 != 0 -> stays open, no resume
    _make_transcript(tmp_path, cwd, saved["session_id"], time.time() - 3600)
    tick_mod.tick(settings)
    _, saved2 = registry.load("p7")
    assert saved2["ticks"] == 1
    assert saved2["quiet_ticks"] == 2

    # tick 3: quiet_ticks becomes 3 -> (3-1)%3 != 0 -> still no resume
    tick_mod.tick(settings)
    _, saved3 = registry.load("p7")
    assert saved3["ticks"] == 1
    assert saved3["quiet_ticks"] == 3

    # tick 4: quiet_ticks becomes 4 -> (4-1)%3 == 0 -> resumes
    tick_mod.tick(settings)
    _, saved4 = registry.load("p7")
    assert saved4["ticks"] == 2
    # per spec quiet_ticks is only reset to 0 when something new arrived
    # (pending instructions or a wake marker); a threshold-triggered resume
    # keeps counting so the next backoff window is measured from here.
    assert saved4["quiet_ticks"] == 4


def test_failed_resume_with_queued_inbox_puts_instructions_back(env, tmp_path, monkeypatch):
    agent = _fake_agent(tmp_path)
    monkeypatch.setenv("PILOT_FAKE_FAIL", "1")
    cwd = _make_project(tmp_path, "p8")
    _make_transcript(tmp_path, cwd, "sess-8", time.time() - 3600)
    rec = _base_rec("p8", cwd, "sess-8")
    registry.save(rec)
    registry.inbox_add("p8", "do the important thing")
    assert len(registry.inbox_pending("p8")) == 1

    tick_mod.tick(_settings(agent))
    # resume failed with "background agent" text -> ticks stay, but the queued
    # instruction should be put back regardless of the failure branch taken
    assert len(registry.inbox_pending("p8")) == 1


class _FakeAgent:
    """A minimal fake `agents` module the tests can drive precisely."""

    def __init__(self, mtime=None, ok=True, reply="fine"):
        self.mtime = mtime
        self.ok = ok
        self.reply = reply
        self.resume_calls = 0

    def for_record(self, rec):
        return self

    def newest_transcript_mtime(self, cwd, session_id, host):
        return self.mtime

    def transcript_is_gone(self, cwd, session_id, host):
        return False

    def newest_session(self, cwd, host):
        return None

    def resume(self, rec, prompt, settings):
        self.resume_calls += 1
        self.last_prompt = prompt
        return self.ok, self.reply

    def estimate_tokens(self, cwd, session_id, host):
        return 0


def test_remote_record_skips_interactive_guard(env, tmp_path, monkeypatch):
    cwd = _make_project(tmp_path, "premote")
    fake = _FakeAgent(mtime=None, ok=True, reply="done here")
    monkeypatch.setattr(tick_mod, "agents", fake)
    monkeypatch.setattr(tick_mod.driver, "resume", fake.resume)
    monkeypatch.setattr(
        tick_mod.sessions, "interactive_agent_pid", lambda cwd_: 12345
    )
    rec = _base_rec("premote", cwd, "sess-r", host="box1", agent="claude")
    registry.save(rec)

    tick_mod.tick(_settings("claude"))
    assert fake.resume_calls == 1
    _, saved = registry.load("premote")
    assert saved["ticks"] == 1


def test_local_record_still_honours_interactive_guard(env, tmp_path, monkeypatch):
    cwd = _make_project(tmp_path, "plocal")
    fake = _FakeAgent(mtime=None, ok=True, reply="done here")
    monkeypatch.setattr(tick_mod, "agents", fake)
    monkeypatch.setattr(tick_mod.driver, "resume", fake.resume)
    monkeypatch.setattr(
        tick_mod.sessions, "interactive_agent_pid", lambda cwd_: 12345
    )
    rec = _base_rec("plocal", cwd, "sess-l")
    registry.save(rec)

    tick_mod.tick(_settings("claude"))
    assert fake.resume_calls == 0


def test_context_command_text_lands_in_the_instruction(env, tmp_path, monkeypatch):
    cwd = _make_project(tmp_path, "pctx")
    fake = _FakeAgent(mtime=None, ok=True, reply="ok")
    monkeypatch.setattr(tick_mod, "agents", fake)
    monkeypatch.setattr(tick_mod.driver, "resume", fake.resume)
    ctx_script = tmp_path / "ctx.sh"
    ctx_script.write_text("#!/bin/sh\ncat >/dev/null\necho 'a red alert from the ledger'\n")
    ctx_script.chmod(0o755)
    rec = _base_rec("pctx", cwd, "sess-c")
    registry.save(rec)

    tick_mod.tick(_settings("claude", context_command=str(ctx_script)))
    assert "a red alert from the ledger" in fake.last_prompt
    assert "Context from your operator's system:" in fake.last_prompt


def test_judge_pause_stops_further_resumes(env, tmp_path, monkeypatch):
    cwd = _make_project(tmp_path, "pjudge")
    fake = _FakeAgent(mtime=None, ok=True, reply="ok")
    monkeypatch.setattr(tick_mod, "agents", fake)
    monkeypatch.setattr(tick_mod.driver, "resume", fake.resume)
    judge_script = tmp_path / "judge.sh"
    judge_script.write_text(
        "#!/bin/sh\ncat >/dev/null\necho '{\"verdict\": \"pause\", \"reason\": \"off track\"}'\n"
    )
    judge_script.chmod(0o755)
    rec = _base_rec("pjudge", cwd, "sess-j")
    registry.save(rec)

    tick_mod.tick(_settings("claude", judge_command=str(judge_script)))
    _, saved = registry.load("pjudge")
    assert saved["state"] == "paused"
    assert saved["paused_reason"] == "off track"

    tick_mod.tick(_settings("claude", judge_command=str(judge_script)))
    assert fake.resume_calls == 0  # an always-pausing judge parks the pilot before any turn is spent


def test_tick_lock_skips_concurrent_tick(env, tmp_path):
    import fcntl
    config.TICK_LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = config.TICK_LOCK.open("w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = tick_mod.tick(_settings("claude"))
        assert rc == 0
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def test_judge_before_the_turn_pauses_without_spending_it(env, tmp_path, monkeypatch, capsys):
    """A 'before' verdict of pause parks the pilot and the agent is never resumed."""
    from claude_pilot import registry, tick, brain
    rec = {"name": "gated", "cwd": str(tmp_path), "session_id": "11111111-1111-1111-1111-111111111111",
           "agent": "claude", "goal": "g", "state": "active", "ticks": 1, "max_ticks": 5,
           "deadline": "2999-01-01T00:00:00+00:00", "history": [{"at": "2026-01-01T00:00:00+00:00", "ok": True, "reply": "prev"}],
           "persistent": False, "quiet_ticks": 0}
    registry.save(rec)
    seen = {}
    def fake_judge(r, reply, settings, phase="after"):
        seen[phase] = reply
        return {"verdict": "pause", "reason": "known off goal"} if phase == "before" else {"verdict": "ok"}
    monkeypatch.setattr(brain, "run_judge", fake_judge)
    monkeypatch.setattr(tick.driver, "resume", lambda *a, **k: (_ for _ in ()).throw(AssertionError("resumed")))
    monkeypatch.setattr(tick.sessions, "interactive_agent_pid", lambda *a, **k: None)
    import types
    fake_agent = types.SimpleNamespace(newest_transcript_mtime=lambda *a, **k: 0.0, transcript_is_gone=lambda *a, **k: False,
                                       newest_session=lambda *a, **k: None, estimate_tokens=lambda *a, **k: 0)
    monkeypatch.setattr(tick.agents, "for_record", lambda r: fake_agent)
    (env / "state").mkdir(exist_ok=True)
    from claude_pilot import config
    (config.WAKE).mkdir(parents=True, exist_ok=True); (config.WAKE / "gated.wake").touch()
    assert tick.tick({"idle_minutes": 8, "idle_backoff": 6}) == 0
    _, rec = registry.load("gated")
    assert rec["state"] == "paused" and rec["ticks"] == 1 and rec["paused_reason"] == "known off goal"
    assert seen == {"before": "prev"}
