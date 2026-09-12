#!/usr/bin/env python3
"""Host-side egress wall for the machine VM — the zero-cost hardening step above ``③`` on a
LINUX host (see docs/isolation-layers.md, "Host-side wall enforcement on Linux").

The in-VM wall (:mod:`foldyard.machine` + ``assets/machine-wall/machine-wall.sh``) is
enforcement the guest applies to itself: it holds against the box and against a container escape
that lands as the VM user, but a guest-KERNEL exploit that reaches VM-root can flush it. This
module closes that last gap on Linux by matching the VM's OWN traffic on the HOST — where the
guest has no reach at all — with nftables, and allowing only the proxy band. Flushing the guest
wall then gains nothing: the packets still have to leave through the host, and the host rejects
everything but the daemon band.

**Why the VM process, matched by cgroup.** Lima's QEMU driver runs the guest's user-mode network
inside the ``qemu-system`` process, so every packet the guest emits leaves the host as that
process's own traffic (guest→host forwards land on the host loopback; external egress leaves as
QEMU). Matching that process by its **cgroup v2 scope** — not by uid — is what makes this precise:
the operator's other work shares their uid, but only the VM lives in the VM's scope. The
proven ruleset (rig, 2026-09-11):

    table inet fy_host_wall_<vm>
      output hook: socket cgroupv2 level <n> "<scope>" jump vm
      vm: established/related accept
          oif lo tcp dport { <ssh port>, <band ranges> } accept   # limactl + the proxy band
          ip daddr <resolver> udp/tcp dport 53 accept              # QEMU's slirp DNS
          reject (tcp reset for tcp, admin-prohibited otherwise)

**Why LINUX only, and why this is not a ``sys.platform`` branch.** The mechanism is nftables
plus cgroup-v2 socket matching; :func:`available` asks whether the host HAS those, the same
"what can this host do?" question the backend's vmtype resolution asks — not "is this a Mac?".
On macOS Lima's user-mode network also runs as the operator, but pf cannot single that process
out without a dedicated uid or a different network mode, so the host wall is a Linux capability,
reported absent elsewhere rather than branched away.

**Not auto-wired into ``fy up`` (yet).** Placing QEMU in a stable, per-VM cgroup scope (via
``systemd-run --user --scope``) and the host-root policy for loading nftables are launch-path and
operator-consent decisions this module deliberately leaves to its caller; :func:`vm_cgroup_scope`
discovers wherever the VM actually landed so the wall can match it either way. Stdlib only.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import config

# The systemd-resolved stub every mainstream Linux distro puts in /etc/resolv.conf; QEMU's slirp
# forwards the guest's DNS here on the host, so the wall must let the VM process reach it.
DEFAULT_RESOLVERS = ("127.0.0.53",)

_SPAN = 89  # the worktree-offset span each daemon band covers (main is +0; ports.py)


def table_name(vm: str) -> str:
    """The nftables table for this VM — per-VM so two projects' walls never collide, and named
    so ``nft list ruleset`` says whose it is. nftables identifiers allow only ``[A-Za-z0-9_./]``,
    so the VM name's other characters collapse to ``_``."""
    safe = "".join(c if c.isalnum() or c in "_./" else "_" for c in vm)
    return f"fy_host_wall_{safe}"


def available() -> bool:
    """True when this host can enforce a host-side wall: ``nft`` on PATH and a cgroup-v2
    hierarchy to match against. A capability probe, not a platform test — it answers False on a
    host with neither (macOS, or a Linux box without nftables) without ever naming an OS."""
    if shutil.which("nft") is None:
        return False
    try:
        # cgroup2 mounts /sys/fs/cgroup as a single unified hierarchy; the marker file only the
        # v2 layout carries is the cheapest positive check that doesn't depend on our own cgroup.
        return (Path("/sys/fs/cgroup") / "cgroup.controllers").exists()
    except OSError:
        return False


def _allowed_ports() -> list[str]:
    """The loopback dports the VM process may reach: THIS project's daemon bands (proxy + minter,
    each covering the full ``base..base+89`` worktree span), as nft range elements. Bases are the
    project's allocated band (or an env override), so a walled VM reaches only its own project's
    daemons — never a sibling project's, exactly as the in-VM wall scopes them."""
    bases = {config.proxy_port_base(), config.gcp_minter_port_base()}
    return [f"{b}-{b + _SPAN}" for b in sorted(bases)]


def cgroup_level(scope: str) -> int:
    """The cgroup-v2 level of ``scope`` — its component count. nftables' ``socket cgroupv2
    level N "path"`` compares the socket's Nth-level ancestor, and the root is level 0, so a
    5-component leaf like ``user.slice/…/fy-machine-x.scope`` is level 5."""
    return len([p for p in scope.strip("/").split("/") if p])


def vm_cgroup_scope(pid: int) -> str:
    """The cgroup-v2 path (no leading ``/``) the VM process ``pid`` lives in, read from
    ``/proc/<pid>/cgroup``. Empty when the pid is gone or the line is unreadable. Discovery, not
    prescription: the wall matches wherever Lima/systemd actually put QEMU, whether that is a
    dedicated ``fy-machine-<vm>.scope`` or the login session's own scope."""
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        # Unified v2 lines are `0::/the/path`; ignore any legacy v1 controller lines.
        if line.startswith("0::"):
            return line[3:].strip().lstrip("/")
    return ""


def render(
    vm: str,
    scope: str,
    ssh_port: int,
    resolvers: tuple[str, ...] = DEFAULT_RESOLVERS,
) -> str:
    """The nftables ruleset that walls the VM whose QEMU process sits in cgroup ``scope``.

    ``ssh_port`` is Lima's forwarded guest-SSH port on the host loopback (``limactl shell`` and
    the socket forward must keep working); the daemon bands come from config. The ``delete table``
    before the ``table`` block makes the whole file idempotent — a re-apply replaces the table
    atomically rather than erroring on the existing one or stacking a second copy."""
    level = cgroup_level(scope)
    ports = ", ".join([str(ssh_port), *_allowed_ports()])
    daddr = ", ".join(resolvers)
    name = table_name(vm)
    return f"""\
table inet {name}
delete table inet {name}
table inet {name} {{
  chain output {{
    type filter hook output priority filter; policy accept;
    socket cgroupv2 level {level} "{scope}" jump vm
  }}
  chain vm {{
    ct state established,related accept
    oif "lo" tcp dport {{ {ports} }} accept
    ip daddr {{ {daddr} }} udp dport 53 accept
    ip daddr {{ {daddr} }} tcp dport 53 accept
    meta l4proto tcp counter reject with tcp reset
    counter reject with icmpx type admin-prohibited
  }}
}}
"""


def install_argv() -> list[str]:
    """The command that loads a ruleset from stdin as root. Streamed over stdin (never a temp
    file) for the same reason the guest wall is: no path a lesser-privileged process could swap
    between write and root-load."""
    return ["sudo", "nft", "-f", "-"]


def remove_argv(vm: str) -> list[str]:
    """The command that tears this VM's wall down — root, and a no-op-safe delete."""
    return ["sudo", "nft", "delete", "table", "inet", table_name(vm)]


def install(
    vm: str, scope: str, ssh_port: int, resolvers: tuple[str, ...] = DEFAULT_RESOLVERS
) -> bool:
    """Load the VM's host wall. False if the host can't enforce one or ``nft`` rejects the
    ruleset. Caller decides WHEN (VM start) and whether the operator consented to host nftables."""
    if not available() or not scope:
        return False
    ruleset = render(vm, scope, ssh_port, resolvers)
    return subprocess.run(install_argv(), input=ruleset, text=True).returncode == 0


def remove(vm: str) -> bool:
    """Tear down the VM's host wall (idempotent-ish: a missing table is the caller's to tolerate).
    False when the host has no nft at all."""
    if shutil.which("nft") is None:
        return False
    return subprocess.run(remove_argv(vm)).returncode == 0
