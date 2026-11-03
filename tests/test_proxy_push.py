"""egress_proxy.py refuses a git push over HTTP(S) — defence in depth for the box's "never push"
posture, whatever credential the box might be holding.

Git's smart-HTTP push is ``GET <repo>/info/refs?service=git-receive-pack`` then
``POST <repo>/git-receive-pack``; a fetch is the same with ``git-upload-pack``. So the proxy tells
a push from a fetch by the URL alone, on any host, and answers it with a 403 that names the fix —
unless the host-side live file says a push-capable mode is on (``allow_push``). Drives the REAL
addon over the stubbed mitmproxy of test_proxy_inject."""

from __future__ import annotations

import json
import types

import pytest

import test_proxy_inject as t

gh = t.gh  # the REAL addon over a stubbed mitmproxy
live = t.live
walled = t.walled
_Flow = t._Flow
_Resp = t._Resp
_write_live = t._write_live
_rows = t._rows

PUSH_REFUSED = (
    b"refused by the foldyard proxy - the box doesn't push; push from your computer"
    b" (or turn on a mode that allows it)\n"
)


# ── the matcher ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/acme/app.git/info/refs?service=git-receive-pack",  # the discovery GET
        "/acme/app.git/git-receive-pack",  # the push POST
        "/acme/app/git-receive-pack",  # no .git suffix (GitHub accepts both)
        "/git-receive-pack",  # a repo served at the root
        "/acme/app.git/info/refs?foo=1&service=git-receive-pack",  # not the first parameter
        "/acme/app.git/info/refs?service=git%2Dreceive-pack",  # percent-encoded, as decoded
        "/acme/app.git/git%2Dreceive-pack",  # …in the path too
        "/scm/proj/app.git/git-receive-pack?x=y",  # with a query of its own
        # The refusal is for a box process that writes its own HTTP, not for git, so what counts
        # is what a SERVER still routes to receive-pack (review on #160):
        "/org/proj/_git/app/GIT-RECEIVE-PACK",  # case-insensitive routing (ASP.NET / IIS)
        "/acme/app.git/info/refs?service=GIT-RECEIVE-PACK",
        "/acme/app.git/info/refs?SERVICE=git-receive-pack",
        "/acme/app.git/info/refs?service=%20git-receive-pack%20",
        "/scm/proj/app.git/git-receive-pack;jsessionid=1",  # path parameters (servlet containers)
        "/acme/app.git/info/refs;x=1?service=git-receive-pack",
        "/acme/app.git/info/refs?x=1;service=git-receive-pack",  # `;` as a query separator
        "/acme/app.git/git-receive-pack/",  # trailing-slash matching (Spring MVC before 6)
        "/acme/app.git/git-receive-pack//",
        "/acme/app.git\\git-receive-pack",  # a backslash, which IIS reads as a slash
        "/acme/app.git%2Fgit-receive-pack",  # an encoded slash, as decoded
    ],
)
def test_a_git_push_is_recognised(gh, path):
    assert gh.module._is_git_push(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/acme/app.git/info/refs?service=git-upload-pack",  # a fetch's discovery
        "/acme/app.git/git-upload-pack",  # a fetch
        "/acme/app.git/info/refs",  # dumb-HTTP discovery, no service
        "/acme/git-receive-pack-docs",  # look-alikes: the last segment must BE it
        "/acme/not-git-receive-pack",
        "/acme/git-receive-pack.html",
        "/acme/git-receive-pack/readme",
        "/search?q=git-receive-pack",  # the word in another parameter's value
        "/acme/app.git/info/refs?xservice=git-receive-pack",  # another parameter's name
        "/acme/app.git/info/refs?service=git-receive-pack-x",
        "/acme/git-receive-pack;x/readme",  # a path parameter on a NON-final segment
        "/acme/app.git/GIT-UPLOAD-PACK",  # a fetch, in any spelling
        "/",
        "",
    ],
)
def test_anything_else_is_not_a_push(gh, path):
    assert gh.module._is_git_push(path) is False


def test_a_path_that_parses_as_an_authority_is_still_judged_by_its_end(gh):
    # urlsplit would read `//git-receive-pack` as a netloc with an empty path — a miss. The
    # matcher splits on `?` itself.
    assert gh.module._is_git_push("//git-receive-pack") is True


# ── the hook ────────────────────────────────────────────────────────────────────────────────


def _push(
    host: str = "github.com",
    path: str = "/acme/app.git/git-receive-pack",
    method: str = "POST",
    **kw,
) -> _Flow:
    flow = _Flow(host, method=method, path=path, **kw)
    flow.response = None  # nothing upstream has answered: a response set now is the proxy's own
    flow.request.headers["user-agent"] = "git/2.47.0"
    return flow


def _refused(flow: _Flow) -> bool:
    return (
        flow.response is not None
        and flow.response.status_code == 403
        and flow.response.content == PUSH_REFUSED
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/acme/app.git/info/refs?service=git-receive-pack"),
        ("POST", "/acme/app.git/git-receive-pack"),
    ],
)
async def test_a_push_is_refused_with_its_own_body_and_row(live, method, path):
    inj = live.Injector()
    flow = _push(method=method, path=path)
    await inj.requestheaders(flow)
    assert _refused(flow)
    # text/plain, so git shows it: its smart-HTTP client prints a text/plain error body as
    # `remote: …` lines, and the operator reads why rather than a bare "403".
    assert flow.response is not None
    assert flow.response.headers["content-type"].startswith("text/plain")
    [row] = _rows(live.log)
    assert row["blocked"] is True and row["push"] is True and row["status"] == 403
    assert (row["host"], row["method"], row["path"]) == ("github.com", method, path)
    assert row["ua"] == "git/2.47.0"
    assert "mismatch" not in row  # a push row is its own reason, not a Host-header one


async def test_a_push_is_refused_on_any_host_and_over_plain_http(live):
    inj = live.Injector()
    for flow in (
        _push("gitlab.example.internal", path="/grp/app.git/git-receive-pack"),
        _push("git.example.com", path="/app.git/git-receive-pack", port=80, scheme="http"),
    ):
        await inj.requestheaders(flow)
        assert _refused(flow), flow.request.host


async def test_a_fetch_goes_through(live):
    inj = live.Injector()
    for path in (
        "/acme/app.git/info/refs?service=git-upload-pack",
        "/acme/app.git/git-upload-pack",
    ):
        flow = _push(path=path)
        await inj.requestheaders(flow)
        assert flow.response is None, path  # not answered: it goes upstream
    assert _rows(live.log) == []  # logged on the way back, like any request


async def test_allow_push_lets_a_push_through_and_says_so_on_the_response(live):
    _write_live(live.live, allow_push=True)
    inj = live.Injector()
    flow = _push()
    await inj.requestheaders(flow)
    assert flow.response is None  # forwarded
    # The upstream's answer carries the proxy's word that the push was let through by the mode,
    # so the box can tell "allowed" from "not refused because the proxy can't see it" (fy verify).
    flow.response = _Resp(401)
    inj.responseheaders(flow)
    assert flow.response.headers["x-foldyard-push"] == "allowed by the active mode"
    await inj.response(flow)
    row = _rows(live.log)[-1]
    assert "blocked" not in row and row["status"] == 401


async def test_the_allowed_header_is_only_on_a_push(live):
    _write_live(live.live, allow_push=True)
    inj = live.Injector()
    flow = _push(path="/acme/app.git/git-upload-pack")
    await inj.requestheaders(flow)
    flow.response = _Resp(200)
    inj.responseheaders(flow)
    await inj.response(flow)
    assert "x-foldyard-push" not in flow.response.headers


@pytest.mark.parametrize(
    "live_data",
    [
        {},  # absent: the field predates nothing — absence is "no push-capable mode"
        {"allow_push": False},
        {"allow_push": "true"},  # only a JSON true lifts it
        {"allow_push": 1},
        {"allow_push": None},
    ],
    ids=["absent", "false", "string", "one", "null"],
)
async def test_anything_but_a_literal_true_refuses(live, live_data):
    live.live.write_text(json.dumps({"rules": [], "default_deny": False, **live_data}))
    inj = live.Injector()
    flow = _push()
    await inj.requestheaders(flow)
    assert _refused(flow)


async def test_an_unreadable_live_file_refuses_pushes(live):
    live.live.write_text("{ not json")
    inj = live.Injector()
    flow = _push()
    await inj.requestheaders(flow)
    assert _refused(flow)
    live.live.unlink()
    again = _push()
    again.client_conn = types.SimpleNamespace(id="client-2")
    await inj.requestheaders(again)
    assert _refused(again)


async def test_allow_push_switches_live_both_ways(live):
    inj = live.Injector()
    first = _push()
    await inj.requestheaders(first)
    assert _refused(first)
    _write_live(live.live, allow_push=True)
    second = _push()
    await inj.requestheaders(second)
    assert second.response is None  # the next request, same process: lifted
    _write_live(live.live, allow_push=False)
    third = _push()
    await inj.requestheaders(third)
    assert _refused(third)  # and back, with no restart


async def test_a_push_is_refused_before_the_wall_whether_or_not_it_enforces(live):
    # An ungranted host under an enforcing wall: the push body, not the allowlist's — granting the
    # host would not let a push through, so the row must not read as a grant's to answer.
    _write_live(live.live, default_deny=True)
    inj = live.Injector()
    flow = _push("git.example.com")
    await inj.requestheaders(flow)
    assert _refused(flow)
    [row] = _rows(live.log)
    assert row["push"] is True


async def test_a_push_in_a_tunnel_the_proxy_answered_owes_no_connect_row(live):
    # An ungranted host on :443: http_connect answers the CONNECT itself and the request inside
    # is judged here. The push's own row replaces the CONNECT row the connection owed.
    _write_live(live.live, default_deny=True)
    inj = live.Injector()
    connect = _Flow("git.example.com", method="CONNECT", path="")
    connect.response = None
    inj.http_connect(connect)
    flow = _push("git.example.com")
    await inj.requestheaders(flow)
    assert _refused(flow)
    inj.client_disconnected(flow.client_conn)
    assert [r["method"] for r in _rows(live.log)] == ["POST"]


async def test_with_allow_push_a_push_still_meets_the_wall(live, gh):
    # Lifting the push refusal is not a grant: an ungranted host is refused by the allowlist.
    _write_live(live.live, default_deny=True, allow_push=True)
    inj = live.Injector()
    flow = _push("git.example.com")
    await inj.requestheaders(flow)
    assert flow.response is not None and flow.response.status_code == 403
    assert flow.response.content == gh.module._REFUSED_BODY
    row = _rows(live.log)[-1]
    assert row["blocked"] is True and "push" not in row
    # …and the box still learns the mode let the push past (fy verify reads this).
    await inj.response(flow)
    assert flow.response.headers["x-foldyard-push"] == "allowed by the active mode"


async def test_without_a_live_file_pushes_are_refused_unless_allow_push(gh, monkeypatch, tmp_path):
    # The standalone addon (no LIVE_FILE): refused by default too; ALLOW_PUSH=1 lifts it.
    monkeypatch.setenv("INJECT_HOST", "")
    monkeypatch.setenv("INJECT_COMMAND", "")
    monkeypatch.setenv("PROXY_LOG_FILE", str(tmp_path / "egress.jsonl"))
    monkeypatch.delenv("LIVE_FILE", raising=False)
    flow = _push()
    await gh.Injector().requestheaders(flow)
    assert _refused(flow)
    monkeypatch.setenv("ALLOW_PUSH", "1")
    flow = _push()
    await gh.Injector().requestheaders(flow)
    assert flow.response is None


def test_a_passthrough_host_is_still_tunnelled_blind(live):
    # The limitation, pinned: the proxy can't see a path inside a blind tunnel, so it doesn't try
    # — a passthrough host's tunnel is never broken open to judge pushes (`fy config widenings`
    # says so instead).
    _write_live(live.live, passthrough=["github.com"])
    inj = live.Injector()
    hello = t._ClientHello("github.com")
    inj.tls_clienthello(hello)
    assert hello.ignore_connection is True


def test_a_push_row_is_never_offered_as_a_grant(tmp_path, monkeypatch):
    # The TUI's allow offer and the refused counts skip it, like a Host-header mismatch: granting
    # the host is not the fix.
    from foldyard import allowlist
    from foldyard.plugins import _netlog

    monkeypatch.setattr(allowlist, "live_hosts", lambda: [])
    monkeypatch.setattr(allowlist, "declined", lambda: [])
    row = {"host": "git.example.com", "blocked": True, "push": True, "status": 403}
    assert allowlist.refused_hosts([row]) == []
    host = _netlog._Host()
    host.add(row)
    assert (host.blocked, host.refused) == (1, 0)


def test_the_network_log_says_why_a_push_was_refused():
    from foldyard.plugins import proxy

    row = {"ts": "2026-11-03T10:00:00+00:00", "host": "github.com", "blocked": True, "push": True}
    row |= {"method": "POST", "path": "/acme/app.git/git-receive-pack"}
    line = proxy._net_leaf_body(row, lambda s: s)
    assert "git push" in line and "not on the allowlist" not in line


def test_fy_verify_reads_the_refusal_and_the_allowed_header_the_addon_writes(gh):
    # verify runs in the box and knows the proxy only by what it answers: the two must agree.
    from foldyard import verify

    assert gh.module._PUSH_REFUSED_BODY.decode().startswith(verify._PUSH_REFUSED)
    assert gh.module._PUSH_HEADER == verify._PUSH_ALLOWED_HEADER
