#!/usr/bin/env python3
"""`fy host` — the ONE foreground process that runs the Mac-side credential daemons.

Reads the authoritative mode file (devmode.py) every couple of seconds and reconciles
the daemons to it: switching github app↔user restarts the proxy with the other minter,
setting an axis to off stops its daemon, and a lapsed `user` TTL kills the daemon AND
writes the axis back to off (the structural guarantee that emergencies never linger).
Each tick also stamps a project-shared liveness heartbeat (so the launch paths can tell a
healthy holder from a wedged one) and refreshes each active worktree's mirror with daemon
health so box sessions' `fy mode` shows live status. Capability probes ride the tick too:
when an axis's merged verdict flips, the supervisor posts a macOS notification (lapse AND
heal), and a heal restarts the consumer's `[resnapshot_on_capability]` services on a worker
thread — boot-snapshotted credentials only re-fetch by rebooting.

Deliberately foreground, one terminal: daemons holding credentials stay visible, logs
interleave here, and Ctrl-C reliably stops everything — no pidfiles to go stale, no
orphan minter still serving tokens after you forgot about it. (If you ever want it
backgrounded, wrap THIS in launchd; don't grow a second daemon-management path.)

Daemon env (GH_APP_ID etc.) loads from ~/.foldyard/<project>/host.env (KEY=VALUE
lines; the process env wins on conflict). Stdlib only; Mac only.
"""

from __future__ import annotations

import enum
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from shutil import which

from . import allowlist, config, configpin, devmode, githeal, transcripts

TICK_SECONDS = 2.0
RESTART_BACKOFF = 10.0
MISSING_ENV_NAG = 30.0
# A live supervisor stamps its heartbeat every tick (~2s, plus per-tick daemon probes); a stamp
# this old while the singleton lock is HELD means the reconcile loop is wedged.
HEARTBEAT_STALE_SECONDS = 30.0
# Graceful stop of a bounced supervisor: its shutdown stops each child with its own 5s
# terminate→kill window, so give the whole process a generous SIGTERM budget before SIGKILL.
BOUNCE_TERM_WAIT = 20.0
BOUNCE_KILL_WAIT = 5.0


def tee_stdio_to_logfile() -> None:
    """Mirror this process's stdout+stderr — AND its child daemons' (mitmdump etc.), which inherit
    the fds — to ``host-supervisor.log``, while keeping whatever the parent gave us (a terminal for
    a foreground ``fy host``; /dev/null for the background ``machine up`` launch). So the TUI's
    log pane + a post-mortem ALWAYS have the proxy's output, however the supervisor was started.

    Done at the fd level (not just in ``log()``) so the children's output is captured too. Routes
    fd 1/2 through a pipe a daemon thread copies to both the real fd and the log file. Best-effort:
    any failure leaves plain stdout untouched — logging must never stop the supervisor booting."""
    try:
        log_path = config.supervisor_log_file()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(log_path, "ab", buffering=0)  # lives for the process lifetime (the pump thread)
        real_out = os.dup(1)  # the terminal (fg) or /dev/null (bg) — keep writing here too
        r, w = os.pipe()
        os.dup2(w, 1)  # fd 1/2 now feed the pipe; print() + children inherit these
        os.dup2(w, 2)
        os.close(w)
    except OSError:
        return  # couldn't set up the tee → carry on with plain stdout

    def pump() -> None:
        buf = b""
        with os.fdopen(r, "rb", buffering=0) as pipe:
            for chunk in iter(lambda: pipe.read(4096), b""):
                try:
                    os.write(real_out, chunk)  # terminal / devnull — raw, unchanged
                except OSError:
                    pass
                # The FILE copy (TUI tail + post-mortem) gets an ISO-8601 UTC prefix per line, so
                # both our [host] lines and mitmdump's inherited output carry a DATE — a bare
                # HH:MM:SS can't tell which day a line is from (this bit us on a stale run).
                stamped, buf = _stamp_log_lines(buf + chunk)
                try:
                    logf.write(stamped)
                except OSError:
                    pass

    threading.Thread(target=pump, daemon=True).start()


def _stamp_log_lines(buf: bytes) -> tuple[bytes, bytes]:
    """Prefix each COMPLETE line in ``buf`` with an ISO-8601 UTC timestamp; return (stamped bytes,
    leftover partial line). A trailing fragment with no newline is held back for the next read so a
    stamp never lands mid-line."""
    out = b""
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        out += stamp.encode() + b" " + line + b"\n"
    return out, buf


def _pidfile():
    return config.state_dir() / "host-supervisor.pid"


def _running_pid() -> int | None:
    """The pid of a background supervisor we launched, if it's still alive — else None (also
    clears a stale pidfile). Best-effort: a launcher dedup hint, NOT the supervisor's own state
    (it stays pidfile-free; foreground Ctrl-C is its real lifecycle)."""
    try:
        pid = int(_pidfile().read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)  # signal 0 = liveness probe, doesn't actually signal
        return pid
    except OSError:
        _pidfile().unlink(missing_ok=True)  # stale → self-heal
        return None


def _lockfile() -> Path:
    return config.state_dir() / "host-supervisor.lock"


# The held singleton lock fd. Kept open for the WHOLE process lifetime once `main` acquires it, so
# flock survives until the supervisor exits/crashes — then the OS drops it with the fd, leaving no
# stale state to clean up. Module-global because there's exactly one supervisor per process.
_lock_fd: int | None = None

# Computed once per process (see _code_fingerprint).
_fingerprint: str | None = None


def _code_fingerprint() -> str:
    """Cheap identity of the RUNNING foldyard code: a hash over the CONTENTS of every file in
    this package (keyed by relpath + size) plus the interpreter path. Hashing contents — not just
    (size, mtime) metadata — guarantees any code edit flips the fingerprint even when mtime is
    unreliable (a `git checkout` / `cp -p` can restore an old timestamp on same-size content).
    Editable installs (the normal `just foldyard install`) point this at the source checkout, so
    editing/updating foldyard changes it. The supervisor stamps it into the singleton lock at
    startup so launchers (`fy host`, `fy up` via ``ensure_background``) can tell a CURRENT
    supervisor from a STALE one — a long-lived supervisor computes daemon specs/allowlists from the
    code loaded at ITS start (the minter-allowlist-snapshot class of bug), and nothing short of a
    restart refreshes that. Best-effort: an unreadable file is skipped rather than crashing a
    launch path."""
    global _fingerprint
    if _fingerprint is None:
        digest = hashlib.sha256(sys.executable.encode())
        pkg = Path(__file__).resolve().parent
        for f in sorted(pkg.rglob("*")):
            if "__pycache__" in f.parts:
                continue
            try:
                if not f.is_file():
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            # relpath + size act as an unambiguous length-prefixed delimiter before the content
            digest.update(f"{f.relative_to(pkg)}:{len(data)}:".encode())
            digest.update(data)
            digest.update(b"\n")
        _fingerprint = digest.hexdigest()[:16]
    return _fingerprint


def acquire_singleton() -> bool:
    """Become THE supervisor for this project, or report that someone else already is.

    Takes a non-blocking exclusive ``flock`` on ``state_dir/host-supervisor.lock`` and HOLDS it for
    the process lifetime (the fd is intentionally never closed). One project ⇒ one state dir ⇒ one
    proxy on :8088 ⇒ one supervisor — this is the structural guarantee that EVERY launch path
    (foreground ``fy host``, the TUI toggle, the detached ``ensure_background``) converges on a
    single owner instead of two daemons fighting over the port. Returns True if we got the lock,
    False if a live supervisor holds it. Self-healing: the lock releases automatically on death, so
    a crashed supervisor never blocks the next one (no pidfile staleness to reason about).

    On success the holder's pid + code fingerprint are written INTO the lock file (the flock is on
    the fd, so the content is free metadata) — that's what lets a later launch detect a stale
    holder (`_holder_stale_reason`) and bounce it instead of leaving old code in charge.

    Best-effort on its OWN failure: if the lock file can't even be opened, return True rather than
    refuse to boot — losing the singleton guard is better than losing the proxy entirely."""
    global _lock_fd
    try:
        path = _lockfile()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return True  # can't create the lock file → don't let that stop the supervisor booting
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False  # another supervisor holds it
    _lock_fd = fd  # keep open → the lock is held until this process exits
    try:  # stamp holder metadata (pid + code fingerprint) for the staleness probe
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()} {_code_fingerprint()}\n".encode(), 0)
    except OSError:
        pass  # metadata is advisory; the lock itself is what matters
    return True


def _holder_info() -> tuple[int | None, str | None]:
    """(pid, fingerprint) the current lock holder stamped at its startup — (None, None) when the
    file is empty/unreadable (a pre-metadata supervisor, or one that never wrote it)."""
    try:
        parts = _lockfile().read_text().split()
    except OSError:
        return None, None
    pid = int(parts[0]) if parts and parts[0].isdigit() else None
    return pid, (parts[1] if len(parts) > 1 else None)


def _holder_pids() -> list[int]:
    """PIDs to signal when bouncing the lock holder: the stamped pid when it's alive, else
    whoever has the lock file open per ``lsof`` (covers pre-metadata supervisors — the exact
    processes a launcher must never `pkill` by name, because other projects' supervisors run
    the same binary). Excludes this process. [] when nothing can be identified."""
    pid, _ = _holder_info()
    if pid and pid != os.getpid():
        try:
            os.kill(pid, 0)
            return [pid]
        except OSError:
            pass  # stamped pid is dead — fall through to lsof
    if not which("lsof"):
        return []
    try:
        out = subprocess.run(
            ["lsof", "-t", str(_lockfile())], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(tok) for tok in out.split() if tok.isdigit() and int(tok) != os.getpid()]


def _stamp_heartbeat() -> None:
    """Write the current time to the PROJECT-shared heartbeat (``config.heartbeat_file``) — the
    liveness signal the launch paths read via :func:`_heartbeat_age`. Called once per reconcile
    tick, so a supervisor whose loop stops ticking goes stale within ``HEARTBEAT_STALE_SECONDS``.
    Best-effort: a write hiccup must never crash the loop (a briefly-missing stamp reads as
    'unknowable', not 'wedged', so it never triggers a false bounce)."""
    try:
        config.heartbeat_file().write_text(devmode._iso(devmode.now()) + "\n")
    except OSError as e:
        log(f"heartbeat: write failed: {e}")


def _heartbeat_age() -> float | None:
    """Seconds since the supervisor last stamped its PROJECT-shared heartbeat (its per-tick liveness
    signal, :func:`_stamp_heartbeat`) — or None when unknowable (no stamp yet / unreadable). Read on
    the launch path to distinguish a healthy holder from a wedged one. NOT the per-worktree mirror:
    that only refreshes while a worktree's box is up, so a down worktree's stale mirror used to read
    as 'the reconcile loop is wedged' and bounce a healthy supervisor."""
    try:
        stamp = devmode._parse(config.heartbeat_file().read_text().strip())
    except OSError:
        return None
    if stamp is None:
        return None
    return (devmode.now() - stamp).total_seconds()


def _holder_stale_reason() -> str | None:
    """Why the CURRENT lock holder should be bounced — or None when it's current and healthy.
    Called by the launch paths (`fy host`, `fy up`) when the singleton lock is already held:
    a supervisor snapshots code (daemon specs, minter allowlists) at ITS start, so 'running' is
    not enough — it must also be running the code that's installed NOW, and its reconcile loop
    must actually be ticking."""
    _pid, fingerprint = _holder_info()
    if fingerprint is None:
        return "it predates the supervisor health metadata (restarting once upgrades it)"
    if fingerprint != _code_fingerprint():
        return "the installed foldyard code changed since it started"
    age = _heartbeat_age()
    if age is not None and age > HEARTBEAT_STALE_SECONDS:
        return f"its heartbeat is {int(age)}s old — the reconcile loop looks wedged"
    return None


def _bounce_holder() -> bool:
    """Stop the current lock holder so a fresh supervisor can take over: SIGTERM (its shutdown
    stops every child daemon cleanly), wait for the flock to release, escalate to SIGKILL only
    if it won't die (orphaned daemons are then reaped by the successor's
    ``reap_orphan_listener`` on the port-conflict path). True iff the lock is free afterwards."""
    pids = _holder_pids()
    if not pids:
        return not _supervisor_running()  # nothing identifiable to signal
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + BOUNCE_TERM_WAIT
    while _supervisor_running() and time.monotonic() < deadline:
        time.sleep(0.25)
    if _supervisor_running():
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        deadline = time.monotonic() + BOUNCE_KILL_WAIT
        while _supervisor_running() and time.monotonic() < deadline:
            time.sleep(0.25)
    return not _supervisor_running()


def _supervisor_running() -> bool:
    """True iff another process already holds the singleton lock (a live supervisor). A best-effort
    launcher probe distinct from ``acquire_singleton``: it briefly tries the lock and immediately
    releases it (closing the fd), so it never competes with the real holder. Lets
    ``ensure_background`` skip spawning a doomed second supervisor in the common case — even one
    whose proxy hasn't bound :8088 yet, which the port probe alone would miss. False on any error
    (then the other guards + the authoritative ``main`` lock still apply)."""
    try:
        fd = os.open(_lockfile(), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False  # we got it → nobody's running
    except OSError:
        return True  # already held → a supervisor is live
    finally:
        os.close(fd)  # releases our probe lock (and the hold, if we just got it)


def stop() -> int:
    """Stop this project's host supervisor, whether it was launched in the foreground or
    detached. The singleton lock scopes the signal to this project; once no machine remains,
    there is no box that needs its proxy or checkout mirror. ``fy up`` / ``fy box up`` launch a
    fresh supervisor before they create or start a box.

    Keep this separate from ``_bounce_holder``'s launcher use: a stop is an intentional teardown,
    so it clears the detached-launch hint as well and reports a failed shutdown to its caller.
    """
    if not _supervisor_running():
        _pidfile().unlink(missing_ok=True)
        return 0
    if not _bounce_holder():
        print("✗ couldn't stop the host supervisor.", file=sys.stderr)
        return 1
    _pidfile().unlink(missing_ok=True)
    print("✓ host supervisor stopped.")
    return 0


def _offer_recommended() -> None:
    """Right after the config gate on a launch verb: offer any ``[proxy] recommend`` hosts the
    now-ADOPTED config carries that the operator hasn't answered yet
    (:func:`allowlist.offer_recommendations`).

    Ordering is the point — gate first, offer second — because the offer reads the adopted copy:
    a first `fy box up` on a locked-down repo adopts the config (Enter), is then offered its
    bootstrap hosts (pypi etc.) one consented yes at a time, and comes up with a working wall
    instead of a wall of refusals. An in-box edit to `recommend` can at most queue an ask that
    surfaces AFTER the operator reviews that edit at adoption. Best-effort, like the gate: a
    broken offer must never stop the yard starting."""
    try:
        cfg = devmode.worktree_config(config.active_worktree())
        with config.using(cfg):
            allowlist.offer_recommendations(
                interactive=sys.stdin.isatty(),
                prompt=input,
                echo=lambda m: print(m, file=sys.stderr, flush=True),
            )
    except SystemExit:
        raise
    except Exception as e:  # pragma: no cover — defensive: never block a launch verb
        print(f"⚠ couldn't offer the recommended egress hosts ({e})", file=sys.stderr, flush=True)


def ensure_background() -> int | None:
    """Launch `foldyard host` DETACHED so the always-on egress proxy (Phase A′) is up whenever the
    stack/box is — `machine up` calls this so a box that ALWAYS routes never hits a dead :8088.
    Mac-only and idempotent: a no-op in the box, where there's no podman, when a live supervisor
    holds this project's singleton lock, or when our pidfile records a live launch. A listening
    daemon alone is deliberately NOT a no-op signal: a dead supervisor can leave its child proxy
    orphaned on :8088, and a new supervisor is what safely reaps and restages that child. Returns
    the launched pid, or None when it no-op'd.

    `fy host` stays the foreground, Ctrl-C-stoppable path; this is the unattended companion that
    keeps the proxy alive across a plain `fy up`. We do NOT move the supervisor's logic here — we
    just spawn the same `foldyard host` process, logging to the state dir.

    A HELD lock is not enough to no-op: the holder must also be CURRENT (same installed code)
    and healthy (heartbeat ticking) — see ``_holder_stale_reason``. A stale holder is bounced
    and replaced right here, so a plain `fy up` after updating foldyard refreshes the daemons
    (minter allowlists etc.) instead of leaving last week's supervisor in charge."""
    if devmode.in_box() or not which("podman"):
        return None
    # Settle any foldyard.toml drift FIRST, on the caller's terminal (`fy up`/`fy box up` run this
    # in the foreground): the supervisor reconciles from the ADOPTED copy, so this is the operator's
    # adopt/revert/ignore moment. Before the "already running" short-circuit below, so a live
    # supervisor doesn't mean the prompt is skipped — adoption lands within a tick either way
    # (the running supervisor re-reads the pin, no bounce needed).
    configpin.gate("fy up")
    _offer_recommended()
    bounced = False
    if _supervisor_running():
        reason = _holder_stale_reason()
        if reason is None:
            return None  # a CURRENT supervisor holds the singleton lock — don't start a second
        print(f"▶ replacing the running host supervisor: {reason}…", file=sys.stderr)
        if not _bounce_holder():
            print(
                "⚠ couldn't stop the stale supervisor — leaving it in charge "
                "(`fy host --restart` to force, or Ctrl-C its terminal).",
                file=sys.stderr,
            )
            return None
        bounced = True
    if not bounced:  # after a bounce, lingering daemons are orphans the successor will reap
        if _running_pid() is not None:
            return None  # we launched one recently; it may still be binding its ports
    # No terminal here, and the spawned supervisor tees its OWN output to host-supervisor.log
    # (tee_stdio_to_logfile), so send the child's stdio to /dev/null — don't double-write the file.
    try:
        proc = subprocess.Popen(
            devmode.host_command(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach: survives this process, its own session/pgrp
        )
    except OSError as e:
        print(f"⚠ couldn't launch host daemons ({e}) — run `fy host` yourself", file=sys.stderr)
        return None
    try:
        _pidfile().write_text(str(proc.pid))
    except OSError:
        pass
    log_path = config.supervisor_log_file()
    print(
        f"▶ host daemons started in the background (pid {proc.pid}; log: {log_path}). "
        "Change posture with `fy mode …` / the TUI; `fy host` runs it in the foreground.",
        file=sys.stderr,
    )
    return proc.pid


# Keys already in the environment when this supervisor first loaded host.env — an operator-exported
# var wins over host.env (the precedence load_host_env has always had via setdefault). Snapshotted
# ONCE (the None guard) so the per-tick reload can make host.env EDITS live — a keyless token
# captured AFTER the supervisor started, or a rotated secret — without ever clobbering that ambient
# override. A fresh `foldyard host` process starts with this None, so it snapshots its own env.
_host_env_ambient: frozenset[str] | None = None


def load_host_env() -> None:
    """Merge ``~/.foldyard/<project>/host.env`` into ``os.environ``. Called at startup AND every
    reconcile tick (see :func:`reconcile_once`), so a secret written to host.env AFTER the
    supervisor booted takes effect within one tick — no ``fy host --restart``. Closes the edge where
    a keyless token captured after `fy up` sat in host.env but the always-on egress proxy still
    refused to launch (its ``requires`` gate reads ``os.environ``), nagging that the token was
    "missing" while the box — which always routes through that proxy — had no network.

    Precedence is unchanged from the original ``setdefault``: a var already in the supervisor's
    ambient environment at boot wins; every other key host.env declares is authoritative and updates
    IN PLACE, so a rotated token propagates too. Re-reading a tiny file each tick is cheap."""
    global _host_env_ambient
    if _host_env_ambient is None:  # first load this process — freeze the ambient overrides
        _host_env_ambient = frozenset(os.environ)
    host_env = config.host_env_file()
    if not host_env.exists():
        return
    for line in host_env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in _host_env_ambient:
            continue  # an operator-exported var wins over host.env (original setdefault semantics)
        os.environ[key] = value.strip().strip("'\"")


def _stage(pairs: list[tuple[str, str]]) -> None:
    """Snapshot each ``(src, dst)`` just before a daemon launches: copy src→dst when dst is missing
    or differs (idempotent). Lets a daemon point at a STABLE launch path instead of
    a working-tree file a git checkout could rewrite under a live process — e.g. the egress proxy's
    mitmdump addon (the proxy plugin's daemon ``stage``). Runs at every (re)launch, so a respawn
    re-snapshots fresh. A missing src is skipped (the cmd then fails visibly, as it would have)."""
    for src, dst in pairs:
        s, d = Path(src), Path(dst)
        if not s.exists():
            continue
        if not d.exists() or d.read_bytes() != s.read_bytes():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(s, d)


class Child:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.signature = repr((spec["cmd"], sorted(spec["env"].items())))
        self.started_at = time.monotonic()
        _stage(spec.get("stage", []))  # snapshot e.g. the proxy addon to its stable launch path
        self.proc = subprocess.Popen(spec["cmd"], env={**os.environ, **spec["env"]})
        log(f"started {name} (pid {self.proc.pid}): {spec['label']}")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        log(f"stopped {self.name}")


def log(message: str) -> None:
    print(f"[host] {message}", flush=True)


def expire_user_modes() -> dict:
    """Apply TTLs to the AUTHORITATIVE file: lapsed axes flip to their default rung, durably —
    and the posture SETTLES coherent: a dependent axis the expiry would strand on an error
    combination (a rung that can only fail without the identity it consumed) cascades down to
    its default in the SAME atomic write (devmode.settle_incoherent), so an emergency lapse
    always lands on a posture that works offline instead of one that errors until the next
    interactive `fy mode`."""
    lapsed = devmode.read(apply_expiry=False)
    live = devmode.read(apply_expiry=True)
    defaults = devmode.axis_defaults()
    flips = {a: defaults[a] for a in devmode.axes() if lapsed["mode"][a] != live["mode"][a]}
    if flips:
        settle = devmode.settle_incoherent(live["mode"])
        if settle:
            log(
                "TTL expiry strands dependent axes — settling "
                + ", ".join(f"{a}={r}" for a, r in settle.items())
            )
        flips.update(settle)
        log(f"TTL expired → {', '.join(f'{a}={r}' for a, r in flips.items())}")
        # force: an expiry is a de-escalation and must NEVER be refused — if settle couldn't
        # fully resolve it (no single downgrade helps), that's surfaced by `fy mode` and gates
        # the next interactive set instead of crashing the supervisor loop.
        devmode.set_mode(flips, force=True)
        return {**live["mode"], **settle}
    return live["mode"]


# Capability-probe results, keyed (worktree, probe name, the probed axis's rung). Each entry
# holds the last result plus the MONOTONIC deadline for the next run — real time on purpose, not
# devmode.now(): a `fy clock` fast-forward must lapse TTLs without stampeding every probe at
# once. The rung is part of the key because a probe closure bakes in the identity of the rung it
# was built for (gcp=sa probes the app SA; gcp=user your own token) — a rung change must never
# serve the previous rung's cached verdict for up to an interval. Module-global because there's
# one supervisor per process (like _lock_fd).
_probe_state: dict[tuple[str, str, str], dict] = {}


def run_capability_probes(wt: str, mode: dict) -> dict[str, dict]:
    """Run worktree ``wt``'s DUE capability probes and return its merged capability map:
    axis → {ok, detail, checked}. Probes come from the plugins (``capability_probes`` — "does
    the credential chain this rung promises actually work right now?"); results are cached
    per-probe until ``interval`` elapses, so the per-tick cost is one dict lookup. A probe that
    raises reads as failing (a broken probe must surface, not crash the tick). When one axis has
    several probes, a failing one wins (the axis is only as healthy as its weakest link). An
    axis whose probes all disappeared (rung back at default) simply drops out of the map —
    clearing its published state — and this worktree's stale cache entries are pruned, so a
    disabled-then-re-enabled probe starts fresh instead of resurrecting an old verdict."""
    results: dict[str, dict] = {}
    active_keys: set[tuple[str, str, str]] = set()
    for probe in devmode.capability_probes(mode):
        key = (wt, probe.name, mode.get(probe.axis, ""))
        active_keys.add(key)
        state = _probe_state.get(key)
        if state is None or time.monotonic() >= state["due"]:
            # A due probe may legitimately take seconds (a gcloud call with its own ≤20s
            # timeout). Re-stamp the heartbeat before EACH one so consecutive due probes can't
            # accumulate past HEARTBEAT_STALE_SECONDS and get a healthy supervisor bounced
            # mid-probe (gcp=user runs two 20s-timeout probes back to back, in sync forever —
            # 40s > the 30s stale threshold). Each probe now spends its own budget against a
            # fresh stamp; the per-probe contract stays "timeout well under 30s".
            _stamp_heartbeat()
            try:
                ok, detail = probe.check()
            except Exception as e:
                ok, detail = False, f"probe crashed: {type(e).__name__}: {e}"
            previous = state["ok"] if state else None
            state = {
                "ok": ok,
                "detail": detail,
                "checked": devmode._iso(devmode.now()),
                "due": time.monotonic() + probe.interval,
            }
            _probe_state[key] = state
            if not ok and previous is not False:  # new failure OR first probe failing
                log(f"⚠ capability {probe.name} ({probe.axis}) DEGRADED — {detail}")
            elif ok and previous is False:
                log(f"✓ capability {probe.name} ({probe.axis}) recovered — {detail}")
        merged = results.get(probe.axis)
        if merged is None or (merged["ok"] and not state["ok"]):
            results[probe.axis] = {
                "ok": state["ok"],
                "detail": state["detail"],
                "checked": state["checked"],
            }
    # Prune THIS worktree's entries whose probe (or its rung) is no longer active, so a
    # disabled-then-re-enabled probe can't resurrect a stale verdict from the old cache.
    for key in [k for k in _probe_state if k[0] == wt and k not in active_keys]:
        del _probe_state[key]
    return results


def write_capabilities(capabilities: dict[str, dict]) -> None:
    """Publish the probe results ({worktree: {axis: result}}; ``""`` = main) to the project
    state file `fy mode`/`fy state` read on the host. Skip creating the file while there's
    nothing to say (no active probes anywhere and no previous file) — but once it exists keep
    rewriting it, so results from a de-activated rung are cleared rather than lingering.
    Written to a sibling temp file + atomic replace so a concurrently-reading `fy mode` can
    never see a torn JSON."""
    path = config.capabilities_file()
    if not any(capabilities.values()) and not path.exists():
        return
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(capabilities, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        log(f"capabilities: write failed: {e}")
        tmp.unlink(missing_ok=True)


# The capability map most recently PUBLISHED ({worktree: {axis: result}}) — the baseline the
# per-axis edge detection diffs each tick's merged results against. Seeded from
# capabilities.json on first use, so a lapse recorded by a PREVIOUS supervisor still yields a
# heal edge after a restart (an in-memory-only baseline loses exactly that transition). None =
# not seeded yet. Module-global like _probe_state: one supervisor per process.
_published_caps: dict[str, dict] | None = None

# In-flight capability-heal restarts, keyed (worktree, axis) — a flapping probe must never
# stack a second `compose restart` onto one still running.
_resnapshot_inflight: set[tuple[str, str]] = set()


def _capability_baseline() -> dict[str, dict]:
    """The last published capability map (seeding from the state file on first call)."""
    global _published_caps
    if _published_caps is None:
        try:
            raw = json.loads(config.capabilities_file().read_text())
        except (OSError, ValueError):
            raw = {}
        _published_caps = raw if isinstance(raw, dict) else {}
    return _published_caps


def capability_edges(
    previous: dict[str, dict], current: dict[str, dict]
) -> list[tuple[str, str, bool, str]]:
    """The per-axis capability TRANSITIONS between two published maps: ``(worktree, axis,
    now_ok, detail)`` for every axis whose MERGED verdict flipped. Diffing the merged maps —
    not the per-probe cache — means an axis backed by several probes only heals when its
    weakest link does (gcp=user's two chains can't fire an up edge while one still fails). A
    first observation counts as an edge only when it is FAILING: a supervisor booting into an
    active lapse must still say so, while booting into health is just normal. An axis that
    disappeared (rung back at default) is not an edge — deactivation isn't a heal."""
    edges: list[tuple[str, str, bool, str]] = []
    for wt, axes in current.items():
        prev_axes = previous.get(wt)
        if not isinstance(prev_axes, dict):
            prev_axes = {}
        for axis, result in axes.items():
            ok = bool(result.get("ok"))
            prev = prev_axes.get(axis)
            prev_ok = prev.get("ok") if isinstance(prev, dict) else None
            if (prev_ok is None and not ok) or (prev_ok is not None and bool(prev_ok) != ok):
                edges.append((wt, axis, ok, str(result.get("detail") or "")))
    return edges


def _advance_capability_baseline(capabilities: dict[str, dict]) -> list[tuple[str, str, bool, str]]:
    """Diff this tick's map against the baseline, advance the baseline, return the edges. The
    baseline advances even if the file write later fails — it's the supervisor's own memory (the
    file is just the cross-restart seed), and re-diffing against a stale baseline would fire the
    same notification every tick."""
    global _published_caps
    edges = capability_edges(_capability_baseline(), capabilities)
    _published_caps = capabilities
    return edges


def _osa_string(s: str) -> str:
    """``s`` as an AppleScript double-quoted string literal (backslash + quote escaped) — probe
    details flow in here and can carry quotes from gcloud error output."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _notify(title: str, body: str) -> None:
    """Best-effort macOS notification — the PUSH surface for capability edges.
    ``fy mode``/``fy state``/the TUI are pull surfaces: when the 12h PAM grant expires under a
    running stack nobody is looking at them, and the supervisor log line used to be the only
    trace (the gcp-elevate-lapse incident).

    Prefers ``terminal-notifier`` (brew) over ``osascript``: macOS SILENTLY DROPS
    ``display notification`` — exit 0, no error — when the calling terminal app (Ghostty,
    iTerm2, …) lacks notification permission, and most terminals never even appear in the
    Notifications settings pane until one delivery succeeds, so the permission can't be
    granted. terminal-notifier registers as its own Notification Center app and macOS prompts
    allow/deny on first use — delivery no longer depends on which terminal launched `fy host`.

    Gated on ``[host] notifications`` (default on); silently a no-op with neither tool on PATH
    (non-Mac hosts, CI); never raises and never blocks the tick for long (5s timeout)."""
    if not config.host_notifications():
        return
    if which("terminal-notifier"):
        cmd = ["terminal-notifier", "-title", title, "-message", body]
    elif which("osascript"):
        script = f"display notification {_osa_string(body)} with title {_osa_string(title)}"
        cmd = ["osascript", "-e", script]
    else:
        return
    try:
        subprocess.run(cmd, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _react_to_capability_edges(edges: list[tuple[str, str, bool, str]]) -> None:
    """Push each capability edge to the operator and, on a heal, kick the configured
    resnapshot (``[resnapshot_on_capability]``): services that snapshot credentials at boot
    re-fetch only by restarting, so after a lapse the operator's ONLY job is the fix the
    DEGRADED notification names (e.g. ``just gcp-elevate``) — recovery, including the container
    bounce, is automatic. The restart runs on a daemon worker thread: it shells out to compose
    (possibly slow), and the tick owns the heartbeat launchers use to spot a wedged supervisor
    — blocking here would get a healthy supervisor bounced mid-restart."""
    for wt, axis, ok, detail in edges:
        where = f" [worktree {wt}]" if wt else ""
        if not ok:
            _notify(
                f"fy {config.project()}: {axis} DEGRADED{where}",
                detail or "capability chain failing",
            )
            continue
        cfg = devmode.worktree_config(wt)
        with config.using(cfg):
            services = config.resnapshot_on_capability().get(axis, [])
        body = detail or "capability chain healthy again"
        key = (wt, axis)
        if services and key not in _resnapshot_inflight:
            _resnapshot_inflight.add(key)
            threading.Thread(
                target=_resnapshot_worker,
                args=(cfg, wt, axis, services),
                daemon=True,
                name=f"resnapshot-{axis}" + (f"@{wt}" if wt else ""),
            ).start()
            body = f"restarting {', '.join(services)} — {body}"
        _notify(f"fy {config.project()}: {axis} recovered{where}", body)


def _resnapshot_worker(cfg: config.Config, wt: str, axis: str, services: list[str]) -> None:
    """The off-tick body of one capability-heal restart. Binds the worktree's config itself —
    a fresh thread starts with an empty contextvar context, so the tick's binding never reaches
    here — and always clears its in-flight key, even on a crash, so a later heal can retry."""
    from . import stack  # deferred: keep the supervisor's import hot path stack-free

    label = ", ".join(services) + (f" [worktree {wt}]" if wt else "")
    try:
        with config.using(cfg):
            log(f"capability {axis} healed — restarting {label} (resnapshot_on_capability)")
            ok, summary = stack.restart_services(services, worktree=wt)
        if ok:
            log(f"resnapshot: restarted {label}")
        else:
            log(f"resnapshot: restart FAILED for {label} — {summary}")
    finally:
        _resnapshot_inflight.discard((wt, axis))


# Config drift last REPORTED per worktree: the tree's digest while it differs from the adopted
# copy, "" while it matches. Keyed by worktree, module-global like _probe_state — one supervisor
# per process. Without it the tick would re-log (and re-notify) the same edit every 2 seconds.
_config_drift_seen: dict[str, str] = {}


def _report_config_drift(wt: str, cfg: config.Config) -> None:
    """Say — once per change — that this checkout's ``foldyard.toml`` differs from the copy the
    host adopted, and that the ADOPTED one is still what's running (:mod:`foldyard.configpin`).

    Reporting, never applying: that's the point of the pin. The notification fires only on the
    clean→drifted edge (a file being rewritten repeatedly is one event to an operator, not twenty),
    while every distinct content gets its own log line so the sequence is reconstructable
    afterwards. Best-effort — a state-dir read hiccup must not break the tick."""
    try:
        drift = configpin.inspect(cfg)
    except OSError as e:  # pragma: no cover — unreadable state dir
        log(f"config: drift check failed: {e}")
        return
    current = drift.tree_digest() if drift.changed else ""
    previous = _config_drift_seen.get(wt)
    if current == previous:
        return
    _config_drift_seen[wt] = current
    where = f" [worktree {wt}]" if wt else ""
    if not current:
        if previous:  # drifted → clean: adopted, reverted, or edited back by hand
            log(f"✓ config{where}: the checkout matches the adopted foldyard.toml again")
        return
    log(
        f"⚠ config{where}: foldyard.toml differs from the copy this host adopted ({current}) — "
        "STILL RUNNING THE ADOPTED ONE. Review with `fy config diff`, then `fy config adopt` "
        "or `fy config revert` (`fy up`/`fy host` also ask)."
    )
    if not previous:
        _notify(
            f"fy {config.project()}: config drift{where}",
            "foldyard.toml changed — the host keeps running the copy it adopted",
        )


def _port_listener_pids(port: int) -> list[int]:
    """PIDs LISTENing on TCP ``port`` (Mac ``lsof``). Best-effort: [] if lsof absent/errors."""
    if not which("lsof"):
        return []
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for tok in out.split():
        try:
            pids.append(int(tok))
        except ValueError:
            pass
    return pids


def _is_our_daemon(pid: int, spec: dict) -> bool:
    """True iff ``pid``'s command line is THIS PROJECT's own instance of the daemon ``spec``
    describes — so reaping it is safe. Every reapable daemon launches a script STAGED under this
    project's state_dir (the proxy's mitmdump addon, the gcp-minter — see ``_stage``), and that
    staged path is the project-scoped marker we match on, not just "is a foldyard daemon":
    ANOTHER project's supervisor legitimately runs the same daemons under its own singleton lock,
    and matching any foldyard proxy made two supervisors whose ports collided reap each other's
    daemons in an endless fight (the pre-band-allocation boot-loop). A foreign project's daemon —
    like any unrelated service — takes the caller's nag path instead."""
    # The marker(s): the spec cmd's absolute path(s) under OUR state_dir. Matched as the full
    # staged path, not a bare state_dir substring: `~/.foldyard/app` is a prefix of a sibling
    # project's `~/.foldyard/app2`, so a substring test would misread app2's live daemon as ours
    # and reap it — re-opening the very mutual-reap boot-loop this scoping exists to end. The
    # trailing separator + full filename can't prefix-collide. (A daemon whose cmd stages nothing
    # under state_dir has no safe marker and is never reaped.)
    prefix = str(config.state_dir()) + os.sep
    markers = [tok for tok in spec.get("cmd", []) if tok.startswith(prefix)]
    if not markers:
        return False
    try:
        cmd = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    words = cmd.split()
    if not words:
        return False
    # AND require the daemon's executable: the staged path alone also matches an editor /
    # `cat`/`grep` that merely has the file on its command line — never reap those. Python daemons
    # match any python interpreter (an orphan may have been launched by an older foldyard venv's
    # `sys.executable`, e.g. python3.12 vs today's python3.13). A Python console script such as
    # mitmdump has a second legitimate process shape on macOS: `ps` exposes its shebang expansion
    # as `python /path/to/mitmdump ...`, rather than putting mitmdump in argv[0]. Accept that only
    # when argv[1] is the EXACT executable from the spec; the project-scoped staged marker below
    # remains independently required. Non-Python executables match on exact basename equality —
    # a substring test would misread a lookalike (`not-mitmdump`) as the daemon itself.
    spec_exe = spec["cmd"][0]
    exe = os.path.basename(spec_exe)
    argv0 = os.path.basename(words[0])
    direct_exec = ("python" in argv0) if "python" in exe else (exe == argv0)
    python_console_script = (
        "python" in argv0 and "python" not in exe and len(words) > 1 and words[1] == spec_exe
    )
    if not direct_exec and not python_console_script:
        return False
    return any(marker in cmd for marker in markers)


def reap_orphan_listener(name: str, port: int, spec: dict) -> bool:
    """We hold the singleton lock, so nothing WE manage is on ``port`` — yet it's occupied. That's
    an ORPHANED daemon from a supervisor that died without reaping its child (SIGKILL/crash): the
    flock is gone but the mitmdump/minter it spawned still holds the port, which would make our
    fresh daemon crash-loop on EADDRINUSE forever. Reap it so ``fy host`` is genuinely idempotent.

    Returns True if the port is free to bind afterwards, False if we couldn't free it — because a
    FOREIGN process holds it (we refuse to kill that) or lsof can't see it. The caller then nags
    with the fix instead of crash-looping."""
    pids = _port_listener_pids(port)
    if any(not _is_our_daemon(pid, spec) for pid in pids):
        return False  # an unrelated service owns the port — never kill it; let the caller nag.
    for pid in pids:
        log(f"reaping orphaned {name} (pid {pid}) holding :{port} from a dead supervisor")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 3.0
    while devmode.probe(port) and time.monotonic() < deadline:
        time.sleep(0.1)
    if devmode.probe(port):  # ignored SIGTERM → SIGKILL
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        deadline = time.monotonic() + 2.0
        while devmode.probe(port) and time.monotonic() < deadline:
            time.sleep(0.1)
    return not devmode.probe(port)


def reconcile_once(children: dict[str, Child], nagged: dict[str, float]) -> None:
    """One supervisor tick: stamp the liveness heartbeat, refresh host.env (so newly-captured
    secrets are live), sweep project-wide allow grants, then reconcile every active worktree's
    desired daemon set into ``children``."""
    # Stamp the PROJECT-shared heartbeat first thing each tick: a launcher checking
    # _holder_stale_reason then sees a fresh tick, and a loop that later wedges (a hung probe/stop)
    # stops refreshing it and goes stale within HEARTBEAT_STALE_SECONDS. Project-level, not the
    # per-worktree mirror, so a worktree whose box is down doesn't read as a wedged supervisor.
    _stamp_heartbeat()

    # Re-read host.env every tick so a keyless token captured AFTER the supervisor started (e.g. the
    # `fy up` → capture-prompt ordering, or a token added from another session while this supervisor
    # holds the singleton lock) is live within a tick — the daemons' `requires` gate below reads
    # os.environ, so without this the always-on proxy stays down and the always-routing box loses
    # egress until a manual `fy host --restart`. load_host_env preserves ambient-var precedence.
    load_host_env()

    # Expire lapsed 'once' grants (rewrites the effective file the addon reads when something
    # changed, so the allow reverts live with no daemon restart). Best-effort — never crash the
    # reconcile loop on a transient file error. The egress allowlist is PROJECT-wide (shared by
    # every worktree's proxy), so it's swept once per tick, outside the per-worktree loop.
    try:
        allowlist.sweep()
    except OSError as e:
        log(f"allowlist: sweep failed: {e}")

    # Fast-forward each checkout's SHARED index after box-side commits (the split's staleness
    # illusion — phantom staged deletions in Mac GUIs). Conditional + non-destructive by design
    # (side-attributed, staged work carried or refused); self-guards + never raises. githeal.py.
    githeal.sweep(log)

    # Reconcile the UNION of every active worktree's desired daemons (ADR-0016 — the singleton
    # reconciles N postures). The ONE supervisor binds each worktree's config in
    # turn: expire its TTLs on ITS posture file, collect its uniquely-named proxy/minter daemons
    # (each on the worktree's offset port), and refresh ITS mirror. Daemon names carry a
    # "@<worktree>" suffix (main stays bare), so N listeners never collide; a worktree whose box
    # went down drops out of `desired` and its daemons are stopped just below.
    #
    # Mirrors go ONLY to checkouts whose box is genuinely up (`up_worktrees`): the mirror exists
    # for box sessions, and rewriting it every tick with no box up meant an idle supervisor kept
    # re-dropping `.dev-mode.json` into a clean checkout forever (dirtying release flows) — even
    # after the machine itself was deleted. Daemons keep the main fallback (`active_worktrees`)
    # so a box coming up always finds a live proxy + CA.
    up = devmode.up_worktrees()
    desired: dict[str, dict] = {}
    capabilities: dict[str, dict] = {}
    for wt in up or [""]:
        cfg = devmode.worktree_config(wt)  # the ADOPTED config for this checkout (configpin)
        with config.using(cfg):
            # Reconciling from the pin means a repo edit is silent by construction — so SAY it
            # happened, or "my config change did nothing" becomes the new mystery.
            _report_config_drift(wt, cfg)
            mode = expire_user_modes()
            # Fill in host-process env a plugin can derive from committed config (a Pulumi App
            # id, a deterministic SA email — see plugins.Plugin.env_defaults) BEFORE the
            # `requires` gate below reads os.environ, so github=app etc. work with no host.env
            # entry at all. setdefault: an ambient export or a real host.env secret always wins.
            for key, value in devmode.env_defaults(mode).items():
                os.environ.setdefault(key, value)
            desired.update(devmode.desired_daemons(mode))
            # Promote this checkout's agent transcripts into their durable host archive on the
            # configured interval (`[claude]/[codex] transcript_sync_seconds`; off by default).
            # Inside the bound block so it reads the ADOPTED config, and inside the `up` loop
            # because a box that's going away is already covered — box down / nuke / worktree
            # remove all call transcripts.sync_current on their way out.
            transcripts.sweep(log, wt, tick_seconds=TICK_SECONDS, notify=_notify)
            # Probe the EXTERNAL capability each active rung promises (PAM grant, ADC, token
            # validity) — due probes only; results feed the state file + this worktree's mirror
            # so `fy mode` on either side renders a DEGRADED axis instead of silent 401s.
            capabilities[wt] = run_capability_probes(wt, mode)
            if wt in up:
                devmode.write_mirror(
                    mode, devmode.read()["expires"], devmode.daemon_status(mode), capabilities[wt]
                )
    # Diff this tick's merged capability map against the last PUBLISHED one and react to the
    # edges: a lapse pushes a macOS notification (the mid-session surface `fy mode` can't be),
    # a heal additionally kicks the configured service resnapshot (consolidation proposal C).
    # Diffed against the published tier — seeded from capabilities.json — rather than the probe
    # cache, so edges are per-axis-merged and a lapse+heal spanning a supervisor restart still
    # fires.
    edges = _advance_capability_baseline(capabilities)
    write_capabilities(capabilities)
    _react_to_capability_edges(edges)

    for name in [n for n in children if n not in desired]:
        children.pop(name).stop()

    for name, spec in desired.items():
        step = _child_step(children.get(name), spec)
        if step in (ChildStep.KEEP, ChildStep.BACKOFF):
            continue
        if step is ChildStep.RESTART:
            log(f"{name} config changed — restarting")
            children.pop(name).stop()
        elif step is ChildStep.RESPAWN:
            exited = children.pop(name)
            log(f"{name} exited (rc {exited.proc.returncode}) — restarting")
        _spawn_child(name, spec, children, nagged)


class ChildStep(enum.Enum):
    """The per-daemon lifecycle decision — one explicit state machine instead of the implicit
    branch chain reconcile_once grew (mode-state consolidation proposal D). Pure decision,
    separated from its effects so it's unit-testable in isolation (`_child_step`)."""

    KEEP = "keep"  # alive, signature matches — nothing to do
    RESTART = "restart"  # alive, but the spec (cmd/env) changed — stop, then spawn fresh
    BACKOFF = "backoff"  # died young — wait out RESTART_BACKOFF before retrying
    RESPAWN = "respawn"  # exited after the backoff window — reap and spawn fresh
    SPAWN = "spawn"  # not running at all — spawn (spawn gates: env, port, exec)


def _child_step(child: Child | None, spec: dict) -> ChildStep:
    """Decide this tick's lifecycle step for one desired daemon (see :class:`ChildStep`)."""
    if child is None:
        return ChildStep.SPAWN
    if child.signature != repr((spec["cmd"], sorted(spec["env"].items()))):
        return ChildStep.RESTART
    if child.alive():
        return ChildStep.KEEP
    if time.monotonic() - child.started_at < RESTART_BACKOFF:
        return ChildStep.BACKOFF
    return ChildStep.RESPAWN


def _spawn_child(
    name: str, spec: dict, children: dict[str, Child], nagged: dict[str, float]
) -> None:
    """The spawn gates + launch for one desired daemon. Three ways NOT to spawn, each nagged at
    most every MISSING_ENV_NAG seconds: required env still missing (host.env not filled in yet),
    the port held by a FOREIGN process (our own orphan from a dead supervisor is reaped first —
    we hold the singleton lock, so nothing we manage is on it), or the exec itself failing."""
    missing = [k for k in spec["requires"] if not os.environ.get(k)]
    if missing:
        if time.monotonic() - nagged.get(name, 0.0) > MISSING_ENV_NAG:
            log(f"✗ {name} needs {', '.join(missing)} — set in {config.host_env_file()}; retrying")
            nagged[name] = time.monotonic()
        return
    # The port we're about to bind is already taken, yet we hold the singleton lock — so it's an
    # orphaned daemon from a dead supervisor. Reap it (idempotent restart); if we can't (a
    # foreign service holds it), nag with the fix instead of starting a doomed child that would
    # crash-loop on EADDRINUSE every backoff.
    port = spec.get("port")
    if port and devmode.probe(port) and not reap_orphan_listener(name, port, spec):
        if time.monotonic() - nagged.get(name, 0.0) > MISSING_ENV_NAG:
            log(
                f"✗ {name} can't bind :{port} — another process is listening (this "
                f"project's leftover daemon would have been reaped, so it's foreign — "
                f"`lsof -nP -iTCP:{port} -sTCP:LISTEN` to see whose). Free the port, or "
                "move this project's band: edit ~/.foldyard/ports.json or set "
                "FY_PROXY_PORT. Retrying"
            )
            nagged[name] = time.monotonic()
        return
    try:
        children[name] = Child(name, spec)
    except OSError as e:
        if time.monotonic() - nagged.get(name, 0.0) > MISSING_ENV_NAG:
            log(f"✗ can't start {name}: {e} (mitmproxy installed?); retrying")
            nagged[name] = time.monotonic()


def main(restart: bool = False) -> int:
    if devmode.in_box():
        raise SystemExit("✗ `fy host` runs ON THE MAC (the daemons need its gcloud/gh creds).")
    # Settle config drift BEFORE the singleton check: the common `fy host` is one where a healthy
    # supervisor already holds the lock and we return 0 below — which would skip the prompt in
    # exactly the case where a supervisor is running to be affected by the change. Adoption reaches
    # it within a tick either way (it re-reads the pin), so no bounce is needed.
    configpin.gate("fy host")
    _offer_recommended()
    if not acquire_singleton():
        # Someone already owns this project's daemons. If it's CURRENT and healthy, exit
        # cleanly — this is the idempotency backstop that makes the launch RACE-FREE: a stray
        # foreground `fy host`, two near-simultaneous `fy up`s, or a second worktree's `fy up`
        # all converge HERE instead of racing to bind :8088 and restart-looping on EADDRINUSE.
        # Bail BEFORE the tee so the loser never touches the shared log either. But a holder
        # running STALE code (or a wedged loop, or `--restart`) is bounced and replaced — a
        # supervisor snapshots daemon specs/allowlists at start, so `fy host` after a foldyard
        # update must hand over to the new code, not defer to the old.
        reason = "restart requested (--restart)" if restart else _holder_stale_reason()
        if reason is None:
            print(
                "✓ a foldyard supervisor is already running and current for this project "
                f"({config.state_dir()}); leaving it in charge. (`fy host --restart` bounces it.)",
                file=sys.stderr,
            )
            return 0
        print(f"▶ replacing the running supervisor: {reason}…", file=sys.stderr)
        if not _bounce_holder():
            print(
                "✗ couldn't stop the running supervisor — stop it yourself (Ctrl-C in its "
                f"terminal, or the pid in {_lockfile()}), then re-run `fy host`.",
                file=sys.stderr,
            )
            return 1
        deadline = time.monotonic() + 5.0
        while not acquire_singleton():
            if time.monotonic() > deadline:
                print("✗ singleton lock still held after stopping the supervisor.", file=sys.stderr)
                return 1
            time.sleep(0.2)
    # We now hold the singleton lock. Stamp the heartbeat immediately — BEFORE the (brief) init
    # below — so a concurrent launcher can't read a previous supervisor's lingering stale stamp and
    # bounce this freshly-booted one during the boot window.
    _stamp_heartbeat()
    tee_stdio_to_logfile()  # mirror our + the daemons' output to host-supervisor.log (TUI tails it)
    load_host_env()

    # Egress allowlist init: this IS the supervisor restart, so drop every 'until restart'
    # (session) and 'once' grant — they must not survive a restart — and (via clear_ephemeral →
    # write_effective) write the resolved effective file the proxy addon reads, so it exists from
    # the very first box request even before any grant. Best-effort: a write hiccup must not stop
    # the supervisor booting (and with default-deny on, a missing file fails safe — blocks).
    try:
        allowlist.clear_ephemeral()
    except OSError as e:
        log(f"allowlist: could not initialise the effective allowlist file: {e}")

    children: dict[str, Child] = {}
    nagged: dict[str, float] = {}
    stopping = False

    def shutdown(*_a):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if sys.__stderr__ is not None and sys.__stderr__.isatty():
        # Foreground run: tell the user how to reclaim the terminal WITHOUT killing the daemons
        # (the #1 "now I'm stuck in the foreground" papercut). Gated on a real tty, so the detached
        # `fy up`/`ensure_background` launch (stdio → /dev/null) doesn't print this misleading hint.
        log(
            "↳ to hand this terminal back WITHOUT stopping the daemons: Ctrl-Z, then `bg`, then "
            "`disown` (or `fy up` to (re)launch detached)."
        )
    log(f"mode file: {config.mode_file()}   env: {config.host_env_file()}")
    log(
        "supervising — change posture from another terminal (`fy mode …`) or the TUI; "
        "Ctrl-C stops everything."
    )

    while not stopping:
        reconcile_once(children, nagged)

        # (Each active worktree's mirror was refreshed inside the reconcile loop above.)
        # Sleep in small slices so Ctrl-C lands promptly.
        for _ in range(int(TICK_SECONDS / 0.2)):
            if stopping:
                break
            time.sleep(0.2)

    log("shutting down…")
    for child in children.values():
        child.stop()
    # Refresh every UP worktree's mirror so each box sees its daemons are now down (a down
    # box has no reader — don't recreate its mirror on the way out).
    for wt in devmode.up_worktrees():
        with config.using(devmode.worktree_config(wt)):
            state = devmode.read()
            devmode.write_mirror(
                state["mode"], state["expires"], devmode.daemon_status(state["mode"])
            )
    log("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
