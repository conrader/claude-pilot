"""Tests for claude_pilot.cli."""
from __future__ import annotations

import json
import os
import time

import pytest

from claude_pilot import cli, config, registry


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(home))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(tmp_path / "nope.json"))
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    config.reload()
    config.ensure_dirs()
    return tmp_path


def _make_transcript(tmp_path, cwd, session_id):
    from claude_pilot import sessions
    slug = sessions.transcript_slug(cwd)
    d = tmp_path / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": "hi"}}) + "\n"
    )


def test_start_refuses_non_project_dir(env, tmp_path, capsys):
    cwd = tmp_path / "not-a-project"
    cwd.mkdir()
    rc = cli.main(["start", "goal here", "--cwd", str(cwd)])
    assert rc == 2
    out = capsys.readouterr().out
    assert "does not look like a project tree" in out


def test_start_refuses_no_transcript(env, tmp_path, capsys):
    cwd = tmp_path / "proj"
    (cwd / ".git").mkdir(parents=True)
    rc = cli.main(["start", "goal here", "--cwd", str(cwd)])
    assert rc == 2
    out = capsys.readouterr().out
    assert "no transcript recorded" in out


def test_start_enrols_with_fake_transcript(env, tmp_path, capsys):
    cwd = tmp_path / "myproj"
    (cwd / ".git").mkdir(parents=True)
    session_id = "44444444-4444-4444-4444-444444444444"
    _make_transcript(tmp_path, str(cwd.resolve()), session_id)
    rc = cli.main(["start", "ship the feature", "--cwd", str(cwd)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "myproj" in out
    assert "enrolled" in out
    _, rec = registry.load("myproj")
    assert rec is not None
    assert rec["session_id"] == session_id
    assert rec["state"] == "active"


def test_stop_is_final_without_force(env, tmp_path, capsys):
    cwd = tmp_path / "stopproj"
    (cwd / ".git").mkdir(parents=True)
    session_id = "55555555-5555-5555-5555-555555555555"
    _make_transcript(tmp_path, str(cwd.resolve()), session_id)
    cli.main(["start", "goal", "--cwd", str(cwd)])
    rc = cli.main(["stop", "stopproj"])
    assert rc == 0
    rc2 = cli.main(["start", "goal again", "--cwd", str(cwd)])
    assert rc2 == 3
    out = capsys.readouterr().out
    assert "a stop is final" in out
    rc3 = cli.main(["start", "goal again", "--cwd", str(cwd), "--force"])
    assert rc3 == 0


def test_tell_and_inbox_round_trip(env, tmp_path, capsys):
    cwd = tmp_path / "telltest"
    (cwd / ".git").mkdir(parents=True)
    session_id = "66666666-6666-6666-6666-666666666666"
    _make_transcript(tmp_path, str(cwd.resolve()), session_id)
    cli.main(["start", "goal", "--cwd", str(cwd)])
    capsys.readouterr()
    cli.main(["tell", "telltest", "please check the logs"])
    out = capsys.readouterr().out
    assert "queued" in out
    cli.main(["inbox", "telltest"])
    out2 = capsys.readouterr().out
    assert "please check the logs" in out2
    assert (config.WAKE / "telltest.wake").exists()


def test_install_units_renders_four_units_with_wake_path(env, capsys):
    rc = cli.main(["install-units"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-pilot.timer" in out
    assert "claude-pilot.service" in out
    assert "claude-pilot-wake.path" in out
    assert "claude-pilot-wake.service" in out
    assert "claude-pilot-hookd.service" in out
    assert str(config.WAKE) in out
    assert "OnUnitActiveSec=10min" in out
