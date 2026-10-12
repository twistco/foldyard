"""`fy prereq` — what this computer needs before a first `fy init` / `fy box up`, and the command
that installs each missing piece. Checks only: foldyard prints the install line, you run it.

Project-free on purpose: it reads no `foldyard.toml` and runs no adopt gate, so it answers in an
empty directory, and before foldyard is even installed (`uvx foldyard prereq`). The one thing it
can't check is uv — it is what runs it — so uv (and, on Windows, WSL2) is the step before.

`--fetch` also does the big downloads now, while the network is good: it bakes the base VM
(:mod:`foldyard.basevm`), so a new project's VM is a clone in seconds rather than an image download
plus a package install plus a box build.

Capability detection, not platform detection, like the rest of foldyard (no ``sys.platform``):
the Linux rows appear when there is a Linux kernel (``/proc/sys/kernel/osrelease``), the install
hints follow the package manager that is THERE (brew, else ``/etc/os-release``). :func:`rows` is
pure over a :class:`Host` of gathered facts, so every host shape is testable without one.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import basevm, config

# Lima: the podman template declares `minimumLimaVersion: 2.0.0`; CI's host tier runs this one.
LIMA_FLOOR = "2.0.0"
LIMA_TESTED = "2.2.0"
DISK_WANT_GIB = 15  # a VM's real (sparse) usage + images + the base VM, with room to spare
MEM_WANT_GIB = 12  # `fy init`'s default VM is 8 GiB; the host keeps the rest

_GLYPH = {"ok": "✓", "fail": "✗", "warn": "⚠", "optional": "ℹ"}


@dataclass(frozen=True)
class Host:
    """Facts about this computer — everything :func:`rows` decides from."""

    native_windows: bool
    linux_kernel: bool
    wsl: bool
    arch: str  # x86_64 | aarch64 (normalised)
    tools: dict[str, str | None]
    versions: dict[str, str]
    vmtypes: list[str]
    kvm: str  # "" (not a Linux kernel) | ok | no-access | absent
    user_systemd: bool
    os_release: dict[str, str]
    disk_free_gib: float
    mem_total_gib: float
    mitmdump: str | None
    base: basevm.Status | None


@dataclass(frozen=True)
class Row:
    level: str  # ok | fail | warn | optional
    name: str
    detail: str
    fix: str = ""


# ── install hints: the package manager that is there ──────────────────────────────────────

# Package names per manager. A tool missing from a manager's column has no package there.
_PKGS: dict[str, dict[str, str]] = {
    "podman": {"brew": "podman", "apt": "podman", "dnf": "podman", "pacman": "podman"},
    "git": {"brew": "git", "apt": "git", "dnf": "git", "pacman": "git"},
    "lima": {"brew": "lima"},
    "qemu/x86_64": {
        "brew": "qemu",
        "apt": "qemu-system-x86 qemu-utils ovmf",
        "dnf": "qemu-system-x86-core qemu-img edk2-ovmf",
        "pacman": "qemu-system-x86 qemu-img edk2-ovmf",
    },
    "qemu/aarch64": {
        "brew": "qemu",
        "apt": "qemu-system-arm qemu-utils qemu-efi-aarch64",
        "dnf": "qemu-system-aarch64-core qemu-img edk2-aarch64",
        "pacman": "qemu-system-aarch64 qemu-img edk2-aarch64",
    },
}
_INSTALL = {
    "brew": "brew install {}",
    "apt": "sudo apt-get install {}",
    "dnf": "sudo dnf install {}",
    "pacman": "sudo pacman -S {}",
}
_DISTRO = {
    "debian": "apt",
    "ubuntu": "apt",
    "fedora": "dnf",
    "rhel": "dnf",
    "centos": "dnf",
    "arch": "pacman",
}


def _manager(host: Host) -> str:
    if host.tools.get("brew"):
        return "brew"
    ids = [host.os_release.get("ID", ""), *host.os_release.get("ID_LIKE", "").split()]
    return next((_DISTRO[i] for i in ids if i in _DISTRO), "")


def install_hint(tool: str, host: Host) -> str:
    """The one command that installs ``tool`` here, or a pointer when there isn't one."""
    manager = _manager(host)
    if tool == "lima" and manager != "brew":
        arch = host.arch
        return (
            f"curl -fsSL https://github.com/lima-vm/lima/releases/download/v{LIMA_TESTED}/"
            f"lima-{LIMA_TESTED}-Linux-{arch}.tar.gz | sudo tar Cxz /usr/local"
        )
    pkg = _PKGS.get(tool, {}).get(manager)
    if pkg:
        return _INSTALL[manager].format(pkg)
    return f"install {tool} with your package manager (`fy docs quickstart` lists the packages)"


# ── the rows ──────────────────────────────────────────────────────────────────────────────


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", v)[:3])


def _tool_row(name: str, tool: str, host: Host, why: str) -> Row:
    found = host.tools.get(tool)
    if found:
        version = host.versions.get(tool, "")
        return Row("ok", name, f"{version} ({found})" if version else found)
    return Row("fail", name, f"not installed — {why}", install_hint(name.lower(), host))


def rows(host: Host) -> list[Row]:
    if host.native_windows:
        return [
            Row(
                "fail",
                "platform",
                "Windows itself isn't a foldyard host; a Linux distro under WSL2 is",
                "install WSL2 (`wsl --install`), then uv and `uvx foldyard prereq` inside it",
            )
        ]
    out = [
        _tool_row("podman", "podman", host, "it drives the VM's container engine"),
        _lima_row(host),
        _hypervisor_row(host),
    ]
    if host.linux_kernel:
        out += [_qemu_row(host), _kvm_row(host), _systemd_row(host)]
    out += [
        _tool_row("git", "git", host, "a foldyard project is a git checkout"),
        _disk_row(host),
        _mem_row(host),
        _foldyard_row(host),
        _proxy_row(host),
        _claude_row(host),
    ]
    if host.base is not None:
        out.append(Row(host.base.level, "base VM", host.base.detail, host.base.fix))
    return out


def _lima_row(host: Host) -> Row:
    if not host.tools.get("limactl"):
        return Row("fail", "Lima", "not installed — it creates the VM", install_hint("lima", host))
    version = host.versions.get("limactl", "")
    if version and _version_tuple(version) < _version_tuple(LIMA_FLOOR):
        return Row(
            "fail",
            "Lima",
            f"{version} is older than {LIMA_FLOOR}, the oldest the podman template runs on",
            install_hint("lima", host),
        )
    return Row("ok", "Lima", version or "installed (version unknown)")


def _hypervisor_row(host: Host) -> Row:
    if not host.tools.get("limactl"):
        return Row("fail", "hypervisor", "unknown until Lima is installed")
    usable = [v for v in ("vz", "qemu") if v in host.vmtypes]
    if not usable:
        return Row(
            "fail",
            "hypervisor",
            f"Lima offers no vz or qemu here (it reports: {', '.join(host.vmtypes) or 'none'})",
            install_hint(f"qemu/{host.arch}", host),
        )
    return Row("ok", "hypervisor", usable[0])


def _qemu_row(host: Host) -> Row:
    missing = [t for t in ("qemu-img", f"qemu-system-{host.arch}") if not host.tools.get(t)]
    if missing:
        return Row(
            "fail",
            "QEMU",
            f"missing {', '.join(missing)} — Lima's VM driver on Linux",
            install_hint(f"qemu/{host.arch}", host),
        )
    return Row("ok", "QEMU", "qemu-img + qemu-system-" + host.arch)


def _kvm_row(host: Host) -> Row:
    if host.kvm == "ok":
        return Row("ok", "KVM", "/dev/kvm is usable")
    if host.kvm == "no-access":
        return Row(
            "fail",
            "KVM",
            "/dev/kvm exists but you can't open it",
            "sudo usermod -aG kvm $USER, then log out and back in",
        )
    fix = (
        "add `nestedVirtualization=true` under [wsl2] in %USERPROFILE%\\.wslconfig, then"
        " `wsl --shutdown`"
        if host.wsl
        else "enable virtualisation (VT-x/AMD-V) in the firmware; on a cloud VM, nested virt"
    )
    return Row("fail", "KVM", "no /dev/kvm — the VM can't be hardware-accelerated", fix)


def _systemd_row(host: Host) -> Row:
    if host.user_systemd:
        return Row("ok", "systemd --user", "running")
    return Row(
        "warn",
        "systemd --user",
        "no user manager in this session — the VM's scope and the host firewall need one",
        "loginctl enable-linger $USER",
    )


def _disk_row(host: Host) -> Row:
    free = f"{host.disk_free_gib:.0f} GiB free where Lima keeps VMs"
    if host.disk_free_gib < DISK_WANT_GIB:
        return Row("warn", "disk", f"{free}; want {DISK_WANT_GIB}+", "free some disk space")
    return Row("ok", "disk", free)


def _mem_row(host: Host) -> Row:
    total = f"{host.mem_total_gib:.0f} GiB"
    if host.mem_total_gib < MEM_WANT_GIB:
        return Row(
            "warn",
            "memory",
            f"{total} — `fy init`'s default VM takes 8 GiB of it",
            "size the VM down: `fy init --memory 4096` (or memory_mib in foldyard.local.toml)",
        )
    return Row("ok", "memory", total)


def _foldyard_row(host: Host) -> Row:
    from . import __version__

    if found := host.tools.get("fy"):
        return Row("ok", "foldyard", f"{__version__} ({found})")
    return Row(
        "warn", "foldyard", f"{__version__}, but no `fy` on PATH", "uv tool install foldyard"
    )


def _proxy_row(host: Host) -> Row:
    if host.mitmdump:
        return Row("ok", "egress proxy", f"mitmdump ({host.mitmdump})")
    from .plugins.proxy import host_install_hint

    return Row(
        "fail",
        "egress proxy",
        "mitmproxy missing from foldyard's install — the box's only way out",
        host_install_hint(),
    )


def _claude_row(host: Host) -> Row:
    if found := host.tools.get("claude"):
        return Row("ok", "Claude Code", found)
    return Row(
        "optional",
        "Claude Code",
        'on your computer only for [claude] keyless = "oauth" (`claude setup-token`)',
        "curl -fsSL https://claude.ai/install.sh | bash",
    )


def exit_code(rs: list[Row]) -> int:
    return 1 if any(r.level == "fail" for r in rs) else 0


def render(rs: list[Row]) -> str:
    width = max(len(r.name) for r in rs)
    lines = ["Prerequisites — this computer"]
    for r in rs:
        lines.append(f"  {_GLYPH[r.level]} {r.name:<{width}}  {r.detail}")
        if r.fix and r.level != "ok":
            lines.append(f"    {'':<{width}}  → {r.fix}")
    return "\n".join(lines)


# ── gathering the facts (the I/O half) ────────────────────────────────────────────────────

_TOOLS = ("podman", "limactl", "git", "uv", "brew", "fy", "claude", "qemu-img")


def _version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"(\d+\.\d+\.\d+)", out or "")
    return m.group(1) if m else ""


def _os_release() -> dict[str, str]:
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip().strip('"').lower()
    return out


def _lima_home() -> Path:
    home = Path(os.environ.get("LIMA_HOME") or Path.home() / ".lima")
    return next((p for p in (home, *home.parents) if p.exists()), Path.home())


def _mem_total_gib() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        return 0.0


def _kvm(linux_kernel: bool) -> str:
    if not linux_kernel:
        return ""
    dev = Path("/dev/kvm")
    if not dev.exists():
        return "absent"
    return "ok" if os.access(dev, os.R_OK | os.W_OK) else "no-access"


def gather() -> Host:
    """Read this computer's facts. Never raises on a missing tool — missing IS the answer."""
    from .machine_backend import LimaBackend
    from .plugins.proxy import mitmdump_path

    osrelease = Path("/proc/sys/kernel/osrelease")
    linux_kernel = osrelease.exists()
    wsl = linux_kernel and "microsoft" in osrelease.read_text().lower()
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    tools = {t: shutil.which(t) for t in (*_TOOLS, f"qemu-system-{arch}")}
    versions = {}
    if tools["podman"]:
        versions["podman"] = _version(["podman", "--version"])
    if tools["limactl"]:
        versions["limactl"] = _version(["limactl", "--version"])
    lima = LimaBackend()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    return Host(
        native_windows=os.name == "nt",
        linux_kernel=linux_kernel,
        wsl=wsl,
        arch=arch,
        tools=tools,
        versions=versions,
        vmtypes=lima.available_vmtypes() if tools["limactl"] else [],
        kvm=_kvm(linux_kernel),
        user_systemd=bool(runtime) and (Path(runtime) / "systemd").is_dir(),
        os_release=_os_release(),
        disk_free_gib=shutil.disk_usage(_lima_home()).free / 2**30,
        mem_total_gib=_mem_total_gib(),
        mitmdump=mitmdump_path(),
        base=basevm.status(lima) if tools["limactl"] else None,
    )


def run(fetch: bool = False) -> int:
    """`fy prereq [--fetch]`."""
    if config.in_box():
        print("ℹ `fy prereq` checks your computer, not the box — run it there.")
        return 0
    host = gather()
    rs = rows(host)
    # Flushed: --fetch's bake streams limactl's and the build's own output after it.
    print(render(rs), flush=True)
    if not fetch:
        if exit_code(rs) == 0 and host.base is not None and host.base.level != "ok":
            print("\n  `fy prereq --fetch` does the big downloads now (the base VM).")
        return exit_code(rs)
    print()
    if exit_code(rs):
        print("✗ fix the ✗ rows above first — --fetch needs Lima, podman and a hypervisor.")
        return 1
    if host.base is not None and host.base.level == "ok":
        print("✓ the base VM is fresh — nothing to fetch.")
        return 0
    return basevm.bake()
