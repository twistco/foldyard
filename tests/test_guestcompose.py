"""`[machine] compose = "vm"` — the compose client in the guest (docs/compose-in-vm-scope.md).

Hermetic: ssh is never run. The payload itself IS run, by this interpreter under ``-I`` (the
interpreter is the one binary the gate lets through), so the code the guest runs — the zip
imported from memory, the runner, foldyard's podman-compose on PyYAML's pure-Python parser — is exercised here
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


@pytest.fixture
def vm(monkeypatch):
    """Your computer, `[machine] compose = "vm"`, a lima backend with an ssh route."""
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setenv("MACHINE_COMPOSE", "vm")
    monkeypatch.setenv("PODMAN_SOCK_IN_VM", GUEST_SOCK)  # else it names this machine's uid
    from foldyard import machine

    monkeypatch.setattr(machine, "MACHINE", "acme")
    monkeypatch.setattr(machine, "BACKEND", _Backend(LIMA))
    return machine


def _ctx(tmp_path: Path) -> stack.Context:
    env = {
        "PATH": "/opt/homebrew/bin:/usr/bin",
        "HOME": "/Users/dain",
        "DOCKER_HOST": "unix:///nonexistent/podman.sock",
        "CONTAINER_HOST": "unix:///nonexistent/podman.sock",
        "PODMAN_COMPOSE_PROVIDER": "/venv/bin/podman-compose",
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
    # REGISTRY_AUTH_FILE is the clamp's empty file here (registrylogins): a path the guest lacks.
    env = stack._host_passthrough() | _ctx(tmp_path).env | {"REGISTRY_AUTH_FILE": "/h/no-logins"}
    sent = guestcompose.guest_env(env)
    for name in (
        "PATH", "HOME", "DOCKER_HOST", "CONTAINER_HOST", "PODMAN_COMPOSE_PROVIDER",
        "REGISTRY_AUTH_FILE",
    ):  # fmt: skip
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
    assert "meta_path" in source  # imported from memory: no file in the guest to swap
    request = _request_in(source)
    assert request["op"] == "compose"
    assert request["argv"] == ["-f", str(tmp_path / "compose.yml"), "config", "--services"]
    assert request["socket"] == GUEST_SOCK
    assert request["cwd"] == str(tmp_path)
    assert "DOCKER_HOST" not in request["env"]


def _request_in(source: str) -> dict:
    """The request in a bootstrap: the base64 literal on its last line."""
    import ast
    import base64

    last = source.strip().splitlines()[-1]
    literal = last.split("b64decode(", 1)[1].split(")", 1)[0]
    return json.loads(base64.b64decode(ast.literal_eval(literal)))


def test_no_ssh_route_is_said_never_run_here_instead(vm, tmp_path, monkeypatch):
    vm.BACKEND = _Backend(None)
    monkeypatch.setattr(guestcompose.subprocess, "run", _never)
    monkeypatch.setattr(stack, "_run", _never)
    proc = stack._compose_output(_ctx(tmp_path), ["ps", "--format", "json"])
    assert proc.returncode == guestcompose.NO_ROUTE
    assert "no ssh route" in proc.stderr


def _never(*a, **kw):
    raise AssertionError(f"ran {a[0] if a else kw}")


# ── which calls move ──────────────────────────────────────────────────────────────────────


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
    # Not a name compose finds by itself, so a run that dropped the `-f` list would fail.
    (tmp_path / "stack.yml").write_text(COMPOSE)
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
        ["-f", str(checkout / "stack.yml"), "config"],
        {"A_VAR": "from-the-request", "COMPOSE_PROJECT_NAME": "acme"},
    )
    assert proc.returncode == 0, proc.stderr.decode()
    web = yaml.safe_load(proc.stdout)["services"]["web"]
    assert web["environment"]["A"] == "from-the-request"


def test_the_payload_hashes_as_the_host_child_does(checkout, monkeypatch):
    """Parity: the guest's hash is the one `fy state` compared before — podman-compose's own,
    over the same render."""
    env = {"A_VAR": "v1", "COMPOSE_PROJECT_NAME": "acme"}
    argv = ["-f", str(checkout / "stack.yml")]
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


# ── phase 2: with it on, no compose client runs on your computer ──────────────────────────


@pytest.fixture
def no_host_client(vm, monkeypatch):
    """Your computer's compose client and engine, unreachable: any argv reaching ``stack``'s own
    subprocess fails the test by name. Stand-ins are set by each test."""
    monkeypatch.setattr(stack, "_run", _never)
    monkeypatch.setattr(stack.subprocess, "run", _never)
    return vm


def _answering(monkeypatch, stdout: str = "", rc: int = 0) -> list[tuple]:
    """``guestcompose.call`` answering ``stdout``, recording ``(op, argv, kwargs)``."""
    asked: list[tuple] = []

    def call(op, argv, env, cwd, **kw):
        asked.append((op, argv, kw))
        return subprocess.CompletedProcess(argv, rc, stdout, "")

    monkeypatch.setattr(guestcompose, "call", call)
    return asked


def test_the_render_comes_from_the_guest(no_host_client, tmp_path, monkeypatch):
    asked = _answering(monkeypatch, "services: {web: {image: x}}")
    assert stack._render_config(_ctx(tmp_path)) == {"services": {"web": {"image": "x"}}}
    assert asked[0][0] == "compose" and asked[0][1][-1] == "config"


def test_the_path_guard_stands_down_when_nothing_on_your_computer_reads(
    no_host_client, tmp_path, monkeypatch
):
    """No client here means nothing here to guard, and the guard itself reads box-writable files
    on your computer. So it isn't asked at all, rendered or raw."""
    from foldyard import composeguard

    monkeypatch.setattr(composeguard, "raw_problems", _never)
    monkeypatch.setattr(composeguard, "rendered_problems", _never)
    assert stack._escapes(_ctx(tmp_path)) == []


def test_off_the_path_guard_still_refuses(tmp_path, monkeypatch):
    from foldyard import composeguard

    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setenv("MACHINE_COMPOSE", "host")
    monkeypatch.setattr(composeguard, "raw_problems", lambda *a: ["env_file: /etc/passwd"])
    monkeypatch.setattr(composeguard, "rendered_problems", lambda *a, **k: [])
    problems = stack._escapes(_ctx(tmp_path), rendered={"services": {}})
    assert problems == ["env_file: /etc/passwd"]


def test_up_and_down_stream_from_the_guest(no_host_client, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(guestcompose, "stream", lambda ctx, args: seen.append(args) or 0)
    ctx = _ctx(tmp_path)
    assert stack._compose(ctx, ["up", "-d", "--no-build"], rendered={"services": {}}) == 0
    assert stack._compose(ctx, ["down", "--remove-orphans"]) == 0
    assert seen == [["up", "-d", "--no-build"], ["down", "--remove-orphans"]]


def test_a_failing_stream_is_the_verbs_exit_code(no_host_client, tmp_path, monkeypatch):
    monkeypatch.setattr(guestcompose, "stream", lambda ctx, args: 7)
    assert stack._compose(_ctx(tmp_path), ["logs", "-f"]) == 7


def test_the_posture_reconcile_streams_from_the_guest_through_emit(
    no_host_client, tmp_path, monkeypatch
):
    from foldyard import devmode

    def run_stream(cmd, on_line, **kw):
        assert kw["stdin"].startswith(b"import base64")  # the payload, never an argv
        assert cmd[0] == "ssh"
        on_line("Container web started")
        return 0

    monkeypatch.setattr(devmode, "run_stream", run_stream)
    lines: list[str] = []
    assert stack._compose_captured(_ctx(tmp_path), ["up", "-d"], lines.append) == 0
    assert lines[-1] == "Container web started"


def test_the_stack_build_runs_in_the_guest_and_logs_here(no_host_client, tmp_path, monkeypatch):
    """Render, then `podman build` + `podman tag`, all there: the client packs the context from
    what the VM sees. The build's output still lands in the log on your computer, and the build's
    secret (the proxy url) travels in the request, never an argv here."""
    rendered = {
        "services": {
            "api": {"build": {"context": str(tmp_path / "api"), "args": {"A": "1"}}},
            "worker": {"build": {"context": str(tmp_path / "api"), "args": {"A": "1"}}},
        }
    }
    monkeypatch.setattr(stack, "_image_id", lambda *a: None)
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path / "state")
    calls = []

    def call(op, argv, env, cwd, **kw):
        calls.append((op, argv, kw))
        if op == "compose":
            import yaml

            return subprocess.CompletedProcess(argv, 0, yaml.safe_dump(rendered), "")
        kw["stdout"].write(f"built {argv[0]}\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(guestcompose, "call", call)
    assert stack._podman_build(_ctx(tmp_path), "http://fy-build:s3cret@gw:4100") == 0
    ops = [(op, argv[0]) for op, argv, _ in calls]
    assert ops[0] == ("compose", "-f")
    assert ("podman", "build") in ops and ("podman", "tag") in ops
    build = next(argv for op, argv, _ in calls if op == "podman" and argv[0] == "build")
    assert "HTTP_PROXY=http://fy-build:s3cret@gw:4100" in build
    log = (tmp_path / "state" / "build-acme.log").read_text()
    assert "built build" in log and "s3cret" not in log  # the echoed line is redacted


def test_the_capability_heal_recreates_from_the_guest(no_host_client, tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(stack, "machine_down", lambda: None)
    monkeypatch.setattr(stack, "resolve", lambda **kw: ctx)
    monkeypatch.setattr(stack, "_running_services", lambda c, d: {"web"})
    monkeypatch.setattr(stack, "_crashed_services", lambda c, d: set())
    monkeypatch.setattr(stack, "_running_extra_profiles", lambda c, d, also=(): [])
    monkeypatch.setattr(stack, "_rendered_services", lambda c, e, d: {"web"})
    asked = _answering(monkeypatch)
    ok, summary = stack.recreate_services(["web"])
    assert ok, summary
    assert asked[0][1][-1] == "web"
    assert "--force-recreate" in asked[0][1]
    assert asked[0][2]["timeout"] is not None  # one deadline over every call, as on the host


def test_fy_shell_is_the_engines_exec_never_a_compose_client(vm, tmp_path, monkeypatch):
    """`exec` needs your terminal on stdin, where the payload goes. So the container is found by
    its labels, through the engine, and entered the way `fy box shell` enters a box: no compose
    file is parsed on your computer."""
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(stack, "stack_declared", lambda *a: None)
    monkeypatch.setattr(stack, "engine_reachable", lambda *a: True)
    monkeypatch.setattr(stack, "resolve", lambda: ctx)
    monkeypatch.setattr(guestcompose, "call", _never)
    ran = []

    def run(cmd, **kw):
        ran.append(cmd)
        if cmd[1] == "ps":
            return subprocess.CompletedProcess(cmd, 0, "abc123\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(stack.subprocess, "run", run)
    assert stack.shell() == 0
    assert "label=io.podman.compose.project=acme" in ran[0]
    assert "label=com.docker.compose.service=app" in ran[0]
    assert ran[1] == ["podman", "exec", "-it", "abc123", "bash"]
    assert "compose" not in ran[1]


def test_fy_shell_says_when_the_service_isnt_running(vm, tmp_path, monkeypatch, capfd):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(stack, "stack_declared", lambda *a: None)
    monkeypatch.setattr(stack, "engine_reachable", lambda *a: True)
    monkeypatch.setattr(stack, "resolve", lambda: ctx)
    monkeypatch.setattr(
        stack.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
    )
    assert stack.shell() == 1
    assert "app" in capfd.readouterr().err


def test_the_profile_read_parses_in_the_guest(no_host_client, tmp_path, monkeypatch):
    """`_service_profiles` used to parse each file on your computer: box-writable bytes, read
    through whatever link the box left there."""
    monkeypatch.setattr(Path, "read_text", _never)
    answer = {str(tmp_path / "compose.yml"): {"web": ["dev"], "db": []}}
    asked = _answering(monkeypatch, json.dumps(answer))
    assert stack._service_profiles(_ctx(tmp_path)) == {"web": ["dev"]}
    assert asked[0][0] == "services"


def test_the_overlay_read_parses_in_the_guest(no_host_client, tmp_path, monkeypatch):
    from foldyard import reconcile

    monkeypatch.setattr(Path, "read_text", _never)
    good, bad = tmp_path / "a.yml", tmp_path / "b.yml"
    asked = _answering(monkeypatch, json.dumps({str(good): {"web": []}, str(bad): None}))
    assert reconcile._overlay_services({"a": good, "b": bad}) == {"a": {"web"}, "b": None}
    assert asked[0][0] == "services"


def test_a_guest_that_cant_answer_the_overlay_read_is_unknown_never_empty(
    no_host_client, tmp_path, monkeypatch
):
    from foldyard import reconcile

    _answering(monkeypatch, "", rc=255)
    assert reconcile._overlay_services({"a": tmp_path / "a.yml"}) == {"a": None}


def test_the_ssh_client_gets_a_clean_environment(vm, tmp_path, monkeypatch):
    """The supervisor calls this, and its environment holds every host.env secret: ssh doesn't
    forward it by default, but a `SendEnv` in your ssh config would."""
    monkeypatch.setenv("GH_PEM_B64", "s3cret")
    seen = {}

    def run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(guestcompose.subprocess, "run", run)
    guestcompose.call("compose", ["ps"], {}, "/")
    assert "GH_PEM_B64" not in seen["env"]
    assert "PATH" in seen["env"]


def test_only_the_client_helpers_ever_touch_the_compose_argv():
    """The guard behind "no compose client on your computer": every use of ``ctx.compose`` as an
    argv is in a function that asks ``guestcompose`` first. A new one here would run the client on
    your computer with the path guard standing down."""
    import ast

    tree = ast.parse(Path(stack.__file__).read_text())
    users = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and node.attr == "compose"
        and isinstance(node.value, ast.Name)
        and node.value.id == "ctx"
    }
    # _compose_files lists the -f paths and _shown echoes; the others branch on wanted().
    assert users == {"_compose", "_compose_captured", "_compose_output", "_compose_files", "_shown"}


def test_the_guests_output_reaches_your_terminal_without_control_sequences():
    line = "\x1b]0;pwned\x07\x1b[31mred\x1b[0m\tok\x08 café"
    assert guestcompose.printable(line) == "]0;pwnedred\tok café"


# ── the runner, run for real ──────────────────────────────────────────────────────────────


def test_the_runner_waits_out_a_compose_file_the_vm_reads_as_missing(checkout):
    """A file your computer just replaced reads as missing from the VM for a moment (up to ~5 s on
    libkrun: ADR-0021's visibility record), and compose would call that a missing file."""
    import threading

    late = checkout / "late.yml"
    timer = threading.Timer(0.5, lambda: late.write_text(COMPOSE))
    timer.start()
    try:
        proc = _run_payload(
            checkout, "compose", ["-f", str(late), "config", "--services"], {"X": "1"}
        )
    finally:
        timer.join()
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.split() == [b"web"]


def test_the_services_op_parses_each_file_and_says_which_it_couldnt(checkout):
    good = checkout / "stack.yml"
    bad = checkout / "bad.yml"
    bad.write_text("services: [\n")
    proc = _run_payload(checkout, "services", [str(good), str(bad), "/nonexistent.yml"], {})
    assert proc.returncode == 0, proc.stderr.decode()
    assert json.loads(proc.stdout) == {
        str(good): {"web": []},
        str(bad): None,
        "/nonexistent.yml": None,
    }


def test_the_guests_client_dies_with_the_ssh_session(checkout):
    """`fy logs -f` interrupted, or a timeout that kills ssh: without a terminal nothing tells the
    guest, and a compose client would carry on (a `logs -f` until its next line, an `up` to the
    end). ``parent`` stands in for sshd's session."""
    import os
    import signal
    import time

    pidfile = checkout / "podman.pid"
    fake = checkout / "bin" / "podman"
    fake.write_text(
        f"#!{sys.executable} -I\n"
        "import os, time\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    request = {"op": "podman", "argv": ["logs"], "env": {}, "cwd": str(checkout), "socket": ""}
    (checkout / "payload.py").write_bytes(guestcompose.bootstrap(request))
    parent = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-I', {str(checkout / 'payload.py')!r}])\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", parent],
        env={"PATH": str(checkout / "bin"), "HOME": str(checkout)},
    )
    try:
        deadline = time.monotonic() + 20
        while not pidfile.exists() or not pidfile.read_text():
            assert time.monotonic() < deadline, "the runner never started podman"
            time.sleep(0.05)
        child = int(pidfile.read_text())
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(child, signal.SIGKILL)
        raise AssertionError("the guest's process outlived its session")
    finally:
        proc.kill()


# ── phase 3: the box image ────────────────────────────────────────────────────────────────


def _gated(monkeypatch):
    """The build gate out of the way (it's tested on its own): runs the build with no proxy."""
    from foldyard import buildgate

    monkeypatch.setattr(buildgate, "run", lambda build, what: build(None))


def test_a_projects_own_box_image_builds_in_the_guest(vm, tmp_path, monkeypatch):
    """Its context is the repo, which the box can write, so it is packed in the VM."""
    from foldyard import box

    _gated(monkeypatch)
    monkeypatch.setattr(
        config,
        "box_image",
        lambda: {"dockerfile": "box.Dockerfile", "tag": "acme-devbox:latest"},
    )
    monkeypatch.setattr(box, "_box_image_fingerprint", lambda main: "fp")
    monkeypatch.setattr(box.subprocess, "run", _never)
    seen = []

    def stream(argv, env, cwd):
        seen.append((argv, cwd))
        return 0

    monkeypatch.setattr(guestcompose, "stream_podman", stream)
    assert box._build_img("podman", tmp_path, {"A": "1"}) == 0
    [(argv, cwd)] = seen
    assert argv[0] == "build" and argv[-1] == str(tmp_path)
    assert argv[argv.index("-f") + 1] == "box.Dockerfile"
    assert cwd == str(tmp_path)


def test_the_packaged_box_image_stays_here(vm, tmp_path, monkeypatch):
    """Its context is foldyard's own files, which the VM can't see and the box can't write."""
    from foldyard import box

    _gated(monkeypatch)
    monkeypatch.setattr(box, "_box_image_fingerprint", lambda main: "fp")
    monkeypatch.setattr(guestcompose, "stream_podman", _never)
    ran = []
    monkeypatch.setattr(
        box.subprocess,
        "run",
        lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    assert box._build_img("podman", tmp_path, {}) == 0
    assert ran[0][:2] == ["podman", "build"]


# ── the relays, run for real: a payload bigger than a pipe's buffer, and hostile output ───


# Bigger than a pipe's 64 KB, which is what deadlocks a relay that writes before it reads. The
# program says something first, so a relay that only starts reading after the write would hang.
_NOISY = (
    "import sys\n"
    "print('\\x1b]0;pwned\\x07out line', flush=True)\n"
    "print('\\x1b[31merr line\\x1b[0m', file=sys.stderr, flush=True)\n"
    "sys.exit(3)\n#" + "x" * 300_000 + "\n"
).encode()


def test_run_relayed_keeps_the_streams_apart_and_cleans_them():
    out: list[str] = []
    err: list[str] = []
    rc = guestcompose.run_relayed([sys.executable, "-I", "-"], _NOISY, out.append, err.append)
    assert rc == 3
    assert out == ["]0;pwnedout line"]
    assert err == ["err line"]


def test_run_stream_feeds_a_large_stdin_and_merges_the_output():
    from foldyard import devmode

    lines: list[str] = []
    assert devmode.run_stream([sys.executable, "-I", "-"], lines.append, stdin=_NOISY) == 3
    assert "err line" in lines


def test_a_stream_with_no_route_says_so_and_runs_nothing(vm, tmp_path, monkeypatch, capfd):
    vm.BACKEND = _Backend(None)
    monkeypatch.setattr(guestcompose.subprocess, "Popen", _never)
    assert guestcompose.stream(_ctx(tmp_path), ["ps"]) == guestcompose.NO_ROUTE
    assert "no ssh route" in capfd.readouterr().err


def test_the_reconciles_stream_says_when_it_cant_run(vm, tmp_path, monkeypatch):
    vm.BACKEND = _Backend(None)
    lines: list[str] = []
    assert guestcompose.stream_to(_ctx(tmp_path), ["up"], lines.append) == guestcompose.NO_ROUTE
    assert "no ssh route" in lines[0]


def test_the_build_line_says_where_it_runs(vm, tmp_path, monkeypatch):
    assert stack._where().startswith("+ (in the VM)")
    monkeypatch.setenv("MACHINE_COMPOSE", "host")
    assert stack._where() == "+ "


def test_a_compose_file_the_vm_cant_read_fails_the_profile_read(vm, tmp_path, monkeypatch):
    """Callers are best-effort and catch this: quietly skipping the file would read as "no service
    has a profile", and the reconcile would then drop running profile-gated services."""
    f = str(tmp_path / "compose.yml")
    monkeypatch.setattr(guestcompose, "file_services", lambda files, timeout=None: {f: None})
    with pytest.raises(OSError, match="couldn't read"):
        stack._service_profiles(_ctx(tmp_path))


def test_a_failed_answer_is_unknown_however_it_reads(vm, tmp_path, monkeypatch):
    """The VM user's shell startup files are box-writable, so a failed call's stdout is anyone's:
    the exit code decides, not whether it happens to parse."""
    f = str(tmp_path / "compose.yml")
    _answering(monkeypatch, json.dumps({f: {"web": []}}), rc=255)
    assert guestcompose.file_services([f]) == {f: None}


def test_fy_ps_to_a_file_keeps_the_guests_stderr_out_of_it(vm, tmp_path, monkeypatch, capfd):
    program = b"import sys\nprint('row')\nprint('noise', file=sys.stderr)\n"
    monkeypatch.setattr(guestcompose, "_command", lambda *a: ([sys.executable, "-I", "-"], program))
    assert guestcompose.stream(_ctx(tmp_path), ["ps"]) == 0
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("row\n", "noise\n")
