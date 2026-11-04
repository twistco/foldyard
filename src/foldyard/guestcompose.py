"""The compose client runs in the guest — ``[machine] compose = "vm"`` (experimental).

Your stack runs in the VM, but by default its compose client runs on your computer, where it
parses compose files the box can edit (``composeguard`` and ``stack._host_passthrough`` clamp
that). With this on, the client runs in the guest instead, as the VM user, over the backend's
ssh route (``Backend.ssh_target``, the route ``crossinject`` and ``guestlog`` use): an
``env_file:`` or a build context there reads the guest, which holds nothing of yours. The design
and its phases are docs/compose-in-vm-scope.md.

Each call is one ssh run of ``python3 -I -`` in the guest, its stdin the whole payload: a
deterministic zip of foldyard's guest runner (``assets/compose/runner.py``), foldyard's own
pinned podman-compose and its pure-Python dependencies (PyYAML without its C extension, and
python-dotenv), plus the request (the argv, the env your computer computed, the working
directory, the guest socket). Nothing is installed in the guest: the zip is imported from memory,
so it is always the client foldyard was tested with.

There is never a fallback to a client on your computer: with no ssh route, the call refuses and
says so. The one verb that doesn't come here is ``fy shell``, which needs your terminal on the
stdin the payload uses: ``stack.shell`` finds the container through the engine and enters it the
way ``fy box shell`` does, parsing no compose file.

What comes back is box-influenced data, like everything the engine answers: the compose files
are the box's, and so is the VM user's home in the guest (anything a box can bind-mount). Callers
use it only for the project they asked about, as they always have, and what is relayed to your
terminal has its control sequences stripped (:func:`printable`).
"""

from __future__ import annotations

import base64
import functools
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from . import config

# Both backends' guests have it (Fedora on Lima, CoreOS on podman machine) — crossinject's too.
PYTHON = "/usr/bin/python3"

# The runner's module name inside the zip.
_RUNNER = "fy_compose_runner"

# Exit codes this module answers with itself, never from the guest.
NO_ROUTE = 125
WRONG_PROVIDER = 2

# Names in the compose env that describe YOUR computer and mean nothing in the guest (paths,
# connection names, the provider's path here). The guest runner supplies its own PATH/HOME/USER/
# LOGNAME/XDG_RUNTIME_DIR; the socket is the guest's (``box_sock_in_vm``).
_HOST_ONLY = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "DOCKER_HOST", "CONTAINER_HOST",
        "CONTAINER_CONNECTION", "DOCKER_CONFIG", "DOCKER_CONTEXT", "REGISTRY_AUTH_FILE",
        "CONTAINERS_CONF", "CONTAINERS_REGISTRIES_CONF", "CONTAINERS_STORAGE_CONF",
        "PODMAN_COMPOSE_PROVIDER",
    }
)  # fmt: skip

# What the ssh client itself may inherit. The supervisor calls this module and its environment holds
# every host.env secret; a `SendEnv` in your ssh config would forward what it names.
_SSH_ENV = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "SSH_AUTH_SOCK")

# A fixed timestamp, so the same sources make the same zip (and the same fingerprint).
_EPOCH = (1980, 1, 1, 0, 0, 0)

# Imports the zip from memory. Stdlib only, and run before anything else in the guest.
_BOOTSTRAP = """\
import base64, importlib.abc, importlib.util, io, json, sys, zipfile
_z = zipfile.ZipFile(io.BytesIO(base64.b64decode({blob!r})))
_n = set(_z.namelist())
class _Zip(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        base = name.replace('.', '/')
        pkg = base + '/__init__.py' in _n
        if not pkg and base + '.py' not in _n:
            return None
        return importlib.util.spec_from_loader(name, self, is_package=pkg)
    def exec_module(self, module):
        base = module.__name__.replace('.', '/')
        file = base + '/__init__.py' if base + '/__init__.py' in _n else base + '.py'
        exec(compile(_z.read(file), 'fy-compose:' + file, 'exec'), module.__dict__)
sys.meta_path.insert(0, _Zip())
import {runner}
sys.exit({runner}.main(json.loads(base64.b64decode({request!r}))))
"""


def wanted() -> bool:
    """Whether compose runs in the guest: ``[machine] compose = "vm"``, on your computer. In the
    box the client already runs in the VM, so nothing changes there."""
    return not config.in_box() and config.machine_compose() == "vm"


def _sources() -> list[tuple[str, Path]]:
    """``(name in the zip, file here)`` for every module the guest runs: the runner, then
    podman-compose, PyYAML and python-dotenv, from the interpreter foldyard runs on (so they are
    the versions it was installed and tested with). ``.py`` only: PyYAML falls back to its
    pure-Python parser without ``_yaml``, and a C extension built here wouldn't load there."""
    import dotenv
    import podman_compose
    import yaml

    files = [
        (f"{_RUNNER}.py", Path(__file__).resolve().parent / "assets" / "compose" / "runner.py"),
        ("podman_compose.py", Path(str(podman_compose.__file__))),
    ]
    for pkg in (yaml, dotenv):
        root = Path(str(pkg.__file__)).parent
        files += [(f"{pkg.__name__}/{f.relative_to(root)}", f) for f in sorted(root.rglob("*.py"))]
    return files


@functools.lru_cache(maxsize=1)
def payload() -> bytes:
    """The zip the guest runs, deterministic: sorted names, one timestamp, fixed modes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in sorted(_sources()):
            info = zipfile.ZipInfo(name, date_time=_EPOCH)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, path.read_bytes())
    return buf.getvalue()


def fingerprint() -> str:
    """The payload's sha256 (first 16 hex digits), for logs and the spike's report."""
    return hashlib.sha256(payload()).hexdigest()[:16]


def guest_env(env: dict[str, str]) -> dict[str, str]:
    """The compose env as the guest gets it: everything foldyard resolved, minus what only
    describes your computer (``_HOST_ONLY``)."""
    return {k: v for k, v in env.items() if k not in _HOST_ONLY}


def bootstrap(request: dict) -> bytes:
    """The program ``python3 -I -`` reads on stdin: the payload and the request as base64
    literals, the zip imported from memory, the runner's exit code ours."""
    return _BOOTSTRAP.format(
        blob=base64.b64encode(payload()).decode(),
        runner=_RUNNER,
        request=base64.b64encode(json.dumps(request).encode()).decode(),
    ).encode()


def request(op: str, argv: list[str], env: dict[str, str], cwd: str) -> dict:
    return {
        "op": op,
        "argv": list(argv),
        "env": guest_env(env),
        "cwd": cwd,
        "socket": config.box_sock_in_vm(),
    }


def _ssh_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _SSH_ENV if k in os.environ}


def _no_route() -> str:
    from . import machine

    return (
        f'[machine] compose = "vm", but there is no ssh route into {machine.MACHINE} '
        "(is it running? `fy machine status`)"
    )


def _command(op: str, argv: list[str], env: dict[str, str], cwd: str) -> tuple[list, bytes] | None:
    """``(ssh argv, stdin)`` for one run of the guest runner; None with no ssh route. Nothing from
    ``argv`` reaches the guest's shell: all of it travels on stdin."""
    from . import machine
    from .sandbox import _ssh_argv

    target = machine.BACKEND.ssh_target(machine.MACHINE)
    if target is None:
        return None
    return [*_ssh_argv(target), f"exec {PYTHON} -I -"], bootstrap(request(op, argv, env, cwd))


def _refused(cmd: list[str], code: int, why: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, code, "", f"✗ {why}\n")


def call(
    op: str,
    argv: list[str],
    env: dict[str, str],
    cwd: str,
    *,
    timeout: float | None = None,
    stdout: IO[Any] | int | None = None,
    stderr: IO[Any] | int | None = None,
) -> subprocess.CompletedProcess:
    """One run of the guest runner. Its output is captured as text, unless ``stdout``/``stderr``
    say where it goes (a build's log). Never raises for the guest's answer (a non-zero exit is the
    caller's to read); a ``timeout`` that runs out raises ``TimeoutExpired``, as
    ``subprocess.run`` would."""
    shown = ["compose-in-vm", op, *argv]
    command = _command(op, argv, env, cwd)
    if command is None:
        return _refused(shown, NO_ROUTE, _no_route())
    cmd, stdin = command
    capture = stdout is None and stderr is None
    out = subprocess.run(
        cmd,
        input=stdin,
        timeout=timeout,
        env=_ssh_env(),
        stdout=subprocess.PIPE if capture else stdout,
        stderr=subprocess.PIPE if capture else stderr,
    )
    return subprocess.CompletedProcess(
        shown,
        out.returncode,
        out.stdout.decode(errors="replace") if capture else "",
        out.stderr.decode(errors="replace") if capture else "",
    )


def _provider_refusal(ctx, argv: list[str]) -> subprocess.CompletedProcess | None:
    """Only podman-compose runs in the guest: it has no docker-compose
    (docs/compose-in-vm-scope.md, decision 3)."""
    from . import stack

    if stack._podman_compose_active(ctx):
        return None
    return _refused(
        ["compose-in-vm", "compose", *argv],
        WRONG_PROVIDER,
        '[machine] compose = "vm" runs foldyard\'s own podman-compose in the VM, and this '
        "stack's compose provider is docker-compose (PODMAN_COMPOSE_PROVIDER). Unset it, or "
        'leave [machine] compose at "host".',
    )


def compose(ctx, args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    """``<engine> compose -f … <args>`` for ``ctx``, run in the guest, its output captured."""
    argv = [*ctx.compose[2:], *args]
    if (refused := _provider_refusal(ctx, argv)) is not None:
        return refused
    return call("compose", argv, ctx.env, str(ctx.main), timeout=timeout)


def config_hashes(ctx, flags: list[str], *, timeout: float | None = None):
    """podman-compose's own per-service config hashes, rendered in the guest —
    ``confighash.desired``'s podman branch. The CompletedProcess's stdout is the JSON map."""
    return call("hash", [*ctx.compose[2:], *flags], ctx.env, str(ctx.main), timeout=timeout)


def file_services(
    files: list[str], *, timeout: float | None = None
) -> dict[str, dict[str, list[str]] | None]:
    """Each compose file parsed in the guest: ``{file: {service: [profile, …]}}``, ``None`` for a
    file it couldn't read as one, and for all of them when the guest couldn't be asked. Parsing
    them here would read box-written bytes on your computer, through whatever link is there."""
    unknown: dict[str, dict[str, list[str]] | None] = dict.fromkeys(files)
    try:
        proc = call("services", files, {}, "/", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return unknown
    if proc.returncode != 0:
        return unknown
    try:
        answer = json.loads(proc.stdout)
    except ValueError:
        return unknown
    if not isinstance(answer, dict):
        return unknown
    return {f: v if isinstance(v := answer.get(f), dict) else None for f in files}


def printable(line: str) -> str:
    """``line`` with every control sequence and character gone (tabs stay): the guest's output is
    box-influenced, and it would otherwise reach your terminal as escape sequences."""
    from .devmode import _strip_ansi

    return "".join(c for c in _strip_ansi(line) if c == "\t" or c.isprintable())


def _relay(source: IO[bytes], sink: Callable[[str], None]) -> None:
    for raw in source:
        sink(printable(raw.decode(errors="replace").rstrip("\r\n")))


def _print_to(file_name: str) -> Callable[[str], None]:
    # Looked up on each line, not bound here: a redirected sys.stdout stays honoured.
    def sink(text: str) -> None:
        print(text, file=getattr(sys, file_name), flush=True)

    return sink


def stream(ctx, args: list[str]) -> int:
    """Like :func:`compose`, its output relayed live to your stdout and stderr (a line at a time,
    :func:`printable`). For the verbs a person watches: ``up``, ``down``, ``ps``, ``logs -f``."""
    argv = [*ctx.compose[2:], *args]
    if (refused := _provider_refusal(ctx, argv)) is not None:
        print(refused.stderr, end="", file=sys.stderr)
        return refused.returncode
    return _stream("compose", argv, ctx.env, str(ctx.main))


def stream_podman(argv: list[str], env: dict[str, str], cwd: str) -> int:
    """``podman <argv>`` in the guest, relayed like :func:`stream`: a project's own box image, whose
    build context is the repo."""
    return _stream("podman", argv, env, cwd)


def _stream(op: str, argv: list[str], env: dict[str, str], cwd: str) -> int:
    command = _command(op, argv, env, cwd)
    if command is None:
        print(f"✗ {_no_route()}", file=sys.stderr)
        return NO_ROUTE
    cmd, stdin = command
    return run_relayed(cmd, stdin, _print_to("stdout"), _print_to("stderr"))


def run_relayed(
    cmd: list[str], stdin: bytes, out: Callable[[str], None], err: Callable[[str], None]
) -> int:
    """Run ``cmd`` with ``stdin`` as its input, its two streams relayed line by line. An interrupt
    ends ssh, and the guest's client with it (the runner watches its session)."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_ssh_env(),
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except OSError:  # ssh already gone: its exit code says why
            pass

    threads = [
        threading.Thread(target=feed, daemon=True),
        threading.Thread(target=_relay, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=_relay, args=(proc.stderr, err), daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        rc = proc.wait()
    except BaseException:
        proc.terminate()
        proc.wait()
        raise
    for t in threads:
        t.join(timeout=5)
    return rc


def stream_to(ctx, args: list[str], emit: Callable[[str], None]) -> int:
    """Like :func:`stream`, its two streams merged and handed to ``emit`` a line at a time (the
    posture reconcile, whose output must not bleed over the TUI)."""
    from . import devmode

    argv = [*ctx.compose[2:], *args]
    refused = _provider_refusal(ctx, argv)
    command = None if refused else _command("compose", argv, ctx.env, str(ctx.main))
    if command is None:
        emit(refused.stderr.strip() if refused else f"✗ {_no_route()}")
        return refused.returncode if refused else NO_ROUTE
    cmd, stdin = command
    return devmode.run_stream(cmd, lambda line: emit(printable(line)), env=_ssh_env(), stdin=stdin)
