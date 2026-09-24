"""machine.py — backend-agnostic VM-lifecycle orchestration. The backend (podman/Lima) is
stubbed; we assert the cross-cutting policy machine.py owns: the one-VM-at-a-time guard for
NON-concurrent backends (helpful error, no doomed start), that a concurrent backend skips
that guard, and that `ensure` errors when an explicitly-chosen backend's CLI is missing.
Backend internals (podman/Lima command shapes) live in test_machine_backend.py."""

from __future__ import annotations

import re
import shutil
import subprocess
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
        # The boot-provisioning id the backend's stored config carries ("" = none recorded).
        self._provision = ""
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

    def start(self, name, prefix=()):
        self.calls.append(f"start:{name}" + (f" under {' '.join(prefix)}" if prefix else ""))
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

    def ssh_target(self, name):
        return None  # no VM to ssh into: guestlog skips, sandbox is patched where wanted

    def provision_id(self, name):
        return self._provision

    def set_provision(self, name, script):
        # the id rides in the script's marker line, as it does in the real lima.yaml
        self._provision = script.splitlines()[1].split()[-1]
        self.calls.append(f"provision:{self._provision}")
        return True

    # the host-side wall's input: the VM's host processes
    _pids: tuple[int, ...] = ()

    def host_pids(self, name):
        return list(self._pids)


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
    assert 'backend = "podman"' in err  # names the other VM backend, never a VM-less one
    assert "native" not in err
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


def test_ensure_provisions_the_gvisor_posture_after_the_guest_checks(
    half_started, tmp_path, monkeypatch
):
    from foldyard import sandbox

    # The fake MUST be installed: without it `ensure` runs against the real backend — and the
    # developer's live VM (it passed for months only while that VM's recorded boot
    # provisioning happened to match the current asset).
    half_started()
    order = []
    monkeypatch.setattr(machine, "_check_guest_provisioning", lambda: order.append("guest"))
    monkeypatch.setattr(sandbox, "ensure", lambda backend, name: order.append(f"sandbox:{name}"))
    monkeypatch.setattr(machine.config, "machine_runtime", lambda: "gvisor")
    machine.ensure(tmp_path, tmp_path / "wt")
    assert order == ["guest", f"sandbox:{machine.MACHINE}"]


def test_ensure_provisions_the_log_budget_after_the_guest_checks_on_every_backend(
    half_started, tmp_path, monkeypatch
):
    from foldyard import guestlog

    # Not gated on a posture: the journal cap / API log level are housekeeping every VM wants.
    # After the guest checks (a VM that failed its root-side provisioning is refused first) and
    # before the sandbox, whose service restarts should see the drop-in already there.
    half_started()
    order = []
    monkeypatch.setattr(machine, "_check_guest_provisioning", lambda: order.append("guest"))
    monkeypatch.setattr(guestlog, "ensure", lambda backend, name: order.append(f"log:{name}"))
    monkeypatch.setattr(machine.config, "machine_runtime", lambda: "")
    machine.ensure(tmp_path, tmp_path / "wt")
    assert order == ["guest", f"log:{machine.MACHINE}"]


def test_ensure_skips_the_sandbox_without_the_posture(half_started, tmp_path, monkeypatch):
    from foldyard import sandbox

    half_started()  # see above — never the real backend
    monkeypatch.setattr(machine, "_check_guest_provisioning", lambda: None)
    monkeypatch.setattr(sandbox, "ensure", lambda backend, name: pytest.fail("not wanted"))
    monkeypatch.setattr(machine.config, "machine_runtime", lambda: "")
    machine.ensure(tmp_path, tmp_path / "wt")


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


def test_ensure_checks_the_guest_provisioning_after_a_revive(half_started, monkeypatch, tmp_path):
    # A revived VM is a booted-from-cold VM: the boot provisioning (sudo grant dropped, wall)
    # re-ran as root at that boot, and ensure must read the guest's report of it as on any start.
    be = half_started(name="lima", cli="limactl", concurrent=True)
    monkeypatch.setattr(machine.config, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(machine.config, "machine_wall", lambda: True)
    be._provision = machine.provision_id()
    reads: list[int] = []
    monkeypatch.setattr(
        machine,
        "_guest_state",
        lambda: (reads.append(1), (machine._provision_want(), True, True))[1],
    )
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["stop:homelab", "reap:homelab", "start:homelab"]
    assert reads == [1]


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

    assert machine.delete(assume_yes=True) == 0
    assert be.calls == ["remove:homelab"]
    assert stopped == [True]


# ── recreate: refuses where there's no VM to recreate ──────────────────────────────────


# ── guest boot provisioning: the sudo grant + the egress wall (lima only) ──────────────
#
# Root in the guest is BOOT-TIME ONLY: a `provision: mode: system` script recorded in the
# instance's lima.yaml runs as root on every boot (Lima re-creates cloud-init's NOPASSWD:ALL
# grant on every boot too — the instance id changes — so the script narrows it each time). The
# host never runs `sudo` in the guest; it records the script while the VM is stopped and reads
# the guest's own report of what it applied after boot. So a change needs a restart, and a
# running VM with stale provisioning is refused — never run unwalled / with the old grant.


@pytest.fixture
def lima_env(fake, monkeypatch, tmp_path):
    """A running lima machine whose guest-state probe is a seam. Returns ``(backend, guest,
    set_wall, guest_ok)``: ``guest`` is what `_guest_state` answers (state text, wall active,
    rootful socket masked) and counts reads; ``guest_ok()`` makes it report the CURRENT desired
    provisioning as applied."""
    be = fake(concurrent=True, name="lima", cli="limactl")
    be._exists = True
    be._state = "running"
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    monkeypatch.setattr(machine.config, "state_dir", lambda: tmp_path / "state")
    monkeypatch.delenv("FY_PROXY_PORT", raising=False)
    monkeypatch.delenv("GCP_MINTER_PORT", raising=False)

    class Guest:
        state = ""
        active = False
        masked = False
        reads = 0

    guest = Guest()

    def _guest_state():
        guest.reads += 1
        return (guest.state, guest.active, guest.masked)

    monkeypatch.setattr(machine, "_guest_state", _guest_state)

    def set_wall(on: bool) -> None:
        monkeypatch.setattr(machine.config, "machine_wall", lambda: on)

    def guest_ok() -> None:
        guest.state = machine._provision_want()
        guest.active = machine.config.machine_wall()
        guest.masked = True

    return be, guest, set_wall, guest_ok


def test_provision_script_drops_the_sudo_grant_and_installs_the_wall(lima_env):
    _, _, set_wall, _ = lima_env
    set_wall(True)
    script = machine.guest_provision_script()
    assert script.splitlines()[1] == f"# fy-provision {machine.provision_id()}"
    # 1. cloud-init's NOPASSWD:ALL becomes Lima's own non-passwordless form (a graceful
    #    `limactl stop` still runs shutdown) — the VM user, i.e. the box's uid, gets no root.
    assert "/etc/sudoers.d/90-cloud-init-users" in script
    assert "NOPASSWD:/sbin/shutdown -h now" in script
    # 2. the wall asset is embedded, so the guest installs it root-owned — never from the mount
    assert "table inet fy_wall" in script
    # 3. installed with the gateway, THIS project's band, and the main proxy URL; the walled uid
    #    is the guest's own record of the Lima user, not a host guess
    assert "install 192.168.5.2 '41000-41089, 41100-41189' http://192.168.5.2:41000" in script
    assert "uid='{{.UID}}'" in script and 'FY_WALL_UID="$uid"' in script
    # 4. the guest records what it applied where the host can read it WITHOUT root
    assert "/run/fy-wall/state" in script
    assert "wall on 41000-41089, 41100-41189" in script


@pytest.mark.parametrize("wall", [True, False])
def test_provision_script_is_valid_bash(lima_env, wall):
    # `bash -n` — the first live run died on an apostrophe inside a `${x:?message}` (a quoting
    # error bash reports only at the END of the file), so the guest never wrote its report and
    # ensure failed closed. Cheap to catch here, on any host with bash.
    if shutil.which("bash") is None:
        pytest.skip("no bash on this host")
    _, _, set_wall, _ = lima_env
    set_wall(wall)
    script = machine.guest_provision_script()
    res = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr


def test_provision_script_uses_only_limas_template_fields(lima_env):
    # Lima renders the script as a Go template at every start (`{{.User}}`, `{{.UID}}` are how a
    # provision script learns the guest user — its LIMA_CIDATA_* env is internal, and referencing
    # it draws a warning on every limactl call). So `{{` anywhere else — e.g. in the embedded wall
    # asset — would break the render, and the guest would boot without provisioning.
    _, _, set_wall, _ = lima_env
    set_wall(True)
    script = machine.guest_provision_script()
    assert set(re.findall(r"\{\{.*?\}\}", script)) == {"{{.User}}", "{{.UID}}"}
    assert "LIMA_CIDATA" not in script


def test_provision_script_caps_the_journal_as_root_at_boot(lima_env):
    from foldyard import guestlog

    # The Lima user has no sudo, so the journald cap (a root-owned /etc drop-in) can only land
    # from the boot script — and riding the recording means an existing VM re-provisions on the
    # next `fy machine stop && fy up`, the same way a wall change does.
    _, _, set_wall, _ = lima_env
    set_wall(False)
    script = machine.guest_provision_script()
    assert f"SystemMaxUse={guestlog.JOURNAL_MAX_USE}" in script
    assert "/etc/systemd/journald.conf.d/50-foldyard-cap.conf" in script
    assert "sudo" not in script.split("# 2. The wall script")[0].split("# 1b.")[-1]


def test_provision_script_wall_off_still_drops_the_sudo_grant(lima_env):
    _, _, set_wall, _ = lima_env
    set_wall(False)
    script = machine.guest_provision_script()
    assert "NOPASSWD:/sbin/shutdown -h now" in script
    assert "fy-machine-wall uninstall" in script
    assert "wall off" in script


def test_provision_id_changes_with_the_port_band(lima_env, monkeypatch):
    # A moved band (registry edit, env override) is a different script — the VM keeps nft
    # ranges nothing listens on otherwise.
    _, _, set_wall, _ = lima_env
    set_wall(True)
    before = machine.provision_id()
    monkeypatch.setenv("FY_PROXY_PORT", "42000")
    monkeypatch.setenv("GCP_MINTER_PORT", "42100")
    assert machine.provision_id() != before
    assert "42000-42089, 42100-42189" in machine.guest_provision_script()


def test_ensure_records_provisioning_before_the_first_boot(lima_env, tmp_path):
    be, guest, set_wall, guest_ok = lima_env
    be._exists = False
    be._state = "stopped"
    set_wall(True)
    guest_ok()
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["create:homelab", f"provision:{machine.provision_id()}", "start:homelab"]
    assert guest.reads == 1  # …and the guest's report was checked after the boot


def test_ensure_updates_stale_provisioning_on_a_stopped_machine(lima_env, tmp_path):
    be, _guest, set_wall, guest_ok = lima_env
    be._state = "stopped"
    be._provision = "stale"
    set_wall(True)
    guest_ok()
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == [f"provision:{machine.provision_id()}", "start:homelab"]


def test_ensure_refuses_a_running_machine_whose_provisioning_is_stale(lima_env, tmp_path, capsys):
    # Provisioning applies at BOOT (it is what runs as root), so a change — the wall flipped, a
    # moved band, or a VM created before the sudo grant was dropped — needs a restart. Fail
    # closed rather than run unwalled / with the old grant, and say exactly what to do.
    be, guest, set_wall, _guest_ok = lima_env
    be._provision = "stale"
    set_wall(True)
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == []
    assert guest.reads == 0
    assert "fy machine stop" in capsys.readouterr().err


def test_ensure_steady_state_only_reads_the_guest(lima_env, tmp_path):
    be, guest, set_wall, guest_ok = lima_env
    set_wall(True)
    be._provision = machine.provision_id()
    guest_ok()
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == []
    assert guest.reads == 1  # one `limactl shell` per steady-state `fy up`: correctness over speed


@pytest.mark.parametrize(
    "state, active, masked, why",
    [
        ("wall off", True, True, "an older/failed boot script left a different state"),
        ("", False, False, "no report at all — the script never ran"),
        (None, False, True, "the wall unit is not active (tampered / failed)"),
        (None, True, False, "the rootful podman.socket is unmasked (container-root → VM-root)"),
    ],
)
def test_ensure_fails_when_the_guest_did_not_apply_the_provisioning(
    lima_env, tmp_path, capsys, state, active, masked, why
):
    be, guest, set_wall, _guest_ok = lima_env
    set_wall(True)
    be._provision = machine.provision_id()
    guest.state = machine._provision_want() if state is None else state
    guest.active, guest.masked = active, masked
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert "provision" in capsys.readouterr().err, why


def test_ensure_wall_off_does_not_require_the_rootful_socket_masked(lima_env, tmp_path):
    be, guest, set_wall, _guest_ok = lima_env
    set_wall(False)
    be._provision = machine.provision_id()
    guest.state, guest.active, guest.masked = machine._provision_want(), False, False
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == []


def test_non_lima_backends_are_not_provisioned(fake, monkeypatch, tmp_path):
    # podman-machine's appliance can't be provisioned and native has no VM: nothing to record,
    # nothing to read — and never an error here (preflight owns the "wall needs lima" error).
    be = fake(concurrent=False, name="podman", cli="podman")
    be._exists = True
    be._state = "running"
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    monkeypatch.setattr(machine.config, "machine_wall", lambda: True)
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == []


def test_recreate_provisions_the_fresh_machine_before_its_first_boot(lima_env, tmp_path):
    be, _guest, set_wall, guest_ok = lima_env
    set_wall(True)
    guest_ok()
    assert machine.recreate(tmp_path / "repo", tmp_path / "repo-wt", assume_yes=True) == 0
    assert be.calls == [
        "stop:homelab",
        "remove:homelab",
        "create:homelab",
        f"provision:{machine.provision_id()}",
        "start:homelab",
    ]


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


def _dir_creations_outside_system_paths(text: str) -> list[str]:
    """Every `install -d` / `mkdir` DIRECTIVE in the wall script (comments skipped) whose target
    is not a fixed system path — i.e. anything that can land under the VM user's home."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or not re.search(r"\b(install -d|mkdir)\b", line):
            continue
        target = line.split()[-1].strip('"')
        if not target.startswith(("/etc/", "/usr/", "/run/", "/var/", "/tmp/")):
            out.append(line)
    return out


def test_wall_script_creates_user_home_dirs_owned_by_the_user():
    # The wall runs as ROOT at boot and writes environment.d under the VM user's ~/.config. On a
    # fresh image where nothing made ~/.config yet, a bare `install -d` leaves it ROOT-owned —
    # then every later user-level step fails: Lima's `systemctl --user enable podman.socket` (so
    # the API socket never comes up and `limactl start` times out) and the sandbox posture's own
    # drop-ins. Fedora 44 happened to pre-create the dir; the Fedora 45 guest didn't (2026-09-13).
    creations = _dir_creations_outside_system_paths(machine._wall_asset().read_text())
    assert creations, "expected the wall script to create ~/.config/environment.d"
    for line in creations:
        assert '-o "$WALL_UID"' in line, f"creates a dir under the user's home as root: {line}"


# ── stop / rm: the rest of the lifecycle (`fy machine stop|rm`) ─────────────────────────


def test_not_running_reason_over_the_lifecycle(fake, monkeypatch):
    be = fake(concurrent=False, available=True)
    assert "does not exist" in (machine.not_running_reason() or "")
    be._exists = True
    assert "stopped" in (machine.not_running_reason() or "")
    be._state = "running"
    assert machine.not_running_reason() is None


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


def test_delete_stops_then_removes_a_lima_machine(fake, monkeypatch, tmp_path, capsys):
    # The boot provisioning (wall + sudo grant) is recorded IN the instance config, so removing
    # the VM removes it — there is no host-side wall state left to clear.
    be = fake(concurrent=True, available=True, name="lima", cli="limactl")
    be._exists = True
    be._state = "running"
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    assert machine.delete(assume_yes=True) == 0
    assert be.calls == ["stop:homelab", "remove:homelab"]
    assert "deleted" in capsys.readouterr().out


def test_delete_drops_the_vms_podman_desktop_connection(fake, monkeypatch, capsys):
    from foldyard import podman_desktop

    be = fake(concurrent=True, available=True, name="lima", cli="limactl")
    be._exists = True
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    dropped = []
    monkeypatch.setattr(podman_desktop, "unregister", lambda n: dropped.append(n) or "removed")
    assert machine.delete(assume_yes=True) == 0
    assert dropped == ["fy-homelab"]  # a connection to a VM that no longer exists is noise
    assert "Podman Desktop connection 'fy-homelab'" in capsys.readouterr().out


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


# ── the host-side wall (lima, [machine] host_firewall): the VM under its persistent slice, the
# wall PROBED after every start and on every steady-state `fy up`; never loaded by foldyard ──


_OWN_SLICE = "user.slice/user-1000.slice/user@1000.service/fy.slice/fy-machine-homelab.slice"
_OWN_SCOPE = f"{_OWN_SLICE}/fy-machine-homelab.scope"


@pytest.fixture
def host_wall_env(lima_env, monkeypatch):
    """`lima_env` with the host wall wanted and every host input a seam: returns
    ``(backend, guest, probes, set_scope)`` — `probes` records each `hostwall.probe` call
    (answering `probes.result`), `set_scope` is what the VM's pid resolves to in /proc. The
    slice is delivered (`ensure_slice` → its path) unless a test says otherwise."""
    be, guest, set_wall, guest_ok = lima_env
    set_wall(True)
    guest_ok()
    be._provision = machine.provision_id()
    be._pids = (4343, 4242)
    monkeypatch.setattr(machine.config, "machine_host_wall", lambda: True)
    monkeypatch.setattr(machine.hostwall, "available", lambda: True)
    monkeypatch.setattr(machine.hostwall, "slice_path", lambda vm: _OWN_SLICE)  # set up, active
    scope = {"path": ""}
    monkeypatch.setattr(machine.hostwall, "vm_cgroup_scope", lambda pid: scope["path"])

    class Probes(list):
        result = machine.hostwall.Probe(
            True, {"loopback": "refused", "external": "refused", "band": "ok"}
        )

    probes = Probes()

    def probe(vm):
        probes.append(vm)
        return probes.result

    monkeypatch.setattr(machine.hostwall, "probe", probe)

    def set_scope(path):
        scope["path"] = path

    set_scope(_OWN_SCOPE)
    return be, guest, probes, set_scope


def test_host_wall_start_runs_the_vm_in_its_own_scope_under_its_slice(host_wall_env, tmp_path):
    be, _guest, probes, _ = host_wall_env
    be._state = "stopped"
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    prefix = " ".join(machine.hostwall.scoped_argv_prefix("homelab"))
    assert be.calls == [f"start:homelab under {prefix}"]
    # …and the wall is PROBED for the VM once it is up — never loaded
    assert probes == ["homelab"]


def test_host_wall_start_refuses_before_booting_when_not_set_up(
    host_wall_env, monkeypatch, tmp_path, capsys
):
    # No active slice: `systemd-run --slice` would create a transient one — a new cgroup ID the
    # operator's table does not hold. A launch verb never sets the slice up (that is the verb's
    # one user-level change, said out loud there): refuse BEFORE booting a VM it could not wall.
    be, _guest, probes, _ = host_wall_env
    be._state = "stopped"
    monkeypatch.setattr(machine.hostwall, "slice_path", lambda vm: "")
    monkeypatch.setattr(machine.hostwall, "ensure_slice", lambda vm: pytest.fail("fy up set up"))
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == [] and probes == []
    err = capsys.readouterr().err
    assert "isn't set up" in err and "fy machine host-firewall" in err


def test_host_wall_is_probed_on_every_steady_state_up(host_wall_env, tmp_path, capsys):
    # The table can't be read back without root, and one that is there may hold a slice ID
    # that no longer exists (a host reboot) — so every `fy up` asks the wall, not the memory.
    be, guest, probes, _ = host_wall_env
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == []
    assert probes == ["homelab"] and guest.reads == 1
    assert "host firewall enforcing" in capsys.readouterr().err


def test_host_wall_not_enforcing_is_a_hard_stop_that_points_at_the_install(
    host_wall_env, tmp_path, capsys
):
    _be, _guest, probes, _ = host_wall_env
    probes.result = machine.hostwall.Probe(
        False, {"loopback": "ok", "external": "timeout", "band": "ok"}
    )
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    err = capsys.readouterr().err
    assert "NOT enforcing" in err
    assert "loopback ✗ ok" in err and "external ✗ timeout" in err  # which half, in the open
    assert "fy machine host-firewall" in err


def test_host_wall_probe_that_could_not_run_is_a_hard_stop(host_wall_env, tmp_path, capsys):
    _be, _guest, probes, _ = host_wall_env
    probes.result = machine.hostwall.Probe(False, {}, "Failed to connect to bus")
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert "Failed to connect to bus" in capsys.readouterr().err


def test_host_wall_refuses_a_vm_outside_its_own_scope(host_wall_env, tmp_path, capsys):
    # Started by hand, or before host_wall was turned on: QEMU sits in the login session's
    # scope. Walling THAT would wall the operator's whole shell — refuse and say how to fix.
    _be, _guest, probes, set_scope = host_wall_env
    set_scope("user.slice/user-1000.slice/session-2.scope")
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert probes == []
    assert "fy machine stop && fy up" in capsys.readouterr().err


def test_host_wall_refuses_when_the_vm_pid_is_unknown(host_wall_env, tmp_path):
    _be, _guest, probes, set_scope = host_wall_env
    set_scope("")  # no pid file → no cgroup line: nothing to place, so nothing safe to match
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert probes == []


def test_host_wall_refuses_a_vm_scope_outside_its_slice(host_wall_env, tmp_path, capsys):
    # The right scope name, but started before the slice existed (an older foldyard): the
    # table would bind to a slice the VM is not under and match nothing — refuse, same cure.
    _be, _guest, probes, set_scope = host_wall_env
    set_scope("user.slice/user-1000.slice/user@1000.service/app.slice/fy-machine-homelab.scope")
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert probes == []
    assert "fy machine stop && fy up" in capsys.readouterr().err


def test_host_wall_asked_for_on_a_host_that_cannot_enforce_it_is_a_hard_stop(
    host_wall_env, monkeypatch, tmp_path, capsys
):
    _be, _guest, probes, _ = host_wall_env
    monkeypatch.setattr(machine.hostwall, "available", lambda: False)
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert probes == [] and "nft" in capsys.readouterr().err


def test_host_wall_off_starts_unscoped_and_probes_nothing(lima_env, monkeypatch, tmp_path):
    be, _guest, set_wall, guest_ok = lima_env
    set_wall(True)
    guest_ok()
    be._provision = machine.provision_id()
    be._state = "stopped"
    monkeypatch.setattr(machine.config, "machine_host_wall", lambda: False)
    monkeypatch.setattr(machine.hostwall, "ensure_slice", lambda vm: pytest.fail("no slice"))
    monkeypatch.setattr(machine.hostwall, "probe", lambda vm: pytest.fail("must not probe"))
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == ["start:homelab"]


def test_host_wall_is_probed_again_after_a_revive(host_wall_env, tmp_path):
    be, _guest, probes, _ = host_wall_env
    be._responsive = False
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert any(c.startswith("start:homelab under") for c in be.calls)
    assert probes == ["homelab"]


def test_delete_leaves_the_operators_wall_install_and_says_so(host_wall_env, monkeypatch, capsys):
    # The install is the operator's, bound to their user manager, not to the VM: `rm` neither
    # touches it nor prompts for root — it names the verb that prints the removal steps.
    be, _guest, _probes, _ = host_wall_env
    monkeypatch.setattr(machine, "_stop_host_supervisor", lambda: True)
    assert machine.delete(assume_yes=True) == 0
    assert be.calls == ["stop:homelab", "remove:homelab"]
    assert "fy machine host-firewall --uninstall" in capsys.readouterr().err


def test_stop_leaves_the_host_wall_alone(host_wall_env, monkeypatch):
    # A table matching an idle slice is inert and still right for the next start.
    monkeypatch.setattr(machine.hostwall, "probe", lambda vm: pytest.fail("must not probe"))
    monkeypatch.setattr(machine, "_stop_host_supervisor", lambda: True)
    assert machine.stop() == 0


# ── `fy machine host-firewall`: the operator's side — files + commands printed, never run ──────


@pytest.fixture
def host_wall_verb(host_wall_env, monkeypatch, tmp_path):
    monkeypatch.setattr(machine.config, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(machine.hostwall, "user_unit_dir", lambda: tmp_path / "units")
    monkeypatch.setattr(machine.hostwall, "ensure_slice", lambda vm: _OWN_SLICE)
    monkeypatch.setattr(machine.config, "proxy_port_base", lambda: 41000)
    monkeypatch.setattr(machine.config, "gcp_minter_port_base", lambda: 41100)
    monkeypatch.setattr(machine.hostwall.shutil, "which", lambda name: "/usr/sbin/nft")
    monkeypatch.setattr(machine.hostwall.os, "getuid", lambda: 1000)
    return host_wall_env


def test_host_wall_verb_prints_the_files_and_the_install_steps(host_wall_verb, capsys):
    assert machine.host_wall() == 0
    out = capsys.readouterr().out
    assert "✓ enforcing" in out
    # the one user-level change, said when it is made (the unit is not on disk in this test)
    assert "user slice fy-machine-homelab.slice written and enabled (no root)" in out
    # the table and the unit, in full, then the four root commands — and a probe, not a load
    assert (
        'socket cgroupv2 level 5 "user.slice/user-1000.slice/user@1000.service/fy.slice/fy-machine-homelab.slice"'
        in out
    )
    assert "ExecStart=/usr/sbin/nft -f /etc/foldyard/host-wall-homelab.nft" in out
    assert "sudo install -D -m 0644" in out
    assert "sudo systemctl enable --now fy-host-wall-homelab.service" in out
    assert "foldyard runs none of this" in out


def test_host_wall_verb_exits_1_when_not_enforcing(host_wall_verb, capsys):
    _be, _guest, probes, _ = host_wall_verb
    probes.result = machine.hostwall.Probe(
        False, {"loopback": "ok", "external": "ok", "band": "ok"}
    )
    assert machine.host_wall() == 1
    out = capsys.readouterr().out
    assert "✗ NOT enforcing" in out and "sudo install" in out  # the cure is still printed


def test_host_wall_verb_uninstall_prints_the_removal_steps_only(host_wall_verb, capsys):
    assert machine.host_wall(uninstall=True) == 0
    out = capsys.readouterr().out
    assert "sudo systemctl disable --now fy-host-wall-homelab.service" in out
    assert "sudo rm /etc/systemd/system/fy-host-wall-homelab.service" in out
    assert "systemctl --user disable --now fy-machine-homelab.slice" in out  # the user half too
    assert "ExecStart" not in out and "sudo install" not in out


def test_host_wall_verb_is_quiet_about_a_slice_already_set_up(host_wall_verb, monkeypatch, capsys):
    monkeypatch.setattr(machine.hostwall, "slice_installed", lambda vm: True)
    machine.host_wall()
    assert "written and enabled" not in capsys.readouterr().out


def test_host_wall_verb_is_a_no_op_note_when_off(host_wall_verb, monkeypatch, capsys):
    monkeypatch.setattr(machine.config, "machine_host_wall", lambda: False)
    assert machine.host_wall() == 0
    assert "host firewall is off" in capsys.readouterr().out


def test_host_wall_verb_uninstall_never_sets_the_slice_up(host_wall_verb, monkeypatch, capsys):
    # Removal with nothing set up: no ensure (that would write + enable the unit), no probe
    # (`systemd-run --slice` would create a transient slice) — just the lines.
    monkeypatch.setattr(machine.hostwall, "slice_path", lambda vm: "")
    monkeypatch.setattr(machine.hostwall, "ensure_slice", lambda vm: pytest.fail("set up"))
    monkeypatch.setattr(machine.hostwall, "probe", lambda vm: pytest.fail("probed"))
    machine.host_wall(uninstall=True)
    out = capsys.readouterr().out
    assert "not set up (no slice)" in out and "systemctl --user disable --now" in out
