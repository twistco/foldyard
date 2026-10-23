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
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import boxmounts, config, forwarder, guestlog, hostwall, monitor, podman_desktop, sandbox
from .machine_backend import Mount, default_unavailable_block, get_backend

MACHINE = config.machine_name()
BACKEND = get_backend(config.machine_backend())


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _resources() -> dict:
    return config.machine_resources()


def _volumes(main: Path, wt_root: Path, vmtype: str = "") -> list[Mount]:
    """The isolation mount set (host==guest path) — the ONLY things the VM may see. REPLACES the
    backend's default mounts. The checkout and the worktrees root, writable; on Lima also
    foldyard's two directories (ADR-0033, :mod:`foldyard.boxmounts`): the from-box, writable, and
    — only where ``vmtype`` has a share type that enforces read-only OUTSIDE the guest, which
    :meth:`LimaBackend._set_expr` / :func:`_record_mounts` pin — the to-box, read-only. An
    enforcing to-box or none: one the guest could write would read as tamper-proof. The podman
    backend's set is unchanged (its mounts are fixed at `podman machine init`)."""
    mounts = [Mount(str(main), str(main)), Mount(str(wt_root), str(wt_root))]
    if BACKEND.name != "lima":
        return mounts
    from_box, to_box = str(boxmounts.from_box_dir()), str(boxmounts.to_box_dir())
    mounts.append(Mount(from_box, from_box))
    if mount_type := boxmounts.mount_type_for(vmtype):
        cache = boxmounts.TO_BOX_9P_CACHE if mount_type == "9p" else ""
        mounts.append(Mount(to_box, to_box, writable=False, cache=cache))
    return mounts


def guest_mounts(main: Path, wt_root: Path) -> list[str]:
    """The GUEST paths `verify`'s mount audit exempts (by exact mountpoint) when it reads the VM's
    real mount table: the checkout, the worktrees root and — where they exist — foldyard's two
    directories (:func:`boxmounts.vm_paths`; in the box, the paths `fy box up` baked)."""
    return [str(main), str(wt_root), *(p for p in boxmounts.vm_paths() if p)]


def to_box_unavailable() -> str | None:
    """Why this project's VM has no to-box, or ``None`` when it has one (or will, at its next
    `fy up`). For the tenants that offer it (`fy monitor hook add --to-box`)."""
    if BACKEND.name != "lima":
        return (
            f"the {BACKEND.name} backend has no to-box: no provider enforces a read-only mount "
            "outside the guest, and its mounts are fixed when the machine is created (the lima "
            "backend has one)"
        )
    vmtype = BACKEND.vmtype(MACHINE) if exists() else BACKEND.resolve_vmtype()
    if not boxmounts.mount_type_for(vmtype):
        return (
            f"this VM's hypervisor ({vmtype or 'Lima’s default'}) has no share type that enforces "
            "read-only outside the guest — only vz (virtiofs) and qemu (9p) do"
        )
    return None


def to_box_mounted() -> bool:
    """Does the VM share the to-box read-only right now (its effective config)? What `fy box up`
    asks before binding it into the box."""
    if BACKEND.name != "lima":
        return False
    to_box = str(boxmounts.to_box_dir())
    return Mount(to_box, to_box, writable=False) in (BACKEND.mount_set(MACHINE) or [])


def from_box_mounted() -> bool:
    """Does the VM share the from-box? (`fy box up` binds transcripts there only then.)"""
    if BACKEND.name != "lima":
        return False
    from_box = str(boxmounts.from_box_dir())
    return Mount(from_box, from_box) in (BACKEND.mount_set(MACHINE) or [])


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


def existing_socket() -> str | None:
    """:func:`socket`, READ-ONLY and safe without a machine: None when the backend names no path
    (Lima: ``""``; podman: a bare ``unix://`` from a failed inspect) or can't answer at all. For
    callers that must reach an engine already there but never provision one — never
    :func:`ensure`. Says nothing about liveness (:func:`responsive`)."""
    try:
        sock = socket()
    except Exception:
        return None
    return sock if sock.removeprefix("unix://") else None


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
            _err("✗ [machine] host_firewall = true but the host firewall isn't set up here:")
            _err(f"  the user slice '{hostwall.slice_unit(MACHINE)}' is not active. Set it up")
            _err("  (and see the install steps) with:   fy machine host-firewall")
            return False
        prefix = hostwall.scoped_argv_prefix(MACHINE)
    _err(f"▶ starting {BACKEND.name} machine '{MACHINE}'…")
    if not BACKEND.start(MACHINE, prefix=prefix):
        _err(f"✗ '{BACKEND.cli}' failed to start '{MACHINE}'.")
        # `limactl start` also gives up on a VM that IS up, when its boot scripts don't finish
        # within its timeout: say which ones are still going.
        if BACKEND.name == "lima":
            for line in _unfinished_boot_lines(_boot_progress()):
                _err(line)
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
    # The one moment this VM is stopped: `ensure`'s own pin skipped it on the `running` flag, and a
    # stale recorded ssh port (the band moved, or it is taken now) hangs the start waiting for ssh.
    _pin_ssh_port()
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
        _err("  Install nftables, or drop `host_firewall` (the VM firewall still applies).")
        raise SystemExit(1)
    pids = BACKEND.host_pids(MACHINE)
    scope = hostwall.vm_cgroup_scope(pids[0] if pids else 0)
    if not hostwall.in_own_scope(MACHINE, scope):
        _err(f"✗ '{MACHINE}' is running OUTSIDE its own scope ({scope or 'no VM pid found'}), so")
        _err("  the host firewall has nothing safe to match — the scope it is in holds the shell")
        _err("  that started it. Restart it under foldyard:   fy machine stop && fy up")
        raise SystemExit(1)
    result = hostwall.probe(MACHINE)
    if result.enforcing:
        _err(f"✓ host firewall enforcing for '{MACHINE}' ({result.detail()})")
        return
    _err(f"✗ the host firewall is NOT enforcing for '{MACHINE}'.")
    if result.error:
        _err(f"  probe: {result.error}")
    else:
        _err(f"  probe: {result.detail()}")
    _err("  Not installed, or installed for a slice that no longer exists (a host reboot, a")
    _err("  changed port range). foldyard never loads it itself — see the files and the steps:")
    _err("      fy machine host-firewall")
    raise SystemExit(1)


def host_wall(uninstall: bool = False) -> int:
    """`fy machine host-firewall`: the operator's side of the host wall. Makes the VM's persistent
    slice exist, renders the root-side files into the project's state dir, prints them and the
    exact commands that install (or, with ``uninstall``, remove) them — and probes whether the
    wall is enforcing right now. Exit 0 when it is, 1 when it is not (so a script can ask).
    foldyard runs none of the printed commands: what root does is in front of the operator."""
    if not _host_wall_wanted():
        print("host firewall is off for this project ([machine] host_firewall; lima backend only).")
        return 0
    if not hostwall.available():
        print("✗ this host can't enforce a host firewall: it needs `nft` (nftables) and cgroup v2.")
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
    print(f"host firewall for machine '{MACHINE}' — slice {slice_path or '(none)'}")
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
    print("the project's daemon port range, and once per host if the files were removed.")
    return 0 if result.enforcing else 1


def _note_host_wall_install() -> None:
    """`rm` deletes the VM, not the operator's wall install: the table is inert while the slice
    is empty and right again for the next VM of this name. Say so, once."""
    if _host_wall_wanted() and hostwall.available():
        _err(
            "ℹ the host firewall install is untouched (`fy machine host-firewall --uninstall` says"
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
# Where the boot script drops the embedded proxy CA for the wall's install to pick up (tmpfs,
# root-owned, rewritten every boot).
_GUEST_CA_PATH = "/run/fy-wall/proxy-ca.pem"


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


def _guest_ca() -> str | None:
    """The proxy CA to embed in walled provisioning (``proxy.guest_ca_pem``), or ``None`` when
    there is none or it is not a plain certificate — :func:`_ensure_guest_ca` refuses that
    before anything is recorded; rendering itself never raises (doctor reads the id too)."""
    from .plugins import proxy  # lazy: the proxy plugin is only needed for a walled VM

    try:
        return proxy.guest_ca_pem()
    except (OSError, ValueError):
        return None


def _ensure_guest_ca() -> None:
    """Before recording walled provisioning: generate the proxy CA if it doesn't exist yet (the
    supervisor's first run would, but the recording comes first), and refuse to go on with one
    that can't be embedded. A CA that still doesn't exist is reported, not fatal: the wall still
    holds, and stack containers trust the proxy from the next re-provisioning on."""
    from .plugins import proxy

    try:
        proxy.ensure_ca()
    except ValueError as e:
        _err(f"⚠ {e}, so containers in the walled VM won't trust the proxy (a stack service's")
        _err("  HTTPS call to a decrypted host fails). Point MITMPROXY_CA at your CA, or unset")
        _err("  it to use mitmproxy's own; then `fy machine stop && fy up`.")
        return
    try:
        pem = proxy.guest_ca_pem()
    except (OSError, ValueError) as e:
        _err(f"✗ the egress proxy's CA can't go into '{MACHINE}''s boot provisioning: {e}.")
        _err("  It is embedded in a script root runs at boot, so it must be a plain PEM")
        _err("  certificate from your computer. Check MITMPROXY_CA, or move the file away and")
        _err("  let foldyard generate a new one.")
        raise SystemExit(1) from None
    if pem is None:
        _err("⚠ no egress proxy CA yet, so containers in the walled VM won't trust the proxy (a")
        _err("  stack service's HTTPS call to a decrypted host fails). `fy doctor` generates it;")
        _err("  then `fy machine stop && fy up`.")


def _render_provisioning() -> tuple[str, str]:
    """The boot script for the CURRENT config and its id (a hash of the rendered content, so a
    flipped wall, a moved band, a changed asset or a new proxy CA all read as a different
    recording)."""
    ca = None
    if config.machine_wall():
        args = ["install", config.LIMA_HOST_GATEWAY, _wall_ports(), config.wall_proxy_url()]
        ca = _guest_ca()
        if ca is not None:
            args.append(_GUEST_CA_PATH)
    else:
        args = ["uninstall"]
    ca_step = (
        f"cat >{_GUEST_CA_PATH} <<'__FY_PROXY_CA__'\n{ca}__FY_PROXY_CA__"
        if ca is not None
        else ": # no proxy CA (wall off, or none generated yet)"
    )
    body = (
        _boot_asset()
        .read_text()
        .replace("@@WALL_ASSET@@", _wall_asset().read_text().rstrip("\n"))
        .replace("@@WALL_PATH@@", _WALL_SCRIPT_PATH)
        .replace("@@WALL_ARGS@@", " ".join(shlex.quote(a) for a in args))
        .replace("@@PROXY_CA_STEP@@", ca_step)
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


_BOOT_LOG = "/run/fy-wall/boot.log"


def _boot_progress() -> str:
    """How far the guest's boot got, read WITHOUT root: ``"done"`` — Lima's boot scripts finished
    (``/run/lima-boot-done`` holds the boot's instance id, written after every provision
    script — what Lima's own start waits for); ``"ours"`` — not finished, but
    foldyard's boot script started (it opens ``/run/fy-wall/boot.log`` first); ``"lima"`` — not
    finished and foldyard's script not started, so Lima's own provisioning, which runs before it,
    is still going or stuck. ``""`` when the guest can't be asked. Both files are on the guest's
    tmpfs ``/run``, so both are THIS boot's. A patchable seam (golden tests set it)."""
    probe = (
        "if [ -s /run/lima-boot-done ]; then echo done; "
        f"elif [ -e {_BOOT_LOG} ]; then echo ours; else echo lima; fi"
    )
    try:
        out = subprocess.run(
            ["limactl", "shell", MACHINE, "--", "sh", "-c", probe],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    got = out.stdout.strip()
    return got if out.returncode == 0 and got in ("done", "ours", "lima") else ""


def _unfinished_boot_lines(progress: str) -> list[str]:
    """What to tell the operator about a boot that hasn't finished; ``[]`` when it has, or when
    there's no telling (:func:`_boot_progress`)."""
    if progress == "lima":
        return [
            "  The VM is up, but Lima's own boot scripts haven't finished, and foldyard's runs",
            "  after them. On a VM created from the template that is most often its podman",
            "  install (`dnf`), which needs the Fedora mirrors: a slow or unreachable mirror",
            "  stalls it. It may still be going:",
            f"      what it runs:       limactl shell {MACHINE} -- ps -eo pid,etime,args",
            f"      the guest console:  ~/.lima/{MACHINE}/serial*.log",
            "  Once it settles, run `fy up` again. Restart only if it is stuck (a restart",
            "  ends an install that may be about to finish):   fy machine stop && fy up",
        ]
    if progress == "ours":
        return [
            "  foldyard's boot script started but hasn't reported: it failed, or a later Lima",
            "  step is still running. Its log is readable without root:",
            f"      limactl shell {MACHINE} cat {_BOOT_LOG}",
            "  If it failed, restart:   fy machine stop && fy up",
        ]
    return []


# The wall's recent refusals, written by the guest's fy-wall-denies unit (machine-wall.sh): a
# connection that ignored the proxy env, which the proxy therefore never logged.
_DENIES_PATH = "/run/fy-wall/denies.jsonl"


def wall_denies() -> list[dict] | None:
    """The walled guest's recent refusals, oldest first — each ``{boot, seq, chain, dst, proto,
    port}``, ``seq`` the kernel log's own sequence number within ``boot`` — read WITHOUT root.
    ``None`` when there is no report to read: not a walled Lima VM, the VM unreachable, or a guest
    provisioned before the reporter. Diagnosis only: nothing decides on it."""
    if BACKEND.name != "lima" or not config.machine_wall():
        return None
    try:
        out = subprocess.run(
            ["limactl", "shell", MACHINE, "--", "cat", _DENIES_PATH],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    rows = []
    for line in out.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("seq"), int) and row.get("dst"):
            rows.append(row)
    return rows


def _record_provisioning() -> None:
    """Record the boot script in the (STOPPED) instance config when it differs from what is
    recorded; refuse a RUNNING VM whose recording is stale (see the section comment)."""
    if BACKEND.name != "lima":
        return
    if config.machine_wall():
        _ensure_guest_ca()
    script, ident = _render_provisioning()
    if BACKEND.provision_id(MACHINE) == ident:
        return
    if state() == "running":
        _err(f"✗ '{MACHINE}' is running with STALE boot provisioning — the sudo grant, the VM")
        _err("  firewall, its port range or the proxy CA changed (or the VM predates boot")
        _err("  provisioning).")
        _err("  It applies at boot, as root, from the recorded config, so restart:")
        _err("      fy machine stop && fy up")
        raise SystemExit(1)
    _err(f"▶ recording boot provisioning for '{MACHINE}' ({_provision_want()}; VM user gets no")
    _err("  sudo)…")
    if not BACKEND.set_provision(MACHINE, script):
        _err(f"✗ recording the boot provisioning into '{MACHINE}' failed (`limactl edit`).")
        raise SystemExit(1)


# Lima copies the host's *_proxy variables into the guest at every start (`propagateProxyEnv`, on
# by default). Under the VM firewall they override the wall's own proxy env, so on a host behind a
# proxy the guest's podman pulled through THAT proxy and the wall refused every pull. Walled, the
# guest gets foldyard's proxy and nothing else; unwalled, the host's proxy is its only one.
_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy")


def _host_proxy_env() -> list[str]:
    return [v for p in _PROXY_VARS for v in (p.upper(), p) if os.environ.get(v)]


def _record_proxy_propagation() -> None:
    want = not config.machine_wall()
    have = BACKEND.proxy_propagation(MACHINE)
    if have is None or have == want:
        return
    if state() == "running":
        # `limactl edit` refuses a running VM, and what its guest got was decided at its start —
        # which can't be read back. Recorded at the next stop; warned about where it matters.
        if not want and (found := _host_proxy_env()):
            _err(f"⚠ '{MACHINE}' was started copying your proxy env ({', '.join(found)}) into the")
            _err("  guest, where it overrides the VM firewall's proxy: the guest's image pulls are")
            _err("  refused. Restart once to stop it:  fy machine stop && fy up")
        return
    _err(f"▶ {'copying' if want else 'not copying'} your proxy env into '{MACHINE}'s guest…")
    if not BACKEND.set_proxy_propagation(MACHINE, want):
        _err(f"✗ recording propagateProxyEnv into '{MACHINE}' failed (`limactl edit`).")
        raise SystemExit(1)


# ── the guest monitor's boot script: a SECOND recording, under its own marker (ADR-0032) ──
#
# Recorded beside the wall/sudo script, never inside it: that script's id covers every byte, so
# folding the monitor in would make every existing VM stale the day it shipped. A VM that never
# had the monitor on records nothing; one that had it on and turned it off keeps an `off`
# rendering, which removes the install at boot. Stale-while-running is refused like the wall's
# (it applies at boot, as root); what the running guest then does with it is a WARNING — the
# monitor only observes.


def _monitor_recording() -> tuple[str, str] | None:
    """``(script, id)`` the current config wants recorded, or ``None`` when nothing should be."""
    if BACKEND.name != "lima":
        return None
    on = monitor.wanted()
    if not on and not BACKEND.provision_id(MACHINE, monitor.MARKER):
        return None
    return monitor.render(on)


def _record_monitor() -> None:
    want = _monitor_recording()
    if want is None:
        return
    script, ident = want
    if BACKEND.provision_id(MACHINE, monitor.MARKER) == ident:
        return
    level = config.machine_monitor()
    if state() == "running":
        _err(f"✗ '{MACHINE}' is running with STALE guest-monitor provisioning ([machine] monitor")
        _err(f'  = "{level}", or a new monitor release). It applies at boot, as root, so restart:')
        _err("      fy machine stop && fy up")
        raise SystemExit(1)
    _err(f"▶ recording the guest monitor's boot provisioning for '{MACHINE}' ({level})…")
    if not BACKEND.set_provision(MACHINE, script, monitor.MARKER):
        _err(f"✗ recording the guest monitor into '{MACHINE}' failed (`limactl edit`).")
        raise SystemExit(1)


def _check_monitor() -> None:
    """After the guest's own checks: deliver the release if the guest waits for one, and say so
    when the monitor isn't observing. Never aborts."""
    if BACKEND.name != "lima" or not monitor.wanted():
        return
    want = _monitor_recording()
    if want is None:
        return
    problem = monitor.ensure(BACKEND, MACHINE, want[1])
    if problem:
        _err(f"⚠ the guest monitor is NOT observing: {problem}.")
        _err(f"  Its boot log:   limactl shell {MACHINE} cat /run/fy-monitor/boot.log")
        _err("  `fy up` carries on — the monitor only observes; `fy doctor` keeps the row red.")
    else:
        _err(f"✓ guest monitor: Tetragon {monitor.TETRAGON_VERSION}, {monitor.POLICY} loaded")


# ── the attributing forwarder's boot script: its own recording, under its own marker (ADR-0034) ─
#
# Recorded like the monitor's, beside the wall's and never inside it, for the same reason: every
# byte of the wall script is in its id. A VM that never turned attribution on records nothing; one
# that turned it off keeps an `off` rendering, which removes the install at boot. Unlike the
# monitor, a forwarder that isn't serving is refused, not warned about: it is the only way from a
# container to the proxy, so a dead one means no egress (it fails closed), and `fy up` says so
# rather than handing over a box that can reach nothing.

FORWARDER_SETTLE = 15.0  # seconds a freshly started forwarder may take to report "serving"


def _forwarder_recording() -> tuple[str, str] | None:
    """``(script, id)`` the current config wants recorded, or ``None`` when nothing should be."""
    if BACKEND.name != "lima":
        return None
    on = forwarder.wanted()
    if not on and not BACKEND.provision_id(MACHINE, forwarder.MARKER):
        return None
    return forwarder.render(on)


def _record_forwarder() -> None:
    want = _forwarder_recording()
    if want is None:
        return
    script, ident = want
    if BACKEND.provision_id(MACHINE, forwarder.MARKER) == ident:
        return
    level = config.machine_attribution()
    if state() == "running":
        _err(f"✗ '{MACHINE}' is running with STALE forwarder provisioning ([machine] attribution")
        _err(f'  = "{level}", a moved port band, or a new forwarder). It applies at boot, as root,')
        _err("  so restart:")
        _err("      fy machine stop && fy up")
        raise SystemExit(1)
    _err(f"▶ recording the attributing forwarder's boot provisioning for '{MACHINE}' ({level})…")
    if not BACKEND.set_provision(MACHINE, script, forwarder.MARKER):
        _err(f"✗ recording the forwarder into '{MACHINE}' failed (`limactl edit`).")
        raise SystemExit(1)


def _check_forwarder(settle: float = FORWARDER_SETTLE) -> None:
    """After the guest's own checks: the forwarder must report serving the recorded id. Read from
    the guest's own report (never the host's memory of what it recorded); refused when it isn't."""
    if BACKEND.name != "lima" or not forwarder.wanted():
        return
    want = _forwarder_recording()
    if want is None:
        return
    deadline = time.monotonic() + settle
    while True:
        report = forwarder.guest_report(BACKEND, MACHINE)
        problem = (
            "can't reach the VM over ssh to read its report"
            if report is None
            else report.problem(want[1])
        )
        starting = report is not None and report.state.startswith("starting")
        if not problem or not starting or time.monotonic() >= deadline:
            break
        time.sleep(1)
    if problem:
        _err(f"✗ the attributing forwarder is NOT serving: {problem}.")
        _err("  It is the only way from a container to the proxy, so nothing in the VM can reach")
        _err("  the egress proxy until it is. Its boot log and journal:")
        _err(f"      limactl shell {MACHINE} cat /run/fy-fwd/boot.log")
        _err(f"      limactl shell {MACHINE} journalctl -u fy-fwd --no-pager | tail")
        _err('  Restart (`fy machine stop && fy up`), or set [machine] attribution = "off".')
        raise SystemExit(1)
    _err("✓ attributing forwarder: serving (proxy requests carry the container they came from)")


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
    # No report at all may be a boot still in progress rather than a script that failed: a report
    # that differs means foldyard's script ran, so only "nothing" asks how far the boot got.
    unfinished = [] if got else _unfinished_boot_lines(_boot_progress())
    if unfinished:
        _err(f"✗ '{MACHINE}' hasn't finished booting: {'; '.join(problems)}.")
        for line in unfinished:
            _err(line)
        raise SystemExit(1)
    _err(f"✗ '{MACHINE}' did not apply its boot provisioning: {'; '.join(problems)}.")
    _err("  The script runs as root at boot from the recorded config; its log is readable")
    _err(f"  without root:   limactl shell {MACHINE} cat {_BOOT_LOG}")
    _err("  then restart:   fy machine stop && fy up")
    raise SystemExit(1)


# ── foldyard's two directories (lima only): recorded on a stopped VM, refused stale if running ──
#
# Lima builds its shares from lima.yaml at every start (vz and qemu alike), and `limactl edit`
# refuses a running instance — so adding ADR-0033's to-box and from-box applies on a RESTART, not
# a recreate, and is recorded the way the boot provisioning is: on the stopped VM, before it
# boots. A running VM that lacks them, or shares them differently (the to-box writable, the wrong
# share type), is REFUSED — fail closed like stale provisioning; `fy machine stop && fy up` keeps
# every volume. Only those two entries (and the pinned `mountType`) are compared and replaced: the
# checkout and worktrees-root mounts are left as the VM was created (`fy machine recreate` is
# still how those change — the drift warning in `ensure` says so), and a mount someone else added
# is left alone (`fy verify` fails it if it exposes your computer). Read from the EFFECTIVE
# config, so Lima's own `_config/override.yaml` widening the to-box (the most permissive
# `writable` wins) or swapping its share type is caught here too, not only by `verify`.


def _box_dir_paths() -> list[str]:
    return [str(boxmounts.from_box_dir()), str(boxmounts.to_box_dir())]


def _mounts_drift(main: Path, wt_root: Path) -> tuple[list[Mount], str, str] | None:
    """``(foldyard's two mounts as wanted, the mountType wanted, what differs)`` when the VM's
    effective config shares them differently; ``None`` when it matches or can't be read."""
    vmtype = BACKEND.vmtype(MACHINE)
    owned = set(_box_dir_paths())
    want = [m for m in _volumes(main, wt_root, vmtype) if m.location in owned]
    want_type = boxmounts.mount_type_for(vmtype)
    have = BACKEND.mount_set(MACHINE)
    if have is None:
        return None
    mine = sorted(m for m in have if m.location in owned or m.mount_point in owned)
    problems = []
    for m in sorted(want):
        if m not in mine:
            problems.append(f"{m.mount_point} ({'read-write' if m.writable else 'read-only'})")
    for m in mine:
        if m not in want:
            problems.append(f"no {m.mount_point} ({'read-write' if m.writable else 'read-only'})")
    for m in want:  # the cache, only where foldyard asks for one (see Mount.cache)
        held = next((h for h in mine if h == m), None)
        if m.cache and held is not None and held.cache != m.cache:
            problems.append(f"{m.mount_point} 9p cache {m.cache}, not {held.cache or 'unset'}")
    have_type = BACKEND.mount_type(MACHINE)
    if want_type and have_type != want_type:
        problems.append(f"mountType {want_type}, not {have_type or 'unset'}")
    return (want, want_type, "; ".join(problems)) if problems else None


def _record_mounts(main: Path, wt_root: Path) -> None:
    """Record foldyard's two directories in the (STOPPED) instance config when they differ; refuse
    a RUNNING VM whose set is stale (see the section comment). Lima only: the podman backend's
    mounts are fixed at `podman machine init`, and nothing about them changes."""
    if BACKEND.name != "lima":
        return
    drift = _mounts_drift(main, wt_root)
    if drift is None:
        return
    want, want_type, what = drift
    if state() == "running":
        _err(f"✗ '{MACHINE}' is running with a STALE mount set — it should share: {what}.")
        _err("  These are foldyard's to-box and from-box (ADR-0033). Lima applies a mount set at")
        _err("  start, so restart (containers and volumes are kept):")
        _err("      fy machine stop && fy up")
        raise SystemExit(1)
    _err(f"▶ recording foldyard's to-box/from-box into '{MACHINE}' ({what})…")
    if not BACKEND.set_mounts(MACHINE, want, want_type, _box_dir_paths()):
        _err(f"✗ recording the mount set into '{MACHINE}' failed (`limactl edit`).")
        raise SystemExit(1)
    still = _mounts_drift(main, wt_root)
    if still is not None:
        _err(f"✗ '{MACHINE}''s mount set is still not foldyard's after recording it: {still[2]}.")
        _err("  Lima's own config wins over the instance's — look for `mounts` / `mountType` in")
        _err("  ~/.lima/_config/override.yaml (or default.yaml), remove them, then `fy up`.")
        raise SystemExit(1)


def _check_box_dirs(main: Path, wt_root: Path) -> None:
    """Lima only: make foldyard's two directories, and refuse a state dir inside a writable mount
    — a read-only to-box nested in the checkout stays writable through it."""
    if BACKEND.name != "lima":
        return
    dirs = [boxmounts.to_box_dir(), boxmounts.from_box_dir()]
    clash = boxmounts.nested(dirs, [main, wt_root])
    if clash:
        d, m = clash[0]
        _err(f"✗ foldyard's {d.name} ({d}) overlaps the writable mount {m}: the state dir")
        _err("  (FOLDYARD_STATE_DIR) must sit outside the checkout and the worktrees root, or the")
        _err("  box could write what it may only read. Point it elsewhere, then `fy up`.")
        raise SystemExit(1)
    boxmounts.prepare()


def ensure(main: Path, wt_root: Path) -> None:
    """Provision the rootless machine (mounts ONLY repo + worktrees root) if absent, start it
    if stopped. No-op inside the box (manages its host VM from outside). A missing backend CLI
    is a hard error either way — one message when the consumer NAMED the backend, another when
    they inherited the default — because a silent skip leaves the socket unset and hands the
    whole stack to the host's own podman
    (:func:`machine_backend.default_unavailable_block`). Progress → stderr."""
    if config.in_box():
        return
    if not config.ambient_adopted():
        # The host reads the ADOPTED config, and with nothing adopted that's an empty one — whose
        # `[machine].firewall` is off. Never provision (or restart) the VM from it.
        _err("✗ nothing adopted for this checkout, so there is no reviewed [machine] config to")
        _err("  provision the VM from. `fy up` asks to adopt it; or `fy config diff`, then")
        _err("  `fy config adopt`.")
        raise SystemExit(1)
    if not BACKEND.available():
        if not config.machine_backend_explicit():
            _err(default_unavailable_block(BACKEND))
            raise SystemExit(1)
        _err(f"✗ [machine].backend = '{BACKEND.name}' but '{BACKEND.cli}' isn't installed.")
        _err(f"  Install it ({BACKEND.install_hint}) or name a different [machine].backend.")
        raise SystemExit(1)
    wt_root.mkdir(parents=True, exist_ok=True)
    _check_box_dirs(main, wt_root)
    if not exists():
        _err(f"▶ {BACKEND.name} machine '{MACHINE}' not found — initialising (rootless; mounts")
        extra = ", and foldyard's to-box + from-box" if BACKEND.name == "lima" else ""
        _err(f"  ONLY {main} and {wt_root}{extra})…")
        vols = _volumes(main, wt_root, BACKEND.resolve_vmtype())
        if not BACKEND.create(MACHINE, _resources(), vols):
            _err(
                f"✗ creating {BACKEND.name} machine '{MACHINE}' failed — see the output above. "
                "(On macOS, a sandbox like nono can block Apple Virtualization: create it from a "
                "plain terminal.)"
            )
            raise SystemExit(1)
    elif str(wt_root) not in mounts():
        _err(f"⚠ machine '{MACHINE}' has no '{wt_root}' mount (created before worktree")
        _err("  support). The main stack still works; worktree stacks need it. To enable")
        _err("  worktrees, recreate once:  fy machine recreate")
    # The mount set and the boot provisioning (the sudo grant + the wall) are recorded BEFORE the
    # VM boots — Lima applies both at start — and a running VM with either stale is refused here.
    _record_mounts(main, wt_root)
    _record_provisioning()
    _record_monitor()
    _record_forwarder()
    _record_proxy_propagation()
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
    _check_forwarder()
    # Housekeeping every VM wants (journal cap, API log level): over ssh, best-effort, before the
    # sandbox so its service restarts already see the drop-in.
    guestlog.ensure(BACKEND, MACHINE)
    _check_monitor()
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
        print(f"✗ run on your computer — the box can't {verb} its own machine")
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
    """Stop + remove + re-create the machine with foldyard's mount set. On the podman backend
    that is the only way to change it (``podman machine set`` has no volume flag); Lima applies a
    changed set at its next start (:func:`_record_mounts`), so there a recreate is for starting
    over. Interactive unless assume_yes."""
    if config.in_box() or not BACKEND.available():
        print("✗ run on your computer — the box can't recreate its own machine")
        return 1
    vols = _volumes(main, wt_root, BACKEND.resolve_vmtype())
    print(f"This stops + REMOVES {BACKEND.name} machine '{MACHINE}' and re-creates it with mounts:")
    for m in vols:
        print(f"    {m.mount_point}" + ("" if m.writable else "   (read-only)"))
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
    _check_box_dirs(main, wt_root)
    BACKEND.stop(MACHINE)
    BACKEND.remove(MACHINE)
    wt_root.mkdir(parents=True, exist_ok=True)
    if not BACKEND.create(MACHINE, _resources(), vols):
        print(f"✗ creating {BACKEND.name} machine '{MACHINE}' failed.")
        return 1
    _record_provisioning()  # the fresh VM is stopped: record before its first boot
    _record_monitor()
    _record_forwarder()
    _record_proxy_propagation()
    if not _start():  # one-VM-at-a-time aware on non-concurrent backends
        return 1
    _check_host_wall()
    _check_guest_provisioning()
    _check_forwarder()
    print(f"✓ machine '{MACHINE}' recreated with foldyard's mount set.")
    return 0
