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

**How :mod:`foldyard.machine` wires it (``[machine].host_wall = true``).** The VM is STARTED
inside its own transient scope (:func:`scoped_argv_prefix` — ``systemd-run --user --scope``), so
limactl, the hostagent and QEMU all land in ``…/app.slice/fy-machine-<vm>.scope`` and nothing
else does; after every start, and again on each steady-state ``fy up``, the wall is rendered for
the scope the VM ACTUALLY sits in (:func:`vm_cgroup_scope`) and loaded as root (idempotent
replace). A VM found outside its own scope — started by hand, or before the option was turned
on — is refused, because matching the login session's scope instead would wall the operator's
whole shell (:func:`in_own_scope` is that guard). The forwarded SSH port is re-read each time:
Lima allocates it per boot. Root is ``sudo nft``; a passwordless sudoers rule for ``nft`` is the
operator's call and makes it silent. Stdlib only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

# QEMU's slirp forwards the guest's DNS to the host's configured resolvers, so the wall must let
# the VM process reach them: read from resolv.conf (:func:`resolvers`), with the systemd-resolved
# stub every mainstream distro uses as the fallback when the file is unreadable or names none.
RESOLV_CONF = Path("/etc/resolv.conf")
DEFAULT_RESOLVERS = ("127.0.0.53",)
PROC = Path("/proc")

_SPAN = 89  # the worktree-offset span each daemon band covers (main is +0; ports.py)


def table_name(vm: str) -> str:
    """The nftables table for this VM — per-VM so two projects' walls never collide, and named
    so ``nft list ruleset`` says whose it is. nftables identifiers allow only ``[A-Za-z0-9_./]``,
    so the VM name's other characters collapse to ``_``."""
    safe = "".join(c if c.isalnum() or c in "_./" else "_" for c in vm)
    return f"fy_host_wall_{safe}"


def scope_unit(vm: str) -> str:
    """The transient systemd scope the VM is started in — per-VM, so two projects' VMs never
    share a cgroup (the wall would otherwise match both). systemd unit names allow
    ``[A-Za-z0-9:_.\\-]``; anything else in the VM name collapses to ``_``."""
    safe = "".join(c if c.isalnum() or c in "_.-:" else "_" for c in vm)
    return f"fy-machine-{safe}.scope"


def scoped_argv_prefix(vm: str) -> list[str]:
    """Prefix for the backend's start argv that runs it — and everything it forks: Lima's
    hostagent, QEMU, the SSH mux — inside :func:`scope_unit`. ``--scope`` keeps the command in
    the foreground (limactl's own output and exit code are unchanged); the scope outlives the
    command while any child lives, which is exactly the VM's lifetime. ``--collect`` garbage-
    collects a failed scope so a retry never trips over "unit already exists"."""
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit", scope_unit(vm)]


def in_own_scope(vm: str, scope: str) -> bool:
    """Is ``scope`` (a cgroup path as :func:`vm_cgroup_scope` reports it) THIS VM's own scope?
    The wall matches every socket in the scope, so a VM that landed anywhere else — the login
    session's scope, a sibling VM's — must be refused, not walled: matching the session would
    reject the operator's own egress."""
    parts = [p for p in scope.strip("/").split("/") if p]
    return bool(parts) and parts[-1] == scope_unit(vm)


def resolvers() -> tuple[str, ...]:
    """The host's DNS resolvers from :data:`RESOLV_CONF` (``nameserver`` lines, in order), or
    :data:`DEFAULT_RESOLVERS` when the file is unreadable or names none."""
    try:
        lines = RESOLV_CONF.read_text().splitlines()
    except OSError:
        return DEFAULT_RESOLVERS
    found = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            found.append(fields[1])
    return tuple(found) or DEFAULT_RESOLVERS


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


def _kernel_config_paths() -> tuple[Path, ...]:
    """Where a Linux kernel publishes its build config, in preference order: ``/proc/config.gz``
    (``CONFIG_IKCONFIG_PROC`` — the WSL2 kernel has it), then the distro's
    ``/boot/config-<release>`` (Debian/Ubuntu/Fedora). Neither is guaranteed; the caller treats
    absence as unknown."""
    return (Path("/proc/config.gz"), Path(f"/boot/config-{os.uname().release}"))


def nft_socket_in_kernel() -> bool | None:
    """Whether the running kernel was built with nftables' ``socket`` expression
    (``CONFIG_NFT_SOCKET``, ``=y`` or ``=m``) — the match :func:`render` needs for
    ``socket cgroupv2``. A kernel without it refuses the rule with ENOENT at load time; the stock
    WSL2 kernel is one (``# CONFIG_NFT_SOCKET is not set`` on its 6.6 and 6.18 branches). Read
    from the first readable kernel config; ``None`` when no config is readable or none mentions
    the symbol — unknown, never a guess, so preflight refuses only on a definite ``False`` and
    the load-time error (:func:`explain_load_failure`) covers the rest."""
    import gzip

    for path in _kernel_config_paths():
        try:
            raw = path.read_bytes()
            text = (gzip.decompress(raw) if path.suffix == ".gz" else raw).decode(
                "utf-8", "replace"
            )
        except (OSError, EOFError, ValueError):
            continue
        for line in text.splitlines():
            if line.startswith("CONFIG_NFT_SOCKET="):
                return line.split("=", 1)[1] in ("y", "m")
            if line.startswith("# CONFIG_NFT_SOCKET is not set"):
                return False
    return None


def explain_load_failure(stderr: str) -> str:
    """The operator-facing reason behind an ``nft -f -`` failure, when nft's own words identify
    one foldyard knows: ENOENT at the ``socket cgroupv2`` rule is a kernel without
    ``CONFIG_NFT_SOCKET`` (nf_tables reports a missing expression as "No such file or directory"
    — nothing to do with a file). Empty for any other error: nft's stderr, printed beside this,
    is then the whole story."""
    if "No such file or directory" in stderr and "socket cgroupv2" in stderr:
        return (
            "  This kernel has no nftables `socket` expression (CONFIG_NFT_SOCKET is not set —\n"
            "  the stock WSL2 kernel, for one), so the host wall's `socket cgroupv2` match can\n"
            "  never load here. Use a kernel built with nft_socket, or drop `host_wall` (the\n"
            "  in-VM wall still applies)."
        )
    return ""


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
        text = (PROC / str(pid) / "cgroup").read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        # Unified v2 lines are `0::/the/path`; ignore any legacy v1 controller lines.
        if line.startswith("0::"):
            return line[3:].strip().lstrip("/")
    return ""


# /proc/net/{tcp,udp} socket states: a TCP listener is 0A (LISTEN); a bound, unconnected UDP
# socket — which is what a UDP "listener" is — shows as 07 (TCP_CLOSE).
_LISTENING = {"tcp": "0A", "udp": "07"}
# Loopback in /proc/net's hex little-endian notation: 127.0.0.1 and ::1.
_LOOPBACK = {"0100007F", "00000000000000000000000001000000"}


def listener_ports(*pids: int) -> tuple[tuple[str, int], ...]:
    """The LOOPBACK listeners the VM's own host processes hold, as ``(proto, port)`` — the
    plumbing the wall must leave open on ``lo`` for the VM to work at all: Lima's host resolver
    (the hostagent serves the guest's DNS on a random loopback udp+tcp port and QEMU forwards
    each query there — measured on the rig, it is where a wall without this rule cut DNS) and
    QEMU's SSH ``hostfwd``. Matched by socket inode: ``/proc/<pid>/fd`` → ``/proc/net/*``. Only
    loopback-bound sockets count — QEMU's outbound UDP sockets are bound to ``0.0.0.0`` and are
    not listeners. A gone pid or an unreadable table contributes nothing."""
    inodes: set[str] = set()
    for pid in pids:
        try:
            fds = list((PROC / str(pid) / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(target[8:-1])
    found: set[tuple[str, int]] = set()
    for proto in ("tcp", "udp"):
        for table in (proto, f"{proto}6"):
            try:
                rows = (PROC / "net" / table).read_text().splitlines()[1:]
            except OSError:
                continue
            for row in rows:
                f = row.split()
                if len(f) < 10 or f[9] not in inodes or f[3] != _LISTENING[proto]:
                    continue
                addr, _, port = f[1].partition(":")
                if addr in _LOOPBACK:
                    found.add((proto, int(port, 16)))
    return tuple(sorted(found))


def render(
    vm: str,
    scope: str,
    ssh_port: int,
    resolvers: tuple[str, ...] = DEFAULT_RESOLVERS,
    plumbing: tuple[tuple[str, int], ...] = (),
) -> str:
    """The nftables ruleset that walls the VM whose QEMU process sits in cgroup ``scope``.

    ``ssh_port`` is Lima's forwarded guest-SSH port on the host loopback (``limactl shell``, the
    hostagent's own session and the socket forward must keep working — Lima allocates it per
    boot, so render after each start); ``plumbing`` is :func:`listener_ports` — the VM's other
    loopback listeners, the hostagent's DNS above all; the daemon bands come from config. Nothing
    off-loopback is opened but the resolvers' ``:53``. The ``delete table``
    before the ``table`` block makes the whole file idempotent — a re-apply replaces the table
    atomically rather than erroring on the existing one or stacking a second copy."""
    level = cgroup_level(scope)
    tcp_extra = [str(p) for proto, p in plumbing if proto == "tcp" and p != ssh_port]
    ports = ", ".join([str(ssh_port), *_allowed_ports(), *tcp_extra])
    udp_ports = ", ".join(str(p) for proto, p in plumbing if proto == "udp")
    udp_rule = f'    oif "lo" udp dport {{ {udp_ports} }} accept\n' if udp_ports else ""
    name = table_name(vm)
    dns = []
    for family, addrs in (
        ("ip", [r for r in resolvers if ":" not in r]),
        ("ip6", [r for r in resolvers if ":" in r]),
    ):
        if addrs:  # nft rejects an empty set, so a family with no resolver renders nothing
            dns.append(f"    {family} daddr {{ {', '.join(addrs)} }} udp dport 53 accept")
            dns.append(f"    {family} daddr {{ {', '.join(addrs)} }} tcp dport 53 accept")
    dns_rules = "\n".join(dns)
    return f"""\
{_declare_then_delete(name)}table inet {name} {{
  chain output {{
    type filter hook output priority filter; policy accept;
    socket cgroupv2 level {level} "{scope}" jump vm
  }}
  chain vm {{
    ct state established,related accept
    oif "lo" tcp dport {{ {ports} }} accept
{udp_rule}{dns_rules}
    meta l4proto tcp counter reject with tcp reset
    counter reject with icmpx type admin-prohibited
  }}
}}
"""


def _declare_then_delete(name: str) -> str:
    """The nft idiom for a no-op-safe delete: declaring the table first means the delete can't
    miss, whether or not the table exists."""
    return f"table inet {name}\ndelete table inet {name}\n"


def install_argv() -> list[str]:
    """The command that loads a ruleset from stdin as root. Streamed over stdin (never a temp
    file) for the same reason the guest wall is: no path a lesser-privileged process could swap
    between write and root-load."""
    return ["sudo", "nft", "-f", "-"]


@dataclass(frozen=True)
class LoadResult:
    """What loading the ruleset came to: ``ok``, and nft's stderr when it did not — kept so the
    caller can print nft's own words and :func:`explain_load_failure` can read them."""

    ok: bool
    stderr: str = ""


def install(
    vm: str,
    scope: str,
    ssh_port: int,
    resolvers: tuple[str, ...] = DEFAULT_RESOLVERS,
    plumbing: tuple[tuple[str, int], ...] = (),
) -> LoadResult:
    """Load the VM's host wall. Not ``ok`` if the host can't enforce one or ``nft`` rejects the
    ruleset (its stderr rides along). Caller decides WHEN (VM start) and whether the operator
    consented to host nftables."""
    if not available() or not scope:
        return LoadResult(False)
    ruleset = render(vm, scope, ssh_port, resolvers, plumbing)
    try:
        res = subprocess.run(install_argv(), input=ruleset, text=True, capture_output=True)
    except OSError as exc:
        # The loader itself could not launch (no `sudo` on PATH — available() vouches for nft
        # only): declined with the OS's reason, the same fail-closed shape as a rejected ruleset.
        return LoadResult(False, str(exc))
    return LoadResult(res.returncode == 0, res.stderr or "")


def remove(vm: str) -> bool:
    """Tear down the VM's host wall — idempotent (a table that is already gone is a clean no-op:
    ``nft delete table`` alone would error, so this streams the declare-then-delete pair).
    False when the host has no nft at all or the load fails."""
    if shutil.which("nft") is None:
        return False
    ruleset = _declare_then_delete(table_name(vm))
    try:
        return subprocess.run(install_argv(), input=ruleset, text=True).returncode == 0
    except OSError:
        return False  # no `sudo` to launch — the caller's "warn, inert without the VM" case
