"""hostwall.py — the Linux host-side egress wall for the machine VM. Pure rendering + the
capability gate + cgroup discovery are unit-tested here; the live nftables behaviour (guest
egress refused, DNS + band allowed, operator untouched) was validated on the GCP rig
(docs/isolation-layers.md), and the boot-STABLE form — loopback decided on the INPUT hook by the
listener's cgroup, a per-project ct mark carrying the origin across — on a GitHub ubuntu-24.04 runner
(2026-09-18). No root, no nft, no VM needed for these."""

from __future__ import annotations

import json
import re
import socket
import subprocess

import pytest

from foldyard import config, hostwall

_SLICE = "user.slice/user-1000.slice/user@1000.service/fy.slice/fy-machine-acme.slice"
_SCOPE = f"{_SLICE}/fy-machine-acme.scope"


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
    assert hostwall.cgroup_level(_SLICE) == 5
    assert hostwall.cgroup_level(_SCOPE) == 6
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
    rs = hostwall.render("acme", _SLICE)
    # the output hook matches the VM's SLICE at its own level, and jumps to the vm chain
    assert f'socket cgroupv2 level 5 "{_SLICE}" jump vm' in rs
    # established first, so replies to allowed flows are never re-evaluated
    assert "ct state established,related accept" in rs
    # DNS to any resolver (both transports): the hostagent resolves on the guest's behalf, and
    # resolvers change with the network — nothing here may vary per boot or per network
    assert "    udp dport 53 accept\n    tcp dport 53 accept" in rs
    # loopback is ALLOWED OUT but MARKED, so the input hook can decide by who is listening
    mark = hostwall.ct_mark()
    assert f'oif "lo" ct mark set {mark:#010x} accept' in rs
    assert f'iif "lo" ct mark {mark:#010x} jump lo' in rs
    # …and on input only two receivers pass: a socket in the VM's own slice (the hostagent's
    # resolver, QEMU's SSH forward — whatever ports Lima picked this boot) or THIS project's bands
    assert f'socket cgroupv2 level 5 "{_SLICE}" accept' in rs
    assert "tcp dport { 41000-41089, 41100-41189 } accept" in rs
    # everything else is REJECTED (fail-closed), tcp with a reset so the guest fails fast
    assert rs.count("meta l4proto tcp counter reject with tcp reset") == 2
    assert rs.count("reject with icmpx type admin-prohibited") == 2


def test_render_names_no_per_boot_or_per_network_fact(bands):
    """The whole point of the stable form: an operator can apply the table ONCE. Nothing in it may
    come from a running VM (Lima's forwarded SSH port, the hostagent's listener ports) or from the
    network (resolv.conf) — only the VM name, its slice and the project's bands."""
    rs = hostwall.render("acme", _SLICE)
    assert "127.0.0.53" not in rs and "daddr" not in rs
    numbers = set(re.findall(r"\b\d{2,5}\b", rs.replace(_SLICE, ""))) - {"53"}
    assert numbers == {"41000", "41089", "41100", "41189"}, numbers


def test_ct_mark_is_the_projects_band_under_foldyards_byte(bands, monkeypatch):
    """Two projects' tables both hook INPUT, each jumping on ITS mark — a shared value would let
    project A's table judge project B's loopback flows (and reject them: B's slice is not A's,
    so B's VM would lose its own plumbing with nothing naming the cause). The mark is therefore
    the project's proxy band base — unique per project on this host by the allocator's
    construction, never by chance — under foldyard's byte."""
    assert hostwall.ct_mark() == (0xF4 << 24) | 41000
    monkeypatch.setattr(config, "proxy_port_base", lambda: 42000)
    assert hostwall.ct_mark() == (0xF4 << 24) | 42000


def test_render_is_idempotent_by_construction(bands):
    """The delete-then-declare pair means a re-apply replaces the table atomically — never
    errors on an existing table, never stacks a second copy."""
    rs = hostwall.render("acme", _SLICE)
    lines = [ln.strip() for ln in rs.splitlines()]
    assert lines[0] == "table inet fy_host_wall_acme"  # declare (so the delete can't miss)
    assert lines[1] == "delete table inet fy_host_wall_acme"
    assert lines[2] == "table inet fy_host_wall_acme {"  # the real definition


def test_render_bands_are_this_projects_only(monkeypatch):
    """A different project's band renders different ranges — a walled VM reaches only its own
    daemons, never a sibling's (the same scoping the in-VM wall enforces)."""
    monkeypatch.setattr(config, "proxy_port_base", lambda: 42000)
    monkeypatch.setattr(config, "gcp_minter_port_base", lambda: 42100)
    rs = hostwall.render("other", _SLICE)
    assert "42000-42089, 42100-42189" in rs
    assert "41000" not in rs


# ── wiring into the machine lifecycle: the per-VM slice + scope ──


def test_scope_and_slice_units_are_per_vm_and_systemd_safe():
    assert hostwall.scope_unit("acme") == "fy-machine-acme.scope"
    assert hostwall.scope_unit("acme two!") == "fy-machine-acme_two_.scope"
    assert hostwall.slice_unit("acme") == "fy-machine-acme.slice"
    assert hostwall.slice_unit("acme two!") == "fy-machine-acme_two_.slice"


def test_scoped_argv_prefix_runs_the_start_in_its_own_scope_under_its_own_slice():
    """`systemd-run --user --scope` puts limactl AND everything it forks (the hostagent, QEMU)
    in ONE per-VM cgroup, so the wall's match is predictable — never the login session's scope,
    which would wall the operator's whole shell. The scope sits under a per-VM SLICE, and the
    slice is what the wall matches: a scope dies with its last process, a slice survives idle —
    the same cgroup id across every VM restart, which is what lets an applied table stay valid."""
    assert hostwall.scoped_argv_prefix("acme") == [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--slice",
        "fy-machine-acme.slice",
        "--unit",
        "fy-machine-acme.scope",
    ]


def test_in_own_scope_requires_the_vm_scope_under_the_vm_slice():
    assert hostwall.in_own_scope("acme", _SCOPE) is True
    # the login session's scope: walling it would wall the operator's whole session
    assert hostwall.in_own_scope("acme", "user.slice/user-1000.slice/session-2.scope") is False
    # a sibling VM's scope is not ours either
    assert hostwall.in_own_scope("acme", _SCOPE.replace("acme", "other")) is False
    # the right scope but not under its slice (started before the slice existed): the wall
    # would match nothing — refuse, as for any other misplaced VM
    unsliced = "user.slice/user-1000.slice/user@1000.service/app.slice/fy-machine-acme.scope"
    assert hostwall.in_own_scope("acme", unsliced) is False
    assert hostwall.in_own_scope("acme", "") is False


def test_vm_slice_is_the_scopes_parent():
    assert hostwall.vm_slice(_SCOPE) == _SLICE
    assert hostwall.vm_slice("fy-machine-acme.scope") == ""
    assert hostwall.vm_slice("") == ""


# ── the kernel half of the capability: nftables' `socket` expression (CONFIG_NFT_SOCKET) ──
#
# The host table matches the VM by `socket cgroupv2`; a kernel built without nft_socket refuses
# the rule with ENOENT at load time. The stock WSL2 kernel is one (`# CONFIG_NFT_SOCKET is not
# set` on its 6.6 and 6.18 branches — the first wsl2-host-e2e run, 2026-09-17). The kernel
# config, when the host exposes one, lets preflight refuse BEFORE the VM is re-provisioned
# walled; on a host whose config is unreadable the operator's own `nft -f` says so (ENOENT at
# the `socket cgroupv2` rule) — in their terminal, since they run it.


def _kernel_configs(monkeypatch, tmp_path, *, proc: str | None, boot: str | None):
    import gzip

    paths = []
    if proc is not None:
        gz = tmp_path / "config.gz"
        gz.write_bytes(gzip.compress(proc.encode()))
        paths.append(gz)
    else:
        paths.append(tmp_path / "missing-config.gz")
    if boot is not None:
        plain = tmp_path / "config-6.8.0"
        plain.write_text(boot)
        paths.append(plain)
    else:
        paths.append(tmp_path / "missing-config-6.8.0")
    monkeypatch.setattr(hostwall, "_kernel_config_paths", lambda: tuple(paths))


def test_nft_socket_in_kernel_reads_proc_config_gz(tmp_path, monkeypatch):
    _kernel_configs(
        monkeypatch, tmp_path, proc="CONFIG_NF_TABLES=y\nCONFIG_NFT_SOCKET=m\n", boot=None
    )
    assert hostwall.nft_socket_in_kernel() is True
    _kernel_configs(monkeypatch, tmp_path, proc="CONFIG_NFT_SOCKET=y\n", boot=None)
    assert hostwall.nft_socket_in_kernel() is True
    _kernel_configs(
        monkeypatch,
        tmp_path,
        proc="CONFIG_NF_TABLES=y\n# CONFIG_NFT_SOCKET is not set\n",
        boot=None,
    )
    assert hostwall.nft_socket_in_kernel() is False


def test_nft_socket_in_kernel_falls_back_to_the_boot_config(tmp_path, monkeypatch):
    _kernel_configs(monkeypatch, tmp_path, proc=None, boot="# CONFIG_NFT_SOCKET is not set\n")
    assert hostwall.nft_socket_in_kernel() is False
    _kernel_configs(monkeypatch, tmp_path, proc=None, boot="CONFIG_NFT_SOCKET=m\n")
    assert hostwall.nft_socket_in_kernel() is True


def test_nft_socket_in_kernel_is_unknown_without_a_readable_config(tmp_path, monkeypatch):
    """No config, or one that never mentions the symbol: unknown (None), never a guess — the
    load-time explanation covers that host."""
    _kernel_configs(monkeypatch, tmp_path, proc=None, boot=None)
    assert hostwall.nft_socket_in_kernel() is None
    _kernel_configs(monkeypatch, tmp_path, proc="CONFIG_NF_TABLES=y\n", boot=None)
    assert hostwall.nft_socket_in_kernel() is None
    # unreadable garbage where the gzip should be: still unknown, never an exception
    (tmp_path / "config.gz").write_bytes(b"not gzip")
    monkeypatch.setattr(hostwall, "_kernel_config_paths", lambda: (tmp_path / "config.gz",))
    assert hostwall.nft_socket_in_kernel() is None


# ── the operator's install: the persistent slice, the staged root-side files ───────────────


def test_slice_unit_is_wanted_by_the_user_managers_default_target():
    text = hostwall.slice_unit_text("acme")
    assert "[Slice]" in text and "WantedBy=default.target" in text
    assert "'acme'" in text


def test_service_unit_is_bound_to_the_users_manager_and_names_what_root_runs():
    text = hostwall.service_unit_text("acme", 1000, "/usr/sbin/nft")
    # up after — and only while — the user manager: the slice (and the cgroup ID the table
    # binds to) lives under user@1000.service
    assert "After=user@1000.service" in text and "BindsTo=user@1000.service" in text
    assert "WantedBy=user@1000.service" in text
    # exactly two root actions, both readable in full — no shell, no template, no globs
    assert "ExecStart=/usr/sbin/nft -f /etc/foldyard/host-wall-acme.nft" in text
    assert "ExecStop=/usr/sbin/nft delete table inet fy_host_wall_acme" in text
    assert "RemainAfterExit=yes" in text


def test_ensure_slice_writes_the_unit_once_and_enables_it(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = "/user.slice/user-1000.slice/user@1000.service/fy.slice/fy-machine-acme.slice\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out if "show" in cmd else "", stderr="")

    monkeypatch.setattr(hostwall.subprocess, "run", fake_run)
    monkeypatch.setattr(hostwall, "user_unit_dir", lambda: tmp_path)
    assert hostwall.ensure_slice("acme") == _SLICE.replace("user-1000", "user-1000")
    assert (tmp_path / "fy-machine-acme.slice").read_text() == hostwall.slice_unit_text("acme")
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "fy-machine-acme.slice"],
        ["systemctl", "--user", "show", "-p", "ControlGroup", "--value", "fy-machine-acme.slice"],
    ]
    # a second run with the unit unchanged: no reload (not free), still enable --now (idempotent)
    calls.clear()
    hostwall.ensure_slice("acme")
    assert ["systemctl", "--user", "daemon-reload"] not in calls


def test_ensure_slice_is_empty_when_the_user_manager_cannot_deliver(tmp_path, monkeypatch):
    monkeypatch.setattr(hostwall, "user_unit_dir", lambda: tmp_path)
    monkeypatch.setattr(
        hostwall.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no manager"),
    )
    assert hostwall.ensure_slice("acme") == ""


def test_stage_renders_the_files_and_the_exact_operator_commands(bands, tmp_path, monkeypatch):
    monkeypatch.setattr(hostwall.shutil, "which", lambda name: "/usr/sbin/nft")
    monkeypatch.setattr(hostwall.os, "getuid", lambda: 1000)
    staged = hostwall.stage("acme", _SLICE, tmp_path / "host-wall")
    assert staged.ruleset.read_text() == hostwall.render("acme", _SLICE)
    assert staged.service.read_text() == hostwall.service_unit_text("acme", 1000, "/usr/sbin/nft")
    # printed for the operator, never run by foldyard: copies (root-owned, so nothing running
    # as the operator can change what root loads), a reload, one enable
    assert staged.install == (
        f"sudo install -D -m 0644 {staged.ruleset} /etc/foldyard/host-wall-acme.nft",
        f"sudo install -D -m 0644 {staged.service} /etc/systemd/system/fy-host-wall-acme.service",
        "sudo systemctl daemon-reload",
        "sudo systemctl enable --now fy-host-wall-acme.service",
    )
    assert staged.uninstall == (
        "sudo systemctl disable --now fy-host-wall-acme.service",
        "sudo rm /etc/systemd/system/fy-host-wall-acme.service /etc/foldyard/host-wall-acme.nft",
        "sudo systemctl daemon-reload",
    )


# ── the probe: enforcement observed from inside the slice, never a table read ──────────────


def test_probe_argv_runs_this_interpreter_under_the_slice():
    argv = hostwall.probe_argv("fy-machine-acme.slice", {"loopback": ("127.0.0.1", 40001)})
    assert argv[:7] == [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--slice",
        "fy-machine-acme.slice",
    ]
    assert argv[7:] == [
        "--",
        hostwall.sys.executable,
        "-m",
        "foldyard.hostwall",
        "--probe",
        "loopback=127.0.0.1:40001",
    ]


@pytest.mark.parametrize(
    ("checks", "enforcing"),
    [
        ({"loopback": "refused", "external": "refused", "band": "ok"}, True),
        # no route off-host: the wall never saw the SYN, so nothing is proven either way and an
        # offline laptop is not refused its own VM
        ({"loopback": "refused", "external": "unreachable", "band": "ok"}, True),
        # the safety half: an out-of-slice loopback listener reachable = no (or a stale) table
        ({"loopback": "ok", "external": "refused", "band": "ok"}, False),
        # the SYN left the host and timed out on TEST-NET: not walled
        ({"loopback": "refused", "external": "timeout", "band": "ok"}, False),
        # the staleness half: the band moved and the table still names the old one
        ({"loopback": "refused", "external": "refused", "band": "refused"}, False),
        ({}, False),
    ],
)
def test_probe_verdict(checks, enforcing):
    assert hostwall._verdict(checks) is enforcing


def test_probe_opens_the_listeners_outside_the_slice_and_reads_the_childs_verdicts(
    bands, monkeypatch
):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        targets = dict(a.split("=") for a in cmd[cmd.index("--probe") + 1 :])
        # the loopback + band listeners are LIVE while the child runs (a connect would succeed)
        for name in ("loopback", "band"):
            host, _, port = targets[name].rpartition(":")
            with socket.create_connection((host, int(port)), timeout=1):
                pass
        seen["targets"] = targets
        out = '{"loopback": "refused", "external": "refused", "band": "ok"}'
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(hostwall.subprocess, "run", fake_run)
    res = hostwall.probe("acme")
    assert res.enforcing is True and res.error == ""
    assert seen["cmd"][5:7] == ["--slice", "fy-machine-acme.slice"]
    assert seen["targets"]["external"] == "192.0.2.1:9"
    assert seen["targets"]["band"].startswith("127.0.0.1:410")  # the first free port of the band
    assert res.detail() == "loopback ✓ refused, external ✓ refused, band ✓ ok"


def test_probe_reports_a_child_that_could_not_run(bands, monkeypatch):
    monkeypatch.setattr(
        hostwall.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="Failed to connect to bus"
        ),
    )
    res = hostwall.probe("acme")
    assert res.enforcing is False and "bus" in res.error
    monkeypatch.setattr(hostwall.subprocess, "run", _raise(FileNotFoundError(2, "no systemd-run")))
    assert hostwall.probe("acme").enforcing is False


def _raise(exc):
    def run(*a, **k):
        raise exc

    return run


def test_probe_child_reports_per_target(capsys):
    # a listener to reach, and a closed port to be refused by — the child's own verdicts
    lis = socket.socket()
    lis.bind(("127.0.0.1", 0))
    lis.listen(1)
    with socket.socket() as gone:
        gone.bind(("127.0.0.1", 0))  # a port nothing listens on once this closes
        refused_port = gone.getsockname()[1]
    try:
        ok_port = lis.getsockname()[1]
        rc = hostwall._probe_main([f"a=127.0.0.1:{ok_port}", f"b=127.0.0.1:{refused_port}"])
    finally:
        lis.close()
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"a": "ok", "b": "refused"}
