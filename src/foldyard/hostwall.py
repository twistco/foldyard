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

**Who loads it: the operator, once (ADR-0028).** foldyard never elevates on the host. The VM's
slice is a PERSISTENT user unit foldyard owns (:func:`ensure_slice`); ``fy machine host-wall``
renders the table and a system unit that loads it with the operator's user manager into the
project's state dir (:func:`stage`) and prints them with the exact ``sudo`` lines that install
them — the operator runs those, with the content in front of them. Then every ``fy up`` PROBES
(:func:`probe`): a child under the slice must be refused an out-of-slice loopback listener and
an off-host address, and must reach the band; anything else refuses ``fy up`` and points at the
verb. An install, not a VM-lifecycle step: ``stop``/``rm`` leave it alone.

**How :mod:`foldyard.machine` wires it (``[machine].host_wall = true``).** The VM is STARTED
inside its own transient scope under that slice (:func:`scoped_argv_prefix` —
``systemd-run --user --scope --slice``), so limactl, the hostagent and QEMU all land in
``…/fy.slice/fy-machine-<vm>.slice/fy-machine-<vm>.scope`` and nothing else does; after every
start, and again on each steady-state ``fy up``, the wall is probed for the slice the VM
ACTUALLY sits under (:func:`vm_cgroup_scope` → :func:`in_own_scope`). A VM found outside its own
scope-under-slice — started by hand, or before the option was turned on — is refused, because
matching the login session's scope instead would wall the operator's whole shell. Stdlib only.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import subprocess
import sys
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


# ── the operator's install: a slice foldyard owns, root-side files the operator applies ──
#
# foldyard never elevates on the host (ADR-0028). It (1) keeps the VM's slice as a PERSISTENT
# user unit, so the cgroup — and the ID a loaded table binds to — exists before the operator
# applies anything and comes back with every user manager; (2) renders the root-side files
# into a staging dir the operator can read and copies with two `install` lines; and (3) PROBES
# the result (below) — it never reads the table back, which would need root too.

ETC_DIR = Path("/etc/foldyard")
SYSTEMD_SYSTEM_DIR = Path("/etc/systemd/system")


def service_unit(vm: str) -> str:
    """The system unit that loads this VM's table at every start of the operator's user manager
    — one per VM, no template: the uid and the paths are baked in, so what root runs reads in
    full from the file."""
    return f"fy-host-wall-{_unit_safe(vm)}.service"


def user_unit_dir() -> Path:
    """Where `systemctl --user` reads the operator's own units from."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def slice_unit_text(vm: str) -> str:
    """The persistent user slice. ``WantedBy=default.target`` so it is up as soon as the user
    manager is — before the system unit (``After=user@<uid>.service``) loads the table that
    binds to it. Empty of settings on purpose: it exists to BE a cgroup, not to limit one."""
    return f"""\
[Unit]
Description=foldyard machine VM '{vm}' (the cgroup the host-side wall matches)
Documentation=https://github.com/twistco/foldyard/blob/main/docs/configuration.md

[Slice]

[Install]
WantedBy=default.target
"""


def service_unit_text(vm: str, uid: int, nft: str) -> str:
    """The system unit the operator installs. Bound to the user manager's lifetime
    (``BindsTo`` + ``After``): the slice lives under ``user@<uid>.service``, so the table is
    loaded once that is up (its slice exists by then — the user manager reports ready only after
    its default target, which wants the slice) and dropped when it stops (the slice, and the
    cgroup ID the table held, are gone with it). ``WantedBy=user@<uid>.service`` makes every
    later start of the user manager pull it in again. ``RemainAfterExit`` keeps the unit
    "active" while the table is loaded, so ``systemctl status`` tells the truth."""
    return f"""\
[Unit]
Description=foldyard host-side wall for machine VM '{vm}' (uid {uid})
Documentation=https://github.com/twistco/foldyard/blob/main/docs/configuration.md
After=user@{uid}.service
BindsTo=user@{uid}.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={nft} -f {ETC_DIR / ruleset_file(vm)}
ExecStop={nft} delete table inet {table_name(vm)}

[Install]
WantedBy=user@{uid}.service
"""


def ruleset_file(vm: str) -> str:
    return f"host-wall-{_unit_safe(vm)}.nft"


def _systemctl_user(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def ensure_slice(vm: str) -> str:
    """Make the VM's persistent slice exist and be active; return its cgroup path (no leading
    ``/``), empty when the user manager can't deliver one (its stderr is lost here: the caller
    says what a missing user manager means). Writes the unit only when its text changed
    (``daemon-reload`` is not free), then ``enable --now`` — idempotent."""
    unit = user_unit_dir() / slice_unit(vm)
    text = slice_unit_text(vm)
    try:
        current = unit.read_text()
    except OSError:
        current = ""
    if current != text:
        try:
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(text)
        except OSError:
            return ""
        _systemctl_user("daemon-reload")
    if _systemctl_user("enable", "--now", slice_unit(vm)).returncode != 0:
        return ""
    return slice_path(vm)


def slice_path(vm: str) -> str:
    """The cgroup path of the VM's slice as systemd placed it (``…/fy.slice/fy-machine-<vm>.slice``
    — it nests by the dashes), read from the unit rather than derived, or empty when the slice
    is not active. This is the path the table is rendered for."""
    res = _systemctl_user("show", "-p", "ControlGroup", "--value", slice_unit(vm))
    return res.stdout.strip().lstrip("/") if res.returncode == 0 else ""


@dataclass(frozen=True)
class Staged:
    """The root-side files, rendered into the operator's staging dir, and the exact commands
    that install (or remove) them — printed, never run, by foldyard."""

    ruleset: Path
    service: Path
    install: tuple[str, ...]
    uninstall: tuple[str, ...]


def stage(vm: str, slice_path: str, into: Path) -> Staged:
    """Render the ruleset and the system unit into ``into`` and say how to install them. The
    copies are root-owned once installed, so nothing running as the operator — a hostile
    process, an agent, the box — can change what root loads afterwards."""
    into.mkdir(parents=True, exist_ok=True)
    ruleset = into / ruleset_file(vm)
    service = into / service_unit(vm)
    ruleset.write_text(render(vm, slice_path))
    nft = shutil.which("nft") or "/usr/sbin/nft"
    service.write_text(service_unit_text(vm, os.getuid(), nft))
    unit = service_unit(vm)
    return Staged(
        ruleset,
        service,
        (
            f"sudo install -D -m 0644 {ruleset} {ETC_DIR / ruleset.name}",
            f"sudo install -D -m 0644 {service} {SYSTEMD_SYSTEM_DIR / unit}",
            "sudo systemctl daemon-reload",
            f"sudo systemctl enable --now {unit}",
        ),
        (
            f"sudo systemctl disable --now {unit}",
            f"sudo rm {SYSTEMD_SYSTEM_DIR / unit} {ETC_DIR / ruleset.name}",
            "sudo systemctl daemon-reload",
        ),
    )


# ── the probe: is the wall ENFORCING for this slice, right now? ────────────────────────────
#
# The only honest check, and the one that turns the cgroup-ID fail-open into fail-closed: a
# table can't be read back without root, and a table that IS there may hold the ID of a slice
# that no longer exists. So a child is run UNDER the slice and asked to connect: to a listener
# foldyard opened OUTSIDE the slice on loopback (must be REFUSED — the input hook's judgement),
# to TEST-NET-1 off-host (must be REFUSED by the output hook's reject; without the wall the SYN
# leaves the host and times out), and to a listener on the project's band (must CONNECT — the
# staleness half: a moved band shows up here). Refusals are tcp resets, so every verdict is
# immediate; a timeout is never mistaken for enforcement.

TEST_NET = ("192.0.2.1", 9)  # RFC 5737: never routable, so an unwalled SYN leaves and times out
_PROBE_TIMEOUT = 3.0


@dataclass(frozen=True)
class Probe:
    """``enforcing`` and, per check, what the child saw (``ok`` / ``refused`` / ``timeout`` /
    ``unreachable`` / an error) — printed on refusal so the operator sees WHICH half failed."""

    enforcing: bool
    checks: dict[str, str]
    error: str = ""

    def detail(self) -> str:
        """One line: each check, ticked when it saw what enforcement predicts."""
        parts = []
        for name, got in self.checks.items():
            good = got in _EXPECTED.get(name, ())
            parts.append(f"{name} {'✓' if good else '✗'} {got}")
        return ", ".join(parts)


# What each check must see for the wall to count as enforcing: `external` also accepts
# "unreachable" — a host with no route never hands the SYN to the wall, so it proves nothing
# either way, and refusing `fy up` on an offline laptop would be the wall walling the operator.
_EXPECTED = {"loopback": ("refused",), "external": ("refused", "unreachable"), "band": ("ok",)}


def _band_listener() -> socket.socket | None:
    """A listener on the first free port of this project's proxy band — outside the slice, so
    only the band rule lets the child reach it."""
    base = config.proxy_port_base()
    for port in range(base, base + _SPAN + 1):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(1)
            return sock
        except OSError:
            sock.close()
    return None


def probe_argv(slice_name: str, targets: dict[str, tuple[str, int]]) -> list[str]:
    """The child, run under the slice: this interpreter, this module, the targets."""
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--slice",
        slice_name,
        "--",
        sys.executable,
        "-m",
        "foldyard.hostwall",
        "--probe",
        *(f"{name}={host}:{port}" for name, (host, port) in targets.items()),
    ]


def probe(vm: str) -> Probe:
    """Run the probe for ``vm``'s slice (which must exist — :func:`ensure_slice`); see the
    section comment for what enforcing means."""
    loopback = socket.socket()
    band = _band_listener()
    try:
        loopback.bind(("127.0.0.1", 0))
        loopback.listen(1)
        targets = {"loopback": loopback.getsockname()[:2], "external": TEST_NET}
        if band is not None:
            targets["band"] = band.getsockname()[:2]
        res = subprocess.run(
            probe_argv(slice_unit(vm), targets), capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Probe(False, {}, str(exc))
    finally:
        loopback.close()
        if band is not None:
            band.close()
    if res.returncode != 0:
        return Probe(False, {}, res.stderr.strip() or f"probe exited {res.returncode}")
    try:
        checks = json.loads(res.stdout)
    except ValueError:
        return Probe(False, {}, f"unreadable probe output: {res.stdout!r}")
    return Probe(_verdict(checks), checks)


def _verdict(checks: dict[str, str]) -> bool:
    return all(checks.get(name) in want for name, want in _EXPECTED.items())


def _try_connect(host: str, port: int) -> str:
    sock = socket.socket()
    sock.settimeout(_PROBE_TIMEOUT)
    try:
        sock.connect((host, port))
        return "ok"
    except TimeoutError:
        return "timeout"
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            return "refused"
        if exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            return "unreachable"  # no route at all — the wall never got to see the packet
        return f"error({exc.errno})"
    finally:
        sock.close()


def _probe_main(args: list[str]) -> int:
    """The child's side: ``name=host:port`` per argument, a JSON object of verdicts out."""
    results = {}
    for arg in args:
        name, _, target = arg.partition("=")
        host, _, port = target.rpartition(":")
        results[name] = _try_connect(host, int(port))
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        raise SystemExit(_probe_main(sys.argv[2:]))
    raise SystemExit("usage: python -m foldyard.hostwall --probe name=host:port ...")
