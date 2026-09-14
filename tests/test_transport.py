"""Tests for claude_pilot.transport: local vs SSH argv shape and quoting."""
from __future__ import annotations

import stat

import pytest

from claude_pilot import transport


def _fake_ssh(tmp_path, name="ssh", exit_code=None):
    """A fake ssh: logs its argv, then runs the LAST argument with sh -c.

    Sets PILOT_FAKE_REMOTE=1 so a test can prove the remote path actually
    ran through this script rather than executing locally.
    """
    script = tmp_path / "bin" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    log = tmp_path / f"{name}.argv.log"
    if exit_code is not None:
        script.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    else:
        script.write_text(
            "#!/bin/sh\n"
            f'printf \'%s\\n\' "$@" > {log}\n'
            "export PILOT_FAKE_REMOTE=1\n"
            'eval "last=\\${$#}"\n'
            'sh -c "$last"\n'
        )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), log


def _settings(ssh_path):
    return {
        "ssh_command": ssh_path,
        "hosts": {
            "h1": {
                "ssh": "user@host",
                "key": "/tmp/fakekey",
                "python": "python3",
                "connect_timeout": 5,
            }
        },
    }


def test_local_run_strips_env_and_sets_markers(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sekret")
    r = transport.run("", ["printenv", "ANTHROPIC_API_KEY"], settings={})
    assert r.returncode != 0
    assert "sekret" not in (r.stdout or "")

    r2 = transport.run("", ["printenv", "CLAUDE_PILOT_TICK"], settings={})
    assert r2.stdout.strip() == "1"
    r3 = transport.run("", ["printenv", "PILOT_HEADLESS"], settings={})
    assert r3.stdout.strip() == "1"


def test_remote_run_argv_shape_and_quoting(tmp_path, monkeypatch):
    ssh_path, log = _fake_ssh(tmp_path)
    settings = _settings(ssh_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    instruction = "say \"hello there\" and 'quote' this"
    r = transport.run(
        "h1", ["echo", instruction], cwd=str(workdir), timeout=5, settings=settings
    )
    assert r.returncode == 0
    assert r.stdout.strip() == instruction

    logged = log.read_text().splitlines()
    assert "-o" in logged
    assert "BatchMode=yes" in logged
    assert "ConnectTimeout=5" in logged
    assert "-i" in logged
    assert "/tmp/fakekey" in logged
    assert "user@host" in logged
    remote_cmd = logged[-1]
    assert f"cd {workdir} &&" in remote_cmd
    assert "CLAUDE_PILOT_TICK=1" in remote_cmd
    assert "PILOT_HEADLESS=1" in remote_cmd
    assert "-u ANTHROPIC_API_KEY" in remote_cmd


def test_remote_run_sets_fake_remote_marker(tmp_path):
    ssh_path, _ = _fake_ssh(tmp_path)
    settings = _settings(ssh_path)
    r = transport.run("h1", ["printenv", "PILOT_FAKE_REMOTE"], settings=settings)
    assert r.stdout.strip() == "1"


def test_python_on_local(tmp_path):
    r = transport.python_on("", "print('hi-local')", settings={})
    assert r.stdout.strip() == "hi-local"


def test_python_on_remote(tmp_path):
    ssh_path, _ = _fake_ssh(tmp_path)
    settings = _settings(ssh_path)
    r = transport.python_on("h1", "print('hi-remote')", settings=settings)
    assert r.stdout.strip() == "hi-remote"


def test_reachable_true(tmp_path):
    ssh_path, _ = _fake_ssh(tmp_path)
    settings = _settings(ssh_path)
    ok, detail = transport.reachable("h1", settings=settings)
    assert ok is True


def test_reachable_false(tmp_path):
    ssh_path, _ = _fake_ssh(tmp_path, name="sshfail", exit_code=3)
    settings = _settings(ssh_path)
    ok, detail = transport.reachable("h1", settings=settings)
    assert ok is False


def test_host_config_unknown_raises(tmp_path):
    ssh_path, _ = _fake_ssh(tmp_path)
    settings = _settings(ssh_path)
    with pytest.raises(KeyError):
        transport.run("nope", ["true"], settings=settings)
