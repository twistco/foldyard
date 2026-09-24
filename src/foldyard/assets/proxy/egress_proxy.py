"""mitmproxy addon — inject a minted Authorization header for specific hosts.

A standalone reimplementation of claude-sandbox's `[[proxy.inject]]` for the podman dev
box (we run our OWN proxy instead of depending on claude-sandbox). It runs on the HOST —
where the minter's gcloud ADC + GitHub egress live — as the box's egress proxy. For each
request to an injected host it OVERWRITES the Authorization header with a token obtained
by running a host command (the minter, `gh-app-token`), caches the token for the ttl the
minter reports, and re-mints + re-issues the request once on an upstream 401.

The security property this buys: the token is added host-side, in flight, so it NEVER
materialises inside the container (not as env, not as a file) — the evolution of the old
exec-injection-of-a-token-file model. The token the minter returns is itself already
≤1h and scoped to one repo + pull_requests/issues (see gh-app-token), so a header that
somehow leaked still self-expires and can do nothing else.

Run it with mitmdump (`fy host` does this for you; the supervisor passes the project's
allocated band port — config.proxy_port() — as --listen-port):
    mitmdump -s egress_proxy.py --listen-host 0.0.0.0 --listen-port 41000

The same addon doubles as a pure CAPTURE proxy: with INJECT_HOST (or INJECT_COMMAND) empty it
injects NOTHING — it just logs every proxied request to PROXY_LOG_FILE, so all dev-box egress is
observable through the one proxy without any credential in play. Injection and capture compose: a
github mode logs everything AND rewrites api.github.com; no injector logs everything and rewrites
nothing.

Phase A′ — the box ALWAYS routes through this (always-on) proxy, so CAPTURE_MODE decides what it
does with HTTPS it isn't injecting:
  - CAPTURE_MODE=full        → MITM-decrypt + log every request, except PASSTHROUGH_HOSTS. What
                               foldyard always sends (ADR-0029 removed the `capture` axis).
  - CAPTURE_MODE=passthrough → blind-tunnel HTTPS without terminating TLS (the box does end-to-end
                               TLS against the REAL cert) and log only an SNI-level row — host +
                               time, no method/path/status. Kept for standalone use of the addon.
The injector host (api.github.com) is ALWAYS decrypted, whatever CAPTURE_MODE is, because we must
read + rewrite its Authorization header. Plain HTTP is always logged in full (it's cleartext, so
there's nothing to passthrough).

Config via env (read once at startup). foldyard's supervisor sets LIVE_FILE, which moves the
rules, the wall switch and the passthrough list out of the env and makes them live:
  LIVE_FILE         a JSON file {"rules": [<rule>, ...], "default_deny": bool, "passthrough":
                    [<pattern>, ...]}, re-read when it changes (per hook + once a second) with no
                    restart. When set, INJECT_*, DEFAULT_DENY and PASSTHROUGH_HOSTS are ignored.
                    Unreadable/malformed ⇒ fail closed (no rules, the wall enforcing, nothing
                    tunnelled). A change that narrows the policy closes the open connections it no
                    longer allows (see Injector.refresh).
  HOST_ENV_FILE     host.env: where a live rule's declared `env` names are resolved (this
                    process's env first — the operator's exports — then the file), re-read when it
                    changes.
Without LIVE_FILE:
  INJECT_RULES      a JSON LIST of injection rules (the multi-injector contract) — one proxy
                    rewriting N hosts, each with its OWN minter + token cache + 401 retry:
                      [{"host","command","header"?,"value_prefix"?,"query_param"?,
                        "path_prefix"?,"retry_401"?,"ca_bundle"?}, ...]
                    When set (non-empty) it WINS over the single INJECT_* vars below. Each object
                    is the same shape as one rule; an entry missing host or command is dropped.
                    EMPTY (default) → fall back to the legacy single-rule env below.
  INJECT_HOST       host to inject for           (default: api.github.com); EMPTY → no
                    injection (capture-only: log every request, rewrite nothing). The legacy
                    single-rule path: used only when INJECT_RULES is empty.
  INJECT_COMMAND    command that mints the token (default: gh-app-token); must print
                    JSON {"value": "<header value>", "ttl": <seconds>} on stdout. EMPTY →
                    no injection (capture-only), same as an empty INJECT_HOST
  CAPTURE_MODE      "full" → MITM-decrypt + log every HTTPS request EXCEPT the trusted
                    PASSTHROUGH_HOSTS (default, back-compat); "passthrough" → blind-tunnel ALL
                    HTTPS not bound for the injector host (no decrypt; real certs end-to-end) and
                    log an SNI-only row. Plain HTTP and the injector host are always decrypted +
                    logged in full regardless.
  PASSTHROUGH_HOSTS comma-list of TRUSTED hosts to blind-tunnel even under CAPTURE_MODE=full
                    (we already trust them + don't need request-level detail; keeps bulk-download
                    hosts fast). Exact hosts or "*.suffix" globs (subdomain match, not the bare
                    domain — Claude Code Web allow-list semantics). Empty → decrypt everything
                    under "full". Ignored under "passthrough" (everything is tunnelled anyway).
  DEFAULT_DENY      "1" → ENFORCE the egress allowlist: refuse any host that isn't allowed
                    (by ALLOW_FILE or the injector host) with a 403 at CONNECT / on the request.
                    Empty/anything else → off (capture/passthrough only, never blocks — the
                    pre-allowlist behaviour). This is the static switch; toggling it restarts the
                    daemon. The live, no-restart part is ALLOW_FILE (re-read per request).
  ALLOW_FILE        path to the resolved effective allowlist JSON the host rewrites on every
                    grant: {"default_deny": <bool>, "allow": ["host"/"*.suffix", ...]}. Re-read
                    per request (mtime-cached) so a host-side `allow` takes effect with NO daemon
                    restart. Missing/malformed ⇒ NO extra allows (fail toward MORE blocking). Only
                    consulted when DEFAULT_DENY is on. Empty → no allowlist file (allow nothing
                    beyond the injector host under default-deny).
  INJECT_HEADER     header to overwrite          (default: Authorization)
  INJECT_VALUE_PREFIX  string PREPENDED to the minted value before injection (e.g. "Bearer " for
                    an OAuth `authorization` header). The header is overwritten verbatim, so the
                    minter returns the BARE token and the scheme is added here. EMPTY (default) →
                    inject the minted value unchanged.
  INJECT_QUERY_PARAM inject the minted value as this URL QUERY PARAM instead of a header
                    (e.g. "userToken" → ?userToken=<value>, for a server that authenticates via
                    the query string). Set → takes precedence over INJECT_HEADER. EMPTY (default)
                    → header injection.
  INJECT_PATH_PREFIX only inject on requests whose path starts with this (e.g. "/mcp"), so one
                    host can serve an app AND a separately-injected endpoint. EMPTY (default) →
                    inject on every request to the host.
  INJECT_RETRY_401  "1" → on a 401 from the host, invalidate + re-mint + re-issue the
                    request once, handing the retried response back to the client
                    (default: "1")
  INJECT_RETRY_CA_BUNDLE  CA bundle the 401 re-issue verifies the upstream against. Empty
                    → system trust (correct for api.github.com). A path pins a specific
                    cert (used by the e2e to trust its self-signed upstream). TLS is never
                    disabled.
  PROXY_LOG_FILE    JSONL network log of every proxied response (ts, method, host,
                    path, status, injected, replayed; passthrough/blocked rows add a flag). Default
                    ~/.foldyard/logs/egress.jsonl (foldyard's supervisor overrides
                    this with the per-project ~/.foldyard/<project>/logs path) —
                    OUTSIDE the repo on purpose (the shared mount must not carry an
                    egress log the box can read-write). "" disables. Rotated to dated
                    backups (<name>.<UTC-stamp>.jsonl) past PROXY_LOG_MAX_BYTES.
  PROXY_LOG_MAX_BYTES  rotate the live log past this many bytes (default 5 MiB).
  PROXY_LOG_BACKUPS    how many dated backups to keep (default 5; 0 = none), pruned
                    oldest-first — so the footprint is bounded at ~(BACKUPS+1)×MAX_BYTES.

Deps: pip install mitmproxy. Minters carry their OWN deps: consumer minter scripts (e.g. the
github plugin's gh-app-token) are launched via ``uv run --script`` and declare PEP 723 inline
metadata, so nothing minter-specific lives in this proxy's environment.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

# mitmproxy is provided at runtime by mitmdump (and stubbed in tests); a bare static-analysis env
# without it shouldn't surface a missing-import error for this standalone, out-of-tree addon.
from mitmproxy import ctx, http, tls  # pyright: ignore[reportMissingImports]


class _DropConnectChatter(logging.Filter):
    """Suppress mitmproxy's per-connection 'connect'/'disconnect' termlog lines (client AND server).

    foldyard health-probes the proxy port every few seconds (``devmode.probe`` opens a bare TCP
    socket just to test the daemon is listening), and mitmproxy logs every accepted-then-closed
    socket as a connect+disconnect pair at INFO — so an empty liveness probe floods the host log
    with noise lines, drowning real traffic. Actual egress is logged to PROXY_LOG_FILE by this
    addon, so these connection-lifecycle lines are pure duplication.

    Two message shapes, both on the ``mitmproxy.proxy.server`` logger (see its ``proxy/server.py``):
      - ``client connect`` / ``client disconnect`` — bare, exact-match.
      - ``server connect {addr}`` / ``server disconnect {addr}`` — carry the upstream address, so
        they need a PREFIX match, not equality. Genuine failures use distinct prefixes
        (``error establishing server connection:``, ``server connection to … killed before
        connect:``) and so are NOT swallowed — only the success/teardown INFO lines are.

    Filtering the source logger drops the record before it reaches any handler (logger filters
    aren't applied on propagation, but these log directly here)."""

    _NOISE = frozenset({"client connect", "client disconnect"})
    _NOISE_PREFIXES = ("server connect ", "server disconnect ")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return msg not in self._NOISE and not msg.startswith(self._NOISE_PREFIXES)


class _DropWebsocketPingPong(logging.Filter):
    """Suppress mitmproxy's per-frame WebSocket ping/pong termlog lines, e.g.::

        Received WebSocket ping from server (payload: b'_\\xfaf\\xde')
        Received WebSocket pong from client (payload: b'_\\xfaf\\xde')

    A live WebSocket (e.g. the platform's dev HMR / any long-poll) heartbeats every few seconds,
    and mitmproxy logs EVERY ping and pong frame at INFO — pure keep-alive noise that livestreams
    into the host log and buries real egress. The proxy layer emits these via ``commands.Log`` on
    the SAME ``mitmproxy.proxy.server`` logger as the connect/disconnect chatter above (see
    ``proxy/layers/websocket.py`` → ``proxy/server.py`` ``self.log``), and only Ping/Pong frames are
    logged this way (data messages go through a hook, not a Log command) — so a ``Received WebSocket
    ping``/``pong`` prefix match is exact and drops nothing real. Filtering the source logger drops
    the record before any handler (incl. the supervisor's log tee) ever writes it."""

    _NOISE_PREFIXES = ("Received WebSocket ping", "Received WebSocket pong")

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith(self._NOISE_PREFIXES)


logging.getLogger("mitmproxy.proxy.server").addFilter(_DropConnectChatter())
logging.getLogger("mitmproxy.proxy.server").addFilter(_DropWebsocketPingPong())


_HTTPS_PORT = 443  # the one port a bare host grant covers at CONNECT
_HTTP_PORT = 80  # …and, for a request seen in the clear, this one

# The 403 body the box sees for a host the allowlist refuses. Grants are made on the host
# machine (never from the box), so the body says where the fix is run.
_REFUSED_BODY = (
    b"refused by the foldyard allowlist - grant it on your computer: `fy allow add <host>`\n"
)

# The proxy-URL user an image BUILD reaches us as (foldyard's `plugins/proxy.BUILD_TUNNEL_USER`,
# duplicated because this addon runs standalone; a test pins the two equal). A build has no proxy
# CA, so a connection carrying it is blind-tunnelled rather than decrypted — after the wall.
_BUILD_TUNNEL_USER = "fy-build"


def _basic_credentials(value: str | None) -> tuple[str, str] | None:
    """A Basic ``Proxy-Authorization`` header's (user, password), or None. Never raises."""
    import base64
    import binascii

    scheme, _, token = (value or "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        user, _, password = base64.b64decode(token.strip(), validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return user, password


def _is_build_marker(value: str | None) -> bool:
    """True when a Proxy-Authorization header is the build marker: Basic, user = the marker (any
    password — clients differ in what they send for an empty one). Never raises."""
    import base64
    import binascii

    scheme, _, token = (value or "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user = base64.b64decode(token.strip(), validate=True).decode().partition(":")[0]
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return user == _BUILD_TUNNEL_USER


def _host_matches(host: str | None, patterns: list[str]) -> bool:
    """Exact host match, or ``*.suffix`` wildcard (matches SUBDOMAINS, not the bare domain) —
    Claude Code Web's allow-list semantics, so its published list drops in unchanged. Used to
    decide which hosts the ``full`` path blind-tunnels instead of decrypting."""
    if not host:
        return False
    for p in patterns:
        if p.startswith("*."):
            if host.endswith(p[1:]):  # "*.foo.com" → endswith ".foo.com": a.foo.com ✓, foo.com ✗
                return True
        elif host == p:
            return True
    return False


def _with_query_param(url: str, name: str, value: str) -> str:
    """Return ``url`` with query param ``name`` set to ``value`` (replacing any existing copy).
    Used by the 401 re-issue when injecting a query param rather than a header — the live request
    already carries the param (set in the ``request`` hook), but a rotated token needs the fresh
    value put back into the URL we re-issue against."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != name]
    pairs.append((name, value))
    return urlunsplit(parts._replace(query=urlencode(pairs)))


_DEFAULT_LOG = str(Path.home() / ".foldyard" / "logs" / "egress.jsonl")
# Rotate the log to a DATED backup (egress-proxy.<UTC-stamp>.jsonl) once it grows past this, then
# prune to the newest PROXY_LOG_BACKUPS — so the on-disk footprint is bounded (decrypted flows log
# every request, so the log fills fast) while keeping readable, timestamped history (foldyard's
# config.tail_jsonl/rotated_logs read this exact naming). Both knobs are env-tunable.
_LOG_MAX_BYTES = int(os.environ.get("PROXY_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
_LOG_BACKUPS = int(os.environ.get("PROXY_LOG_BACKUPS", "5"))
# On a 4xx/5xx, capture this many bytes of the (decrypted) response body into the log entry so the
# Network Log shows WHY it failed (e.g. an auth-token parse error), not just the status code.
_ERROR_BODY_MAX = 1024
# The client's User-Agent, kept on request + would-block rows so a learned host says WHICH TOOL
# reached it (npm/…, uv/…, curl/…) without instrumenting the box. Capped: it is untrusted text.
_UA_MAX = 120
# A would-block row is written at most once per host per this many seconds: observing a package
# install would otherwise write one per connection (hundreds to the same registry) and rotate the
# log away. The review needs "this host, this tool, first/last seen" — not every connection.
_WOULD_BLOCK_EVERY = 60.0
# …and the per-host timestamps behind that limit are bounded: one entry per distinct refused host
# would otherwise grow for ever on a proxy seeing generated hostnames.
_WOULD_BLOCK_MAX = 4096


def _redact_param(path: str, name: str) -> str:
    """``path`` with the value of query parameter ``name`` replaced by ``‹redacted›`` — the
    injector's credential, which it wrote into the URL, never reaches the egress log. Everything
    else in the path is kept as sent (ADR-0029 logs full paths)."""
    from urllib.parse import unquote_plus

    base, sep, query = path.partition("?")
    if not sep:
        return path
    parts = []
    for part in query.split("&"):
        key = part.split("=", 1)[0]
        # Compared DECODED (mitmproxy writes `auth[token]` as `auth%5Btoken%5D`); logged as sent.
        parts.append(f"{key}=‹redacted›" if unquote_plus(key) == name else part)
    return base + "?" + "&".join(parts)


def _user_agent(request) -> str:
    """The request's User-Agent, capped, or '' — never raises (logging must not break the proxy)."""
    try:
        return str(request.headers.get("user-agent", ""))[:_UA_MAX]
    except Exception:
        return ""


def _error_snippet(response) -> str:
    """A short, single-line text snippet of an error response body for the Network Log. Returns ''
    when there's no readable body (empty, binary, or a decode/read failure) — never raises, because
    logging must not break the proxy."""
    try:
        raw = response.content  # decoded (de-chunked/decompressed) bytes, or None if not buffered
    except Exception:
        return ""
    if not raw:
        return ""
    text = raw[:_ERROR_BODY_MAX].decode("utf-8", "replace")
    return " ".join(text.split())[:_ERROR_BODY_MAX]  # collapse whitespace → one tidy line


# What EVERY minter gets: enough to run a program and reach the network, and nothing else.
# HOME matters (gh reads ~/.config/gh, the Codex minter its auth.json); the proxy vars matter
# because a minter on a host machine behind a mandatory egress proxy has no other way out.
_MINTER_BASE_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)


def _minter_env(env_keys: tuple[str, ...], values: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a minter subprocess runs with: the base above plus the names its own rule
    declared — NOT this process's whole environment.

    mitmdump is a child of the supervisor, which merges ALL of `host.env` into its environment
    (every axis's secret, for every mechanism). Inheriting that gave each minter — and anything it
    ran — the full credential set for the price of one mint, which is a blast radius no minter
    needs:
    the github minter has no business reading the Anthropic key. Withholding is cheap and each
    plugin already knows exactly which vars its minter reads."""
    env = {k: os.environ[k] for k in _MINTER_BASE_ENV if k in os.environ}
    # A live-configured rule carries its secrets resolved (see `_resolve_secrets`); the legacy env
    # path still reads this process's environment.
    source = os.environ if values is None else values
    env.update({k: source[k] for k in env_keys if k in source})
    return env


def _read_host_env(path: Path | None) -> dict[str, str]:
    """``host.env``'s ``KEY=VALUE`` lines, parsed exactly as the supervisor's ``load_host_env``
    does (blank/comment lines skipped, surrounding quotes stripped). Missing/unreadable → ``{}``."""
    if path is None:
        return {}
    try:
        text = path.read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("'\"")
    return out


def _resolve_secrets(
    keys, host_env: dict[str, str], defaults: dict[str, str] | None = None
) -> dict[str, str]:
    """The values of ``keys`` a rule's minter may read: this process's environment first (the
    operator's own exports — the supervisor strips host.env's keys from it, so what remains is
    ambient, and ambient has always won), else ``host.env``, else the live file's derived
    ``defaults`` (non-secret identity a plugin computes from committed config — the supervisor's
    ``env_defaults``, same precedence as its ``setdefault``). Absent names are simply absent."""
    out: dict[str, str] = {}
    for key in keys:
        if key in os.environ:
            out[key] = os.environ[key]
        elif key in host_env:
            out[key] = host_env[key]
        elif defaults and key in defaults:
            out[key] = str(defaults[key])
    return out


def _file_stamp(path: Path | None) -> tuple[int, int, int] | None:
    """Identity + mtime + size: a writer that renames a new file into place (the supervisor)
    always changes the inode, so this can't miss a same-second rewrite the way mtime alone can."""
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


# 20+ unbroken chars from the base64/token alphabet: longer than any word a human-written
# diagnosis needs, shorter than every credential a minter handles (gh App tokens, JWT
# segments, OAuth access tokens are all well past 20). Paths/URLs can match too — redacting
# a path from a traceback is a price worth paying to never log a token.
_TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9_+/=.-]{20,}")


def _redact_token_runs(text: str) -> str:
    return _TOKEN_RUN_RE.sub("‹redacted›", text)


def _mint_failure_detail(e: BaseException) -> str:
    """
    The minter's OWN reason, not just ``returned non-zero exit status 1``.

    Minters write a diagnosis to stderr and exit 1 by contract (e.g. "github_app_token: GitHub
    returned 404 — …"), but `subprocess.run(check=True)` captures stderr and `CalledProcessError`
    stringifies to the exit status alone. Dropping that text is the difference between a log line
    that names the broken credential and one that sends whoever reads it hunting: the whole
    symptom is `gh` getting 401s inside the box, where the host log is the only witness.

    stdout is deliberately NOT included at any level — that's where the minted token comes back.
    stderr is diagnostic *by contract*, but a crashing minter can still echo a half-minted
    credential (or argv/env carrying one) into a traceback, so token-shaped runs are redacted
    before any of it reaches the log — the prose of a diagnosis survives, opaque blobs don't.
    """
    if isinstance(e, subprocess.CalledProcessError):
        detail = (e.stderr or "").strip()
        if not detail:
            return f"exit {e.returncode}, no stderr"
        # Last lines first: a traceback's useful line is at the end.
        lines = detail.splitlines()
        tail = " / ".join(lines[-3:])
        return f"exit {e.returncode}: {_redact_token_runs(tail)[:500]}"
    if isinstance(e, subprocess.TimeoutExpired):
        return f"no output after {e.timeout}s — token service hung"
    if isinstance(e, json.JSONDecodeError):
        # The token rides in this stdout, so report the shape of the failure, never the bytes.
        return f"token service stdout was not the expected JSON ({e.msg} at position {e.pos})"
    if isinstance(e, KeyError):
        return f"token service JSON is missing the {e} key (expected 'value' and 'ttl')"
    return f"{type(e).__name__}: {e}"


class _Rule:
    """One injection rule — a host whose auth this proxy rewrites, with its OWN minted-token cache.

    The proxy carries a SET of these (the multi-injector "rule set": github + claude + codex + any
    ``[[inject]]`` can all be live at once), each minting + caching independently. Built either from
    one entry of the ``INJECT_RULES`` JSON the foldyard proxy plugin emits, or — back-compat — from
    the legacy single ``INJECT_HOST``/``INJECT_COMMAND``/… env, which the proxy plugin still
    emits whenever there is exactly ONE rule."""

    def __init__(self, spec: dict, secrets: dict[str, str] | None = None) -> None:
        # The resolved values of `env_keys` for a live-configured rule (None → read os.environ).
        self._secrets = secrets
        self.key: str | None = None  # the live file's identity for this rule (see `refresh`)
        self.host = spec.get("host") or None
        self.command = spec.get("command") or None
        self.header = spec.get("header") or "Authorization"
        # Prepended to the minted value (e.g. "Bearer " for an OAuth `authorization` header): we
        # overwrite the header VERBATIM, so the minter/host.env carry the bare token + the scheme is
        # added here. Inject as a URL query param instead when query_param is set (XOR header).
        self.value_prefix = spec.get("value_prefix") or ""
        self.query_param = spec.get("query_param") or None
        # Only inject under this path prefix (e.g. "/mcp"), so one host can serve an app AND an
        # injected endpoint; empty → the whole host. Re-mint + re-issue once on a 401 if retry_401.
        self.path_prefix = spec.get("path_prefix") or None
        self.retry_401 = bool(spec.get("retry_401", True))
        # `verify` for the 401 re-issue: a CA-bundle path pins trust (the e2e points this at its
        # self-signed upstream); empty → system trust. TLS is never disabled.
        ca_bundle = spec.get("ca_bundle") or ""
        self.verify: str | bool = ca_bundle if ca_bundle else True
        # The host env var names THIS minter is allowed to see (declared by the contributing
        # plugin as `InjectRule.env`). Everything else is withheld — see `_minter_env`.
        self.env_keys = tuple(str(k) for k in (spec.get("env") or ()))
        # This rule's own cached value + the monotonic time it stops being usable, behind a
        # lock because they are read/written from more than one thread: the `running` hook
        # warms every rule on its own thread while the request path may already be calling
        # token(). Without it the two mint concurrently (defeating the warm-up) and, worse,
        # a reader can observe a fresh _value against the previous _expires_at — a token
        # handed out as valid long after it isn't, or vice versa.
        self._lock = threading.Lock()
        self._value: str | None = None
        self._expires_at: float = 0.0

    @property
    def active(self) -> bool:
        """A rule injects only with BOTH a host and a minter; one without (the capture-only env)
        contributes nothing — the Injector drops it so ``injecting`` reflects real rules only."""
        return bool(self.host and self.command)

    def matches(self, host: str | None, path: str) -> bool:
        """True when this rule should inject on a request: its host, and — if a path prefix is set —
        a path under it. Lets one host serve both an app and a separately-injected endpoint."""
        if host != self.host:
            return False
        return not self.path_prefix or path.startswith(self.path_prefix)

    def _mint(self) -> str:
        """Run this rule's host minter and cache its {value, ttl}. Raises on failure."""
        assert self.command, "minter command must be set before _mint() (active implies it)"
        out = subprocess.run(
            shlex.split(self.command),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env=_minter_env(self.env_keys, self._secrets),
        ).stdout
        data = json.loads(out)
        self._value = str(data["value"])
        # Refresh a little before the reported ttl; the minter already subtracts its own safety
        # margin, this is belt-and-braces against clock skew on long-lived flows.
        self._expires_at = time.monotonic() + max(0, int(data["ttl"]) - 30)
        ctx.log.info(f"egress_proxy: minted token for {self.host} (ttl≈{data['ttl']}s)")
        return self._value

    def token(self, *, force: bool = False, warm: bool = False) -> str | None:
        # Held across the mint, so a warm-up and a concurrent request collapse to ONE
        # subprocess: the second caller waits and then sees the fresh cache. The minter has
        # its own 30s timeout, which bounds how long that wait can be.
        with self._lock:
            if force or self._value is None or time.monotonic() >= self._expires_at:
                try:
                    return self._mint()
                except Exception as e:
                    # WARN on the startup warm-up (`warm`), ERROR on the request path. Not
                    # cosmetic: mitmproxy's ErrorCheck addon EXITS the process when anything logs
                    # at ERROR during startup, so one rule whose credential is missing from
                    # host.env would kill the whole proxy — which the supervisor respawns, forever
                    # — cutting egress for every host whose credential was fine. Same invariant
                    # `requires` protects (a proxy that won't launch connection-refuses every box
                    # request): degrade ONE host, never all egress. The request path is past the
                    # startup window, so it keeps the loud level.
                    report = ctx.log.warn if warm else ctx.log.error
                    # Lead with the HOST: that's the axis a reader is trying to identify, and
                    # the command alone makes them map a module path back to a mode by hand.
                    report(
                        f"egress_proxy: mint failed for {self.host} — "
                        f"{_mint_failure_detail(e)} [{self.command}]"
                    )
                    return None
            return self._value

    def invalidate(self) -> None:
        """Forget the cached value, so the next request mints afresh."""
        with self._lock:
            self._value = None
            self._expires_at = 0.0

    def apply(self, request, value: str) -> None:
        """Inject the minted value — a URL query param or, by default, an OVERWRITTEN header (the
        box only ever sends a dummy). ``value_prefix`` (e.g. "Bearer ") is added here so the
        minter/host.env keep the bare token. A ``request.query`` assignment reflects in the URL."""
        value = self.value_prefix + value
        if self.query_param:
            request.query[self.query_param] = value
        else:
            request.headers[self.header] = value


class Injector:
    def __init__(self) -> None:
        # The injection rule SET. INJECT_RULES (a JSON list) wins — the multi-injector rules the
        # foldyard proxy plugin emits; else the legacy single INJECT_HOST/COMMAND/… env. That is
        # NOT a compatibility shim: the plugin emits those keys for every single-rule config.
        #
        # With LIVE_FILE set (foldyard's supervisor always sets it) the rules, the wall switch and
        # the passthrough list come from that file instead, re-read whenever it changes — so a
        # posture change reaches a RUNNING proxy and nothing in flight is cut (see `refresh`).
        live = os.environ.get("LIVE_FILE", "")
        self.live_path = Path(live) if live else None
        host_env = os.environ.get("HOST_ENV_FILE", "")
        self.host_env_path = Path(host_env) if host_env else None
        self._live_stamp: tuple | None = None
        self._observing_since: str | None = None
        self._running = False
        self._warm_threads: list[threading.Thread] = []
        self._set_rules([] if self.live_path else self._load_rules())
        # Phase A′: what to do with HTTPS we aren't injecting — "full" (decrypt+log) or
        # "passthrough" (blind-tunnel + SNI-log). Default "full" keeps the pre-A′ behaviour for
        # any caller that doesn't set CAPTURE_MODE.
        self.capture_mode = os.environ.get("CAPTURE_MODE", "full")
        # Trusted hosts to blind-tunnel even under "full" (don't decrypt — fast + low-noise).
        self.passthrough_hosts = [
            h for h in os.environ.get("PASSTHROUGH_HOSTS", "").split(",") if h
        ]
        log_file = os.environ.get("PROXY_LOG_FILE", _DEFAULT_LOG)
        self.log_path = Path(log_file) if log_file else None
        # Egress wall: when DEFAULT_DENY is on, refuse any host not in the allowlist. The static
        # switch is read once (toggling it restarts the daemon); the `allow` patterns live in
        # ALLOW_FILE, re-read per request (mtime-cached) so a host-side grant lands with no restart.
        self.default_deny = os.environ.get("DEFAULT_DENY", "") == "1"
        allow_file = os.environ.get("ALLOW_FILE", "")
        self.allow_path = Path(allow_file) if allow_file else None
        self._allow_mtime: float = -1.0
        self._allow_patterns: list[str] = []
        # host key → monotonic time of its last would-block row (see _WOULD_BLOCK_EVERY).
        self._would_block_seen: dict[str, float] = {}
        # The live build secrets' hashes → expiry (BUILD_TOKENS_FILE, written by the host's build
        # gate): a connection presenting one is a build the host started, and may use the
        # build-scoped grants (ALLOW_FILE's `build_allow`). The bare marker never can.
        tokens = os.environ.get("BUILD_TOKENS_FILE", "")
        self.tokens_path = Path(tokens) if tokens else None
        self._tokens_stamp: tuple | None = None
        self._tokens: dict[str, float] = {}
        self._build_patterns: list[str] = []
        # Client connections whose CONNECT carried the build marker and passed the wall: their TLS
        # is tunnelled, not decrypted (see _BUILD_TUNNEL_USER). Dropped on disconnect.
        self._build_clients: set[str] = set()
        # Every connection a CONNECT let through, by client id: what `_sweep` re-judges when the
        # policy narrows (see `refresh`). Dropped on disconnect.
        self._conns: dict[str, dict] = {}
        self._policy_dirty = False
        if self.live_path is not None:
            self.refresh()  # the first read: rules, wall and passthrough all come from the file

    def _set_rules(self, rules: list[_Rule]) -> None:
        self.rules = rules
        self.inject_hosts = {r.host for r in rules}
        self.injecting = bool(rules)

    def refresh(self) -> bool:
        """Pick up any policy change — the live settings, the allowlist — and close the open
        connections the new policy would not have allowed; True when something changed. Called at
        the top of every hook and once a second by ``_watch`` (an idle tunnel sends no requests,
        and a narrowing must still reach it).

        The restart this replaced cut every connection, which also cut the ones a narrowing no
        longer allows. Keeping that property without the collateral means re-judging each one:
        a connection whose CONNECT the wall would now refuse is closed, and so is a blind tunnel
        the policy would now decrypt (its host left the passthrough list, or became an injector's).
        Widening closes nothing."""
        changed = self._refresh_live()
        self._refresh_allow()
        if self._policy_dirty:
            self._policy_dirty = False
            changed = True
            self._sweep()
        return changed

    def _refresh_live(self) -> bool:
        """Re-read LIVE_FILE (and the host.env its rules' secrets come from) if either changed;
        True when it did.

        A rule whose spec AND resolved secrets are unchanged keeps its object — so its cached
        token survives a posture change that only touched other rules. A new or changed rule
        starts cold and, once the proxy is running, is warmed off the request path (at WARN, as
        at startup: one host's missing credential is never all egress). An unreadable or
        malformed file fails CLOSED — no rules, the wall enforcing, nothing tunnelled blind —
        like ALLOW_FILE: a parse error never widens egress."""
        if self.live_path is None:
            return False
        stamp = (_file_stamp(self.live_path), _file_stamp(self.host_env_path))
        if stamp == self._live_stamp:
            return False
        first = self._live_stamp is None
        self._live_stamp = stamp
        try:
            data = json.loads(self.live_path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as e:
            ctx.log.warn(f"egress_proxy: live settings unreadable ({e}) — failing closed")
            data = {}
        host_env = _read_host_env(self.host_env_path)
        defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
        previous = {getattr(r, "key", None): r for r in self.rules}
        rules: list[_Rule] = []
        fresh: list[_Rule] = []
        specs = data.get("rules")
        for spec in specs if isinstance(specs, list) else []:
            if not isinstance(spec, dict):
                continue
            secrets = _resolve_secrets(spec.get("env") or (), host_env, defaults)
            key = json.dumps([spec, secrets], sort_keys=True)
            rule = previous.get(key)
            if rule is None:
                rule = _Rule(spec, secrets)
                rule.key = key
                fresh.append(rule)
            if rule.active:
                rules.append(rule)
        self._set_rules(rules)
        # A new learn window: rows rate-limited before it must not hide a host inside it.
        observing = data.get("observing_since")
        if not first and observing != self._observing_since:
            self._would_block_seen = {}
        self._observing_since = observing
        self.default_deny = data.get("default_deny", True) is not False
        passthrough = data.get("passthrough")
        self.passthrough_hosts = (
            [str(h) for h in passthrough if h] if isinstance(passthrough, list) else []
        )
        if not first:
            enforce = "on" if self.default_deny else "off"
            hosts = ", ".join(sorted(r.host for r in rules if r.host)) or "none"
            ctx.log.info(
                f"egress_proxy: settings reloaded — injecting {hosts}; allowlist enforce {enforce}"
            )
        if self._running:
            self._warm([r for r in fresh if r.active])
        self._policy_dirty = True
        return True

    def _warm(self, rules: list[_Rule]) -> None:
        threads = [
            threading.Thread(
                target=lambda r=r: r.token(warm=True), daemon=True, name=f"egress-warm-{r.host}"
            )
            for r in rules
        ]
        self._warm_threads += threads
        for t in threads:
            t.start()

    async def _watch(self) -> None:
        """Poll the live settings once a second, so a change lands even on a proxy with no new
        requests — an idle tunnel sends none, and a narrowing must still reach it."""
        while True:
            await asyncio.sleep(1)
            try:
                self.refresh()
            except Exception as e:  # the watcher must outlive any one bad read
                ctx.log.warn(f"egress_proxy: live settings refresh failed: {e}")

    def done(self) -> None:
        watcher = getattr(self, "_watcher", None)
        if watcher is not None:
            watcher.cancel()

    def _load_rules(self) -> list[_Rule]:
        """Build the injection rule set. ``INJECT_RULES`` (a JSON list of rule objects) is the
        multi-injector contract; absent/empty/malformed falls back to ONE rule from the legacy
        single-injector env (defaults preserve the historical github behaviour for the bash recipe).
        Capture-only (empty host/command) yields no active rule, so ``injecting`` is then False."""
        raw = os.environ.get("INJECT_RULES", "")
        if raw:
            try:
                specs = json.loads(raw)
            except ValueError:
                ctx.log.error(
                    f"egress_proxy: INJECT_RULES is not valid JSON — injecting nothing: {raw[:200]}"
                )
                specs = []
            rules = [_Rule(s) for s in specs if isinstance(s, dict)]
            return [r for r in rules if r.active]
        legacy = _Rule(
            {
                "host": os.environ.get("INJECT_HOST", "api.github.com"),
                "command": os.environ.get("INJECT_COMMAND", ""),
                "header": os.environ.get("INJECT_HEADER", "Authorization") or "Authorization",
                "value_prefix": os.environ.get("INJECT_VALUE_PREFIX", ""),
                "query_param": os.environ.get("INJECT_QUERY_PARAM", ""),
                "path_prefix": os.environ.get("INJECT_PATH_PREFIX", ""),
                "retry_401": os.environ.get("INJECT_RETRY_401", "1") == "1",
                "ca_bundle": os.environ.get("INJECT_RETRY_CA_BUNDLE", ""),
                "env": [k for k in os.environ.get("INJECT_ENV_KEYS", "").split(",") if k],
            }
        )
        return [legacy] if legacy.active else []

    def _rule_for(self, flow: http.HTTPFlow) -> _Rule | None:
        """The rule that should inject on this request, or None (capture-only / non-target). First
        match wins — distinct injectors use distinct hosts, so at most one matches in practice.
        HTTPS only, by scheme: a cleartext request to a target host — even on :443 — gets no
        credential, on the way out (`request`) or on a 401 re-issue (`response`)."""
        if flow.request.scheme != "https":
            return None
        for rule in self.rules:
            if rule.matches(flow.request.pretty_host, flow.request.path):
                return rule
        return None

    # ── egress allowlist (the default-deny wall) ───────────────────────────────
    def _refresh_allow(self) -> None:
        """Re-read ALLOW_FILE when its mtime changes, so a host-side `allow` grant takes effect
        with NO daemon restart. Fail toward MORE blocking: a missing/unreadable/malformed file
        leaves the allow set EMPTY — a parse error never widens egress."""
        before = (self._allow_patterns, self._build_patterns)
        if self.allow_path is None:
            self._allow_patterns, self._build_patterns = [], []
        else:
            try:
                mtime = self.allow_path.stat().st_mtime
            except OSError:
                self._allow_patterns, self._build_patterns = [], []
                self._allow_mtime = -1.0
                mtime = None
            if mtime is not None and mtime != self._allow_mtime:
                self._allow_mtime = mtime
                try:
                    data = json.loads(self.allow_path.read_text())
                    data = data if isinstance(data, dict) else {}
                except (OSError, ValueError):
                    data = {}
                allow, build = data.get("allow", []), data.get("build_allow", [])
                self._allow_patterns = [str(p) for p in allow] if isinstance(allow, list) else []
                self._build_patterns = [str(p) for p in build] if isinstance(build, list) else []
        if (self._allow_patterns, self._build_patterns) != before:
            self._policy_dirty = True  # a grant came or went: `refresh` re-judges open tunnels

    def _allowed(self, host: str | None) -> bool:
        """True if `host` may egress under default-deny: any injector host is always exempt (we must
        reach it to mint), else it must match an ALLOW_FILE pattern (exact or ``*.suffix``)."""
        if host in self.inject_hosts:
            return True
        self._refresh_allow()
        return _host_matches(host, self._allow_patterns)

    # ── network log (read by the TUI's Network tab) ────────────────────────────
    def _rotate(self) -> None:
        """Rotate the live log to a dated backup (<stem>.<UTC-stamp><suffix>) and prune to the
        newest _LOG_BACKUPS — readable, timestamped history that stays bounded, vs a single
        overwritten .1. The naming matches foldyard's config.rotated_logs glob."""
        log = self.log_path
        assert log is not None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        log.replace(log.with_name(f"{log.stem}.{stamp}{log.suffix}"))
        backups = sorted(log.parent.glob(f"{log.stem}.*{log.suffix}"))
        for old in backups[:-_LOG_BACKUPS] if _LOG_BACKUPS > 0 else backups:
            try:
                old.unlink()
            except OSError:
                pass

    def _write_entry(self, entry: dict) -> None:
        """Append one JSONL row to the egress log, rotating first if it's grown past the cap.
        Logging must NEVER break the proxy — swallow write errors with a warning."""
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > _LOG_MAX_BYTES:
                self._rotate()
            with self.log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            ctx.log.warn(f"egress_proxy: network log write failed: {e}")

    def _log_flow(self, flow: http.HTTPFlow) -> None:
        if flow.response is None:
            return
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "method": flow.request.method,
            "host": flow.request.pretty_host,
            "path": self._logged_path(flow)[:200],
            "status": flow.response.status_code,
            "injected": flow.request.pretty_host in self.inject_hosts,
            "replayed": bool(flow.metadata.get("egress_proxy_retried")),
        }  # fmt: skip
        ua = _user_agent(flow.request)
        if ua:
            entry["ua"] = ua
        # Error responses carry the reason in their body — capture a short snippet so "injected=True
        # but 401, why?" is answerable at a glance instead of by re-running the failing client.
        if flow.response.status_code >= 400:
            snippet = _error_snippet(flow.response)
            if snippet:
                entry["error_body"] = snippet
        self._write_entry(entry)

    def _logged_path(self, flow: http.HTTPFlow) -> str:
        """The request path as logged: a query-param injector's credential redacted (its rule
        wrote the minted value into the URL — the header case never reaches the path)."""
        path = flow.request.path
        rule = self._rule_for(flow)
        for name in {flow.metadata.get("egress_proxy_redact"), rule.query_param if rule else None}:
            if name:
                path = _redact_param(path, name)
        return path

    def _log_passthrough(self, host: str | None, *, build: bool = False) -> None:
        """An SNI-level row for a blind-tunnelled (not decrypted) HTTPS connection: host + time
        only — TLS hides method/path/status. The Network Log panel renders `passthrough` rows as
        a `tls tunnel` marker; ``build`` says it was tunnelled because an image build asked, not
        because the host is on the passthrough list. No host (no SNI) → nothing to log."""
        if not host:
            return
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "method": "",
            "host": host,
            "path": "",
            "status": 0,
            "injected": False,
            "replayed": False,
            "passthrough": True,
        }  # fmt: skip
        if build:
            entry["build"] = True
        self._write_entry(entry)

    def _log_blocked(self, host: str | None, request=None) -> None:
        """A row for a host REFUSED by the default-deny wall — no upstream is ever contacted, so
        there's no method/path/real status (we synthesise a 403). The Network Log panel keys off
        ``blocked`` to paint it red and offer an 'allow' action. With the refused ``request``,
        the row also carries its User-Agent and, when it carried the build marker, ``build`` —
        what `fy box build`/`fy up` read back to offer the host. No host → nothing to log."""
        if not host:
            return
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "method": "",
            "host": host,
            "path": "",
            "status": 403,
            "injected": False,
            "replayed": False,
            "blocked": True,
        }  # fmt: skip
        if request is not None:
            ua = _user_agent(request)
            if ua:
                entry["ua"] = ua
            if _is_build_marker(request.headers.get("Proxy-Authorization")):
                entry["build"] = True
        self._write_entry(entry)

    def _log_would_block(self, key: str | None, request) -> None:
        """While the wall only OBSERVES (``fy allow enforce off``, or a learn window): a row for a
        host enforcement WOULD have refused — the same policy check as the wall, so the set
        ``fy allow learn`` offers is exactly what enforcing would need, ports included. Nothing
        is refused. Rate-limited per host (:data:`_WOULD_BLOCK_EVERY`). No host → nothing."""
        if not key:
            return
        now = time.monotonic()
        last = self._would_block_seen.get(key)
        if last is not None and now - last < _WOULD_BLOCK_EVERY:
            return
        if len(self._would_block_seen) >= _WOULD_BLOCK_MAX:
            self._would_block_seen = {
                k: t for k, t in self._would_block_seen.items() if now - t < _WOULD_BLOCK_EVERY
            }
            if len(self._would_block_seen) >= _WOULD_BLOCK_MAX:
                self._would_block_seen = {}  # all recent: forgetting costs only extra rows
        self._would_block_seen[key] = now
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "method": "",
            "host": key,
            "path": "",
            "status": 0,
            "injected": False,
            "replayed": False,
            "would_block": True,
        }  # fmt: skip
        ua = _user_agent(request)
        if ua:
            entry["ua"] = ua
        self._write_entry(entry)

    # ── mitmproxy hooks ──────────────────────────────────────────────────────────
    def running(self) -> None:
        """Pre-mint every rule's token at proxy start, OFF the request path. ``request`` calls
        ``rule.token()`` synchronously on the event loop, so a slow first mint (e.g. ``uv run
        --script`` resolving a minter's PEP 723 env on a cold cache) would stall ALL proxied
        egress for its duration. Fire-and-forget daemon threads: a warm failure just logs (at WARN
        — ``warm=True``; an ERROR here makes mitmproxy exit, see ``token``) and the request path
        re-mints as before. Handles kept on ``self`` so tests can join."""
        self._running = True
        self._warm(list(self.rules))
        if self.live_path is not None:
            try:
                self._watcher = asyncio.get_running_loop().create_task(self._watch())
            except RuntimeError:
                pass  # no event loop (the unit tests): the hooks still refresh per request

    def http_connect(self, flow: http.HTTPFlow) -> None:
        """The egress wall for proxied HTTPS. The box reaches every HTTPS host through an explicit
        CONNECT to this proxy, and this hook fires on that CONNECT BEFORE any tunnel/TLS — so
        answering it with a 403 refuses the host outright (no upstream dialled, no TLS handshake,
        ``tls_clienthello`` never runs for it). Only the default-deny wall lives here; the
        decrypt-vs-passthrough choice is still ``tls_clienthello``'s job for hosts we DO allow.

        The PORT is fenced too: a host grant means ``host:443``. CONNECT is a raw TCP tunnel — a
        client that speaks something other than TLS through it is relayed as-is — so a bare host
        grant used to let ``github.com:22`` out, and with an SSH agent forwarded into the box by an
        editor attach that is a push path (seen live, 2026-09-17). Another port needs its own
        grant, ``host:port`` (``fy allow add github.com:22``), so the blocked row carries the port
        and the TUI's allow action offers exactly that."""
        self.refresh()
        host = flow.request.pretty_host
        port = flow.request.port
        if self._allowed_connect(host, port) or (
            self._trusted_build(flow.request) and self._build_granted(host, port, _HTTPS_PORT)
        ):
            self._note_build(flow)
            return
        key = host if port == _HTTPS_PORT else f"{host}:{port}"
        if not self.default_deny:
            # Observing: let it through, but record that enforcing would refuse it — the CONNECT
            # carries the client's User-Agent even for a host that is then tunnelled blind.
            self._log_would_block(key, flow.request)
            self._note_build(flow)
            return
        flow.response = http.Response.make(403, _REFUSED_BODY)
        self._log_blocked(key, flow.request)

    def _trusted_build(self, request) -> bool:
        """Did this request present a LIVE build secret (not just the public marker)?"""
        creds = _basic_credentials(request.headers.get("Proxy-Authorization"))
        if creds is None or creds[0] != _BUILD_TUNNEL_USER or self.tokens_path is None:
            return False
        stamp = _file_stamp(self.tokens_path)
        if stamp != self._tokens_stamp:
            self._tokens_stamp = stamp
            tokens: dict[str, float] = {}
            try:
                doc = json.loads(self.tokens_path.read_text())
                for digest, expires in (doc.get("tokens") or {}).items():
                    tokens[str(digest)] = datetime.fromisoformat(expires).timestamp()
            except (OSError, ValueError, TypeError, AttributeError):
                tokens = {}  # unreadable ⇒ no build is trusted (fail toward MORE blocking)
            self._tokens = tokens
        import hashlib

        expires = self._tokens.get(hashlib.sha256(creds[1].encode()).hexdigest())
        return expires is not None and expires > time.time()

    def _build_granted(self, host: str | None, port: int, default_port: int) -> bool:
        """The build-scoped grants, with the same port rule as the runtime ones."""
        if not host:
            return False
        self._refresh_allow()
        key = host if port == default_port else f"{host}:{port}"
        return _host_matches(key, self._build_patterns)

    def _note_build(self, flow: http.HTTPFlow) -> None:
        """Remember a CONNECT that got through (granted, or let through while observing): for
        ``_sweep``, and — when it carried the build marker — so ``tls_clienthello`` tunnels it."""
        client = getattr(flow, "client_conn", None)
        if client is None:
            return
        build = _is_build_marker(flow.request.headers.get("Proxy-Authorization"))
        if build:
            self._build_clients.add(client.id)
        self._conns[client.id] = {
            "host": flow.request.pretty_host,
            "port": flow.request.port,
            "blind": None,  # the tunnelled SNI target once tls_clienthello tunnels it
            "build": build,
            "trusted": self._trusted_build(flow.request),  # may use build-scoped grants
        }

    def client_disconnected(self, client) -> None:
        self._build_clients.discard(client.id)
        self._conns.pop(client.id, None)

    def _tunnel(self, target: str | None, build: bool) -> bool:
        """Would a TLS connection to ``target`` be blind-tunnelled now? (``tls_clienthello``.)"""
        if target in self.inject_hosts:
            return False
        return build or self.capture_mode != "full" or _host_matches(target, self.passthrough_hosts)

    def _sweep(self) -> None:
        """Close each open connection the current policy would not have allowed (see `refresh`)."""
        for client_id, conn in list(self._conns.items()):
            host, port, blind = conn["host"], conn["port"], conn["blind"]
            refused = self.default_deny and not (
                self._allowed_connect(host, port)
                or (conn["trusted"] and self._build_granted(host, port, _HTTPS_PORT))
            )
            decrypt_now = blind is not None and not self._tunnel(blind, conn["build"])
            if not (refused or decrypt_now):
                continue
            key = host if port == _HTTPS_PORT else f"{host}:{port}"
            why = "the allowlist refuses it now" if refused else "it is decrypted now"
            self._close(client_id, key, why)

    def _close(self, client_id: str, key: str, why: str) -> None:
        """Close one client connection (and so its upstream). mitmproxy has no public API for a
        connection without a flow — a blind tunnel is exactly that — so this reaches its
        ``proxyserver`` addon's connection table, pinned by the real-mitmdump e2e."""
        self._conns.pop(client_id, None)
        self._build_clients.discard(client_id)
        try:
            handler = ctx.master.addons.get("proxyserver").connections[client_id]
            handler.close_connection(handler.client)
        except Exception as e:
            ctx.log.warn(f"egress_proxy: couldn't close the connection to {key} ({why}): {e!r}")
            return
        ctx.log.info(f"egress_proxy: closed the connection to {key} — {why}")

    def _allowed_connect(self, host: str | None, port: int) -> bool:
        """The CONNECT policy: the host granted (:meth:`_allowed`) on :443, else ``host:port``
        granted explicitly."""
        return self._allowed(host) if port == _HTTPS_PORT else self._granted_port(host, port)

    def _allowed_plain(self, host: str | None, port: int, scheme: str) -> bool:
        """The policy for a request the proxy sees in the clear — cleartext HTTP, or HTTPS it
        decrypted — by SCHEME: a bare grant covers HTTPS on :443 (as CONNECT does, injector
        exempt) and cleartext on :80 (apt, redirects; no exemption — a minted credential never
        leaves in the clear); anything else, ``http://host:443/`` included, needs ``host:port``."""
        if scheme == "https" and port == _HTTPS_PORT:
            return self._allowed(host)
        if scheme != "https" and port == _HTTP_PORT:
            return self._granted(host)
        return self._granted_port(host, port)

    def _granted(self, host: str | None) -> bool:
        """The host matches a grant (no injector exemption)."""
        self._refresh_allow()
        return _host_matches(host, self._allow_patterns)

    def _granted_port(self, host: str | None, port: int) -> bool:
        if not host:
            return False
        self._refresh_allow()
        return _host_matches(f"{host}:{port}", self._allow_patterns)

    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        """Decide, before the TLS handshake, whether to MITM-decrypt this connection or blind-
        tunnel it (passthrough). Fires only for TLS (plain HTTP never reaches here, so it's always
        decrypted+logged).

          - The injector host is ALWAYS decrypted — we must read + rewrite its Authorization
            header — regardless of CAPTURE_MODE.
          - CAPTURE_MODE=passthrough → ``ignore_connection`` (mitmproxy relays the raw bytes; the
            box completes TLS end-to-end against the REAL upstream cert) and log an SNI row.
          - CAPTURE_MODE=full → fall through → mitmproxy terminates TLS and the request/response
            hooks log the decrypted request.
        """
        self.refresh()
        # The target host: the SNI if the client sent one, else the CONNECT target address (a
        # client reaching a bare IP sends no SNI). We must identify the injector host either way,
        # so it's NEVER tunnelled — even when addressed by IP — or we couldn't rewrite its header.
        target = data.client_hello.sni
        if not target:
            try:
                addr = data.context.server.address
                target = addr[0] if addr else None
            except AttributeError:
                target = None
        if target in self.inject_hosts:
            return  # an injector host: always decrypt (to rewrite its header), whatever the mode
        client = getattr(getattr(data, "context", None), "client", None)
        conn = self._conns.get(client.id) if client is not None else None
        if client is not None and client.id in self._build_clients:
            data.ignore_connection = True  # a trusted build: it has no CA to verify ours with
            self._log_passthrough(target, build=True)
            if conn is not None:
                conn["blind"] = target
            return
        # Blind-tunnel (no decrypt, real certs end-to-end, SNI-only log) when either we're in
        # passthrough mode (tunnel everything) OR we're in full mode but the host is
        # trusted. Otherwise (full mode, untrusted host) fall through → mitmproxy decrypts + the
        # request/response hooks log the full request — the surprising egress worth scrutinising.
        if self.capture_mode != "full" or _host_matches(target, self.passthrough_hosts):
            data.ignore_connection = True
            self._log_passthrough(target)
            if conn is not None:
                conn["blind"] = target

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        """Where a request is judged and its credential written: the headers are in, the body
        isn't. With ``stream_large_bodies`` set, a body past the threshold goes upstream as it
        arrives and ``request`` fires only once it's gone — too late for a header. A long Claude
        conversation (>1 MiB) reached Anthropic with the box's dummy token that way (401 "OAuth
        access token is invalid", 2026-09-23). Refusing here also means a refused request never
        streams its body anywhere."""
        self._on_request(flow)

    def request(self, flow: http.HTTPFlow) -> None:
        """Fires after ``requestheaders`` (after a buffered body, or after a streamed one has gone
        upstream). The work happened there; this only covers a caller that skips that hook."""
        self._on_request(flow)

    def _on_request(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("egress_proxy_judged"):
            return
        flow.metadata["egress_proxy_judged"] = True
        self.refresh()
        # The egress wall for plain HTTP (cleartext never CONNECTs, so http_connect can't catch it):
        # refuse a disallowed host — or port: `http://host:8080/` is as much a tunnel past a host
        # grant as CONNECT :22 — here, before it leaves the box. HTTPS is walled at http_connect.
        host, port = flow.request.pretty_host, flow.request.port
        default = _HTTPS_PORT if flow.request.scheme == "https" else _HTTP_PORT
        allowed = self._allowed_plain(host, port, flow.request.scheme) or (
            self._trusted_build(flow.request) and self._build_granted(host, port, default)
        )
        if not allowed:
            key = host if port == default else f"{host}:{port}"
            if self.default_deny:
                flow.response = http.Response.make(403, _REFUSED_BODY)
                flow.metadata["egress_proxy_blocked"] = True  # so `response` doesn't re-log a 403
                self._log_blocked(key, flow.request)
                return
            if flow.request.scheme != "https":
                # Observing, cleartext: record it (HTTPS was already recorded at its CONNECT).
                self._log_would_block(key, flow.request)
        if _is_build_marker(flow.request.headers.get("Proxy-Authorization")):
            del flow.request.headers["Proxy-Authorization"]  # ours, not the upstream's
        rule = self._rule_for(flow)
        if rule is None:
            return  # capture-only, or not a target host/path → log on the way back, rewrite nothing
        value = rule.token()
        if value is not None:
            rule.apply(flow.request, value)
            if rule.query_param:  # redact what WAS written, whatever the rules are at log time
                flow.metadata["egress_proxy_redact"] = rule.query_param

    async def response(self, flow: http.HTTPFlow) -> None:
        # Re-issue once on an upstream 401 BEFORE logging, so the log + the client both see the
        # final (retried) response. The response hook fires before the client is written to, so
        # overwriting flow.response here hands the retry straight back to the waiting caller. The
        # MATCHING rule decides (its own retry_401 + minter), so distinct injectors don't interfere.
        rule = self._rule_for(flow)
        if (
            rule is not None
            and rule.retry_401
            and flow.response is not None
            and flow.response.status_code == 401
            and not flow.metadata.get("egress_proxy_retried")  # at most once — loop guard
        ):
            await self._reissue_401(flow, rule)
        if flow.metadata.get("egress_proxy_blocked"):
            return  # our own 403, already logged as blocked — nothing went upstream
        self._log_flow(flow)

    async def _reissue_401(self, flow: http.HTTPFlow, rule: _Rule) -> None:
        """Re-mint THIS rule's token, re-issue the request ourselves with it, and replace
        flow.response with the result. We can't `replay.client` a *live* flow on modern mitmproxy
        (it rejects it with "Can't replay live flow"), and a replayed copy is detached from the
        original client — so the retry has to be a direct request whose response we hand back."""
        if flow.request.raw_content is None:
            # The request body was STREAMED upstream (past stream_large_bodies), so there is no
            # copy left to re-send. Hand the 401 back as-is; the client's own retry re-mints.
            # …but the token it was refused with must not serve the next request for the rest of
            # its TTL: drop it, so the next one re-mints.
            rule.invalidate()
            ctx.log.warn(f"egress_proxy: 401 from {rule.host} on a streamed upload — not re-issued")
            return
        value = rule.token(force=True)
        if value is None:
            return
        flow.metadata["egress_proxy_retried"] = True
        ctx.log.info(f"egress_proxy: 401 from {rule.host} — re-minted + re-issuing the request")
        # `requests` is already a host dep (the minter uses it); import lazily so the always-on
        # unit test (stubbed transport, no network) can substitute it without installing it.
        import requests

        headers = dict(flow.request.headers)
        url = flow.request.url
        value = (
            rule.value_prefix + value
        )  # same scheme prefix the live request got (e.g. "Bearer ")
        if rule.query_param:
            url = _with_query_param(url, rule.query_param, value)  # fresh token into the URL
        else:
            headers[rule.header] = value
        # Dial the upstream DIRECTLY, exactly like the mitmproxy flow being replayed —
        # trust_env=False keeps requests from honouring an ambient HTTPS_PROXY, which would
        # re-route the re-issue through ANOTHER proxy the upstream may not be reachable from
        # (a dev box's Phase A′ env routes ALL egress at the host proxy: the re-issue then dials
        # a box-network upstream via the host and read-times-out — the in-box e2e failure mode).
        session = requests.Session()
        session.trust_env = False
        try:
            with session:
                r = await asyncio.to_thread(
                    session.request,
                    flow.request.method,
                    url,
                    headers=headers,
                    data=flow.request.raw_content,
                    verify=rule.verify,
                    allow_redirects=False,
                    timeout=30,
                )
        except Exception as e:
            ctx.log.error(f"egress_proxy: 401 re-issue failed: {e}")
            return
        # Drop content-encoding/length: requests already decoded the body, and Response.make
        # recomputes Content-Length — passing the upstream's stale values corrupts the client read.
        skip = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        out_headers = {k: v for k, v in r.headers.items() if k.lower() not in skip}
        flow.response = http.Response.make(r.status_code, r.content, out_headers)


addons = [Injector()]
