"""claude-pilot-hookd: receive Claude Code hook events and record them.

Claude Code's Stop hook POSTs its JSON here; every event is appended to
EVENTS/<date>.jsonl for later inspection. Deliberately dumb: it records and,
for a Stop event, drops a wake marker for any active pilot whose cwd matches;
it never judges or decides anything else. A hook that blocks or errors
interrupts the session that fired it, so every path here ends in a response
and no exception escapes.

Loopback is trusted. Reaching this from anywhere else requires the token in
`X-Pilot-Token` to match the token file; without a token file the server
binds loopback only, since an unauthenticated write endpoint reachable from
elsewhere is unsafe by default.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claude_pilot import config

MAX_BODY = 512 * 1024


def _load_token() -> str:
    settings = config.settings()
    token_file = config.Path(settings["hookd_token_file"]).expanduser()
    if token_file.exists():
        return token_file.read_text().strip()
    return ""


class Handler(BaseHTTPRequestHandler):
    def _authorised(self) -> bool:
        """Loopback is trusted; anything else must present the token."""
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        token = _load_token()
        return bool(token) and self.headers.get("X-Pilot-Token", "") == token

    def do_POST(self):  # noqa: N802
        if not self._authorised():
            # A rejected hook must not stall the session firing it, and an
            # attacker learns nothing extra from a 204 either way.
            self.send_response(204)
            self.end_headers()
            print(
                f"hookd: rejected unauthenticated POST from {self.client_address[0]}",
                file=sys.stderr,
            )
            return
        try:
            length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                payload = {"unparsed": raw.decode("utf-8", "replace")[:4000]}

            payload["_received_at"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(timespec="seconds")
            payload["_path"] = self.path

            config.EVENTS.mkdir(parents=True, exist_ok=True)
            day = config.EVENTS / f"{datetime.date.today().isoformat()}.jsonl"
            with day.open("a") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            day.chmod(0o600)

            if payload.get("hook_event_name") == "Stop" and payload.get("_agent") != "codex":
                host = self.headers.get("X-Pilot-Host") or payload.get("_host") or ""
                self._wake_matching(payload.get("cwd") or "", payload["_received_at"], host)

            self.send_response(204)
            self.end_headers()
        except Exception as exc:  # noqa: BLE001
            self.send_response(204)
            self.end_headers()
            print(f"hookd error: {type(exc).__name__}: {exc}", file=sys.stderr)

    def _wake_matching(self, cwd: str, received_at: str, host: str) -> None:
        """Drop a wake marker for every active record whose cwd and host match.

        `host` comes from the `X-Pilot-Host` request header or the payload's
        `_host` field, defaulting to "" (local); a record only wakes for an
        event carrying the same host it was enrolled under.
        """
        from claude_pilot import registry

        if not cwd:
            return
        for rec in registry.all_records():
            if rec.get("state") != "active":
                continue
            if rec.get("host", "") != host:
                continue
            rec_cwd = (rec.get("cwd") or "").rstrip("/")
            if rec_cwd and (cwd == rec_cwd or cwd.startswith(rec_cwd + "/")):
                config.WAKE.mkdir(parents=True, exist_ok=True)
                (config.WAKE / f"{rec['name']}.wake").write_text(received_at)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        """Silence per-request logging; the jsonl is the record."""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--bind", default=None)
    args = ap.parse_args()

    settings = config.settings()
    config.EVENTS.mkdir(parents=True, exist_ok=True)
    port = args.port if args.port is not None else settings["hookd_port"]
    token = _load_token()
    bind = args.bind or ("0.0.0.0" if token else "127.0.0.1")

    server = ThreadingHTTPServer((bind, port), Handler)
    print(
        f"claude-pilot-hookd listening on {bind}:{port} "
        f"({'token required off-loopback' if token else 'loopback only, no token file'}), "
        f"writing {config.EVENTS}",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
