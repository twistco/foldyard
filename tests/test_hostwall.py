"""hostwall.py — the Linux host-side egress wall for the machine VM. Pure rendering + the
capability gate + cgroup discovery are unit-tested here; the live nftables behaviour (guest
egress refused, DNS + band allowed, operator untouched) was validated on the GCP rig
(docs/isolation-layers.md). No root, no nft, no VM needed for these."""

from __future__ import annotations

import subprocess

import pytest

from foldyard import config, hostwall

_SCOPE = "user.slice/user-1000.slice/user@1000.service/app.slice/fy-machine-acme.scope"


@pytest.fixture
def bands(monkeypatch):
    """Pin the project's daemon bands so the rendered ports are deterministic (no ports.json)."""
    monkeypatch.setattr(config, "proxy_port_base", lambda: 41000)
    monkeypatch.setattr(config, "gcp_minter_port_base", lambda: 41100)


def test_table_name_is_per_vm_and_nft_safe():
    assert hostwall.table_name("acme") == "fy_host_wall_acme"
    # nftables identifiers allow only [A-Za-z0-9_./]; anything else collapses to _
    assert hostwall.table_name("acme two!") == "fy_host_wall_acme_two_"


def test_cgroup_level_counts_components():
    assert hostwall.cgroup_level(_SCOPE) == 5
    assert hostwall.cgroup_level("/user.slice/") == 1
    assert hostwall.cgroup_level("/") == 0


def test_vm_cgroup_scope_reads_the_unified_v2_line(tmp_path, monkeypatch):
    proc = tmp_path / "proc" / "999"
    proc.mkdir(parents=True)
    # a v1 controller line first (must be ignored), then the unified v2 line
    (proc / "cgroup").write_text(f"1:name=systemd:/legacy\n0::/{_SCOPE}\n")
    monkeypatch.setattr(hostwall, "PROC", tmp_path / "proc")
    assert hostwall.vm_cgroup_scope(999) == _SCOPE


def test_vm_cgroup_scope_missing_pid_is_empty(monkeypatch):
    # a pid with no /proc entry reads as "" — install() then declines rather than walling nothing
    assert hostwall.vm_cgroup_scope(2_000_000_000) == ""


def test_available_needs_nft_and_cgroup2(monkeypatch):
    monkeypatch.setattr(hostwall.shutil, "which", lambda c: None)
    assert hostwall.available() is False
    monkeypatch.setattr(hostwall.shutil, "which", lambda c: "/usr/sbin/nft")
    monkeypatch.setattr(hostwall.Path, "exists", lambda self: False)
    assert hostwall.available() is False
    monkeypatch.setattr(hostwall.Path, "exists", lambda self: True)
    assert hostwall.available() is True


def test_render_matches_the_validated_ruleset(bands):
    rs = hostwall.render("acme", _SCOPE, ssh_port=45285)
    # the output hook matches the VM's cgroup at its own level, and jumps to the vm chain
    assert f'socket cgroupv2 level 5 "{_SCOPE}" jump vm' in rs
    # established first, so replies to allowed flows are never re-evaluated
    assert "ct state established,related accept" in rs
    # loopback: Lima's forwarded SSH port + THIS project's two daemon bands, nothing else
    assert 'oif "lo" tcp dport { 45285, 41000-41089, 41100-41189 } accept' in rs
    # QEMU's slirp DNS to the host resolver stays open (both transports)
    assert "ip daddr { 127.0.0.53 } udp dport 53 accept" in rs
    assert "ip daddr { 127.0.0.53 } tcp dport 53 accept" in rs
    # everything else is REJECTED (fail-closed), tcp with a reset so the guest fails fast
    assert "meta l4proto tcp counter reject with tcp reset" in rs
    assert "reject with icmpx type admin-prohibited" in rs


def test_render_is_idempotent_by_construction(bands):
    """The delete-then-declare pair means a re-apply replaces the table atomically — never
    errors on an existing table, never stacks a second copy."""
    rs = hostwall.render("acme", _SCOPE, ssh_port=22)
    lines = [ln.strip() for ln in rs.splitlines()]
    assert lines[0] == "table inet fy_host_wall_acme"  # declare (so the delete can't miss)
    assert lines[1] == "delete table inet fy_host_wall_acme"
    assert lines[2] == "table inet fy_host_wall_acme {"  # the real definition


def test_render_bands_are_this_projects_only(monkeypatch):
    """A different project's band renders different ranges — a walled VM reaches only its own
    daemons, never a sibling's (the same scoping the in-VM wall enforces)."""
    monkeypatch.setattr(config, "proxy_port_base", lambda: 42000)
    monkeypatch.setattr(config, "gcp_minter_port_base", lambda: 42100)
    rs = hostwall.render("other", _SCOPE, ssh_port=22)
    assert "42000-42089, 42100-42189" in rs
    assert "41000" not in rs


def test_render_honours_custom_resolvers(bands):
    rs = hostwall.render("acme", _SCOPE, ssh_port=22, resolvers=("10.0.0.1", "10.0.0.2"))
    assert "ip daddr { 10.0.0.1, 10.0.0.2 } udp dport 53 accept" in rs


def test_install_streams_over_stdin_as_root(bands, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["input"] = kw.get("input")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(hostwall, "available", lambda: True)
    monkeypatch.setattr(hostwall.subprocess, "run", fake_run)
    assert hostwall.install("acme", _SCOPE, ssh_port=22) is True
    # loaded from stdin (`nft -f -`), never a temp file a lesser process could swap
    assert seen["cmd"] == ["sudo", "nft", "-f", "-"]
    assert seen["input"] is not None and "fy_host_wall_acme" in seen["input"]


def test_install_declines_when_the_host_cannot_enforce(bands, monkeypatch):
    monkeypatch.setattr(hostwall, "available", lambda: False)
    monkeypatch.setattr(
        hostwall.subprocess, "run", lambda *a, **k: pytest.fail("must not shell nft")
    )
    assert hostwall.install("acme", _SCOPE, ssh_port=22) is False


def test_install_declines_on_an_empty_scope(bands, monkeypatch):
    """An empty scope (VM pid gone) must never wall the whole world — decline, don't render a
    table whose match is `cgroupv2 level 0 ""`."""
    monkeypatch.setattr(hostwall, "available", lambda: True)
    monkeypatch.setattr(
        hostwall.subprocess, "run", lambda *a, **k: pytest.fail("must not shell nft")
    )
    assert hostwall.install("acme", "", ssh_port=22) is False


# ── wiring into the machine lifecycle: the per-VM scope, the resolver, idempotent removal ──


def test_scope_unit_is_per_vm_and_systemd_safe():
    assert hostwall.scope_unit("acme") == "fy-machine-acme.scope"
    assert hostwall.scope_unit("acme two!") == "fy-machine-acme_two_.scope"


def test_scoped_argv_prefix_runs_the_start_in_its_own_transient_scope():
    """`systemd-run --user --scope` puts limactl AND everything it forks (the hostagent, QEMU)
    in ONE per-VM cgroup, so the wall's match is predictable — never the login session's scope,
    which would wall the operator's whole shell."""
    assert hostwall.scoped_argv_prefix("acme") == [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--unit",
        "fy-machine-acme.scope",
    ]


def test_in_own_scope_requires_the_vm_scope_as_the_leaf():
    assert hostwall.in_own_scope("acme", _SCOPE) is True
    # the login session's scope: walling it would wall the operator's whole session
    assert hostwall.in_own_scope("acme", "user.slice/user-1000.slice/session-2.scope") is False
    # a sibling VM's scope is not ours either
    assert hostwall.in_own_scope("acme", _SCOPE.replace("acme", "other")) is False
    assert hostwall.in_own_scope("acme", "") is False


def test_resolvers_come_from_resolv_conf(tmp_path, monkeypatch):
    conf = tmp_path / "resolv.conf"
    conf.write_text(
        "# Generated\nnameserver 10.0.0.2\nsearch example.internal\nnameserver fd00::1\n"
        "options edns0\nnameserver 8.8.8.8\n"
    )
    monkeypatch.setattr(hostwall, "RESOLV_CONF", conf)
    assert hostwall.resolvers() == ("10.0.0.2", "fd00::1", "8.8.8.8")


def test_resolvers_fall_back_to_the_stub_when_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(hostwall, "RESOLV_CONF", tmp_path / "missing")
    assert hostwall.resolvers() == hostwall.DEFAULT_RESOLVERS
    empty = tmp_path / "empty"
    empty.write_text("# nothing\n")
    monkeypatch.setattr(hostwall, "RESOLV_CONF", empty)
    assert hostwall.resolvers() == hostwall.DEFAULT_RESOLVERS


def test_render_splits_resolvers_by_address_family(bands):
    rs = hostwall.render("acme", _SCOPE, ssh_port=22, resolvers=("10.0.0.2", "fd00::1"))
    assert "ip daddr { 10.0.0.2 } udp dport 53 accept" in rs
    assert "ip6 daddr { fd00::1 } udp dport 53 accept" in rs
    assert "ip6 daddr { fd00::1 } tcp dport 53 accept" in rs
    # no empty set is ever rendered (nft rejects `{ }`)
    v4_only = hostwall.render("acme", _SCOPE, ssh_port=22, resolvers=("10.0.0.2",))
    assert "ip6 daddr" not in v4_only


def test_remove_is_idempotent_by_construction(monkeypatch):
    """`nft delete table` errors on a missing table, so removal streams the same declare-then-
    delete pair `render` uses — a table that is already gone is a clean no-op."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw.get("input")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(hostwall.shutil, "which", lambda c: "/usr/sbin/nft")
    monkeypatch.setattr(hostwall.subprocess, "run", fake_run)
    assert hostwall.remove("acme") is True
    assert seen["cmd"] == ["sudo", "nft", "-f", "-"]
    assert seen["input"].splitlines() == [
        "table inet fy_host_wall_acme",
        "delete table inet fy_host_wall_acme",
    ]


# ── the VM's own host-side plumbing: loopback listeners its processes hold (Lima's host
# resolver in the hostagent, QEMU's SSH hostfwd) — discovered from /proc, allowed on lo ──


def _proc(tmp_path, pids: dict[int, list[int]], tcp: str, udp: str, tcp6: str = "", udp6: str = ""):
    """A fake /proc: per-pid fd/ symlinks to socket inodes, plus the net tables."""
    root = tmp_path / "proc"
    for pid, inodes in pids.items():
        fd = root / str(pid) / "fd"
        fd.mkdir(parents=True)
        for i, ino in enumerate(inodes):
            (fd / str(i)).symlink_to(f"socket:[{ino}]")
        (fd / "99").symlink_to("/dev/null")  # a non-socket fd, must be ignored
    net = root / "net"
    net.mkdir()
    hdr = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    for name, body in (("tcp", tcp), ("udp", udp), ("tcp6", tcp6), ("udp6", udp6)):
        (net / name).write_text(hdr + body)
    return root


def _row(local: str, st: str, inode: int) -> str:
    return f"   0: {local} 00000000:0000 {st} 00000000:00000000 00:00000000 00000000  1000        0 {inode} 1 0 100 0 0 0 0\n"


def test_listener_ports_finds_the_vm_processes_loopback_listeners(tmp_path, monkeypatch):
    root = _proc(
        tmp_path,
        {1093: [500, 501, 502], 1114: [600, 601]},
        # 1093 (hostagent): DNS on 127.0.0.1:38020 tcp LISTEN + udp; 1114 (QEMU): ssh hostfwd
        # 127.0.0.1:40209 LISTEN, an outbound udp socket bound to 0.0.0.0 (slirp), and an
        # ESTABLISHED tcp flow — only the loopback LISTENERS count
        tcp=_row("0100007F:9484", "0A", 500)
        + _row("0100007F:9D11", "0A", 600)
        + _row("0100007F:8409", "01", 601),
        udp=_row("0100007F:9484", "07", 501) + _row("00000000:A3D2", "07", 502),
        tcp6=_row("00000000000000000000000001000000:9484", "0A", 700),  # not one of ours
    )
    monkeypatch.setattr(hostwall, "PROC", root)
    assert hostwall.listener_ports(1093, 1114) == (("tcp", 38020), ("tcp", 40209), ("udp", 38020))


def test_listener_ports_ignores_a_gone_pid_and_unreadable_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(hostwall, "PROC", tmp_path / "nope")
    assert hostwall.listener_ports(1, 2) == ()


def test_render_opens_the_plumbing_on_loopback_only(bands):
    rs = hostwall.render(
        "acme", _SCOPE, ssh_port=45285, plumbing=(("tcp", 38020), ("udp", 38020), ("tcp", 40209))
    )
    assert 'oif "lo" tcp dport { 45285, 41000-41089, 41100-41189, 38020, 40209 } accept' in rs
    assert 'oif "lo" udp dport { 38020 } accept' in rs
    # nothing without plumbing: no empty udp set
    assert "udp dport {" not in hostwall.render("acme", _SCOPE, ssh_port=45285)
