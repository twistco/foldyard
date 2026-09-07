#!/usr/bin/env python3
"""GCE metadata-server emulator — per-container SA, prod-parity, no token files.

Runs as a container ON the podman network. Speaks just enough of the GCE metadata
protocol that Google auth libraries (Node `google-auth-library`, Python `google-auth`,
`gcloud`) believe they're on a GCE instance with an attached service account. For each
request it:

  1. resolves the CALLER by source IP → its container (via the rootless podman socket),
  2. reads that container's `gcp.serviceAccount` label to get the target SA,
  3. asks the Mac-side minter (which holds the gcloud creds — they never enter the VM)
     for a short-lived impersonated token for that SA, caching per-SA until expiry,
  4. returns it in the GCE metadata JSON shape.

So the app container (label = the app's runtime SA) and the dev box (label = a read-only
log-reader SA) each transparently get a token for THEIR identity — the same mechanism
prod uses (attached SA via the metadata server), with no token on any disk.

Why source-IP resolution lives here and not on the Mac: a container→Mac request is
NAT'd through gvproxy, so the Mac only sees the gateway IP. On the podman network the
emulator sees the real container IP, which is what makes per-caller resolution possible.

Env:
  GCP_MINTER_URL   URL of the Mac-side minter (e.g. http://host.containers.internal:8079)
  GCP_PROJECT      project id served at /project/project-id — REQUIRED, no default
  SA_LABEL         container label holding the SA email (default gcp.serviceAccount)
  MINTER_SECRET    shared secret sent to the minter as X-Minter-Secret (optional)
  DOCKER_SOCK      path to the engine socket (default /var/run/docker.sock)
  LISTEN_PORT      default 80
  GCP_MINTER_TIMEOUT  seconds to wait on the minter (default 45) — keep ABOVE the minter's own
                   GCP_MINT_TIMEOUT so its error, not our timeout, is what the box is told

Stdlib only (so it runs on a stock python image with no pip install).
"""

import json
import os
import socket
import sys
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib import request as urlrequest

# REQUIRED, deliberately without a default. This value is served as the instance's project id, so
# a baked-in fallback would quietly hand every consumer some other project's name — and the SA
# emails the minter is asked for are built from it. Empty is a misconfiguration, and the emulator
# says so at startup rather than serving a plausible lie (checked in main(), so importing the
# module for tests stays side-effect free).
PROJECT = os.environ.get("GCP_PROJECT", "")
# gcloud's "am I on GCE?" probe reads project/numeric-project-id (NOT the root ping that the
# google-auth libraries use), so the emulator must serve it or gcloud declares "not on GCE" and
# ignores the metadata token entirely. The value only feeds GCE-detection + a default project here
# (callers pass --project), so a placeholder is fine; override with GCP_NUMERIC_PROJECT if needed.
NUMERIC_PROJECT = os.environ.get("GCP_NUMERIC_PROJECT", "000000000000")
MINTER_URL = os.environ.get("GCP_MINTER_URL", "http://host.containers.internal:8079")
# How long to wait on the minter. MUST comfortably exceed the minter's own GCP_MINT_TIMEOUT
# (default 30): if we give up first we replace its real, actionable error with our anonymous
# "no token available", which is exactly the reading that sent a live debugging session after
# the wrong cause. Losing that race costs nothing else — the caller has long since gone.
MINTER_TIMEOUT = float(os.environ.get("GCP_MINTER_TIMEOUT", "45"))
SA_LABEL = os.environ.get("SA_LABEL", "gcp.serviceAccount")
# Label marking a caller eligible for the emergency user token (the dev box only). Passed to the
# minter, which grants it only when this is set AND its mode enabled it — apps never carry it.
ESCALATE_LABEL = os.environ.get("GCP_ESCALATE_LABEL", "gcp.userEscalatable")
MINTER_SECRET = os.environ.get("MINTER_SECRET", "")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "80"))
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Token cache keyed by (sa, escalatable) -> (token, monotonic_good_until). Short-lived (the minter
# reports a small expires_in) so a mode change the minter picks up reaches callers within ~one ttl.
_tok_cache: dict[str, tuple[str, float]] = {}


# ── the rootless podman socket (Docker-compatible API over a unix socket) ──────────
class _UnixHTTPConnection(HTTPConnection):
    def __init__(self, sock_path: str) -> None:
        super().__init__("localhost")
        self._sock_path = sock_path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(self._sock_path)
        self.sock = s


def _docker_get(path: str):
    conn = _UnixHTTPConnection(DOCKER_SOCK)
    try:
        conn.request("GET", path, headers={"Host": "localhost"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"docker GET {path} → {resp.status}: {body[:200]!r}")
        return json.loads(body)
    finally:
        conn.close()


def sa_for_ip(ip: str) -> tuple[str, bool] | None:
    """Map a caller IP → its container → (SA from `gcp.serviceAccount`, escalatable?). Escalatable
    when the container also carries the `gcp.userEscalatable` label (the dev box) — that flag lets
    the minter swap in the user's own token in gcp=user. None when the container is unlabelled."""
    for c in _docker_get("/containers/json"):
        nets = (c.get("NetworkSettings") or {}).get("Networks") or {}
        if any((n or {}).get("IPAddress") == ip for n in nets.values()):
            labels = c.get("Labels") or {}
            sa = labels.get(SA_LABEL)
            if sa:
                return sa, str(labels.get(ESCALATE_LABEL, "")).lower() in ("1", "true", "yes")
            # Container found but unlabelled — explicit, so we don't hand it a token.
            return None
    return None


# ── token minting (delegated to the Mac, cached per (sa, escalatable)) ──────────────
def token_for_sa(sa: str, escalatable: bool) -> tuple[str, int]:
    """Return (access_token, expires_in_seconds), minting via the Mac on miss/expiry. Raises on a
    minter error (down / refused) so the caller can answer 404 ⇒ "no service account"."""
    key = f"{sa}\x00{int(escalatable)}"
    now = time.monotonic()
    cached = _tok_cache.get(key)
    if cached and now < cached[1]:
        return cached[0], max(1, int(cached[1] - now))
    headers = {"Content-Type": "application/json"}
    if MINTER_SECRET:
        headers["X-Minter-Secret"] = MINTER_SECRET
    payload = json.dumps({"sa": sa, "user_escalatable": escalatable}).encode()
    req = urlrequest.Request(MINTER_URL, data=payload, headers=headers, method="POST")
    with urlrequest.urlopen(req, timeout=MINTER_TIMEOUT) as r:
        data = json.loads(r.read())
    token, ttl = data["access_token"], int(data["expires_in"])
    # Refetch a little early so callers never see an expired token; tiny margin for short ttls so we
    # still cache (rather than re-mint every request) when the minter reports a ~60s refresh window.
    margin = min(15, max(1, ttl // 4))
    _tok_cache[key] = (token, now + max(1, ttl - margin))
    return token, ttl


def _reason(e: Exception) -> str:
    """Why a mint failed, in words the operator can act on.

    A refusal arrives as an HTTPError whose ``str()`` is only "HTTP Error 502: Bad Gateway" — the
    minter's actual message (which names the fix, e.g. a lapsed host login) is in the BODY. Read
    it, or the box is told "no token available" for a minter that is up and explaining itself.
    """
    if isinstance(e, urlerror.HTTPError):
        try:
            body = e.read().decode(errors="replace").strip()
        except Exception:
            body = ""
        # The minter answers JSON; a forwarder or proxy shadowing it answers text or HTML, and
        # that body is the whole explanation — never let a parse failure erase it.
        msg = body
        if body:
            try:
                parsed = json.loads(body)
                msg = parsed.get("error", body) if isinstance(parsed, dict) else body
            except ValueError:
                pass
        return f"minter {e.code}: {msg[:300]}" if msg else f"minter {e.code}"
    return f"{type(e).__name__}: {e}"


# ── the HTTP surface (subset of the GCE metadata protocol) ─────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/text"):
        self.send_response(code)
        self.send_header("Metadata-Flavor", "Google")  # detection + responses
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # quieter than default access log
        pass

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        # Root ping — how the libraries detect "am I on GCE?": 200 + the flavor header.
        if path in ("/", "/computeMetadata/v1/", "/computeMetadata/v1"):
            return self._send(200, b"computeMetadata/\n")

        # All real endpoints require the anti-SSRF header the libraries always send.
        if self.headers.get("Metadata-Flavor") != "Google":
            return self._send(403, b"Missing Metadata-Flavor: Google\n")

        if path == "/computeMetadata/v1/project/project-id":
            return self._send(200, PROJECT.encode())
        # gcloud's GCE-detection reads this (the libraries' root ping isn't enough for the CLI).
        if path == "/computeMetadata/v1/project/numeric-project-id":
            return self._send(200, NUMERIC_PROJECT.encode())

        sa_prefix = "/computeMetadata/v1/instance/service-accounts/"
        if path.startswith(sa_prefix):
            resolved = sa_for_ip(self.client_address[0])
            if not resolved:
                return self._send(404, b"no gcp.serviceAccount label for the calling container\n")
            sa, escalatable = resolved
            rest = path[len(sa_prefix) :].rstrip("/")  # "", default, default/token, <email>/token
            leaf = rest.rsplit("/", 1)[-1] if rest else ""

            if rest == "":  # list the accounts
                return self._send(200, f"default/\n{sa}/\n".encode())
            if leaf == "token":
                try:
                    token, ttl = token_for_sa(sa, escalatable)
                except Exception as e:
                    # Minter down or refusing (gcp=off, or SA not allowed) ⇒ no token, so the caller
                    # (gcloud / google-auth) cleanly sees "no service account" rather than a 5xx.
                    # The STATUS stays 404 for that contract, but the REASON must survive: the
                    # minter says things like "gcloud failed: Reauthentication failed — run
                    # `gcloud auth login` on the host", and swallowing that leaves the box with a
                    # generic "minter down or mode off" for a minter that is up and answering.
                    # stderr so it lands in `fy logs metadata-emulator` even if the body is
                    # never read.
                    reason = _reason(e)
                    print(f"[metadata] mint failed for {sa}: {reason}", file=sys.stderr, flush=True)
                    return self._send(404, f"no token available — {reason}\n".encode())
                body = json.dumps(
                    {"access_token": token, "expires_in": ttl, "token_type": "Bearer"}
                ).encode()
                return self._send(200, body, "application/json")
            if leaf == "email":
                return self._send(200, sa.encode())
            if leaf == "scopes":
                return self._send(200, ("\n".join(SCOPES) + "\n").encode())
            if leaf in ("default", sa):  # the account node itself (often ?recursive=true)
                body = json.dumps({"aliases": ["default"], "email": sa, "scopes": SCOPES}).encode()
                return self._send(200, body, "application/json")

        return self._send(404, b"not found\n")


def main() -> None:
    if not PROJECT:
        raise SystemExit(
            "[metadata] GCP_PROJECT is not set — refusing to start.\n"
            "  Set [plugins.gcp-metadata].project in foldyard.toml (or GCP_PROJECT in the\n"
            "  emulator service's environment). There is deliberately no default: the project id\n"
            "  is served as this instance's identity, and the SA emails are built from it."
        )
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(
        f"[metadata] serving on :{LISTEN_PORT} — project={PROJECT}, "
        f"minter={MINTER_URL}, label={SA_LABEL}",
        flush=True,
    )
    srv.serve_forever()


if __name__ == "__main__":
    main()
