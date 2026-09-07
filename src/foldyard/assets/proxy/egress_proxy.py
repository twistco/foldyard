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

The same addon doubles as a pure CAPTURE proxy (foldyard's `capture` axis): with INJECT_HOST
(or INJECT_COMMAND) empty it injects NOTHING — it just logs every proxied request to
PROXY_LOG_FILE, so all dev-box egress is observable through the one proxy without any
credential in play. Injection and capture compose: a github mode logs everything AND rewrites
api.github.com; capture-only logs everything and rewrites nothing.

Phase A′ — the box ALWAYS routes through this (always-on) proxy, so CAPTURE_MODE decides what it
does with HTTPS it isn't injecting:
  - CAPTURE_MODE=full        → MITM-decrypt + log every request (the `capture=on` axis).
  - CAPTURE_MODE=passthrough → blind-tunnel HTTPS without terminating TLS (the box does end-to-end
                               TLS against the REAL cert) and log only an SNI-level row — host +
                               time, no method/path/status (the `capture=off` axis).
The injector host (api.github.com) is ALWAYS decrypted, whatever CAPTURE_MODE is, because we must
read + rewrite its Authorization header. Plain HTTP is always logged in full (it's cleartext, so
there's nothing to passthrough). This lets `capture` toggle on a RUNNING box — flipping it just
restarts the daemon with a different CAPTURE_MODE; the box's routing + trust never change.

Config via env (read once at startup):
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


def _host_matches(host: str | None, patterns: list[str]) -> bool:
    """Exact host match, or ``*.suffix`` wildcard (matches SUBDOMAINS, not the bare domain) —
    Claude Code Web's allow-list semantics, so its published list drops in unchanged. Used to
    decide which hosts the capture=on (``full``) path blind-tunnels instead of decrypting."""
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
# prune to the newest PROXY_LOG_BACKUPS — so the on-disk footprint is bounded (capture=on logs
# every request, so the log fills fast) while keeping readable, timestamped history (foldyard's
# config.tail_jsonl/rotated_logs read this exact naming). Both knobs are env-tunable.
_LOG_MAX_BYTES = int(os.environ.get("PROXY_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
_LOG_BACKUPS = int(os.environ.get("PROXY_LOG_BACKUPS", "5"))
# On a 4xx/5xx, capture this many bytes of the (decrypted) response body into the log entry so the
# Network Log shows WHY it failed (e.g. an auth-token parse error), not just the status code.
_ERROR_BODY_MAX = 1024


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
# because a minter on a Mac behind a mandatory egress proxy has no other way out.
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


def _minter_env(env_keys: tuple[str, ...]) -> dict[str, str]:
    """The environment a minter subprocess runs with: the base above plus the names its own rule
    declared — NOT this process's whole environment.

    mitmdump is a child of the supervisor, which merges ALL of `host.env` into its environment
    (every axis's secret, for every mechanism). Inheriting that gave each minter — and anything it
    ran — the full credential set for the price of one mint, which is a blast radius no minter
    needs:
    the github minter has no business reading the Anthropic key. Withholding is cheap and each
    plugin already knows exactly which vars its minter reads."""
    env = {k: os.environ[k] for k in _MINTER_BASE_ENV if k in os.environ}
    env.update({k: os.environ[k] for k in env_keys if k in os.environ})
    return env


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
        return f"no output after {e.timeout}s — minter hung"
    if isinstance(e, json.JSONDecodeError):
        # The token rides in this stdout, so report the shape of the failure, never the bytes.
        return f"minter stdout was not the expected JSON ({e.msg} at position {e.pos})"
    if isinstance(e, KeyError):
        return f"minter JSON is missing the {e} key (expected 'value' and 'ttl')"
    return f"{type(e).__name__}: {e}"


class _Rule:
    """One injection rule — a host whose auth this proxy rewrites, with its OWN minted-token cache.

    The proxy carries a SET of these (the multi-injector "rule set": github + claude + codex + any
    ``[[inject]]`` can all be live at once), each minting + caching independently. Built either from
    one entry of the ``INJECT_RULES`` JSON the foldyard proxy plugin emits, or — back-compat — from
    the legacy single ``INJECT_HOST``/``INJECT_COMMAND``/… env, which the proxy plugin still
    emits whenever there is exactly ONE rule."""

    def __init__(self, spec: dict) -> None:
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
            env=_minter_env(self.env_keys),
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
        self.rules = self._load_rules()
        self.inject_hosts = {r.host for r in self.rules}
        self.injecting = bool(self.rules)
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

    def _rule_for(self, host: str | None, path: str) -> _Rule | None:
        """The rule that should inject on this request, or None (capture-only / non-target). First
        match wins — distinct injectors use distinct hosts, so at most one matches in practice."""
        for rule in self.rules:
            if rule.matches(host, path):
                return rule
        return None

    # ── egress allowlist (the default-deny wall) ───────────────────────────────
    def _refresh_allow(self) -> None:
        """Re-read ALLOW_FILE when its mtime changes, so a host-side `allow` grant takes effect
        with NO daemon restart. Fail toward MORE blocking: a missing/unreadable/malformed file
        leaves the allow set EMPTY — a parse error never widens egress."""
        if self.allow_path is None:
            self._allow_patterns = []
            return
        try:
            mtime = self.allow_path.stat().st_mtime
        except OSError:
            self._allow_patterns = []
            self._allow_mtime = -1.0
            return
        if mtime == self._allow_mtime:
            return
        self._allow_mtime = mtime
        try:
            data = json.loads(self.allow_path.read_text())
            allow = data.get("allow", []) if isinstance(data, dict) else []
            self._allow_patterns = [str(p) for p in allow] if isinstance(allow, list) else []
        except (OSError, ValueError):
            self._allow_patterns = []

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
            "path": flow.request.path[:200],
            "status": flow.response.status_code,
            "injected": flow.request.pretty_host in self.inject_hosts,
            "replayed": bool(flow.metadata.get("egress_proxy_retried")),
        }  # fmt: skip
        # Error responses carry the reason in their body — capture a short snippet so "injected=True
        # but 401, why?" is answerable at a glance instead of by re-running the failing client.
        if flow.response.status_code >= 400:
            snippet = _error_snippet(flow.response)
            if snippet:
                entry["error_body"] = snippet
        self._write_entry(entry)

    def _log_passthrough(self, host: str | None) -> None:
        """An SNI-level row for a blind-tunnelled (not decrypted) HTTPS connection: host + time
        only — TLS hides method/path/status. The Network Log panel renders `passthrough` rows as
        a `tls tunnel` marker. No host (a client that sent no SNI) → nothing to log."""
        if not host:
            return
        self._write_entry({
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "method": "",
            "host": host,
            "path": "",
            "status": 0,
            "injected": False,
            "replayed": False,
            "passthrough": True,
        })  # fmt: skip

    def _log_blocked(self, host: str | None) -> None:
        """A row for a host REFUSED by the default-deny wall — no upstream is ever contacted, so
        there's no method/path/real status (we synthesise a 403). The Network Log panel keys off
        ``blocked`` to paint it red and offer an 'allow' action. No host → nothing to log."""
        if not host:
            return
        self._write_entry({
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "method": "",
            "host": host,
            "path": "",
            "status": 403,
            "injected": False,
            "replayed": False,
            "blocked": True,
        })  # fmt: skip

    # ── mitmproxy hooks ──────────────────────────────────────────────────────────
    def running(self) -> None:
        """Pre-mint every rule's token at proxy start, OFF the request path. ``request`` calls
        ``rule.token()`` synchronously on the event loop, so a slow first mint (e.g. ``uv run
        --script`` resolving a minter's PEP 723 env on a cold cache) would stall ALL proxied
        egress for its duration. Fire-and-forget daemon threads: a warm failure just logs (at WARN
        — ``warm=True``; an ERROR here makes mitmproxy exit, see ``token``) and the request path
        re-mints as before. Handles kept on ``self`` so tests can join."""
        self._warm_threads = [
            threading.Thread(
                target=lambda r=r: r.token(warm=True), daemon=True, name=f"egress-warm-{r.host}"
            )
            for r in self.rules
        ]
        for t in self._warm_threads:
            t.start()

    def http_connect(self, flow: http.HTTPFlow) -> None:
        """The egress wall for proxied HTTPS. The box reaches every HTTPS host through an explicit
        CONNECT to this proxy, and this hook fires on that CONNECT BEFORE any tunnel/TLS — so
        answering it with a 403 refuses the host outright (no upstream dialled, no TLS handshake,
        ``tls_clienthello`` never runs for it). Only the default-deny wall lives here; the
        decrypt-vs-passthrough choice is still ``tls_clienthello``'s job for hosts we DO allow."""
        if not self.default_deny:
            return
        host = flow.request.pretty_host
        if self._allowed(host):
            return
        flow.response = http.Response.make(403, b"blocked by foldyard egress wall\n")
        self._log_blocked(host)

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
        # Blind-tunnel (no decrypt, real certs end-to-end, SNI-only log) when either we're in
        # passthrough mode (capture=off — tunnel everything) OR we're in full mode but the host is
        # trusted. Otherwise (full mode, untrusted host) fall through → mitmproxy decrypts + the
        # request/response hooks log the full request — the surprising egress worth scrutinising.
        if self.capture_mode != "full" or _host_matches(target, self.passthrough_hosts):
            data.ignore_connection = True
            self._log_passthrough(target)

    def request(self, flow: http.HTTPFlow) -> None:
        # The egress wall for plain HTTP (cleartext never CONNECTs, so http_connect can't catch it):
        # refuse a disallowed host here, before it leaves the box. HTTPS is walled at http_connect.
        if self.default_deny and not self._allowed(flow.request.pretty_host):
            flow.response = http.Response.make(403, b"blocked by foldyard egress wall\n")
            flow.metadata["egress_proxy_blocked"] = True  # so `response` doesn't re-log it as a 403
            self._log_blocked(flow.request.pretty_host)
            return
        rule = self._rule_for(flow.request.pretty_host, flow.request.path)
        if rule is None:
            return  # capture-only, or not a target host/path → log on the way back, rewrite nothing
        value = rule.token()
        if value is not None:
            rule.apply(flow.request, value)

    async def response(self, flow: http.HTTPFlow) -> None:
        # Re-issue once on an upstream 401 BEFORE logging, so the log + the client both see the
        # final (retried) response. The response hook fires before the client is written to, so
        # overwriting flow.response here hands the retry straight back to the waiting caller. The
        # MATCHING rule decides (its own retry_401 + minter), so distinct injectors don't interfere.
        rule = self._rule_for(flow.request.pretty_host, flow.request.path)
        if (
            rule is not None
            and rule.retry_401
            and flow.response is not None
            and flow.response.status_code == 401
            and not flow.metadata.get("egress_proxy_retried")  # at most once — loop guard
        ):
            await self._reissue_401(flow, rule)
        self._log_flow(flow)

    async def _reissue_401(self, flow: http.HTTPFlow, rule: _Rule) -> None:
        """Re-mint THIS rule's token, re-issue the request ourselves with it, and replace
        flow.response with the result. We can't `replay.client` a *live* flow on modern mitmproxy
        (it rejects it with "Can't replay live flow"), and a replayed copy is detached from the
        original client — so the retry has to be a direct request whose response we hand back."""
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
        # (a dev box's Phase A′ env routes ALL egress at the Mac proxy: the re-issue then dials
        # a box-network upstream via the Mac and read-times-out — the in-box e2e failure mode).
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
