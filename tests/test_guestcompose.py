"""`[machine] compose = "vm"` — the compose client in the guest (docs/compose-in-vm-scope.md).

Hermetic: ssh is never run. The payload itself IS run, by this interpreter under ``-I`` (the
interpreter is the one binary the gate lets through), so the code the guest runs — the memfd
zip, the runner, foldyard's podman-compose on PyYAML's pure-Python parser — is exercised here
without a VM. The live check is the spike's (docs/compose-in-vm-scope.md, phase 0).
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from foldyard import config, confighash, guestcompose, stack
from foldyard.machine_backend import SshTarget

LIMA = SshTarget(user="dain", port=60022, identity="/h/.lima/_config/user")
GUEST_SOCK = "/run/user/501/podman/podman.sock"


class _Backend:
    name = "lima"

    def __init__(self, target):
        self._target = target

    def ssh_target(self, name):
        return self._target

    def guest_socket(self):
        return GUEST_SOCK


@pytest.fixture
def vm(monkeypatch):
    """Your computer, `[machine] compose = "vm"`, a lima backend with an ssh route."""
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setenv("MACHINE_COMPOSE", "vm")
    from foldyard import machine

    monkeypatch.setattr(machine, "MACHINE", "acme")
    monkeypatch.setattr(machine, "BACKEND", _Backend(LIMA))
    return machine


def _ctx(tmp_path: Path, provider: str = "/venv/bin/podman-compose") -> stack.Context:
    env = {
        "PATH": "/opt/homebrew/bin:/usr/bin",
        "HOME": "/Users/dain",
        "DOCKER_HOST": "unix:///nonexistent/podman.sock",
        "CONTAINER_HOST": "unix:///nonexistent/podman.sock",
        "PODMAN_COMPOSE_PROVIDER": provider,
        "PODMAN_PROJECT": "acme",
        "COMPOSE_PROJECT_NAME": "acme",
        "FOLDYARD_CHECKOUT": str(tmp_path),
        "APP_PORT": "3007",
    }
    compose = ["podman", "compose", "-f", str(tmp_path / "compose.yml")]
    return stack.Context(tmp_path, env, compose, "app", "acme", "")


# ── the switch ────────────────────────────────────────────────────────────────────────────


def test_the_switch_defaults_to_host_and_refuses_anything_else(monkeypatch):
    monkeypatch.delenv("MACHINE_COMPOSE", raising=False)
    monkeypatch.setattr(config, "_table", lambda name: {})
    assert config.machine_compose() == "host"
    monkeypatch.setenv("MACHINE_COMPOSE", " VM ")
    assert config.machine_compose() == "vm"
    monkeypatch.setenv("MACHINE_COMPOSE", "/usr/bin/docker-compose")
    with pytest.raises(SystemExit, match="never a program"):
        config.machine_compose()


def test_in_the_box_the_client_already_runs_in_the_vm(monkeypatch):
    monkeypatch.setenv("MACHINE_COMPOSE", "vm")
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert not guestcompose.wanted()
    monkeypatch.setattr(config, "in_box", lambda: False)
    assert guestcompose.wanted()


# ── the payload ───────────────────────────────────────────────────────────────────────────


def test_the_payload_is_deterministic_and_pure_python():
    guestcompose.payload.cache_clear()
    first = guestcompose.payload()
    guestcompose.payload.cache_clear()
    assert guestcompose.payload() == first
    names = zipfile.ZipFile(io.BytesIO(first)).namelist()
    assert "fy_compose_runner.py" in names
    assert "podman_compose.py" in names
    assert "yaml/__init__.py" in names
    assert "dotenv/__init__.py" in names
    assert all(n.endswith(".py") for n in names)  # no C extension built for THIS machine


def test_the_guest_env_drops_what_only_describes_your_computer(tmp_path):
    env = stack._host_passthrough() | _ctx(tmp_path).env
    sent = guestcompose.guest_env(env)
    for name in ("PATH", "HOME", "DOCKER_HOST", "CONTAINER_HOST", "PODMAN_COMPOSE_PROVIDER"):
        assert name not in sent
    assert sent["PODMAN_PROJECT"] == "acme"
    assert sent["APP_PORT"] == "3007"


# ── one call: the ssh route, the payload on stdin ─────────────────────────────────────────


def test_a_call_is_one_ssh_run_of_the_payload(vm, tmp_path, monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, b"web\n", b"")

    monkeypatch.setattr(guestcompose.subprocess, "run", run)
    ctx = _ctx(tmp_path)
    proc = guestcompose.compose(ctx, ["config", "--services"], timeout=7)
    assert (proc.returncode, proc.stdout) == (0, "web\n")
    [(cmd, kw)] = calls
    assert cmd[0] == "ssh" and "dain@127.0.0.1" in cmd
    assert cmd[-1] == "exec /usr/bin/python3 -I -"  # nothing from argv reaches the guest's shell
    assert kw["timeout"] == 7
    source = kw["input"].decode()
    assert "memfd_create" in source
    request = json.loads(_literal(source, "_r = json.loads(base64.b64decode("))
    assert request["op"] == "compose"
    assert request["argv"] == ["-f", str(tmp_path / "compose.yml"), "config", "--services"]
    assert request["socket"] == GUEST_SOCK
    assert request["cwd"] == str(tmp_path)
    assert "DOCKER_HOST" not in request["env"]


def _literal(source: str, prefix: str) -> str:
    import ast
    import base64

    line = next(x for x in source.splitlines() if x.startswith(prefix))
    return base64.b64decode(ast.literal_eval(line[len(prefix) : -2])).decode()


def test_no_ssh_route_is_said_never_run_here_instead(vm, tmp_path, monkeypatch):
    vm.BACKEND = _Backend(None)
    monkeypatch.setattr(guestcompose.subprocess, "run", _never)
    monkeypatch.setattr(stack, "_run", _never)
    proc = stack._compose_read(_ctx(tmp_path), ["ps", "--format", "json"])
    assert proc.returncode == guestcompose.NO_ROUTE
    assert "no ssh route" in proc.stderr


def test_the_docker_compose_provider_is_refused_in_the_vm(vm, tmp_path, monkeypatch):
    monkeypatch.setattr(guestcompose.subprocess, "run", _never)
    proc = guestcompose.compose(_ctx(tmp_path, provider="/usr/libexec/docker/docker-compose"), [])
    assert proc.returncode == guestcompose.WRONG_PROVIDER
    assert "docker-compose" in proc.stderr


def _never(*a, **kw):
    raise AssertionError(f"ran {a[0] if a else kw}")


# ── which calls move (phase 1: reads whose answer gates no read here) ─────────────────────


def test_ps_and_the_service_list_go_to_the_guest(vm, tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(
        guestcompose,
        "compose",
        lambda ctx, args, timeout=None: (
            asked.append(args) or subprocess.CompletedProcess(args, 0, "[]", "")
        ),
    )
    monkeypatch.setattr(stack, "_run", _never)
    ctx = _ctx(tmp_path)
    stack._running_services(ctx)
    stack._rendered_services(ctx)
    assert asked[0] == ["ps", "--format", "json"]
    assert asked[1][-2:] == ["config", "--services"]


def test_the_full_render_stays_here_while_up_does(vm, tmp_path, monkeypatch):
    """The path guard judges what THIS client will read; a render from the guest is the box's to
    shape. So `_render_config` runs here until `up` and the builds move too (phase 2)."""
    monkeypatch.setattr(guestcompose, "compose", _never)
    seen = []
    monkeypatch.setattr(
        stack,
        "_run",
        lambda cmd, **kw: (
            seen.append(cmd) or subprocess.CompletedProcess(cmd, 0, "services: {}", "")
        ),
    )
    assert stack._render_config(_ctx(tmp_path)) == {"services": {}}
    assert seen[0][:2] == ["podman", "compose"]


def test_off_nothing_goes_to_the_guest(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setenv("MACHINE_COMPOSE", "host")
    monkeypatch.setattr(guestcompose, "call", _never)
    monkeypatch.setattr(
        stack, "_run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "web\n", "")
    )
    assert stack._rendered_services(_ctx(tmp_path)) == {"web"}


def test_the_config_hash_is_asked_of_the_guest(vm, tmp_path, monkeypatch):
    monkeypatch.setattr(confighash.subprocess, "run", _never)
    monkeypatch.setattr(
        guestcompose,
        "config_hashes",
        lambda ctx, flags, timeout=None: subprocess.CompletedProcess([], 0, '{"web": "abc"}', ""),
    )
    assert confighash.desired(_ctx(tmp_path)) == ({"web": "abc"}, "")


# ── the payload, run for real (by this interpreter, as the guest's python3 would) ─────────

COMPOSE = """\
services:
  web:
    image: busybox
    environment:
      A: ${A_VAR:-unset}
    env_file: [web.env]
"""


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "compose.yml").write_text(COMPOSE)
    (tmp_path / "web.env").write_text("FROM_FILE=1\n")
    # podman-compose asks `podman version` before most commands; the guest has podman. This one
    # only answers that, so the payload runs with no engine at all.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "podman"
    fake.write_text(f"#!{sys.executable} -I\nprint('podman version 5.8.4')\n")
    fake.chmod(0o755)
    return tmp_path


def _run_payload(
    checkout: Path, op: str, argv: list[str], env: dict
) -> subprocess.CompletedProcess:
    request = {"op": op, "argv": argv, "env": env, "cwd": str(checkout), "socket": GUEST_SOCK}
    return subprocess.run(
        [sys.executable, "-I", "-"],
        input=guestcompose.bootstrap(request),
        capture_output=True,
        env={"PATH": str(checkout / "bin"), "HOME": str(checkout)},
        timeout=60,
    )


def test_the_payload_renders_from_the_request_env_alone(checkout):
    import yaml

    proc = _run_payload(
        checkout,
        "compose",
        ["-f", str(checkout / "compose.yml"), "config"],
        {"A_VAR": "from-the-request", "COMPOSE_PROJECT_NAME": "acme"},
    )
    assert proc.returncode == 0, proc.stderr.decode()
    web = yaml.safe_load(proc.stdout)["services"]["web"]
    assert web["environment"]["A"] == "from-the-request"


def test_the_payload_hashes_as_the_host_child_does(checkout, monkeypatch):
    """Parity: the guest's hash is the one `fy state` compared before — podman-compose's own,
    over the same render."""
    env = {"A_VAR": "v1", "COMPOSE_PROJECT_NAME": "acme"}
    argv = ["-f", str(checkout / "compose.yml")]
    proc = _run_payload(checkout, "hash", argv, env)
    assert proc.returncode == 0, proc.stderr.decode()
    guest = json.loads(proc.stdout)
    here = subprocess.run(
        [sys.executable, "-I", "-c", _HOST_HASH, *argv],
        capture_output=True,
        text=True,
        env={**env, "PATH": str(checkout / "bin"), "HOME": str(checkout)},
        cwd=checkout,
        timeout=60,
    )
    assert here.returncode == 0, here.stderr
    assert guest == json.loads(here.stdout)
    other = json.loads(_run_payload(checkout, "hash", argv, {**env, "A_VAR": "v2"}).stdout)
    assert other["web"] != guest["web"]  # an env-only change shows, as `up` would see it


_HOST_HASH = (
    "import json, sys; from foldyard import confighash; "
    "print(json.dumps(confighash._render_podman(sys.argv[1:])))"
)


def test_the_payload_never_runs_an_unknown_op(checkout):
    proc = _run_payload(checkout, "sh", ["-c", "id"], {})
    assert proc.returncode == 125
    assert b"unknown op" in proc.stderr
