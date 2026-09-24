"""Showing every project's Lima VM in Podman Desktop at once.

Podman Desktop's Lima extension shows ONE instance, read once at start. Its podman extension,
with "Load remote system connections (ssh)" on, polls `podman system connection list` every 5s
and shows each `ssh://` connection as its own entry — so foldyard registers `fy-<machine>` per
VM (never the default), pins the VM's ssh port so the entry survives a reboot, and turns the
setting on. Nothing else in the operator's settings or connections changes."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from foldyard import machine, podman_desktop
from foldyard.machine_backend import SshTarget

_URI = "ssh://dain@127.0.0.1:41390/run/user/501/podman/podman.sock"
_KEY = "/Users/dain/.lima/_config/user"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(podman_desktop, "settings_path", lambda: path)
    return path


class FakePodman:
    """`podman system connection …` over an in-memory list, recording every call."""

    def __init__(self, connections=(), promote_first=False):
        self.connections = [dict(c) for c in connections]
        self.calls: list[list[str]] = []
        self.promote_first = promote_first  # podman makes a lone new connection the default

    def __call__(self, argv):
        self.calls.append(argv[1:])
        verb = argv[3]
        if verb == "list":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.connections), "")
        if verb == "add":
            name, uri = argv[4], argv[5]
            identity = argv[argv.index("--identity") + 1]
            promote = self.promote_first or not any(c["Default"] for c in self.connections)
            if promote:
                for c in self.connections:
                    c["Default"] = False
            self.connections.append(
                {"Name": name, "URI": uri, "Identity": identity, "Default": promote}
            )
        elif verb == "remove":
            self.connections = [c for c in self.connections if c["Name"] != argv[4]]
        elif verb == "default":
            for c in self.connections:
                c["Default"] = c["Name"] == argv[4]
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def podman(monkeypatch):
    def install(**kw):
        fake = FakePodman(**kw)
        monkeypatch.setattr(podman_desktop, "_podman", lambda: "podman")
        monkeypatch.setattr(podman_desktop, "_run", fake)
        return fake

    return install


def test_the_connection_uri_is_the_vms_rootless_socket_over_its_ssh_port():
    target = SshTarget(user="dain", port=41390, identity=_KEY)
    assert podman_desktop.uri(target, "/run/user/501/podman/podman.sock") == _URI


def test_registers_without_taking_the_default(podman):
    fake = podman(
        connections=[{"Name": "tangible", "URI": "ssh://core@x", "Identity": "k", "Default": True}],
        promote_first=True,
    )
    assert podman_desktop.register("fy-repower", _URI, _KEY) == "added"
    by_name = {c["Name"]: c for c in fake.connections}
    assert by_name["fy-repower"]["URI"] == _URI and by_name["fy-repower"]["Identity"] == _KEY
    # bare `podman` on the host keeps talking to what it talked to before
    assert by_name["tangible"]["Default"] is True and by_name["fy-repower"]["Default"] is False
    assert podman_desktop.register("fy-repower", _URI, _KEY) == "unchanged"
    assert [c for c in fake.calls if c[2] in ("add", "remove")] == [
        ["system", "connection", "add", "fy-repower", _URI, "--identity", _KEY]
    ]


def test_a_moved_port_replaces_the_connection(podman):
    old = _URI.replace("41390", "63841")
    fake = podman(
        connections=[{"Name": "fy-repower", "URI": old, "Identity": _KEY, "Default": False}]
    )
    assert podman_desktop.register("fy-repower", _URI, _KEY) == "updated"
    assert [c["URI"] for c in fake.connections] == [_URI]


def test_without_a_podman_cli_nothing_is_registered(monkeypatch):
    monkeypatch.setattr(podman_desktop, "_podman", lambda: None)
    monkeypatch.setattr(podman_desktop, "_run", lambda argv: pytest.fail("no podman to run"))
    assert podman_desktop.register("fy-repower", _URI, _KEY) == "no-podman"
    assert podman_desktop.unregister("fy-repower") == "no-podman"


def test_unregister_removes_only_ours(podman):
    fake = podman(
        connections=[{"Name": "fy-repower", "URI": _URI, "Identity": _KEY, "Default": False}]
    )
    assert podman_desktop.unregister("fy-garmin") == "absent"
    assert podman_desktop.unregister("fy-repower") == "removed"
    assert fake.connections == []


def test_turns_on_remote_connections_and_keeps_every_other_key(settings):
    settings.write_text(json.dumps({"window.bounds": {"x": 1}, "lima.name": "garmin"}, indent=2))
    assert podman_desktop.enable_remote() == "enabled"
    doc = json.loads(settings.read_text())
    assert doc == {
        "window.bounds": {"x": 1},
        "lima.name": "garmin",
        "podman.system.connections.remote": True,
    }
    assert podman_desktop.enable_remote() == "already"


def test_leaves_an_absent_or_unreadable_settings_file_alone(settings):
    # not installed / never started: never a file we create
    assert podman_desktop.enable_remote() == "absent" and not settings.exists()
    settings.write_text("{ half written")
    assert podman_desktop.enable_remote() == "unreadable"
    assert settings.read_text() == "{ half written"


def test_follows_only_when_the_operator_says_so(monkeypatch):
    assert podman_desktop.following() is False
    monkeypatch.setenv("FOLDYARD_PODMAN_DESKTOP", "1")
    assert podman_desktop.following() is True


# ── the machine side: pin the port, register, clean up ────────────────────────────────────


class LimaStub:
    name = "lima"

    def __init__(self, port=63841, running=True):
        self.port, self.running, self.pinned = port, running, []

    def ssh_port(self, name):
        return self.port

    def pin_ssh_port(self, name, port):
        self.pinned.append(port)
        self.port = port
        return True

    def ssh_target(self, name):
        return SshTarget(user="dain", port=self.port, identity=_KEY)

    def guest_socket(self):
        return "/run/user/501/podman/podman.sock"


@pytest.fixture
def lima(monkeypatch):
    def install(**kw):
        be = LimaStub(**kw)
        monkeypatch.setattr(machine, "BACKEND", be)
        monkeypatch.setattr(machine, "MACHINE", "repower")
        monkeypatch.setattr(machine, "state", lambda: "running" if be.running else "stopped")
        monkeypatch.setattr(machine.config, "machine_ssh_port", lambda: 41390)
        return be

    return install


def test_the_ssh_port_is_pinned_only_on_a_stopped_vm_of_an_opted_in_operator(lima, monkeypatch):
    be = lima(running=False)
    machine._pin_ssh_port()
    assert be.pinned == []  # not opted in
    monkeypatch.setenv("FOLDYARD_PODMAN_DESKTOP", "1")
    be = lima(running=True)
    machine._pin_ssh_port()
    assert be.pinned == []  # `limactl edit` refuses a running VM: pinned at its next start
    be = lima(running=False)
    machine._pin_ssh_port()
    machine._pin_ssh_port()
    assert be.pinned == [41390]  # once: already pinned the second time


def test_ensure_registers_the_vm_when_opted_in(lima, podman, settings, monkeypatch, capsys):
    lima(port=41390)
    fake = podman()
    machine._follow_in_podman_desktop()
    assert fake.calls == []  # not opted in
    monkeypatch.setenv("FOLDYARD_PODMAN_DESKTOP", "1")
    machine._follow_in_podman_desktop()
    assert [c["Name"] for c in fake.connections] == ["fy-repower"]
    assert "fy-repower" in capsys.readouterr().err
    machine._follow_in_podman_desktop()
    assert capsys.readouterr().err == ""  # the steady state says nothing


def test_the_verb_registers_on_demand_without_the_opt_in(lima, podman, settings, monkeypatch):
    from typer.testing import CliRunner

    from foldyard import cli

    lima(port=41390)
    fake = podman()
    settings.write_text("{}")
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    result = CliRunner().invoke(cli.app, ["machine", "desktop"])
    assert result.exit_code == 0, result.output
    assert [c["URI"] for c in fake.connections] == [_URI]
    assert json.loads(settings.read_text())["podman.system.connections.remote"] is True
    assert "restart Podman Desktop once" in result.output
    assert "FOLDYARD_PODMAN_DESKTOP=1" in result.output  # how to keep it current
    # the box has no Podman Desktop, and a podman-machine VM is shown natively
    monkeypatch.setattr(machine.config, "in_box", lambda: True)
    assert CliRunner().invoke(cli.app, ["machine", "desktop"]).exit_code == 1
    monkeypatch.setattr(machine.config, "in_box", lambda: False)
    monkeypatch.setattr(machine, "BACKEND", SimpleNamespace(name="podman"))
    assert CliRunner().invoke(cli.app, ["machine", "desktop"]).exit_code == 1
