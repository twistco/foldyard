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
QEMU). Matching that process by its **cgroup v2 slice** — not by uid — is what makes this precise:
the operator's other work shares their uid, but only the VM lives under the VM's slice. The
ruleset (the match proven on the rig, 2026-09-11; the boot-stable loopback form on a GitHub
ubuntu-24.04 runner, 2026-09-18):

    table inet fy_host_wall_<vm>
      output hook: socket cgroupv2 level <n> "<slice>" jump vm
      vm: established/related accept
          udp/tcp dport 53 accept                 # the hostagent resolves for the guest
          oif lo ct mark set <mark> accept        # loopback: allowed OUT, judged on INPUT
          reject (tcp reset for tcp, admin-prohibited otherwise)
      input hook: iif lo ct mark <mark> jump lo
      lo: established/related accept
          socket cgroupv2 level <n> "<slice>" accept   # the receiver is the VM's own plumbing
          tcp dport { <band ranges> } accept           # …or this project's daemons
          reject

**Why loopback is judged by the RECEIVER.** The VM's host-side plumbing — the hostagent's DNS
resolver, QEMU's SSH forward — listens on loopback ports Lima picks per boot. Naming them would
make the table a per-boot artefact; instead every loopback flow the VM opens is marked (a ct mark
set on OUTPUT rides the connection to INPUT), and on INPUT the same ``socket cgroupv2`` match is
asked of the LISTENING socket: in the VM's own slice (its plumbing) or on the project's daemon
band, and nothing else — the operator's local services stay out of reach. The mark is the
project's band base under foldyard's byte (:func:`ct_mark`), so two projects' tables never judge
each other's flows. Nothing in the rendered table comes from a
running VM or from the network: it is a function of the VM name, its slice and the project's
bands, which is what lets it be applied ONCE and stay valid.

**Why a SLICE, and what the rule actually holds.** ``socket cgroupv2`` compiles the path to a
cgroup ID at load time — the cgroup must exist then, and a cgroup destroyed and recreated gets
a new ID the loaded rule no longer matches (silently: fail-OPEN). A transient scope dies with
its last process, so it is the wrong thing to bind to; a slice survives being emptied, so the
VM runs in its scope UNDER a per-VM slice and the table matches the slice — the same ID across
every VM restart. A host reboot is a new ID: whether the loaded table still bites is therefore
a thing to PROBE, never assume.

**Why LINUX only, and why this is not a ``sys.platform`` branch.** The mechanism is nftables
plus cgroup-v2 socket matching; :func:`available` asks whether the host HAS those, the same
"what can this host do?" question the backend's vmtype resolution asks — not "is this a Mac?".
On macOS Lima's user-mode network also runs as the operator, but pf cannot single that process
out without a dedicated uid or a different network mode, so the host wall is a Linux capability,
reported absent elsewhere rather than branched away.

**How :mod:`foldyard.machine` wires it (``[machine].host_wall = true``).** The VM is STARTED
inside its own transient scope under its own slice (:func:`scoped_argv_prefix` —
``systemd-run --user --scope --slice``), so limactl, the hostagent and QEMU all land in
``…/fy.slice/fy-machine-<vm>.slice/fy-machine-<vm>.scope`` and nothing else does; after every
start, and again on each steady-state ``fy up``, the wall is rendered for the slice the VM
ACTUALLY sits under (:func:`vm_cgroup_scope` → :func:`vm_slice`) and loaded as root (idempotent
replace). A VM found outside its own scope-under-slice — started by hand, or before the option
was turned on — is refused, because matching the login session's scope instead would wall the
operator's whole shell (:func:`in_own_scope` is that guard). Root is ``sudo nft``; a passwordless
sudoers rule for ``nft`` is the operator's call and makes it silent. Stdlib only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

PROC = Path("/proc")
# The top byte of every foldyard ct mark — a namespace, so the per-project value below can
# never collide with a mark another tool on the host sets (firewalld, Docker, a VPN client).
_MARK_BYTE = 0xF4

_SPAN = 89  # the worktree-offset span each daemon band covers (main is +0; ports.py)


def table_name(vm: str) -> str:
    """The nftables table for this VM — per-VM so two projects' walls never collide, and named
    so ``nft list ruleset`` says whose it is. nftables identifiers allow only ``[A-Za-z0-9_./]``,
    so the VM name's other characters collapse to ``_``."""
    safe = "".join(c if c.isalnum() or c in "_./" else "_" for c in vm)
    return f"fy_host_wall_{safe}"


def _unit_safe(vm: str) -> str:
    """systemd unit names allow ``[A-Za-z0-9:_.\\-]``; anything else in the VM name collapses
    to ``_``."""
    return "".join(c if c.isalnum() or c in "_.-:" else "_" for c in vm)


def scope_unit(vm: str) -> str:
    """The transient systemd scope the VM is started in — per-VM, so two projects' VMs never
    share a cgroup (the wall would otherwise match both)."""
    return f"fy-machine-{_unit_safe(vm)}.scope"


def slice_unit(vm: str) -> str:
    """The per-VM slice the scope runs under, and the cgroup the wall MATCHES. systemd nests
    it by its dashes: ``…/user@<uid>.service/fy.slice/fy-machine-<vm>.slice``. A slice survives
    being emptied (the VM stopped), so the cgroup ID a loaded table holds stays the same across
    every restart — the transient scope, gone with its last process, would not."""
    return f"fy-machine-{_unit_safe(vm)}.slice"


def scoped_argv_prefix(vm: str) -> list[str]:
    """Prefix for the backend's start argv that runs it — and everything it forks: Lima's
    hostagent, QEMU, the SSH mux — inside :func:`scope_unit` under :func:`slice_unit`.
    ``--scope`` keeps the command in the foreground (limactl's own output and exit code are
    unchanged); the scope outlives the command while any child lives, which is exactly the VM's
    lifetime. ``--collect`` garbage-collects a failed scope so a retry never trips over "unit
    already exists". ``--slice`` creates the slice if it does not exist yet."""
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--slice",
        slice_unit(vm),
        "--unit",
        scope_unit(vm),
    ]


def in_own_scope(vm: str, scope: str) -> bool:
    """Is ``scope`` (a cgroup path as :func:`vm_cgroup_scope` reports it) THIS VM's own scope,
    under its own slice? The wall matches every socket under the slice, so a VM that landed
    anywhere else — the login session's scope, a sibling VM's, its own scope but unsliced (an
    older foldyard started it) — must be refused, not walled: matching the session would reject
    the operator's own egress, and a slice the VM is not under would match nothing."""
    parts = [p for p in scope.strip("/").split("/") if p]
    return len(parts) >= 2 and parts[-1] == scope_unit(vm) and parts[-2] == slice_unit(vm)


def vm_slice(scope: str) -> str:
    """The slice a VM scope path sits under — its parent cgroup — or empty when there is none."""
    parts = [p for p in scope.strip("/").split("/") if p]
    return "/".join(parts[:-1])


def ct_mark() -> int:
    """The conntrack mark that tags this project's VM loopback flows between the OUTPUT hook
    (set by the sender's slice) and the INPUT hook (judged by the receiver). Per project — two
    projects' tables both hook INPUT, each jumping on ITS mark; a shared one would let project
    A's table judge B's flows (and reject them: B's slice is not A's, so B's VM would lose its
    own plumbing with nothing naming the cause). The value is the project's proxy band base
    under foldyard's byte: a port number, but chosen because :mod:`foldyard.ports` already
    allocates it unique per project on this host — uniqueness by construction, where a hash of
    the VM name would be a collision nothing could detect or explain."""
    return (_MARK_BYTE << 24) | config.proxy_port_base()


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


def render(vm: str, slice_path: str) -> str:
    """The nftables ruleset that walls the VM whose processes run under cgroup ``slice_path``.

    A pure function of the VM name, its slice and the project's daemon bands (from config) —
    nothing from a running VM (Lima's forwarded SSH port, the hostagent's listener ports) and
    nothing from the network (resolvers), so the same text is valid across every boot of the VM.
    Loopback is allowed out under the VM's :func:`ct_mark` and judged on INPUT by the receiving
    socket (the module docstring has the why); DNS goes to any resolver, both transports. The
    ``delete table`` before the ``table`` block makes the whole file idempotent — a re-apply
    replaces the table atomically rather than erroring on the existing one or stacking a second
    copy."""
    level = cgroup_level(slice_path)
    bands = ", ".join(_allowed_ports())
    name = table_name(vm)
    mark = f"{ct_mark():#010x}"
    return f"""\
{_declare_then_delete(name)}table inet {name} {{
  chain output {{
    type filter hook output priority filter; policy accept;
    socket cgroupv2 level {level} "{slice_path}" jump vm
  }}
  chain vm {{
    ct state established,related accept
    udp dport 53 accept
    tcp dport 53 accept
    oif "lo" ct mark set {mark} accept
    meta l4proto tcp counter reject with tcp reset
    counter reject with icmpx type admin-prohibited
  }}
  chain input {{
    type filter hook input priority filter; policy accept;
    iif "lo" ct mark {mark} jump lo
  }}
  chain lo {{
    ct state established,related accept
    socket cgroupv2 level {level} "{slice_path}" accept
    tcp dport {{ {bands} }} accept
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


def install(vm: str, slice_path: str) -> LoadResult:
    """Load the VM's host wall for the slice it runs under. Not ``ok`` if the host can't enforce
    one or ``nft`` rejects the ruleset (its stderr rides along). Caller decides WHEN (VM start)
    and whether the operator consented to host nftables."""
    if not available() or not slice_path:
        return LoadResult(False)
    ruleset = render(vm, slice_path)
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
