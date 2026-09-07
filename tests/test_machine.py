"""machine.py — backend-agnostic VM-lifecycle orchestration. The backend (podman/Lima) is
stubbed; we assert the cross-cutting policy machine.py owns: the one-VM-at-a-time guard for
NON-concurrent backends (helpful error, no doomed start), that a concurrent backend skips
that guard, and that `ensure` errors when an explicitly-chosen backend's CLI is missing.
Backend internals (podman/Lima command shapes) live in test_machine_backend.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from foldyard import machine, supervisor


class FakeBackend:
    """A scriptable stand-in for a real Backend. Records start/stop/create/remove calls."""

    def __init__(self, *, concurrent, running=(), available=True, name="podman", cli="podman"):
        self.name = name
        self.cli = cli
        self.install_hint = f"brew install {cli}"
        self._concurrent = concurrent
        self._running = list(running)
        self._available = available
        self._exists = False
        self._state = "stopped"
        self._mounts: list[str] = []
        # Liveness is INDEPENDENT of `_state`: the pair (running, not responsive) is the
        # half-started VM this fake exists to reproduce. `_revives` is whether a restart cures it.
        self._responsive = True
        self._revives = True
        # Probes to answer False before `_responsive` is honoured — the socket that starts
        # accepting a beat after `machine start` returns.
        self._responsive_delay = 0
        self._orphans: list[int] = []
        self._stops = True
        self.calls: list[str] = []

    def supports_concurrent(self):
        return self._concurrent

    def available(self):
        return self._available

    def list_running(self, name=""):
        return list(self._running)

    def exists(self):  # machine.exists() takes no args; the module passes MACHINE through
        return self._exists

    def state(self):
        return self._state

    def mounts(self):
        return list(self._mounts)

    def responsive(self, name=""):
        if self._responsive_delay > 0:
            self._responsive_delay -= 1
            return False
        return self._responsive

    def reap_orphans(self, name):
        self.calls.append(f"reap:{name}")
        return list(self._orphans)

    def start(self, name):
        self.calls.append(f"start:{name}")
        self._running.append(name)
        self._state = "running"
        self._responsive = self._revives
        return True

    def create(self, name, resources, volumes):
        self.calls.append(f"create:{name}")
        self._exists = True
        return True

    def stop(self, name):
        self.calls.append(f"stop:{name}")
        self._state = "stopped"
        return self._stops

    def remove(self, name):
        self.calls.append(f"remove:{name}")
        return True


@pytest.fixture
def fake(monkeypatch):
    """Install a FakeBackend as machine.BACKEND and pin MACHINE; return a factory so each
    test shapes its own backend. machine.exists()/state()/mounts() delegate to BACKEND with
    MACHINE bound, so the fake's no-arg signatures match."""
    monkeypatch.setattr(machine, "MACHINE", "homelab")

    def install(**kw):
        be = FakeBackend(**kw)
        monkeypatch.setattr(machine, "BACKEND", be)
        # machine.exists/state/mounts call BACKEND.<f>(MACHINE); the fake ignores the arg.
        monkeypatch.setattr(machine, "exists", be.exists)
        monkeypatch.setattr(machine, "state", be.state)
        monkeypatch.setattr(machine, "mounts", be.mounts)
        return be

    return install


# ── one-VM-at-a-time guard (non-concurrent backend, e.g. podman on macOS) ──────────────


def test_start_blocks_when_another_machine_runs_on_nonconcurrent_backend(fake, capsys):
    be = fake(concurrent=False, running=["tangible"])
    assert machine._start() is False
    assert be.calls == []  # never attempted the doomed start
    err = capsys.readouterr().err
    assert "one" in err.lower()
    assert "podman machine stop tangible" in err  # tells you to stop the other yourself
    assert 'backend = "lima"' in err  # …or run concurrently via Lima


def test_start_clean_when_nothing_else_running(fake):
    be = fake(concurrent=False, running=[])
    assert machine._start() is True
    assert be.calls == ["start:homelab"]


def test_start_skips_guard_on_concurrent_backend(fake):
    # Lima: another VM running is fine — start ours beside it, no guard, no error.
    be = fake(concurrent=True, running=["tangible"], name="lima", cli="limactl")
    assert machine._start() is True
    assert be.calls == ["start:homelab"]


# ── ensure: availability + create→start flow ───────────────────────────────────────────


def test_ensure_noop_in_box(fake, monkeypatch):
    be = fake(concurrent=False)
    monkeypatch.setattr(machine.config, "in_box", lambda: True)
    machine.ensure(Path("/repo"), Path("/repo-wt"))
    assert be.calls == []  # the box manages its host VM from outside — nothing to do


def test_ensure_noop_for_native_backend(fake, monkeypatch, tmp_path):
    be = fake(concurrent=True, available=True, name="native", cli="podman")
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == []


def test_ensure_noop_under_the_explicit_native_backend(fake, monkeypatch, tmp_path):
    # `backend = "native"` is the KNOWING opt-out of the VM: there is no machine to manage, and
    # the host socket is what the consumer asked for. The only remaining silent no-op.
    be = fake(concurrent=False, available=False, name="native", cli="podman")
    monkeypatch.setattr(machine.config, "machine_backend_explicit", lambda: "native")
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == []


def test_ensure_errors_when_chosen_backend_cli_missing(fake, monkeypatch, tmp_path, capsys):
    # A backend the consumer NAMED whose CLI is absent → loud error, never a silent fallback to
    # another backend (which would swap the isolation profile without saying so).
    fake(concurrent=True, available=False, name="lima", cli="limactl")
    monkeypatch.setattr(machine.config, "machine_backend_explicit", lambda: "lima")
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    err = capsys.readouterr().err
    assert "limactl" in err and "lima" in err


def test_ensure_fails_closed_when_the_INHERITED_default_backend_has_no_cli(
    fake, monkeypatch, tmp_path, capsys
):
    # Nobody named a backend, so this is the lima DEFAULT with no limactl. It must NOT skip
    # quietly: machine.socket() would then be "", stack would export neither DOCKER_HOST nor
    # CONTAINER_HOST, and every engine verb would land on the host's own podman socket — the
    # native profile, with the VM boundary gone and nobody having chosen it (ADR-0011).
    be = fake(concurrent=True, available=False, name="lima", cli="limactl")
    monkeypatch.setattr(machine.config, "machine_backend_explicit", lambda: "")
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    err = capsys.readouterr().err
    assert "limactl" in err and "DEFAULT" in err
    assert 'backend = "native"' in err  # names the explicit opt-out rather than taking it
    assert be.calls == []  # and nothing was provisioned


def test_ensure_creates_then_starts_when_absent(fake, monkeypatch, tmp_path):
    be = fake(concurrent=False, available=True)
    be._exists = False
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["create:homelab", "start:homelab"]


# ── ensure: the half-started VM (running per the backend, socket dead) ─────────────────


@pytest.fixture
def half_started(fake, monkeypatch):
    """A machine the backend calls `running` while nothing serves its socket — what a host
    crash (or a guest that never signals ready) leaves behind. Trusting the flag made `ensure`
    no-op and every later engine call die on a raw `dial unix …: no such file` instead."""

    def install(**kw):
        kw.setdefault("concurrent", False)
        kw.setdefault("available", True)
        be = fake(**kw)
        be._exists = True
        be._state = "running"
        be._responsive = False
        monkeypatch.setattr(machine.config, "in_box", lambda: False)
        # `_revive` polls for the socket after starting; don't spend the real wall-clock on it
        # (the give-up path would otherwise sleep out the whole budget on every run).
        monkeypatch.setattr(machine, "_REVIVE_PROBE_DELAY", 0)
        return be

    return install


def test_ensure_restarts_a_running_but_unresponsive_machine(half_started, tmp_path, capsys):
    be = half_started()
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["stop:homelab", "reap:homelab", "start:homelab"]
    assert be.responsive() is True
    err = capsys.readouterr().err
    assert "reports running" in err and "Restarting" in err


def test_ensure_leaves_a_healthy_machine_alone(half_started, tmp_path):
    # The guard must cost a probe and nothing else on the steady-state path — no stop/start
    # churn on every `fy up` just because we now look closer than the flag.
    be = half_started()
    be._responsive = True
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == []


def test_ensure_reaps_the_orphan_the_stop_left_behind(half_started, tmp_path, capsys):
    # `podman machine stop` reports success without reaping the hypervisor once it has lost
    # track of it; the survivor then races the next start. Reaping is why the restart works.
    be = half_started()
    be._orphans = [1405]
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["stop:homelab", "reap:homelab", "start:homelab"]
    assert "1405" in capsys.readouterr().err


def test_ensure_reaps_and_starts_even_when_the_stop_fails(half_started, tmp_path):
    # A stop that errors is NOT a reason to give up — the machine is already unusable, and the
    # reap + start is the recovery. Bailing here would strand exactly the case we're fixing.
    be = half_started()
    be._stops = False
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["stop:homelab", "reap:homelab", "start:homelab"]


def test_ensure_waits_for_a_socket_that_comes_up_a_beat_after_the_start(half_started, tmp_path):
    # `machine start` returns when the BACKEND is satisfied; the api socket can take a moment
    # longer to accept. A single probe there would call a merely-slow machine dead and send the
    # user off to rebuild a VM that was about to come up — so poll, and take the first yes.
    be = half_started()
    be._responsive_delay = 3  # one probe from `ensure`, then two from `_revive`'s poll
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["stop:homelab", "reap:homelab", "start:homelab"]  # started ONCE
    assert be.responsive() is True


def test_ensure_fails_loudly_when_the_restart_does_not_revive_the_socket(
    half_started, tmp_path, capsys
):
    # A VM that boots but never serves (e.g. the guest dropping to emergency mode) can't be
    # fixed by restarting. Say so, and point at the rebuild — don't hand back a dead socket.
    be = half_started()
    be._revives = False
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["stop:homelab", "reap:homelab", "start:homelab"]
    err = capsys.readouterr().err
    assert "still dead" in err and "machine rm homelab" in err


def test_ensure_reasserts_the_wall_after_a_revive(half_started, monkeypatch, tmp_path):
    # A revived VM is a booted-from-cold VM: it lost any in-VM provisioning, so the wall must be
    # re-installed exactly as on a normal start (`force`), not skipped because the marker matches.
    be = half_started(name="lima", cli="limactl", concurrent=True)
    forced: list[bool] = []
    monkeypatch.setattr(machine, "wall_sync", lambda force=False: forced.append(force) or True)
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["stop:homelab", "reap:homelab", "start:homelab"]
    assert forced == [True]


def test_not_running_reason_reports_a_dead_socket_on_a_running_machine(fake):
    # `down`/`ps`/`logs` must name the condition rather than reach a socket nothing serves and
    # fail on the engine's dial error. Only `up`/`ensure` may revive it.
    be = fake(concurrent=False, available=True)
    be._exists = True
    be._state = "running"
    be._responsive = False
    assert "socket is dead" in (machine.not_running_reason() or "")


def test_ensure_warns_on_missing_worktrees_mount(fake, monkeypatch, tmp_path, capsys):
    be = fake(concurrent=False, available=True)
    be._exists = True
    be._state = "running"
    be._mounts = ["/some/repo"]  # no worktrees-root mount
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert "no" in capsys.readouterr().err.lower()
    assert be.calls == []  # already running → no create/start


# ── stop/rm: VM teardown also ends the project-scoped host supervisor ───────────────────


def test_stop_stops_the_supervisor_after_stopping_the_vm(fake, monkeypatch):
    be = fake(concurrent=True, name="lima", cli="limactl")
    be._exists = True
    be._state = "running"
    stopped: list[bool] = []
    monkeypatch.setattr(machine, "_stop_host_supervisor", lambda: stopped.append(True) or True)

    assert machine.stop() == 0
    assert be.calls == ["stop:homelab"]
    assert stopped == [True]


def test_stop_without_a_vm_still_stops_the_supervisor(fake, monkeypatch):
    # This is the release-hygiene case: an out-of-band `limactl delete` left the host process
    # alive, so `fy machine stop` must tear it down even though there is no VM command to run.
    be = fake(concurrent=True, name="lima", cli="limactl")
    stopped: list[bool] = []
    monkeypatch.setattr(machine, "_stop_host_supervisor", lambda: stopped.append(True) or True)

    assert machine.stop() == 0
    assert be.calls == []
    assert stopped == [True]


def test_stop_host_supervisor_waits_then_clears_the_mirror(monkeypatch, tmp_path):
    mirror = tmp_path / ".dev-mode.json"
    mirror.write_text("{}\n")
    monkeypatch.setattr(machine.config, "mirror_file", lambda: mirror)

    monkeypatch.setattr(supervisor, "stop", lambda: 0)
    assert machine._stop_host_supervisor() is True
    assert not mirror.exists()


def test_delete_stops_the_supervisor_after_removing_the_vm(fake, monkeypatch, tmp_path):
    be = fake(concurrent=True, name="lima", cli="limactl")
    be._exists = True
    be._state = "stopped"
    stopped: list[bool] = []
    monkeypatch.setattr(machine, "_stop_host_supervisor", lambda: stopped.append(True) or True)
    monkeypatch.setattr(machine, "_wall_marker", lambda: tmp_path / "machine-wall-homelab")

    assert machine.delete(assume_yes=True) == 0
    assert be.calls == ["remove:homelab"]
    assert stopped == [True]


# ── recreate: refuses where there's no VM to recreate ──────────────────────────────────


def test_recreate_native_backend_refuses_with_native_message(fake, monkeypatch, capsys, tmp_path):
    # The native backend has no VM — recreate must refuse, but with a native-appropriate reason,
    # not the misleading "run on the host (Mac) — the box can't recreate its own machine".
    be = fake(concurrent=True, available=True, name="native", cli="podman")
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    rc = machine.recreate(tmp_path / "repo", tmp_path / "repo-wt", assume_yes=True)
    assert rc == 1
    out = capsys.readouterr().out
    assert "native backend" in out and "no VM" in out
    assert "Mac" not in out  # the box/Mac message must NOT be used for native
    assert be.calls == []  # refused before any stop/remove/create


# ── the in-VM egress wall ([machine].wall — lima only; golden limactl sequences) ───────


@pytest.fixture
def wall_env(fake, monkeypatch, tmp_path):
    """A running lima machine + tmp state dir + the `_sh` seam captured (rc 0). Returns
    ``(backend, calls, set_wall, set_vm)`` — ``set_wall(True/False)`` flips the DESIRED wall
    state; ``set_vm(present, masked)`` sets what the in-VM probe (`_wall_vm_state`) reports.
    The VM defaults to NOT enforcing (False, False); a test that wants the steady-state fast
    path to no-op must declare the VM enforcing via ``set_vm(True, True)``."""
    be = fake(concurrent=True, name="lima", cli="limactl")
    be._exists = True
    be._state = "running"
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    monkeypatch.setattr(machine.config, "state_dir", lambda: tmp_path / "state")
    monkeypatch.delenv("FY_PROXY_PORT", raising=False)
    monkeypatch.delenv("GCP_MINTER_PORT", raising=False)
    calls: list[list[str]] = []
    monkeypatch.setattr(machine, "_sh", lambda cmd, stdin=None: (calls.append(cmd), 0)[1])
    vm = {"present": False, "masked": False}
    monkeypatch.setattr(machine, "_wall_vm_state", lambda: (vm["present"], vm["masked"]))

    def set_wall(on: bool) -> None:
        monkeypatch.setattr(machine.config, "machine_wall", lambda: on)

    def set_vm(present: bool, masked: bool) -> None:
        vm["present"], vm["masked"] = present, masked

    return be, calls, set_wall, set_vm


def test_wall_sync_installs_with_gateway_ports_and_proxy_url(wall_env, tmp_path):
    _, calls, set_wall, _set_vm = wall_env
    set_wall(True)
    assert machine.wall_sync() is True
    (install,) = calls
    # The script is STREAMED over stdin (`sudo bash -s --`), never staged in the guest's /tmp
    # (a swappable staging path would be a root-execution TOCTOU).
    assert install[:6] == ["limactl", "shell", "homelab", "sudo", "bash", "-s"]
    assert "/tmp/fy-machine-wall.sh" not in install
    assert "install" in install
    assert "192.168.5.2" in install  # the Lima host gateway the wall opens toward
    # the project's allocated band bases + the full worktree-offset span (disjoint 90-spans)
    assert "41000-41089, 41100-41189" in install
    assert "http://192.168.5.2:41000" in install  # VM-level proxy env (pulls/builds) — main port
    # the marker records the port set too, so a band change re-provisions (not just on/off)
    assert (
        tmp_path / "state" / "machine-wall-homelab"
    ).read_text() == "on 41000-41089, 41100-41189"


def test_wall_sync_skips_when_marker_matches_and_vm_enforces_then_force_reruns(wall_env):
    _, calls, set_wall, set_vm = wall_env
    set_wall(True)
    assert machine.wall_sync() is True
    set_vm(True, True)  # VM now actually enforcing with the rootful socket masked
    calls.clear()
    assert machine.wall_sync() is True
    assert calls == []  # marker matches AND the VM confirms — no limactl on the fast path
    assert machine.wall_sync(force=True) is True
    assert [c for c in calls if "install" in c]  # force re-provisions (self-heal)


def test_wall_sync_reprovisions_when_marker_says_on_but_vm_is_not_enforcing(wall_env):
    # The #3 fix: an out-of-band `limactl delete`/factory-reset drops the nft rules while the
    # host marker still reads "on <ports>". Trusting the marker would leave the VM UNWALLED;
    # the in-VM probe catches it and re-provisions.
    _, calls, set_wall, set_vm = wall_env
    set_wall(True)
    machine.wall_sync()
    set_vm(False, False)  # VM lost the wall behind foldyard's back; marker still "on"
    calls.clear()
    assert machine.wall_sync() is True
    assert [c for c in calls if "install" in c]  # re-provisioned despite the matching marker


def test_wall_sync_reprovisions_and_warns_when_rootful_socket_unmasked(wall_env, capsys):
    # The #4 regression: the wall is loaded but the rootful podman.socket got re-enabled — the
    # container-root→VM-root bypass. The probe flags it and re-provisions (which re-masks).
    _, calls, set_wall, set_vm = wall_env
    set_wall(True)
    machine.wall_sync()
    set_vm(True, False)  # table present, but the rootful socket is NOT masked
    calls.clear()
    assert machine.wall_sync() is True
    assert [c for c in calls if "install" in c]
    assert "rootful podman.socket is NOT masked" in capsys.readouterr().err


def test_wall_sync_off_uninstalls_when_vm_still_enforces_despite_absent_marker(wall_env):
    # The #8 fix: a wiped state_dir loses the marker while the VM's persistent fy-wall.service
    # keeps enforcing. Desired=off + absent marker must PROBE and uninstall, not assume clean.
    _, calls, set_wall, set_vm = wall_env
    set_wall(False)
    set_vm(True, True)  # marker absent (fresh tmp state), but the VM still has the wall
    assert machine.wall_sync() is True
    assert any("uninstall" in c for c in calls)


def test_wall_sync_reprovisions_when_the_port_band_changes(wall_env, monkeypatch):
    # The marker records the PORT SET, not just on/off — a moved band (registry edit, env
    # override) must re-provision, or the VM keeps nft ranges nothing listens on.
    _, calls, set_wall, _set_vm = wall_env
    set_wall(True)
    assert machine.wall_sync() is True
    calls.clear()
    monkeypatch.setenv("FY_PROXY_PORT", "42000")
    monkeypatch.setenv("GCP_MINTER_PORT", "42100")
    assert machine.wall_sync() is True
    install = [c for c in calls if "install" in c]
    assert install and "42000-42089, 42100-42189" in install[0]
    assert "http://192.168.5.2:42000" in install[0]


def test_wall_sync_uninstalls_when_flipped_off(wall_env, tmp_path):
    _, calls, set_wall, _set_vm = wall_env
    set_wall(True)
    machine.wall_sync()
    calls.clear()
    set_wall(False)
    assert machine.wall_sync() is True
    assert any("uninstall" in c for c in calls)
    assert (tmp_path / "state" / "machine-wall-homelab").read_text() == "off"


def test_wall_sync_off_never_installed_touches_nothing_in_vm(wall_env, tmp_path):
    _, calls, set_wall, _set_vm = wall_env
    set_wall(False)
    assert machine.wall_sync() is True
    assert calls == []  # nothing was ever installed — no limactl at all
    assert (tmp_path / "state" / "machine-wall-homelab").read_text() == "off"


def test_wall_sync_warns_and_noops_on_non_lima(fake, monkeypatch, capsys):
    fake(concurrent=False, name="podman", cli="podman")
    monkeypatch.setattr(machine.config, "machine_wall", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(machine, "_sh", lambda cmd, stdin=None: (calls.append(cmd), 0)[1])
    assert machine.wall_sync() is True  # never blocks `up` here — preflight owns that error
    assert calls == []
    assert "lima-only" in capsys.readouterr().err


def test_ensure_on_fresh_lima_machine_provisions_the_wall(wall_env, tmp_path):
    # The wiring: a just-created machine gets the wall before anything else touches it.
    be, calls, set_wall, _set_vm = wall_env
    be._exists = False
    be._state = "stopped"
    set_wall(True)
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["create:homelab", "start:homelab"]
    assert any("install" in c for c in calls)


def test_ensure_reasserts_the_wall_on_every_machine_start(wall_env, tmp_path):
    # Self-healing: a start transition re-runs the idempotent install even when the marker
    # already says "on" (a VM tampered with / drifted while stopped gets re-walled on boot).
    be, calls, set_wall, _set_vm = wall_env
    set_wall(True)
    machine.wall_sync()  # marker now "on"
    calls.clear()
    be._state = "stopped"
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["start:homelab"]
    assert any("install" in c for c in calls)


def test_wall_asset_script_shape():
    # The provisioning script the sync copies in: correct verbs, own nft tables (NEVER a global
    # `flush ruleset` — netavark/pasta state must survive), fail-fast REJECT, no in-VM proxy.
    text = machine._wall_asset().read_text()
    # No global `flush ruleset` DIRECTIVE (a comment may mention it) — the proof rig owns its
    # whole VM; the real machine's netavark/pasta nft state must survive a wall (re)install.
    assert not any(line.strip().startswith("flush ruleset") for line in text.splitlines())
    for needle in (
        "install)",
        "uninstall)",
        "status)",
        "table inet fy_wall",
        "reject with tcp reset",
    ):
        assert needle in text, f"machine-wall.sh missing {needle!r}"
    assert "transproxy" not in text  # no in-VM proxy — the Mac proxy stays the only chokepoint


# ── stop / rm: the rest of the lifecycle (`fy machine stop|rm`) ─────────────────────────


def test_not_running_reason_over_the_lifecycle(fake, monkeypatch):
    be = fake(concurrent=False, available=True)
    assert "does not exist" in (machine.not_running_reason() or "")
    be._exists = True
    assert "stopped" in (machine.not_running_reason() or "")
    be._state = "running"
    assert machine.not_running_reason() is None


def test_not_running_reason_native_backend_is_always_reachable(fake):
    fake(concurrent=True, available=True, name="native", cli="podman")
    assert machine.not_running_reason() is None  # no VM lifecycle to be down


def test_not_running_reason_without_backend_cli(fake):
    fake(concurrent=False, available=False)
    assert "CLI" in (machine.not_running_reason() or "")


def test_stop_noop_when_machine_absent(fake, monkeypatch, capsys):
    be = fake(concurrent=False, available=True)
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    assert machine.stop() == 0
    assert be.calls == []
    assert "does not exist" in capsys.readouterr().out


def test_stop_noop_when_already_stopped(fake, monkeypatch, capsys):
    be = fake(concurrent=False, available=True)
    be._exists = True
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    assert machine.stop() == 0
    assert be.calls == []
    assert "already stopped" in capsys.readouterr().out


def test_stop_stops_a_running_machine(fake, monkeypatch, capsys):
    be = fake(concurrent=False, available=True)
    be._exists = True
    be._state = "running"
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    assert machine.stop() == 0
    assert be.calls == ["stop:homelab"]
    assert "stopped" in capsys.readouterr().out


def test_stop_refused_in_box(fake, monkeypatch):
    be = fake(concurrent=False, available=True)
    monkeypatch.setattr(machine.config, "in_box", lambda: True)
    assert machine.stop() == 1
    assert be.calls == []


def test_stop_refused_on_native_backend(fake, monkeypatch, capsys):
    fake(concurrent=True, available=True, name="native", cli="podman")
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    assert machine.stop() == 1
    assert "native backend" in capsys.readouterr().out


def test_stop_refused_on_host_when_backend_cli_missing(fake, monkeypatch, capsys):
    # Missing-backend on the host must read as "no CLI", NOT the "inside the box" message.
    fake(concurrent=True, available=False, name="lima", cli="limactl")
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    assert machine.stop() == 1
    out = capsys.readouterr().out
    assert "limactl" in out and "CLI" in out
    assert "box can't" not in out  # not misattributed to the in-box guard


def test_delete_refused_on_host_when_backend_cli_missing(fake, monkeypatch, capsys):
    be = fake(concurrent=True, available=False, name="lima", cli="limactl")
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    assert machine.delete(assume_yes=True) == 1
    assert be.calls == []
    out = capsys.readouterr().out
    assert "limactl" in out and "CLI" in out
    assert "box can't" not in out


def test_delete_noop_when_machine_absent(fake, monkeypatch, capsys):
    be = fake(concurrent=False, available=True)
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    assert machine.delete(assume_yes=True) == 0
    assert be.calls == []
    assert "does not exist" in capsys.readouterr().out


def test_delete_stops_removes_and_clears_the_wall_marker(fake, monkeypatch, tmp_path, capsys):
    be = fake(concurrent=True, available=True, name="lima", cli="limactl")
    be._exists = True
    be._state = "running"
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    monkeypatch.setattr(machine.config, "state_dir", lambda: tmp_path)
    marker = tmp_path / "machine-wall-homelab"
    marker.write_text("on 41000-41089")  # host-side wall state must not outlive the VM
    assert machine.delete(assume_yes=True) == 0
    assert be.calls == ["stop:homelab", "remove:homelab"]
    assert not marker.exists()
    assert "deleted" in capsys.readouterr().out


def test_delete_skips_stop_when_machine_not_running(fake, monkeypatch, tmp_path):
    be = fake(concurrent=False, available=True)
    be._exists = True  # state stays "stopped"
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    monkeypatch.setattr(machine.config, "state_dir", lambda: tmp_path)
    assert machine.delete(assume_yes=True) == 0
    assert be.calls == ["remove:homelab"]


def test_delete_prompt_defaults_to_abort(fake, monkeypatch, capsys):
    be = fake(concurrent=False, available=True)
    be._exists = True
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert machine.delete() == 0
    assert be.calls == []
    assert "Aborted" in capsys.readouterr().out


def test_delete_refused_in_box(fake, monkeypatch):
    be = fake(concurrent=False, available=True)
    monkeypatch.setattr(machine.config, "in_box", lambda: True)
    assert machine.delete(assume_yes=True) == 1
    assert be.calls == []
