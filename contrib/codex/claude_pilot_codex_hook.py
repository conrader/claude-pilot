#!/usr/bin/env python3
"""Bridge Codex lifecycle hooks to claude-pilot-hookd and the synchronous
Stop controller.

Runs on the Codex host itself, invoked by hooks.json for SessionStart,
Stop and SessionEnd. Every event is POSTed as telemetry (a failure there
must never block a Codex turn); a Stop event additionally asks a
synchronous controller (local `claude-pilot codex-stop`, or an SSH
fallback to a configured controller host) whether the turn should
continue, and prints that answer back to Codex. Any failure on the
controller path prints {} (fail open).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

CONFIG = pathlib.Path.home() / ".config" / "claude-pilot" / "codex-hook.json"


def load_config() -> dict:
    """The hook's own config file: hook_url, token_file, host, controller."""
    try:
        value = json.loads(CONFIG.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def post_event(payload: dict, hook_config: dict) -> None:
    """Send the raw hook payload to hookd. Swallows every failure."""
    url = str(hook_config.get("hook_url") or os.environ.get("CLAUDE_PILOT_HOOKD_URL")
              or "http://127.0.0.1:8910/hook")
    token_path = pathlib.Path(str(hook_config.get("token_file")
                                  or pathlib.Path.home() / ".config/claude-pilot/hookd.token"))
    headers = {"Content-Type": "application/json"}
    try:
        token = token_path.read_text().strip()
    except OSError:
        token = ""
    if token:
        headers["X-Pilot-Token"] = token
    host = hook_config.get("host") or os.environ.get("CLAUDE_PILOT_HOST") or ""
    if host:
        headers["X-Pilot-Host"] = host
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=2):
            pass
    except (OSError, urllib.error.URLError):
        pass


def controller_command(hook_config: dict) -> list[str]:
    """The synchronous Stop controller: configured, local, or an SSH fallback."""
    configured = hook_config.get("controller")
    if isinstance(configured, list) and all(isinstance(x, str) for x in configured):
        return [*configured, "codex-stop"]
    from shutil import which

    local = which("claude-pilot")
    if local:
        return [local, "codex-stop"]
    controller_host = hook_config.get("controller_host") or ""
    if controller_host:
        return ["ssh", "-o", "BatchMode=yes", controller_host, "claude-pilot", "codex-stop"]
    return ["claude-pilot", "codex-stop"]


def stop_decision(payload: dict, hook_config: dict) -> dict:
    """Ask the controller whether this turn should continue. Fails open."""
    try:
        result = subprocess.run(
            controller_command(hook_config), input=json.dumps(payload), text=True,
            capture_output=True, timeout=15)
        if result.returncode == 0:
            value = json.loads(result.stdout or "{}")
            if isinstance(value, dict):
                return value
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return {}


def main() -> int:
    """Read the hook payload from stdin, report it, and for Stop, decide."""
    try:
        payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        payload = {}
    payload["_agent"] = "codex"
    payload["_hook_transport"] = "claude-pilot-plugin"
    hook_config = load_config()
    post_event(payload, hook_config)
    if payload.get("hook_event_name") == "Stop":
        print(json.dumps(stop_decision(payload, hook_config)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
