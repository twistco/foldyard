"""forwarder.py — the attributing forwarder's host side (ADR-0034): its boot script, its key, the
guest's report, and how machine.ensure records and checks it; plus preflight and the doctor row.
The guest is never reached: ssh is stubbed at ``forwarder._ssh``. What only a real guest can show —
the policy routes under the VM firewall, the namespace following, the OCI hook's wait — is the
host-tier e2e's (tests/test_forwarder_e2e.py)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import test_machine
from foldyard import devmode, forwarder, machine, monitor, preflight
from foldyard.machine_backend import PROVISION_MARKER, SshTarget

fake = test_machine.fake
lima_env = test_machine.lima_env
no_proxy_ca = test_machine.no_proxy_ca

KEY = "cd" * 32


@pytest.fixture(autouse=True)
def fixed_keys(monkeypatch):
    """render(True) reads (and on first use creates) the host's keys; pin them here."""
    monkeypatch.setattr(forwarder, "key", lambda: KEY)
    monkeypatch.setattr(monitor, "relay_key", lambda: "ab" * 32)


# ── the boot script ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("on", [True, False])
def test_boot_script_is_valid_bash_with_its_own_marker(on):
    if shutil.which("bash") is None:
        pytest.skip("no bash on this host")
    script, ident = forwarder.render(on)
    assert script.splitlines()[1] == f"{forwarder.MARKER}{ident}"
    res = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr


def test_the_hook_is_valid_sh():
    if shutil.which("bash") is None:
        pytest.skip("no bash on this host")
    res = subprocess.run(
        ["bash", "-n"], input=forwarder._asset("wait.sh").read_text(), text=True,
        capture_output=True,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize("on", [True, False])
def test_boot_script_uses_only_limas_template_fields(on):
    # Lima renders every provision script as a Go template at each start: a stray `{{` (in the
    # forwarder's code, say) breaks the render and the guest boots without it.
    script, _ = forwarder.render(on)
    assert set(re.findall(r"\{\{.*?\}\}", script)) <= {"{{.User}}", "{{.UID}}"}
    assert "}}" not in script.replace("{{.User}}", "").replace("{{.UID}}", "")


@pytest.mark.parametrize("on", [True, False])
def test_no_boot_script_carries_anothers_marker(lima_env, on):
    # set_provision drops every entry whose script MATCHES its marker: a mention of another
    # script's marker would delete that script when this one is recorded (and the reverse).
    script, _ = forwarder.render(on)
    assert PROVISION_MARKER not in script and monitor.MARKER not in script
    assert forwarder.MARKER not in machine._render_provisioning()[0]
    assert forwarder.MARKER not in monitor.render(True)[0]
    assert forwarder.MARKER not in monitor.render(False)[0]


def test_turning_attribution_on_never_changes_the_other_scripts_ids(lima_env, monkeypatch):
    # The whole point of a recording of its own: every byte of the wall script is in its id, and
    # a changed id refuses every running VM as stale — including VMs that never opt in.
    _, _, set_wall, _ = lima_env
    set_wall(True)
    before = (machine.provision_id(), monitor.render(True)[1])
    monkeypatch.setattr(forwarder.config, "machine_attribution", lambda: "record")
    assert (machine.provision_id(), monitor.render(True)[1]) == before


def test_record_script_installs_the_forwarder_its_root_only_key_and_its_hook(lima_env):
    script, ident = forwarder.render(True)
    assert "MODE='record'" in script
    assert "class Follower" in script  # fwd.py, embedded
    assert f"printf '%s\\n' '{KEY}' >/etc/fy-fwd/forwarder.key" in script
    assert "(umask 077 &&" in script and "install -d -m 0700 /etc/fy-fwd" in script
    # podman reads OCI hooks from /usr/share only (seen live): /etc would be silently ignored
    assert "cat >/usr/share/containers/oci/hooks.d/fy-fwd-wait.json" in script
    assert '"stages": ["prestart"]' in script
    assert 'netns = "bridge"' in script
    # this project's proxy span is stamped, its token services' span forwarded untouched
    assert "--proxy-ports '41000-41089' --plain-ports '41100-41189'" in script
    assert "--gw 192.168.5.2" in script and f"--id {ident}" in script
    # the uid is Lima's own record of the VM user, and the walled set is the firewall's
    assert "uid='{{.UID}}'" in script and "/etc/subuid" in script
    # never enabled: an enabled unit would start early with the previous boot's copy
    assert "systemctl enable fy-fwd" not in script and "WantedBy" not in script
    assert "exit 0" in script.splitlines()[-1]


def test_off_script_removes_the_install_and_carries_no_key():
    script, ident = forwarder.render(False)
    assert "MODE='off'" in script
    assert KEY not in script and "class Follower" not in script
    assert "systemctl disable --now fy-fwd.service" in script
    assert "/usr/share/containers/oci/hooks.d/fy-fwd-wait.json" in script
    assert "93-fy-fwd-bridge.conf" in script and "nft delete table ip fy_fwd" in script
    # the routes go before the table: routes without it would send traffic to nothing
    assert script.index("ip rule del priority") < script.index("nft delete table ip fy_fwd")
    assert ident != forwarder.render(True)[1]


@pytest.mark.parametrize("on", [True, False])
def test_no_recursive_removal_reaches_run_or_sys(on):
    script, _ = forwarder.render(on)
    for line in script.replace("\\\n", " ").splitlines():
        if "rm -rf" in line or "rm -r " in line:
            assert not re.search(r"(^|\s)/(var/)?run/|(^|\s)/sys/", line), line


def test_the_id_changes_with_the_key_and_the_band(monkeypatch):
    before, before_off = forwarder.render(True)[1], forwarder.render(False)[1]
    monkeypatch.setattr(forwarder, "key", lambda: "ef" * 32)
    assert forwarder.render(True)[1] != before
    assert forwarder.render(False)[1] == before_off  # the off rendering carries no key
    monkeypatch.setattr(forwarder, "ports", lambda: ("42000-42089", "42100-42189"))
    assert forwarder.render(False)[1] != before_off


def test_the_key_is_created_once_private_and_stable(monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setattr(forwarder.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(forwarder.Path, "chmod", lambda self, mode: None)  # no fixing it up later
    old = os.umask(0o022)
    try:
        key = forwarder.key()
    finally:
        os.umask(old)
    path = tmp_path / "forwarder.key"
    assert len(key) == 64 and int(key, 16) >= 0
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert forwarder.key() == key  # stable: it is in the provisioning id
    path.write_text("truncated")
    assert forwarder.key() != key  # a damaged key is replaced, never used


# ── recording it (machine.ensure) ───────────────────────────────────────────────────────


@pytest.fixture
def fwd(lima_env, monkeypatch):
    """lima_env, walled, with attribution switchable and the guest's report scripted."""
    be, _guest, set_wall, guest_ok = lima_env
    state = SimpleNamespace(level="off", reports=[])
    monkeypatch.setattr(forwarder.config, "machine_attribution", lambda: state.level)
    set_wall(True)
    be._provision = machine.provision_id()
    guest_ok()

    def report(backend, name):
        if state.reports:
            return state.reports.pop(0) if len(state.reports) > 1 else state.reports[0]
        return forwarder.Report(
            applied=f"applied {forwarder.render(True)[1]}",
            state=f"serving {forwarder.render(True)[1]}",
            service="active",
        )

    monkeypatch.setattr(forwarder, "guest_report", report)
    monkeypatch.setattr(machine.time, "sleep", lambda s: None)
    return be, state


def test_attribution_off_and_never_on_records_nothing(fwd, tmp_path):
    be, _ = fwd
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == []


def test_attribution_on_records_its_own_entry_before_the_boot(fwd, tmp_path, capsys):
    be, state = fwd
    state.level = "record"
    be._state = "stopped"
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == [f"forwarder:{forwarder.render(True)[1]}", "start:homelab"]
    assert "attributing forwarder: serving" in capsys.readouterr().err


def test_attribution_stale_on_a_running_vm_is_refused(fwd, tmp_path, capsys):
    be, state = fwd
    state.level = "record"
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == []
    err = capsys.readouterr().err
    assert "STALE forwarder provisioning" in err and "fy machine stop && fy up" in err


def test_attribution_turned_off_records_the_removal(fwd, tmp_path):
    be, _ = fwd
    be._forwarder = forwarder.render(True)[1]  # it was on
    be._state = "stopped"
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    assert be.calls == [f"forwarder:{forwarder.render(False)[1]}", "start:homelab"]


def test_a_forwarder_that_isnt_serving_is_refused(fwd, tmp_path, capsys):
    # it is the only way from a container to the proxy: a dead one is no egress, so say so
    be, state = fwd
    state.level = "record"
    want = forwarder.render(True)[1]
    be._forwarder = want
    state.reports = [
        forwarder.Report(applied=f"applied {want}", state="failed: x", service="failed")
    ]
    with pytest.raises(SystemExit):
        machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")
    err = capsys.readouterr().err
    assert "NOT serving" in err and "fy-fwd.service is failed" in err
    assert "/run/fy-fwd/boot.log" in err


def test_a_forwarder_still_starting_is_waited_for(fwd, tmp_path):
    be, state = fwd
    state.level = "record"
    want = forwarder.render(True)[1]
    be._forwarder = want
    starting = forwarder.Report(
        applied=f"applied {want}", state=f"starting {want}", service="active"
    )
    serving = forwarder.Report(applied=f"applied {want}", state=f"serving {want}", service="active")
    state.reports = [starting, starting, serving]
    machine.ensure(tmp_path / "repo", tmp_path / "repo-wt")  # no SystemExit


# ── the report ──────────────────────────────────────────────────────────────────────────

WANT = "0123456789abcdef"
GOOD = {"applied": f"applied {WANT}", "state": f"serving {WANT}", "service": "active"}


@pytest.mark.parametrize(
    "change, why",
    [
        ({}, ""),
        ({"applied": ""}, "boot script did not run"),
        ({"applied": "applied ffff"}, "different recording"),
        ({"service": "failed"}, "fy-fwd.service is failed"),
        ({"state": "degraded: following failed"}, "degraded"),
        ({"state": "serving ffff"}, "serving ffff"),
    ],
)
def test_report_problem(change, why):
    problem = forwarder.Report(**{**GOOD, **change}).problem(WANT)
    assert (problem == "") if not why else (why in problem)


def test_the_report_is_read_over_ssh_as_the_vm_user(monkeypatch):
    seen = []

    def ssh(target, script, stdin=None):
        seen.append(script)
        out = f"applied {WANT}\nserving {WANT}\n4026532492\n{{}}\nactive\n"
        return subprocess.CompletedProcess([], 0, out, "")

    monkeypatch.setattr(forwarder, "_ssh", ssh)

    class Be:
        name = "lima"

        def __init__(self, target: SshTarget | None) -> None:
            self.target = target

        def ssh_target(self, name: str) -> SshTarget | None:
            return self.target

    report = forwarder.guest_report(Be(SshTarget("u", 22, "/id")), "vm")
    assert report == forwarder.Report(
        f"applied {WANT}", f"serving {WANT}", "4026532492", "{}", "active"
    )
    assert "sudo" not in seen[0]  # never root: the report is world-readable
    assert forwarder.guest_report(Be(None), "vm") is None


# ── preflight and doctor ───────────────────────────────────────────────────────────────


def test_preflight_needs_lima_and_the_firewall(monkeypatch):
    monkeypatch.setattr(preflight.config, "machine_attribution", lambda: "record")
    monkeypatch.setattr(preflight.config, "machine_wall", lambda: False)
    monkeypatch.setattr(preflight.config, "machine_host_wall", lambda: False)
    monkeypatch.setattr(preflight.config, "machine_monitor", lambda: "off")
    issues = "\n".join(preflight._attribution_issues("podman"))
    assert "needs the lima backend" in issues and "needs the VM firewall" in issues
    monkeypatch.setattr(preflight.config, "machine_wall", lambda: True)
    assert preflight._attribution_issues("lima") == []
    monkeypatch.setattr(preflight.config, "machine_attribution", lambda: "off")
    assert preflight._attribution_issues("podman") == []


def test_the_config_names_a_level_never_anything_else(monkeypatch):
    monkeypatch.setenv("MACHINE_ATTRIBUTION", "Record")
    assert forwarder.config.machine_attribution() == "record"
    monkeypatch.setenv("MACHINE_ATTRIBUTION", "/usr/bin/evil")
    with pytest.raises(SystemExit):
        forwarder.config.machine_attribution()


def _doctor(monkeypatch, report, state="running"):
    monkeypatch.setattr(devmode.config, "machine_attribution", lambda: "record")
    be = SimpleNamespace(name="lima", state=lambda n: state)
    monkeypatch.setattr(devmode, "_BACKEND", be)
    monkeypatch.setattr(forwarder, "guest_report", lambda b, n: report)
    return [r for r in devmode._forwarder_check() if r[0] != "running" and r[1] == "forwarder"]


def test_the_doctor_row_is_red_when_the_forwarder_isnt_serving(monkeypatch):
    want = forwarder.render(True)[1]
    good = forwarder.Report(f"applied {want}", f"serving {want}", "1", '{"bridge": 3}', "active")
    [(level, name, text)] = _doctor(monkeypatch, good)
    assert (level, name) == ("ok", "forwarder") and "3 from bridge containers" in text
    bad = forwarder.Report(f"applied {want}", "failed: boom", "", "", "failed")
    [(level, _, text)] = _doctor(monkeypatch, bad)
    assert level == "fail" and "NOT serving" in text
    [(level, _, _)] = _doctor(monkeypatch, None)
    assert level == "warn"  # unreachable: can't tell
    [(level, _, text)] = _doctor(monkeypatch, good, state="stopped")
    assert level == "warn" and "isn't running" in text


def test_the_doctor_row_is_silent_when_off(monkeypatch):
    monkeypatch.setattr(devmode.config, "machine_attribution", lambda: "off")
    monkeypatch.setattr(devmode, "_BACKEND", SimpleNamespace(name="lima"))
    assert list(devmode._forwarder_check()) == []
