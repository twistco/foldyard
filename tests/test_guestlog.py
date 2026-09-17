"""guestlog.py — the VM's log budget: a journald size cap (root: Lima's boot script, or sudo over
ssh on podman machine's appliance) and the API service's log level (a user-level podman.service
drop-in over ssh on both). The guest is mocked; nothing here touches a VM."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from foldyard import guestlog
from foldyard.sandbox import SshTarget


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_TARGET = SshTarget(user="core", port=50022, identity="/h/.local/share/containers/podman/machine/k")


class FakeBackend:
    def __init__(self, name: str, target: SshTarget | None = _TARGET):
        self.name = name
        self._target = target

    def ssh_target(self, name):
        return self._target


@pytest.fixture
def guest(monkeypatch):
    state: dict = {"scripts": [], "rc": 0}

    def fake_ssh(target, script, stdin=None):
        state["scripts"].append(script)
        return _Proc(state["rc"], "", "ssh: boom" if state["rc"] else "")

    monkeypatch.setattr(guestlog, "_ssh", fake_ssh)
    return state


def test_user_script_pins_the_api_log_level_as_a_drop_in():
    # The stock rootless unit runs `podman $LOGGING system service` with LOGGING=--log-level=info,
    # which logs every API request — the TUI/doctor polling alone was ~48k journal lines per 2h,
    # the single biggest producer. A drop-in's Environment= wins over the unit's own; applied
    # only when the file changed, so the steady-state `fy up` restarts nothing.
    script = guestlog.user_script()
    assert "podman.service.d/50-foldyard-loglevel.conf" in script
    assert f"Environment=LOGGING=--log-level={guestlog.API_LOG_LEVEL}" in script
    assert "systemctl --user daemon-reload" in script
    assert "systemctl --user try-restart podman.service" in script


def test_journal_snippet_caps_journald_and_vacuums_on_change():
    # Containers log to journald, so the journal IS the container logs: Fedora's default cap is
    # min(10% of the fs, 4G) and a 90G VM disk sat at the 4G ceiling. 1G keeps a useful
    # `podman logs` window for a chatty worker; the vacuum reclaims the backlog at once rather
    # than waiting for rotation.
    snippet = guestlog.journal_snippet(sudo="")
    assert "journald.conf.d/50-foldyard-cap.conf" in snippet
    assert f"SystemMaxUse={guestlog.JOURNAL_MAX_USE}" in snippet
    assert "systemctl restart systemd-journald" in snippet
    assert f"journalctl --vacuum-size={guestlog.JOURNAL_MAX_USE}" in snippet


def test_journal_snippet_prefixes_every_root_step_with_the_given_sudo():
    snippet = guestlog.journal_snippet(sudo="sudo -n")
    for verb in ("install -d", "tee", "systemctl restart systemd-journald", "journalctl --vacuum"):
        assert f"sudo -n {verb}" in snippet, verb


@pytest.mark.parametrize("sudo", ["", "sudo -n"])
def test_scripts_are_valid_bash(sudo):
    if shutil.which("bash") is None:
        pytest.skip("no bash on this host")
    for script in (guestlog.user_script(), guestlog.journal_snippet(sudo=sudo)):
        res = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert res.returncode == 0, res.stderr


def test_ensure_on_podman_machine_does_both_over_ssh_with_sudo_for_the_journal(guest):
    # podman machine's CoreOS appliance can't take a boot script, but its user keeps
    # passwordless sudo — so root-side housekeeping goes over ssh there, non-interactive.
    guestlog.ensure(FakeBackend("podman"), "tangible")
    assert len(guest["scripts"]) == 1
    script = guest["scripts"][0]
    assert "50-foldyard-loglevel.conf" in script
    assert "sudo -n tee /etc/systemd/journald.conf.d/50-foldyard-cap.conf" in script


def test_ensure_on_lima_leaves_the_journal_cap_to_the_boot_script(guest):
    # Lima's VM user has no sudo (root is boot-time only, machine.py) — the cap is rendered into
    # the root boot script there, and the ssh pass carries only the user-level drop-in.
    guestlog.ensure(FakeBackend("lima"), "homelab")
    assert len(guest["scripts"]) == 1
    assert "50-foldyard-loglevel.conf" in guest["scripts"][0]
    assert "journald.conf.d" not in guest["scripts"][0]


def test_ensure_skips_a_backend_without_ssh(guest):
    guestlog.ensure(
        FakeBackend("podman", target=None), "x"
    )  # a machine whose ssh target can't be read
    assert guest["scripts"] == []


def test_ensure_warns_and_carries_on_when_the_guest_refuses(guest, capsys):
    # Housekeeping, not a posture: a failed log-budget pass must never block `fy up` the way a
    # failed gVisor provisioning does.
    guest["rc"] = 1
    guestlog.ensure(FakeBackend("podman"), "tangible")
    err = capsys.readouterr().err
    assert "⚠" in err and "log budget" in err and "ssh: boom" in err


def test_ensure_warns_and_carries_on_when_ssh_stalls(guest, monkeypatch, capsys):
    # `_ssh` has a timeout; a guest that accepts the connection and then hangs must surface as
    # the same warning, not as a TimeoutExpired escaping `machine ensure`.
    def stalled(target, script, stdin=None):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=600)

    monkeypatch.setattr(guestlog, "_ssh", stalled)
    guestlog.ensure(FakeBackend("podman"), "tangible")
    err = capsys.readouterr().err
    assert "⚠" in err and "log budget" in err and "timed out" in err
