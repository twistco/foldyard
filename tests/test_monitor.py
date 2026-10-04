"""monitor.py — the guest monitor (ADR-0031): its boot script, the release delivery, the report,
and how machine.ensure records and checks it. The guest is never reached: ssh is stubbed at
``monitor._ssh``, the download at ``monitor._fetch``. What only a real guest can show — Tetragon
loading the packaged policy, the .path unit firing, the box unable to stop it — is the host-tier
e2e's (tests/test_monitor_e2e.py)."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import test_machine
from foldyard import devmode, machine, monitor
from foldyard.machine_backend import PROVISION_MARKER, SshTarget

# The machine fixtures, reused: a scripted backend (`fake`) and a running lima VM (`lima_env`).
fake = test_machine.fake
lima_env = test_machine.lima_env

KEY = "ab" * 32


@pytest.fixture(autouse=True)
def fixed_relay_key(monkeypatch):
    """render(True) reads (and on first use creates) the host's relay key; pin it here."""
    monkeypatch.setattr(monitor, "relay_key", lambda: KEY)


# ── the boot script ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("on", [True, False])
def test_boot_script_is_valid_bash_with_its_own_marker(on):
    if shutil.which("bash") is None:
        pytest.skip("no bash on this host")
    script, ident = monitor.render(on)
    assert script.splitlines()[1] == f"{monitor.MARKER}{ident}"
    res = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr


def test_the_helper_is_valid_bash():
    if shutil.which("bash") is None:
        pytest.skip("no bash on this host")
    helper = (monitor._asset("fy-monitor.sh")).read_text()
    res = subprocess.run(["bash", "-n"], input=helper, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize("on", [True, False])
def test_boot_script_uses_only_limas_template_fields(on):
    # Lima renders every provision script as a Go template at each start: a stray `{{` (in the
    # helper, the policy, a unit) breaks the render and the guest boots without the monitor.
    script, _ = monitor.render(on)
    assert set(re.findall(r"\{\{.*?\}\}", script)) <= {"{{.User}}", "{{.UID}}"}
    assert "LIMA_CIDATA" not in script


@pytest.mark.parametrize("on", [True, False])
def test_neither_boot_script_carries_the_others_marker(on):
    # set_provision drops every entry whose script MATCHES its marker. If the monitor's script
    # contained "# fy-provision ", recording the wall would silently delete the monitor (and the
    # reverse) — the two must never mention each other's marker.
    script, _ = monitor.render(on)
    assert PROVISION_MARKER not in script
    assert monitor.MARKER not in machine._render_provisioning()[0]


def test_turning_the_monitor_on_never_changes_the_wall_scripts_id(lima_env, monkeypatch):
    # The whole point of a SECOND recording: every byte of the wall script is in its id, and a
    # changed id refuses every running VM as stale. The monitor must not be one of those bytes.
    before = machine.provision_id()
    monkeypatch.setattr(monitor.config, "machine_monitor", lambda: "observe")
    assert machine.provision_id() == before


def test_observe_script_carries_the_pinned_release_and_the_findings():
    script, _ = monitor.render(True)
    assert "MODE='observe'" in script
    assert f"VERSION='{monitor.TETRAGON_VERSION}'" in script
    for sha in monitor.TETRAGON_SHA256.values():
        assert sha in script  # the anchor the root installer re-checks
    # spike finding 4: the health server listens on every interface unless told otherwise
    assert "conf health-server-address 127.0.0.1:6789" in script
    # spike finding 5: attribution keys on namespaces, so they must be in every event
    assert "conf enable-process-ns true" in script
    # the policy is the packaged one, named as the report expects
    assert f"name: {monitor.POLICY}" in script
    assert "rm -rf /etc/tetragon" in script  # nothing left from an earlier boot survives
    # the root installer verifies the ROOT-OWNED copy, and refuses links in the inbox
    assert "sha256sum -c --status" in script and '[ -L "$DELIVERY" ]' in script
    # a bad delivery beside a RUNNING install is recorded apart, never over the artifact line —
    # else one junk file from the inbox's uid would turn a healthy monitor's report red
    assert 'put rejected "checksum mismatch' in script


def test_observe_script_installs_the_relay_with_a_root_only_key():
    script, _ = monitor.render(True)
    assert "def snapshot(" in script  # relay.py, embedded
    assert f"printf '%s\\n' '{KEY}' >/etc/fy-monitor/relay.key" in script
    assert "(umask 077 &&" in script and "install -d -m 0700 /etc/fy-monitor" in script
    # its one argument is the VM user's uid, from Lima's own template field
    assert "uid='{{.UID}}'" in script
    assert "fy-monitor-relay $uid" in script
    # the off rendering carries no key
    assert KEY not in monitor.render(False)[0]


def test_the_relay_key_is_created_once_private_and_stable(monkeypatch, tmp_path):
    monkeypatch.undo()  # this test exercises the real relay_key
    monkeypatch.setattr(monitor.config, "state_dir", lambda: tmp_path)
    key = monitor.relay_key()
    path = tmp_path / "monitor-relay.key"
    assert len(key) == 64 and int(key, 16) >= 0
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert monitor.relay_key() == key  # stable: it is in the provisioning id
    path.write_text("truncated")
    assert monitor.relay_key() != key  # a damaged key is replaced, never used


def test_the_policy_never_enforces():
    # observe means observe: no selector may carry an action (Sigkill, Override, NotifyEnforcer…)
    policy = monitor._asset("policy.yaml").read_text()
    assert "matchActions" not in policy and "action:" not in policy.lower()


@pytest.mark.parametrize("on", [True, False])
def test_no_recursive_removal_reaches_a_mount(on):
    # Tetragon mounts cgroup2 under /var/run/tetragon; a recursive removal there rmdirs empty
    # system cgroups (it happened on the first live off-run). Nothing under /run, /var/run or
    # /sys is ours to remove recursively — /run is tmpfs and clears itself.
    script, _ = monitor.render(on)
    # one logical command per line: a path on a `\`-continued line is still that rm's argument
    # (the line this test exists for was exactly that, and a per-line scan missed it)
    for line in script.replace("\\\n", " ").splitlines():
        if "rm -rf" in line:
            assert not re.search(r"(^|\s)/(var/)?run/|(^|\s)/sys/", line), line


def test_off_script_removes_the_install():
    script, ident = monitor.render(False)
    assert "MODE='off'" in script
    assert "systemctl disable --now fy-monitor-relay.service tetragon.service" in script
    assert "rm -rf /etc/fy-monitor" in script  # the relay key goes with it
    assert ident != monitor.render(True)[1]


def test_the_id_changes_with_the_release(monkeypatch):
    before = monitor.render(True)[1]
    monkeypatch.setattr(monitor, "TETRAGON_VERSION", "v9.9.9")
    assert monitor.render(True)[1] != before


# ── recording it (machine.ensure) ───────────────────────────────────────────────────────


@pytest.fixture
def mon(lima_env, monkeypatch):
    """lima_env with the monitor switchable and its ensure stubbed (records the id it was asked
    for and returns ``problem``)."""
    be, _guest, set_wall, guest_ok = lima_env
    state = SimpleNamespace(level="off", asked=[], problem="")
    monkeypatch.setattr(monitor.config, "machine_monitor", lambda: state.level)

    def _ensure(backend, name, want_id, **kw):
        state.asked.append(want_id)
        return state.problem

    monkeypatch.setattr(monitor, "ensure", _ensure)
    set_wall(False)
    be._provision = machine.provision_id()
    guest_ok()
    return be, state


def test_monitor_off_and_never_on_records_nothing(mon, tmp_path):
    be, state = mon
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == [] and state.asked == []


def test_monitor_on_records_its_own_entry_before_the_boot(mon, tmp_path):
    be, state = mon
    state.level = "observe"
    be._state = "stopped"
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    want = monitor.render(True)[1]
    assert be.calls == [f"monitor:{want}", "start:homelab"]  # the wall's entry untouched
    assert state.asked == [want]  # …then checked against the guest after the boot


def test_monitor_stale_on_a_running_vm_is_refused(mon, tmp_path, capsys):
    be, state = mon
    state.level = "observe"
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == []
    err = capsys.readouterr().err
    assert "guest-monitor" in err and "fy machine stop && fy up" in err


def test_monitor_turned_off_records_the_removal(mon, tmp_path):
    be, state = mon
    be._monitor = monitor.render(True)[1]  # it was on
    be._state = "stopped"
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == [f"monitor:{monitor.render(False)[1]}", "start:homelab"]
    assert state.asked == []  # off: nothing to check in the guest


def test_monitor_not_observing_warns_and_carries_on(mon, tmp_path, capsys):
    be, state = mon
    state.level = "observe"
    be._monitor = monitor.render(True)[1]
    state.problem = "tetragon.service is failed"
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")  # no SystemExit: it only observes
    err = capsys.readouterr().err
    assert "NOT observing: tetragon.service is failed" in err


# ── the report ──────────────────────────────────────────────────────────────────────────


WANT = "0123456789abcdef"
GOOD = {
    "arch": "x86_64",
    "applied": f"applied {WANT}",
    "artifact": f"installed {monitor.TETRAGON_VERSION}",
    "policy": f"loaded {monitor.POLICY}",
    "service": "active",
    "relay": "active",
}


@pytest.mark.parametrize(
    "change, why",
    [
        ({}, ""),
        ({"applied": ""}, "boot script did not run"),
        ({"applied": "applied ffff"}, "different recording"),
        ({"artifact": "awaiting v1.7.1 amd64"}, "not installed"),
        ({"artifact": "rejected: checksum mismatch"}, "checksum mismatch"),
        ({"service": "failed"}, "tetragon.service is failed"),
        ({"policy": "loading"}, "policy is loading"),
        ({"relay": "failed"}, "relay (fy-monitor-relay.service) is failed"),
    ],
)
def test_report_problem(change, why):
    problem = monitor.Report(**{**GOOD, **change}).problem(WANT)
    assert (problem == "") if not why else (why in problem)


class Guest:
    """A scripted guest behind ``monitor._ssh``: answers the report, accepts a delivery (and
    then installs it, as the root .path unit would)."""

    def __init__(self, **state):
        self.state = {**GOOD, **state}
        self.delivered: list[bytes] = []
        self.reads = 0

    def ssh(self, target, script, stdin=None):
        if stdin is not None:
            self.delivered.append(stdin)
            self.state.update(artifact=f"installed {monitor.TETRAGON_VERSION}", policy="loading")
            return subprocess.CompletedProcess([], 0, "", "")
        self.reads += 1
        # ensure() waits on a real clock with sleep stubbed out: a loop that never converges
        # would spin until its deadline — fail it here instead of hanging the suite
        assert self.reads < 50, "monitor.ensure is polling a guest that will never settle"
        if self.reads > 2 and self.state["policy"] == "loading":
            self.state["policy"] = f"loaded {monitor.POLICY}"
        s = self.state
        out = "\n".join(s[k] for k in ("arch", "applied", "artifact", "policy", "service", "relay"))
        return subprocess.CompletedProcess([], 0, out + "\n", "")


@pytest.fixture
def guest(monkeypatch, tmp_path):
    def install(**state):
        g = Guest(**state)
        monkeypatch.setattr(monitor, "_ssh", g.ssh)
        monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
        monkeypatch.setattr(monitor, "_cache_dir", lambda: tmp_path / "cache")
        return g

    return install


class SshOnly:
    """The slice of a backend monitor.ensure uses: a name and an ssh route (or none)."""

    def __init__(self, reachable: bool = True):
        self.name = "lima"
        self._reachable = reachable

    def ssh_target(self, name: str) -> SshTarget | None:
        return SshTarget(user="u", port=22, identity="/k") if self._reachable else None


BACKEND = SshOnly()


def test_ensure_steady_state_only_reads(guest):
    g = guest()
    assert monitor.ensure(BACKEND, "acme", WANT) == ""
    assert g.delivered == [] and g.reads == 1


def test_ensure_delivers_the_pinned_release_when_the_guest_waits(guest, monkeypatch):
    blob = b"tetragon release bytes"
    monkeypatch.setitem(monitor.TETRAGON_SHA256, "x86_64", hashlib.sha256(blob).hexdigest())
    fetched = []
    monkeypatch.setattr(monitor, "_fetch", lambda url: fetched.append(url) or blob)
    g = guest(artifact="awaiting v1.7.1 amd64", policy="not installed")
    assert monitor.ensure(BACKEND, "acme", WANT) == ""
    assert g.delivered == [blob]
    assert fetched == [monitor.release_url("x86_64")]
    assert "tetragon-v1.7.1-amd64.tar.gz" in fetched[0]


def test_ensure_never_delivers_a_download_that_fails_the_pin(guest, monkeypatch):
    monkeypatch.setattr(monitor, "_fetch", lambda url: b"something else")
    g = guest(artifact="awaiting v1.7.1 amd64", policy="not installed")
    problem = monitor.ensure(BACKEND, "acme", WANT)
    assert "sha256" in problem
    assert g.delivered == []


def test_a_cached_release_is_rechecked_before_use(guest, monkeypatch, tmp_path):
    blob = b"good release"
    monkeypatch.setitem(monitor.TETRAGON_SHA256, "x86_64", hashlib.sha256(blob).hexdigest())
    cached = tmp_path / "cache" / f"tetragon-{monitor.TETRAGON_VERSION}-amd64.tar.gz"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"truncated")  # a damaged cache must never reach the guest
    monkeypatch.setattr(monitor, "_fetch", lambda url: blob)
    guest()
    assert monitor.download("x86_64").read_bytes() == blob


def test_ensure_reports_a_guest_that_ran_another_recording(guest):
    guest(applied="applied ffff")
    assert "different recording" in monitor.ensure(BACKEND, "acme", WANT)


def test_ensure_gives_up_waiting_and_says_why(guest):
    guest(policy="loading")
    g_problem = monitor.ensure(BACKEND, "acme", WANT, wait=0)
    assert "policy is loading" in g_problem


def test_ensure_unreachable_guest(monkeypatch):
    assert "can't reach" in monitor.ensure(SshOnly(reachable=False), "acme", WANT)


# ── the doctor row ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def doctor_env(monkeypatch, tmp_path):
    be = SimpleNamespace(name="lima", state=lambda name: "running")
    monkeypatch.setattr(devmode, "_BACKEND", be)
    monkeypatch.setattr(devmode.config, "machine_monitor", lambda: "observe")
    monkeypatch.setattr(devmode.config, "state_dir", lambda: tmp_path)  # the pull cursor's home
    return be


def test_doctor_row_silent_when_off(doctor_env, monkeypatch):
    monkeypatch.setattr(devmode.config, "machine_monitor", lambda: "off")
    assert list(devmode._guest_monitor_check()) == []


def test_doctor_row_observing(doctor_env, monkeypatch):
    want = monitor.render(True)[1]
    report = monitor.Report(**{**GOOD, "applied": f"applied {want}"})
    monkeypatch.setattr(monitor, "guest_report", lambda be, name: report)
    rows = [r for r in devmode._guest_monitor_check() if r[0] != "running"]
    assert [r[:2] for r in rows] == [("ok", "guest monitor"), ("warn", "monitor events")]
    assert "observing" in rows[0][2]
    assert "nothing pulled yet" in rows[1][2]  # no cursor: the supervisor hasn't pulled


def test_doctor_events_row_reads_the_pull_cursor(doctor_env, monkeypatch, tmp_path):
    import time

    from foldyard import monitorlog

    monkeypatch.setattr(monitorlog.config, "state_dir", lambda: tmp_path)
    cur = monitorlog.Cursor(pulled=time.time() - 3, stored=120, gaps=2)
    cur.save()
    [row] = list(devmode._monitor_events_check())
    assert row[0] == "ok" and "120 stored, 2 lost in transit" in row[2]
    monitorlog.Cursor(pulled=time.time(), forged=1).save()
    [row] = list(devmode._monitor_events_check())
    assert row[0] == "warn" and "failed their signature" in row[2]
    monitorlog.Cursor(pulled=time.time() - 600).save()
    [row] = list(devmode._monitor_events_check())
    assert row[0] == "warn" and "supervisor" in row[2]


def test_doctor_row_warns_never_fails(doctor_env, monkeypatch):
    report = monitor.Report(**{**GOOD, "service": "failed"})
    monkeypatch.setattr(monitor, "guest_report", lambda be, name: report)
    rows = [r for r in devmode._guest_monitor_check() if r[0] != "running"]
    assert rows[0][0] == "warn" and "NOT observing" in rows[0][2]
