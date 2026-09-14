"""Paths and settings for claude-pilot.

Settings resolve in three layers: built-in defaults, then the JSON config
file, then environment variables (CLAUDE_PILOT_<KEY>). Nothing here talks to
the network or mutates state outside HOME.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULTS = {
    "agent_command": "claude",
    "model": "",
    "permission_mode": "bypassPermissions",
    "idle_minutes": 8,
    "idle_backoff": 6,
    "default_ticks": 40,
    "default_hours": 12,
    "cap": 3,
    "compact_tokens": 150000,
    "resume_timeout_s": 3600,
    "notify_command": "",
    "hookd_port": 8910,
    "hookd_token_file": "~/.config/claude-pilot/hookd.token",
}


def _home() -> Path:
    return Path(os.environ.get("CLAUDE_PILOT_HOME") or "~/.local/state/claude-pilot").expanduser()


def _config_file() -> Path:
    return Path(os.environ.get("CLAUDE_PILOT_CONFIG") or "~/.config/claude-pilot/config.json").expanduser()


HOME = _home()
PILOTS = HOME / "pilots"
WAKE = HOME / "wake"
EVENTS = HOME / "events"
TICK_LOCK = HOME / "tick.lock"
CONFIG_FILE = _config_file()


def reload() -> None:
    """Recompute module-level paths from the current environment.

    Tests that change CLAUDE_PILOT_HOME or CLAUDE_PILOT_CONFIG after import
    should call this instead of re-importing the module.
    """
    global HOME, PILOTS, WAKE, EVENTS, TICK_LOCK, CONFIG_FILE
    HOME = _home()
    PILOTS = HOME / "pilots"
    WAKE = HOME / "wake"
    EVENTS = HOME / "events"
    TICK_LOCK = HOME / "tick.lock"
    CONFIG_FILE = _config_file()


def ensure_dirs() -> None:
    """Create HOME, PILOTS, WAKE and EVENTS if they do not exist yet."""
    for d in (HOME, PILOTS, WAKE, EVENTS):
        d.mkdir(parents=True, exist_ok=True)


def settings() -> dict:
    """Effective settings: defaults, overridden by the config file, then env."""
    out = dict(_DEFAULTS)

    cfg_path = _config_file()
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            for key in out:
                if key in data:
                    out[key] = data[key]

    for key, default in _DEFAULTS.items():
        env_key = f"CLAUDE_PILOT_{key.upper()}"
        if env_key in os.environ:
            raw = os.environ[env_key]
            if isinstance(default, bool):
                out[key] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                try:
                    out[key] = int(raw)
                except ValueError:
                    out[key] = default
            else:
                out[key] = raw

    return out
