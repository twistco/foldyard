"""monitor.py — the guest monitor (ADR-0032): its boot script, the release delivery, the report,
and how machine.ensure records and checks it. The guest is never reached: ssh is stubbed at
``monitor._ssh``, the download at ``monitor._fetch``. What only a real guest can show — Tetragon
loading the packaged policy, the .path unit firing, the box unable to stop it — is the host-tier
e2e's (tests/test_monitor_e2e.py)."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import test_machine
from foldyard import devmode, machine, monitor
from foldyard.machine_backend import PROVISION_MARKER, SshTarget

# The machine fixtures, reused: a scripted backend (`fake`), a running lima VM (`lima_env`) and
# the CA isolation it stands on (`no_proxy_ca`).
fake = test_machine.fake
lima_env = test_machine.lima_env
no_proxy_ca = test_machine.no_proxy_ca

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


# `tetra tracingpolicy list`'s table as each release prints it (tabwriter pads with spaces):
# v1.8.0 put a DOMAIN column between NAME and STATE, which a v1.7-shaped match never sees.
_V171 = (
    "ID   NAME         STATE      FILTERID   NAMESPACE   SENSORS          KERNELMEMORY   MODE\n"
    "1    fy-observe   {state}    0          (global)    generic_kprobe   1.23 MB        monitor\n"
)
_V180 = (
    "ID   NAME         DOMAIN   STATE      FILTERID   NAMESPACE   SENSORS          KERNELMEMORY\n"
    "1    fy-observe   grpc     {state}    0          (global)    generic_kprobe   1.23 MB\n"
)


@pytest.mark.spawns("bash")
@pytest.mark.parametrize("table", [_V171, _V180], ids=["v1.7.1", "v1.8.0"])
@pytest.mark.parametrize(
    ("name", "state", "enabled"),
    [
        ("fy-observe", "enabled", True),
        ("fy-observe", "disabled", False),
        ("fy-observe", "load_error", False),
        ("fy-observe-old", "enabled", False),  # another policy whose name starts the same
    ],
)
def test_the_report_reads_the_policy_as_loaded_in_either_tetra_table(table, name, state, enabled):
    if shutil.which("bash") is None:
        pytest.skip("no bash on this host")
    helper = (monitor._asset("fy-monitor.sh")).read_text()
    fn = re.search(r"^policy_enabled\(\) \{.*?^\}", helper, re.S | re.M)
    assert fn, "fy-monitor.sh defines policy_enabled()"
    rows = table.format(state=state).replace("1    fy-observe", "1    " + name)
    res = subprocess.run(
        ["bash", "-c", f"POLICY=fy-observe\n{fn.group(0)}\npolicy_enabled"],
        input=rows, text=True, capture_output=True,
    )  # fmt: skip
    assert (res.returncode == 0) is enabled, (rows, res.stderr)


@pytest.mark.spawns("bash")
def test_the_report_reads_tetras_whole_table_even_after_a_match():
    # Returning at the match closes the pipe on a tetra still writing (one write per row, v1.8.0):
    # SIGPIPE fails the pipeline under pipefail, and the report reads a loaded policy as not yet.
    if shutil.which("bash") is None:
        pytest.skip("no bash on this host")
    helper = (monitor._asset("fy-monitor.sh")).read_text()
    fn = re.search(r"^policy_enabled\(\) \{.*?^\}", helper, re.S | re.M)
    assert fn
    rows = _V180.format(state="enabled") + "2    other-policy   grpc     enabled    0\n"
    res = subprocess.run(
        ["bash", "-c", f"POLICY=fy-observe\n{fn.group(0)}\n{{ policy_enabled; echo rc=$?; while read -r l; do echo left; done; }}"],
        input=rows, text=True, capture_output=True,
    )  # fmt: skip
    assert res.stdout.strip() == "rc=0", res.stdout


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


def test_the_relay_key_is_never_on_disk_with_wider_permissions(monkeypatch, tmp_path):
    # the key is what makes the spool's signatures mean anything: created 0600, not chmodded after
    monkeypatch.undo()
    monkeypatch.setattr(monitor.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(monitor.Path, "chmod", lambda self, mode: None)  # no fixing it up later
    old = os.umask(0o022)
    try:
        monitor.relay_key()
    finally:
        os.umask(old)
    assert oct((tmp_path / "monitor-relay.key").stat().st_mode & 0o777) == "0o600"


def test_the_collector_is_started_by_the_boot_script_never_by_systemd_at_boot():
    # An enabled unit starts early in boot with the PREVIOUS boot's config, before Lima runs this
    # script — so a policy fix only applied minutes into the boot (seen live). Never enabled; any
    # enablement an older script left is removed.
    script, _ = monitor.render(True)
    assert "systemctl enable tetragon" not in script
    assert "systemctl enable fy-monitor-relay" not in script
    assert "systemctl disable tetragon.service fy-monitor-relay.service" in script
    tetragon_unit = script.split("/etc/systemd/system/tetragon.service <<'__FY_UNIT__'")[1]
    assert "WantedBy" not in tetragon_unit.split("__FY_UNIT__")[0]
    relay_unit = script.split(">/etc/systemd/system/fy-monitor-relay.service")[0][-600:]
    assert "WantedBy" not in relay_unit


def test_the_policy_never_watches_the_monitors_own_traffic():
    # The relay writes the spool and the host's pull reads it: a watched spool records the
    # monitor's own I/O, which it then spools — a feedback loop (~68,000 events in minutes, live).
    policy = monitor._asset("policy.yaml").read_text()
    prefixes = re.findall(r"^\s+- (/\S+)$", policy, re.M)
    assert "/var/lib/fy-monitor/inbox" in prefixes
    for watched in prefixes:
        for own in ("/var/lib/fy-monitor/spool/", "/var/lib/fy-monitor/relay.pos",
                    "/var/log/tetragon/tetragon.log", "/run/fy-monitor/relay"):  # fmt: skip
            assert not own.startswith(watched), (watched, own)


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
    assert f"tetragon-{monitor.TETRAGON_VERSION}-amd64.tar.gz" in fetched[0]


def test_ensure_never_delivers_a_download_that_fails_the_pin(guest, monkeypatch):
    monkeypatch.setattr(monitor, "_fetch", lambda url: b"something else")
    g = guest(artifact="awaiting v1.7.1 amd64", policy="not installed")
    problem = monitor.ensure(BACKEND, "acme", WANT)
    assert "sha256" in problem
    assert g.delivered == []


def test_a_stalled_delivery_is_a_reason_never_an_abort(guest, monkeypatch):
    # `_ssh` times out after 600 s; the monitor only observes, so a delivery that hangs is a
    # warning for `fy up` to print, not an exception that takes `machine.ensure` down with it
    blob = b"tetragon release bytes"
    monkeypatch.setitem(monitor.TETRAGON_SHA256, "x86_64", hashlib.sha256(blob).hexdigest())
    monkeypatch.setattr(monitor, "_fetch", lambda url: blob)
    g = guest(artifact="awaiting v1.7.1 amd64", policy="not installed")
    reads = g.ssh

    def stall(target, script, stdin=None):
        if stdin is not None:
            raise subprocess.TimeoutExpired("ssh", 600)
        return reads(target, script)

    monkeypatch.setattr(monitor, "_ssh", stall)
    problem = monitor.ensure(BACKEND, "acme", WANT)
    assert "copying Tetragon into the VM failed" in problem and "timed out" in problem


class Clock:
    """``monitor.time`` with a clock that moves only when ensure() sleeps."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_a_relay_that_never_comes_up_is_reported_within_its_grace_not_the_wait(guest, monkeypatch):
    # Restart=always, and the relay waits for Tetragon's export in-process: `activating` with
    # Tetragon up and the policy loaded is a crash loop — waiting the whole 600 s helps no one
    g = guest(relay="activating")
    clock = Clock()
    monkeypatch.setattr(monitor, "time", clock)
    problem = monitor.ensure(BACKEND, "acme", WANT)
    assert "fy-monitor-relay.service" in problem and "activating" in problem
    assert clock.now <= monitor.RELAY_GRACE + 5 and g.reads < 20


def test_a_relay_not_started_because_tetragon_failed_is_reported_at_once(guest, monkeypatch):
    # fy-monitor.sh starts the relay only after Tetragon starts: an inactive relay beside a
    # failed Tetragon will never change by itself
    guest(service="failed", policy="failed: tetragon.service did not start", relay="inactive")
    clock = Clock()
    monkeypatch.setattr(monitor, "time", clock)
    assert "tetragon.service is failed" in monitor.ensure(BACKEND, "acme", WANT)
    assert clock.now == 0


def test_a_policy_that_failed_beside_a_running_tetragon_is_reported_at_once(guest, monkeypatch):
    # Tetragon up, its policy refused: the relay grace never starts (it counts from a LOADED
    # policy), so an inactive relay must not hold ensure() for the whole wait either
    guest(policy="failed: fy-observe did not load", relay="inactive")
    clock = Clock()
    monkeypatch.setattr(monitor, "time", clock)
    assert "policy is failed" in monitor.ensure(BACKEND, "acme", WANT)
    assert clock.now == 0


def test_a_relay_still_starting_after_the_policy_loads_is_waited_for(guest, monkeypatch):
    g = guest(relay="inactive")
    clock = Clock()
    monkeypatch.setattr(monitor, "time", clock)
    reads = g.ssh

    def comes_up(target, script, stdin=None):
        if g.reads == 2:
            g.state["relay"] = "active"
        return reads(target, script, stdin)

    monkeypatch.setattr(monitor, "_ssh", comes_up)
    assert monitor.ensure(BACKEND, "acme", WANT) == ""


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


# ── the hooks rows ──────────────────────────────────────────────────────────────────────


def test_doctor_hooks_row_says_none_and_how_to_add_one(doctor_env):
    [row] = list(devmode._monitor_hooks_check())
    assert row[:2] == ("ok", "monitor hooks") and "`fy monitor hook add`" in row[2]


def test_doctor_hooks_rows_are_silent_when_the_monitor_is_off(doctor_env, monkeypatch):
    monkeypatch.setattr(devmode.config, "machine_monitor", lambda: "off")
    assert list(devmode._monitor_hooks_check()) == []


def test_doctor_hooks_rows_report_each_hooks_health(doctor_env, tmp_path):
    from foldyard import monitorhooks

    (tmp_path / "monitor-hooks.toml").write_text(
        '[[hook]]\nname = "score"\ncommand = ["/bin/score"]\n'
        '[[hook]]\nname = "ca"\nfile = "/srv/e.jsonl"\n'
        '[[hook]]\nname = "bad"\ncommand = ["rel"]\n'
    )
    monitorhooks._save_state(
        {"score": monitorhooks.HookState(delivered=3, ok_at=1.0),
         "ca": monitorhooks.HookState(dropped=4, error="refused: a symlink")}
    )  # fmt: skip
    rows = {r[1]: r for r in devmode._monitor_hooks_check()}
    assert rows["monitor hooks"][0] == "warn" and "absolute path" in rows["monitor hooks"][2]
    assert (
        rows["monitor hook score"][0] == "ok"
        and "3 events delivered" in rows["monitor hook score"][2]
    )
    assert rows["monitor hook ca"][0] == "warn" and "4 events skipped" in rows["monitor hook ca"][2]


# ── the `observe-files` level: writes under a general set of directories, container processes only


@pytest.fixture
def observe_files(monkeypatch):
    monkeypatch.setattr(monitor.config, "machine_monitor", lambda: "observe-files")


def _selectors(script: str) -> list[str]:
    policy = script.split("__FY_MONITOR_POLICY__")[1]
    body = policy.split("call: security_file_permission", 1)[1].split("\n  - call:", 1)[0]
    body = re.sub(r"^\s*#.*\n", "", body, flags=re.M)
    return [s.rstrip() for s in re.split(r"^    - (?=match)", body, flags=re.M)[1:]]


def test_observe_renders_exactly_the_packaged_policy(monkeypatch):
    # A changed byte is a changed id, and refuses every running observed VM as stale: a level
    # nobody asked for must not do that to the one they run.
    monkeypatch.setattr(monitor.config, "machine_monitor", lambda: "observe")
    script, _ = monitor.render(True)
    assert script.split("__FY_MONITOR_POLICY__")[1].strip("'\n") == monitor._asset(
        "policy.yaml"
    ).read_text().rstrip("\n")


def test_observe_files_adds_one_selector_to_observes_own(observe_files, monkeypatch):
    wide, wide_id = monitor.render(True)
    monkeypatch.setattr(monitor.config, "machine_monitor", lambda: "observe")
    plain, plain_id = monitor.render(True)
    assert wide_id != plain_id
    assert _selectors(wide)[: len(_selectors(plain))] == _selectors(plain)
    assert len(_selectors(wide)) == len(_selectors(plain)) + 1
    assert len(_selectors(wide)) <= 5  # Tetragon's limit for one hook


def test_observe_files_keeps_only_writes_by_processes_outside_the_vms_mount_namespace(
    observe_files,
):
    added = _selectors(monitor.render(True)[0])[-1]
    assert re.search(r"index: 1\s+operator: Equal\s+values:\s+- \"2\"", added)  # MAY_WRITE
    assert re.search(r"namespace: Mnt\s+operator: NotIn\s+values:\s+- host_ns", added)


def test_observe_files_watches_whole_directories_and_never_container_storage(observe_files):
    # `/etc` would match `/etcetera`. And podman's own processes are outside the VM's mount
    # namespace too: a watched /home or /var would record every image layer they unpack.
    added = _selectors(monitor.render(True)[0])[-1]
    paths = re.findall(r"- (/\S+)$", added, re.M)
    assert paths and all(p.endswith("/") for p in paths)
    storage = "/home/user.guest/.local/share/containers/storage/overlay/x"
    for own in (storage, "/var/lib/containers/storage/overlay/x",
                "/var/lib/fy-monitor/spool/1", "/var/log/tetragon/tetragon.log"):  # fmt: skip
        assert not any(own.startswith(p) for p in paths), own


def test_observe_files_never_enforces(observe_files):
    script, _ = monitor.render(True)
    assert "matchActions" not in script and "action:" not in "".join(_selectors(script)).lower()


# Measured 2026-11-24 (Tetragon v1.8.0, kernel 7.2): a write selector alone misses each of these.
_HOOKS = {
    # A name's DIRECTORY, resolved through mounts, not the name: that reads as `/etc/x` for
    # `/dev/shm/etc/x`, and as `/x` in a volume at `/opt` (measured 2026-11-24).
    "security_path_rename": {"0", "2"},  # the old directory and the new: either a watched one
    "security_path_unlink": {"0"},
    "security_path_chmod": {"0"},
    "security_path_truncate": {"0"},
    "security_file_truncate": {"0"},  # ftruncate, and an open that truncates
    "security_path_link": {"0", "1"},  # the old name (its directory isn't given), the new directory
}
_NAMES = {"security_path_rename": "0 1 2 3", "security_path_unlink": "0 1", "security_path_link": "0 1 2"}  # fmt: skip


def _hooks(script: str) -> dict[str, str]:
    policy = script.split("__FY_MONITOR_POLICY_2__")[1] if "POLICY_2__" in script else ""
    return {c.split("\n", 1)[0]: c for c in re.split(r"^  - call: ", policy, flags=re.M)[1:]}


def test_observe_files_records_what_a_write_selector_misses(observe_files, monkeypatch):
    assert set(_hooks(monitor.render(True)[0])) == {*_HOOKS, "security_path_chroot"}
    monkeypatch.setattr(monitor.config, "machine_monitor", lambda: "observe")
    assert _hooks(monitor.render(True)[0]) == {}
    monkeypatch.setattr(monitor.config, "machine_monitor", lambda: "observe-files")
    assert _hooks(monitor.render(False)[0]) == {}


def test_no_policy_holds_more_prefix_filters_than_tetragon_does(observe_files):
    # Nine or more in one policy and it doesn't load, which stops Tetragon ("failed to insert
    # string_prefix_map_8: key too big for map", live 2026-11-24).
    script, _ = monitor.render(True)
    for policy in (script.split("__FY_MONITOR_POLICY__")[1], "".join(_hooks(script).values())):
        assert 0 < policy.count("operator: Prefix") <= 8


@pytest.mark.parametrize(("call", "indexes"), _HOOKS.items())
def test_each_added_hook_is_held_to_the_watched_directories_and_to_containers(
    observe_files, call, indexes
):
    # Unfiltered, they cost 27,000 events for one package install, against 3,100 without them.
    script, _ = monitor.render(True)
    watched = re.findall(r"- (/\S+)$", _selectors(script)[-1], re.M)
    selectors = re.split(r"^    - (?=matchArgs)", _hooks(script)[call], flags=re.M)[1:]
    assert {re.search(r"index: (\d)", s).group(1) for s in selectors} == indexes  # type: ignore
    assert len(selectors) <= 5  # Tetragon's limit for one hook
    for s in selectors:
        exact = "operator: Equal" in s  # the directory itself: `/etc`, and not `/etcetera`
        assert re.findall(r"- (/\S+)$", s, re.M) == [w.rstrip("/") if exact else w for w in watched]
        assert re.search(r"namespace: Mnt\s+operator: NotIn\s+values:\s+- host_ns", s)


@pytest.mark.parametrize(("call", "indexes"), _NAMES.items())
def test_a_hook_on_names_takes_their_directories_too(observe_files, call, indexes):
    hook = _hooks(monitor.render(True)[0])[call]
    args = hook.split("returnArg")[0]
    assert re.findall(r"index: (\d)", args) == indexes.split()
    for i in re.findall(
        r"index: (\d)\s+operator: Equal", hook
    ):  # a directory, as its process sees it
        assert re.search(rf"index: {i}\s+type: path", args)


def test_observe_files_records_a_container_that_changes_its_root(observe_files):
    # After it, every path the process uses is another file's than it says.
    hook = _hooks(monitor.render(True)[0])["security_path_chroot"]
    assert re.search(r"index: 0\s+type: path", hook) and "matchArgs" not in hook
    assert re.search(r"namespace: Mnt\s+operator: NotIn\s+values:\s+- host_ns", hook)


@pytest.mark.parametrize("call", [*_HOOKS, "security_path_chroot"])
def test_an_added_hook_the_kernel_lacks_never_stops_tetragon(observe_files, call):
    # One hook that can't attach fails its whole policy, and with it every other recording.
    assert re.search(r"ignore:\s+callNotFound: true", _hooks(monitor.render(True)[0])[call])


def _reported(script: str, *rows: str) -> str:
    """What the rendered helper's report says of a tetra that lists ``rows``."""
    if shutil.which("bash") is None:
        pytest.skip("no bash on this host")
    names = ("policy_enabled", "verb_report")
    fns = [re.search(rf"^{n}\(\) \{{.*?^\}}", script, re.S | re.M) for n in names]
    body = "\n".join(f.group(0) for f in fns if f).replace("/usr/local/bin/tetra", "tetra")
    table = "ID NAME DOMAIN STATE\n" + "".join(f"{i} {r}\n" for i, r in enumerate(rows, 1))
    stubs = 'systemctl() { :; }; sleep() { :; }; seq() { echo 1 2; }; put() { echo "$1: $2"; }\n'
    stubs += f"tetra() {{ printf '%s' '{table}'; }}\n"
    res = subprocess.run(
        ["bash", "-c", f"set -uo pipefail\nPOLICY=fy-observe\n{stubs}{body}\nverb_report"],
        text=True, capture_output=True, check=True,
    )  # fmt: skip
    return res.stdout.strip()


@pytest.mark.spawns("bash")
@pytest.mark.parametrize("state", ["", "load_error", "disabled"])
def test_observe_files_isnt_loaded_until_both_its_policies_are(observe_files, state):
    # Tetragon runs on with the second one refused, and records none of what it's for.
    script, _ = monitor.render(True)
    first, second = "fy-observe static enabled", "fy-observe-files static "
    said = _reported(script, first, *([second + state] if state else []))
    assert said == "policy: failed: fy-observe or fy-observe-files not loaded after 600s"
    assert _reported(script, second + "enabled") == said
    assert _reported(script, second + "enabled", first) == "policy: loaded fy-observe"


@pytest.mark.spawns("bash")
def test_observe_waits_for_its_one_policy(monkeypatch):
    monkeypatch.setattr(monitor.config, "machine_monitor", lambda: "observe")
    script, _ = monitor.render(True)
    assert _reported(script, "fy-observe static enabled") == "policy: loaded fy-observe"
    assert _reported(script) == "policy: failed: fy-observe not loaded after 600s"
    assert "fy-observe-files" not in script


def test_both_levels_want_the_monitor(monkeypatch):
    for level, on in (("off", False), ("observe", True), ("observe-files", True)):
        monkeypatch.setattr(monitor.config, "machine_monitor", lambda level=level: level)
        assert monitor.wanted() is on
