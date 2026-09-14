import json

from claude_pilot import config


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(tmp_path / "nope.json"))
    config.reload()
    s = config.settings()
    assert s["agent_command"] == "claude"
    assert s["cap"] == 3
    assert s["idle_minutes"] == 8


def test_config_file_overrides_defaults(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"cap": 7, "agent_command": "myagent"}))
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(home))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(cfg))
    config.reload()
    s = config.settings()
    assert s["cap"] == 7
    assert s["agent_command"] == "myagent"
    assert s["idle_minutes"] == 8


def test_env_overrides_config_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"cap": 7}))
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(home))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(cfg))
    monkeypatch.setenv("CLAUDE_PILOT_CAP", "9")
    config.reload()
    s = config.settings()
    assert s["cap"] == 9


def test_ensure_dirs_creates_tree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(home))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(tmp_path / "nope.json"))
    config.reload()
    config.ensure_dirs()
    assert config.PILOTS.is_dir()
    assert config.WAKE.is_dir()
    assert config.EVENTS.is_dir()
