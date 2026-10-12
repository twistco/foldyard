"""prereq.py — `fy prereq`: what this computer needs before a first `fy init` / `fy box up`.

`rows()` is pure over a `Host` of gathered facts, so every host shape (macOS with brew, a Debian
or Fedora Linux, WSL2, a host missing KVM) is a constructed value here — no real PATH, `limactl`
or `/dev/kvm` is consulted. `gather()` itself is the thin I/O half.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from foldyard import basevm, prereq


def _mac(**over) -> prereq.Host:
    """A macOS-shaped host with everything installed: brew, vz, no Linux kernel."""
    host = prereq.Host(
        native_windows=False,
        linux_kernel=False,
        wsl=False,
        arch="aarch64",
        tools={
            "podman": "/opt/homebrew/bin/podman",
            "limactl": "/opt/homebrew/bin/limactl",
            "git": "/usr/bin/git",
            "uv": "/opt/homebrew/bin/uv",
            "brew": "/opt/homebrew/bin/brew",
            "fy": "/Users/x/.local/bin/fy",
            "claude": "/Users/x/.local/bin/claude",
        },
        versions={"podman": "6.0.2", "limactl": "2.1.3"},
        vmtypes=["vz", "qemu"],
        kvm="",
        user_systemd=False,
        os_release={},
        disk_free_gib=200.0,
        mem_total_gib=32.0,
        mitmdump="/Users/x/.local/share/uv/tools/foldyard/bin/mitmdump",
        base=basevm.Status("ok", "baked 2 days ago (vz)"),
    )
    return replace(host, **over)


def _debian(**over) -> prereq.Host:
    """A Debian/Ubuntu x86-64 Linux host with everything installed, Lima from the tarball."""
    host = _mac(
        linux_kernel=True,
        arch="x86_64",
        tools={
            "podman": "/usr/bin/podman",
            "limactl": "/usr/local/bin/limactl",
            "git": "/usr/bin/git",
            "uv": "/home/x/.local/bin/uv",
            "fy": "/home/x/.local/bin/fy",
            "qemu-img": "/usr/bin/qemu-img",
            "qemu-system-x86_64": "/usr/bin/qemu-system-x86_64",
        },
        vmtypes=["qemu"],
        kvm="ok",
        user_systemd=True,
        os_release={"ID": "ubuntu", "ID_LIKE": "debian"},
    )
    return replace(host, **over)


def _by_name(rows) -> dict[str, prereq.Row]:
    return {r.name: r for r in rows}


def _levels(rows) -> dict[str, str]:
    return {r.name: r.level for r in rows}


# ── all present ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", [_mac(), _debian()], ids=["macos", "debian"])
def test_a_complete_host_has_no_failures(host):
    rows = prereq.rows(host)
    assert not [r for r in rows if r.level == "fail"], rows
    assert prereq.exit_code(rows) == 0


def test_linux_only_rows_appear_only_on_a_linux_kernel():
    mac = _by_name(prereq.rows(_mac()))
    linux = _by_name(prereq.rows(_debian()))
    for name in ("KVM", "systemd --user", "QEMU"):
        assert name not in mac and name in linux


# ── missing tools: a failure with the command that fixes it ──────────────────────────────


def test_missing_podman_on_macos_says_brew():
    rows = prereq.rows(_mac(tools={**_mac().tools, "podman": None}))
    row = _by_name(rows)["podman"]
    assert row.level == "fail" and row.fix == "brew install podman"
    assert prereq.exit_code(rows) == 1


def test_missing_podman_on_debian_says_apt():
    row = _by_name(prereq.rows(_debian(tools={**_debian().tools, "podman": None})))["podman"]
    assert row.fix == "sudo apt-get install podman"


def test_missing_podman_on_fedora_says_dnf():
    host = _debian(os_release={"ID": "fedora"}, tools={**_debian().tools, "podman": None})
    assert _by_name(prereq.rows(host))["podman"].fix == "sudo dnf install podman"


def test_missing_lima_on_linux_without_brew_is_the_release_tarball():
    row = _by_name(prereq.rows(_debian(tools={**_debian().tools, "limactl": None})))["Lima"]
    assert row.level == "fail"
    assert f"v{prereq.LIMA_TESTED}/lima-{prereq.LIMA_TESTED}-Linux-x86_64.tar.gz" in row.fix
    assert "sudo tar Cxz /usr/local" in row.fix


def test_an_unknown_distro_gets_a_pointer_not_a_guess():
    host = _debian(os_release={"ID": "gentoo"}, tools={**_debian().tools, "git": None})
    row = _by_name(prereq.rows(host))["git"]
    assert row.level == "fail" and "package manager" in row.fix


def test_lima_below_the_floor_fails():
    row = _by_name(prereq.rows(_mac(versions={"podman": "6.0.2", "limactl": "1.2.0"})))["Lima"]
    assert row.level == "fail" and prereq.LIMA_FLOOR in row.detail


def test_no_usable_hypervisor_fails():
    row = _by_name(prereq.rows(_mac(vmtypes=["krunkit"])))["hypervisor"]
    assert row.level == "fail"


# ── the Linux shape ──────────────────────────────────────────────────────────────────────


def test_qemu_needs_qemu_img_and_the_system_emulator():
    host = _debian(tools={**_debian().tools, "qemu-img": None})
    row = _by_name(prereq.rows(host))["QEMU"]
    assert row.level == "fail" and "qemu-utils" in row.fix


def test_no_kvm_device_fails_with_the_wsl2_hint_on_wsl2():
    row = _by_name(prereq.rows(_debian(kvm="absent", wsl=True)))["KVM"]
    assert row.level == "fail" and "nestedVirtualization" in row.fix


def test_kvm_without_access_says_the_group():
    row = _by_name(prereq.rows(_debian(kvm="no-access")))["KVM"]
    assert row.level == "fail" and "kvm" in row.fix and "usermod" in row.fix


def test_no_user_systemd_warns_with_linger():
    row = _by_name(prereq.rows(_debian(user_systemd=False)))["systemd --user"]
    assert row.level == "warn" and "loginctl enable-linger" in row.fix


# ── resources ────────────────────────────────────────────────────────────────────────────


def test_low_disk_warns():
    row = _by_name(prereq.rows(_mac(disk_free_gib=8.0)))["disk"]
    assert row.level == "warn"


def test_low_memory_warns_and_says_how_to_size_the_vm_down():
    row = _by_name(prereq.rows(_mac(mem_total_gib=8.0)))["memory"]
    assert row.level == "warn" and "--memory" in row.fix


# ── foldyard itself ──────────────────────────────────────────────────────────────────────


def test_running_without_an_installed_fy_says_how_to_install():
    # `uvx foldyard prereq` runs before foldyard is installed — the check says what's next.
    row = _by_name(prereq.rows(_mac(tools={**_mac().tools, "fy": None})))["foldyard"]
    assert row.level == "warn" and row.fix == "uv tool install foldyard"


def test_missing_mitmproxy_fails():
    row = _by_name(prereq.rows(_mac(mitmdump=None)))["egress proxy"]
    assert row.level == "fail"


def test_claude_on_the_host_is_optional():
    rows = prereq.rows(_mac(tools={**_mac().tools, "claude": None}))
    row = _by_name(rows)["Claude Code"]
    assert row.level == "optional" and "setup-token" in row.detail
    assert prereq.exit_code(rows) == 0


# ── the base VM row ──────────────────────────────────────────────────────────────────────


def test_no_base_vm_is_optional_and_points_at_fetch():
    st = basevm.Status("optional", "none", "`fy machine base bake` (or `fy prereq --fetch`)")
    rows = prereq.rows(_mac(base=st))
    assert _by_name(rows)["base VM"].level == "optional"
    assert prereq.exit_code(rows) == 0


def test_no_base_row_without_lima():
    rows = prereq.rows(_mac(tools={**_mac().tools, "limactl": None}, base=None))
    assert "base VM" not in _by_name(rows)


# ── native Windows: one row, pointing at WSL2 ────────────────────────────────────────────


def test_native_windows_is_one_row_pointing_at_wsl2():
    rows = prereq.rows(_mac(native_windows=True))
    assert len(rows) == 1 and rows[0].level == "fail" and "WSL2" in rows[0].fix


# ── rendering + --fetch ──────────────────────────────────────────────────────────────────


def test_render_puts_the_fix_under_the_row():
    out = prereq.render(prereq.rows(_mac(tools={**_mac().tools, "podman": None})))
    lines = out.splitlines()
    i = next(n for n, line in enumerate(lines) if "podman" in line and "✗" in line)
    assert "brew install podman" in lines[i + 1]


def test_fetch_refuses_while_a_required_tool_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(prereq, "gather", lambda: _mac(tools={**_mac().tools, "limactl": None}))
    monkeypatch.setattr(basevm, "bake", lambda *a, **k: pytest.fail("baked anyway"))
    assert prereq.run(fetch=True) == 1


def test_fetch_bakes_a_missing_base(monkeypatch):
    baked = []
    st = basevm.Status("optional", "none", "bake")
    monkeypatch.setattr(prereq, "gather", lambda: _mac(base=st))
    monkeypatch.setattr(basevm, "bake", lambda *a, **k: baked.append(True) or 0)
    assert prereq.run(fetch=True) == 0 and baked == [True]


def test_fetch_leaves_a_fresh_base_alone(monkeypatch, capsys):
    monkeypatch.setattr(prereq, "gather", lambda: _mac())
    monkeypatch.setattr(basevm, "bake", lambda *a, **k: pytest.fail("rebaked a fresh base"))
    assert prereq.run(fetch=True) == 0
    assert "fresh" in capsys.readouterr().out


def test_gather_runs_on_the_hermetic_path():
    # The suite's PATH has no podman/limactl: gather must degrade to "missing", never raise.
    host = prereq.gather()
    assert host.tools["limactl"] is None and host.base is None
