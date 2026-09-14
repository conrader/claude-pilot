"""Tests for claude_pilot.agents.codex."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import stat
import subprocess
import sys

import pytest

from claude_pilot import config, instruction, registry
from claude_pilot.agents import codex


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(home))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(tmp_path / "nope.json"))
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    config.reload()
    config.ensure_dirs()
    return tmp_path


def _write_rollout(codex_home: pathlib.Path, sid: str, cwd: str, mtime: float | None = None):
    sessions = codex_home / "sessions" / "2026" / "09"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"rollout-2026-09-01T00-00-00-{sid}.jsonl"
    first = {"payload": {"cwd": cwd, "session_id": sid}}
    path.write_text(json.dumps(first) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _fake_codex(tmp_path, argv_log):
    script = tmp_path / "bin" / "codex"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {argv_log}\n'
        'printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"agent_message","text":"first"}}\'\n'
        'printf \'%s\\n\' "{\\"type\\":\\"item.completed\\",\\"item\\":{\\"type\\":\\"agent_message\\",\\"text\\":\\"$PILOT_FAKE_REPLY\\"}}"\n'
        "exit ${PILOT_FAKE_EXIT:-0}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


# --- transcript discovery -------------------------------------------------

def test_transcript_ids_newest_first_cwd_prefix(env, tmp_path):
    codex_home = tmp_path / "codex-home"
    cwd = str(tmp_path / "proj")
    _write_rollout(codex_home, "11111111-1111-1111-1111-111111111111", cwd, mtime=100)
    _write_rollout(codex_home, "22222222-2222-2222-2222-222222222222", cwd, mtime=200)
    _write_rollout(codex_home, "33333333-3333-3333-3333-333333333333", cwd + "/nested", mtime=300)
    _write_rollout(codex_home, "44444444-4444-4444-4444-444444444444", "/somewhere/else", mtime=400)

    ids = codex.transcript_ids(cwd, "")
    assert ids == [
        "33333333-3333-3333-3333-333333333333",
        "22222222-2222-2222-2222-222222222222",
        "11111111-1111-1111-1111-111111111111",
    ]
    assert codex.newest_session(cwd, "") == ids[0]


def test_transcript_ids_empty_when_root_missing(env, tmp_path):
    assert codex.transcript_ids(str(tmp_path / "proj"), "") == []


def test_newest_transcript_mtime_needs_sid(env, tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    cwd = str(tmp_path / "proj")
    sid = "11111111-1111-1111-1111-111111111111"
    _write_rollout(codex_home, sid, cwd, mtime=555)

    assert codex.newest_transcript_mtime(cwd, None, "") is None
    assert codex.newest_transcript_mtime(cwd, sid, "") == 555
    # a remote host runs the same lookup over the transport; stand it in with a local runner
    import subprocess
    monkeypatch.setattr(codex.transport, "python_on",
                        lambda host, script, *a, **k: subprocess.run(
                            ["python3", "-c", script, a[0], str(codex_home / "sessions")],
                            capture_output=True, text=True))
    assert codex.newest_transcript_mtime(cwd, sid, "somehost") == 555


def test_transcript_is_gone(env, tmp_path):
    codex_home = tmp_path / "codex-home"
    cwd = str(tmp_path / "proj")
    sid = "11111111-1111-1111-1111-111111111111"
    assert codex.transcript_is_gone(cwd, sid, "") is True
    _write_rollout(codex_home, sid, cwd, mtime=1)
    assert codex.transcript_is_gone(cwd, sid, "") is False


def test_estimate_tokens_and_last_assistant_text_unsupported(env):
    assert codex.estimate_tokens("/tmp/x", "sid", "") == 0
    assert codex.last_assistant_text("/tmp/x", "sid", "") == ""


# --- resume ---------------------------------------------------------------

def test_resume_argv_and_reply_uses_last_agent_message(env, tmp_path, monkeypatch):
    argv_log = tmp_path / "argv.log"
    codex_bin = _fake_codex(tmp_path, argv_log)
    monkeypatch.setenv("PILOT_FAKE_REPLY", "the final word")
    rec = {"cwd": str(tmp_path), "session_id": "sess-1"}
    settings = {"codex_command": codex_bin, "resume_timeout_s": 30}

    ok, reply = codex.resume(rec, "do the thing", settings)

    assert ok is True
    assert reply == "the final word"
    argv = argv_log.read_text().strip()
    assert "exec resume sess-1 do the thing" in argv
    assert "--json" in argv
    assert "mcp_servers={}" in argv


def test_resume_sandbox_flags(env, tmp_path, monkeypatch):
    argv_log = tmp_path / "argv.log"
    codex_bin = _fake_codex(tmp_path, argv_log)
    monkeypatch.setenv("PILOT_FAKE_REPLY", "ok")
    rec = {"cwd": str(tmp_path), "session_id": "sess-1", "sandbox": "workspace-write",
           "writable_roots": ["/home/user/proj"]}
    settings = {"codex_command": codex_bin, "resume_timeout_s": 30}

    ok, _ = codex.resume(rec, "go", settings)

    assert ok is True
    argv = argv_log.read_text()
    assert 'sandbox_mode="workspace-write"' in argv
    assert "sandbox_workspace_write.network_access=true" in argv
    assert "sandbox_workspace_write.writable_roots=" in argv
    assert "/home/user/proj" in argv


# --- stop_decision ----------------------------------------------------------

def _make_record(tmp_path, sid, **updates):
    rec = {
        "name": "demo",
        "cwd": str(tmp_path),
        "session_id": sid,
        "agent": "codex",
        "goal": "finish the demo",
        "state": "active",
        "started_at": registry.iso(registry.now()),
        "deadline": registry.iso(registry.now() + datetime.timedelta(hours=1)),
        "max_ticks": 3,
        "ticks": 0,
        "history": [],
        "persistent": False,
    }
    rec.update(updates)
    registry.save(rec)
    return rec


def _payload(sid, message, turn_id="turn-1"):
    return {
        "session_id": sid,
        "turn_id": turn_id,
        "hook_event_name": "Stop",
        "last_assistant_message": message,
    }


def test_stop_decision_continue_blocks_with_goal_and_inbox(env, tmp_path):
    sid = "sid-continue"
    _make_record(tmp_path, sid)
    registry.inbox_add("demo", "also check the logs")

    decision = codex.stop_decision(_payload(sid, "partial progress"), config.settings())

    assert decision["decision"] == "block"
    assert "finish the demo" in decision["reason"]
    assert "continuation 1 of at most 3" in decision["reason"]
    assert "also check the logs" in decision["reason"]
    _, rec = registry.load("demo")
    assert rec["ticks"] == 1
    assert rec["history"][-1]["reply"] == "partial progress"
    # inbox drained
    assert registry.inbox_pending("demo") == []


def test_stop_decision_done(env, tmp_path):
    sid = "sid-done"
    _make_record(tmp_path, sid)

    decision = codex.stop_decision(_payload(sid, f"all set\n{instruction.DONE}\nsummary"),
                                    config.settings())

    assert decision == {}
    _, rec = registry.load("demo")
    assert rec["state"] == "done"


def test_stop_decision_blocked(env, tmp_path):
    sid = "sid-blocked"
    _make_record(tmp_path, sid)

    decision = codex.stop_decision(_payload(sid, f"{instruction.BLOCKED} need a decision"),
                                    config.settings())

    assert decision == {}
    _, rec = registry.load("demo")
    assert rec["state"] == "blocked"


def test_stop_decision_expired(env, tmp_path):
    sid = "sid-expired"
    _make_record(tmp_path, sid, deadline=registry.iso(registry.now() - datetime.timedelta(hours=1)))

    decision = codex.stop_decision(_payload(sid, "still working"), config.settings())

    assert decision == {}
    _, rec = registry.load("demo")
    assert rec["state"] == "expired"


def test_stop_decision_exhausted(env, tmp_path):
    sid = "sid-exhausted"
    _make_record(tmp_path, sid, ticks=3, max_ticks=3)

    decision = codex.stop_decision(_payload(sid, "still going"), config.settings())

    assert decision == {}
    _, rec = registry.load("demo")
    assert rec["state"] == "exhausted"


def test_stop_decision_persistent_renews(env, tmp_path):
    sid = "sid-persistent"
    _make_record(tmp_path, sid, persistent=True, ticks=3, max_ticks=3,
                 deadline=registry.iso(registry.now() - datetime.timedelta(hours=1)))

    decision = codex.stop_decision(_payload(sid, "keep going"), config.settings())

    assert decision["decision"] == "block"
    _, rec = registry.load("demo")
    assert rec["state"] == "active"
    assert rec["ticks"] == 1
    assert datetime.datetime.fromisoformat(rec["deadline"]) > registry.now()


def test_stop_decision_unknown_session(env, tmp_path):
    _make_record(tmp_path, "sid-real")
    decision = codex.stop_decision(_payload("no-such-session", "hi"), config.settings())
    assert decision == {}


def test_stop_decision_malformed_payload_never_raises(env):
    assert codex.stop_decision({}, config.settings()) == {}
    assert codex.stop_decision({"session_id": None}, config.settings()) == {}
    assert codex.stop_decision(None, config.settings()) == {}  # type: ignore[arg-type]


def test_stop_decision_judge_pause(env, tmp_path):
    sid = "sid-judge"
    _make_record(tmp_path, sid)
    judge = tmp_path / "judge.py"
    judge.write_text(
        "import sys, json\n"
        "json.loads(sys.stdin.read())\n"
        "print(json.dumps({'verdict': 'pause', 'reason': 'looked off-goal'}))\n"
    )
    settings = dict(config.settings())
    settings["judge_command"] = f"{sys.executable} {judge}"

    decision = codex.stop_decision(_payload(sid, "doing stuff"), settings)

    assert decision == {}
    _, rec = registry.load("demo")
    assert rec["state"] == "paused"
    assert rec["paused_reason"] == "looked off-goal"


# --- the hook script (subprocess, fail-open) --------------------------------

HOOK_SCRIPT = pathlib.Path(__file__).parents[1] / "contrib" / "codex" / "claude_pilot_codex_hook.py"


def _fake_controller(tmp_path, reply="{}"):
    script = tmp_path / "controller.py"
    script.write_text(
        "import sys, json\n"
        "json.loads(sys.stdin.read())\n"
        f"print({reply!r})\n"
    )
    return [sys.executable, str(script)]


def test_hook_script_uses_configured_controller_and_fails_open_on_telemetry(tmp_path, monkeypatch):
    config_dir = tmp_path / ".config" / "claude-pilot"
    config_dir.mkdir(parents=True)
    controller = _fake_controller(tmp_path, reply=json.dumps({"decision": "block", "reason": "go"}))
    hook_config = {
        "hook_url": "http://127.0.0.1:1/hook",  # unreachable, telemetry must fail open
        "controller": controller,
    }
    (config_dir / "codex-hook.json").write_text(json.dumps(hook_config))
    monkeypatch.setenv("HOME", str(tmp_path))

    payload = {"session_id": "sid", "hook_event_name": "Stop", "last_assistant_message": "hi"}
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=15,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout.strip()) == {"decision": "block", "reason": "go"}


def test_hook_script_fails_open_when_controller_broken(tmp_path, monkeypatch):
    config_dir = tmp_path / ".config" / "claude-pilot"
    config_dir.mkdir(parents=True)
    hook_config = {
        "hook_url": "http://127.0.0.1:1/hook",
        "controller": ["/no/such/controller-binary"],
    }
    (config_dir / "codex-hook.json").write_text(json.dumps(hook_config))
    monkeypatch.setenv("HOME", str(tmp_path))

    payload = {"session_id": "sid", "hook_event_name": "Stop", "last_assistant_message": "hi"}
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=15,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout.strip()) == {}


def test_hook_script_session_start_skips_controller(tmp_path, monkeypatch):
    config_dir = tmp_path / ".config" / "claude-pilot"
    config_dir.mkdir(parents=True)
    hook_config = {"hook_url": "http://127.0.0.1:1/hook"}
    (config_dir / "codex-hook.json").write_text(json.dumps(hook_config))
    monkeypatch.setenv("HOME", str(tmp_path))

    payload = {"session_id": "sid", "hook_event_name": "SessionStart"}
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=15,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_stop_decision_carries_context_from_the_brain(env, tmp_path, monkeypatch):
    """context_command output reaches the Codex continuation prompt too."""
    from claude_pilot import registry
    cwd = str(tmp_path / "proj")
    sid = "22222222-2222-2222-2222-222222222222"
    registry.save({"name": "ctxproj", "cwd": cwd, "session_id": sid, "agent": "codex",
                   "goal": "finish the thing", "state": "active", "ticks": 0, "max_ticks": 5,
                   "deadline": "2999-01-01T00:00:00+00:00", "history": [], "persistent": False})
    settings = {"context_command": "printf 'guardrail: never touch billing'", "default_hours": 12}
    out = codex.stop_decision({"session_id": sid, "turn_id": "t1", "last_assistant_message": "working"}, settings)
    assert out.get("decision") == "block"
    assert "Context from your operator's system:" in out["reason"]
    assert "never touch billing" in out["reason"]
