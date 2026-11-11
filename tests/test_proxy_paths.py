"""Path grants in the egress proxy: ``host/prefix/`` lets the box READ under one prefix of a host
and nothing else on it.

The case they exist for is a redirect: a granted CDN (``cdn.playwright.dev``) sends a browser
download to one bucket of a shared host (``storage.googleapis.com/chrome-for-testing-public/``),
where granting the host would grant every bucket on it, uploads included. Following redirects in
the proxy instead was rejected: an open redirector on any granted host would then reach anywhere.

What the grant must hold, each pinned below against the REAL addon over a stubbed mitmproxy (the
wire path — the dial counted on a real socket — is ``test_proxy_wall_e2e.py``):

- only GET/HEAD, no body, no ``Upgrade``, at or under the prefix on a segment boundary, judged on
  a raw path that no server could normalise to somewhere else (dot segments, ``//``, encoded
  separators refused);
- the host is never blind-tunnelled (not on ``passthrough``, not for a build): a path can only be
  judged decrypted. A path grant can be BUILD-scoped too (``build_allow``): a host-started build
  reaches it decrypted — the walled VM's build steps trust the proxy CA — while the box doesn't
  reach it at all;
- nothing is dialled for a connection until it has earned it — a ClientHello (so every request in
  it is decrypted and judged) or a cleartext request the grant let through — so a raw TCP tunnel
  to the host is refused its dial, and raw bytes inside a decrypted one are never relayed.
"""

from __future__ import annotations

import asyncio
import types

import pytest

import test_proxy_inject as t
from foldyard import allowlist

gh = t.gh  # the REAL addon over a stubbed mitmproxy
walled = t.walled
_Flow = t._Flow
_ClientHello = t._ClientHello
_dial = t._dial
_answered = t._answered
_write_allow = t._write_allow
_rows = t._rows
building = t.building  # `walled` while the host runs a build (its secret live)

GCS = "storage.googleapis.com"
BUCKET = "/chrome-for-testing-public/"
GRANT = f"{GCS}{BUCKET}"
ZIP = f"{BUCKET}156.0.8078.4/linux-arm64/chrome-linux-arm64.zip"


def _redirect(host: str, location: str, path: str = "/x") -> _Flow:
    """A granted host's 302 to ``location``, as the upstream sent it."""
    flow = _Flow(host, status=302, path=path)
    response = t._Resp(302)
    response.headers["location"] = location
    flow.response = response
    return flow


def _request(host: str = GCS, path: str = ZIP, method: str = "GET", **kw) -> _Flow:
    flow = _Flow(host, method=method, path=path, **kw)
    flow.response = None
    return flow


async def _judged(inj, flow: _Flow) -> bool:
    """Did the wall let ``flow`` through (no answer of the proxy's own)?"""
    await inj.requestheaders(flow)
    return flow.response is None


# ── parsing: the proxy and foldyard agree on what a path grant is ─────────────────────────────

KEYS = [
    GRANT,
    f"{GCS}/chrome-for-testing-public",  # no trailing slash: a segment boundary still applies
    "Storage.GoogleAPIs.com/bucket/",  # the host compares case-insensitively
    "127.0.0.1/repo/",  # an address: typed, never offered (allowlist.local_key)
    f"{GCS}/a;b/",
    f"{GCS}/a/b/c/",
]
NOT_KEYS = [
    GCS,  # a host grant
    f"{GCS}/",  # no segment: that is the whole host
    f"*.googleapis.com{BUCKET}",  # no glob
    f"{GCS}:443{BUCKET}",  # no port
    f"{GCS}/a/../b/",
    f"{GCS}/a/./b/",
    f"{GCS}/a/..;x/b/",
    f"{GCS}/a//b/",
    f"{GCS}/a%2fb/",
    f"{GCS}/a%2E%2E/",
    f"{GCS}/a%255c/",
    f"{GCS}/a\\b/",
    f"{GCS}/a?b",
    f"{GCS}/a b/",
    f"https://{GCS}{BUCKET}",
    f"/{BUCKET}",
]


@pytest.mark.parametrize("key", KEYS)
def test_a_path_grant_parses_the_same_in_the_proxy_and_in_foldyard(gh, key):
    ours = allowlist.path_grant(key)
    assert ours is not None, key
    assert gh.module._path_grant(ours[0] + ours[1]) == ours  # what foldyard stores, the proxy reads
    assert gh.module._path_grant(key) == ours


@pytest.mark.parametrize("key", NOT_KEYS)
def test_what_is_not_a_path_grant_grants_no_path_anywhere(gh, key):
    assert allowlist.path_grant(key) is None, key
    assert gh.module._path_grant(key) is None, key


# ── the request wall ───────────────────────────────────────────────────────────────────────────


async def test_a_read_under_the_prefix_goes_through_and_nothing_else_on_the_host(walled):
    inj, allow, log = walled
    _write_allow(allow, [GRANT])
    assert await _judged(inj, _request())
    assert await _judged(inj, _request(method="HEAD"))
    assert await _judged(inj, _request(path=f"{ZIP}?generation=1&alt=media"))  # a query is fine

    outside = _request(path="/another-bucket/secret.txt")
    assert not await _judged(inj, outside)
    assert outside.response is not None and outside.response.status_code == 403
    assert GRANT in outside.response.content.decode()  # names what IS granted
    row = _rows(log)[-1]
    assert row["blocked"] and row["outside"] and row["path"] == "/another-bucket/secret.txt"


@pytest.mark.parametrize(
    "method, path, headers",
    [
        ("POST", ZIP, {}),
        ("PUT", ZIP, {}),
        ("DELETE", ZIP, {}),
        ("OPTIONS", ZIP, {}),
        ("GET", ZIP, {"content-length": "7"}),  # a read carries no body
        ("GET", ZIP, {"transfer-encoding": "chunked"}),
        ("GET", ZIP, {"upgrade": "websocket"}),  # one read must not become a channel
        ("GET", "/chrome-for-testing-public-evil/x", {}),  # a segment boundary
        ("GET", "/chrome-for-testing-public", {}),  # the prefix is a directory: `/` included
        ("GET", "/chrome-for-testing-public/../other/x", {}),
        ("GET", "/chrome-for-testing-public/..;/other/x", {}),
        ("GET", "/chrome-for-testing-public/%2e%2e/other/x", {}),
        ("GET", "/chrome-for-testing-public%2f..%2fother/x", {}),
        ("GET", "/chrome-for-testing-public//other/x", {}),
        ("GET", "/chrome-for-testing-public/\\..\\other", {}),
        ("GET", "/chrome-for-testing-public/%252e%252e/other", {}),  # decoded twice
        ("GET", "/chrome-for-testing-public/café", {}),
        ("GET", "/upload/storage/v1/b/chrome-for-testing-public/o", {}),
        ("GET", "*", {}),
    ],
)
async def test_anything_but_a_plain_read_under_the_prefix_is_refused(walled, method, path, headers):
    inj, allow, _log = walled
    _write_allow(allow, [GRANT])
    flow = _request(path=path, method=method)
    flow.request.headers.update(headers)
    assert not await _judged(inj, flow)
    assert flow.response is not None and flow.response.status_code == 403


async def test_a_body_the_headers_dont_announce_never_leaves(walled):
    # HTTP/2: HEADERS without END_STREAM, then DATA, and no content-length — nothing in the headers
    # to refuse, and mitmproxy streams a body past `stream_large_bodies` upstream before `request`
    # fires. So the read is streamed through a filter that keeps nothing (the wire: the e2e's
    # `test_a_body_an_http2_read_doesnt_announce_never_reaches_the_host`).
    inj, allow, _log = walled
    _write_allow(allow, [GRANT])
    flow = _request()
    assert await _judged(inj, flow)
    stream = flow.request.stream
    assert callable(stream)
    assert stream(b"exfiltrated") == b"" and stream(b"") == b""
    assert flow.metadata["egress_proxy_body_dropped"] is True
    # Trailers come after the body and go upstream after `request`: dropped there too.
    flow.request.trailers = {"x-trailer": "exfiltrated"}
    await inj.request(flow)
    assert not flow.request.trailers and flow.response is None
    # Only a read that leaves: mitmproxy won't stream a request the proxy answered itself.
    refused = _request(path="/another-bucket/x")
    assert not await _judged(inj, refused)
    assert refused.request.stream is None
    # A request whose body is already in when it is first judged (a caller that skips
    # `requestheaders`) is refused outright.
    late = _request()
    late.request.raw_content = b"exfiltrated"
    await inj.request(late)
    assert late.response is not None and late.response.status_code == 403


async def test_a_prefix_without_a_slash_covers_itself_and_below_never_a_sibling(walled):
    inj, allow, _log = walled
    _write_allow(allow, [f"{GCS}/bucket/file.zip"])
    assert await _judged(inj, _request(path="/bucket/file.zip"))
    assert await _judged(inj, _request(path="/bucket/file.zip/part"))
    assert not await _judged(inj, _request(path="/bucket/file.zip.sig"))


async def test_a_path_grant_covers_the_ports_a_host_grant_does_and_no_other(walled):
    inj, allow, _log = walled
    _write_allow(allow, [GRANT])
    assert await _judged(inj, _request(port=80, scheme="http"))
    assert not await _judged(inj, _request(port=8443))
    assert not await _judged(inj, _request(port=443, scheme="http"))


async def test_a_path_grant_reaches_no_other_host(walled):
    inj, allow, _log = walled
    _write_allow(allow, [GRANT])
    for host in ("evil.googleapis.com", "googleapis.com", "x.storage.googleapis.com"):
        assert not await _judged(inj, _request(host=host)), host


async def test_a_host_grant_beside_it_still_grants_the_whole_host(walled):
    inj, allow, _log = walled
    _write_allow(allow, [GRANT, GCS])
    flow = _request(path="/upload/x", method="POST")
    assert await _judged(inj, flow)


async def test_a_malformed_or_build_entry_grants_the_box_nothing(walled):
    inj, allow, _log = walled
    allow.write_text(
        '{"default_deny": true, "allow": ["storage.googleapis.com/a/../b/"],'
        ' "build_allow": ["storage.googleapis.com/chrome-for-testing-public/"]}'
    )
    assert not await _judged(inj, _request(path="/a/../b/x"))
    assert not await _judged(inj, _request(path="/b/x"))
    assert not await _judged(inj, _request())  # a build entry is never read as runtime …
    assert inj._build_patterns == []  # … nor as its host for a build
    assert inj._build_path_grants == {GCS: (BUCKET,)}  # it is a path grant for builds


# ── the tunnel: answered, decrypted, dialled only once earned ───────────────────────────────────


def _connect(inj, host: str = GCS, client_id: str = "client-1") -> _Flow:
    flow = _Flow(host, method="CONNECT", path="")
    flow.client_conn = types.SimpleNamespace(id=client_id)
    flow.response = None
    inj.http_connect(flow)
    return flow


def test_the_connect_is_answered_and_nothing_dialled_before_a_client_hello(walled):
    inj, allow, _log = walled
    _write_allow(allow, [GRANT])
    connect = _connect(inj)
    assert _answered(inj, connect)  # no eager dial; a raw TCP tunnel's dial is refused
    assert _dial(inj, GCS, client_id="client-1") is not None

    hello = _ClientHello(GCS)
    inj.tls_clienthello(hello)
    assert hello.ignore_connection is False  # decrypted: every request inside is judged
    assert _dial(inj, GCS, client_id="client-1") is None  # the handshake's dial may go out
    assert _dial(inj, GCS, client_id="client-2") is not None  # for that connection alone

    inj.client_disconnected(types.SimpleNamespace(id="client-1"))
    assert _dial(inj, GCS, client_id="client-1") is not None


def test_a_path_host_is_never_blind_tunnelled(gh, walled, monkeypatch):
    inj, allow, _log = walled
    _write_allow(allow, [GRANT])
    inj.passthrough_hosts = [GCS, "*.googleapis.com"]
    _connect(inj)
    hello = _ClientHello(GCS)
    inj.tls_clienthello(hello)
    assert hello.ignore_connection is False
    assert inj._tunnel(GCS, build=True) is False  # not for a build either: it has no path to judge
    # Granted as a whole, passthrough applies as before.
    _write_allow(allow, [GRANT, GCS])
    assert inj._tunnel(GCS, build=False) is True


def test_a_revoked_path_grant_refuses_the_next_dial(walled):
    inj, allow, _log = walled
    _write_allow(allow, [GRANT])
    _connect(inj)
    inj.tls_clienthello(_ClientHello(GCS))
    assert _dial(inj, GCS) is None
    _write_allow(allow, [])
    inj._allow_mtime = -1.0  # same-second rewrite: force the re-read
    assert _dial(inj, GCS) is not None
    assert not inj._connect_ok(GCS, 443)  # and `_sweep` closes the connection


async def test_a_cleartext_read_dials_for_its_own_connection(walled):
    inj, allow, _log = walled
    _write_allow(allow, [GRANT])
    assert _dial(inj, GCS, 80) is not None
    assert await _judged(inj, _request(port=80, scheme="http"))
    assert _dial(inj, GCS, 80) is None
    assert _dial(inj, GCS, 80, client_id="client-2") is not None


def test_raw_bytes_inside_a_decrypted_tunnel_are_never_relayed(walled):
    # mitmproxy hands a decrypted stream whose first bytes don't look like HTTP (a leading CRLF
    # does it) to its raw TCP layer, which sends each message after `tcp_message`.
    inj, allow, log = walled
    _write_allow(allow, [GRANT])
    message = types.SimpleNamespace(content=b"\r\nPUT /other-bucket/x HTTP/1.1\r\n\r\n")
    flow = types.SimpleNamespace(
        server_conn=types.SimpleNamespace(address=(GCS, 443)),
        client_conn=types.SimpleNamespace(id="client-1"),
        messages=[message],
        metadata={},
    )
    inj.tcp_message(flow)
    assert message.content == b""
    row = _rows(log)[-1]
    assert row["blocked"] and row["method"] == "TCP" and row["outside"] and row["host"] == GCS
    second = types.SimpleNamespace(content=b"more")
    flow.messages.append(second)
    inj.tcp_message(flow)
    assert second.content == b"" and len([r for r in _rows(log) if r["method"] == "TCP"]) == 1

    # A host granted as a whole keeps its tunnel.
    _write_allow(allow, [GCS])
    inj._allow_mtime = -1.0
    kept = types.SimpleNamespace(content=b"raw")
    inj.tcp_message(
        types.SimpleNamespace(
            server_conn=flow.server_conn, client_conn=flow.client_conn, messages=[kept], metadata={}
        )
    )
    assert kept.content == b"raw"


async def test_a_path_grant_on_an_address_reaches_it_by_that_address_only(walled):
    # A path grant naming a local address lets that address's own earned dial through …
    inj, allow, _log = walled
    _write_allow(allow, ["127.0.0.1/repo/", "dev.example.com"])
    _connect(inj, "127.0.0.1")
    inj.tls_clienthello(_ClientHello("127.0.0.1"))
    assert _dial(inj, "127.0.0.1") is None
    # … but never a name resolving there: that name is granted as a whole, so every request on
    # it would reach the address the operator granted one read under /repo/ on.
    await t._resolving_to(inj, ["127.0.0.1"])
    with pytest.raises(OSError, match="a local address"):
        await asyncio.get_running_loop().getaddrinfo("dev.example.com", 443)


# ── the redirect that brought the box here ────────────────────────────────────────────────────


async def test_a_refusal_after_a_redirect_names_it_and_the_path_grant(walled):
    inj, allow, log = walled
    _write_allow(allow, ["cdn.playwright.dev"])
    cdn = _redirect(
        "cdn.playwright.dev",
        f"https://{GCS}{ZIP}?x-goog-signature=abc",
        "/builds/chromium/1200/chromium-linux.zip",
    )
    inj.responseheaders(cdn)

    follow = _request()
    assert not await _judged(inj, follow)
    assert follow.response is not None
    body = follow.response.content.decode()
    assert "cdn.playwright.dev redirected here" in body and f"fy allow add {GRANT}" in body
    row = _rows(log)[-1]
    assert row["blocked"] and row["via"] == "cdn.playwright.dev"

    # A request it didn't redirect to is refused plainly.
    other = _request(path="/elsewhere/x")
    assert not await _judged(inj, other)
    assert "via" not in _rows(log)[-1]

    # Remembered only to explain: the redirect never lets anything through.
    assert not await _judged(inj, _request())


def test_a_redirect_to_a_granted_host_or_the_same_one_is_not_remembered(walled):
    inj, allow, _log = walled
    _write_allow(allow, ["cdn.playwright.dev", "objects.example.com"])
    for location in ("https://objects.example.com/x", "/relative/path"):
        inj.responseheaders(_redirect("cdn.playwright.dev", location))
    assert inj._redirects == {}


def test_the_redirect_memory_is_bounded(gh, walled):
    inj, allow, _log = walled
    _write_allow(allow, ["cdn.example.com"])
    for i in range(gh.module._REDIRECT_MEMORY_MAX + 50):
        inj.responseheaders(_redirect("cdn.example.com", f"https://h{i}.example.net/x"))
    assert len(inj._redirects) == gh.module._REDIRECT_MEMORY_MAX


# ── the Network Log line ──────────────────────────────────────────────────────────────────────


def test_the_network_log_says_outside_and_where_the_redirect_came_from():
    from foldyard.plugins import proxy

    row = {"ts": "2026-11-08T20:23:05+00:00", "method": "GET", "path": ZIP, "blocked": True}
    line = proxy._net_leaf_body({**row, "outside": True}, lambda s: s)
    assert "outside the paths this host is granted for" in line
    line = proxy._net_leaf_body({**row, "via": "cdn.playwright.dev"}, lambda s: s)
    assert "not on the allowlist (redirected from cdn.playwright.dev)" in line


# ── for builds: a build-scoped path grant, judged decrypted ───────────────────────────────────


def _write_build_allow(allow, build: list[str], runtime: list[str] | None = None) -> None:
    import json

    allow.write_text(
        json.dumps({"default_deny": True, "allow": runtime or [], "build_allow": build}) + "\n"
    )


def _build_connect(inj, host: str = GCS, client_id: str = "build-1") -> _Flow:
    flow = t._build(_Flow(host, method="CONNECT", path=""))
    flow.client_conn = types.SimpleNamespace(id=client_id, sni=None)
    flow.response = None
    inj.http_connect(flow)
    return flow


def _inside(host: str = GCS, path: str = ZIP, client_id: str = "build-1", **kw) -> _Flow:
    """A request inside a decrypted connection: no Proxy-Authorization of its own."""
    flow = _request(host, path, **kw)
    flow.client_conn = types.SimpleNamespace(id=client_id, sni=None)
    return flow


async def test_a_build_path_grant_lets_the_build_read_under_it_decrypted(building):
    inj, allow, log = building
    _write_build_allow(allow, [GRANT])
    connect = _build_connect(inj)
    assert _answered(inj, connect)  # nothing dialled before a ClientHello …
    hello = _ClientHello(GCS, client_id="build-1")
    inj.tls_clienthello(hello)
    assert hello.ignore_connection is False  # … decrypted, never tunnelled, though it is a build
    assert _dial(inj, GCS, client_id="build-1") is None

    read = _inside()
    assert await _judged(inj, read)
    read.response = t._Resp(200)
    await inj.response(read)
    row = _rows(log)[-1]
    assert row["build"] is True and row["path"] == ZIP and not row.get("blocked")

    outside = _inside(path="/another-bucket/x")
    assert not await _judged(inj, outside)
    assert outside.response is not None and GRANT in outside.response.content.decode()
    row = _rows(log)[-1]
    assert row["blocked"] and row["outside"] and row["build"] is True  # the gate reads this
    assert not await _judged(inj, _inside(method="PUT"))  # a read only, as for the box


async def test_a_build_path_grant_is_nothing_to_the_box(building):
    inj, allow, _log = building
    _write_build_allow(allow, [GRANT])
    box = _Flow(GCS, method="CONNECT", path="")
    box.client_conn = types.SimpleNamespace(id="box-1", sni=None)
    box.response = None
    inj.http_connect(box)
    inj.tls_clienthello(_ClientHello(GCS, client_id="box-1"))
    assert _dial(inj, GCS, client_id="box-1") is not None
    assert not await _judged(inj, _inside(client_id="box-1"))
    # The bare marker is public: it is the box.
    bare = t._marked(_Flow(GCS, method="CONNECT", path=""))
    bare.client_conn = types.SimpleNamespace(id="box-2", sni=None)
    bare.response = None
    inj.http_connect(bare)
    assert not await _judged(inj, _inside(client_id="box-2"))


def test_a_builds_whole_host_grant_is_tunnelled_as_before(building):
    # The whole host for builds wins over a path grant for the box: the build's connection is
    # tunnelled; only a host that the build reaches by path alone is decrypted.
    inj, allow, _log = building
    _write_build_allow(allow, [GCS], runtime=[GRANT])
    assert _build_connect(inj).response is None
    hello = _ClientHello(GCS, client_id="build-1")
    inj.tls_clienthello(hello)
    assert hello.ignore_connection is True
    assert inj._tunnel(GCS, build=True) is True and inj._tunnel(GCS, build=False) is False


def test_raw_bytes_in_a_builds_path_connection_are_never_relayed(building):
    inj, allow, log = building
    _write_build_allow(allow, [GRANT])
    _build_connect(inj)
    message = types.SimpleNamespace(content=b"\r\nPUT /other-bucket/x HTTP/1.1\r\n\r\n")
    inj.tcp_message(
        types.SimpleNamespace(
            server_conn=types.SimpleNamespace(address=(GCS, 443)),
            client_conn=types.SimpleNamespace(id="build-1"),
            messages=[message],
            metadata={},
        )
    )
    assert message.content == b""
    row = _rows(log)[-1]
    assert row["method"] == "TCP" and row["outside"] and row["build"] is True


def test_a_revoked_build_path_grant_refuses_the_next_dial(building):
    inj, allow, _log = building
    _write_build_allow(allow, [GRANT])
    _build_connect(inj)
    inj.tls_clienthello(_ClientHello(GCS, client_id="build-1"))
    assert _dial(inj, GCS, client_id="build-1") is None
    _write_build_allow(allow, [])
    inj._allow_mtime = -1.0
    assert _dial(inj, GCS, client_id="build-1") is not None
    assert not inj._connect_ok(GCS, 443, build=True)  # and `_sweep` closes it


async def test_a_builds_refused_request_names_its_path(building):
    # A refused host is answered and decrypted for a build too, so the row the build gate reads
    # carries the path it can offer a path grant from — not only the host.
    inj, _allow, log = building
    connect = _build_connect(inj)
    assert _answered(inj, connect)
    inj.tls_clienthello(_ClientHello(GCS, client_id="build-1"))
    refused = _inside()
    assert not await _judged(inj, refused)
    row = _rows(log)[-1]
    assert row["blocked"] and row["build"] is True and row["path"] == ZIP and row["host"] == GCS


def test_a_build_path_grant_on_an_address_dials_it_for_the_build_alone(building):
    inj, allow, _log = building
    _write_build_allow(allow, ["127.0.0.1/repo/"])
    _build_connect(inj, "127.0.0.1")
    inj.tls_clienthello(_ClientHello("127.0.0.1", client_id="build-1"))
    assert _dial(inj, "127.0.0.1", client_id="build-1") is None
    box = _Flow("127.0.0.1", method="CONNECT", path="")
    box.client_conn = types.SimpleNamespace(id="box-1", sni=None)
    box.response = None
    inj.http_connect(box)
    inj.tls_clienthello(_ClientHello("127.0.0.1", client_id="box-1"))
    assert _dial(inj, "127.0.0.1", client_id="box-1") is not None
