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
    assert _owner(f"{USER}/app.slice/crun-buildah-b1.scope", None) == ("build", "", 501)
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
        fwd.vm_nft("192.168.5.2", [(41000, 41089)], "501, 524288-1074266111", "127.42.0.1", 4747),
    ):
        assert text.startswith("table ip fy_fwd\ndelete table ip fy_fwd\ntable ip fy_fwd {")
        assert "{{" not in text and "}}" not in text  # Lima renders the boot script as a template


def test_the_vm_table_redirects_and_guards_only_the_walled_principals():
    text = fwd.vm_nft(
        "192.168.5.2", [(41000, 41089), (41100, 41189)], "501, 5-9", "127.42.0.1", 4747
    )
    match = "meta skuid { 501, 5-9 } ip daddr 192.168.5.2 tcp dport { 41000-41089, 41100-41189 }"
    assert f"{match} dnat to 127.42.0.1:4747" in text
    # anything of theirs still naming a daemon port after the redirect is refused: never around
    assert f"{match} counter reject with tcp reset" in text
    assert "type filter hook output" in text


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
