"""egress_proxy.py and the attributing forwarder's stamp (ADR-0034): verified on a connection's
FIRST head, bound to the client connection (so a keep-alive request, a 407 retry and every request
decrypted inside a CONNECT inherit it), stripped from every head before anything goes upstream, and
recorded on the egress log's rows. Drives the REAL addon against the stubbed mitmproxy of
test_proxy_inject; the forwarder's own stamp() makes the stamps, so the two can't drift apart."""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from typing import Any, cast

import pytest

import test_proxy_inject as t

gh = t.gh  # the REAL addon over a stubbed mitmproxy
_Flow = t._Flow
_ClientHello = t._ClientHello

_FWD = Path(__file__).resolve().parents[1] / "src/foldyard/assets/forwarder/fwd.py"
_spec = importlib.util.spec_from_file_location("fy_fwd_for_proxy", _FWD)
assert _spec and _spec.loader
fwd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fwd)

KEY = "ab" * 32
BRIDGE = [("via", "bridge"), ("src", "10.89.0.6:58516"), ("net", "podman1")]


class _Headers:
    """mitmproxy's Headers, as far as the addon uses it: case-insensitive, multi-valued."""

    def __init__(self, items=()) -> None:
        self._items: list[tuple[str, str]] = list(items)

    def get_all(self, name: str) -> list[str]:
        return [v for k, v in self._items if k.lower() == name.lower()]

    def get(self, name: str, default=None):
        values = self.get_all(name)
        return values[0] if values else default

    def __getitem__(self, name: str) -> str:
        values = self.get_all(name)
        if not values:
            raise KeyError(name)
        return values[0]

    def __setitem__(self, name: str, value: str) -> None:
        self._items = [(k, v) for k, v in self._items if k.lower() != name.lower()]
        self._items.append((name, value))

    def __delitem__(self, name: str) -> None:
        if not self.get_all(name):
            raise KeyError(name)
        self._items = [(k, v) for k, v in self._items if k.lower() != name.lower()]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and bool(self.get_all(name))

    def pop(self, name: str, default=None):
        value = self.get(name, default)
        if name in self:
            del self[name]
        return value

    def keys(self):
        return [k for k, _ in self._items]

    def items(self):
        return list(self._items)

    def __iter__(self):
        return iter(self.keys())


def _stamp(items=BRIDGE, key: str = KEY, seq: int = 1) -> str:
    return fwd.stamp(key.encode(), [("v", "1"), *items, ("boot", "b00t"), ("seq", str(seq))])


def _flow(host="example.com", *, client="c1", stamps=(), port=443, scheme="https", method="GET"):
    flow = _Flow(host, method=method, port=port, scheme=scheme)
    flow.client_conn = types.SimpleNamespace(id=client, sni=None)
    request: Any = flow.request  # the stub's headers are a plain dict; these are multi-valued
    request.headers = _Headers([("Host", host)] + [("X-Fy-Origin", s) for s in stamps])
    return flow


def _all_rows(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _rows(log: Path) -> list[dict]:
    """The rows for requests that went out (the wall observes, so each ungranted host also gets a
    `would_block` row — :func:`test_an_observed_refusal_names_its_connection_too`)."""
    return [r for r in _all_rows(log) if not r.get("would_block")]


@pytest.fixture
def origin(gh, monkeypatch, tmp_path):
    """A live-configured Injector whose live settings name a forwarder key (``record`` on), the
    wall observing. ``set(record=…, key_file=…)`` rewrites the live file."""
    paths = types.SimpleNamespace(
        live=tmp_path / "proxy-live.json",
        key=tmp_path / "forwarder.key",
        log=tmp_path / "egress.jsonl",
        allow=tmp_path / "allow-effective.json",
    )
    paths.key.write_text(KEY + "\n")
    t._write_allow(paths.allow, [], default_deny=False)

    def set_live(record: bool | None = True, key_file: Path | None = paths.key) -> None:
        data = {"rules": [], "default_deny": False, "passthrough": ["tunnelled.example"]}
        if record is not None:
            data["origin"] = {"key_file": str(key_file), "record": record}
        paths.live.write_text(json.dumps(data))

    set_live()
    for name, value in (
        ("LIVE_FILE", paths.live),
        ("ALLOW_FILE", paths.allow),
        ("PROXY_LOG_FILE", paths.log),
        ("HOST_ENV_FILE", ""),
        ("DEFAULT_DENY", ""),
    ):
        monkeypatch.setenv(name, str(value))
    paths.set = set_live
    paths.Injector = gh.Injector
    return paths


async def _request(inj, flow) -> None:
    await inj.requestheaders(flow)
    await inj.response(flow)


async def test_a_verified_stamp_is_recorded_and_never_goes_upstream(origin):
    inj = origin.Injector()
    flow = _flow(scheme="http", port=80, stamps=[_stamp()])
    await _request(inj, flow)
    assert "X-Fy-Origin" not in flow.request.headers  # stripped before anything left
    [row] = _rows(origin.log)
    assert row["origin"] == {
        "via": "bridge",
        "src": "10.89.0.6:58516",
        "net": "podman1",
        "boot": "b00t",
        "seq": "1",
    }


async def test_every_later_head_on_the_connection_inherits_the_first(origin):
    # A keep-alive request and a 407 retry arrive unstamped: the forwarder stamps the first head
    # only (spike finding 4). One that DOES carry a stamp wrote it itself: stripped, never trusted.
    inj = origin.Injector()
    first = _flow(scheme="http", port=80, stamps=[_stamp()])
    await _request(inj, first)
    other = _stamp([("via", "bridge"), ("src", "10.89.0.99:1"), ("net", "podman1")], seq=2)
    second = _flow(scheme="http", port=80, stamps=[other])
    await _request(inj, second)
    third = _flow(scheme="http", port=80)
    await _request(inj, third)
    assert "X-Fy-Origin" not in second.request.headers
    srcs = [r["origin"]["src"] for r in _rows(origin.log)]
    assert srcs == ["10.89.0.6:58516"] * 3


async def test_requests_inside_a_connect_inherit_its_stamp(origin):
    inj = origin.Injector()
    connect = _flow("api.example", method="CONNECT", stamps=[_stamp()])
    inj.http_connect(connect)
    assert "X-Fy-Origin" not in connect.request.headers
    inner = _flow("api.example", stamps=["v=1; via=bridge; src=10.0.0.1:1; mac=00"])
    await _request(inj, inner)
    assert "X-Fy-Origin" not in inner.request.headers
    assert _rows(origin.log)[-1]["origin"]["src"] == "10.89.0.6:58516"


async def test_a_passthrough_tunnel_row_names_its_connection(origin):
    inj = origin.Injector()
    connect = _flow("tunnelled.example", method="CONNECT", stamps=[_stamp()])
    inj.http_connect(connect)
    hello = _ClientHello("tunnelled.example", client_id="c1")
    hello.context.server = types.SimpleNamespace(address=("tunnelled.example", 443))
    inj.tls_clienthello(hello)
    assert hello.ignore_connection
    [row] = [r for r in _rows(origin.log) if r.get("passthrough")]
    assert row["origin"]["net"] == "podman1"


@pytest.mark.parametrize(
    "stamps, want",
    [
        ([], "unattributed"),
        (["v=1; via=bridge; src=10.89.0.6:1; mac=" + "0" * 64], "forged"),
        ([_stamp(), _stamp()], "forged"),  # the forwarder writes exactly one
        ([_stamp(key="cd" * 32)], "forged"),  # another project's key
        ([_stamp().replace("v=1", "v=2")], "forged"),  # tampered: the mac no longer matches
        (["garbage"], "forged"),
    ],
)
async def test_a_first_head_that_isnt_one_good_stamp_is_never_trusted(origin, stamps, want):
    inj = origin.Injector()
    flow = _flow(scheme="http", port=80, stamps=stamps)
    await _request(inj, flow)
    assert "X-Fy-Origin" not in flow.request.headers
    assert _rows(origin.log)[-1]["origin"] == want


async def test_with_recording_off_a_kept_key_still_strips_but_marks_nothing_unattributed(origin):
    # Turned on once, then off: a VM still running the forwarder until its restart must never
    # have its stamps forwarded, and a real stamp is still worth keeping; but rows without one
    # aren't all labelled `unattributed` any more.
    origin.set(record=False)
    inj = origin.Injector()
    await _request(inj, plain := _flow("a.example", scheme="http", port=80))
    stamped = _flow("b.example", client="c2", scheme="http", port=80, stamps=[_stamp()])
    await _request(inj, stamped)
    assert (
        "X-Fy-Origin" not in stamped.request.headers and "X-Fy-Origin" not in plain.request.headers
    )
    rows = _rows(origin.log)
    assert "origin" not in rows[0] and rows[1]["origin"]["src"] == "10.89.0.6:58516"


async def test_never_turned_on_nothing_changes(origin):
    origin.set(record=None)  # no `origin` in the live settings: no key was ever made
    inj = origin.Injector()
    flow = _flow(scheme="http", port=80, stamps=[_stamp()])
    await _request(inj, flow)
    assert cast(_Headers, flow.request.headers).get_all("X-Fy-Origin") == [
        _stamp()
    ]  # untouched, as before
    assert "origin" not in _rows(origin.log)[0]


async def test_an_unreadable_key_trusts_no_stamp_and_still_strips(origin, tmp_path):
    origin.set(key_file=tmp_path / "missing.key")
    inj = origin.Injector()
    flow = _flow(scheme="http", port=80, stamps=[_stamp()])
    await _request(inj, flow)
    assert "X-Fy-Origin" not in flow.request.headers
    assert _rows(origin.log)[-1]["origin"] == "forged"


async def test_a_refusal_names_who_asked(origin):
    t._write_allow(origin.allow, [], default_deny=True)
    data = json.loads(origin.live.read_text())
    origin.live.write_text(json.dumps({**data, "default_deny": True}))
    inj = origin.Injector()
    flow = _flow("10.89.1.2", scheme="http", port=8083, stamps=[_stamp()])
    flow.response = None
    await inj.requestheaders(flow)
    assert flow.response is not None and flow.response.status_code == 403
    row = _rows(origin.log)[-1]
    assert row["blocked"] is True and row["origin"]["src"] == "10.89.0.6:58516"


async def test_an_observed_refusal_names_its_connection_too(origin):
    inj = origin.Injector()
    await _request(inj, _flow(scheme="http", port=80, stamps=[_stamp()]))
    [would] = [r for r in _all_rows(origin.log) if r.get("would_block")]
    assert would["origin"]["src"] == "10.89.0.6:58516"


async def test_a_disconnect_drops_the_binding(origin):
    inj = origin.Injector()
    await _request(inj, _flow(scheme="http", port=80, stamps=[_stamp()]))
    inj.client_disconnected(types.SimpleNamespace(id="c1"))
    # a NEW connection that reuses the id (mitmproxy's are uuids; this is the worst case) starts
    # over: its first head decides
    await _request(inj, _flow(scheme="http", port=80))
    assert _rows(origin.log)[-1]["origin"] == "unattributed"


async def test_the_vm_listeners_stamp_verifies_too(origin):
    inj = origin.Injector()
    items = [("via", "vm"), ("src", "192.168.5.15:4"), ("who", "container"), ("ctr", "a" * 64),
             ("uid", "525287")]  # fmt: skip
    await _request(inj, _flow(scheme="http", port=80, stamps=[_stamp(items)]))
    got = _rows(origin.log)[-1]["origin"]
    assert got["who"] == "container" and got["ctr"] == "a" * 64 and got["uid"] == "525287"


def test_a_row_from_no_known_connection_still_says_so_while_recording(origin):
    # A refused dial mitmproxy reports with no client (or one no head was seen on) has no stamp to
    # inherit: while recording it is `unattributed`, said, never silently blank.
    inj = origin.Injector()
    inj._log_blocked("refused.example")
    assert _all_rows(origin.log)[-1]["origin"] == "unattributed"
    origin.set(record=False)
    inj.refresh()
    inj._log_blocked("refused.example")
    assert "origin" not in _all_rows(origin.log)[-1]
