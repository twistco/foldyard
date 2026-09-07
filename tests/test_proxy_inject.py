"""The egress-proxy machinery, end-to-end APART FROM mitmproxy's transport and a real token.

``egress_proxy.py`` (the mitmdump addon foldyard's ``proxy`` plugin runs) is the heart of the
injection: mint a header value HOST-side, OVERWRITE it on the target host, log every proxied
response, and re-mint + re-issue the request once on an upstream 401. This drives that REAL addon
with a fake minter (prints a dummy ``{value, ttl}``) against a STUBBED mitmproxy transport + a
stubbed ``requests`` (so it runs anywhere, no mitmproxy/network needed) and asserts the rewrite,
the network log, the 401 re-issue, and token caching. The real mitmdump WIRE path (TLS MITM + the
box trusting the CA) is the opt-in ``test_proxy_e2e.py``. Skips if the (still consumer-side) addon
isn't in the checkout.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

# The canonical addon the proxy plugin runs (consumer-side until the framework extracts to core).
ADDON = Path(__file__).resolve().parents[1] / "src/foldyard/assets/proxy/egress_proxy.py"
pytestmark = pytest.mark.skipif(not ADDON.exists(), reason=f"proxy addon not found at {ADDON}")


class _Req:
    def __init__(self, host: str, method: str = "GET", path: str = "/x") -> None:
        self.pretty_host = host
        self.method = method
        self.path = path
        self.url = f"https://{host}{path}"  # the addon re-issues against this on a 401
        self.raw_content = b""
        self.headers: dict[str, str] = {}
        # mitmproxy's request.query is a settable MultiDictView reflected into the URL; the addon
        # only ever does `query[name] = value`, so a plain dict is a faithful enough stand-in.
        self.query: dict[str, str] = {}


class _Resp:
    def __init__(self, status: int, content: bytes = b"") -> None:
        self.status_code = status
        self.content = content  # the addon reads this for the 4xx/5xx error-body snippet


class _Flow:
    """A stand-in for mitmproxy's HTTPFlow — just the surface egress_proxy.py touches."""

    def __init__(
        self,
        host: str,
        status: int = 200,
        method: str = "GET",
        path: str = "/x",
        content: bytes = b"",
    ) -> None:
        self.request = _Req(host, method, path)
        self.response: _Resp | None = _Resp(status, content)
        self.metadata: dict = {}


@pytest.fixture
def gh(monkeypatch):
    """Load the REAL egress_proxy.py against a stubbed `mitmproxy` + a stubbed `requests`; expose the
    module plus the list of upstream re-issue requests the addon made (so the 401 retry is
    observable without a real network)."""
    # Build the fake `mitmproxy` as SimpleNamespaces (kwargs, no attribute assignment → no type
    # checker grumbles) and drop them into sys.modules so `from mitmproxy import ctx, http` binds
    # them. Only the surface egress_proxy.py touches: ctx.log.{warn,info,error}, http.HTTPFlow (a bare
    # annotation), and http.Response.make (used to rebuild the response from the 401 re-issue).
    # Record (level, message) rather than discarding: the LEVEL a mint failure is reported at is
    # load-bearing, not cosmetic — mitmproxy's ErrorCheck addon exits the process when anything
    # logs at ERROR during startup (see test_warm_up_mint_failure_stays_below_error).
    logged: list[tuple[str, str]] = []

    def _record(level: str):
        return lambda msg="", *a, **k: logged.append((level, str(msg)))

    ctx = types.SimpleNamespace(
        log=types.SimpleNamespace(
            warn=_record("warn"), info=_record("info"), error=_record("error")
        ),
        master=types.SimpleNamespace(),
    )

    class _MadeResp:
        def __init__(self, status, content, headers) -> None:
            self.status_code = status
            self.content = content
            self.headers = headers

    http = types.SimpleNamespace(
        HTTPFlow=object,
        Response=types.SimpleNamespace(
            make=lambda status, content=b"", headers=None: _MadeResp(status, content, headers or {})
        ),
    )
    # tls: only ClientHelloData is referenced (a bare annotation on tls_clienthello). A bare object
    # type satisfies the annotation; tests pass their own _ClientHello double at call time.
    tls = types.SimpleNamespace(ClientHelloData=object)

    mitm = types.SimpleNamespace(ctx=ctx, http=http, tls=tls)
    monkeypatch.setitem(sys.modules, "mitmproxy", mitm)
    monkeypatch.setitem(sys.modules, "mitmproxy.ctx", ctx)
    monkeypatch.setitem(sys.modules, "mitmproxy.http", http)
    monkeypatch.setitem(sys.modules, "mitmproxy.tls", tls)

    # The addon imports `requests` lazily to re-issue a 401. Stub it: record every call and return
    # a 200 echoing the Authorization it was handed, so a test can prove the FRESH token rode along.
    requests_made: list = []

    def _fake_request(method, url, *, headers=None, data=None, verify=None, **_):
        headers = dict(headers or {})
        requests_made.append({"method": method, "url": url, "headers": headers, "verify": verify})
        return types.SimpleNamespace(
            status_code=200,
            content=headers.get("Authorization", "").encode(),
            headers={"Content-Type": "text/plain"},
        )

    class _FakeSession:
        # The addon re-issues via a Session with trust_env=False (never the ambient proxy env);
        # mirror that surface, delegating to the recording _fake_request.
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, **kw):
            assert self.trust_env is False  # the re-issue must never honour ambient proxy env
            return _fake_request(method, url, **kw)

    monkeypatch.setitem(
        sys.modules,
        "requests",
        types.SimpleNamespace(request=_fake_request, Session=_FakeSession),
    )

    spec = importlib.util.spec_from_file_location("egress_proxy_under_test", ADDON)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Expose just what the tests need (the Injector class + the recorded re-issue requests + the
    # module itself, for rotation tests poking its size/backup globals) so we don't bolt an
    # attribute onto the ModuleType (which the type checkers rightly dislike).
    return types.SimpleNamespace(
        Injector=module.Injector, requests=requests_made, module=module, logs=logged
    )


@pytest.fixture
def fake_minter(tmp_path):
    """A host minter that prints `{value, ttl}` and counts its invocations (to prove caching).
    Stands in for gh-app-token — same contract, no GitHub App / real token needed."""
    calls = tmp_path / "minter.calls"
    script = tmp_path / "minter.py"
    script.write_text(
        "import json, pathlib\n"
        f"p = pathlib.Path({str(calls)!r})\n"
        "p.write_text(str((int(p.read_text()) if p.exists() else 0) + 1))\n"
        "print(json.dumps({'value': 'token FAKE', 'ttl': 3600}))\n"
    )
    return types.SimpleNamespace(command=f"{sys.executable} {script}", calls=calls)


@pytest.fixture
def injector(gh, monkeypatch, fake_minter, tmp_path):
    """A configured Injector + its egress log path (target host = api.github.com, fake minter)."""
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("INJECT_HOST", "api.github.com")
    monkeypatch.setenv("INJECT_COMMAND", fake_minter.command)
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    return gh.Injector(), log


def _last_log(log: Path) -> dict:
    return json.loads(log.read_text().splitlines()[-1])


def _write_allow(path: Path, allow: list[str], default_deny: bool = True) -> None:
    path.write_text(json.dumps({"default_deny": default_deny, "allow": allow}) + "\n")


@pytest.fixture
def walled(gh, monkeypatch, tmp_path):
    """A capture-only Injector with the default-deny wall ON, reading a writable ALLOW_FILE.
    Returns (Injector, allow_path, log_path) so a test can flip the allowlist live."""
    log = tmp_path / "egress.jsonl"
    allow = tmp_path / "allow-effective.json"
    _write_allow(allow, [])
    monkeypatch.setenv("INJECT_HOST", "")
    monkeypatch.setenv("INJECT_COMMAND", "")
    monkeypatch.setenv("DEFAULT_DENY", "1")
    monkeypatch.setenv("ALLOW_FILE", str(allow))
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    return gh.Injector(), allow, log


def test_drop_connect_chatter_filters_probe_noise_but_keeps_errors(gh):
    """The liveness probe (a bare TCP open/close) makes mitmproxy log connect+disconnect pairs on
    both ends; ``_DropConnectChatter`` swallows them so they don't drown real egress in the host
    log — but genuine connection FAILURES (distinct message prefixes) must survive."""
    import logging

    filt = gh.module._DropConnectChatter()

    def keeps(msg: str) -> bool:
        rec = logging.LogRecord("mitmproxy.proxy.server", logging.INFO, __file__, 0, msg, (), None)
        return filt.filter(rec)

    # Dropped: bare client lines (exact match) AND server lines carrying the upstream address.
    assert not keeps("client connect")
    assert not keeps("client disconnect")
    assert not keeps("server connect example.com:443")
    assert not keeps("server disconnect example.com:443")
    # Kept: real failures use other prefixes, and unrelated traffic is untouched.
    assert keeps("error establishing server connection: timed out")
    assert keeps("server connection to example.com:443 killed before connect: boom")
    assert keeps("GET https://api.github.com/x -> 200")


def test_drop_websocket_ping_pong_but_keeps_real_traffic(gh):
    """A live WebSocket heartbeats ping/pong frames every few seconds; mitmproxy logs each at INFO
    on the same ``mitmproxy.proxy.server`` logger. ``_DropWebsocketPingPong`` swallows those keep-
    alive lines so they don't livestream into the host log — real egress is untouched."""
    import logging

    filt = gh.module._DropWebsocketPingPong()

    def keeps(msg: str) -> bool:
        rec = logging.LogRecord("mitmproxy.proxy.server", logging.INFO, __file__, 0, msg, (), None)
        return filt.filter(rec)

    assert not keeps("Received WebSocket ping from server (payload: b'_\\xfaf\\xde')")
    assert not keeps("Received WebSocket pong from client (payload: b'_\\xfaf\\xde')")
    # Kept: a WebSocket handshake/close and unrelated egress are not keep-alive noise.
    assert keeps("WebSocket connection closed by client")
    assert keeps("GET https://api.github.com/x -> 200")


async def test_injects_minted_header_on_the_target_host(injector):
    inj, log = injector
    flow = _Flow("api.github.com")
    flow.request.headers["Authorization"] = "Bearer DUMMY"  # the box only ever sends a dummy
    inj.request(flow)
    assert flow.request.headers["Authorization"] == "token FAKE"  # overwritten host-side
    await inj.response(flow)
    entry = _last_log(log)
    assert (
        entry["injected"] is True and entry["host"] == "api.github.com" and entry["status"] == 200
    )


async def test_value_prefix_is_prepended_to_the_minted_value(
    gh, monkeypatch, fake_minter, tmp_path
):
    # INJECT_VALUE_PREFIX (the OAuth `authorization: Bearer <token>` case): the minter returns the
    # BARE token; the addon prepends the scheme in flight, so the upstream sees "Bearer token FAKE"
    # while the box only ever carried a dummy. Covers the live request AND the 401 re-issue.
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("INJECT_HOST", "api.anthropic.com")
    monkeypatch.setenv("INJECT_COMMAND", fake_minter.command)
    monkeypatch.setenv("INJECT_HEADER", "authorization")
    monkeypatch.setenv("INJECT_VALUE_PREFIX", "Bearer ")
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    inj = gh.Injector()

    flow = _Flow("api.anthropic.com")
    flow.request.headers["authorization"] = "Bearer sk-ant-oat-dummy"  # the box's dummy
    inj.request(flow)
    assert flow.request.headers["authorization"] == "Bearer token FAKE"  # bare token + scheme

    # The 401 re-issue must carry the SAME prefixed value (not a bare or double-prefixed token).
    flap = _Flow("api.anthropic.com", status=401)
    flap.request.headers["authorization"] = "Bearer sk-ant-oat-dummy"
    await inj.response(flap)
    assert gh.requests[-1]["headers"]["authorization"] == "Bearer token FAKE"


async def test_inject_rules_multi_injector_each_host_uses_its_own_minter(gh, monkeypatch, tmp_path):
    # The multi-injector rule set (INJECT_RULES JSON): two hosts, two distinct minters — each
    # request is rewritten with ITS host's token, proving independent per-rule caches/commands. This
    # is what the lifted single-minter contract buys (github + claude + … live at once).
    import json
    import sys

    def minter(token: str) -> str:
        s = tmp_path / f"m_{token}.py"
        s.write_text(f"import json; print(json.dumps({{'value': {token!r}, 'ttl': 3600}}))\n")
        return f"{sys.executable} {s}"

    rules = [
        {"host": "api.github.com", "command": minter("GH"), "header": "Authorization"},
        {
            "host": "api.anthropic.com",
            "command": minter("ANT"),
            "header": "authorization",
            "value_prefix": "Bearer ",
            "retry_401": True,
        },
    ]
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("INJECT_RULES", json.dumps(rules))
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    inj = gh.Injector()
    assert inj.injecting and inj.inject_hosts == {"api.github.com", "api.anthropic.com"}

    gh_flow = _Flow("api.github.com")
    gh_flow.request.headers["Authorization"] = "dummy"
    inj.request(gh_flow)
    assert gh_flow.request.headers["Authorization"] == "GH"  # github's minter, no prefix

    ant_flow = _Flow("api.anthropic.com")
    ant_flow.request.headers["authorization"] = "Bearer dummy"
    inj.request(ant_flow)
    assert ant_flow.request.headers["authorization"] == "Bearer ANT"  # anthropic's minter + Bearer

    # A host in NEITHER rule is left untouched (capture-only for it).
    other = _Flow("example.com")
    other.request.headers["Authorization"] = "keep"
    inj.request(other)
    assert other.request.headers["Authorization"] == "keep"


def test_inject_rules_wins_over_legacy_single_env(gh, monkeypatch, tmp_path):
    # When INJECT_RULES is set it WINS over the legacy single INJECT_* vars (which the proxy plugin
    # leaves empty in the multi-rule case, but a stray value must not leak a phantom extra rule).
    import json

    monkeypatch.setenv("INJECT_HOST", "legacy.example.com")
    monkeypatch.setenv("INJECT_COMMAND", "should-be-ignored")
    monkeypatch.setenv("INJECT_RULES", json.dumps([{"host": "api.x.test", "command": "true"}]))
    monkeypatch.setenv("PROXY_LOG_FILE", str(tmp_path / "e.jsonl"))
    inj = gh.Injector()
    assert inj.inject_hosts == {"api.x.test"}  # the legacy single env is ignored entirely


def test_inject_rules_malformed_json_injects_nothing(gh, monkeypatch, tmp_path):
    # A corrupt INJECT_RULES fails safe: no rules (never falls back to legacy, never half-injects).
    monkeypatch.setenv("INJECT_RULES", "{ not json")
    monkeypatch.setenv("INJECT_HOST", "legacy.example.com")
    monkeypatch.setenv("INJECT_COMMAND", "x")
    monkeypatch.setenv("PROXY_LOG_FILE", str(tmp_path / "e.jsonl"))
    inj = gh.Injector()
    assert inj.injecting is False and inj.inject_hosts == set()


@pytest.fixture
def qp_injector(gh, monkeypatch, fake_minter, tmp_path):
    """An Injector configured for QUERY-PARAM injection scoped to a path prefix — the penpot shape
    (rewrite ?userToken= on truenas, but only under /mcp). Returns (Injector, log)."""
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("INJECT_HOST", "truenas.example.ts.net")
    monkeypatch.setenv("INJECT_COMMAND", fake_minter.command)
    monkeypatch.setenv("INJECT_QUERY_PARAM", "userToken")
    monkeypatch.setenv("INJECT_HEADER", "")  # the proxy clears the header for query-param mode
    monkeypatch.setenv("INJECT_PATH_PREFIX", "/mcp")
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    return gh.Injector(), log


async def test_injects_query_param_on_the_target_path(qp_injector):
    inj, log = qp_injector
    flow = _Flow("truenas.example.ts.net", path="/mcp/stream")
    inj.request(flow)
    # The token lands in the query string, NOT a header (the box's MCP config carries no secret).
    assert flow.request.query["userToken"] == "token FAKE"
    assert "Authorization" not in flow.request.headers
    await inj.response(flow)
    assert _last_log(log)["injected"] is True


async def test_query_param_not_injected_outside_path_prefix(qp_injector):
    inj, _ = qp_injector
    flow = _Flow("truenas.example.ts.net", path="/app/index.html")  # same host, non-/mcp path
    inj.request(flow)
    assert "userToken" not in flow.request.query  # the app traffic on the same host is untouched


async def test_capture_only_logs_everything_and_injects_nothing(gh, monkeypatch, tmp_path):
    # Empty INJECT_HOST/COMMAND (the `capture` axis with no injector): the addon rewrites nothing
    # but still logs every proxied request, so all egress is observable with no credential.
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("INJECT_HOST", "")
    monkeypatch.setenv("INJECT_COMMAND", "")
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    inj = gh.Injector()
    assert inj.injecting is False
    flow = _Flow("api.github.com")
    flow.request.headers["Authorization"] = "Bearer DUMMY"
    inj.request(flow)
    assert flow.request.headers["Authorization"] == "Bearer DUMMY"  # untouched — no minting
    await inj.response(flow)
    entry = _last_log(log)
    assert entry["host"] == "api.github.com" and entry["status"] == 200
    assert entry["injected"] is False  # logged, never injected


async def test_error_response_captures_a_body_snippet(gh, monkeypatch, tmp_path):
    # On a 4xx the addon captures a truncated, single-lined body snippet so the Network Log shows
    # WHY it failed (e.g. an auth-token parse error), not just the code. 2xx bodies aren't captured.
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("INJECT_HOST", "")
    monkeypatch.setenv("INJECT_COMMAND", "")
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    inj = gh.Injector()
    body = b'{\n  "error": { "message": "Could not parse your authentication token." }\n}'
    await inj.response(_Flow("chatgpt.com", status=401, content=body))
    entry = _last_log(log)
    assert entry["status"] == 401
    assert "Could not parse your authentication token" in entry["error_body"]
    assert "\n" not in entry["error_body"]  # collapsed to one tidy line

    await inj.response(_Flow("chatgpt.com", status=200, content=b"hello"))
    assert "error_body" not in _last_log(log)  # success path carries no body snippet


def test_error_snippet_collapses_and_never_raises(gh):
    snip = gh.module._error_snippet
    assert snip(types.SimpleNamespace(content=b"  a\n  b  ")) == "a b"
    assert snip(types.SimpleNamespace(content=b"")) == ""  # empty body → no snippet

    class _Boom:  # a body that explodes when read must never break logging
        @property
        def content(self):
            raise RuntimeError("nope")

    assert snip(_Boom()) == ""


class _ClientHello:
    """A stand-in for mitmproxy's tls.ClientHelloData — the surface tls_clienthello touches:
    `.client_hello.sni` (the SNI) and the writable `.ignore_connection` (passthrough decision)."""

    def __init__(self, sni: str | None) -> None:
        self.client_hello = types.SimpleNamespace(sni=sni)
        self.ignore_connection = False


def test_passthrough_blind_tunnels_and_logs_an_sni_row(gh, monkeypatch, tmp_path):
    # CAPTURE_MODE=passthrough, no injector: a TLS ClientHello for any host is NOT decrypted
    # (ignore_connection=True) and logged as an SNI-only `passthrough` row (no method/path/status).
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("INJECT_HOST", "")
    monkeypatch.setenv("INJECT_COMMAND", "")
    monkeypatch.setenv("CAPTURE_MODE", "passthrough")
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    inj = gh.Injector()
    data = _ClientHello("example.com")
    inj.tls_clienthello(data)
    assert data.ignore_connection is True  # blind-tunnel: real certs end-to-end, no decrypt
    entry = _last_log(log)
    assert entry["host"] == "example.com" and entry["passthrough"] is True
    assert entry["method"] == "" and entry["status"] == 0  # TLS hides method/path/status


def test_full_capture_decrypts_every_host(gh, monkeypatch, tmp_path):
    # CAPTURE_MODE=full: tls_clienthello does NOT ignore — mitmproxy terminates TLS so the
    # request/response hooks can log the decrypted request. Nothing logged at ClientHello time.
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("INJECT_HOST", "")
    monkeypatch.setenv("INJECT_COMMAND", "")
    monkeypatch.setenv("CAPTURE_MODE", "full")
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    inj = gh.Injector()
    data = _ClientHello("example.com")
    inj.tls_clienthello(data)
    assert data.ignore_connection is False  # decrypt it
    assert not log.exists()  # full-capture logs at response time, not at ClientHello


def test_injector_host_is_always_decrypted_even_in_passthrough(injector):
    # The injector host MUST be decrypted (we rewrite its Authorization), so it's never tunnelled —
    # even under passthrough. INJECT_HOST=api.github.com from the `injector` fixture.
    inj, _log = injector
    inj.capture_mode = "passthrough"
    data = _ClientHello("api.github.com")
    inj.tls_clienthello(data)
    assert data.ignore_connection is False  # decrypted so the header rewrite can happen


def test_injector_host_beats_an_explicit_passthrough_listing(injector):
    # Even when a consumer LISTS the keyless host under `[proxy] passthrough` (e.g. via a broad
    # @bundle), injection must win: no decrypt = no header rewrite = keyless silently broken.
    # The inject-host check runs BEFORE any passthrough match, whatever [proxy] says.
    inj, _log = injector
    inj.capture_mode = "full"
    inj.passthrough_hosts = ["api.github.com"]
    data = _ClientHello("api.github.com")
    inj.tls_clienthello(data)
    assert data.ignore_connection is False


def test_host_matches_wildcard_semantics(gh):
    m = gh.module._host_matches
    assert m("registry.npmjs.org", ["registry.npmjs.org"])  # exact
    assert m("a.foo.com", ["*.foo.com"])  # subdomain
    assert m("a.b.foo.com", ["*.foo.com"])  # nested subdomain
    assert not m(
        "foo.com", ["*.foo.com"]
    )  # bare domain NOT matched by *.foo.com (CC Web semantics)
    assert not m("evil.com", ["*.foo.com", "bar.com"])  # no match
    assert not m(None, ["*.foo.com"])  # no SNI/target → no match


def test_full_capture_passes_through_trusted_but_decrypts_the_rest(gh, monkeypatch, tmp_path):
    # capture=on (full): TRUSTED hosts (PASSTHROUGH_HOSTS) are blind-tunnelled + SNI-logged (fast,
    # not decrypted); everything else is decrypted (ignore_connection stays False → mitmproxy
    # terminates TLS and the request hooks log it). This is the "watch the unknowns" posture.
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("INJECT_HOST", "")
    monkeypatch.setenv("INJECT_COMMAND", "")
    monkeypatch.setenv("CAPTURE_MODE", "full")
    monkeypatch.setenv("PASSTHROUGH_HOSTS", "registry.npmjs.org,*.googleapis.com")
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    inj = gh.Injector()

    trusted_exact = _ClientHello("registry.npmjs.org")
    trusted_glob = _ClientHello("storage.googleapis.com")
    unknown = _ClientHello("evil.example.com")
    for d in (trusted_exact, trusted_glob, unknown):
        inj.tls_clienthello(d)

    assert trusted_exact.ignore_connection is True  # trusted exact → passthrough
    assert trusted_glob.ignore_connection is True  # trusted *.googleapis.com → passthrough
    assert unknown.ignore_connection is False  # untrusted under full → decrypt (scrutinise)
    # Only the two trusted hosts produced SNI passthrough rows; the unknown is logged later (on
    # response), not here, so it's absent from the clienthello-time rows.
    logged = {json.loads(line)["host"] for line in log.read_text().splitlines()}
    assert logged == {"registry.npmjs.org", "storage.googleapis.com"}


def test_rotation_uses_dated_backups_and_prunes_to_newest(gh, monkeypatch, tmp_path):
    # Rotation renames the live log to a DATED backup (<stem>.<UTC-stamp>.jsonl, not a single .1)
    # and prunes to the newest _LOG_BACKUPS — readable, timestamped, bounded history.
    log = tmp_path / "egress.jsonl"
    log.write_text("live\n")
    for ts in ("20200101T000000Z", "20210101T000000Z", "20220101T000000Z"):
        (tmp_path / f"egress.{ts}.jsonl").write_text("old\n")  # backups share the live log's stem
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    monkeypatch.setattr(gh.module, "_LOG_BACKUPS", 2)
    inj = gh.Injector()
    inj._rotate()  # rename live → fresh dated backup (stamped "now"), prune to newest 2
    backups = sorted(p.name for p in tmp_path.glob("egress.*.jsonl"))
    assert len(backups) == 2  # bounded to _LOG_BACKUPS
    assert "egress.20200101T000000Z.jsonl" not in backups  # oldest pruned
    assert "egress.20220101T000000Z.jsonl" in backups  # 2nd-newest kept
    assert not log.exists()  # the live file was rotated away (a fresh one is created on next write)


async def test_leaves_other_hosts_untouched(injector):
    inj, log = injector
    flow = _Flow("example.com")
    flow.request.headers["Authorization"] = "keep me"
    inj.request(flow)
    assert flow.request.headers["Authorization"] == "keep me"  # not the target → no rewrite
    await inj.response(flow)
    assert _last_log(log)["injected"] is False


def test_caches_the_token_within_ttl(injector, fake_minter):
    inj, _ = injector
    inj.request(_Flow("api.github.com"))
    inj.request(_Flow("api.github.com"))
    assert fake_minter.calls.read_text() == "1"  # minted once, reused (ttl=3600)


def test_running_pre_mints_off_the_request_path(injector, fake_minter):
    # The `running` hook warms every rule's token in background threads, so a slow first mint
    # (e.g. `uv run --script` resolving a PEP 723 env cold) never stalls the event loop — and
    # the first real request reuses the warmed token instead of minting again.
    inj, _ = injector
    inj.running()
    for t in inj._warm_threads:
        t.join(timeout=10)
    assert fake_minter.calls.read_text() == "1"  # warmed at startup
    inj.request(_Flow("api.github.com"))
    assert fake_minter.calls.read_text() == "1"  # request path reused the warm token


def test_warm_up_mint_failure_stays_below_error(gh, monkeypatch, tmp_path, fake_minter):
    """A minter that fails during the startup warm-up must NOT log at ERROR — and must not take
    the healthy rules down with it.

    mitmproxy's ErrorCheck addon exits the process ("Error logged during startup, exiting...")
    when anything logs at ERROR while starting. So an inject rule whose credential is missing
    from host.env used to kill the WHOLE proxy — which the supervisor then respawns, forever —
    taking down egress for every host whose credential was perfectly fine, in every box on that
    checkout. Same invariant `requires` protects (a proxy that won't launch connection-refuses
    every box request): a missing credential degrades ONE host, never all egress. Two rules here,
    one broken and one working, so that "ONE host" half is asserted and not just implied.
    """
    boom = tmp_path / "boom.py"
    boom.write_text("import sys; sys.exit(1)\n")
    rules = [
        {"host": "api.github.com", "command": f"{sys.executable} {boom}"},
        {"host": "api.anthropic.com", "command": fake_minter.command},
    ]
    monkeypatch.setenv("INJECT_RULES", json.dumps(rules))
    monkeypatch.setenv("PROXY_LOG_FILE", str(tmp_path / "egress.jsonl"))
    inj = gh.Injector()

    inj.running()
    for t in inj._warm_threads:
        t.join(timeout=10)

    warm = [(level, msg) for level, msg in gh.logs if "mint failed" in msg]
    assert warm, "the warm-up failure must still be REPORTED, just not at ERROR"
    assert {level for level, _ in warm} == {"warn"}, f"ERROR during startup exits mitmproxy: {warm}"

    # The healthy rule warmed straight through the broken one: its token is cached, so the first
    # real request is served from that cache instead of paying a mint on the event loop.
    assert fake_minter.calls.read_text() == "1"
    ok = _Flow("api.anthropic.com")
    inj.request(ok)
    assert ok.request.headers["Authorization"] == "token FAKE"
    assert fake_minter.calls.read_text() == "1"

    # The request path is not the startup window, so a failure there stays a loud ERROR.
    gh.logs.clear()
    inj.request(_Flow("api.github.com"))
    assert {level for level, msg in gh.logs if "mint failed" in msg} == {"error"}


def test_mint_failure_log_carries_the_minters_own_reason(tmp_path, monkeypatch, gh):
    """
    A mint failure must name the host and quote the minter's stderr.

    `CalledProcessError` stringifies to "returned non-zero exit status 1" and nothing else, so
    the log used to show only the exit status and a python -m command line — while the minter
    had written the actual diagnosis to the stderr `subprocess.run` captured and dropped. The
    box-side symptom is `gh` getting 401s with a healthy-looking `fy mode`, and this log line
    is the only witness.
    """
    boom = tmp_path / "boom.py"
    boom.write_text(
        "import sys\n"
        "print('SECRET-TOKEN-VALUE')\n"  # stdout is where a real minter returns the token
        "print('github_app_token: GitHub returned 404 — app not installed', file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    monkeypatch.setenv(
        "INJECT_RULES",
        json.dumps([{"host": "api.github.com", "command": f"{sys.executable} {boom}"}]),
    )
    monkeypatch.setenv("PROXY_LOG_FILE", str(tmp_path / "egress.jsonl"))
    inj = gh.Injector()

    inj.request(_Flow("api.github.com"))

    failures = [msg for level, msg in gh.logs if "mint failed" in msg]
    assert len(failures) == 1
    message = failures[0]
    assert "api.github.com" in message, "the log has to name the axis that broke"
    assert "app not installed" in message, "the minter's stderr is the diagnosis — don't drop it"
    assert "exit 1" in message
    # stdout is where the token comes back; it must never reach the log.
    assert "SECRET-TOKEN-VALUE" not in message


def test_mint_failure_log_redacts_token_shaped_stderr(tmp_path, monkeypatch, gh):
    """
    stderr is diagnostic by contract, but a CRASHING minter doesn't honour contracts: a
    traceback can echo argv/env — or the half-minted credential itself — into stderr. The
    log keeps the prose of the diagnosis and the exit status, never an opaque token run.
    """
    boom = tmp_path / "boom.py"
    boom.write_text(
        "import sys\n"
        "print('mint blew up handling token ghs_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789', "
        "file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    monkeypatch.setenv(
        "INJECT_RULES",
        json.dumps([{"host": "api.github.com", "command": f"{sys.executable} {boom}"}]),
    )
    monkeypatch.setenv("PROXY_LOG_FILE", str(tmp_path / "egress.jsonl"))
    inj = gh.Injector()

    inj.request(_Flow("api.github.com"))

    failures = [msg for level, msg in gh.logs if "mint failed" in msg]
    assert len(failures) == 1
    message = failures[0]
    assert "ghs_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789" not in message
    assert "mint blew up handling token" in message, "the diagnosis prose must survive"
    assert "exit 1" in message, "exit-status context must survive redaction"


def test_mint_failure_never_logs_minter_stdout(tmp_path, monkeypatch, gh):
    """Malformed minter output is reported by SHAPE — its stdout may be a half-written token."""
    leaky = tmp_path / "leaky.py"
    leaky.write_text("print('ghs_averyrealtokenvalue')\n")  # exit 0, but not the {value,ttl} JSON
    monkeypatch.setenv(
        "INJECT_RULES",
        json.dumps([{"host": "api.github.com", "command": f"{sys.executable} {leaky}"}]),
    )
    monkeypatch.setenv("PROXY_LOG_FILE", str(tmp_path / "egress.jsonl"))
    inj = gh.Injector()

    inj.request(_Flow("api.github.com"))

    failures = [msg for level, msg in gh.logs if "mint failed" in msg]
    assert len(failures) == 1
    assert "ghs_averyrealtokenvalue" not in failures[0]
    assert "not the expected JSON" in failures[0]


async def test_re_mints_and_re_issues_once_on_401(injector, gh):
    inj, log = injector
    flow = _Flow("api.github.com", status=401)
    flow.request.headers["Authorization"] = "Bearer DUMMY"
    await inj.response(flow)

    assert flow.metadata.get("egress_proxy_retried") is True
    # The 401 was re-issued upstream exactly once, carrying the FRESHLY minted token (not the dummy).
    assert len(gh.requests) == 1
    assert gh.requests[0]["headers"]["Authorization"] == "token FAKE"
    assert gh.requests[0]["verify"] is True  # TLS verification stays on (system trust by default)
    # The retried response (200) replaced the 401 and is what the client now receives.
    assert flow.response is not None and flow.response.status_code == 200
    # ...and the egress log records it as a replayed 200.
    entry = _last_log(log)
    assert entry["status"] == 200 and entry["replayed"] is True

    # A flow already carrying the retry flag must NOT re-issue again (the loop guard).
    gh.requests.clear()
    retried = _Flow("api.github.com", status=401)
    retried.metadata["egress_proxy_retried"] = True
    await inj.response(retried)
    assert gh.requests == []


# ── the default-deny egress wall (DEFAULT_DENY + ALLOW_FILE) ──────────────────────────────


def test_default_deny_blocks_a_disallowed_https_connect_and_passes_allowed(walled):
    # The wall lives in http_connect: a host not in ALLOW_FILE is refused with a 403 at CONNECT
    # (no upstream dialled), an allowed host (exact or *.suffix) tunnels through untouched.
    inj, allow, log = walled
    _write_allow(allow, ["good.example.com", "*.trusted.dev"])

    blocked = _Flow("evil.example.com")
    inj.http_connect(blocked)
    assert blocked.response is not None and blocked.response.status_code == 403
    entry = _last_log(log)
    assert (
        entry["host"] == "evil.example.com" and entry["blocked"] is True and entry["status"] == 403
    )

    for ok in ("good.example.com", "api.trusted.dev"):
        f = _Flow(ok)
        f.response = None  # an allowed host must NOT be short-circuited
        inj.http_connect(f)
        assert f.response is None  # tunnel proceeds (exact + glob both allowed)


def test_default_deny_off_never_blocks(gh, monkeypatch, tmp_path):
    # No DEFAULT_DENY: http_connect is a no-op — back-compat for the pre-allowlist behaviour.
    monkeypatch.setenv("INJECT_HOST", "")
    monkeypatch.setenv("INJECT_COMMAND", "")
    monkeypatch.setenv("PROXY_LOG_FILE", str(tmp_path / "egress.jsonl"))
    inj = gh.Injector()
    assert inj.default_deny is False
    f = _Flow("anything.example.com")
    f.response = None
    inj.http_connect(f)
    assert f.response is None


def test_default_deny_exempts_the_injector_host(gh, monkeypatch, tmp_path):
    # The injector host is ALWAYS reachable even with an empty allowlist — we must reach it to mint.
    allow = tmp_path / "allow-effective.json"
    _write_allow(allow, [])
    monkeypatch.setenv("INJECT_HOST", "api.github.com")
    monkeypatch.setenv("INJECT_COMMAND", "true")
    monkeypatch.setenv("DEFAULT_DENY", "1")
    monkeypatch.setenv("ALLOW_FILE", str(allow))
    monkeypatch.setenv("PROXY_LOG_FILE", str(tmp_path / "egress.jsonl"))
    inj = gh.Injector()
    f = _Flow("api.github.com")
    f.response = None
    inj.http_connect(f)
    assert f.response is None  # exempt despite the empty allowlist


def test_default_deny_blocks_plain_http_in_the_request_hook(walled):
    # Cleartext HTTP never CONNECTs, so http_connect can't catch it — the wall also guards `request`.
    inj, _allow, log = walled
    f = _Flow("plain.example.com")
    inj.request(f)
    assert f.response is not None and f.response.status_code == 403
    assert f.metadata.get("egress_proxy_blocked") is True  # so `response` won't re-log it as a 403
    entry = _last_log(log)
    assert entry["host"] == "plain.example.com" and entry["blocked"] is True


def test_allow_grant_takes_effect_live_without_a_restart(walled):
    # The whole point of re-reading ALLOW_FILE per request (mtime-cached): a host-side `allow`
    # lands with NO daemon restart. Same Injector instance, blocked → granted → allowed.
    import os

    inj, allow, _log = walled
    first = _Flow("later.example.com")
    first.response = None
    inj.http_connect(first)
    assert first.response is not None  # blocked at first (empty allowlist)

    _write_allow(allow, ["later.example.com"])
    os.utime(allow, (inj._allow_mtime + 10, inj._allow_mtime + 10))  # force a fresh mtime

    second = _Flow("later.example.com")
    second.response = None
    inj.http_connect(second)
    assert second.response is None  # now allowed, without rebuilding the addon


def test_malformed_allow_file_fails_closed(gh, monkeypatch, tmp_path):
    # Fail toward MORE blocking: a corrupt ALLOW_FILE allows nothing extra (parse error ≠ open egress).
    allow = tmp_path / "allow-effective.json"
    allow.write_text("{ not valid json")
    monkeypatch.setenv("INJECT_HOST", "")
    monkeypatch.setenv("INJECT_COMMAND", "")
    monkeypatch.setenv("DEFAULT_DENY", "1")
    monkeypatch.setenv("ALLOW_FILE", str(allow))
    monkeypatch.setenv("PROXY_LOG_FILE", str(tmp_path / "egress.jsonl"))
    inj = gh.Injector()
    f = _Flow("anything.example.com")
    f.response = None
    inj.http_connect(f)
    assert f.response is not None and f.response.status_code == 403  # corrupt file → still blocks


# ── the minter's environment is an ALLOWLIST, not an inheritance ───────────────────────


def test_minter_env_is_the_base_plus_declared_names_only(gh, monkeypatch):
    # mitmdump is a child of the supervisor, which merges ALL of host.env into its environment —
    # every axis's secret. A minter that inherited that got the whole credential set for the price
    # of one mint; the github minter has no business reading the Anthropic key.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    monkeypatch.setenv("GH_PEM_B64", "cGVt")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/Users/dev")
    env = gh.module._minter_env(("GH_PEM_B64",))
    assert env["GH_PEM_B64"] == "cGVt"
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/Users/dev"  # base: run + find configs
    assert "ANTHROPIC_API_KEY" not in env


def test_minter_env_passes_proxy_vars_through(gh, monkeypatch):
    # A minter on a Mac behind a mandatory egress proxy has no other way out, so the proxy vars are
    # part of the base (the github kind then decides which of them is OUR listener).
    monkeypatch.setenv("HTTPS_PROXY", "http://corp.example:3128")
    monkeypatch.setenv("no_proxy", "localhost")
    env = gh.module._minter_env(())
    assert env["HTTPS_PROXY"] == "http://corp.example:3128" and env["no_proxy"] == "localhost"


def test_mint_subprocess_really_cannot_see_undeclared_secrets(gh, monkeypatch, tmp_path):
    # End to end through the real _mint(): a minter that reports the GH_*/ANTHROPIC_* names it can
    # actually see. Proves the allowlist reaches the subprocess, not just the helper.
    script = tmp_path / "reporter.py"
    script.write_text(
        "import json, os\n"
        "seen = sorted(k for k in os.environ if k.startswith(('GH_', 'ANTHROPIC_')))\n"
        "print(json.dumps({'value': ','.join(seen), 'ttl': 60}))\n"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    monkeypatch.setenv("GH_PEM_B64", "cGVt")
    monkeypatch.setenv("GH_APP_ID", "1234567")
    rule = gh.module._Rule(
        {
            "host": "api.github.com",
            "command": f"{sys.executable} {script}",
            "env": ["GH_APP_ID"],  # deliberately NOT GH_PEM_B64, to prove the allowlist bites
        }
    )
    assert rule.token() == "GH_APP_ID"
