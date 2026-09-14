"""Run a command locally or on a configured remote host over SSH.

Every pilot action that touches an agent CLI or a transcript goes through
this module so the local and remote code paths are identical apart from the
SSH wrapper: a script string handed to python_on runs the same way whether
the interpreter is spawned on this box or over SSH on a remote one.
"""
from __future__ import annotations

import os
import shlex
import subprocess

from . import config


def _strip_env(env_unset: tuple[str, ...]) -> dict:
    env = dict(os.environ)
    for key in env_unset:
        env.pop(key, None)
    env["CLAUDE_PILOT_TICK"] = "1"
    env["PILOT_HEADLESS"] = "1"
    return env


def _remote_command(
    argv: list[str], cwd: str | None, env_unset: tuple[str, ...]
) -> str:
    """Build the single shell string executed on the remote side.

    Everything variable (cwd, env var names, argv) is shlex-quoted before
    joining so an instruction containing quotes or spaces cannot break out
    of the remote command.
    """
    parts = []
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)} &&")
    env_parts = ["env"]
    for var in env_unset:
        env_parts.append(f"-u {shlex.quote(var)}")
    env_parts.append("CLAUDE_PILOT_TICK=1")
    env_parts.append("PILOT_HEADLESS=1")
    parts.append(" ".join(env_parts))
    parts.append(shlex.join(argv))
    return " ".join(parts)


def _ssh_prefix(host: str, settings: dict) -> list[str]:
    hc = config.host_config(host, settings)
    ssh_bin = settings.get("ssh_command", "ssh")
    prefix = [
        ssh_bin,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={hc.get('connect_timeout', 10)}",
    ]
    if hc.get("key"):
        prefix += ["-i", hc["key"]]
    prefix.append(hc["ssh"])
    return prefix


def run(
    host: str,
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout: int = 3600,
    env_unset: tuple[str, ...] = ("ANTHROPIC_API_KEY",),
    input: str | None = None,
    settings: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run `argv` locally (host "") or on a named remote host over SSH.

    Locally, argv is spawned directly with a stripped environment. Remotely
    the same effective command line is shipped as one shlex-quoted shell
    string appended to the SSH invocation, never shell-interpolated
    unquoted.
    """
    settings = settings if settings is not None else config.settings()
    if not host:
        env = _strip_env(env_unset)
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            input=input,
        )
    prefix = _ssh_prefix(host, settings)
    remote_cmd = _remote_command(argv, cwd, env_unset)
    full = prefix + [remote_cmd]
    return subprocess.run(
        full, capture_output=True, text=True, timeout=timeout, input=input,
    )


def python_on(
    host: str, script: str, *args: str, timeout: int = 60, settings: dict | None = None
) -> subprocess.CompletedProcess:
    """Run `script` with python3 -c, locally or on a remote host.

    The same script string is used both ways so transcript scans exercise
    one code path regardless of where the agent's transcripts live; only
    python3 is assumed on the remote side.
    """
    settings = settings if settings is not None else config.settings()
    if not host:
        python_bin = "python3"
    else:
        python_bin = config.host_config(host, settings).get("python", "python3")
    argv = [python_bin, "-c", script, *args]
    return run(host, argv, timeout=timeout, settings=settings)


def reachable(host: str, settings: dict | None = None) -> tuple[bool, str]:
    """Whether a configured remote host answers SSH within 10 seconds."""
    settings = settings if settings is not None else config.settings()
    try:
        r = run(host, ["true"], timeout=10, settings=settings)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if r.returncode == 0:
        return True, "ok"
    return False, (r.stderr or "").strip()[-500:] or f"exit {r.returncode}"
