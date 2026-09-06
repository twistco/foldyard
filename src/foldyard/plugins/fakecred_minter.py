#!/usr/bin/env python3
"""Fake credential minter — the zero-secret daemon behind the ``fakecred`` TESTING axis.

Behaves like the real credential chain without any real credential, so the whole
mode/TTL/daemon/probe machinery can be exercised live (docs/testing-modes.md): serves
short-lived dummy tokens while "capable", and refuses them (403, like an expired PAM grant)
when the capability file says the grant lapsed — flip it with:

    echo lapsed > ~/.foldyard/<project>/fakecred-capability     # simulate the lapse
    echo ok     > ~/.foldyard/<project>/fakecred-capability     # restore

Env: LISTEN_PORT; FAKECRED_CAPABILITY_FILE (missing file or "ok" = capable).
Stdlib only; run from the per-project staged copy (see the plugin's daemon ``stage``).
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8290"))
CAPABILITY_FILE = os.environ.get("FAKECRED_CAPABILITY_FILE", "")


def capable() -> tuple[bool, str]:
    try:
        text = Path(CAPABILITY_FILE).read_text().strip() if CAPABILITY_FILE else "ok"
    except FileNotFoundError:
        return True, "ok"  # no file yet = capable (the resting state)
    except OSError as e:  # unreadable file (perms, a directory) — fail CLOSED, like a credential
        return False, f"capability file unreadable: {e}"
    return (True, "ok") if text in ("", "ok") else (False, text)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
            return
        if self.path == "/token":
            ok, detail = capable()
            if ok:
                self._json(200, {"access_token": f"fake-{int(time.time())}", "expires_in": 60})
            else:
                self._json(403, {"error": f"fake capability lapsed: {detail}"})
            return
        self._json(404, {"error": f"unknown path {self.path}"})

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        print(f"[fake-minter] {format % args}", flush=True)


if __name__ == "__main__":
    print(f"[fake-minter] listening on :{LISTEN_PORT} (capability file: {CAPABILITY_FILE or '—'})")
    ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()
