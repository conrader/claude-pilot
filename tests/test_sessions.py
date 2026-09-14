"""Tests for claude_pilot.sessions."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

from claude_pilot import sessions


def test_transcript_slug_replaces_every_non_alnum():
    assert sessions.transcript_slug("/home/user/dotted.example") == "-home-user-dotted-example"
    assert sessions.transcript_slug("/a/b_c d") == "-a-b-c-d"


def _make_transcript(dirpath, session_id, lines, mtime=None):
    dirpath.mkdir(parents=True, exist_ok=True)
    f = dirpath / f"{session_id}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


def test_transcript_ids_and_newest_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    cwd = "/home/user/proj"
    slug = sessions.transcript_slug(cwd)
    d = tmp_path / slug
    old_id = "11111111-1111-1111-1111-111111111111"
    new_id = "22222222-2222-2222-2222-222222222222"
    _make_transcript(d, old_id, [{"type": "assistant"}], mtime=1000)
    _make_transcript(d, new_id, [{"type": "assistant"}], mtime=2000)
    # A non-uuid file must never be treated as a session id.
    (d / "notes.jsonl").write_text("{}\n")

    ids = sessions.transcript_ids(cwd)
    assert ids == [new_id, old_id]
    assert sessions.newest_session(cwd) == new_id


def test_transcript_ids_empty_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    assert sessions.transcript_ids("/nowhere") == []
    assert sessions.newest_session("/nowhere") is None


def test_newest_transcript_mtime(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    cwd = "/home/user/proj2"
    slug = sessions.transcript_slug(cwd)
    d = tmp_path / slug
    sid = "33333333-3333-3333-3333-333333333333"
    _make_transcript(d, sid, [{"type": "assistant"}], mtime=5000)
    sub = d / sid / "subagents"
    sub.mkdir(parents=True)
    sub_file = sub / "agent-1.jsonl"
    sub_file.write_text("{}\n")
    os.utime(sub_file, (9000, 9000))

    assert sessions.newest_transcript_mtime(cwd, sid) == 9000
    assert sessions.newest_transcript_mtime(cwd) == 9000
    assert sessions.newest_transcript_mtime("/gone") is None


def test_transcript_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    cwd = "/home/user/proj3"
    slug = sessions.transcript_slug(cwd)
    d = tmp_path / slug
    sid = "44444444-4444-4444-4444-444444444444"
    _make_transcript(d, sid, [{"type": "assistant"}])
    assert sessions.transcript_is_gone(cwd, sid) is False
    assert sessions.transcript_is_gone(cwd, "55555555-5555-5555-5555-555555555555") is True


def test_estimate_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    cwd = "/home/user/proj4"
    slug = sessions.transcript_slug(cwd)
    d = tmp_path / slug
    sid = "66666666-6666-6666-6666-666666666666"
    lines = [
        {"message": {"content": "a" * 40}},
        {"message": {"content": [{"type": "text", "text": "b" * 40}]}},
        {"message": {"content": [{"type": "tool_use", "input": {"x": "c" * 36}}]}},
        {"message": {"content": [{"type": "tool_result", "content": "d" * 40}]}},
    ]
    _make_transcript(d, sid, lines)
    n = sessions.estimate_tokens(cwd, sid)
    assert n > 0
    assert sessions.estimate_tokens(cwd, "77777777-7777-7777-7777-777777777777") == 0


def test_last_assistant_text(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    cwd = "/home/user/proj5"
    slug = sessions.transcript_slug(cwd)
    d = tmp_path / slug
    sid = "88888888-8888-8888-8888-888888888888"
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "user", "message": {"content": "ignored"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "final answer"}]}},
    ]
    _make_transcript(d, sid, lines)
    assert sessions.last_assistant_text(cwd, sid) == "final answer"
    assert sessions.last_assistant_text(cwd, "99999999-9999-9999-9999-999999999999") == ""


def _fake_agent(tmp_path, name, args, with_tty):
    """A real child process named `name`, cwd tmp_path, with or without a tty.

    A real binary, not a shebang script: with `exec` on a script, the kernel
    puts the interpreter in argv[0], and the guard would (rightly) see the
    interpreter's name instead of the agent's.
    """
    exe = tmp_path / name
    shutil.copy2(shutil.which("sleep"), exe)
    exe.chmod(0o755)
    args = ["30", *args]
    if with_tty:
        cmd = ["script", "-qfc", f"exec {exe} {' '.join(args)}", "/dev/null"]
    else:
        cmd = [str(exe), *args]
    p = subprocess.Popen(
        cmd,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.4)
    return p


def test_headless_pilot_turn_does_not_count(tmp_path):
    p = _fake_agent(tmp_path, "claude", [], with_tty=False)
    try:
        assert sessions.interactive_agent_pid(str(tmp_path)) is None
    finally:
        p.kill()


def test_interactive_session_is_detected(tmp_path):
    if not shutil.which("script"):
        pytest.skip("script(1) not available for a pty")
    p = _fake_agent(tmp_path, "claude", [], with_tty=True)
    try:
        pid = sessions.interactive_agent_pid(str(tmp_path))
        assert pid is not None
        assert os.readlink(f"/proc/{pid}/cwd").rstrip("/") == str(tmp_path).rstrip("/")
    finally:
        os.killpg(os.getpgid(p.pid), 9)


def test_other_directory_is_ignored(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    p = _fake_agent(other, "claude", [], with_tty=False)
    try:
        assert sessions.interactive_agent_pid(str(tmp_path)) is None
    finally:
        p.kill()
