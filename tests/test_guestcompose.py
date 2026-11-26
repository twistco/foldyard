"""`[machine] compose = "vm"` — the compose client in the guest (docs/compose-in-vm-scope.md).

Hermetic: ssh is never run. The payload itself IS run, by this interpreter under ``-I`` (the
interpreter is the one binary the gate lets through), so the code the guest runs — the zip
imported from memory, the runner, foldyard's podman-compose on PyYAML's pure-Python parser — is exercised here
without a VM. The live check is the spike's (docs/compose-in-vm-scope.md, phase 0).
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
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
    monkeypatch.setattr(guestcompose, "stream_here", _never)
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
    monkeypatch.setattr(guestcompose, "stream", lambda ctx, args, **_: seen.append(args) or 0)
    ctx = _ctx(tmp_path)
    assert stack._compose(ctx, ["up", "-d", "--no-build"], rendered={"services": {}}) == 0
    assert stack._compose(ctx, ["down", "--remove-orphans"]) == 0
    assert seen == [["up", "-d", "--no-build"], ["down", "--remove-orphans"]]


def test_what_the_guests_compose_prints_is_restyled_too(no_host_client, tmp_path, monkeypatch):
    styles = []
    monkeypatch.setattr(guestcompose, "stream", lambda ctx, args, **kw: styles.append(kw) or 0)
    assert stack._compose(_ctx(tmp_path), ["logs", "-f", "--timestamps"], stamps=True) == 0
    [kw] = styles
    assert kw["style"]("[web] | 2026-11-26T10:00:00Z hi", True, False).endswith(" [web] | hi")


def test_stream_here_restyles_each_cleaned_line_for_a_terminal_only(monkeypatch):
    from foldyard import term

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    prog = "print('[web]   | \\x1b[32mERROR\\x1b[0m boom', flush=True)"
    cmd = [sys.executable, "-I", "-c", prog]
    style = guestcompose.compose_style()
    for out, want in (
        (
            _Terminal(),
            f"\x1b[{term.service_color('web')}m[web]   |\x1b[0m \x1b[31mERROR\x1b[0m boom\n",
        ),
        (io.StringIO(), "[web]   | ERROR boom\n"),  # a file or a pipe: no colour
    ):
        monkeypatch.setattr(sys, "stdout", out)
        assert guestcompose.stream_here(cmd, dict(os.environ), style=style) == 0
        assert out.getvalue() == want


def test_a_pipes_sink_that_fails_is_raised_once_its_writers_are_done():
    # `fy up -v 2>&1 | head`: the sink fails as your end of the pipe goes; that is said (as the
    # relay says it: raised once the process has ended), never lost to an exit code alone.
    def sink(line):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    with pytest.raises(BrokenPipeError):
        with guestcompose.pipe_to(sink) as out:
            out.write("first\n")
            subprocess.run(
                [sys.executable, "-c", "print('second')"], stdout=out, stderr=subprocess.DEVNULL
            )


def test_a_failing_stream_is_the_verbs_exit_code(no_host_client, tmp_path, monkeypatch):
    monkeypatch.setattr(guestcompose, "stream", lambda ctx, args, **_: 7)
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


def test_the_runner_waits_out_a_long_form_file_flag_too(checkout):
    import threading

    late = checkout / "late.yml"
    timer = threading.Timer(0.5, lambda: late.write_text(COMPOSE))
    timer.start()
    try:
        proc = _run_payload(
            checkout, "compose", ["--file", str(late), "config", "--services"], {"X": "1"}
        )
    finally:
        timer.join()
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.split() == [b"web"]


def _runner():
    """The guest runner, imported here from its file (the guest imports it from the zip)."""
    import importlib.util

    path = Path(guestcompose.__file__).parent / "assets" / "compose" / "runner.py"
    spec = importlib.util.spec_from_file_location("fy_compose_runner_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_runner_waits_only_for_the_compose_files(checkout, monkeypatch):
    # `fy logs` sends `-f <file>… logs -f --tail 50`: the `-f` after the command is "follow", and
    # waiting for a file called `--tail` held every followed log back by the whole settle time.
    runner = _runner()

    class Waited(Exception):
        pass

    def sleep(_seconds: float) -> None:
        raise Waited

    # Its own `time`, not the shared module's `sleep`: the runner was imported for this test alone.
    clock = types.SimpleNamespace(monotonic=time.monotonic, sleep=sleep)
    monkeypatch.setattr(runner, "time", clock)
    runner._settle(["-f", str(checkout / "stack.yml"), "logs", "-f", "--tail", "50", "web"])
    with pytest.raises(Waited):  # a compose file that isn't there yet is still waited for
        runner._settle(["-f", str(checkout / "stack.yml"), "--file", str(checkout / "late.yml")])


def test_the_guests_podman_carries_no_registry_logins(checkout):
    """Parity with the clamp on your computer: a `podman login` in the VM would otherwise ride
    along with every guest build. The request can't widen it either."""
    (checkout / "bin" / "podman").write_text(
        f"#!{sys.executable} -I\nimport os; print(os.environ.get('REGISTRY_AUTH_FILE'))\n"
    )
    proc = _run_payload(checkout, "podman", ["info"], {"REGISTRY_AUTH_FILE": "/home/me/auth.json"})
    assert proc.returncode == 0, proc.stderr.decode()
    pinned = proc.stdout.decode().strip()
    assert pinned != "/home/me/auth.json"
    assert not Path(pinned).exists()
    assert not os.access(Path(pinned).parent, os.F_OK)  # nowhere the VM user could create it


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


def test_the_guests_client_hands_on_each_line_as_it_comes(checkout):
    """`fy logs -f` with compose in the VM: podman-compose print()s each log line, and the runner's
    stdout is the ssh pipe, where Python block-buffers unless told otherwise (``-I`` ignores a
    PYTHONUNBUFFERED in the env, and the request's env is set after start anyway). The second line
    only comes once the first has arrived here."""
    seen = checkout / "seen"
    (checkout / "bin" / "podman").write_text(
        f"#!{sys.executable} -I\n"
        "import os, sys, time\n"
        "if sys.argv[1] != 'logs':\n"  # podman-compose asks `podman --version` first
        "    print('podman version 5.8.4')\n"
        "    sys.exit(0)\n"
        "print('first', flush=True)\n"
        "deadline = time.monotonic() + 10\n"
        f"while not os.path.exists({str(seen)!r}):\n"
        "    if time.monotonic() > deadline:\n"
        "        sys.exit(0)\n"
        "    time.sleep(0.01)\n"
        "print('second', flush=True)\n"
    )
    argv = ["-f", str(checkout / "stack.yml"), "logs", "-f", "--tail", "50", "web"]  # `fy logs`'s
    request = {"op": "compose", "argv": argv, "env": {}, "cwd": str(checkout), "socket": ""}
    proc = subprocess.Popen(
        [sys.executable, "-I", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": str(checkout / "bin"), "HOME": str(checkout)},
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(guestcompose.bootstrap(request))
    proc.stdin.close()
    lines = []
    try:
        for raw in proc.stdout:
            lines.append(guestcompose.printable(raw.decode().rstrip("\n")))
            seen.touch()
    finally:
        assert proc.wait(timeout=30) == 0
    assert [line.split("|", 1)[-1].strip() for line in lines] == ["first", "second"], lines


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

    def stream(argv, env, cwd, *, to=None):
        assert to is not None  # into the build's log, not your terminal
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
    monkeypatch.setattr(box.subprocess, "run", _never)
    ran = []
    monkeypatch.setattr(
        guestcompose, "stream_here", lambda cmd, env, cwd, **_: ran.append(cmd) or 0
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


# ── the same relay for what runs on your computer: a compose client, a build, a container ──


def _sinks(monkeypatch) -> dict[str, list[str]]:
    """Where :func:`guestcompose.stream_here` prints, recorded a line at a time."""
    got: dict[str, list[str]] = {"stdout": [], "stderr": []}
    monkeypatch.setattr(guestcompose, "_print_to", lambda name, style=None: got[name].append)
    return got


def test_stream_here_keeps_the_streams_apart_and_cleans_them(capsys):
    prog = _NOISY.decode().split("#", 1)[0]
    rc = guestcompose.stream_here([sys.executable, "-I", "-c", prog], dict(os.environ))
    assert rc == 3
    out, err = capsys.readouterr()
    assert out == "]0;pwnedout line\n"
    assert err == "err line\n"


def test_stream_here_gives_its_process_no_terminal(monkeypatch, tmp_path):
    # Its stdin is empty, never yours: a verb that wants your terminal isn't sent here.
    got = _sinks(monkeypatch)
    prog = "import os, sys\nprint(repr(sys.stdin.read()), os.getcwd())\n"
    yours, typed = os.pipe()  # this process's stdin, with something typed on it
    os.write(typed, b"yours\n")
    os.close(typed)
    saved = os.dup(0)
    os.dup2(yours, 0)
    try:
        rc = guestcompose.stream_here([sys.executable, "-I", "-c", prog], {}, str(tmp_path))
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(yours)
    assert rc == 0
    assert got["stdout"] == [f"'' {os.path.realpath(tmp_path)}"]


_UNFLUSHED = (
    "import os, sys, time\n"
    "print('first')\n"  # no flush: a pipe block-buffers it unless PYTHONUNBUFFERED says not to
    "deadline = time.monotonic() + 10\n"
    "while not os.path.exists(sys.argv[1]):\n"
    "    if time.monotonic() > deadline:\n"
    "        sys.exit(1)\n"
    "    time.sleep(0.01)\n"
)


def test_stream_here_relays_an_unbuffered_python_clients_lines_as_they_come(monkeypatch, tmp_path):
    # podman-compose prints `logs -f` with a plain print(): through a pipe, a Python process holds
    # that back until 8 KB pile up unless its env says not to (the compose env does:
    # `stack.resolve`), and the relay must hand each line on as it comes.
    seen = tmp_path / "seen"
    lines: list[str] = []

    def sink(line: str) -> None:
        lines.append(line)
        seen.touch()

    monkeypatch.setattr(guestcompose, "_print_to", lambda name, style=None: sink)
    cmd = [sys.executable, "-s", "-c", _UNFLUSHED, str(seen)]
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1"}
    assert guestcompose.stream_here(cmd, env) == 0
    assert lines == ["first"]


def test_stream_here_runs_with_exactly_the_env_it_is_given(monkeypatch):
    # The compose client interpolates its compose files from this env: a name added here only
    # (`${PYTHONUNBUFFERED}`) would render differently from the captured calls and the config-hash
    # check, which run with the same ctx.env, and read as drift.
    got = _sinks(monkeypatch)
    prog = "import os\nprint(os.environ.get('PYTHONUNBUFFERED'), os.environ.get('A'))\n"
    assert guestcompose.stream_here([sys.executable, "-I", "-c", prog], {"A": "1"}) == 0
    assert got["stdout"] == ["None 1"]


# A child that writes far more than a pipe holds, then says how it ended.
_FLOOD = "import sys\nfor i in range(200000):\n    print('line', i)\n"


def _spawn_harness(code: str, tmp_path: Path) -> tuple[subprocess.Popen, Path]:
    """``code`` run by this interpreter as foldyard's own process would be (its stderr to a file),
    in a session of its own, so a test can be the terminal: read its stdout, close it, Ctrl-C it."""
    err = tmp_path / "stderr"
    with err.open("wb") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=sink,
            start_new_session=True,
        )
    return proc, err


def _ended(proc: subprocess.Popen, timeout: float = 30) -> int:
    """``proc``'s exit code, or the test fails (never hangs) when it doesn't end in time."""
    import signal

    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        pytest.fail(f"still running after {timeout}s")


def test_a_reader_that_goes_away_ends_the_relay_instead_of_hanging_it(tmp_path):
    # `fy logs | head -1`: once your end of the pipe is gone, the child's next write fails as it
    # would have when it held the pipe itself, and foldyard ends with a BrokenPipeError the CLI
    # turns into a quiet exit. Not a relay thread dying while the child blocks on a full pipe.
    code = (
        "import os, sys\n"
        "from foldyard import guestcompose\n"
        f"child = [sys.executable, '-c', {_FLOOD!r}]\n"
        "try:\n"
        "    guestcompose.stream_here(child, dict(os.environ))\n"
        "except BrokenPipeError:\n"
        "    os._exit(32)\n"
        "os._exit(0)\n"
    )
    proc, err = _spawn_harness(code, tmp_path)
    assert proc.stdout is not None
    assert proc.stdout.readline() == b"line 0\n"
    proc.stdout.close()  # what `head -1` does next
    assert _ended(proc) == 32
    assert "Exception in thread" not in err.read_text(errors="replace")


def test_the_guests_relay_stops_too_when_its_sink_fails():
    import threading

    spawned: list[subprocess.Popen] = []

    def gone(_line: str) -> None:
        raise BrokenPipeError

    raised: list[BaseException] = []

    def run() -> None:
        try:
            guestcompose.run_relayed(
                [sys.executable, "-I", "-"], _FLOOD.encode(), gone, print, spawned.append
            )
        except BaseException as e:
            raised.append(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(30)
    if t.is_alive():
        spawned[0].kill()
        pytest.fail("the relay hung once its sink failed")
    assert [type(e) for e in raised] == [BrokenPipeError]


def test_a_relayed_line_is_not_painted_as_foldyards_own(monkeypatch):
    # `term.ColorStream` colours ✓/✗/▶ in foldyard's own output: a box's "✓ verify passed" must not
    # come out in foldyard's green, as if foldyard said it.
    from foldyard import term

    raw = io.StringIO()
    monkeypatch.setattr(sys, "stderr", term.ColorStream(raw))
    guestcompose._print_to("stderr")("✓ verify passed")
    assert raw.getvalue() == "✓ verify passed\n"


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


_TWENTY_THOUSAND = "import sys\nfor i in range(20000):\n    print('line', i)\n"


def test_a_reader_that_stalls_still_gets_every_line(tmp_path):
    # `fy up 2>&1 | less`, still on page one: the relay is blocked writing to you, not waiting on
    # the child, so however long that lasts, nothing it has yet to hand on is dropped.
    code = (
        "import os, sys\n"
        "from foldyard import guestcompose\n"
        f"child = [sys.executable, '-c', {_TWENTY_THOUSAND!r}]\n"
        "sys.exit(guestcompose.stream_here(child, dict(os.environ)))\n"
    )
    proc, err = _spawn_harness(code, tmp_path)
    assert proc.stdout is not None
    first = proc.stdout.read(100_000)
    time.sleep(6)  # longer than any patience the relay has for a pipe that stays open
    rest = proc.stdout.read()
    assert _ended(proc) == 0, err.read_text(errors="replace")
    assert (first + rest).count(b"\n") == 20000


# A child that leaves a grandchild holding its stdout and stderr, says the grandchild's pid, exits.
_LEAVES_A_GRANDCHILD = (
    "import subprocess, sys\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(g.pid, flush=True)\n"
)


def test_a_grandchild_holding_the_pipe_doesnt_keep_the_relay_waiting(monkeypatch):
    import threading

    monkeypatch.setattr(guestcompose, "_DRAIN_PATIENCE", 0.5)
    got = _sinks(monkeypatch)
    finished: list[int] = []
    cmd = [sys.executable, "-I", "-c", _LEAVES_A_GRANDCHILD]
    t = threading.Thread(target=lambda: finished.append(guestcompose.stream_here(cmd, {})))
    t.daemon = True
    t.start()
    t.join(20)
    try:
        assert finished == [0], "the relay waited on a pipe only a grandchild held"
    finally:
        for pid in got["stdout"]:
            os.kill(int(pid), signal.SIGKILL)


def _chatty_grandchild(stream: str) -> str:
    """A child that leaves a grandchild (in a session of its own) writing to ``stream`` nonstop,
    says ``pid <grandchild>`` and exits."""
    nonstop = f"import sys\nwhile True:\n    sys.{stream}.write('x' * 100 + '\\n')\n"
    return (
        "import subprocess, sys\n"
        f"g = subprocess.Popen([sys.executable, '-c', {nonstop!r}], start_new_session=True)\n"
        "print('pid', g.pid, flush=True)\n"
    )


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_a_grandchild_that_never_stops_writing_doesnt_keep_the_relay_going(monkeypatch, stream):
    # Once the child has exited, only what was already in the pipe can be its own: a grandchild
    # still writing to the pipe it inherited can't keep foldyard relaying it for good.
    import threading

    monkeypatch.setattr(guestcompose, "_DRAIN_PATIENCE", 2.0)
    pids: list[int] = []

    def sink(line: str) -> None:
        if line.startswith("pid "):
            pids.append(int(line.split()[1]))

    monkeypatch.setattr(guestcompose, "_print_to", lambda name, style=None: sink)
    finished: list[int] = []
    cmd = [sys.executable, "-I", "-c", _chatty_grandchild(stream)]
    t = threading.Thread(target=lambda: finished.append(guestcompose.stream_here(cmd, {})))
    t.daemon = True
    t.start()
    t.join(30)
    try:
        assert finished == [0], "the relay kept relaying a grandchild after its process exited"
    finally:
        for pid in pids:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGKILL)


def test_show_prints_the_boxs_line_cleaned_and_unpainted(monkeypatch):
    # The one sink for relayed text that isn't a stream: a build's tail, an engine's stderr.
    from foldyard import term

    raw = io.StringIO()
    monkeypatch.setattr(sys, "stderr", term.ColorStream(raw))
    guestcompose.show("\x1b]52;c;ZXZpbA==\x07✓ verify passed")
    assert raw.getvalue() == "]52;c;ZXZpbA==✓ verify passed\n"


def test_a_line_your_terminal_cant_encode_is_shown_escaped_not_fatal(monkeypatch):
    # A stdout that isn't UTF-8 (LANG=C): the relay's print would raise, and stop relaying.
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="ascii")
    monkeypatch.setattr(sys, "stdout", out)
    guestcompose._print_to("stdout")("caf\u00e9 ok")
    out.flush()
    assert raw.getvalue() == b"caf\\xe9 ok\n"


def test_a_line_with_no_end_is_relayed_in_bounded_pieces():
    # A box can print gigabytes with no newline: the relay must not hold it all to find one.
    pieces: list[int] = []
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys\nsys.stdout.write('x' * 1_000_000)\n"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert guestcompose.relay(proc, lambda line: pieces.append(len(line)), print) == 0
    assert sum(pieces) == 1_000_000
    assert max(pieces) <= guestcompose._LINE_CAP


# A child that is busy writing when the interrupt lands, and ignores it (as a client mid-write
# might), so the relay has to stop it and its threads are still relaying when it does.
_BUSY = (
    "import signal\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "while True:\n"
    "    print('x' * 200, flush=True)\n"
)


@pytest.mark.parametrize("round_", range(5))
def test_ctrl_c_during_busy_output_ends_as_an_interrupt_not_an_abort(tmp_path, round_):
    # The relay threads were still printing as the interpreter shut down, and Python aborted
    # ("could not acquire lock for <stdout> at interpreter shutdown"): SIGABRT, not the interrupt.
    import signal

    code = (
        "import os, sys\n"
        "from foldyard import guestcompose\n"
        f"child = [sys.executable, '-c', {_BUSY!r}]\n"
        "try:\n"
        "    guestcompose.stream_here(child, dict(os.environ))\n"
        "except KeyboardInterrupt:\n"
        "    sys.exit(130)\n"
    )
    proc, err = _spawn_harness(code, tmp_path)
    assert proc.stdout is not None
    assert proc.stdout.readline()  # it is relaying
    os.killpg(proc.pid, signal.SIGINT)  # the terminal's Ctrl-C: the whole foreground group

    import threading

    drained = threading.Thread(target=proc.stdout.read, daemon=True)  # a terminal keeps reading
    drained.start()
    rc = _ended(proc)
    said = err.read_text(errors="replace")
    assert "Fatal Python error" not in said, said
    assert rc == 130, said


# A child that says its pid, then sleeps: Python's own Ctrl-C, a KeyboardInterrupt traceback on its
# stderr, is what podman-compose prints when the terminal's Ctrl-C reaches it.
_SAYS_ITS_PID = "import os, time\nprint('pid', os.getpid(), flush=True)\ntime.sleep(60)\n"


def test_ctrl_c_reaches_foldyard_alone_and_ends_the_relayed_process_quietly(tmp_path):
    # `fy logs`, Ctrl-C: the relayed process isn't the terminal's to interrupt, so what it would
    # say about an interrupt (podman-compose's traceback) never reaches you. foldyard stops it, and
    # it is gone (no orphan) by the time foldyard exits, promptly and with the interrupt's code.
    code = (
        "import os, sys\n"
        "from foldyard import guestcompose\n"
        f"child = [sys.executable, '-c', {_SAYS_ITS_PID!r}]\n"
        "try:\n"
        "    guestcompose.stream_here(child, dict(os.environ))\n"
        "except KeyboardInterrupt:\n"
        "    sys.exit(130)\n"
    )
    proc, err = _spawn_harness(code, tmp_path)
    assert proc.stdout is not None
    said = proc.stdout.readline().split()
    assert said[:1] == [b"pid"], said
    child = int(said[1])
    started = time.monotonic()
    os.killpg(proc.pid, signal.SIGINT)  # the terminal's Ctrl-C: the whole foreground group
    rc = _ended(proc)
    elapsed = time.monotonic() - started
    stderr = err.read_text(errors="replace")
    assert rc == 130, stderr
    assert "Traceback" not in stderr and "KeyboardInterrupt" not in stderr, stderr
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    assert elapsed < 5, f"took {elapsed:.1f}s to end"


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT])
def test_a_signal_that_ends_foldyard_ends_the_relayed_process_too(tmp_path, sig):
    # The terminal closed (SIGHUP), `kill %1` or `timeout` (SIGTERM), Ctrl-\ (SIGQUIT): the relayed
    # process is in a group of its own, so nothing else would reach it, and it would run on.
    code = (
        "import os, sys\n"
        "from foldyard import guestcompose\n"
        f"child = [sys.executable, '-c', {_SAYS_ITS_PID!r}]\n"
        "guestcompose.stream_here(child, dict(os.environ))\n"
    )
    proc, err = _spawn_harness(code, tmp_path)
    assert proc.stdout is not None
    said = proc.stdout.readline().split()
    assert said[:1] == [b"pid"], said
    child = int(said[1])
    os.kill(proc.pid, sig)  # foldyard alone: the child would get nothing from it otherwise
    rc = _ended(proc)
    assert rc == 128 + sig, err.read_text(errors="replace")
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def _main_interrupted_after(seconds: float) -> threading.Timer:
    """A real SIGINT to the main thread in ``seconds``, as the terminal's Ctrl-C would land."""
    main = threading.main_thread().ident
    assert main is not None
    timer = threading.Timer(seconds, signal.pthread_kill, (main, signal.SIGINT))
    timer.daemon = True
    timer.start()
    return timer


# A child that leaves a grandchild in its group holding its pipes (it marks a file when SIGTERM
# reaches it), says the grandchild's pid and exits: the relay is then draining.
def _leaves_a_marking_grandchild(mark: Path) -> str:
    grandchild = (
        "import signal, sys, time\n"
        f"signal.signal(signal.SIGTERM, lambda *_: (open({str(mark)!r}, 'w').close(), sys.exit(0)))\n"
        "time.sleep(60)\n"
    )
    return (
        "import subprocess, sys\n"
        f"g = subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "print(g.pid, flush=True)\n"
    )


def test_an_interrupt_while_the_relay_drains_stops_the_group(monkeypatch, tmp_path):
    # The process has exited, but what it started is still in its group, holding the pipe.
    monkeypatch.setattr(guestcompose, "_DRAIN_PATIENCE", 30.0)
    got = _sinks(monkeypatch)
    mark = tmp_path / "stopped"
    cmd = [sys.executable, "-I", "-c", _leaves_a_marking_grandchild(mark)]
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    timer = _main_interrupted_after(1.5)
    try:
        with pytest.raises(KeyboardInterrupt):
            guestcompose.stream_here(cmd, {})
    finally:
        timer.cancel()
        signal.signal(signal.SIGINT, previous)
        for pid in got["stdout"]:
            with contextlib.suppress(OSError):
                os.kill(int(pid), signal.SIGKILL)
    deadline = time.monotonic() + 10
    while not mark.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert mark.exists(), "the grandchild left in the group was never stopped"


def test_an_interrupt_in_on_spawn_still_stops_the_process():
    spawned: list[subprocess.Popen] = []

    def interrupted(proc: subprocess.Popen) -> None:
        spawned.append(proc)
        raise KeyboardInterrupt

    cmd = [sys.executable, "-I", "-c", _SAYS_ITS_PID]
    with pytest.raises(KeyboardInterrupt):
        guestcompose.stream_here(cmd, {}, on_spawn=interrupted)
    assert spawned[0].poll() is not None


def _sleeper_in_its_own_group(on_term: str) -> subprocess.Popen:
    """A process in a group of its own whose SIGTERM handler is ``on_term``."""
    prog = (
        "import signal, sys, time\n"
        f"signal.signal(signal.SIGTERM, {on_term})\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", prog], stdout=subprocess.PIPE, process_group=0
    )
    assert proc.stdout is not None
    assert proc.stdout.readline() == b"ready\n"
    return proc


def test_stop_group_ends_a_stopped_group_with_its_sigterm():
    # Ctrl-Z'd, or stopped by a debugger: a SIGTERM that a handler catches (podman's does) waits
    # for a SIGCONT, and without one the group sat out the whole grace before the SIGKILL.
    from foldyard import devmode

    proc = _sleeper_in_its_own_group("lambda *_: sys.exit(42)")
    os.killpg(proc.pid, signal.SIGSTOP)
    try:
        devmode.stop_group(proc)
    finally:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    assert proc.returncode == 42  # its handler ran: it ended on the SIGTERM


# A leader that ends on SIGTERM, leaving a member of its group that ignores it.
_LEADER_LEAVES_A_STUBBORN_MEMBER = (
    "import subprocess, sys, time\n"
    "member = 'import signal, time\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
    "print(1, flush=True)\\ntime.sleep(60)\\n'\n"
    "g = subprocess.Popen([sys.executable, '-c', member], stdout=subprocess.PIPE)\n"
    "g.stdout.readline()\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _gone(pid: int, within: float = 10.0) -> bool:
    """Whether ``pid`` is gone within ``within`` seconds (an orphan is reaped by whoever adopts it)."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_stop_group_kills_a_member_that_outlives_its_leader(monkeypatch):
    # The leader is reaped at once, and with it the wait ended: a member that ignores SIGTERM got
    # nothing more. While the group has a member its id can't be reused, so it is still the one.
    from foldyard import devmode

    monkeypatch.setattr(devmode, "_STOP_GRACE", 1.0, raising=False)
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", _LEADER_LEAVES_A_STUBBORN_MEMBER],
        stdout=subprocess.PIPE,
        process_group=0,
    )
    assert proc.stdout is not None
    member = int(proc.stdout.readline())
    try:
        devmode.stop_group(proc)
        assert _gone(member), "the member that ignored SIGTERM outlived the stop"
    finally:
        with contextlib.suppress(OSError):
            os.kill(member, signal.SIGKILL)


def test_a_stop_cut_short_by_another_signal_is_done_again(monkeypatch):
    # A second signal mid-stop (between its SIGTERM and its SIGCONT, say) raises out of it: the
    # stop isn't done, so the way out does it again rather than leave the group with a SIGTERM.
    from foldyard import devmode

    real_stop = devmode.stop_group
    calls: list[subprocess.Popen] = []

    def cut_short_once(proc: subprocess.Popen) -> None:
        calls.append(proc)
        if len(calls) == 1:
            raise SystemExit(143)
        real_stop(proc)

    monkeypatch.setattr(devmode, "stop_group", cut_short_once)
    spawned: list[subprocess.Popen] = []

    def interrupted(proc: subprocess.Popen) -> None:
        spawned.append(proc)
        raise KeyboardInterrupt

    with pytest.raises(SystemExit):
        guestcompose.stream_here(
            [sys.executable, "-I", "-c", _SAYS_ITS_PID], {}, on_spawn=interrupted
        )
    assert len(calls) == 2
    assert spawned[0].poll() is not None


def test_a_second_interrupt_while_stopping_a_group_kills_it_at_once():
    # Ctrl-C again during the SIGTERM grace: the group still goes, at once, with no new raise.
    from foldyard import devmode

    proc = _sleeper_in_its_own_group("signal.SIG_IGN")
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    timer = _main_interrupted_after(0.5)
    started = time.monotonic()
    raised = False
    try:
        devmode.stop_group(proc)
    except KeyboardInterrupt:
        raised = True
    finally:
        timer.cancel()
        signal.signal(signal.SIGINT, previous)
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    assert not raised, "the second interrupt left the group without its SIGKILL"
    assert proc.returncode == -signal.SIGKILL
    assert time.monotonic() - started < 5


# What `fy box exec` may be asked for as data: a NUL, a carriage return, bytes that aren't UTF-8,
# and no newline at the end.
_DATA = b"a\x00b\r\n\xff\xfe\x1b[31mred"


def test_a_stream_not_going_to_a_terminal_is_handed_over_byte_for_byte(tmp_path):
    # `fy box exec 'pg_dump' > dump.sql`, `fy box exec 'git ls-files -z' | xargs -0`.
    prog = (
        f"import sys; sys.stdout.buffer.write({_DATA!r}); sys.stderr.buffer.write({_DATA!r}); "
        "sys.exit(3)"
    )
    code = (
        "import os, sys\n"
        "from foldyard import guestcompose\n"
        f"cmd = [sys.executable, '-c', {prog!r}]\n"
        "sys.exit(guestcompose.stream_here(cmd, dict(os.environ), clean_only_terminals=True))\n"
    )
    proc, err = _spawn_harness(code, tmp_path)
    assert proc.stdout is not None
    out = proc.stdout.read()
    assert _ended(proc) == 3
    assert out == _DATA
    assert err.read_bytes() == _DATA


def test_a_stream_going_to_a_terminal_is_cleaned(monkeypatch):
    monkeypatch.setattr(guestcompose, "_terminal", lambda fd: fd == 1)
    got = _sinks(monkeypatch)
    prog = "import sys; print('\\x1b]52;c;ZXZpbA==\\x07out'); sys.exit(3)"
    cmd = [sys.executable, "-I", "-c", prog]
    assert guestcompose.stream_here(cmd, {}, clean_only_terminals=True) == 3
    assert got["stdout"] == ["]52;c;ZXZpbA==out"]


def _interrupt_on(word: str):
    """A sink that sends the main thread a real SIGINT when ``word`` arrives: an interrupt
    landing while the relay waits, as the terminal's Ctrl-C would."""
    import signal
    import threading

    main = threading.main_thread().ident
    assert main is not None

    def sink(line: str) -> None:
        if line == word:
            signal.pthread_kill(main, signal.SIGINT)

    return sink


def test_an_interrupted_watched_stream_here_stops_its_process_group(monkeypatch):
    # Its own group means the terminal's Ctrl-C no longer reaches it: the interrupt must, and
    # reach what it started too (a compose client's podman children), so the group is stopped.
    from foldyard import devmode

    monkeypatch.setattr(
        guestcompose, "_print_to", lambda name, style=None: _interrupt_on("started")
    )
    stopped: list[subprocess.Popen] = []
    real_stop = devmode.stop_group
    monkeypatch.setattr(devmode, "stop_group", lambda p: stopped.append(p) or real_stop(p))
    spawned: list[subprocess.Popen] = []
    cmd = [sys.executable, "-I", "-c", _CHATTY_SLEEPER.decode()]
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)  # a worker may ignore it
    try:
        with pytest.raises(KeyboardInterrupt):
            guestcompose.stream_here(cmd, {}, on_spawn=spawned.append)
    finally:
        signal.signal(signal.SIGINT, previous)
    assert stopped == spawned
    assert spawned[0].poll() is not None


class _Interrupted:
    """A process whose wait is interrupted once, as by Ctrl-C."""

    pid = 4242

    def __init__(self) -> None:
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self.waits = 0
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.waits == 1:
            raise KeyboardInterrupt
        return -15

    def terminate(self) -> None:
        self.terminated = True


def test_an_interrupted_relay_ends_its_process_before_it_passes_the_interrupt_on():
    from typing import Any, cast

    proc = _Interrupted()
    with pytest.raises(KeyboardInterrupt):
        guestcompose.relay(cast(Any, proc), print, print)
    assert proc.terminated and proc.waits == 2  # reaped, not left behind
    stopped: list[object] = []
    proc = _Interrupted()
    with pytest.raises(KeyboardInterrupt):
        guestcompose.relay(cast(Any, proc), print, print, stop=stopped.append)
    assert stopped == [proc] and not proc.terminated


def test_run_stream_feeds_a_large_stdin_and_merges_the_output():
    from foldyard import devmode

    lines: list[str] = []
    assert devmode.run_stream([sys.executable, "-I", "-"], lines.append, stdin=_NOISY) == 3
    assert "err line" in lines


_GROUP = b"import os\nprint(os.getpgid(0) == os.getpid())\n"


def test_a_watched_relay_hands_over_its_process_in_a_group_of_its_own():
    # The dependency watch stops a hung `up` by its process group: the client and what it started.
    spawned: list[subprocess.Popen] = []
    out: list[str] = []
    rc = guestcompose.run_relayed(
        [sys.executable, "-I", "-"], _GROUP, out.append, lambda _: None, on_spawn=spawned.append
    )
    assert rc == 0
    assert len(spawned) == 1
    assert out == ["True"]


def test_a_watched_stream_hands_over_its_process_in_a_group_of_its_own():
    from foldyard import devmode

    spawned: list[subprocess.Popen] = []
    lines: list[str] = []
    rc = devmode.run_stream(
        [sys.executable, "-I", "-"], lines.append, stdin=_GROUP, on_spawn=spawned.append
    )
    assert rc == 0
    assert len(spawned) == 1
    assert lines == ["True"]


_CHATTY_SLEEPER = b"import time\nprint('started', flush=True)\ntime.sleep(60)\n"


@pytest.mark.parametrize("fed", [False, True])
def test_an_interrupted_watched_stream_stops_its_process_group(fed: bool):
    # Its own group means the terminal's Ctrl-C no longer reaches it: the interrupt must.
    from foldyard import devmode

    spawned: list[subprocess.Popen] = []

    def interrupt(_line: str) -> None:
        raise KeyboardInterrupt

    cmd = [sys.executable, "-I", *(["-"] if fed else ["-c", _CHATTY_SLEEPER.decode()])]
    with pytest.raises(KeyboardInterrupt):
        devmode.run_stream(
            cmd, interrupt, stdin=_CHATTY_SLEEPER if fed else None, on_spawn=spawned.append
        )
    assert spawned[0].poll() is not None


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
