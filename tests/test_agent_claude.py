"""Tests for claude_pilot.agents.claude: local and SSH transcript discovery."""
from __future__ import annotations

import json
import stat
import time

from claude_pilot.agents import claude as claude_agent


def _fake_ssh(tmp_path):
    script = tmp_path / "bin" / "ssh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/sh\n"
        "export PILOT_FAKE_REMOTE=1\n"
        'eval "last=\\${$#}"\n'
        'sh -c "$last"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _settings(ssh_path):
    return {
        "ssh_command": ssh_path,
        "hosts": {"h1": {"ssh": "user@host", "key": "", "python": "python3", "connect_timeout": 5}},
    }


def _write_transcript(path, sid):
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{sid}.jsonl").write_text("")


def test_local_ids_newest_mtime_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    slug = claude_agent.sessions.transcript_slug(str(tmp_path / "work"))
    d = tmp_path / "projects" / slug
    _write_transcript(d, "11111111-1111-1111-1111-111111111111")
    time.sleep(0.01)
    _write_transcript(d, "22222222-2222-2222-2222-222222222222")

    ids = claude_agent.transcript_ids(str(tmp_path / "work"))
    assert ids[0] == "22222222-2222-2222-2222-222222222222"
    assert claude_agent.newest_session(str(tmp_path / "work")) == ids[0]
    assert claude_agent.newest_transcript_mtime(str(tmp_path / "work")) is not None
    assert claude_agent.transcript_is_gone(str(tmp_path / "work"), "nope-nope") is True
    assert claude_agent.transcript_is_gone(str(tmp_path / "work"), ids[0]) is False


def test_remote_ids_newest_mtime_gone(tmp_path, monkeypatch):
    ssh_path = _fake_ssh(tmp_path)
    settings = _settings(ssh_path)
    monkeypatch.setattr("claude_pilot.config.settings", lambda: settings)
    fake_home = tmp_path / "remote_home"
    monkeypatch.setenv("HOME", str(fake_home))

    cwd = "/work/proj"
    slug = claude_agent.sessions.transcript_slug(cwd)
    d = fake_home / ".claude" / "projects" / slug
    _write_transcript(d, "11111111-1111-1111-1111-111111111111")
    time.sleep(0.01)
    _write_transcript(d, "22222222-2222-2222-2222-222222222222")

    rows = claude_agent._remote_rows(cwd, "h1")
    assert len(rows) == 2

    ids = claude_agent.transcript_ids(cwd, "h1")
    assert ids[0] == "22222222-2222-2222-2222-222222222222"
    assert claude_agent.newest_session(cwd, "h1") == ids[0]
    assert claude_agent.newest_transcript_mtime(cwd, host="h1") is not None
    assert claude_agent.transcript_is_gone(cwd, "nope-nope", "h1") is True
    assert claude_agent.transcript_is_gone(cwd, ids[0], "h1") is False


def test_host_config_defaults_and_keyerror():
    from claude_pilot import config

    hc = config.host_config("h1", {"hosts": {"h1": {"ssh": "user@host"}}})
    assert hc["python"] == "python3"
    assert hc["claude"] == "claude"
    assert hc["connect_timeout"] == 10

    import pytest

    with pytest.raises(KeyError):
        config.host_config("nope", {"hosts": {}})


def test_resume_argv_permission_mode_output_format_model(tmp_path):
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "argv.log"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > {log}\n'
        'echo "ok reply"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    rec = {"cwd": str(tmp_path), "session_id": "sess-1", "agent": "claude", "host": ""}
    settings = {
        "agent_command": str(script),
        "permission_mode": "bypassPermissions",
        "model": "some-model",
        "resume_timeout_s": 30,
    }
    ok, reply = claude_agent.resume(rec, "do it", settings)
    assert ok is True
    assert reply == "ok reply"
    logged = log.read_text()
    assert "-r sess-1 -p do it --permission-mode bypassPermissions --output-format text --model some-model" in logged
