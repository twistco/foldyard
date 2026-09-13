"""sandbox.py — the gVisor machine posture (`[machine].runtime = "gvisor"`): the guest-side
provisioning `machine ensure` runs (a second, runsc-default podman API service, no root), the
ssh:// engine endpoint `box up` creates the box through, and the fail-closed probe. The guest is
mocked (recorded ssh scripts); nothing here touches a VM."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from foldyard import config, sandbox
from foldyard.sandbox import SshTarget


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_TARGET = SshTarget(user="dain", port=60022, identity="/h/.lima/_config/user")


class FakeBackend:
    name = "lima"

    def __init__(self, target: SshTarget | None = _TARGET):
        self._target = target

    def ssh_target(self, name):
        return self._target


@pytest.fixture
def guest(monkeypatch):
    """A scripted guest: `have` is what `runsc --version` reports there, `arch` its uname -m."""
    state: dict[str, Any] = {
        "have": "",
        "arch": "aarch64",
        "scripts": [],
        "stdins": [],
        "probe": sandbox.RUNTIME,
    }

    def fake_ssh(target, script, stdin=None):
        state["scripts"].append(script)
        state["stdins"].append(stdin)
        if "uname -m" in script:
            return _Proc(0, f"{state['arch']}\n{state['have']}\n")
        return _Proc(0, "")

    monkeypatch.setattr(sandbox, "_ssh", fake_ssh)
    monkeypatch.setattr(
        sandbox,
        "_download",
        lambda arch: (_ for _ in ()).throw(AssertionError("no download expected")),
    )
    monkeypatch.setattr(sandbox, "_probe_runtime", lambda env: state["probe"])
    monkeypatch.setattr(config, "machine_runtime", lambda: "gvisor")
    return state


def test_wanted_follows_the_machine_runtime_key(monkeypatch):
    monkeypatch.setattr(config, "machine_runtime", lambda: "")
    assert sandbox.wanted() is False
    monkeypatch.setattr(config, "machine_runtime", lambda: "gvisor")
    assert sandbox.wanted() is True


def test_guest_socket_is_the_vm_users_runtime_dir(monkeypatch):
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 501)
    assert sandbox.guest_socket() == "/run/user/501/podman/podman-runsc.sock"


def test_engine_env_points_podman_remote_at_the_runsc_socket_over_the_backends_ssh(monkeypatch):
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 501)
    env = sandbox.engine_env(
        {"CONTAINER_HOST": "unix:///s", "DOCKER_HOST": "unix:///s", "X": "1"}, FakeBackend(), "acme"
    )
    uri = "ssh://dain@127.0.0.1:60022/run/user/501/podman/podman-runsc.sock"
    assert env["CONTAINER_HOST"] == uri and env["DOCKER_HOST"] == uri
    assert env["CONTAINER_SSHKEY"] == "/h/.lima/_config/user"
    assert env["X"] == "1"  # the rest of the stack env rides along


def test_engine_env_refuses_a_backend_without_ssh(monkeypatch):
    with pytest.raises(SystemExit):
        sandbox.engine_env({}, FakeBackend(target=None), "acme")


def test_ensure_installs_runsc_when_absent_then_provisions_and_probes(guest, monkeypatch, tmp_path):
    blob = tmp_path / "runsc"
    blob.write_bytes(b"ELF...")
    seen = {}
    monkeypatch.setattr(
        sandbox, "_download", lambda arch: (seen.setdefault("arch", arch) and blob) or blob
    )
    sandbox.ensure(FakeBackend(), "acme")
    scripts = guest["scripts"]
    assert seen["arch"] == "aarch64"
    # the binary is streamed in over ssh stdin and moved into place atomically
    push = [s for s, i in zip(scripts, guest["stdins"], strict=True) if i == b"ELF..."]
    assert push and "~/.local/bin/runsc" in push[0] and "mv " in push[0]
    # then the guest files: wrapper WITHOUT --allow-flag-override, the engine-wide runtime name
    # as a drop-in (never clobbering an existing containers.conf), the override, the user unit
    prov = scripts[-1]
    exec_lines = [ln for ln in prov.splitlines() if ln.startswith("exec ")]
    # $HOME is expanded when the wrapper is WRITTEN (an unquoted heredoc): conmon runs the runtime
    # with no HOME, so the wrapper must carry the absolute path; "$@" is escaped to survive.
    assert exec_lines == ['exec "$HOME/.local/bin/runsc" --ignore-cgroups --host-uds=all "\\$@"']
    assert "<<'EOF'\n#!/bin/sh" not in prov
    assert "containers.conf.d/50-foldyard-runsc.conf" in prov and "[engine.runtimes]" in prov
    assert "runsc.conf" in prov and 'runtime = "runsc-fy"' in prov
    assert "label = false" in prov  # in-box `podman run` through the socket: no SELinux label
    assert "podman-runsc.service" in prov and "CONTAINERS_CONF_OVERRIDE" in prov
    assert "systemctl --user" in prov and "enable --now podman-runsc.service" in prov


def test_ensure_skips_the_download_when_the_pinned_release_is_there(guest):
    guest["have"] = f"runsc version release-{sandbox.GVISOR_RELEASE}"
    sandbox.ensure(FakeBackend(), "acme")  # _download would raise
    assert all(i is None for i in guest["stdins"])


def test_ensure_replaces_a_runsc_of_another_release(guest, monkeypatch, tmp_path):
    guest["have"] = "runsc version release-20250101.0"
    blob = tmp_path / "runsc"
    blob.write_bytes(b"new")
    monkeypatch.setattr(sandbox, "_download", lambda arch: blob)
    sandbox.ensure(FakeBackend(), "acme")
    assert b"new" in guest["stdins"]


def test_ensure_fails_closed_when_the_socket_does_not_default_to_runsc(guest):
    guest["have"] = f"runsc version release-{sandbox.GVISOR_RELEASE}"
    guest["probe"] = "crun"
    with pytest.raises(SystemExit) as e:
        sandbox.ensure(FakeBackend(), "acme")
    assert "crun" in str(e.value) and "runsc-fy" in str(e.value)


def test_ensure_is_a_noop_without_the_posture(guest, monkeypatch):
    monkeypatch.setattr(config, "machine_runtime", lambda: "")
    sandbox.ensure(FakeBackend(), "acme")
    assert guest["scripts"] == []


def test_ssh_argv_is_batch_and_loopback_scoped():
    argv = sandbox._ssh_argv(SshTarget(user="core", port=50501, identity="/k"))
    assert argv[0] == "ssh" and argv[-1] == "core@127.0.0.1"
    assert "-p" in argv and argv[argv.index("-p") + 1] == "50501"
    assert "-i" in argv and argv[argv.index("-i") + 1] == "/k"
    assert "BatchMode=yes" in " ".join(argv)


def test_release_url_is_pinned_per_arch():
    assert sandbox.release_url("x86_64").endswith(f"/release/{sandbox.GVISOR_RELEASE}/x86_64/runsc")
    assert sandbox.release_url("aarch64").endswith("/aarch64/runsc")


def test_download_verifies_the_sha512_and_caches(monkeypatch, tmp_path):
    import hashlib

    body = b"runsc-bytes"
    digest = hashlib.sha512(body).hexdigest()
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return body if url.endswith("/runsc") else f"{digest}  runsc\n".encode()

    monkeypatch.setattr(sandbox, "_fetch", fake_fetch)
    monkeypatch.setattr(sandbox, "_cache_dir", lambda: tmp_path)
    path = sandbox._download("aarch64")
    assert path.read_bytes() == body and len(fetched) == 2
    assert sandbox._download("aarch64") == path and len(fetched) == 2  # cached: no refetch


def test_download_refuses_a_bad_checksum(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sandbox, "_fetch", lambda url: b"x" if url.endswith("/runsc") else b"0" * 128 + b"  runsc\n"
    )
    monkeypatch.setattr(sandbox, "_cache_dir", lambda: tmp_path)
    with pytest.raises(SystemExit):
        sandbox._download("x86_64")
    assert not list(tmp_path.iterdir())


def test_probe_runtime_asks_podman_info_through_the_given_env(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw.get("env")
        return _Proc(0, "runsc-fy\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert sandbox._probe_runtime({"CONTAINER_HOST": "ssh://x"}) == "runsc-fy"
    assert seen["cmd"][:2] == ["podman", "info"] and seen["env"]["CONTAINER_HOST"] == "ssh://x"
