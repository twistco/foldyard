"""assets/forwarder/fwd.py — the attributing forwarder (ADR-0034), its parts that run anywhere: the
stamp, the head rewrite, the cgroup classification, the sock_diag codec, the rendered nft tables and
policy routes, and the relay itself between loopback sockets. Following a real rootless namespace,
the policy routes under the VM firewall and the OCI hook's wait are the host-tier e2e's
(tests/test_forwarder_e2e.py)."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import importlib.util
import socket
import struct
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "src/foldyard/assets/forwarder/fwd.py"
_spec = importlib.util.spec_from_file_location("fy_fwd", _PATH)
assert _spec and _spec.loader
fwd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fwd)

KEY = b"ab" * 32
UID = 501
BOX = "a" * 64
USER = f"/user.slice/user-{UID}.slice/user@{UID}.service"


# ── the stamp and the head ─────────────────────────────────────────────────────────────


def test_a_stamp_is_fields_then_an_hmac_over_exactly_them():
    value = fwd.stamp(KEY, [("v", "1"), ("via", "bridge"), ("src", "10.89.0.6:5851")])
    fields, _, mac = value.rpartition("; mac=")
    assert fields == "v=1; via=bridge; src=10.89.0.6:5851"
    assert mac == hmac.new(KEY, fields.encode(), hashlib.sha256).hexdigest()


@pytest.mark.parametrize("bad", ["a;b", "a b", "", "x\r\ny"])
def test_a_field_that_could_break_the_header_is_never_stamped(bad):
    with pytest.raises(ValueError):
        fwd.stamp(KEY, [("net", bad)])


def test_restamp_drops_every_client_stamp_and_appends_ours_last():
    head = (
        b"CONNECT api.example:443 HTTP/1.1\r\nHost: api.example:443\r\n"
        b"X-Fy-Origin: v=1; mac=deadbeef\r\nx-fy-origin:forged\r\n"
        b"Proxy-Authorization: Basic Zg==\r\n\r\n"
    )
    out, stripped = fwd.restamp(head, "OURS")
    assert stripped == 2
    assert out == (
        b"CONNECT api.example:443 HTTP/1.1\r\nHost: api.example:443\r\n"
        b"Proxy-Authorization: Basic Zg==\r\nX-Fy-Origin: OURS\r\n\r\n"
    )


def test_only_an_http1_request_head_is_stamped():
    assert fwd.is_request_head(b"GET http://x/ HTTP/1.1\r\nHost: x\r\n\r\n")
    assert fwd.is_request_head(b"CONNECT x:443 HTTP/1.0\r\n\r\n")
    # HTTP/2's connection preface would be corrupted by a header line
    assert not fwd.is_request_head(b"PRI * HTTP/2.0\r\n\r\n")
    assert not fwd.is_request_head(b"\x16\x03\x01junk\r\n\r\n")


# ── who owns a socket ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,want",
    [
        (f"{USER}/user.slice/libpod-{BOX}.scope/container", ("container", BOX)),
        # the OUTERMOST podman scope wins: a nested one is the client's to name (ADR-0032)
        (f"{USER}/user.slice/libpod-{BOX}.scope/libpod-{'b' * 64}.scope", ("container", BOX)),
        (f"{USER}/app.slice/crun-buildah-buildah12.scope/container", ("build", "")),
        # a build through the engine API (the host's `fy up`, the box's): buildah's own cgroup,
        # no crun scope (seen live on CI's Linux host and again by hand, 2026-10-23)
        (f"{USER}/app.slice/buildah-buildah921013546", ("build", "")),
        (f"{USER}/app.slice/buildah-buildah921013546/x", ("build", "")),
        (f"{USER}/app.slice/buildah-somethingelse", ("vm", "")),
        (f"{USER}/user.slice/podman-1234.scope", ("vm", "")),
        (f"/user.slice/user-{UID}.slice/session-3.scope", ("vm", "")),
        # outside the VM user's delegated subtree nothing is a container, whatever it is named
        (f"/system.slice/libpod-{BOX}.scope", ("vm", "")),
        (f"{USER}/user.slice/libpod-{'A' * 64}.scope", ("vm", "")),  # not podman's own spelling
    ],
)
def test_a_sockets_cgroup_says_whose_it_is(path, want):
    assert fwd.classify(path, UID) == want


def _diag_reply(uid: int, inode: int, cgroup: int | None) -> bytes:
    body = struct.pack("=BBBB", 2, 1, 0, 0) + bytes(48) + struct.pack("=IIIII", 0, 0, 0, uid, inode)
    attrs = b""
    if cgroup is not None:
        attrs = struct.pack("=HH", 12, fwd.INET_DIAG_CGROUP_ID) + struct.pack("=Q", cgroup)
    attrs = struct.pack("=HH", 8, 99) + b"\0\0\0\0" + attrs  # an attribute it skips
    msg = body + attrs
    return struct.pack("=IHHII", 16 + len(msg), fwd.SOCK_DIAG_BY_FAMILY, 0, 1, 0) + msg


def test_the_sock_diag_reply_gives_uid_inode_and_cgroup():
    assert fwd.parse_diag(_diag_reply(525287, 4242, 9001)) == (525287, 4242, 9001)
    assert fwd.parse_diag(_diag_reply(501, 7, None)) == (501, 7, None)
    err = struct.pack("=IHHII", 20, fwd.NLMSG_ERROR, 0, 1, 0) + struct.pack("=i", -2)
    assert fwd.parse_diag(err) is None
    assert fwd.parse_diag(b"") is None


def test_the_sock_diag_request_names_the_clients_own_socket():
    req = fwd.diag_request(("192.168.5.15", 41930), ("192.168.5.2", 41000))
    length, kind, flags = struct.unpack_from("=IHH", req)
    assert (length, kind, flags) == (len(req), fwd.SOCK_DIAG_BY_FAMILY, 1)  # exact, not a dump
    family, proto = struct.unpack_from("=BB", req, 16)
    assert (family, proto) == (socket.AF_INET, socket.IPPROTO_TCP)
    sport, dport = struct.unpack_from("!HH", req, 24)
    assert (sport, dport) == (41930, 41000)  # local = the client, remote = the daemon port
    assert req[28:32] == socket.inet_aton("192.168.5.15")
    assert req[44:48] == socket.inet_aton("192.168.5.2")


class _Cgroups(fwd.Cgroups):
    def __init__(self, paths: dict[int, str], comm: str | None) -> None:
        super().__init__("/nonexistent")
        self.paths = paths
        self.comm = comm

    def _walk(self) -> None:
        pass

    def owner_comm(self, path, inode, uids, proc="/proc"):
        return self.comm


def _owner(cgroup_path: str | None, comm: str | None, found=(501, 77, 5)):
    cg = _Cgroups({5: cgroup_path} if cgroup_path else {}, comm)
    return fwd.owner(
        ("192.168.5.15", 1), ("192.168.5.2", 41000), UID, [(501, 501)], cg, lambda a, b: found
    )


def test_owner_names_a_container_build_or_the_vm_and_refuses_pasta_or_the_unknown():
    assert _owner(f"{USER}/user.slice/libpod-{BOX}.scope/container", None) == (
        "container",
        BOX,
        501,
    )
    assert _owner(f"{USER}/app.slice/crun-buildah-buildah1.scope", None) == ("build", "", 501)
    assert _owner(f"{USER}/user.slice/podman.service", "podman") == ("vm", "", 501)
    # some container's traffic re-made by a pasta helper: whose can't be told, so it's refused
    assert _owner(f"{USER}/user.slice/rootless-netns-1.scope", "pasta") == ("pasta", "", 501)
    assert _owner(f"{USER}/user.slice/podman.service", None) is None  # no holder found
    assert _owner(None, None) is None  # a cgroup that can't be found
    assert _owner(f"{USER}/x", None, found=None) is None  # no such socket
    assert _owner(f"{USER}/x", None, found=(501, 77, None)) is None  # no cgroup attribute


def test_owner_comm_finds_a_socket_moved_out_of_its_cgroup(tmp_path):
    # pasta opens a pool of sockets BEFORE podman moves it into its scope: the socket names a
    # cgroup the process has left, so the walled uids' processes are searched too (live finding).
    proc = tmp_path / "proc"
    for pid, comm, sock in (("100", "podman", "socket:[1]"), ("200", "pasta", "socket:[77]")):
        (proc / pid / "fd").mkdir(parents=True)
        (proc / pid / "comm").write_text(comm + "\n")
        (proc / pid / "fd" / "3").symlink_to(sock)
    root = tmp_path / "cg"
    (root / "old").mkdir(parents=True)
    (root / "old" / "cgroup.procs").write_text("100\n")
    me = (proc / "200").stat().st_uid
    cg = fwd.Cgroups(str(root))
    assert cg.owner_comm("/old", 77, [(me, me)], proc=str(proc)) == "pasta"
    assert cg.owner_comm("/old", 1, [(me, me)], proc=str(proc)) == "podman"
    assert cg.owner_comm("/old", 77, [(me + 1, me + 1)], proc=str(proc)) is None


def test_a_cgroup_id_is_its_directorys_inode(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    cg = fwd.Cgroups(str(tmp_path))
    assert cg.path((tmp_path / "a" / "b").stat().st_ino) == "/a/b"
    assert cg.path(1) is None


# ── what it installs ───────────────────────────────────────────────────────────────────


def test_ports_parse_from_the_firewalls_spelling():
    assert fwd.parse_ports("41000-41089, 41100-41189") == [(41000, 41089), (41100, 41189)]
    assert fwd.parse_ports("41000") == [(41000, 41000)]
    assert fwd.in_ranges(41189, [(41000, 41089), (41100, 41189)])
    assert not fwd.in_ranges(41090, [(41000, 41089), (41100, 41189)])
    assert not fwd.in_ranges(None, [(1, 2)])
    with pytest.raises(ValueError):
        fwd.parse_ports("9-1")


def test_both_tables_replace_themselves_whole():
    # a restart must never pile up duplicate rules (spike finding 10)
    for text in (
        fwd.bridge_nft("192.168.5.2", [(41000, 41089)], 4748),
        fwd.vm_nft("192.168.5.2", [(41000, 41089)], "501, 524288-1074266111", "127.42.0.1"),
    ):
        assert text.startswith("table ip fy_fwd\ndelete table ip fy_fwd\ntable ip fy_fwd {")
        assert "{{" not in text and "}}" not in text  # Lima renders the boot script as a template


def test_the_vm_table_redirects_and_guards_only_the_walled_principals():
    text = fwd.vm_nft("192.168.5.2", [(41000, 41089), (41100, 41189)], "501, 5-9", "127.42.0.1")
    match = "meta skuid { 501, 5-9 } ip daddr 192.168.5.2 tcp dport { 41000-41089, 41100-41189 }"
    # the port is KEPT: one listener per daemon port. Every port DNATed onto one listener made a
    # client port reused toward another daemon port clash with the earlier connection's
    # conntrack entry, conntrack remapped the source port, and sock_diag could no longer find the
    # client's socket — about a third of connections refused at ~1,400/s (seen live, 2026-10-23)
    assert f"{match} dnat to 127.42.0.1\n" in text
    # anything of theirs still naming a daemon port after the redirect is refused: never around
    assert f"{match} counter reject with tcp reset" in text
    assert "type filter hook output" in text


def test_the_vm_listener_takes_only_what_the_table_redirected():
    ranges = [(41000, 41089)]
    gw = "192.168.5.2"
    assert fwd.vm_dest_ok((gw, 41005), ("127.42.0.1", 41005), ranges, gw)
    assert not fwd.vm_dest_ok(None, ("127.42.0.1", 41005), ranges, gw)
    assert not fwd.vm_dest_ok(("127.42.0.1", 41005), ("127.42.0.1", 41005), ranges, gw)  # direct
    assert not fwd.vm_dest_ok((gw, 22), ("127.42.0.1", 22), ranges, gw)  # no rule names it
    assert not fwd.vm_dest_ok((gw, 41005), ("127.42.0.1", 41006), ranges, gw)  # another port's


def test_the_vm_listener_listens_on_every_daemon_port():
    assert fwd.each_port([(41000, 41002), (41100, 41100)]) == [41000, 41001, 41002, 41100]


def test_the_policy_routes_cover_each_uid_range_and_port_span():
    cmds = fwd.vm_route_cmds("192.168.5.2", [(41000, 41089), (41100, 41189)], ["501-501", "5-9"])
    assert cmds[0] == ["ip", "route", "replace", "192.168.5.2/32", "dev", "lo", "table", "4747"]
    rules = [c for c in cmds if c[1] == "rule"]
    assert len(rules) == 4
    assert [r[r.index("priority") + 1] for r in rules] == ["4747", "4748", "4749", "4750"]
    assert ["uidrange", "5-9", "to", "192.168.5.2"] == rules[3][5:9]
    assert rules[3][-4:] == ["dport", "41100-41189", "lookup", "4747"]
    assert fwd.uid_ranges("501, 524288-1074266111") == ["501-501", "524288-1074266111"]


# ── the relay, between loopback sockets ────────────────────────────────────────────────


def _args(proxy: int, plain: int = 0) -> argparse.Namespace:
    return fwd.parse_args(
        ["--uid", "501", "--walled", "501", "--gw", "127.0.0.1", "--proxy-ports", str(proxy)]
        + (["--plain-ports", str(plain)] if plain else [])
    )


async def _round_trip(port_kind: str, payload: bytes) -> bytes:
    """Upstream records what it is sent; the forwarder relays a client's bytes to it."""
    got = bytearray()
    done = asyncio.Event()

    async def upstream(r, w):
        while data := await r.read(65536):
            got.extend(data)
            if b"END" in got:
                break
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await w.drain()
        w.close()
        done.set()

    up = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = up.sockets[0].getsockname()[1]
    a = _args(port) if port_kind == "proxy" else _args(1, plain=port)
    f = fwd.Forwarder(a, KEY, "b00t")

    async def front(r, w):
        await f.relay(r, w, ("127.0.0.1", port), [("via", "bridge"), ("src", "10.89.0.6:1")])

    fr = await asyncio.start_server(front, "127.0.0.1", 0)
    cr, cw = await asyncio.open_connection("127.0.0.1", fr.sockets[0].getsockname()[1])
    cw.write(payload)
    await cw.drain()
    assert (await asyncio.wait_for(cr.read(64), 5)).startswith(b"HTTP/1.1 200")
    await asyncio.wait_for(done.wait(), 5)
    cw.close()
    fr.close()
    up.close()
    return bytes(got)


def test_a_proxy_request_reaches_upstream_with_one_valid_stamp_and_its_body_intact():
    sent = (
        b"POST http://x.example/p HTTP/1.1\r\nHost: x.example\r\nX-Fy-Origin: v=1; mac=00\r\n"
        b"Content-Length: 3\r\n\r\nEND"
    )
    got = asyncio.run(_round_trip("proxy", sent))
    head, _, body = got.partition(b"\r\n\r\n")
    assert body == b"END"
    stamps = [ln for ln in head.split(b"\r\n") if ln.lower().startswith(b"x-fy-origin:")]
    assert len(stamps) == 1
    value = stamps[0].split(b":", 1)[1].strip().decode()
    fields, _, mac = value.rpartition("; mac=")
    assert mac == hmac.new(KEY, fields.encode(), hashlib.sha256).hexdigest()
    assert fields == "v=1; via=bridge; src=10.89.0.6:1; boot=b00t; seq=1"


def test_a_token_service_connection_is_spliced_untouched():
    sent = b"GET /token HTTP/1.1\r\nHost: m\r\nX-Fy-Origin: client\r\n\r\nEND"
    assert asyncio.run(_round_trip("plain", sent)) == sent


def test_a_head_that_isnt_http1_is_spliced_unstamped():
    sent = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\nEND"
    assert asyncio.run(_round_trip("proxy", sent)) == sent


def test_a_finished_relay_closes_both_of_its_sockets():
    # Half-closing each direction is not enough: a relay that returns with its sockets still open
    # leaks two descriptors per connection until a garbage collection happens to reach them.
    async def run() -> list[bool]:
        async def upstream(r, w):
            await r.read(65536)
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
            w.close()

        up = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = up.sockets[0].getsockname()[1]
        f = fwd.Forwarder(_args(1, plain=port), KEY, "b00t")
        opened: list = []
        real_open = asyncio.open_connection

        async def tracking_open(*a, **k):
            pair = await real_open(*a, **k)
            opened.append(pair[1])
            return pair

        closed: list[bool] = []

        async def front(r, w):
            fwd.asyncio.open_connection = tracking_open
            try:
                await f.relay(r, w, ("127.0.0.1", port), [])
            finally:
                fwd.asyncio.open_connection = real_open
            closed.extend([w.is_closing(), opened[0].is_closing()])

        fr = await asyncio.start_server(front, "127.0.0.1", 0)
        cr, cw = await asyncio.open_connection("127.0.0.1", fr.sockets[0].getsockname()[1])
        cw.write(b"GET /t HTTP/1.1\r\nHost: m\r\n\r\n")
        await cw.drain()
        await asyncio.wait_for(cr.read(), 5)  # until the forwarder's side is done
        cw.close()
        for _ in range(50):
            if closed:
                break
            await asyncio.sleep(0.02)
        fr.close()
        up.close()
        return closed

    assert asyncio.run(run()) == [True, True]


# ── the upstream socket: re-dial a slow connect, close abortively after a host-first close ─────
#
# Lima's vz user-mode network (gVisor's netstack in the hostagent) silently drops a SYN that
# reuses a 4-tuple still in its 60 s TIME_WAIT (google/gvisor#15013), which it holds whenever the
# host side closed first: connects freeze 7–68 s for every caller of one proxy port at once
# (docs/lima-usernet-freeze.md, Findings 6 and 10, Mitigations 1–2).


async def _run_relay(f, upstream_port, client_actions, kind="plain"):
    """A client → forwarder.relay → upstream round trip; returns what client_actions returned,
    once the relay itself has finished (its close runs after the client has its answer)."""
    finished = asyncio.Event()

    async def front(r, w):
        try:
            await f.relay(r, w, ("127.0.0.1", upstream_port), [])
        finally:
            finished.set()

    fr = await asyncio.start_server(front, "127.0.0.1", 0)
    try:
        cr, cw = await asyncio.open_connection("127.0.0.1", fr.sockets[0].getsockname()[1])
        got = await client_actions(cr, cw)
        await asyncio.wait_for(finished.wait(), 10)
        return got
    finally:
        fr.close()


def _upstream_closing_first(seen: list):
    """An upstream that answers, half-closes FIRST (the host side's FIN), then reports how the
    forwarder ended its side: ``b""`` for a FIN, ``"reset"`` for an RST."""

    async def handle(r, w):
        await r.read(65536)
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await w.drain()
        w.write_eof()
        try:
            seen.append(await asyncio.wait_for(r.read(65536), 5))
        except ConnectionResetError:
            seen.append("reset")
        w.close()

    return handle


async def _ask_and_read_to_eof(cr, cw):
    cw.write(b"GET /t HTTP/1.1\r\nHost: m\r\n\r\n")
    await cw.drain()
    try:
        body = await asyncio.wait_for(cr.read(), 5)
    except ConnectionResetError:  # a failed relay closes with the request unread: an RST
        body = b""
    cw.close()
    return body


def test_after_the_upstream_closes_first_its_socket_is_closed_with_a_reset(monkeypatch):
    # An RST instead of a FIN: gVisor never enters TIME_WAIT for the connection (Finding 6:
    # 0 stalls at 7,398 conn/s, against 22 stalls of up to 68 s with FINs).
    monkeypatch.setattr(fwd, "send_queue", lambda sock: 0)

    async def run():
        seen: list = []
        up = await asyncio.start_server(_upstream_closing_first(seen), "127.0.0.1", 0)
        f = fwd.Forwarder(_args(1, plain=up.sockets[0].getsockname()[1]), KEY, "b00t")
        body = await _run_relay(f, up.sockets[0].getsockname()[1], _ask_and_read_to_eof)
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.02)
        up.close()
        return body, seen, f.stats

    body, seen, stats = asyncio.run(run())
    assert body.endswith(b"ok")
    assert seen == ["reset"]
    assert stats["aborted"] == 1


def test_bytes_not_yet_acknowledged_are_never_discarded_by_a_reset(monkeypatch):
    # An RST throws away whatever is still in the send queue: while anything owed upstream is
    # unacknowledged (SIOCOUTQ > 0), or that can't be read at all, the close stays a FIN.
    for queued in (5, None):
        monkeypatch.setattr(fwd, "send_queue", lambda sock, q=queued: q)
        monkeypatch.setattr(fwd, "DRAIN_WAIT", 0.05)

        async def run():
            seen: list = []
            up = await asyncio.start_server(_upstream_closing_first(seen), "127.0.0.1", 0)
            f = fwd.Forwarder(_args(1, plain=up.sockets[0].getsockname()[1]), KEY, "b00t")
            await _run_relay(f, up.sockets[0].getsockname()[1], _ask_and_read_to_eof)
            for _ in range(100):
                if seen:
                    break
                await asyncio.sleep(0.02)
            up.close()
            return seen, f.stats

        seen, stats = asyncio.run(run())
        assert seen == [b""], queued
        assert stats["aborted"] == 0


def test_a_client_that_closes_first_closes_upstream_with_a_fin(monkeypatch):
    # Then the guest side closed first and holds TIME_WAIT itself: nothing to avoid, so no reset.
    monkeypatch.setattr(fwd, "send_queue", lambda sock: 0)

    async def run():
        seen: list = []

        async def upstream(r, w):
            got = b""
            while chunk := await r.read(65536):
                got += chunk
            seen.append(got)  # the client's EOF arrived as a FIN, before we answered
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
            w.close()

        up = await asyncio.start_server(upstream, "127.0.0.1", 0)
        f = fwd.Forwarder(_args(1, plain=up.sockets[0].getsockname()[1]), KEY, "b00t")

        async def half_close_then_read(cr, cw):
            cw.write(b"GET /t HTTP/1.1\r\nHost: m\r\n\r\n")
            cw.write_eof()
            return await asyncio.wait_for(cr.read(), 5)

        body = await _run_relay(f, up.sockets[0].getsockname()[1], half_close_then_read)
        up.close()
        return body, seen, f.stats

    body, seen, stats = asyncio.run(run())
    assert body.startswith(b"HTTP/1.1 200") and seen and seen[0].startswith(b"GET /t")
    assert stats["aborted"] == 0


class _Dialler:
    """Stands in for asyncio.open_connection: hangs the first ``slow`` calls, then connects."""

    def __init__(self, slow: int, refuse: bool = False) -> None:
        self.slow, self.refuse, self.calls = slow, refuse, 0

    async def __call__(self, host, port):
        self.calls += 1
        if self.refuse:
            raise ConnectionRefusedError("refused")
        if self.calls <= self.slow:
            await asyncio.sleep(3600)
        return await asyncio.open_connection(host, port)


def _redial(monkeypatch, dialler):
    monkeypatch.setattr(fwd, "CONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(fwd, "send_queue", lambda sock: None)

    async def run():
        async def upstream(r, w):
            await r.read(65536)
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
            w.close()

        up = await asyncio.start_server(upstream, "127.0.0.1", 0)
        f = fwd.Forwarder(_args(1, plain=up.sockets[0].getsockname()[1]), KEY, "b00t")
        f.dial = dialler
        body = await _run_relay(f, up.sockets[0].getsockname()[1], _ask_and_read_to_eof)
        up.close()
        return body, f.stats

    return asyncio.run(run())


def test_a_slow_connect_is_redialled_on_a_new_socket(monkeypatch):
    # A frozen SYN waits out gVisor's TIME_WAIT; a new socket has a new source port, a new tuple
    # (Finding 10: 2 s and re-dial bounded a connect at 10 s instead of 68 s).
    dialler = _Dialler(slow=2)
    body, stats = _redial(monkeypatch, dialler)
    assert body.startswith(b"HTTP/1.1 200")
    assert dialler.calls == 3 and stats["redials"] == 2 and stats["upstream_failed"] == 0


def test_redials_are_bounded_then_the_client_connection_fails(monkeypatch):
    dialler = _Dialler(slow=10**6)
    body, stats = _redial(monkeypatch, dialler)
    assert body == b""  # closed, as any failed upstream connect is
    assert dialler.calls == fwd.CONNECT_ATTEMPTS and stats["upstream_failed"] == 1


def test_a_refused_connect_is_not_redialled(monkeypatch):
    # Refused is an answer (the proxy isn't listening), not a lost SYN: fail at once, as before.
    dialler = _Dialler(slow=0, refuse=True)
    body, stats = _redial(monkeypatch, dialler)
    assert body == b"" and dialler.calls == 1 and stats["redials"] == 0


def test_the_send_queue_reads_where_it_can_and_never_raises():
    with socket.socket() as s:
        got = fwd.send_queue(s)
    assert got is None or got == 0


def test_a_descriptor_closed_mid_scan_doesnt_hide_the_rest_of_its_process(tmp_path, monkeypatch):
    # A busy client opens and closes sockets while the forwarder reads its /proc/<pid>/fd: one fd
    # gone between the listing and its readlink used to abandon the whole process, and the
    # connection was refused as "owner unknown" (0.7% at ~600/s from 4 threads, seen live).
    proc = tmp_path / "proc"
    (proc / "100" / "fd").mkdir(parents=True)
    (proc / "100" / "comm").write_text("python3\n")
    (proc / "100" / "fd" / "3").symlink_to("socket:[1]")
    (proc / "100" / "fd" / "9").symlink_to("socket:[77]")
    root = tmp_path / "cg"
    (root / "s").mkdir(parents=True)
    (root / "s" / "cgroup.procs").write_text("100\n")
    real = fwd.os.readlink

    def flaky(path, *a, **k):
        if path.endswith("/fd/3"):
            raise FileNotFoundError(path)  # closed after the listing
        return real(path, *a, **k)

    real_listdir = fwd.os.listdir

    def ordered(path):
        names = real_listdir(path)
        return sorted(names) if str(path).endswith("/fd") else names  # the vanished fd first

    monkeypatch.setattr(fwd.os, "readlink", flaky)
    monkeypatch.setattr(fwd.os, "listdir", ordered)
    me = (proc / "100").stat().st_uid
    assert fwd.Cgroups(str(root)).owner_comm("/s", 77, [(me, me)], proc=str(proc)) == "python3"
