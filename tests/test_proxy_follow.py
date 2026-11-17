"""Follow grants in the egress proxy: a redirect from under one trusted path prefix lets the box
read the URL it names, exactly, for a minute — when one of the grant's targets holds it.

The case they exist for: GitHub's logs and artifacts. ``api.github.com/repos/o/r/actions/…``
answers with a 302 to one of ~200 Azure storage accounts (``productionresultssa<N>``), only some
of which a hand-kept list would name, and ``*.blob.core.windows.net`` would be every storage
account anyone can create. GitHub's own redirect is the only thing that says which account is
GitHub's, so the grant comes from that response, never from a hostname.

ADR-0029 rejected "follow a granted host's redirects": an open redirector on the host lets the
box choose the target. What makes this narrower, each pinned below against the REAL addon over a
stubbed mitmproxy (the wire path is ``test_proxy_wall_e2e.py``'s):

- the source is a PATH prefix the operator vouches for, never a whole host, and only a redirect
  read decrypted from the real upstream over HTTPS counts;
- the target is bounded by the grant's patterns (host or ``*.suffix``, optionally a path), over
  HTTPS on :443, a name and never an address;
- what is let through is the one URL the redirect named — path AND query — read-only (GET/HEAD,
  no body), for ``_VOUCH_SECONDS``, while the rule still stands; a chain follows at most
  ``_VOUCH_HOPS`` links, each under the same rule;
- the target host is reached as a path-granted one is: CONNECT answered in the proxy, decrypted,
  never blind, raw bytes never relayed — and a connection a vouched read went out on stays so
  after the vouch lapses (a download outlives its minute), until its rule is revoked.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

import test_proxy_inject as t
import test_proxy_paths as p
from foldyard import allowlist

gh = t.gh
walled = t.walled
closer = t.closer
fake_minter = t.fake_minter
_Flow = t._Flow
_ClientHello = t._ClientHello
_dial = t._dial
_answered = t._answered
_rows = t._rows

API = "api.github.com"
SOURCE = f"{API}/repos/twistco/foldyard/actions/"
RECEIVER = "results-receiver.actions.githubusercontent.com"
SHARD = "productionresultssa30.blob.core.windows.net"
TARGETS = ["*.blob.core.windows.net/actions-results/", f"{RECEIVER}/rest/runs/"]
LOG_PATH = "/actions-results/eaf9/workflow-job-run-d7b8/logs/job/job-logs.txt"
SAS = "?rsct=text%2Fplain&se=2026-11-16T12%3A00%3A00Z&sig=abc%2Fdef%3D"
LOG_URL = f"https://{SHARD}{LOG_PATH}{SAS}"
JOB_LOGS = "/repos/twistco/foldyard/actions/jobs/133366017478/logs"


def _write(allow, follow: list[dict] | None = None, grants: list[str] | None = None) -> None:
    allow.write_text(
        json.dumps({"default_deny": True, "allow": grants or [], "follow": follow or []}) + "\n"
    )


def _granted(inj, allow, targets: list[str] | None = None, source: str = SOURCE) -> None:
    _write(allow, [{"from": source, "to": targets or TARGETS}])
    inj._allow_mtime = -1.0  # same-second rewrite: force the re-read


def _redirected(inj, location: str, host: str = API, path: str = JOB_LOGS, **kw) -> _Flow:
    """The upstream's 302 to ``location`` for a request to ``host`` + ``path``, seen by the
    proxy as it relays it."""
    flow = p._redirect(host, location, path)
    for k, v in kw.items():
        setattr(flow.request, k, v)
    inj.responseheaders(flow)
    return flow


def _read(host: str = SHARD, path: str = LOG_PATH + SAS, **kw) -> _Flow:
    return p._request(host, path, **kw)


# ── parsing: the proxy and foldyard agree on what a follow target is ───────────────────────────

TARGET_KEYS = [
    "*.blob.core.windows.net/actions-results/",
    "*.blob.core.windows.net",
    f"{RECEIVER}/rest/runs/",
    "Storage.GoogleAPIs.com/chrome-for-testing-public/",
    "a-b.example.com/x",
]
NOT_TARGET_KEYS = [
    "*.com",  # one label: every .com host
    "*",
    "blob.*.windows.net",
    f"{SHARD}:443",
    f"{SHARD}/",  # write the host alone for any path
    f"{SHARD}/a/../b/",
    f"{SHARD}/a%2fb/",
    f"{SHARD}/a?b",
    f"https://{SHARD}/x/",
    "under_score.example.com",
    "20.209.112.225",  # an address is a grant of its own
    "*.0.0.1",  # …and a glob over addresses: it would hold 127.0.0.1
    "*.112.225/actions-results/",
    "",
]


@pytest.mark.parametrize("key", TARGET_KEYS)
def test_a_follow_target_parses_the_same_in_the_proxy_and_in_foldyard(gh, key):
    ours = allowlist.follow_target(key)
    assert ours is not None, key
    host, sep, rest = ours.partition("/")
    assert gh.module._follow_target(ours) == (host, f"/{rest}" if sep else "")
    assert gh.module._follow_target(key.lower()) == gh.module._follow_target(ours)


@pytest.mark.parametrize("key", NOT_TARGET_KEYS)
def test_what_is_not_a_follow_target_is_refused_by_both(gh, key):
    assert allowlist.follow_target(key) is None, key
    assert gh.module._follow_target(key) is None, key


def test_a_follow_source_is_a_path_never_a_whole_host(gh):
    # An operator vouches for one endpoint's redirects, never for every redirect a host can send:
    # a whole-host source is what ADR-0029 rejected.
    rules = gh.module._follow_rules(
        [
            {"from": API, "to": TARGETS},
            {"from": f"*.github.com{SOURCE[len(API) :]}", "to": TARGETS},
            {"from": SOURCE, "to": ["*.com", "nope"]},  # no target survives: follows nothing
            {"from": SOURCE, "to": "*.blob.core.windows.net"},  # not a list
            "junk",
            {"from": SOURCE, "to": [*TARGETS, "*.com"]},  # the bad target is dropped alone
        ]
    )
    assert [(r[0], [h + pre for h, pre in r[3]]) for r in rules] == [(SOURCE, TARGETS)]
    assert allowlist.follow_source(API) is None
    assert allowlist.follow_source(SOURCE) == SOURCE
    assert gh.module._follow_rules("junk") == ()


# ── what a follow grant lets through ───────────────────────────────────────────────────────────


async def test_the_exact_url_a_redirect_named_is_read_and_nothing_else(walled):
    inj, allow, log = walled
    _granted(inj, allow)
    _redirected(inj, LOG_URL)

    assert await p._judged(inj, _read())
    assert await p._judged(inj, _read(method="HEAD"))
    assert not any(r.get("blocked") for r in _rows(log))

    for refused in (
        _read(path=LOG_PATH),  # the query is part of what was named
        _read(path=LOG_PATH + SAS + "&comp=list"),
        _read(path="/actions-results/other/blob.txt" + SAS),
        _read(host="productionresultssa31.blob.core.windows.net"),  # another shard
        _read(method="PUT"),
        _read(method="POST"),
    ):
        assert not await p._judged(inj, refused), (refused.request.host, refused.request.path)
        assert refused.response is not None and refused.response.status_code == 403


async def test_a_vouched_read_carries_no_body_or_upgrade(walled):
    inj, allow, _log = walled
    _granted(inj, allow)
    _redirected(inj, LOG_URL)
    for headers in ({"content-length": "7"}, {"transfer-encoding": "chunked"}, {"upgrade": "h2c"}):
        flow = _read()
        flow.request.headers.update(headers)
        assert not await p._judged(inj, flow), headers
    flow = _read()
    flow.request.raw_content = b"data"
    assert not await p._judged(inj, flow)


async def test_nothing_is_vouched_without_a_follow_grant_or_outside_its_source(walled):
    inj, allow, _log = walled
    _redirected(inj, LOG_URL)  # no follow grant at all
    assert not await p._judged(inj, _read())

    _granted(inj, allow)
    # The same host, outside the vouched prefix: where an open redirector would live.
    _redirected(inj, LOG_URL, path="/login/oauth?return_to=" + LOG_URL)
    _redirected(inj, LOG_URL, path="/repos/twistco/other/actions/jobs/1/logs")
    _redirected(inj, LOG_URL, host="github.com", path=JOB_LOGS)
    # A path the proxy can't judge safely never matches the prefix.
    _redirected(inj, LOG_URL, path="/repos/twistco/foldyard/actions/../../../redirect")
    assert not await p._judged(inj, _read())


async def test_a_redirect_outside_the_targets_is_not_followed(walled):
    inj, allow, log = walled
    _granted(inj, allow)
    for location in (
        "https://evil.example.net/actions-results/x",  # outside every target's host
        f"https://{SHARD}/other-container/x",  # the right host, outside the target's path
        f"http://{SHARD}{LOG_PATH}",  # cleartext
        f"https://{SHARD}:8443{LOG_PATH}",  # another port
        f"https://user:pw@{SHARD}{LOG_PATH}",  # credentials in the URL
        "https://20.209.112.225/actions-results/x",  # an address, whatever the pattern says
        f"https://{SHARD}/actions-results/a/../../x",  # a path the proxy can't judge
    ):
        _redirected(inj, location)
    assert inj._vouched == {}
    refused = _read(path="/other-container/x")
    assert not await p._judged(inj, refused)
    assert refused.response is not None
    assert f"{API} redirected here" in refused.response.content.decode()  # still explained
    assert _rows(log)[-1]["via"] == API


def test_a_redirect_to_an_address_is_never_followed(walled):
    # No target pattern can hold an address (the parsers refuse one), and a vouch refuses an
    # address again whatever the pattern: a redirect to your computer's network is never followed.
    inj, allow, _log = walled
    _granted(inj, allow)
    inj._follows = ((SOURCE, "api.github.com", SOURCE[len(API) :], (("*.0.0.1", ""),)),)
    inj._allow_mtime = allow.stat().st_mtime  # keep that rule: no re-read
    _redirected(inj, "https://127.0.0.1/actions-results/x")
    assert inj._vouched == {}


async def test_only_a_redirect_read_over_https_vouches(walled):
    inj, allow, _log = walled
    _granted(inj, allow)
    _redirected(inj, LOG_URL, scheme="http", port=80)  # anyone on the path could write it
    _redirected(inj, LOG_URL, port=8443)
    assert inj._vouched == {}
    flow = _Flow(API, status=200, path=JOB_LOGS)  # not a redirect
    assert flow.response is not None
    flow.response.headers["location"] = LOG_URL
    inj.responseheaders(flow)
    assert inj._vouched == {}


async def test_a_relative_redirect_resolves_against_the_source(walled):
    inj, allow, _log = walled
    _granted(inj, allow, targets=[f"{API}/repos/twistco/foldyard/actions/artifacts/"])
    _redirected(inj, "/repos/twistco/foldyard/actions/artifacts/9/zip?x=1")
    assert (API, "/repos/twistco/foldyard/actions/artifacts/9/zip?x=1") in inj._vouched


async def test_a_vouch_lapses_and_dies_with_its_rule(gh, walled, monkeypatch):
    inj, allow, _log = walled
    clock = [1000.0]
    monkeypatch.setattr(gh.module.time, "monotonic", lambda: clock[0])
    _granted(inj, allow)
    _redirected(inj, LOG_URL)
    clock[0] += gh.module._VOUCH_SECONDS - 1
    assert await p._judged(inj, _read())
    clock[0] += 2
    assert not await p._judged(inj, _read())

    _redirected(inj, LOG_URL)
    _write(allow, [])
    inj._allow_mtime = -1.0
    assert not await p._judged(inj, _read())  # revoked: what it vouched for goes with it


async def test_a_chain_follows_under_its_rule_up_to_the_hop_limit(gh, walled):
    inj, allow, _log = walled
    _granted(inj, allow)
    # api.github.com → results-receiver → a blob shard: the run's whole log, as gh fetches it.
    run_logs = "/rest/runs/eaf9/logs"
    _redirected(inj, f"https://{RECEIVER}{run_logs}")
    hop = _read(host=RECEIVER, path=run_logs)
    assert await p._judged(inj, hop)
    hop.response = t._Resp(302)
    hop.response.headers["location"] = LOG_URL
    inj.responseheaders(hop)
    assert await p._judged(inj, _read())
    assert inj._vouched[(SHARD, LOG_PATH + SAS)][1:3] == (RECEIVER, 2)

    # The receiver is no source of its own: a redirect it sends unvouched-for is not followed.
    stray = p._redirect(RECEIVER, f"https://{SHARD}/actions-results/stray", "/rest/runs/x")
    inj.responseheaders(stray)
    assert (SHARD, "/actions-results/stray") not in inj._vouched

    # A chain stops at _VOUCH_HOPS links, however many the upstreams send.
    _granted(inj, allow, targets=["*.blob.core.windows.net"])
    flow = None
    for n in range(gh.module._VOUCH_HOPS + 1):
        url = f"https://{SHARD}/hop{n}"
        if flow is None:
            _redirected(inj, url)
        else:
            flow.response = t._Resp(302)
            flow.response.headers["location"] = url
            inj.responseheaders(flow)
        flow = _read(path=f"/hop{n}")
        if n < gh.module._VOUCH_HOPS:
            assert await p._judged(inj, flow), n
        else:
            assert not await p._judged(inj, flow)


def test_the_vouch_table_is_bounded(gh, walled):
    inj, allow, _log = walled
    _granted(inj, allow)
    for i in range(gh.module._VOUCH_MAX + 50):
        _redirected(inj, f"https://{SHARD}/actions-results/{i}")
    assert len(inj._vouched) == gh.module._VOUCH_MAX
    assert (SHARD, f"/actions-results/{gh.module._VOUCH_MAX + 49}") in inj._vouched


async def test_the_network_log_says_which_redirect_was_followed(walled):
    from foldyard.plugins import proxy

    inj, allow, log = walled
    _granted(inj, allow)
    _redirected(inj, LOG_URL)
    flow = _read()
    assert await p._judged(inj, flow)
    flow.response = t._Resp(200)
    inj._log_flow(flow)
    row = _rows(log)[-1]
    assert row["followed"] == API and row["host"] == SHARD and not row.get("blocked")
    line = proxy._net_leaf_body(row, lambda s: s)
    assert f"followed from {API}" in line


# ── the connection: reached as a path-granted host is ─────────────────────────────────────────


def test_the_target_is_answered_decrypted_and_dialled_only_once_earned(walled):
    inj, allow, _log = walled
    _granted(inj, allow)
    connect = p._connect(inj, SHARD)
    assert _answered(inj, connect)  # no vouch yet: walled like any refused host, rows owed
    assert inj._answered["client-1"] is not None
    inj.client_disconnected(types.SimpleNamespace(id="client-1"))

    _redirected(inj, LOG_URL)
    inj.passthrough_hosts = ["*.blob.core.windows.net"]
    connect = p._connect(inj, SHARD)
    assert _answered(inj, connect)  # nothing dialled before a ClientHello
    assert inj._answered["client-1"] is None  # and no refusal owed: it may be read
    hello = _ClientHello(SHARD)
    inj.tls_clienthello(hello)
    assert hello.ignore_connection is False  # never blind, passthrough or not
    assert _dial(inj, SHARD) is None
    assert _dial(inj, SHARD, client_id="client-2") is not None


async def test_raw_bytes_to_the_target_are_never_relayed_even_after_the_vouch(
    gh, walled, monkeypatch
):
    inj, allow, log = walled
    clock = [1000.0]
    monkeypatch.setattr(gh.module.time, "monotonic", lambda: clock[0])
    _granted(inj, allow)
    _redirected(inj, LOG_URL)
    p._connect(inj, SHARD)
    inj.tls_clienthello(_ClientHello(SHARD))
    clock[0] += gh.module._VOUCH_SECONDS + 1  # the vouch lapsed, the dialled connection didn't
    message = types.SimpleNamespace(content=b"\r\nPUT /actions-results/x HTTP/1.1\r\n\r\n")
    inj.tcp_message(
        types.SimpleNamespace(
            server_conn=types.SimpleNamespace(address=(SHARD, 443)),
            client_conn=types.SimpleNamespace(id="client-1"),
            messages=[message],
            metadata={},
        )
    )
    assert message.content == b""
    assert _rows(log)[-1]["method"] == "TCP" and _rows(log)[-1]["blocked"]


async def test_a_download_outlives_its_vouch_but_not_its_rule(gh, walled, monkeypatch):
    inj, allow, _log = walled
    clock = [1000.0]
    monkeypatch.setattr(gh.module.time, "monotonic", lambda: clock[0])
    _granted(inj, allow)
    _redirected(inj, LOG_URL)
    p._connect(inj, SHARD)
    inj.tls_clienthello(_ClientHello(SHARD))
    assert await p._judged(inj, _read())
    clock[0] += gh.module._VOUCH_SECONDS * 10

    # An unrelated grant changes: the sweep re-judges every open connection, and keeps this one.
    _write(allow, [{"from": SOURCE, "to": TARGETS}], grants=["pypi.org"])
    inj._allow_mtime = -1.0
    inj.refresh()
    assert "client-1" in inj._conns and _dial(inj, SHARD) is None
    assert not await p._judged(inj, _read())  # but nothing more is read on it

    # The follow grant is revoked: the connection goes with it.
    _write(allow, [], grants=["pypi.org"])
    inj._allow_mtime = -1.0
    inj.refresh()
    assert "client-1" not in inj._conns
    assert _dial(inj, SHARD) is not None


async def test_a_host_granted_as_a_whole_is_not_narrowed_by_a_follow(walled):
    inj, allow, _log = walled
    _write(allow, [{"from": SOURCE, "to": TARGETS}], grants=[SHARD])
    _redirected(inj, LOG_URL)
    assert not inj._path_only(SHARD)
    assert await p._judged(inj, _read(path="/anything/else", method="PUT"))


def _client(flow: _Flow, client_id: str) -> _Flow:
    flow.client_conn = types.SimpleNamespace(id=client_id)
    return flow


async def _download(inj, closer, client_id: str, path: str = LOG_PATH + SAS) -> None:
    """A vouched read on its own decrypted connection to the shard, which ``closer`` can end."""
    closer.track(client_id)
    p._connect(inj, SHARD, client_id=client_id)
    inj.tls_clienthello(_ClientHello(SHARD, client_id=client_id))
    assert await p._judged(inj, _client(_read(path=path), client_id))


OTHER_SOURCE = f"{API}/repos/twistco/other/actions/"
OTHER_PATH = "/actions-results/0bb1/workflow-job-run-a4c2/logs/job/job-logs.txt"


async def test_revoking_one_follow_grant_ends_its_downloads_and_not_anothers(walled, closer):
    # Two grants vouch URLs on one shard: each connection is judged by the grant that vouched for
    # what it read, never by whatever else still reaches the host.
    inj, allow, _log = walled
    both = [{"from": SOURCE, "to": TARGETS}, {"from": OTHER_SOURCE, "to": TARGETS}]
    _write(allow, both)
    inj._allow_mtime = -1.0
    _redirected(inj, LOG_URL)
    _redirected(inj, f"https://{SHARD}{OTHER_PATH}{SAS}", path=f"{OTHER_SOURCE[len(API) :]}x/logs")
    await _download(inj, closer, "client-1")
    await _download(inj, closer, "client-2", OTHER_PATH + SAS)

    _write(allow, both[1:])
    inj._allow_mtime = -1.0
    inj.refresh()
    assert closer.closed == ["client-1"]

    _write(allow, [])
    inj._allow_mtime = -1.0
    inj.refresh()
    assert closer.closed == ["client-1", "client-2"]  # each closed once, whichever loop saw it


async def test_a_connection_answered_before_its_vouch_still_ends_with_its_rule(
    gh, walled, closer, monkeypatch
):
    # Its CONNECT came before the redirect, so it was answered as a refused host's and the sweep
    # has no CONNECT of it to re-judge: what it read is what ties it to its rule.
    inj, allow, _log = walled
    clock = [1000.0]
    monkeypatch.setattr(gh.module.time, "monotonic", lambda: clock[0])
    _granted(inj, allow)
    closer.track("client-1")
    p._connect(inj, SHARD)
    inj.tls_clienthello(_ClientHello(SHARD))
    assert "client-1" not in inj._conns
    _redirected(inj, LOG_URL)
    assert await p._judged(inj, _read())
    clock[0] += gh.module._VOUCH_SECONDS * 10  # the vouch lapsed; its rule stands

    _write(allow, [{"from": SOURCE, "to": TARGETS}], grants=["pypi.org"])
    inj._allow_mtime = -1.0
    inj.refresh()
    assert closer.closed == []  # a download outlives its vouch

    _write(allow, [], grants=["pypi.org"])
    inj._allow_mtime = -1.0
    inj.refresh()
    assert closer.closed == ["client-1"]


async def test_a_vouch_ends_when_its_rule_no_longer_targets_it(walled, closer):
    # The source stays (its key with it) while a target goes — a `once` target lapsing beside
    # the source's others: what that target vouched for goes with it.
    inj, allow, _log = walled
    _granted(inj, allow)
    _redirected(inj, LOG_URL)
    await _download(inj, closer, "client-1")

    _granted(inj, allow, targets=[f"{RECEIVER}/rest/runs/"])
    inj.refresh()
    assert closer.closed == ["client-1"]
    assert not inj._path_only(SHARD)
    assert not await p._judged(inj, _client(_read(), "client-2"))

    # The host still matches, the path no longer does: the URL is no longer held.
    _granted(inj, allow)
    _redirected(inj, LOG_URL)
    _granted(inj, allow, targets=["*.blob.core.windows.net/other-container/"])
    assert not await p._judged(inj, _client(_read(), "client-3"))


@pytest.fixture
def injected(gh, monkeypatch, tmp_path, fake_minter):
    """The wall on, with a credential injected on the source host (whose 401 is re-issued)."""
    log = tmp_path / "egress.jsonl"
    allow = tmp_path / "allow-effective.json"
    _write(allow, [])
    monkeypatch.setenv("INJECT_HOST", API)
    monkeypatch.setenv("INJECT_COMMAND", fake_minter.command)
    monkeypatch.setenv("DEFAULT_DENY", "1")
    monkeypatch.setenv("ALLOW_FILE", str(allow))
    monkeypatch.setenv("PROXY_LOG_FILE", str(log))
    return gh.Injector(), allow, log


async def test_a_redirect_a_401_re_issue_brought_back_is_followed(injected, monkeypatch):
    # The 401 was what the response headers showed; the re-issue's redirect is what the box gets.
    inj, allow, _log = injected
    _granted(inj, allow)

    class _Session:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, **kw):
            return types.SimpleNamespace(
                status_code=302, content=b"", headers={"location": LOG_URL}
            )

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(Session=_Session))
    flow = _Flow(API, status=401, path=JOB_LOGS)
    flow.request.headers["Authorization"] = "Bearer DUMMY"
    inj.responseheaders(flow)
    assert inj._vouched == {}
    await inj.response(flow)
    assert flow.metadata.get("egress_proxy_retried")
    assert flow.response is not None and flow.response.status_code == 302
    assert (SHARD, LOG_PATH + SAS) in inj._vouched
    assert await p._judged(inj, _client(_read(), "client-2"))


async def test_a_chain_carries_on_through_a_hop_granted_on_its_own(walled):
    # The receiver is granted as a whole: its request needs no vouch, but it is still the link
    # its own redirect is judged under.
    inj, allow, _log = walled
    _write(allow, [{"from": SOURCE, "to": TARGETS}], grants=[RECEIVER])
    inj._allow_mtime = -1.0
    run_logs = "/rest/runs/eaf9/logs"
    _redirected(inj, f"https://{RECEIVER}{run_logs}")
    hop = _read(host=RECEIVER, path=run_logs)
    assert await p._judged(inj, hop)
    # Admitted by its own grant: nothing of a vouched read's handling, only the link.
    assert not hop.metadata.get("egress_proxy_path_read")
    assert inj._vouch_conns == {} and ("client-1", RECEIVER) not in inj._path_dials
    hop.response = t._Resp(302)
    hop.response.headers["location"] = LOG_URL
    inj.responseheaders(hop)
    assert inj._vouched[(SHARD, LOG_PATH + SAS)][1:3] == (RECEIVER, 2)
    assert await p._judged(inj, _client(_read(), "client-2"))
