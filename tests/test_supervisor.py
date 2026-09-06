"""`supervisor.ensure_background` — the unattended launcher `machine up` (stack.up) calls so the
always-on egress proxy (Phase A′) is up whenever the stack is. It must be Mac-only, idempotent
(never start a second supervisor), and detached. We stub the spawn + the liveness signals so the
guards are exercised with no real `foldyard host` process or podman."""

from __future__ import annotations

import fcntl
import os
import types
from pathlib import Path

import pytest

from foldyard import supervisor

# expire_user_modes iterates the active axes; bind a full resolved config so they exist.
pytestmark = pytest.mark.usefixtures("full_config_bound")


@pytest.fixture(autouse=True)
def _no_capability_probes(monkeypatch):
    """reconcile_once probes capabilities via the live registry, but the reconcile tests bind
    SimpleNamespace fake configs the registry can't resolve — default the probe source to "none"
    (symmetric with how they stub desired_daemons). The probe tests override this."""
    monkeypatch.setattr(supervisor.devmode, "capability_probes", lambda mode: [])


@pytest.fixture(autouse=True)
def _isolated_capability_state(monkeypatch, tmp_path):
    """Keep every test's capability machinery hermetic (conftest's first rule): the heartbeat
    (re-stamped before each due probe) and capabilities.json (the edge baseline's seed) both go
    to tmp — same filenames the state_dir-patching tests expect — and the module-global probe
    cache / edge baseline / in-flight set start fresh, so tests can't feed each other edges."""
    monkeypatch.setenv("FOLDYARD_HEARTBEAT_FILE", str(tmp_path / "host-supervisor.heartbeat"))
    monkeypatch.setenv("FOLDYARD_CAPABILITIES_FILE", str(tmp_path / "capabilities.json"))
    monkeypatch.setattr(supervisor, "_probe_state", {})
    monkeypatch.setattr(supervisor, "_published_caps", None)
    monkeypatch.setattr(supervisor, "_resnapshot_inflight", set())


def _patch(monkeypatch, tmp_path, *, in_box=False, has_podman=True, daemon_up=False):
    """Wire ensure_background's environment: state dir → tmp, the box/podman/daemon signals, a
    no-op host_command. Returns the list spawns land in (each entry the argv Popen was given)."""
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(supervisor.config, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(supervisor.devmode, "in_box", lambda: in_box)
    monkeypatch.setattr(supervisor, "which", lambda _x: "/usr/bin/podman" if has_podman else None)
    monkeypatch.setattr(supervisor.devmode, "read", lambda: {"mode": {"capture": "off"}})
    monkeypatch.setattr(
        supervisor.devmode, "daemon_status", lambda _m: {"egress-proxy": {"up": daemon_up}}
    )
    monkeypatch.setattr(supervisor.devmode, "host_command", lambda: ["foldyard", "host"])
    # The launch path runs the config-pin gate first, and under pytest there's no TTY — so an
    # unadopted checkout REFUSES the launch (configpin.gate). Not these tests' subject; they're
    # about the spawn/singleton logic. test_config_pin.py owns the gate's own behaviour.
    monkeypatch.setattr(supervisor.configpin, "gate", lambda _verb: "clean")

    spawns: list = []

    def _fake_popen(cmd, **kw):
        spawns.append(cmd)
        return types.SimpleNamespace(pid=4242)

    monkeypatch.setattr(supervisor.subprocess, "Popen", _fake_popen)
    return spawns


def test_launches_detached_when_nothing_is_serving(monkeypatch, tmp_path):
    spawns = _patch(monkeypatch, tmp_path)
    pid = supervisor.ensure_background()
    assert pid == 4242
    assert spawns == [["foldyard", "host"]]  # spawned the SAME `foldyard host`, not moved logic
    assert (tmp_path / "host-supervisor.pid").read_text() == "4242"  # dedup hint written


def test_noop_in_the_box(monkeypatch, tmp_path):
    spawns = _patch(monkeypatch, tmp_path, in_box=True)
    assert supervisor.ensure_background() is None and spawns == []  # the box can't run host daemons


def test_noop_without_podman(monkeypatch, tmp_path):
    spawns = _patch(monkeypatch, tmp_path, has_podman=False)
    assert supervisor.ensure_background() is None and spawns == []


def test_launches_when_only_an_orphan_daemon_is_serving(monkeypatch, tmp_path):
    # A port probe cannot prove that a supervisor owns the listener: a dead `fy host` can leave
    # mitmdump orphaned on :8088. With the singleton lock free, launch a replacement supervisor;
    # its first reconcile safely reaps this project's orphan and stages the current addon.
    spawns = _patch(monkeypatch, tmp_path, daemon_up=True)
    assert supervisor.ensure_background() == 4242
    assert spawns == [["foldyard", "host"]]


def test_noop_when_our_pidfile_records_a_live_launch(monkeypatch, tmp_path):
    # A previous launch's pid is still alive (daemons may not have bound their ports yet) → skip.
    spawns = _patch(monkeypatch, tmp_path)
    (tmp_path / "host-supervisor.pid").write_text("999999")
    monkeypatch.setattr(supervisor.os, "kill", lambda _pid, _sig: None)  # 999999 "alive"
    assert supervisor.ensure_background() is None and spawns == []


def test_stale_pidfile_is_cleared_and_a_fresh_one_launched(monkeypatch, tmp_path):
    # A dead pid in the pidfile must self-heal: os.kill raises → clear it → launch fresh.
    spawns = _patch(monkeypatch, tmp_path)
    (tmp_path / "host-supervisor.pid").write_text("999999")

    def _dead(_pid, _sig):
        raise OSError("no such process")

    monkeypatch.setattr(supervisor.os, "kill", _dead)
    assert supervisor.ensure_background() == 4242 and spawns == [["foldyard", "host"]]
    assert (tmp_path / "host-supervisor.pid").read_text() == "4242"  # rewritten to the live pid


# ── singleton lock: the structural one-supervisor-per-project guarantee ──────────────────────────


def test_acquire_singleton_is_exclusive(monkeypatch, tmp_path):
    # The first holder gets the lock; a SECOND attempt (simulating another process via its own fd —
    # flock conflicts across fds) is refused. The held fd stays open so the lock persists.
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    assert supervisor.acquire_singleton() is True
    held = supervisor._lock_fd
    assert held is not None

    # A separate fd to the same lock file = what a second process sees: the flock must fail.
    other = supervisor.os.open(tmp_path / "host-supervisor.lock", supervisor.os.O_RDWR)
    try:
        with pytest.raises(OSError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        supervisor.os.close(other)
        supervisor.os.close(held)  # release so the test process holds no lock afterwards
        supervisor._lock_fd = None


def test_supervisor_running_reflects_the_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    assert supervisor._supervisor_running() is False  # nobody holds it yet

    fd = supervisor.os.open(
        tmp_path / "host-supervisor.lock", supervisor.os.O_RDWR | supervisor.os.O_CREAT
    )
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a "live supervisor" holds the lock
    try:
        assert supervisor._supervisor_running() is True
    finally:
        supervisor.os.close(fd)
    assert supervisor._supervisor_running() is False  # released → free again


def test_stop_bounces_the_project_supervisor_and_clears_the_launch_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    (tmp_path / "host-supervisor.pid").write_text("4242")
    monkeypatch.setattr(supervisor, "_supervisor_running", lambda: True)
    monkeypatch.setattr(supervisor, "_bounce_holder", lambda: True)

    assert supervisor.stop() == 0
    assert not (tmp_path / "host-supervisor.pid").exists()


def test_stop_keeps_the_launch_hint_when_bouncing_the_supervisor_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    pidfile = tmp_path / "host-supervisor.pid"
    pidfile.write_text("4242")
    monkeypatch.setattr(supervisor, "_supervisor_running", lambda: True)
    monkeypatch.setattr(supervisor, "_bounce_holder", lambda: False)

    assert supervisor.stop() == 1
    assert pidfile.exists()


def test_stop_is_a_noop_when_no_project_supervisor_is_running(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    (tmp_path / "host-supervisor.pid").write_text("4242")
    monkeypatch.setattr(supervisor, "_supervisor_running", lambda: False)

    assert supervisor.stop() == 0
    assert not (tmp_path / "host-supervisor.pid").exists()


def test_ensure_background_noop_when_lock_is_held(monkeypatch, tmp_path):
    # Even with the proxy not yet bound (daemon_up=False) and no pidfile, a held singleton lock —
    # e.g. a foreground `fy host` still starting up (it stamps its metadata at acquire, before
    # binding any port) — must stop a second supervisor spawning.
    spawns = _patch(monkeypatch, tmp_path)
    monkeypatch.setattr(supervisor, "_heartbeat_age", lambda: None)  # too fresh to have ticked

    lock = tmp_path / "host-supervisor.lock"
    lock.write_text(f"999999 {supervisor._code_fingerprint()}\n")
    fd = supervisor.os.open(lock, supervisor.os.O_RDWR | supervisor.os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert supervisor.ensure_background() is None and spawns == []
    finally:
        supervisor.os.close(fd)


# ── orphan reap: idempotency when a dead supervisor left its proxy bound to the port ─────────────


def _patch_ps(monkeypatch, cmdline: str):
    class _R:
        stdout = cmdline

    monkeypatch.setattr(supervisor.subprocess, "run", lambda *a, **k: _R())


def _proxy_spec(state_dir) -> dict:
    """A daemon spec shaped like the proxy plugin's: mitmdump + the staged addon under state_dir."""
    return {
        "cmd": [
            "/venv/bin/mitmdump",
            "-s",
            f"{state_dir}/egress_proxy.py",
            "--listen-port",
            "41000",
        ]
    }


def _minter_spec(state_dir) -> dict:
    """A daemon spec shaped like the gcp plugin's: python + the staged minter under state_dir."""
    return {"cmd": ["/venv/bin/python3.13", f"{state_dir}/gcp_minter.py"]}


def test_is_our_daemon_matches_this_projects_addon_path(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path / "tangible")
    _patch_ps(monkeypatch, f"mitmdump -s {tmp_path}/tangible/egress_proxy.py --listen-port 41000")
    assert supervisor._is_our_daemon(1, _proxy_spec(tmp_path / "tangible")) is True


def test_is_our_daemon_matches_python_launched_mitmdump_on_macos(monkeypatch, tmp_path):
    # macOS `ps` exposes a Python console script's shebang expansion: argv[0] is the interpreter
    # and the mitmdump executable is argv[1]. This is the real orphan shape emitted by a uv tool
    # install; rejecting it made `fy up` nag forever instead of reaping its own leftover proxy.
    state_dir = tmp_path / "tangible"
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: state_dir)
    _patch_ps(
        monkeypatch,
        f"/uv/foldyard/bin/python /venv/bin/mitmdump -s {state_dir}/egress_proxy.py "
        "--listen-host 0.0.0.0 --listen-port 41000 --set flow_detail=0",
    )
    assert supervisor._is_our_daemon(1, _proxy_spec(state_dir)) is True


def test_is_our_daemon_rejects_other_python_script_with_our_addon_path(monkeypatch, tmp_path):
    # Merely mentioning our staged addon is insufficient: the script behind Python must be the
    # exact daemon executable from the spec, so an unrelated process is never reaped.
    state_dir = tmp_path / "tangible"
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: state_dir)
    _patch_ps(monkeypatch, f"python /tmp/not-mitmdump -s {state_dir}/egress_proxy.py")
    assert supervisor._is_our_daemon(1, _proxy_spec(state_dir)) is False


def test_is_our_daemon_rejects_a_lookalike_direct_executable(monkeypatch, tmp_path):
    # Direct-exec matching is exact basename equality: a lookalike binary whose name merely
    # CONTAINS the daemon's (`not-mitmdump`) must not be misread as the mitmdump process itself,
    # even with our staged addon path on its command line.
    state_dir = tmp_path / "tangible"
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: state_dir)
    _patch_ps(monkeypatch, f"not-mitmdump -s {state_dir}/egress_proxy.py --listen-port 41000")
    assert supervisor._is_our_daemon(1, _proxy_spec(state_dir)) is False


def test_is_our_daemon_rejects_a_sibling_projects_proxy(monkeypatch, tmp_path):
    # Another project's supervisor legitimately runs its own mitmdump under its own singleton
    # lock. Matching ANY foldyard proxy made two projects whose ports collided reap each other's
    # daemons every tick — an endless kill loop (the 2026-07 boot-loop). A sibling's proxy must
    # take the foreign-listener nag path instead.
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path / "tangible")
    _patch_ps(monkeypatch, f"mitmdump -s {tmp_path}/claude-code-log/egress_proxy.py")
    assert supervisor._is_our_daemon(1, _proxy_spec(tmp_path / "tangible")) is False


def test_is_our_daemon_rejects_a_prefix_named_sibling(monkeypatch, tmp_path):
    # `~/.foldyard/app` is a path-PREFIX of `~/.foldyard/app2` — a bare-substring scope would
    # misread app2's live proxy as ours and reap it (the boot-loop, again). The exact staged
    # addon filename (`…/egress_proxy.py`) can't prefix-collide.
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path / "app")
    _patch_ps(monkeypatch, f"mitmdump -s {tmp_path}/app2/egress_proxy.py --listen-port 8088")
    assert supervisor._is_our_daemon(1, _proxy_spec(tmp_path / "app")) is False


def test_is_our_daemon_rejects_an_unrelated_command(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path / "tangible")
    _patch_ps(monkeypatch, f"node {tmp_path}/tangible/server.js --port 41000")
    assert supervisor._is_our_daemon(1, _proxy_spec(tmp_path / "tangible")) is False


def test_is_our_daemon_rejects_a_non_mitmdump_holding_the_addon_path(monkeypatch, tmp_path):
    # The addon path alone is NOT enough: an editor / `cat` with the staged addon file on its
    # command line must never be misread as the proxy and reaped.
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path / "tangible")
    _patch_ps(monkeypatch, f"cat {tmp_path}/tangible/egress_proxy.py")
    assert supervisor._is_our_daemon(1, _proxy_spec(tmp_path / "tangible")) is False


def test_is_our_daemon_matches_a_staged_minter_from_an_older_python(monkeypatch, tmp_path):
    # THE 2026-07-10 incident: an orphaned gcp-minter held :8188 but the ownership test only knew
    # the mitmdump shape, so the supervisor nagged "foreign" forever about its own leftover child.
    # A python daemon must match on the staged script even when the orphan was launched by a
    # DIFFERENT interpreter (an older foldyard venv's python3.12 vs the spec's python3.13).
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path / "tangible")
    _patch_ps(monkeypatch, f"/old/venv/bin/python3.12 {tmp_path}/tangible/gcp_minter.py")
    assert supervisor._is_our_daemon(1, _minter_spec(tmp_path / "tangible")) is True


def test_is_our_daemon_rejects_a_non_python_holding_the_minter_path(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path / "tangible")
    _patch_ps(monkeypatch, f"cat {tmp_path}/tangible/gcp_minter.py")
    assert supervisor._is_our_daemon(1, _minter_spec(tmp_path / "tangible")) is False


def test_is_our_daemon_rejects_a_sibling_projects_minter(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path / "tangible")
    _patch_ps(monkeypatch, f"/venv/bin/python3.13 {tmp_path}/other/gcp_minter.py")
    assert supervisor._is_our_daemon(1, _minter_spec(tmp_path / "tangible")) is False


def test_is_our_daemon_rejects_the_packaged_unstaged_minter(monkeypatch, tmp_path):
    # An orphan from a PRE-STAGING foldyard ran the packaged minter.py, whose path is shared by
    # every project — no safe project scoping, so it takes the nag path (kill it by hand once).
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path / "tangible")
    _patch_ps(monkeypatch, "/venv/bin/python3.13 /venv/lib/foldyard/plugins/gcp_metadata/minter.py")
    assert supervisor._is_our_daemon(1, _minter_spec(tmp_path / "tangible")) is False


def _patch_reap(monkeypatch, *, pids, ours, frees_on):
    """Wire reap_orphan_listener's world: which pids hold the port, whether each is our proxy, and
    after WHICH signal the port frees (``frees_on`` ∈ {SIGTERM, SIGKILL, None}; None = stays bound).
    probe() is bound until that signal lands; an incrementing clock makes the wait deadlines fire."""
    monkeypatch.setattr(supervisor, "_port_listener_pids", lambda _p: pids)
    monkeypatch.setattr(supervisor, "_is_our_daemon", lambda pid, _spec: ours.get(pid, False))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(
        supervisor.devmode,
        "probe",
        lambda _p, host=None: frees_on not in [sig for _, sig in killed],
    )
    clock = {"t": 0.0}

    def _mono():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(supervisor.time, "monotonic", _mono)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)
    monkeypatch.setattr(supervisor, "log", lambda _m: None)
    return killed


def test_reap_kills_our_orphan_and_frees_the_port(monkeypatch):
    # An orphaned mitmdump (our proxy) holds :8088 → SIGTERM it; it releases → port free → True.
    killed = _patch_reap(
        monkeypatch, pids=[11924], ours={11924: True}, frees_on=supervisor.signal.SIGTERM
    )
    assert supervisor.reap_orphan_listener("egress-proxy", 8088, _proxy_spec("/state")) is True
    assert killed == [(11924, supervisor.signal.SIGTERM)]


def test_reap_escalates_to_sigkill_when_sigterm_ignored(monkeypatch):
    # Port stays bound after SIGTERM (past the deadline) → escalate to SIGKILL → then it frees.
    killed = _patch_reap(
        monkeypatch, pids=[222], ours={222: True}, frees_on=supervisor.signal.SIGKILL
    )
    assert supervisor.reap_orphan_listener("egress-proxy", 8088, _proxy_spec("/state")) is True
    assert (222, supervisor.signal.SIGTERM) in killed and (222, supervisor.signal.SIGKILL) in killed


def test_reap_refuses_to_kill_a_foreign_listener(monkeypatch):
    # Something that ISN'T our proxy holds the port → never kill it; return False so the caller nags.
    killed = _patch_reap(monkeypatch, pids=[555], ours={555: False}, frees_on=None)
    assert supervisor.reap_orphan_listener("egress-proxy", 8088, _proxy_spec("/state")) is False
    assert killed == []


def _fake_cfg(wt: str) -> types.SimpleNamespace:
    """A ``config.Config``-shaped stand-in for the reconcile tests: ``config.using()`` only needs an
    object, but the tick also asks :mod:`foldyard.configpin` whether that checkout's foldyard.toml
    still matches the copy the host adopted — so it needs a ``repo_root`` too. A path that doesn't
    exist reads as "no config either side", i.e. no drift, which is what these tests want."""
    return types.SimpleNamespace(worktree=wt, repo_root=Path("/nonexistent-checkout"))


class _StopTick(Exception):
    """Sentinel raised from the stubbed sweep to break main()'s reconcile loop after one tick (it's
    NOT an OSError, so the loop's best-effort guard around sweep doesn't swallow it)."""


def test_reconcile_once_unions_selected_worktree_daemons(monkeypatch, tmp_path):
    cfgs = {"": _fake_cfg(""), "feat": _fake_cfg("feat")}
    modes = {"": {"github": "off"}, "feat": {"github": "app"}}
    desired = {
        "": {
            "egress-proxy": {
                "cmd": ["proxy-main"],
                "env": {"WORKTREE": ""},
                "requires": [],
                "port": None,
                "label": "main",
            }
        },
        "feat": {
            "egress-proxy@feat": {
                "cmd": ["proxy-feat"],
                "env": {"WORKTREE": "feat"},
                "requires": [],
                "port": None,
                "label": "feat",
            }
        },
    }
    launched: list[str] = []

    class FakeChild:
        def __init__(self, name, spec):
            self.name = name
            self.spec = spec
            self.signature = repr((spec["cmd"], sorted(spec["env"].items())))
            self.started_at = 0.0
            self.proc = types.SimpleNamespace(returncode=None)
            launched.append(name)

        def alive(self):
            return True

        def stop(self):
            launched.append(f"stop:{self.name}")

    monkeypatch.setattr(supervisor, "_stamp_heartbeat", lambda: None)  # don't touch the real home
    monkeypatch.setattr(supervisor.allowlist, "sweep", lambda: None)
    monkeypatch.setattr(supervisor.githeal, "sweep", lambda log: None)
    monkeypatch.setattr(supervisor.devmode, "up_worktrees", lambda: ["", "feat"])
    monkeypatch.setattr(supervisor.devmode, "worktree_config", lambda wt: cfgs[wt])
    monkeypatch.setattr(
        supervisor, "expire_user_modes", lambda: modes[supervisor.config.active_worktree()]
    )
    monkeypatch.setattr(
        supervisor.devmode,
        "desired_daemons",
        lambda mode: desired[supervisor.config.active_worktree()],
    )
    monkeypatch.setattr(supervisor.devmode, "env_defaults", lambda mode: {})
    monkeypatch.setattr(supervisor.devmode, "read", lambda: {"expires": {}})
    monkeypatch.setattr(supervisor.devmode, "daemon_status", lambda mode: {})
    monkeypatch.setattr(supervisor.devmode, "write_mirror", lambda *a, **k: None)
    monkeypatch.setattr(supervisor, "Child", FakeChild)

    children: dict = {}
    supervisor.reconcile_once(children, {})

    assert set(children) == {"egress-proxy", "egress-proxy@feat"}
    assert launched == ["egress-proxy", "egress-proxy@feat"]


def test_reconcile_once_applies_env_defaults_via_setdefault(monkeypatch, tmp_path):
    # The environment-default application loop: plugin-derived defaults (env_defaults) land in
    # os.environ BEFORE the daemons' `requires` gate reads it, but only as setdefault — an
    # ambient export (or a host.env secret loaded earlier) always wins over the derived value.
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(supervisor.allowlist, "sweep", lambda: None)
    monkeypatch.setattr(supervisor.githeal, "sweep", lambda log: None)
    monkeypatch.setattr(supervisor.devmode, "up_worktrees", lambda: [])
    monkeypatch.setattr(supervisor.devmode, "worktree_config", _fake_cfg)
    monkeypatch.setattr(supervisor, "expire_user_modes", lambda: {})
    monkeypatch.setattr(supervisor.devmode, "desired_daemons", lambda mode: {})
    monkeypatch.setattr(
        supervisor.devmode,
        "env_defaults",
        lambda mode: {"GH_APP_ID": "1234567", "GH_REPO": "derived-repo"},
    )
    monkeypatch.delenv("GH_APP_ID", raising=False)
    monkeypatch.setenv("GH_REPO", "ambient-wins")

    supervisor.reconcile_once({}, {})

    assert os.environ["GH_APP_ID"] == "1234567"  # missing default gets filled in
    assert os.environ["GH_REPO"] == "ambient-wins"  # existing ambient value untouched


def test_stamp_and_read_heartbeat_roundtrip(monkeypatch, tmp_path):
    # _stamp_heartbeat writes the current time to the PROJECT-shared heartbeat; _heartbeat_age reads
    # it back as a small positive age. Missing file → None (unknowable), so it never false-fires as
    # 'wedged'. The heartbeat lives at state_dir (project-level), NOT a per-worktree mirror — that's
    # the fix for a fresh worktree's stale mirror bouncing a healthy supervisor.
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    assert supervisor._heartbeat_age() is None  # nothing stamped yet → unknowable
    supervisor._stamp_heartbeat()
    assert (tmp_path / "host-supervisor.heartbeat").exists()
    age = supervisor._heartbeat_age()
    assert age is not None and 0 <= age < 5


def test_reconcile_once_stamps_the_heartbeat(monkeypatch, tmp_path):
    # Every tick refreshes the liveness stamp so a launcher sees a healthy holder (the
    # per-worktree loop is stubbed inert here, isolating the stamp).
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(supervisor.allowlist, "sweep", lambda: None)
    monkeypatch.setattr(supervisor.githeal, "sweep", lambda log: None)
    monkeypatch.setattr(supervisor.devmode, "up_worktrees", lambda: [])
    monkeypatch.setattr(supervisor.devmode, "worktree_config", _fake_cfg)
    monkeypatch.setattr(supervisor, "expire_user_modes", lambda: {})
    monkeypatch.setattr(supervisor.devmode, "desired_daemons", lambda mode: {})
    monkeypatch.setattr(supervisor.devmode, "env_defaults", lambda mode: {})
    supervisor.reconcile_once({}, {})
    assert (tmp_path / "host-supervisor.heartbeat").exists()


def test_reconcile_serves_main_daemons_but_writes_no_mirror_when_nothing_is_up(monkeypatch):
    # NOTHING up: main stays the daemon fallback (a box coming up must find a live proxy +
    # CA), but NO mirror is written — an idle supervisor used to re-drop `.dev-mode.json`
    # into a clean checkout every tick, forever, even after the machine itself was deleted.
    written: list = []
    spec = {
        "egress-proxy": {
            "cmd": ["proxy-main"],
            "env": {},
            "requires": [],
            "port": None,
            "label": "main",
        }
    }

    class FakeChild:
        def __init__(self, name, spec):
            self.name = name
            self.spec = spec
            self.signature = repr((spec["cmd"], sorted(spec["env"].items())))
            self.started_at = 0.0
            self.proc = types.SimpleNamespace(returncode=None)

        def alive(self):
            return True

        def stop(self):
            pass

    monkeypatch.setattr(supervisor, "_stamp_heartbeat", lambda: None)
    monkeypatch.setattr(supervisor.allowlist, "sweep", lambda: None)
    monkeypatch.setattr(supervisor.githeal, "sweep", lambda log: None)
    monkeypatch.setattr(supervisor.devmode, "up_worktrees", lambda: [])
    monkeypatch.setattr(supervisor.devmode, "worktree_config", _fake_cfg)
    monkeypatch.setattr(supervisor, "expire_user_modes", lambda: {"github": "off"})
    monkeypatch.setattr(supervisor.devmode, "desired_daemons", lambda mode: spec)
    monkeypatch.setattr(supervisor.devmode, "env_defaults", lambda mode: {})
    monkeypatch.setattr(supervisor.devmode, "write_mirror", lambda *a, **k: written.append(a))
    monkeypatch.setattr(supervisor, "Child", FakeChild)

    children: dict = {}
    supervisor.reconcile_once(children, {})

    assert set(children) == {"egress-proxy"}  # the main fallback still serves daemons
    assert written == []  # …but no checkout got a mirror dropped into it


def test_main_clears_ephemeral_grants_on_start_then_sweeps_each_tick(monkeypatch, tmp_path):
    # The supervisor owns the allowlist lifecycle: at startup it drops 'until restart'/'once' grants
    # (clear_ephemeral — a restart must not preserve them), then expires lapsed 'once' grants every
    # tick (sweep). Drive main() with everything stubbed and break out after the first sweep.
    calls: list[str] = []
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(supervisor.config, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(supervisor.devmode, "in_box", lambda: False)
    monkeypatch.setattr(supervisor, "acquire_singleton", lambda: True)
    monkeypatch.setattr(supervisor, "tee_stdio_to_logfile", lambda: None)
    monkeypatch.setattr(supervisor, "load_host_env", lambda: None)
    monkeypatch.setattr(supervisor, "log", lambda _m: None)
    monkeypatch.setattr(supervisor, "expire_user_modes", lambda: {"capture": "off"})
    monkeypatch.setattr(supervisor.devmode, "desired_daemons", lambda _m: {})
    monkeypatch.setattr(supervisor.devmode, "write_mirror", lambda *a, **k: None)
    monkeypatch.setattr(supervisor.devmode, "daemon_status", lambda _m: {})
    monkeypatch.setattr(supervisor.devmode, "read", lambda: {"mode": {}, "expires": {}})
    monkeypatch.setattr(supervisor.signal, "signal", lambda *a, **k: None)  # don't clobber handlers
    monkeypatch.setattr(supervisor.allowlist, "clear_ephemeral", lambda: calls.append("clear"))
    monkeypatch.setattr(supervisor.configpin, "gate", lambda _verb: "clean")  # see _patch

    def _sweep():
        calls.append("sweep")
        raise _StopTick

    monkeypatch.setattr(supervisor.allowlist, "sweep", _sweep)

    with pytest.raises(_StopTick):
        supervisor.main()
    assert calls == ["clear", "sweep"]  # cleared once at startup, then swept on the first tick


# ── staleness: `fy host` / `fy up` must replace a supervisor running old code ────────────────────


def _hold_lock(tmp_path, content: str | None = None) -> int:
    """Simulate another process holding the singleton lock (flock conflicts across fds), with
    optional holder metadata stamped the way acquire_singleton writes it."""
    path = tmp_path / "host-supervisor.lock"
    if content is not None:
        path.write_text(content)
    fd = supervisor.os.open(path, supervisor.os.O_RDWR | supervisor.os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_acquire_singleton_stamps_pid_and_fingerprint(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    assert supervisor.acquire_singleton() is True
    held = supervisor._lock_fd
    assert held is not None
    try:
        pid, fingerprint = supervisor._holder_info()
        assert pid == supervisor.os.getpid()
        assert fingerprint == supervisor._code_fingerprint()
    finally:
        supervisor.os.close(held)
        supervisor._lock_fd = None


def test_holder_stale_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_heartbeat_age", lambda: 2.0)
    fp = supervisor._code_fingerprint()
    # Same fingerprint + fresh heartbeat → current, leave it in charge.
    (tmp_path / "host-supervisor.lock").write_text(f"4242 {fp}\n")
    assert supervisor._holder_stale_reason() is None
    # A different fingerprint → the installed code changed since it started.
    (tmp_path / "host-supervisor.lock").write_text("4242 deadbeefdeadbeef\n")
    reason = supervisor._holder_stale_reason()
    assert reason and "code changed" in reason
    # No metadata at all → a pre-metadata supervisor; bounce once to upgrade it.
    (tmp_path / "host-supervisor.lock").write_text("")
    reason = supervisor._holder_stale_reason()
    assert reason and "predates" in reason
    # Same code but the heartbeat stopped → the reconcile loop is wedged.
    (tmp_path / "host-supervisor.lock").write_text(f"4242 {fp}\n")
    monkeypatch.setattr(supervisor, "_heartbeat_age", lambda: 300.0)
    reason = supervisor._holder_stale_reason()
    assert reason and "wedged" in reason


def test_ensure_background_replaces_a_stale_holder(monkeypatch, tmp_path):
    # The lock is held by a supervisor with a STALE fingerprint → ensure_background bounces it
    # (stubbed: the "holder" releases on SIGTERM) and spawns a fresh one.
    spawns = _patch(monkeypatch, tmp_path, daemon_up=True)  # daemons up must NOT protect stale code
    fd = _hold_lock(tmp_path, "4242 deadbeefdeadbeef\n")
    killed: list[tuple[int, int]] = []

    def _kill(pid, sig):
        killed.append((pid, sig))
        if sig == supervisor.signal.SIGTERM:
            supervisor.os.close(fd)  # the holder shuts down cleanly → flock releases

    monkeypatch.setattr(supervisor.os, "kill", _kill)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)
    assert supervisor.ensure_background() == 4242
    assert spawns == [["foldyard", "host"]]
    assert (4242, supervisor.signal.SIGTERM) in killed


def test_ensure_background_still_noops_on_a_current_holder(monkeypatch, tmp_path):
    # Same code + healthy heartbeat → exactly the old behaviour: never start a second.
    spawns = _patch(monkeypatch, tmp_path)
    monkeypatch.setattr(supervisor, "_heartbeat_age", lambda: 2.0)
    fd = _hold_lock(tmp_path, f"999999 {supervisor._code_fingerprint()}\n")
    try:
        assert supervisor.ensure_background() is None and spawns == []
    finally:
        supervisor.os.close(fd)


def test_code_fingerprint_tracks_source_changes(monkeypatch, tmp_path):
    # The fingerprint must change when any packaged file's CONTENTS change — that's the whole
    # staleness signal — and be stable across calls (module-cached). Point the hashed root
    # (`Path(__file__).parent`) at a throwaway tree so a real change can be exercised.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    mod = pkg / "mod.py"
    mod.write_text("x = 1\n")
    monkeypatch.setattr(supervisor, "__file__", str(pkg / "__init__.py"))
    monkeypatch.setattr(supervisor, "_fingerprint", None)
    first = supervisor._code_fingerprint()
    assert supervisor._code_fingerprint() == first  # module-cached
    monkeypatch.setattr(supervisor, "_fingerprint", None)
    assert supervisor._code_fingerprint() == first  # same tree → same value, cache or not

    (pkg / "mod.py").write_text("x = 22\n")  # size change
    monkeypatch.setattr(supervisor, "_fingerprint", None)
    changed = supervisor._code_fingerprint()
    assert changed != first  # a source edit must flip the staleness signal
    monkeypatch.setattr(supervisor, "_fingerprint", None)
    assert supervisor._code_fingerprint() == changed  # stable again on the modified tree

    # A same-SIZE edit with the mtime restored — the case content-hashing catches but a
    # (size, mtime) metadata fingerprint would miss entirely.
    st = mod.stat()
    mod.write_text("x = 33\n")  # same byte count as "x = 22\n"
    os.utime(mod, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore the old mtime
    monkeypatch.setattr(supervisor, "_fingerprint", None)
    assert supervisor._code_fingerprint() != changed  # content change flips it despite same meta


def test_stamp_log_lines_prefixes_complete_lines_and_holds_a_partial():
    out, rest = supervisor._stamp_log_lines(b"alpha\nbeta\npartial")
    lines = out.decode().splitlines()
    assert len(lines) == 2 and lines[0].endswith(" alpha") and lines[1].endswith(" beta")
    assert all(
        "T" in ln[:30] for ln in lines
    )  # each is prefixed with an ISO-8601 (date'T'time) stamp
    assert rest == b"partial"  # the newline-less tail is held back so a stamp never lands mid-line
    assert supervisor._stamp_log_lines(b"none yet") == (
        b"",
        b"none yet",
    )  # no newline → nothing emitted


def test_load_host_env_refreshes_edits_but_keeps_ambient_overrides(monkeypatch, tmp_path):
    # host.env is re-read every reconcile tick, so a secret written AFTER the supervisor booted goes
    # live without a restart — the fix for "the token's in host.env but the daemon still nags it's
    # missing". But a var the operator exported into the supervisor's OWN env still wins (the
    # original setdefault precedence), and a rotated host.env value updates in place.
    host_env = tmp_path / "host.env"
    monkeypatch.setattr(supervisor.config, "host_env_file", lambda: host_env)
    monkeypatch.setenv("AMBIENT_ONE", "from-shell")  # exported into the supervisor's env at boot
    monkeypatch.delenv("KEYLESS_TOKEN", raising=False)  # not set yet — lives only in host.env
    monkeypatch.setattr(
        supervisor, "_host_env_ambient", None
    )  # fresh ambient snapshot for the test

    host_env.write_text("AMBIENT_ONE=from-file\nKEYLESS_TOKEN=sk-ant-oat-first\n")
    supervisor.load_host_env()  # first load — snapshots {AMBIENT_ONE, …} as the ambient overrides
    assert supervisor.os.environ["AMBIENT_ONE"] == "from-shell"  # ambient export wins over host.env
    assert supervisor.os.environ["KEYLESS_TOKEN"] == "sk-ant-oat-first"  # loaded from host.env

    host_env.write_text("AMBIENT_ONE=from-file\nKEYLESS_TOKEN=sk-ant-oat-rotated\n")
    supervisor.load_host_env()  # a later tick re-reads the file
    assert supervisor.os.environ["KEYLESS_TOKEN"] == "sk-ant-oat-rotated"  # host.env edit is live
    assert supervisor.os.environ["AMBIENT_ONE"] == "from-shell"  # still not clobbered


def test_reconcile_launches_the_proxy_once_host_env_gains_its_required_token(monkeypatch, tmp_path):
    # THE fix, end to end. The box always routes through the always-on egress proxy, whose
    # `requires` gate reads os.environ. A keyless token captured AFTER the supervisor started (the
    # `fy up` → capture-prompt ordering) sits in host.env; a healthy supervisor that `fy up` won't
    # restart must pick it up on a later tick and launch the proxy — NOT stay down (nagging "needs
    # CLAUDE_CODE_OAUTH_TOKEN"), which left the always-routing box with no network.
    host_env = tmp_path / "host.env"
    monkeypatch.setattr(supervisor.config, "host_env_file", lambda: host_env)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)  # only ever lives in host.env
    monkeypatch.setattr(
        supervisor, "_host_env_ambient", None
    )  # fresh ambient snapshot for the test

    spec = {
        "cmd": ["proxy"],
        "env": {},
        "requires": ["CLAUDE_CODE_OAUTH_TOKEN"],
        "port": None,
        "label": "egress proxy (claude keyless)",
    }
    launched: list[str] = []

    class FakeChild:
        def __init__(self, name, spec):
            self.name, self.spec = name, spec
            self.signature = repr((spec["cmd"], sorted(spec["env"].items())))
            self.started_at = 0.0
            self.proc = types.SimpleNamespace(returncode=None)
            launched.append(name)

        def alive(self):
            return True

        def stop(self):
            launched.append(f"stop:{self.name}")

    monkeypatch.setattr(supervisor, "_stamp_heartbeat", lambda: None)
    monkeypatch.setattr(supervisor.allowlist, "sweep", lambda: None)
    monkeypatch.setattr(supervisor.githeal, "sweep", lambda log: None)
    monkeypatch.setattr(supervisor.devmode, "up_worktrees", lambda: [""])
    monkeypatch.setattr(supervisor.devmode, "worktree_config", _fake_cfg)
    monkeypatch.setattr(supervisor, "expire_user_modes", lambda: {"claude": "on"})
    monkeypatch.setattr(supervisor.devmode, "desired_daemons", lambda mode: {"egress-proxy": spec})
    monkeypatch.setattr(supervisor.devmode, "env_defaults", lambda mode: {})
    monkeypatch.setattr(supervisor.devmode, "read", lambda: {"expires": {}})
    monkeypatch.setattr(supervisor.devmode, "daemon_status", lambda mode: {})
    monkeypatch.setattr(supervisor.devmode, "write_mirror", lambda *a, **k: None)
    monkeypatch.setattr(supervisor, "Child", FakeChild)

    children: dict = {}
    nagged: dict = {}

    # Tick 1: the token isn't captured yet → the proxy's `requires` is unmet → it must NOT launch.
    supervisor.reconcile_once(children, nagged)
    assert children == {} and launched == []

    # The keyless capture appends the token to host.env AFTER the supervisor is already running.
    host_env.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-captured\n")

    # Tick 2: the per-tick reload makes it live → the proxy launches, with no `fy host --restart`.
    supervisor.reconcile_once(children, nagged)
    assert set(children) == {"egress-proxy"} and launched == ["egress-proxy"]
    assert supervisor.os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-captured"


# ── TTL expiry settles the posture coherent (consolidation proposal E) ───────────────────


def test_expiry_settles_stranded_dependents(isolated_state):
    # gcp lapsing under llm=record used to strand llm on an error combination (real Vertex
    # calls with no identity — only failure possible) until the next interactive `fy mode`.
    # The expiry write must cascade the dependent down IN THE SAME atomic write.
    import json as _json
    from datetime import timedelta

    from foldyard import devmode

    devmode.set_mode({"gcp": "sa", "llm": "record"})
    raw = _json.loads(isolated_state["auth"].read_text())
    raw["expires"]["gcp"] = devmode._iso(devmode.now() - timedelta(hours=1))
    isolated_state["auth"].write_text(_json.dumps(raw))

    mode = supervisor.expire_user_modes()

    assert mode["gcp"] == "off" and mode["llm"] == "off"
    durable = devmode.read(apply_expiry=False)["mode"]
    assert durable["gcp"] == "off" and durable["llm"] == "off"  # settled durably, not just read
    assert not [
        m for s, m in devmode.registry().mode_issues(durable) if s == "error"
    ]  # landed coherent


def test_expiry_without_dependents_reverts_only_the_lapsed_axis(isolated_state):
    # github=app is genuinely UNRELATED to gcp (no mode_issues link) — it must survive the
    # gcp expiry untouched, proving the settle cascade only reaches actual dependents.
    import json as _json
    from datetime import timedelta

    from foldyard import devmode

    devmode.set_mode({"gcp": "user", "github": "app"}, ttl=60)
    raw = _json.loads(isolated_state["auth"].read_text())
    raw["expires"]["gcp"] = devmode._iso(devmode.now() - timedelta(hours=1))
    isolated_state["auth"].write_text(_json.dumps(raw))
    mode = supervisor.expire_user_modes()
    assert mode["gcp"] == "off" and mode["github"] == "app"  # unrelated raised axis kept


# ── capability probes (consolidation proposal B) ─────────────────────────────────────────


def _probe(check, name="p1", axis="gcp", interval=3600.0):
    from foldyard.plugins import CapabilityProbe

    return CapabilityProbe(axis=axis, name=name, check=check, interval=interval)


def test_probe_results_cache_until_interval(monkeypatch):
    calls: list[int] = []

    def check():
        calls.append(1)
        return True, f"ok {len(calls)}"

    monkeypatch.setattr(supervisor.devmode, "capability_probes", lambda mode: [_probe(check)])
    first = supervisor.run_capability_probes("", {"gcp": "sa"})
    second = supervisor.run_capability_probes("", {"gcp": "sa"})
    assert calls == [1]  # the second tick was served from the cache (interval not due)
    assert first == second
    assert first["gcp"]["ok"] is True and first["gcp"]["detail"] == "ok 1"


def test_probe_failure_wins_the_axis_merge(monkeypatch):
    probes = [
        _probe(lambda: (True, "chain A ok"), name="a"),
        _probe(lambda: (False, "chain B lapsed"), name="b"),
    ]
    monkeypatch.setattr(supervisor.devmode, "capability_probes", lambda mode: probes)
    result = supervisor.run_capability_probes("", {"gcp": "sa"})
    assert result["gcp"]["ok"] is False and result["gcp"]["detail"] == "chain B lapsed"


def test_probe_crash_reads_as_failing(monkeypatch):
    def boom():
        raise RuntimeError("probe bug")

    monkeypatch.setattr(supervisor.devmode, "capability_probes", lambda mode: [_probe(boom)])
    result = supervisor.run_capability_probes("", {"gcp": "sa"})
    assert result["gcp"]["ok"] is False and "probe crashed" in result["gcp"]["detail"]


def test_probe_transitions_are_logged(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(supervisor, "log", lines.append)
    healthy = [True]

    def check():
        return healthy[0], "detail"

    probe = _probe(check, interval=0.0)  # always due — each call re-probes
    monkeypatch.setattr(supervisor.devmode, "capability_probes", lambda mode: [probe])
    supervisor.run_capability_probes("", {"gcp": "sa"})
    healthy[0] = False
    supervisor.run_capability_probes("", {"gcp": "sa"})
    healthy[0] = True
    supervisor.run_capability_probes("", {"gcp": "sa"})
    assert any("DEGRADED" in ln for ln in lines) and any("recovered" in ln for ln in lines)


def test_write_capabilities_skips_creating_an_empty_file(tmp_path, monkeypatch):
    caps_file = tmp_path / "capabilities.json"
    monkeypatch.setenv("FOLDYARD_CAPABILITIES_FILE", str(caps_file))
    supervisor.write_capabilities({"": {}})
    assert not caps_file.exists()  # nothing to say, nothing created
    supervisor.write_capabilities({"": {"gcp": {"ok": True, "detail": "ok", "checked": "t"}}})
    assert caps_file.exists()
    supervisor.write_capabilities({"": {}})  # rung back at default → cleared, not lingering
    import json as _json

    assert _json.loads(caps_file.read_text()) == {"": {}}


# ── the per-daemon lifecycle decision (consolidation proposal D) ─────────────────────────


def _fake_child(alive=True, signature=None, started_at=0.0) -> supervisor.Child:
    # A SimpleNamespace quacking like Child for the pure decision function (typed via cast).
    from typing import cast

    spec = {"cmd": ["proxy"], "env": {}}
    fake = types.SimpleNamespace(
        signature=signature or repr((spec["cmd"], sorted(spec["env"].items()))),
        alive=lambda: alive,
        started_at=started_at,
        proc=types.SimpleNamespace(returncode=1),
    )
    return cast("supervisor.Child", fake)


def test_child_step_decisions():
    import time

    spec = {"cmd": ["proxy"], "env": {}}
    Step = supervisor.ChildStep
    assert supervisor._child_step(None, spec) is Step.SPAWN
    assert supervisor._child_step(_fake_child(alive=True), spec) is Step.KEEP
    assert supervisor._child_step(_fake_child(signature="old"), spec) is Step.RESTART
    assert (
        supervisor._child_step(_fake_child(alive=False, started_at=time.monotonic()), spec)
        is Step.BACKOFF
    )
    assert supervisor._child_step(_fake_child(alive=False, started_at=0.0), spec) is Step.RESPAWN


def test_child_step_signature_change_beats_death():
    # A dead child whose spec ALSO changed must restart with the new spec, not respawn the old
    # one — RESTART ranks above the exited checks.
    spec = {"cmd": ["proxy"], "env": {"NEW": "1"}}
    assert (
        supervisor._child_step(_fake_child(alive=False, signature="old"), spec)
        is supervisor.ChildStep.RESTART
    )


def test_probe_cache_is_keyed_by_rung(monkeypatch):
    # A probe closure bakes in the identity of the rung it was built for — a rung change must
    # re-probe immediately, never serve the previous rung's cached verdict for up to an interval.
    seen_rungs: list[str] = []
    rung = ["sa"]

    def check():
        seen_rungs.append(rung[0])
        return True, f"ok for {rung[0]}"

    monkeypatch.setattr(
        supervisor.devmode, "capability_probes", lambda mode: [_probe(check, interval=3600.0)]
    )
    supervisor.run_capability_probes("", {"gcp": "sa"})
    rung[0] = "user"
    result = supervisor.run_capability_probes("", {"gcp": "user"})
    assert seen_rungs == ["sa", "user"]  # the rung change bypassed the hour-long cache
    assert result["gcp"]["detail"] == "ok for user"


def test_disabled_probe_is_pruned_not_resurrected(monkeypatch):
    # axis on → probed; axis off → result cleared AND cache pruned; axis on again → the check
    # runs fresh instead of resurrecting the old verdict from before the disable.
    calls: list[int] = []

    def check():
        calls.append(1)
        return True, "ok"

    probe = [_probe(check, interval=3600.0)]
    active = {"on": probe, "off": []}
    monkeypatch.setattr(supervisor.devmode, "capability_probes", lambda mode: active[mode["gcp"]])
    assert supervisor.run_capability_probes("", {"gcp": "on"})  # probed once
    assert supervisor.run_capability_probes("", {"gcp": "off"}) == {}  # cleared + pruned
    assert supervisor.run_capability_probes("", {"gcp": "on"})  # re-enabled → fresh run
    assert calls == [1, 1]


def test_each_due_probe_restamps_the_heartbeat(monkeypatch):
    # gcp=user registers TWO 20s-timeout probes that are created together and stay
    # interval-synchronized — run back to back inside one tick they can exceed
    # HEARTBEAT_STALE_SECONDS (30s) and get a healthy supervisor bounced MID-PROBE by a
    # concurrent launcher. The fix: a fresh stamp before each due check, so the budget is per
    # probe, not per tick. Cached (not-due) probes must not stamp — the tick already does.
    stamps: list[int] = []
    monkeypatch.setattr(supervisor, "_stamp_heartbeat", lambda: stamps.append(1))
    probes = [
        _probe(lambda: (True, "a ok"), name="a", interval=3600.0),
        _probe(lambda: (True, "b ok"), name="b", interval=3600.0),
    ]
    monkeypatch.setattr(supervisor.devmode, "capability_probes", lambda mode: probes)
    supervisor.run_capability_probes("", {"gcp": "user"})
    assert len(stamps) == 2  # one fresh stamp per due probe
    supervisor.run_capability_probes("", {"gcp": "user"})
    assert len(stamps) == 2  # served from cache → no extra stamps


# ── capability edges → notification + heal-restart (consolidation proposal C) ────────────


def _ok(detail="chain ok"):
    return {"ok": True, "detail": detail, "checked": "t"}


def _bad(detail="PAM lapsed — just gcp-elevate"):
    return {"ok": False, "detail": detail, "checked": "t"}


def test_capability_edges_fire_on_flips_only():
    prev = {"": {"gcp": _ok()}}
    assert supervisor.capability_edges(prev, {"": {"gcp": _ok()}}) == []  # stable → quiet
    down = supervisor.capability_edges(prev, {"": {"gcp": _bad()}})
    assert down == [("", "gcp", False, "PAM lapsed — just gcp-elevate")]
    up = supervisor.capability_edges({"": {"gcp": _bad()}}, {"": {"gcp": _ok()}})
    assert up == [("", "gcp", True, "chain ok")]


def test_capability_edges_first_observation_only_fires_when_failing():
    # Booting into an active lapse must still notify; booting into health is just normal.
    assert supervisor.capability_edges({}, {"": {"gcp": _ok()}}) == []
    assert supervisor.capability_edges({}, {"": {"gcp": _bad("down")}}) == [
        ("", "gcp", False, "down")
    ]
    # …and a deactivated axis (rung back at default) is not an edge — deactivation isn't a heal.
    assert supervisor.capability_edges({"": {"gcp": _bad()}}, {"": {}}) == []


def test_capability_edges_are_per_worktree():
    prev = {"": {"gcp": _bad()}, "feat": {"gcp": _ok()}}
    cur = {"": {"gcp": _ok("main healed")}, "feat": {"gcp": _ok()}}
    assert supervisor.capability_edges(prev, cur) == [("", "gcp", True, "main healed")]


def test_heal_edge_spanning_a_supervisor_restart_still_fires(monkeypatch, tmp_path):
    # The baseline seeds from capabilities.json, NOT just supervisor memory: axis lapses,
    # supervisor restarts (fresh process → empty _probe_state), THEN the operator re-elevates —
    # the heal edge must still fire, or the resnapshot never happens exactly when it matters.
    caps_file = tmp_path / "capabilities.json"
    monkeypatch.setenv("FOLDYARD_CAPABILITIES_FILE", str(caps_file))
    import json as _json

    caps_file.write_text(_json.dumps({"": {"gcp": _bad()}}))
    edges = supervisor._advance_capability_baseline({"": {"gcp": _ok("healed")}})
    assert edges == [("", "gcp", True, "healed")]
    # The baseline advanced: the same map again is stable → no repeat edge each tick.
    assert supervisor._advance_capability_baseline({"": {"gcp": _ok("healed")}}) == []


class _SyncThread:
    """threading.Thread stand-in that runs the target synchronously on start() — keeps the
    resnapshot tests deterministic (no real worker thread racing the assertions)."""

    def __init__(self, target=None, args=(), **_kw):
        self._target, self._args = target, args

    def start(self):
        if self._target:
            self._target(*self._args)


def _wire_react(monkeypatch, resnapshot: dict):
    """Wire _react_to_capability_edges's world: a real bound-able config carrying ``resnapshot``,
    captured notifications + restarts, and a synchronous Thread. Returns (notified, restarts)."""
    from conftest import FULL_TOML, make_config
    from foldyard import stack

    cfg = make_config({**FULL_TOML, "resnapshot_on_capability": resnapshot})
    monkeypatch.setattr(supervisor.devmode, "worktree_config", lambda wt: cfg)
    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(supervisor, "_notify", lambda t, b: notified.append((t, b)))
    restarts: list[tuple[tuple[str, ...], str]] = []

    def _restart(services, *, worktree="", timeout=180.0):
        restarts.append((tuple(services), worktree))
        return True, ""

    monkeypatch.setattr(stack, "restart_services", _restart)
    monkeypatch.setattr(supervisor.threading, "Thread", _SyncThread)
    monkeypatch.setattr(supervisor, "log", lambda _m: None)
    return notified, restarts


def test_heal_edge_notifies_and_restarts_configured_services(monkeypatch):
    notified, restarts = _wire_react(monkeypatch, {"gcp": ["queue-worker", "graph-api"]})
    supervisor._react_to_capability_edges([("", "gcp", True, "chain ok")])
    assert restarts == [(("queue-worker", "graph-api"), "")]
    assert len(notified) == 1
    title, body = notified[0]
    assert "recovered" in title and "queue-worker, graph-api" in body
    assert not supervisor._resnapshot_inflight  # worker cleared its key (sync thread)


def test_lapse_edge_notifies_with_the_fix_and_restarts_nothing(monkeypatch):
    notified, restarts = _wire_react(monkeypatch, {"gcp": ["queue-worker"]})
    supervisor._react_to_capability_edges([("", "gcp", False, "PAM lapsed — just gcp-elevate")])
    assert restarts == []  # a lapse only notifies; the restart waits for the heal
    assert len(notified) == 1
    title, body = notified[0]
    assert "DEGRADED" in title and "just gcp-elevate" in body


def test_heal_without_resnapshot_config_only_notifies(monkeypatch):
    notified, restarts = _wire_react(monkeypatch, {})
    supervisor._react_to_capability_edges([("", "gcp", True, "chain ok")])
    assert restarts == [] and len(notified) == 1 and "recovered" in notified[0][0]


def test_heal_edge_skips_a_restart_already_in_flight(monkeypatch):
    # A flapping probe (heal, lapse, heal within one compose restart) must not stack a second
    # restart onto the running one — the in-flight key gates it; the notification still goes out.
    notified, restarts = _wire_react(monkeypatch, {"gcp": ["queue-worker"]})
    supervisor._resnapshot_inflight.add(("", "gcp"))
    supervisor._react_to_capability_edges([("", "gcp", True, "chain ok")])
    assert restarts == []
    assert len(notified) == 1 and "restarting" not in notified[0][1]


def test_resnapshot_worker_targets_the_edges_worktree(monkeypatch):
    notified, restarts = _wire_react(monkeypatch, {"gcp": ["queue-worker"]})
    supervisor._react_to_capability_edges([("feat", "gcp", True, "chain ok")])
    assert restarts == [(("queue-worker",), "feat")]
    assert "worktree feat" in notified[0][0]


def test_reconcile_once_notifies_lapse_then_heals_with_a_restart(monkeypatch, tmp_path):
    # The full loop, end to end: a probe failing on one tick posts the DEGRADED notification;
    # flipping healthy on a later tick posts the recovery AND restarts the configured services.
    # This is the running-stack story: `just gcp-elevate` is the only human step.
    from conftest import FULL_TOML, make_config
    from foldyard import config as config_mod
    from foldyard import stack

    cfg = make_config(FULL_TOML)
    monkeypatch.delenv("WORKTREE", raising=False)
    monkeypatch.setattr(supervisor, "_stamp_heartbeat", lambda: None)
    monkeypatch.setattr(supervisor.allowlist, "sweep", lambda: None)
    monkeypatch.setattr(supervisor.githeal, "sweep", lambda log: None)
    monkeypatch.setattr(supervisor.devmode, "up_worktrees", lambda: [""])
    monkeypatch.setattr(supervisor.devmode, "worktree_config", lambda wt: cfg)
    monkeypatch.setattr(supervisor, "expire_user_modes", lambda: {"gcp": "sa"})
    monkeypatch.setattr(supervisor.devmode, "desired_daemons", lambda mode: {})
    monkeypatch.setattr(supervisor.devmode, "env_defaults", lambda mode: {})
    monkeypatch.setattr(supervisor.devmode, "read", lambda: {"expires": {}})
    monkeypatch.setattr(supervisor.devmode, "daemon_status", lambda mode: {})
    monkeypatch.setattr(supervisor.devmode, "write_mirror", lambda *a, **k: None)
    monkeypatch.setattr(supervisor, "log", lambda _m: None)
    monkeypatch.setattr(supervisor.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        config_mod, "resnapshot_on_capability", lambda: {"gcp": ["queue-worker", "graph-api"]}
    )
    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(supervisor, "_notify", lambda t, b: notified.append((t, b)))
    restarts: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        stack,
        "restart_services",
        lambda services, *, worktree="", timeout=180.0: (
            restarts.append(tuple(services)),
            (True, ""),
        )[1],
    )

    healthy = [False]
    probe = _probe(lambda: (healthy[0], "PAM lapsed — just gcp-elevate"), interval=0.0)
    monkeypatch.setattr(supervisor.devmode, "capability_probes", lambda mode: [probe])

    supervisor.reconcile_once({}, {})  # tick 1: booted into the lapse → DEGRADED notification
    assert len(notified) == 1 and "gcp DEGRADED" in notified[0][0]
    assert "just gcp-elevate" in notified[0][1]  # the fix rides in the notification body
    assert restarts == []

    supervisor.reconcile_once({}, {})  # tick 2: still lapsed — a steady state must stay quiet
    assert len(notified) == 1

    healthy[0] = True  # `just gcp-elevate` ran; the chain heals
    supervisor.reconcile_once({}, {})  # tick 3: heal edge → notification + resnapshot
    assert len(notified) == 2 and "gcp recovered" in notified[1][0]
    assert restarts == [("queue-worker", "graph-api")]


def test_notify_prefers_terminal_notifier_when_installed(monkeypatch):
    # macOS silently drops `osascript display notification` (exit 0!) when the calling terminal
    # app lacks notification permission — and most terminals never appear in the settings pane
    # until one delivery succeeds. terminal-notifier registers as its own Notification Center
    # app (permission prompt on first use), so it wins whenever it's on PATH.
    from conftest import FULL_TOML, make_config
    from foldyard import config as config_mod

    runs: list[list[str]] = []
    monkeypatch.setattr(
        supervisor,
        "which",
        lambda x: f"/opt/homebrew/bin/{x}",  # both tools installed
    )
    monkeypatch.setattr(
        supervisor.subprocess, "run", lambda cmd, **kw: runs.append(cmd) or types.SimpleNamespace()
    )
    with config_mod.using(make_config(FULL_TOML)):
        supervisor._notify("fy: gcp DEGRADED", "PAM lapsed — just gcp-elevate")
    assert runs == [
        [
            "terminal-notifier",
            "-title",
            "fy: gcp DEGRADED",
            "-message",
            "PAM lapsed — just gcp-elevate",
        ]
    ]


def test_notify_falls_back_to_osascript_with_escaped_strings(monkeypatch):
    from conftest import FULL_TOML, make_config
    from foldyard import config as config_mod

    runs: list[list[str]] = []
    monkeypatch.setattr(
        supervisor, "which", lambda x: "/usr/bin/osascript" if x == "osascript" else None
    )
    monkeypatch.setattr(
        supervisor.subprocess, "run", lambda cmd, **kw: runs.append(cmd) or types.SimpleNamespace()
    )
    with config_mod.using(make_config(FULL_TOML)):
        supervisor._notify("fy: gcp DEGRADED", 'error "impersonation" failed')
    assert len(runs) == 1 and runs[0][0] == "osascript"
    script = runs[0][2]
    assert 'display notification "error \\"impersonation\\" failed"' in script
    assert 'with title "fy: gcp DEGRADED"' in script


def test_notify_respects_the_config_opt_out_and_missing_osascript(monkeypatch):
    from conftest import FULL_TOML, make_config
    from foldyard import config as config_mod

    runs: list[list[str]] = []
    monkeypatch.setattr(
        supervisor.subprocess, "run", lambda cmd, **kw: runs.append(cmd) or types.SimpleNamespace()
    )
    # [host] notifications = false → no osascript, even when it's installed.
    monkeypatch.setattr(supervisor, "which", lambda _x: "/usr/bin/osascript")
    with config_mod.using(make_config({**FULL_TOML, "host": {"notifications": False}})):
        supervisor._notify("t", "b")
    assert runs == []
    # Neither notifier tool on PATH (Linux CI) → silently a no-op.
    monkeypatch.setattr(supervisor, "which", lambda _x: None)
    with config_mod.using(make_config(FULL_TOML)):
        supervisor._notify("t", "b")
    assert runs == []
