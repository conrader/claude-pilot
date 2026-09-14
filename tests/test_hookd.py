"""Tests for claude_pilot.hookd.

registry.all_records() is provided by another module being written in
parallel; if it is not importable yet, a minimal stub is installed here only
for the duration of these tests (never written as a package file).
"""
from __future__ import annotations

import http.client
import importlib
import json
import sys
import threading
import time
import types

import pytest

from claude_pilot import config


def _ensure_registry_stub(records):
    """Write the records into the real registry under the test's CLAUDE_PILOT_HOME."""
    from claude_pilot import registry

    for rec in records:
        registry.save(dict(rec))


@pytest.fixture
def pilot_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(tmp_path / "config.json"))
    from claude_pilot import config as cfg

    cfg.reload()
    yield cfg
    cfg.reload()


def _free_port():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(records, monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_pilot.registry", None)
    _ensure_registry_stub(records)
    import claude_pilot.hookd as hookd

    importlib.reload(hookd)
    from http.server import ThreadingHTTPServer

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), hookd.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    return server, port, hookd


def test_health(pilot_home, monkeypatch):
    server, port, hookd = _start_server([], monkeypatch)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.read() == b"ok\n"
    finally:
        server.shutdown()


def test_stop_event_wakes_matching_active_pilot(pilot_home, monkeypatch):
    records = [
        {"name": "myproj", "cwd": "/home/user/myproj", "state": "active"},
        {"name": "other", "cwd": "/home/user/other", "state": "active"},
    ]
    server, port, hookd = _start_server(records, monkeypatch)
    try:
        body = json.dumps(
            {"hook_event_name": "Stop", "cwd": "/home/user/myproj/sub"}
        ).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/hook", body=body, headers={"Content-Length": str(len(body))})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 204

        marker = pilot_home.WAKE / "myproj.wake"
        assert marker.exists()
        other_marker = pilot_home.WAKE / "other.wake"
        assert not other_marker.exists()

        day = list(pilot_home.EVENTS.glob("*.jsonl"))
        assert len(day) == 1
        events = [json.loads(l) for l in day[0].read_text().splitlines()]
        assert events[-1]["hook_event_name"] == "Stop"
    finally:
        server.shutdown()


def test_stop_event_unrelated_cwd_no_marker(pilot_home, monkeypatch):
    records = [{"name": "myproj", "cwd": "/home/user/myproj", "state": "active"}]
    server, port, hookd = _start_server(records, monkeypatch)
    try:
        body = json.dumps(
            {"hook_event_name": "Stop", "cwd": "/home/user/unrelated"}
        ).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/hook", body=body, headers={"Content-Length": str(len(body))})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 204
        assert not (pilot_home.WAKE / "myproj.wake").exists()
    finally:
        server.shutdown()


def test_inactive_pilot_not_woken(pilot_home, monkeypatch):
    records = [{"name": "stopped", "cwd": "/home/user/stopped", "state": "stopped"}]
    server, port, hookd = _start_server(records, monkeypatch)
    try:
        body = json.dumps(
            {"hook_event_name": "Stop", "cwd": "/home/user/stopped"}
        ).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/hook", body=body, headers={"Content-Length": str(len(body))})
        resp = conn.getresponse()
        resp.read()
        assert not (pilot_home.WAKE / "stopped.wake").exists()
    finally:
        server.shutdown()


def test_authorised_loopback_and_token(pilot_home, monkeypatch):
    _, _, hookd = _start_server([], monkeypatch)

    class FakeClient:
        def __init__(self, addr, headers=None):
            self.client_address = (addr,)
            self.headers = headers or {}

    class FakeHeaders(dict):
        def get(self, key, default=""):
            return super().get(key, default)

    h = hookd.Handler.__new__(hookd.Handler)
    h.client_address = ("127.0.0.1", 12345)
    assert h._authorised() is True

    h.client_address = ("10.0.0.5", 12345)
    h.headers = FakeHeaders()
    assert h._authorised() is False

    token_file = pilot_home.HOME.parent / "token.txt"
    settings = dict(config.settings())
    token_file.write_text("secret-token\n")
    monkeypatch.setenv("CLAUDE_PILOT_HOOKD_TOKEN_FILE", str(token_file))
    h.headers = FakeHeaders({"X-Pilot-Token": "secret-token"})
    assert h._authorised() is True
    h.headers = FakeHeaders({"X-Pilot-Token": "wrong"})
    assert h._authorised() is False
