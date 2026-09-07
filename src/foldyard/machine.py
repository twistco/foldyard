"""Rootless dev-VM lifecycle — core foldyard (init/start/recreate).

Backend-agnostic orchestration over :mod:`foldyard.machine_backend`: ``podman machine``
by default, Lima (``[machine].backend = "lima"``) for concurrent per-project VMs, or native
Linux/WSL2 podman (``[machine].backend = "native"``) as an explicit lower-isolation opt-in. The
backend is chosen once at import (``BACKEND``); this module only sequences create → start
→ socket and owns the cross-cutting policy (isolation mounts, the one-VM-at-a-time guard
for non-concurrent backends).

Host-only: the dev box runs INSIDE the engine boundary, so it can't manage its own host VM/engine
— ``ensure`` no-ops there and ``recreate`` refuses. For VM-backed backends, the isolation property
rests on the VM seeing ONLY the repo + the worktrees root (asserted by ``fy verify``).

``ensure`` progress → stderr (it runs inside ``foldyard shellenv``, whose stdout an ``eval``
consumes). ``recreate`` is interactive, so it talks on stdout.

When explicitly selected on Linux/WSL2, the native backend has no VM lifecycle, so
ensure/start/stop are no-ops over the host's rootless podman socket.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from . import config
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
    _err(f"▶ starting {BACKEND.name} machine '{MACHINE}'…")
    if not BACKEND.start(MACHINE):
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


# ── the in-VM egress wall ([machine].wall — lima only) ─────────────────────────────────
#
# The Mac-side proxy stays the chokepoint (allowlist, keyless injection, network log, creds all on
# the Mac); the wall makes the LIMA VM fail-closed: an nftables default-deny on the VM user's uid
# (which ALL rootless-container egress NATs out as) whose only opening is the Mac's foldyard
# daemons at the Lima host gateway. Provisioned by `assets/machine-wall/machine-wall.sh` on
# ensure/recreate: every machine create/START re-runs the idempotent install (self-healing — a
# tampered/drifted wall is re-asserted on the next boot), and a host-side marker catches
# `[machine].wall` flips between runs without shelling `limactl` on the already-running path.
# There is deliberately NO separate CLI verb: toggling IS editing foldyard.toml + `fy up`, and
# in-VM status/diagnosis lives in example/test_network.sh (limactl shell is right there).


def _wall_asset() -> Path:
    return Path(__file__).resolve().parent / "assets" / "machine-wall" / "machine-wall.sh"


def _wall_marker() -> Path:
    """Host-side record of the last wall state synced into MACHINE ("on"/"off"), so flipping
    ``[machine].wall`` takes effect on the next `fy up` without shelling `limactl` every run."""
    return config.state_dir() / f"machine-wall-{MACHINE}"


def _sh(cmd: list[str], stdin: Path | None = None) -> int:
    """Run a wall-provisioning command, streaming output; ``stdin`` (a file to feed the command)
    carries the wall script itself. The seam golden tests patch to capture the exact ``limactl``
    argv without a real Lima."""
    if stdin is None:
        return subprocess.run(cmd).returncode
    with stdin.open("rb") as f:
        return subprocess.run(cmd, stdin=f).returncode


def _wall_vm_state() -> tuple[bool, bool]:
    """Query the VM for the wall's ACTUAL state — the host marker can NEVER certify it. An
    out-of-band ``limactl delete``/factory-reset or an in-VM tamper drops the nft rules while the
    marker still reads ``on``; a wiped ``state_dir`` loses the marker while ``fy-wall.service``
    keeps enforcing. Returns ``(fy_wall_table_present, rootful_socket_masked)``. Best-effort:
    ``(False, False)`` if the VM is unreachable — which makes :func:`wall_sync` (re)provision or
    clean up rather than trust stale host-side state. A patchable seam (golden tests set it)."""
    if BACKEND.name != "lima":
        return (False, False)

    def _vm(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["limactl", "shell", MACHINE, "--", *args],
            capture_output=True,
            text=True,
            timeout=20,
        )

    try:
        present = _vm(["sudo", "nft", "list", "table", "inet", "fy_wall"]).returncode == 0
        # `is-enabled` prints "masked" (rc 1) when masked, "enabled"/"disabled" otherwise — the
        # install masks it, so anything but "masked" means the container-root→VM-root hole is open.
        masked = _vm(["systemctl", "is-enabled", "podman.socket"]).stdout.strip() == "masked"
    except (OSError, subprocess.SubprocessError):
        return (False, False)
    return (present, masked)


def _wall_ports() -> str:
    """The nft port elements the wall opens toward the Mac: each daemon base port + the full
    worktree-offset span (``config.worktree_offset`` is 1..89, main is 0), as ``base-base+89``
    ranges. Bases are THIS PROJECT's (allocated band or env override — ``config.proxy_port_base``/
    ``gcp_minter_port_base``), so a walled VM can reach only its own project's daemons, not a
    sibling project's."""
    bases = {config.proxy_port_base(), config.gcp_minter_port_base()}
    return ", ".join(f"{b}-{b + 89}" for b in sorted(bases))


def wall_sync(force: bool = False) -> bool:
    """Reconcile the in-VM wall with ``config.machine_wall()`` (assumes MACHINE is running).
    Skips when the marker already matches (unless ``force``). True on success/no-op."""
    desired = config.machine_wall()
    if BACKEND.name != "lima":
        if desired:
            _err(
                "⚠ [machine].wall is lima-only — ignored on the "
                f"'{BACKEND.name}' backend (preflight blocks `fy up`; fix foldyard.toml)."
            )
        return True
    # The marker records the wall's PORT SET too, not just on/off — the allocated band (or an
    # env override) can change between runs, and stale nft ranges would strand the VM's egress
    # on ports nothing listens on. A port change re-provisions exactly like an off→on flip.
    want = f"on {_wall_ports()}" if desired else "off"
    marker = _wall_marker()
    current = marker.read_text().strip() if marker.exists() else ""
    if desired:
        # The VM — not the marker — decides whether we can skip. Re-provision unless the marker
        # matches the wanted ports AND the VM actually has the rules loaded AND the rootful
        # podman.socket is still masked. Trusting the marker alone let a recreated/reset VM run
        # UNWALLED while foldyard reported locked-down, and missed a re-enabled rootful socket
        # (the container-root→VM-root bypass). This costs one `limactl shell` per steady-state
        # `fy up`; correctness over the marker's speed.
        present, masked = (False, False) if force else _wall_vm_state()
        if not force and current == want and present and masked:
            return True
        if present and not masked:
            _err("⚠ wall enforcing but the rootful podman.socket is NOT masked — re-provisioning")
    else:
        # Off: only skip if we can be SURE the VM carries no wall. An explicit 'off' marker we
        # wrote is trustworthy; an ABSENT marker is ambiguous (a wiped state_dir loses it while
        # fy-wall.service keeps enforcing), so probe the VM once and uninstall if it's still there.
        if not force and current == "off":
            return True
        present = False if force else _wall_vm_state()[0]
        if not force and not present and not current.startswith("on"):
            _record_wall(marker, want)  # confirmed clean — nothing to remove in the VM
            return True
    if desired:
        _err(f"▶ provisioning the egress wall into '{MACHINE}' (default-deny; Mac proxy only)…")
        gateway = config.LIMA_HOST_GATEWAY
        proxy_url = f"http://{gateway}:{config.proxy_port_base()}"
        args = ["install", gateway, _wall_ports(), proxy_url]
    else:
        _err(f"▶ removing the egress wall from '{MACHINE}'…")
        args = ["uninstall"]
    # Stream the script over stdin (`sudo bash -s --`) rather than staging it in the guest's
    # /tmp: a world-writable staging path could be swapped by a non-root guest process between
    # copy and root execution.
    cmd = ["limactl", "shell", MACHINE, "sudo", "bash", "-s", "--", *args]
    if _sh(cmd, stdin=_wall_asset()) != 0:
        return False
    _record_wall(marker, want)
    return True


def _record_wall(marker: Path, state: str) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(state)


def ensure(main: Path, wt_root: Path) -> None:
    """Provision the rootless machine (mounts ONLY repo + worktrees root) if absent, start it
    if stopped. No-op inside the box (manages its host VM from outside) and under the explicit
    ``native`` backend (no VM to manage). A missing backend CLI is a hard error either way — one
    message when the consumer NAMED the backend, another when they inherited the default — because
    a silent skip leaves the socket unset and hands the whole stack to the host's own podman
    (:func:`machine_backend.default_unavailable_block`). Progress → stderr."""
    if config.in_box():
        return
    if BACKEND.name == "native":
        return
    if not BACKEND.available():
        if not config.machine_backend_explicit():
            _err(default_unavailable_block(BACKEND))
            raise SystemExit(1)
        _err(f"✗ [machine].backend = '{BACKEND.name}' but '{BACKEND.cli}' isn't installed.")
        _err(f"  Install it ({BACKEND.install_hint}) or name a different [machine].backend.")
        raise SystemExit(1)
    wt_root.mkdir(parents=True, exist_ok=True)
    created = False
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
        created = True
    elif str(wt_root) not in mounts():
        _err(f"⚠ machine '{MACHINE}' has no '{wt_root}' mount (created before worktree")
        _err("  support). The main stack still works; worktree stacks need it. To enable")
        _err("  worktrees, recreate once:  fy machine recreate")
    started = False
    if state() != "running":
        if not _start():
            raise SystemExit(1)
        started = True
    elif not responsive():
        # Running per the backend, dead in fact — restart it rather than hand the caller a
        # socket nothing serves (see _revive).
        if not _revive():
            raise SystemExit(1)
        started = True
    # Re-assert the wall on every machine create/START (idempotent install → self-healing across
    # boots and against in-VM drift); on the already-running path, sync only when [machine].wall
    # flipped since the last run (the marker).
    if not wall_sync(force=created or started):
        _err(f"✗ syncing the egress wall into '{MACHINE}' failed — fix the above and re-run.")
        raise SystemExit(1)


def not_running_reason() -> str | None:
    """Why engine verbs can't reach the machine WITHOUT provisioning it — None when it's
    running (or the backend has no VM to manage). Never creates or starts anything: this is
    the read/teardown verbs' counterpart to :func:`ensure` (``fy down`` against a deleted
    machine must say "nothing to stop", not download a VM image)."""
    if BACKEND.name == "native":
        return None  # host podman directly — no VM lifecycle to be down
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
    """The shared guards for the stop/rm lifecycle verbs: no VM on the native backend, and
    host-only (the box must not manage the VM it lives in). None when OK to proceed."""
    if BACKEND.name == "native":
        print(f"✗ the native backend has no VM to {verb} — its engine is the host's podman.")
        return 1
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
    # Host-side wall state must not outlive the VM it describes (a later create re-probes anyway).
    _wall_marker().unlink(missing_ok=True)
    if not _stop_host_supervisor():
        return 1
    print(f"✓ machine '{MACHINE}' deleted. `fy up` / `fy machine ensure` re-creates it.")
    return 0


def recreate(main: Path, wt_root: Path, assume_yes: bool = False) -> int:
    """Stop + remove + re-create the machine so it mounts the worktrees root (VM mount sets
    are init-only — neither podman's ``--volume`` nor Lima's ``mounts:`` can be edited live).
    Interactive unless assume_yes."""
    if BACKEND.name == "native":
        print(
            "✗ the native backend has no VM to recreate — its stack runs directly on the host's "
            "rootless podman. The worktrees mount is a host dir, always visible; nothing to redo."
        )
        return 1
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
    if not _start():  # one-VM-at-a-time aware on non-concurrent backends
        return 1
    if not wall_sync(force=True):  # the fresh VM lost any previous provisioning
        print(
            f"✗ machine '{MACHINE}' recreated but the egress wall sync failed — re-run `fy up` "
            "(the wall re-asserts on every start), or check `limactl shell` connectivity."
        )
        return 1
    print(f"✓ machine '{MACHINE}' recreated with the worktrees mount.")
    return 0
