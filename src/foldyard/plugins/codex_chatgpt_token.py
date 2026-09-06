"""ChatGPT-subscription refresh-minter for keyless Codex (the egress proxy's ``INJECT_COMMAND``).

Codex in ``auth_mode: chatgpt`` authenticates with a SHORT-LIVED access-token JWT held in
``~/.codex/auth.json`` and refreshed via a (rotating) refresh token — unlike a static API key. So
keyless Codex-ChatGPT can't ride the :mod:`static_token` minter; this is a real refresh-minter that
runs HOST-side (on the Mac, where the real ``auth.json`` lives) and the proxy injects the *current*
access token into the box's egress to ``chatgpt.com``. The box only ever carries a DUMMY auth.json
(a far-future-exp JWT, so codex there never refreshes); the real token never enters it.

Contract (the proxy runs this and reads ``{"value","ttl"}`` from stdout)::

    python -m foldyard.plugins.codex_chatgpt_token [AUTH_JSON_PATH]

It reads the access + refresh tokens from ``auth.json`` (default ``$CODEX_HOME``/``~/.codex``), and:
  - if the access token still has > REFRESH_WINDOW left, returns it as-is (no network);
  - else POSTs ``grant_type=refresh_token`` to ``auth.openai.com/oauth/token`` (the EXACT shape
    codex itself uses — verified from openai/codex ``login/src/auth/manager.rs``: JSON body
    ``{client_id, grant_type, refresh_token}``, ``CLIENT_ID`` below), then writes the rotated tokens
    back to ``auth.json`` ATOMICALLY (preserving every other field), exactly like codex does — so
    the one canonical ``auth.json`` keeps the real codex on the Mac working too.

Concurrency: the whole read→refresh→write runs under an ``flock`` on a sidecar lock + re-reads the
file inside the lock (another minter call, or codex if it flocked, may have rotated meanwhile) — so
the single-use refresh token isn't double-spent by concurrent minter calls. (Codex on the Mac
doesn't take this lock, so a refresh racing a manual codex run is still possible but unlikely; if it
ever invalidates the refresh token, ``codex login`` re-establishes it.)

Stdlib only (urllib, no `requests`) — it's a subprocess entry point, NOT on the registry hot path.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# Verified from openai/codex (login/src/auth/manager.rs): the OAuth client id + token endpoint codex
# uses for the ChatGPT refresh-token grant. The egress to these is host-side (the Mac), never the
# box, so they're not behind the box's egress wall.
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

_REFRESH_WINDOW = 300  # refresh when the access token has < 5 min left (matches codex's window)
_TTL_MARGIN = 60  # report a ttl a touch shorter than exp, so the proxy re-mints before expiry


def _auth_json_path(argv: list[str]) -> Path:
    if argv:
        return Path(argv[0]).expanduser()
    base = os.environ.get("CODEX_HOME")
    return (Path(base) if base else Path.home() / ".codex").expanduser() / "auth.json"


def _jwt_exp(token: str) -> int | None:
    """The ``exp`` (unix seconds) from a JWT's payload, or ``None`` if unparseable. We only READ the
    claim (to decide freshness) — no signature check, like codex's own freshness logic."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64url padding
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:
        return None


def _refresh(refresh_token: str) -> dict:
    """POST the refresh-token grant; return the parsed JSON (``access_token``/``refresh_token``/
    ``id_token`` all optional). Raises on transport/HTTP error (the caller turns that into a clear
    non-zero exit so the proxy keeps its cached value rather than injecting nothing)."""
    body = json.dumps(
        {"client_id": _CLIENT_ID, "grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode()
    req = urllib.request.Request(
        _TOKEN_URL, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _now_iso() -> str:
    # Match codex's last_refresh shape (RFC3339 UTC with a trailing Z).
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _atomic_write(path: Path, data: dict) -> None:
    """Write ``auth.json`` atomically (temp in the same dir + ``os.replace``), 0600 — so a reader
    (codex, or the next mint) never sees a half-written file, and the secret stays owner-only."""
    tmp = path.with_name(path.name + ".fy-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)
    os.replace(tmp, path)


@contextlib.contextmanager
def _locked(path: Path):
    """Best-effort exclusive lock (flock on a sidecar) so concurrent MINTER calls serialize their
    refresh — the single-use refresh token can't be double-spent within this process family. No-op
    where fcntl is unavailable (non-unix); codex on the Mac doesn't take this lock (documented)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover — non-unix
        yield
        return
    lock = path.with_name(path.name + ".fy-lock")
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_tokens(path: Path) -> tuple[dict, dict]:
    data = json.loads(path.read_text())
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("no `tokens` object")
    return data, tokens


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = _auth_json_path(args)
    try:
        data, tokens = _read_tokens(path)
    except (OSError, ValueError) as e:
        print(f"codex_chatgpt_token: cannot read ChatGPT tokens from {path}: {e}", file=sys.stderr)
        return 1
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not access or not refresh:
        print(f"codex_chatgpt_token: {path} has no access/refresh token", file=sys.stderr)
        return 1

    now = int(time.time())
    exp = _jwt_exp(access)
    if exp is not None and exp - now > _REFRESH_WINDOW:
        # Still fresh — inject the current token, no network, ttl until just before it expires.
        json.dump({"value": access, "ttl": max(1, exp - now - _TTL_MARGIN)}, sys.stdout)
        return 0

    # Stale (or no exp): refresh under the lock, re-reading inside it in case another mint/codex
    # already rotated the token (then we don't spend our now-stale refresh token a second time).
    with _locked(path):
        with contextlib.suppress(OSError, ValueError):
            data, tokens = _read_tokens(path)  # re-read inside the lock (may have been rotated)
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        if not refresh:
            print(f"codex_chatgpt_token: {path} lost its refresh token", file=sys.stderr)
            return 1
        exp = _jwt_exp(access) if access else None
        if exp is None or exp - now <= _REFRESH_WINDOW:  # still needs refreshing
            try:
                resp = _refresh(refresh)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:200] if e.fp else ""
                print(f"codex_chatgpt_token: refresh HTTP {e.code}: {detail}", file=sys.stderr)
                return 1
            except Exception as e:
                print(f"codex_chatgpt_token: refresh error: {e}", file=sys.stderr)
                return 1
            access = str(resp.get("access_token") or access)
            if resp.get("refresh_token"):
                tokens["refresh_token"] = resp["refresh_token"]
            if resp.get("id_token"):
                tokens["id_token"] = resp["id_token"]
            tokens["access_token"] = access
            data["tokens"] = tokens
            data["last_refresh"] = _now_iso()
            _atomic_write(path, data)
            exp = _jwt_exp(access)

    ttl = max(1, (exp - now - _TTL_MARGIN)) if exp is not None else 3600
    json.dump({"value": access, "ttl": ttl}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
