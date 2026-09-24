"""Rootless dev-VM lifecycle — core foldyard (init/start/recreate).

Backend-agnostic orchestration over :mod:`foldyard.machine_backend`: Lima
(``[machine].backend = "lima"``, the default) for concurrent per-project VMs, or ``podman
machine`` (``backend = "podman"``). There is no VM-less backend (ADR-0027). The
backend is chosen once at import (``BACKEND``); this module only sequences create → start
→ socket and owns the cross-cutting policy (isolation mounts, the one-VM-at-a-time guard
for non-concurrent backends).

Host-only: the dev box runs INSIDE the engine boundary, so it can't manage its own host VM/engine
— ``ensure`` no-ops there and ``recreate`` refuses. The isolation property rests on the VM seeing
ONLY the repo + the worktrees root (asserted by ``fy verify``).

``ensure`` progress → stderr (it runs inside ``foldyard shellenv``, whose stdout an ``eval``
consumes). ``recreate`` is interactive, so it talks on stdout.

"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import config, guestlog, hostwall, podman_desktop, sandbox
from .machine_backend import default_unavailable_block, get_backend

MACHINE = config.machine_name()
BACKEND = get_backend(config.machine_backend())


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _resources() -> dict:
    return config.machine_resources()


def _volumes(main: Path, wt_root: Path) -> list[tuple[str, str]]:
    """The isolation mount set as ``(host, guest)`` pairs (host==guest path) — the ONLY
    things the VM may see. REPLACES the backend's default mounts."""
    return [(str(main), str(main)), (str(wt_root), str(wt_root))]


def guest_mounts(main: Path, wt_root: Path) -> list[str]:
    """The GUEST paths of the isolation mount set — the only host paths `verify`'s mount audit
    exempts (by exact mountpoint) when it reads the VM's real mount table."""
    return [guest for _, guest in _volumes(main, wt_root)]


def running_machines() -> list[str]:
    """Names of currently-running VMs of the active backend."""
    return BACKEND.list_running()


def exists() -> bool:
    return BACKEND.exists(MACHINE)


def state() -> str:
    return BACKEND.state(MACHINE)


def mounts() -> list[str]:
    return BACKEND.mounts(MACHINE)


def socket() -> str:
    """The machine's docker-compat (libpod) socket URI (assumes the machine exists)."""
    return BACKEND.socket(MACHINE)


def responsive() -> bool:
    """Is the machine's socket actually served? (:meth:`Backend.responsive` — liveness, as
    opposed to :func:`state`'s lifecycle flag.)"""
    return BACKEND.responsive(MACHINE)


def _start() -> bool:
    """Start MACHINE. On a NON-concurrent backend (podman on macOS) refuse to start beside
    another running VM — rather than emit the backend's cryptic error, print actionable
    guidance: stop the other, or switch this project to the Lima backend. Returns True on a
    running machine."""
    if not BACKEND.supports_concurrent():
        others = [m for m in running_machines() if m != MACHINE]
        if others:
            _err(f"✗ {BACKEND.name} machine '{others[0]}' is already running, and macOS runs")
            _err(f"  only ONE {BACKEND.name} VM at a time — '{MACHINE}' can't start beside it.")
            _err("  Either:")
            _err(f"    • stop the other yourself:   {BACKEND.cli} machine stop {others[0]}")
            _err("    • or run projects concurrently with the Lima backend — set")
            _err('      backend = "lima"  under [machine] in foldyard.toml (needs limactl).')
            return False
    # Under the host wall the VM's host processes are launched inside their own transient
    # cgroup scope under a per-VM PERSISTENT slice, so the wall has one predictable,
    # restart-stable thing to match (see _check_host_wall). The slice must already be active:
    # `systemd-run --slice` would otherwise create a transient one — a new cgroup ID the
    # operator's installed table does not hold. Setting it up is `fy machine host-firewall`'s job
    # (the one user-level change the wall makes, said out loud there); a launch verb only
    # checks, and refuses BEFORE booting a VM it could not wall.
    prefix: list[str] = []
    if _host_wall_wanted():
        if not hostwall.slice_path(MACHINE):
            _err("✗ [machine] host_firewall = true but the host wall is not set up on this host:")
            _err(f"  the user slice '{hostwall.slice_unit(MACHINE)}' is not active. Set it up")
            _err("  (and see the install steps) with:   fy machine host-firewall")
            return False
        prefix = hostwall.scoped_argv_prefix(MACHINE)
    _err(f"▶ starting {BACKEND.name} machine '{MACHINE}'…")
    if not BACKEND.start(MACHINE, prefix=prefix):
        _err(f"✗ '{BACKEND.cli}' failed to start '{MACHINE}'.")
        return False
    return True


_REVIVE_PROBES = 10
_REVIVE_PROBE_DELAY = 0.5
"""How long :func:`_revive` waits for the restarted machine's socket to start accepting —
10 probes, ~5s of sleeping. Bounded on purpose: a VM that is genuinely failing to boot must
reach the actionable error rather than hang ``fy up``. A dead socket fails ``connect``
immediately, so the budget is only ever spent on a machine that never comes up."""


def _revive() -> bool:
    """Restart a machine whose lifecycle flag says ``running`` but whose socket is dead.

    The flag survives the VM dying underneath it (host crash / battery-death mid-sleep, or a
    start whose guest never signalled ready): the backend tears down its own half — api socket,
    network forwarder — while the hypervisor process lives on, unreachable. ``ensure`` used to
    trust the flag and no-op, so every later engine call died on a raw ``dial unix …: no such
    file or directory`` and the only way out was knowing to stop, hand-kill the orphan and
    start again. That is exactly what this does."""
    _err(f"⚠ {BACKEND.name} machine '{MACHINE}' reports running, but nothing is serving its")
    _err("  socket — a half-started VM (host crash, or a boot that never came up). Restarting…")
    if not BACKEND.stop(MACHINE):
        _err(f"  ('{BACKEND.cli} machine stop' failed — continuing; the reap below is the point.)")
    # A stop that "succeeds" may still leave the hypervisor alive: once the backend has lost
    # track of the process it reports success without reaping it, and the next start races it.
    reaped = BACKEND.reap_orphans(MACHINE)
    if reaped:
        _err(f"  reaped {len(reaped)} orphaned VM process(es): {', '.join(map(str, reaped))}")
    if not _start():
        return False
    # `machine start` returns once the backend is satisfied, but the api socket can take a
    # moment longer to accept — so a single probe here would call a machine that is merely
    # slow "still dead" and send the user off to rebuild a VM that was about to come up.
    # Poll instead, and return the moment it answers.
    for attempt in range(_REVIVE_PROBES):
        if responsive():
            return True
        if attempt + 1 < _REVIVE_PROBES:
            time.sleep(_REVIVE_PROBE_DELAY)
    _err(f"✗ '{MACHINE}' started but its socket is still dead. The VM may be failing to")
    _err(f"  boot — check the console log, then `{BACKEND.cli} machine rm {MACHINE}`")
    _err("  and `fy up` to rebuild it (containers and named volumes are lost).")
    return False


# ── the host firewall (lima, `[machine] host_firewall`): nftables on the HOST, by cgroup ──
#
# The tier above the guest wall. That one is enforcement the guest applies to itself, so a
# guest-KERNEL exploit reaching VM-root can flush it; this one matches the VM process's own
# traffic on the host — where the guest has no reach — and allows only this project's daemon
# band (hostwall.py has the ruleset and the why). foldyard never loads it: the OPERATOR installs
# the table once, from files foldyard renders and prints (`fy machine host-firewall`; ADR-0028 — no
# elevation on the host, nothing to approve blind). What foldyard does is (1) start the VM
# inside its own transient scope under its own PERSISTENT slice (`_start`), the cgroup the
# operator's table binds to, and (2) after every start — and on every steady-state `fy up` —
# PROBE that the wall is enforcing for that slice (`_check_host_wall`): a table can't be read
# back without root, and one that is there may hold the ID of a slice that no longer exists
# (a host reboot), which is fail-open unless something asks. A VM found outside its own
# scope-under-slice (started by hand, or before `host_wall` was turned on) is REFUSED:
# matching the login session's scope instead would wall the operator's entire shell, and a
# slice the VM is not under would match nothing.


def _host_wall_wanted() -> bool:
    return BACKEND.name == "lima" and config.machine_host_wall()


def _check_host_wall() -> None:
    """Refuse to go on unless the host-side wall is ENFORCING for the running VM's slice; a hard
    stop when it was asked for and isn't there — never a silent downgrade to the guest wall
    alone. Says which half failed and where the install steps are."""
    if not _host_wall_wanted():
        return
    if not hostwall.available():
        _err(
            "✗ [machine] host_firewall = true but this host has no `nft` / cgroup v2 to enforce it."
        )
        _err("  Install nftables, or drop `host_wall` (the in-VM wall still applies).")
        raise SystemExit(1)
    pids = BACKEND.host_pids(MACHINE)
    scope = hostwall.vm_cgroup_scope(pids[0] if pids else 0)
    if not hostwall.in_own_scope(MACHINE, scope):
        _err(f"✗ '{MACHINE}' is running OUTSIDE its own scope ({scope or 'no VM pid found'}), so")
        _err("  the host wall has nothing safe to match — walling the scope it is in would wall")
        _err("  the shell that started it. Restart it under foldyard:   fy machine stop && fy up")
        raise SystemExit(1)
    result = hostwall.probe(MACHINE)
    if result.enforcing:
        _err(f"✓ host-side wall enforcing for '{MACHINE}' ({result.detail()})")
        return
    _err(f"✗ the host-side wall is NOT enforcing for '{MACHINE}'.")
    if result.error:
        _err(f"  probe: {result.error}")
    else:
        _err(f"  probe: {result.detail()}")
    _err("  Not installed, or installed for a slice that no longer exists (a host reboot, a")
    _err("  changed band). foldyard never loads it itself — see the files and the steps:")
    _err("      fy machine host-firewall")
    raise SystemExit(1)


def host_wall(uninstall: bool = False) -> int:
    """`fy machine host-firewall`: the operator's side of the host wall. Makes the VM's persistent
    slice exist, renders the root-side files into the project's state dir, prints them and the
    exact commands that install (or, with ``uninstall``, remove) them — and probes whether the
    wall is enforcing right now. Exit 0 when it is, 1 when it is not (so a script can ask).
    foldyard runs none of the printed commands: what root does is in front of the operator."""
    if not _host_wall_wanted():
        print("host_wall is off for this project ([machine] host_firewall; lima backend only).")
        return 0
    if not hostwall.available():
        print("✗ this host can't enforce a host wall: it needs `nft` (nftables) and cgroup v2.")
        return 1
    fresh = False
    if uninstall:
        # removal never sets anything up: no slice means nothing to probe, only lines to print
        slice_path = hostwall.slice_path(MACHINE)
    else:
        fresh = not hostwall.slice_installed(MACHINE)
        slice_path = hostwall.ensure_slice(MACHINE)
        if not slice_path:
            print(f"✗ can't set up the user slice '{hostwall.slice_unit(MACHINE)}':")
            print("  `systemctl --user` failed — no user manager for this login?")
            print("  `loginctl enable-linger` gives one to a session without it.")
            return 1
    if fresh:  # the one user-level change the wall makes, said once, when it happens
        print(f"✓ user slice {hostwall.slice_unit(MACHINE)} written and enabled (no root):")
        print(f"    {hostwall.user_unit_dir() / hostwall.slice_unit(MACHINE)}")
    staged = hostwall.stage(MACHINE, slice_path, config.state_dir() / "host-wall")
    if slice_path:
        result = hostwall.probe(MACHINE)
    else:  # uninstall with no slice: probing would create a transient one — nothing to ask
        result = hostwall.Probe(False, {}, "not set up (no slice)")
    print(f"host-side wall for machine '{MACHINE}' — slice {slice_path or '(none)'}")
    if result.enforcing:
        print(f"  ✓ enforcing ({result.detail()})")
    else:
        print(f"  ✗ NOT enforcing ({result.error or result.detail()})")
    if uninstall:
        print("\nTo remove the install (the `sudo` lines as root, the last two as you — foldyard")
        print("runs none of this; stop the VM first, the slice goes down with its last line):")
        for line in staged.uninstall:
            print(f"    {line}")
        return 0 if result.enforcing else 1
    print(f"\n── {staged.ruleset} ── the table root loads:\n")
    print(staged.ruleset.read_text())
    print(f"── {staged.service} ── the unit that loads it with your user manager:\n")
    print(staged.service.read_text())
    print("To install (as root — foldyard runs none of this; the copies are root-owned):")
    for line in staged.install:
        print(f"    {line}")
    print("\nThen `fy up` (or this verb again) probes it. Re-run the install after a change to")
    print("the project's daemon band, and once per host if the files were removed.")
    return 0 if result.enforcing else 1


def _note_host_wall_install() -> None:
    """`rm` deletes the VM, not the operator's wall install: the table is inert while the slice
    is empty and right again for the next VM of this name. Say so, once."""
    if _host_wall_wanted() and hostwall.available():
        _err(
            "ℹ the host-side wall install is untouched (`fy machine host-firewall --uninstall` says"
        )
        _err("  how to remove it).")


# ── guest boot provisioning: the sudo grant + the in-VM egress wall (lima only) ────────────
#
# Root in the guest is BOOT-TIME ONLY. foldyard records ONE `provision: mode: system` script in
# the instance's lima.yaml (`assets/machine-wall/guest-boot.sh`, rendered); Lima runs it as root
# on every boot, after cloud-init. It (1) narrows the sudo grant Lima's cloud-init re-creates on
# every boot — the instance id changes each boot — to Lima's own non-passwordless form, shutdown
# only, which a graceful `limactl stop` still needs: the VM user, the uid the box runs as, gets
# no path to VM-root; (2) installs or removes the nftables wall (`machine-wall.sh`, embedded and
# root-owned in the guest — never read from the repo mount); (3) writes what it applied to
# /run/fy-wall/state, world-readable, so the host can check without root.
#
# The host therefore never runs `sudo` in the guest: it records the script while the VM is
# STOPPED (`limactl edit` refuses a running one) and reads the guest's report after boot. A
# change — the wall flipped, a moved port band, a VM created before the grant was dropped — needs
# a restart, and a running VM with stale provisioning is REFUSED: fail closed rather than run
# unwalled or with the old grant. The host-side proxy stays the chokepoint (allowlist, keyless
# injection, network log, creds all host-side); the wall makes the VM fail-closed on the VM
# user's uid, whose only opening is the host's foldyard daemons at the Lima host gateway. There
# is deliberately NO separate CLI verb: toggling IS editing foldyard.toml + `fy machine stop` +
# `fy up`; in-VM diagnosis is `limactl shell <name> cat /run/fy-wall/boot.log`.

_WALL_SCRIPT_PATH = "/usr/local/libexec/fy-machine-wall"


def _wall_asset() -> Path:
    return Path(__file__).resolve().parent / "assets" / "machine-wall" / "machine-wall.sh"


def _boot_asset() -> Path:
    return Path(__file__).resolve().parent / "assets" / "machine-wall" / "guest-boot.sh"


def _wall_ports() -> str:
    """The nft port elements the wall opens toward the host: each daemon base port + the full
    worktree-offset span (``config.worktree_offset`` is 1..89, main is 0), as ``base-base+89``
    ranges. Bases are THIS PROJECT's (allocated band or env override — ``config.proxy_port_base``/
    ``gcp_minter_port_base``), so a walled VM can reach only its own project's daemons, not a
    sibling project's."""
    bases = {config.proxy_port_base(), config.gcp_minter_port_base()}
    return ", ".join(f"{b}-{b + 89}" for b in sorted(bases))


def _provision_want() -> str:
    """What the guest must report after boot: the wall state, port set included."""
    return f"wall on {_wall_ports()}" if config.machine_wall() else "wall off"


def _render_provisioning() -> tuple[str, str]:
    """The boot script for the CURRENT config and its id (a hash of the rendered content, so a
    flipped wall, a moved band or a changed asset all read as a different recording)."""
    if config.machine_wall():
        gw = config.LIMA_HOST_GATEWAY
        args = ["install", gw, _wall_ports(), f"http://{gw}:{config.proxy_port_base()}"]
    else:
        args = ["uninstall"]
    body = (
        _boot_asset()
        .read_text()
        .replace("@@WALL_ASSET@@", _wall_asset().read_text().rstrip("\n"))
        .replace("@@WALL_PATH@@", _WALL_SCRIPT_PATH)
        .replace("@@WALL_ARGS@@", " ".join(shlex.quote(a) for a in args))
        .replace("@@WANT@@", _provision_want())
        .replace("@@JOURNAL@@", guestlog.journal_snippet(sudo="").strip("\n"))
    )
    ident = hashlib.sha256(body.encode()).hexdigest()[:16]
    return body.replace("@@ID@@", ident, 1), ident


def guest_provision_script() -> str:
    """The root boot script to record in the instance config (see the section comment)."""
    return _render_provisioning()[0]


def provision_id() -> str:
    """The id of the boot script the current config wants recorded."""
    return _render_provisioning()[1]


def _guest_state() -> tuple[str, bool, bool]:
    """What the guest applied at its last boot, read WITHOUT root: ``(the /run/fy-wall/state
    line, fy-wall.service active, rootful podman.socket masked)``. ``("", False, False)`` when
    the VM is unreachable — which reads as "not applied" and fails :func:`ensure` closed. A
    patchable seam (golden tests set it)."""

    def _vm(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["limactl", "shell", MACHINE, "--", *args],
            capture_output=True,
            text=True,
            timeout=20,
        )

    try:
        state = _vm(["cat", "/run/fy-wall/state"]).stdout.strip()
        active = _vm(["systemctl", "is-active", "fy-wall.service"]).stdout.strip() == "active"
        # `is-enabled` prints "masked" (rc 1) when masked, "enabled"/"disabled" otherwise — the
        # install masks it, so anything but "masked" means the container-root→VM-root hole is open.
        masked = _vm(["systemctl", "is-enabled", "podman.socket"]).stdout.strip() == "masked"
    except (OSError, subprocess.SubprocessError):
        return ("", False, False)
    return (state, active, masked)


def _record_provisioning() -> None:
    """Record the boot script in the (STOPPED) instance config when it differs from what is
    recorded; refuse a RUNNING VM whose recording is stale (see the section comment)."""
    if BACKEND.name != "lima":
        return
    script, ident = _render_provisioning()
    if BACKEND.provision_id(MACHINE) == ident:
        return
    if state() == "running":
        _err(f"✗ '{MACHINE}' is running with STALE boot provisioning — the sudo grant, the egress")
        _err("  wall or its port band changed (or the VM predates boot provisioning). It applies")
        _err("  at boot, as root, from the recorded config, so restart:   fy machine stop && fy up")
        raise SystemExit(1)
    _err(f"▶ recording boot provisioning for '{MACHINE}' ({_provision_want()}; VM user gets no")
    _err("  sudo)…")
    if not BACKEND.set_provision(MACHINE, script):
        _err(f"✗ recording the boot provisioning into '{MACHINE}' failed (`limactl edit`).")
        raise SystemExit(1)


def _check_guest_provisioning() -> None:
    """After a boot, and on the steady-state path: the guest's own report must match what the
    config wants. One `limactl shell` per `fy up` — correctness over speed: a VM reset behind
    foldyard's back, a failed boot script or a re-enabled rootful socket all surface here."""
    if BACKEND.name != "lima":
        return
    want = _provision_want()
    got, active, masked = _guest_state()
    problems = []
    if got != want:
        problems.append(f"guest reports {got or 'nothing'!r}, wanted {want!r}")
    if config.machine_wall():
        if not active:
            problems.append("fy-wall.service is not active")
        if not masked:
            problems.append("the rootful podman.socket is NOT masked (container-root → VM-root)")
    if not problems:
        return
    _err(f"✗ '{MACHINE}' did not apply its boot provisioning: {'; '.join(problems)}.")
    _err("  The script runs as root at boot from the recorded config; its log is readable")
    _err(f"  without root:   limactl shell {MACHINE} cat /run/fy-wall/boot.log")
    _err("  then restart:   fy machine stop && fy up")
    raise SystemExit(1)


def ensure(main: Path, wt_root: Path) -> None:
    """Provision the rootless machine (mounts ONLY repo + worktrees root) if absent, start it
    if stopped. No-op inside the box (manages its host VM from outside). A missing backend CLI
    is a hard error either way — one message when the consumer NAMED the backend, another when
    they inherited the default — because a silent skip leaves the socket unset and hands the
    whole stack to the host's own podman
    (:func:`machine_backend.default_unavailable_block`). Progress → stderr."""
    if config.in_box():
        return
    if not BACKEND.available():
        if not config.machine_backend_explicit():
            _err(default_unavailable_block(BACKEND))
            raise SystemExit(1)
        _err(f"✗ [machine].backend = '{BACKEND.name}' but '{BACKEND.cli}' isn't installed.")
        _err(f"  Install it ({BACKEND.install_hint}) or name a different [machine].backend.")
        raise SystemExit(1)
    wt_root.mkdir(parents=True, exist_ok=True)
    if not exists():
        _err(f"▶ {BACKEND.name} machine '{MACHINE}' not found — initialising (rootless; mounts")
        _err(f"  ONLY {main} and {wt_root})…")
        _err("  (one-time, a few minutes: downloads the VM image + provisions disk)")
        if not BACKEND.create(MACHINE, _resources(), _volumes(main, wt_root)):
            _err(
                f"✗ creating {BACKEND.name} machine '{MACHINE}' failed (Apple Virtualization "
                "blocked — e.g. under nono? Then create it in a plain terminal)."
            )
            raise SystemExit(1)
    elif str(wt_root) not in mounts():
        _err(f"⚠ machine '{MACHINE}' has no '{wt_root}' mount (created before worktree")
        _err("  support). The main stack still works; worktree stacks need it. To enable")
        _err("  worktrees, recreate once:  fy machine recreate")
    # Boot provisioning (the sudo grant + the wall) is recorded BEFORE the VM boots — it is
    # what runs as root at boot — and a running VM whose recording is stale is refused here.
    _record_provisioning()
    _pin_ssh_port()
    if state() != "running":
        if not _start():
            raise SystemExit(1)
    elif not responsive():
        # Running per the backend, dead in fact — restart it rather than hand the caller a
        # socket nothing serves (see _revive).
        if not _revive():
            raise SystemExit(1)
    # Every path ends by probing the host-side wall for wherever the VM actually sits, then
    # reading the guest's own report of what it applied (never the host's memory of it): a
    # fresh boot, a revive, and the steady state alike.
    _check_host_wall()
    _check_guest_provisioning()
    # Housekeeping every VM wants (journal cap, API log level): over ssh, best-effort, before the
    # sandbox so its service restarts already see the drop-in.
    guestlog.ensure(BACKEND, MACHINE)
    # The gVisor posture is user-level in the guest (no root, so not the boot script): provisioned
    # over the backend's ssh once the VM is up and its root-side provisioning is verified.
    if sandbox.wanted():
        sandbox.ensure(BACKEND, MACHINE)
    _follow_in_podman_desktop()


def _pin_ssh_port() -> None:
    """Pin the VM's ssh forward to its band (``ssh.localPort``) so the Podman Desktop connection
    survives a reboot — Lima picks a fresh port at every start otherwise. Only where Podman
    Desktop is followed (:func:`podman_desktop.following`), and only on a STOPPED VM
    (``limactl edit`` refuses a running one): a running VM picks it up at its next start."""
    if BACKEND.name != "lima" or not podman_desktop.following():
        return
    want = config.machine_ssh_port()
    if BACKEND.ssh_port(MACHINE) == want or state() == "running":
        return
    if not BACKEND.pin_ssh_port(MACHINE, want):
        _err(f"⚠ couldn't pin '{MACHINE}''s ssh port to {want} — Podman Desktop's entry for it")
        _err("  goes stale at each reboot until it is")


def _follow_in_podman_desktop() -> None:
    """Keep this VM's Podman Desktop entry current, where Podman Desktop is followed. Silent
    unless it changed something."""
    if BACKEND.name != "lima" or not podman_desktop.following():
        return
    for line in _show_in_podman_desktop():
        _err(line)


def _show_in_podman_desktop(*, verbose: bool = False) -> list[str]:
    target = BACKEND.ssh_target(MACHINE)
    if target is None:
        return [f"⚠ no ssh route into '{MACHINE}' yet — Podman Desktop left as it was"]
    name = podman_desktop.connection_name(MACHINE)
    registered = podman_desktop.register(
        name, podman_desktop.uri(target, BACKEND.guest_socket()), target.identity
    )
    return podman_desktop.messages(name, registered, podman_desktop.remote_state(), verbose=verbose)


def point_podman_desktop() -> int:
    """`fy machine desktop`: register this VM with Podman Desktop now."""
    if config.in_box():
        print("✗ run on the host — Podman Desktop lives there, not in the box")
        return 1
    if BACKEND.name != "lima":
        print(f"✗ Podman Desktop shows {BACKEND.name} machines itself — nothing to register")
        return 1
    lines = _show_in_podman_desktop(verbose=True)
    if not any(line.startswith(("▶", "⚠")) for line in lines):
        name = podman_desktop.connection_name(MACHINE)
        lines.insert(0, f"✓ '{name}' is registered for Podman Desktop")
    for line in lines:
        print(line)
    detected = podman_desktop.remote_state() != "absent"
    if podman_desktop.choice() is False and detected:
        print("  FOLDYARD_PODMAN_DESKTOP=0 is set, so `fy up` won't keep it current across reboots")
        print("  (a pinned ssh port) — unset it to let `fy up` maintain it.")
    elif not podman_desktop.following():
        print("  Podman Desktop wasn't detected, so `fy up` won't keep it current across reboots")
        print("  (a pinned ssh port) — export FOLDYARD_PODMAN_DESKTOP=1 in your shell profile.")
    return 1 if any(line.startswith("⚠") for line in lines) else 0


def not_running_reason() -> str | None:
    """Why engine verbs can't reach the machine WITHOUT provisioning it — None when it's
    running. Never creates or starts anything: this is
    the read/teardown verbs' counterpart to :func:`ensure` (``fy down`` against a deleted
    machine must say "nothing to stop", not download a VM image)."""
    if not BACKEND.available():
        return f"no '{BACKEND.cli}' CLI on this host"
    if not exists():
        return f"{BACKEND.name} machine '{MACHINE}' does not exist"
    if state() != "running":
        return f"{BACKEND.name} machine '{MACHINE}' is stopped"
    if not responsive():
        # `down`/`ps`/`logs` against a half-started VM would otherwise reach a socket nothing
        # serves and fail on the engine's raw dial error. Only `up`/`ensure` may revive it.
        return f"{BACKEND.name} machine '{MACHINE}' is running but its socket is dead"
    return None


def _host_only_or_rc(verb: str) -> int | None:
    """The shared guard for the stop/rm lifecycle verbs: host-only (the box must not manage the
    VM it lives in). None when OK to proceed."""
    if config.in_box():
        print(f"✗ run on the host (Mac) — the box can't {verb} its own machine")
        return 1
    if not BACKEND.available():
        print(f"✗ no '{BACKEND.cli}' CLI on this host — can't {verb} the machine")
        return 1
    return None


def _stop_host_supervisor() -> bool:
    """The VM and its host supervisor are one lifecycle: after a successful stop/removal (or a
    no-op against an already-gone VM), no box can use the proxy and the supervisor must not retain
    a checkout mirror. Lazy import avoids pulling the daemon process machinery into machine's
    normal provision path.
    """
    from . import supervisor

    if supervisor.stop() != 0:
        return False
    # Waited shutdown matters: a pre-0.270.0 supervisor writes its mirror on exit, so unlink only
    # after `supervisor.stop()` released its lock and cannot recreate the generated checkout file.
    config.mirror_file().unlink(missing_ok=True)
    return True


def stop() -> int:
    """Stop the machine VM and its host supervisor (containers + named volumes kept)."""
    rc = _host_only_or_rc("stop")
    if rc is not None:
        return rc
    if not exists():
        if not _stop_host_supervisor():
            return 1
        print(f"({BACKEND.name} machine '{MACHINE}' does not exist — nothing to stop.)")
        return 0
    if state() != "running":
        if not _stop_host_supervisor():
            return 1
        print(f"({BACKEND.name} machine '{MACHINE}' is already stopped.)")
        return 0
    if not BACKEND.stop(MACHINE):
        print(f"✗ '{BACKEND.cli}' failed to stop '{MACHINE}'.")
        return 1
    if not _stop_host_supervisor():
        return 1
    print(f"✓ machine '{MACHINE}' stopped (containers + volumes kept; `fy up` restarts it).")
    return 0


def delete(assume_yes: bool = False) -> int:
    """Stop + REMOVE the machine VM. Everything inside it — containers, named volumes (dev-box
    login, caches) — is destroyed; the repo on the shared mount is untouched. Interactive
    unless assume_yes."""
    rc = _host_only_or_rc("delete")
    if rc is not None:
        return rc
    if not exists():
        if not _stop_host_supervisor():
            return 1
        print(f"({BACKEND.name} machine '{MACHINE}' does not exist — nothing to delete.)")
        return 0
    print(f"This stops + REMOVES {BACKEND.name} machine '{MACHINE}'. Every container and named")
    print("volume inside it (dev box login, caches) is destroyed; the repo itself is untouched.")
    if not assume_yes:
        try:
            answer = input("Proceed? (y/N): ").strip()
        except EOFError:
            answer = ""
        if answer not in ("y", "Y"):
            print("Aborted.")
            return 0
    if state() == "running":
        BACKEND.stop(MACHINE)
    if not BACKEND.remove(MACHINE):
        print(f"✗ '{BACKEND.cli}' failed to remove '{MACHINE}'.")
        return 1
    # The boot provisioning lives in the instance config, which the backend removes with the VM;
    # the host-side wall is the operator's install, not the VM's.
    _note_host_wall_install()
    if podman_desktop.unregister(podman_desktop.connection_name(MACHINE)) == "removed":
        print(
            f"✓ removed its Podman Desktop connection '{podman_desktop.connection_name(MACHINE)}'"
        )
    if not _stop_host_supervisor():
        return 1
    print(f"✓ machine '{MACHINE}' deleted. `fy up` / `fy machine ensure` re-creates it.")
    return 0


def recreate(main: Path, wt_root: Path, assume_yes: bool = False) -> int:
    """Stop + remove + re-create the machine so it mounts the worktrees root (VM mount sets
    are init-only — neither podman's ``--volume`` nor Lima's ``mounts:`` can be edited live).
    Interactive unless assume_yes."""
    if config.in_box() or not BACKEND.available():
        print("✗ run on the host (Mac) — the box can't recreate its own machine")
        return 1
    print(f"This stops + REMOVES {BACKEND.name} machine '{MACHINE}' and re-creates it with mounts:")
    print(f"    {main}")
    print(f"    {wt_root}")
    print("Named volumes (pnpm/playwright caches, dev box login) live IN the machine and")
    print("will be lost — they rebuild on next 'fy up'. Stacks are recreated too.")
    if not assume_yes:
        try:
            answer = input("Proceed? (y/N): ").strip()
        except EOFError:
            answer = ""
        if answer not in ("y", "Y"):
            print("Aborted.")
            return 0
    BACKEND.stop(MACHINE)
    BACKEND.remove(MACHINE)
    wt_root.mkdir(parents=True, exist_ok=True)
    if not BACKEND.create(MACHINE, _resources(), _volumes(main, wt_root)):
        print(f"✗ creating {BACKEND.name} machine '{MACHINE}' failed.")
        return 1
    _record_provisioning()  # the fresh VM is stopped: record before its first boot
    if not _start():  # one-VM-at-a-time aware on non-concurrent backends
        return 1
    _check_host_wall()
    _check_guest_provisioning()
    print(f"✓ machine '{MACHINE}' recreated with the worktrees mount.")
    return 0
