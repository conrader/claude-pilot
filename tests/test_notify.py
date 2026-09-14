import stat

import pytest

from claude_pilot import config, notify


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(tmp_path / "nope.json"))
    config.reload()
    yield


def _echo_script(tmp_path):
    script = tmp_path / "capture.sh"
    out = tmp_path / "captured.txt"
    script.write_text(f"#!/bin/sh\ncat > {out}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, out


def test_notify_prints_to_stdout(capsys):
    notify.send("hello world")
    out = capsys.readouterr().out
    assert "notify: hello world" in out


def test_notify_runs_notify_command_with_stdin(tmp_path, monkeypatch):
    script, out = _echo_script(tmp_path)
    monkeypatch.setenv("CLAUDE_PILOT_NOTIFY_COMMAND", str(script))
    notify.send("payload text")
    assert out.read_text().strip() == "payload text"


def test_notify_dedup_within_window(tmp_path, monkeypatch):
    script, out = _echo_script(tmp_path)
    monkeypatch.setenv("CLAUDE_PILOT_NOTIFY_COMMAND", str(script))
    notify.send("same text")
    out.unlink()
    notify.send("same text")
    assert not out.exists()


def test_notify_different_text_not_deduped(tmp_path, monkeypatch):
    script, out = _echo_script(tmp_path)
    monkeypatch.setenv("CLAUDE_PILOT_NOTIFY_COMMAND", str(script))
    notify.send("text one")
    out.unlink()
    notify.send("text two")
    assert out.read_text().strip() == "text two"
