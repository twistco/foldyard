#!/usr/bin/env python3
"""SA-token minter — HOST side of the metadata emulator. Runs on the Mac.

Holds the only thing that must never enter the VM: the gcloud credentials. The
on-network emulator (server.py) resolves which SA a calling container should get, then
POSTs the SA here; this validates it against an allowlist and mints a short-lived
impersonated access token with `gcloud`. The long-lived creds stay on the Mac; only a
≤1h token crosses the boundary, and only for an allowlisted SA.

Launch with `just gcp-metadata-minter` (see that recipe).

Env:
  GCP_SA_ALLOWLIST      comma-separated SA emails this minter may impersonate (REQUIRED —
                        empty ⇒ refuse everything; never impersonate arbitrary SAs)
  GCP_ALLOW_USER_TOKEN  "1" → mint the Mac identity's OWN token (no impersonation) for a request
                        flagged `user_escalatable` (the dev box carries the gcp.userEscalatable
                        label; app containers never do). EMERGENCY mode — only `fy host` sets it,
                        for `gcp=user` (TTL-bound, auto-reverts). The legacy sentinel SA "user" is
                        still honoured the same way for older boxes.
  GCP_TOKEN_REFRESH     expires_in (s) reported to clients — a SHORT window (default =
                        TOKEN_LIFETIME's old margin) so a `fy host` mode change reaches the box
                        and gcp=off revokes within it, instead of the box clinging to a ~1h token.
  MINTER_SECRET         if set, require a matching X-Minter-Secret header (guards the
                        port against anything else on the LAN that can reach it)
  LISTEN_PORT           default 8079
  TOKEN_LIFETIME        seconds, default 3600 (gcloud caps impersonated tokens at 3600)
  GCP_MINT_TIMEOUT      seconds a single `gcloud` mint may take before the request fails 502
                        (default 30). A HARD bound on the response, not just on the child —
                        see _run_noninteractive.
  GCP_MINTER_LOG_FILE   if set, append a JSONL line per request (ts, sa, user, status, error)
                        for the TUI's "GCP Tokens" panel. The token itself is NEVER logged —
                        only WHICH identity was minted and the outcome (the security-relevant bit).

Prereq: `gcloud auth login` on the Mac, and the Mac identity must hold
serviceAccountTokenCreator on each allowlisted SA (PAM-elevate for the broad ones).
"""

import contextlib
import json
import os
import re
import signal
import subprocess
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ALLOWLIST = {s.strip() for s in os.environ.get("GCP_SA_ALLOWLIST", "").split(",") if s.strip()}
ALLOW_USER = os.environ.get("GCP_ALLOW_USER_TOKEN") == "1"
SECRET = os.environ.get("MINTER_SECRET", "")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8079"))
LIFETIME = int(os.environ.get("TOKEN_LIFETIME", "3600"))
# Hard wall-clock bound on one mint. Every caller behind us has its own (shorter) patience, so a
# mint that can't finish must FAIL rather than hang — an unanswered request is indistinguishable
# from a dead host and strands the box with no token and no reason.
MINT_TIMEOUT = int(os.environ.get("GCP_MINT_TIMEOUT", "30"))
# expires_in we report to clients — short so mode changes propagate / off revokes quickly. Default
# keeps the old behaviour (refresh ~5 min before the real expiry) when the var isn't set.
REFRESH = int(os.environ.get("GCP_TOKEN_REFRESH", str(max(60, LIFETIME - 300))))
# Per-request JSONL log (the TUI's GCP Tokens panel tails it). Empty ⇒ logging off.
LOG_FILE = os.environ.get("GCP_MINTER_LOG_FILE", "")
_LOG_MAX_BYTES = int(os.environ.get("GCP_MINTER_LOG_MAX_BYTES", str(512 * 1024)))
_LOG_BACKUPS = int(os.environ.get("GCP_MINTER_LOG_BACKUPS", "5"))
_SA_RE = re.compile(r"^[a-z0-9-]+@[a-z0-9.-]+\.iam\.gserviceaccount\.com$")


def _rotate_log() -> None:
    """Rotate LOG_FILE to a dated backup (<stem>.<UTC-stamp>.jsonl) and prune to the newest
    _LOG_BACKUPS — readable, bounded history matching foldyard's config.rotated_logs naming."""
    log = Path(LOG_FILE)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log.replace(log.with_name(f"{log.stem}.{stamp}{log.suffix}"))
    backups = sorted(log.parent.glob(f"{log.stem}.*{log.suffix}"))
    for old in backups[:-_LOG_BACKUPS] if _LOG_BACKUPS > 0 else backups:
        try:
            old.unlink()
        except OSError:
            pass


def _append_log(entry: dict) -> None:
    """Append one JSONL line to LOG_FILE, rotating once it grows past _LOG_MAX_BYTES (the panel
    tails the live log + its dated backups). Best-effort: a logging failure must never break
    token minting."""
    if not LOG_FILE:
        return
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        try:
            if os.path.getsize(LOG_FILE) > _LOG_MAX_BYTES:
                _rotate_log()
        except OSError:
            pass
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _gcloud_error(stderr: str) -> str:
    """gcloud prefixes every impersonated call with a WARNING banner ("This command is using
    service account impersonation…"); on failure that boilerplate eats the log's truncation
    budget and hides the actual ERROR line, so drop WARNING lines before truncating (falling
    back to the raw text if nothing else remains)."""
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    kept = "\n".join(ln for ln in lines if not ln.startswith("WARNING")).strip()
    return kept or stderr.strip()


# gcloud's several spellings of "the host session lapsed" — the one mint failure an operator can
# clear with a single command, and the one that otherwise reads as an anonymous 502 in the mint log.
_REAUTH_MARKERS = ("reauthentication failed", "invalid_rapt", "cannot prompt", "reauth")


def _remedy(err: str) -> str:
    """Suffix naming the fix when the failure is a lapsed host-side login. Everything else keeps
    gcloud's own words — a wrong remedy is worse than none."""
    low = err.lower()
    if any(m in low for m in _REAUTH_MARKERS):
        return " — run `gcloud auth login` on the host"
    return ""


def _run_noninteractive(args: list[str], timeout: int) -> str:
    """Run argv with a CLOSED stdin and a bound that really does apply to the RESPONSE.

    Two failure modes this exists to prevent, both observed live as "the minter never answers":

    stdin. The supervisor runs from `fy host`'s foreground terminal, so an inherited stdin is a
    live TTY. gcloud, seeing one, answers a reauth requirement with an interactive prompt and
    waits forever for input nobody will type — and with the output captured, the prompt is
    invisible, so the daemon merely looks slow. Non-interactive it exits instead with
    "cannot prompt during non-interactive execution", which is an error we can report.

    the bound. `subprocess.run(timeout=…)` bounds only the direct child: on expiry it kills that
    child and then re-reads the pipes to reap, and a grandchild holding the inherited stdout keeps
    that read blocked indefinitely. gcloud is a wrapper that execs Python and can leave helpers
    behind, so its nominal 30s becomes unbounded. Own the group (start_new_session) and kill the
    GROUP, then reap without touching the pipes again.
    """
    proc = subprocess.Popen(  # fixed argv, no shell
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()  # reap only — re-reading the pipes is the hang we just avoided
        raise
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, out, err)
    return out.strip()


def mint(sa: str, user: bool) -> str:
    # Fixed argv (no shell) + validated SA; gcloud uses the host identity's logged-in credentials.
    # The user-token path (gated by the caller on GCP_ALLOW_USER_TOKEN) mints that identity's OWN
    # token — no impersonation, no --lifetime (plain user tokens don't take one).
    args = ["gcloud", "auth", "print-access-token"]
    if not user:
        args += [f"--impersonate-service-account={sa}", f"--lifetime={LIFETIME}"]
    return _run_noninteractive(args, MINT_TIMEOUT)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        # Liveness for the host-side probe: proof that THIS process still owns the port. "Something
        # is listening" can be satisfied by anything that grabbed it — and a forwarder binding
        # 127.0.0.1 beats our wildcard bind for loopback traffic, after which every mint hangs
        # while the daemon looks healthy. Deliberately UNLOGGED (it fires on a timer and would bury
        # the token events the TUI panel exists to show) and it carries no identity or secret, so
        # it needs no MINTER_SECRET gate: it discloses nothing a connect didn't already.
        self._send(200, {"foldyard": "gcp-minter", "port": LISTEN_PORT})

    def do_POST(self) -> None:
        code, obj, sa, user = self._handle()
        # Log the OUTCOME (never the token in obj["access_token"]): which identity, granted or not.
        _append_log(
            {
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                "sa": sa,
                "user": user,
                "status": code,
                **({"error": obj["error"][:200]} if "error" in obj else {}),
            }
        )
        self._send(code, obj)

    def _handle(self) -> tuple[int, dict, str, bool]:
        """Decide the response: (http code, json body, resolved SA, is-user-token). Pure of I/O
        beyond reading the request + minting, so do_POST can log the outcome from one place."""
        if SECRET and self.headers.get("X-Minter-Secret") != SECRET:
            return 403, {"error": "bad or missing X-Minter-Secret"}, "", False
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
            sa = body.get("sa", "")
            # The emulator flags the dev box (gcp.userEscalatable label); the legacy "user"
            # sentinel SA from older boxes means the same thing.
            escalatable = bool(body.get("user_escalatable")) or sa == "user"
        except Exception as e:
            return 400, {"error": f"bad request: {e}"}, "", False
        # The emergency user token: only for an escalatable caller AND only when this mode enabled
        # it (gcp=user). Anyone else — i.e. every app container — falls through to SA impersonation.
        user = escalatable and ALLOW_USER
        if escalatable and not ALLOW_USER and sa == "user":
            return 403, {"error": "user-token mode not enabled on this minter"}, sa, user
        if not user:
            if not _SA_RE.match(sa):
                return 400, {"error": f"not an SA email: {sa!r}"}, sa, user
            if sa not in ALLOWLIST:
                return 403, {"error": f"SA not in allowlist: {sa}"}, sa, user
        try:
            token = mint(sa, user)
        except subprocess.CalledProcessError as e:
            err = _gcloud_error(e.stderr)
            return 502, {"error": f"gcloud failed: {err[:300]}{_remedy(err)}"}, sa, user
        except Exception as e:
            return 502, {"error": str(e)}, sa, user
        # Short expires_in (REFRESH) so the emulator + clients refetch soon and see mode changes.
        return 200, {"access_token": token, "expires_in": REFRESH}, sa, user

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    if not ALLOWLIST:
        raise SystemExit(
            "minter: set GCP_SA_ALLOWLIST (comma-separated SA emails) — refusing an empty allowlist"
        )
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"[minter] :{LISTEN_PORT} — allowlist: {', '.join(sorted(ALLOWLIST))}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
