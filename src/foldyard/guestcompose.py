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
directory, the guest socket). Nothing is installed in the guest and nothing there is read for
code. The zip is loaded from a memfd, so no file the VM user can change sits between the bytes
sent and the code run.

What comes back is box-influenced data, like everything the engine answers: the compose files
are the box's, and so is the VM user's home in the guest (anything a box can bind-mount). Callers
use it only for the project they asked about, as they always have.

Phase 1 moves the READ-ONLY calls (``config``, ``config --services``, ``ps`` and the config
hash). ``up``, the builds and the interactive verbs still run here, under the guard.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

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
# LOGNAME/XDG_RUNTIME_DIR; the socket is the guest's (``guest_socket``).
_HOST_ONLY = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "DOCKER_HOST", "CONTAINER_HOST",
        "CONTAINER_CONNECTION", "DOCKER_CONFIG", "DOCKER_CONTEXT", "REGISTRY_AUTH_FILE",
        "CONTAINERS_CONF", "CONTAINERS_REGISTRIES_CONF", "CONTAINERS_STORAGE_CONF",
        "PODMAN_COMPOSE_PROVIDER",
    }
)  # fmt: skip

# A fixed timestamp, so the same sources make the same zip (and the same fingerprint).
_EPOCH = (1980, 1, 1, 0, 0, 0)


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
    literals, the zip mounted from a memfd, the runner's exit code ours."""
    blob = base64.b64encode(payload()).decode()
    req = base64.b64encode(json.dumps(request).encode()).decode()
    return (
        "import base64, json, os, sys\n"
        f"_p = base64.b64decode({blob!r})\n"
        f"_r = json.loads(base64.b64decode({req!r}))\n"
        "_fd = os.memfd_create('fy-compose')\n"
        "os.write(_fd, _p)\n"
        "sys.path.insert(0, f'/proc/self/fd/{_fd}')\n"
        f"import {_RUNNER}\n"
        f"sys.exit({_RUNNER}.main(_r))\n"
    ).encode()


def request(op: str, argv: list[str], env: dict[str, str], cwd: str) -> dict:
    from . import machine

    return {
        "op": op,
        "argv": list(argv),
        "env": guest_env(env),
        "cwd": cwd,
        "socket": machine.BACKEND.guest_socket(),
    }


def _refused(cmd: list[str], code: int, why: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, code, "", f"✗ {why}\n")


def call(
    op: str,
    argv: list[str],
    env: dict[str, str],
    cwd: str,
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """One run of the guest runner, its output captured as text. Never raises for the guest's
    answer (a non-zero exit is the caller's to read); a ``timeout`` that runs out raises
    ``TimeoutExpired``, as ``subprocess.run`` would."""
    from . import machine
    from .sandbox import _ssh_argv

    shown = ["compose-in-vm", op, *argv]
    target = machine.BACKEND.ssh_target(machine.MACHINE)
    if target is None:
        return _refused(
            shown,
            NO_ROUTE,
            f'[machine] compose = "vm", but there is no ssh route into {machine.MACHINE} '
            "(is it running? `fy machine status`)",
        )
    cmd = [*_ssh_argv(target), f"exec {PYTHON} -I -"]
    out = subprocess.run(
        cmd, input=bootstrap(request(op, argv, env, cwd)), capture_output=True, timeout=timeout
    )
    return subprocess.CompletedProcess(
        shown,
        out.returncode,
        out.stdout.decode(errors="replace"),
        out.stderr.decode(errors="replace"),
    )


def compose(ctx, args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    """``<engine> compose -f … <args>`` for ``ctx``, run in the guest. Only podman-compose runs
    there: a ``PODMAN_COMPOSE_PROVIDER`` naming docker-compose is refused, since the guest has no
    docker-compose (docs/compose-in-vm-scope.md, decision 3)."""
    from . import stack

    argv = [*ctx.compose[2:], *args]
    if not stack._podman_compose_active(ctx):
        return _refused(
            ["compose-in-vm", "compose", *argv],
            WRONG_PROVIDER,
            '[machine] compose = "vm" runs foldyard\'s own podman-compose in the VM, and this '
            "stack's compose provider is docker-compose (PODMAN_COMPOSE_PROVIDER). Unset it, or "
            'leave [machine] compose at "host".',
        )
    return call("compose", argv, ctx.env, str(ctx.main), timeout=timeout)


def config_hashes(ctx, flags: list[str], *, timeout: float | None = None):
    """podman-compose's own per-service config hashes, rendered in the guest —
    ``confighash.desired``'s podman branch. The CompletedProcess's stdout is the JSON map."""
    return call("hash", [*ctx.compose[2:], *flags], ctx.env, str(ctx.main), timeout=timeout)
