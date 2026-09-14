"""Tests for claude_pilot.driver."""
from __future__ import annotations

import os
import stat

import pytest

from claude_pilot import config, driver


def _fake_agent(tmp_path):
    script = tmp_path / "bin" / "claude"
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


def _settings(agent_path):
    return {
        "agent_command": agent_path,
        "model": "",
        "resume_timeout_s": 30,
    }


def test_resume_success_passes_flags(tmp_path, monkeypatch):
    agent = _fake_agent(tmp_path)
    monkeypatch.setenv("PILOT_FAKE_REPLY", "all done here")
    rec = {"cwd": str(tmp_path), "session_id": "sess-123"}
    ok, reply = driver.resume(rec, "do the thing", _settings(agent))
    assert ok is True
    assert reply == "all done here"


def test_resume_failure_reports_background_agent(tmp_path, monkeypatch):
    agent = _fake_agent(tmp_path)
    monkeypatch.setenv("PILOT_FAKE_FAIL", "1")
    monkeypatch.delenv("PILOT_FAKE_REPLY", raising=False)
    rec = {"cwd": str(tmp_path), "session_id": "sess-123"}
    ok, reply = driver.resume(rec, "do the thing", _settings(agent))
    assert ok is False
    assert "background agent" in reply


def test_resume_uses_correct_argv(tmp_path, monkeypatch):
    agent = tmp_path / "bin" / "claude"
    agent.parent.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "argv.log"
    agent.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > {log}\n'
        'echo "ok"\n'
    )
    agent.chmod(agent.stat().st_mode | stat.S_IEXEC)
    rec = {"cwd": str(tmp_path), "session_id": "sess-xyz"}
    ok, reply = driver.resume(rec, "hello world", _settings(str(agent)))
    assert ok is True
    logged = log.read_text()
    assert "-r sess-xyz -p hello world" in logged
