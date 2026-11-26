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

``stack._compose_output`` / ``_compose`` / ``_compose_captured`` are the only places that branch on
it (an AST guard in tests/test_guestcompose.py holds every use of ``ctx.compose`` to them), and
``composeguard`` stands down under it. It needs the checkout mounted in the VM, and registry logins
on your computer don't reach it.

What comes back is box-influenced data, like everything the engine answers: the compose files
are the box's, and so is the VM user's home in the guest (anything a box can bind-mount). Callers
use it only for the project they asked about, as they always have, and what is relayed to your
terminal has its control sequences stripped (:func:`printable`). The same relay serves what runs
on your computer with output the box chose (:func:`stream_here`): the compose client when this
switch is off, a build of the box's Dockerfile, a container of the box's image.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Any

from . import config

# Lima's Fedora guest has it — crossinject's too.
PYTHON = "/usr/bin/python3"

# The runner's module name inside the zip.
_RUNNER = "fy_compose_runner"

# Exit codes this module answers with itself, never from the guest.
NO_ROUTE = 125

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


def compose(ctx, args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    """``<engine> compose -f … <args>`` for ``ctx``, run in the guest, its output captured."""
    return call("compose", [*ctx.compose[2:], *args], ctx.env, str(ctx.main), timeout=timeout)


def config_hashes(ctx, flags: list[str], *, timeout: float | None = None):
    """podman-compose's own per-service config hashes, rendered in the guest —
    what ``confighash.desired``'s child computes here. The CompletedProcess's stdout is the JSON
    map."""
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


# The most a relay reads before handing it on: output with no newline in it (the box chooses it)
# can't make foldyard hold more than this.
_LINE_CAP = 64 * 1024


# How long, once the process has exited, a relay waits on a pipe with nothing coming before it
# stops: a grandchild that outlives it (a detached client) can hold the pipe open for good. Time
# spent writing to you (a pager on its first page) never counts: that output is all handed on.
_DRAIN_PATIENCE = 5.0

# How much a relay reads once the process has exited before it stops: only what was already in the
# pipe (a pipe's capacity, and a line short of _LINE_CAP) can be the process's own, and a
# grandchild still writing to the pipe it inherited never lets it go quiet.
_DRAIN_BUDGET = 1024 * 1024


class _Pump:
    """One stream's relay thread: ``source`` handed to ``sink`` a line at a time
    (:func:`printable`) until it ends, or until ``quit_``. A sink that raises (your end of the
    pipe is gone: ``fy logs | head``) ends it too, recorded in ``failed``. Either way ``source``
    is closed after: the child's next write fails, as it would have on a pipe it held itself,
    rather than blocking on one nobody reads. It keeps count of the time it has spent waiting for
    input, which is what tells a pipe a grandchild still holds from you not reading yet."""

    def __init__(
        self,
        source: IO[bytes],
        sink: Callable[[str], None],
        quit_: threading.Event,
        failed: list[BaseException],
    ) -> None:
        # Read by `drain` while the thread updates them: always both together, under the lock.
        self._lock = threading.Lock()
        self._waited = 0.0  # finished waits for input
        self._since: float | None = None  # when the wait in progress began
        self._read = 0  # bytes read
        self.thread = threading.Thread(
            target=self._run, args=(source, sink, quit_, failed), daemon=True
        )
        self.thread.start()

    def progress(self) -> tuple[float, int]:
        """Seconds spent waiting for input so far (the wait in progress included), and bytes
        read."""
        with self._lock:
            since, waited, read = self._since, self._waited, self._read
        return waited + (time.monotonic() - since if since is not None else 0.0), read

    def _run(
        self,
        source: IO[bytes],
        sink: Callable[[str], None],
        quit_: threading.Event,
        failed: list[BaseException],
    ) -> None:
        try:
            while not quit_.is_set():
                with self._lock:
                    start = self._since = time.monotonic()
                raw = source.readline(_LINE_CAP)
                with self._lock:
                    self._since = None
                    self._waited += time.monotonic() - start
                    self._read += len(raw)
                if not raw:
                    break
                line = printable(raw.decode(errors="replace").rstrip("\r\n"))
                if quit_.is_set():
                    break
                sink(line)
        except Exception as e:
            failed.append(e)
        finally:
            source.close()

    def drain(self) -> None:
        """Once the process has exited: wait while this relay has output to hand on, however
        long you take to read it. Stop waiting on a pipe that stays open with nothing in it for
        :data:`_DRAIN_PATIENCE` seconds, or that has given more than :data:`_DRAIN_BUDGET` bytes
        since: either is a grandchild's, not the process's."""
        waited, read = self.progress()
        while self.thread.is_alive():
            now_waited, now_read = self.progress()
            if now_waited - waited >= _DRAIN_PATIENCE or now_read - read > _DRAIN_BUDGET:
                return
            self.thread.join(timeout=0.05)


def _print_to(file_name: str) -> Callable[[str], None]:
    # Looked up on each line, not bound here: a redirected sys.stdout stays honoured.
    def sink(text: str) -> None:
        from .term import ColorStream

        out = getattr(sys, file_name)
        if isinstance(out, ColorStream):  # its ✓ and ✗ are the box's, not foldyard's own to paint
            out = out.raw
        # A terminal that isn't UTF-8 (LANG=C) can't take every character: escape the rest.
        encoding = getattr(out, "encoding", None) or "utf-8"
        print(text.encode(encoding, "backslashreplace").decode(encoding), file=out, flush=True)

    return sink


def show(line: str, to: str = "stderr") -> None:
    """One line the box chose (a build's tail, what the engine said about its image) on your
    ``to`` stream, cleaned (:func:`printable`) and as it is: not painted as foldyard's own."""
    _print_to(to)(printable(line))


def stream(ctx, args: list[str], on_spawn: Callable[[subprocess.Popen], None] | None = None) -> int:
    """Like :func:`compose`, its output relayed live to your stdout and stderr (a line at a time,
    :func:`printable`). For the verbs a person watches: ``up``, ``down``, ``ps``, ``logs -f``.
    ``on_spawn``: as :func:`run_relayed`'s."""
    return _stream("compose", [*ctx.compose[2:], *args], ctx.env, str(ctx.main), on_spawn)


def stream_podman(argv: list[str], env: dict[str, str], cwd: str) -> int:
    """``podman <argv>`` in the guest, relayed like :func:`stream`: a project's own box image, whose
    build context is the repo."""
    return _stream("podman", argv, env, cwd)


def _stream(
    op: str,
    argv: list[str],
    env: dict[str, str],
    cwd: str,
    on_spawn: Callable[[subprocess.Popen], None] | None = None,
) -> int:
    command = _command(op, argv, env, cwd)
    if command is None:
        print(f"✗ {_no_route()}", file=sys.stderr)
        return NO_ROUTE
    cmd, stdin = command
    return run_relayed(cmd, stdin, _print_to("stdout"), _print_to("stderr"), on_spawn=on_spawn)


def stream_here(
    cmd: list[str],
    env: dict[str, str],
    cwd: str | None = None,
    on_spawn: Callable[[subprocess.Popen], None] | None = None,
    *,
    clean_only_terminals: bool = False,
) -> int:
    """``cmd`` run on your computer, relayed like :func:`stream` (a line at a time to your stdout
    and stderr, :func:`printable`): for a process whose output the box chose — the compose client
    reading its compose files, a build of its Dockerfile, a container of its image. Its stdin is
    empty, so a verb that needs your terminal (a shell, ``compose exec``) doesn't come here.
    ``clean_only_terminals``: a stream that isn't going to a terminal (a file, a pipe) gets the
    process's bytes as they are, for output that is data (``fy box exec 'pg_dump' > dump.sql``).

    It runs in a process group of its own, so the terminal's Ctrl-C reaches foldyard alone: the
    relay stops printing, then stops the group (the process and what it started on your computer;
    a command a ``podman exec`` started in a container runs on). What it would say about an
    interrupt (podman-compose's KeyboardInterrupt traceback) never reaches you. A SIGHUP, SIGTERM
    or SIGQUIT that would end foldyard stops the group too (:func:`_ending_signals_raise`). Ctrl-Z
    stops foldyard and not the group, which runs on (until a pipe foldyard no longer reads fills).
    ``on_spawn`` gets the process, so the caller can stop it too."""
    from .devmode import stop_group

    pipe = subprocess.PIPE
    stopped: list[subprocess.Popen] = []

    def stop(spawned: subprocess.Popen) -> None:
        if not stopped:
            stop_group(spawned)
            # Only once it is done: a signal that cut it short leaves it to be done again.
            stopped.append(spawned)

    proc: subprocess.Popen | None = None
    with _ending_signals_raise():
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,  # exactly: a compose client interpolates from it (see stack.resolve)
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=pipe if not clean_only_terminals or _terminal(1) else None,
                stderr=pipe if not clean_only_terminals or _terminal(2) else None,
                process_group=0,
            )
            if on_spawn:
                on_spawn(proc)
            return relay(proc, _print_to("stdout"), _print_to("stderr"), stop=stop)
        except BaseException:
            # relay stopped it already, unless this came before relay started or another signal
            # cut its stop short. Then it is done here, again for each signal that cuts it short,
            # and foldyard ends on the last of them.
            later: BaseException | None = None
            while proc is not None and not stopped:
                try:
                    stop(proc)
                except (KeyboardInterrupt, SystemExit) as e:
                    later = e
            if later is not None:
                raise later  # the first is its context
            raise


def _terminal(fd: int) -> bool:
    try:
        return os.isatty(fd)
    except OSError:
        return False


@contextlib.contextmanager
def _ending_signals_raise() -> Iterator[None]:
    """While it lasts, a SIGHUP (the terminal closed), SIGTERM (``kill``, ``timeout``) or SIGQUIT
    that would end foldyard raises ``SystemExit(128 + signal)`` instead, so a relay stops its
    process group on the way out: that group gets no signal from your terminal or shell. Only a
    signal left at its default, and only in the main thread (the one Python delivers them to); one
    you ignore (``nohup``) stays ignored."""
    taken: dict[signal.Signals, Any] = {}

    def ended(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT):
            if signal.getsignal(sig) == signal.SIG_DFL:
                taken[sig] = signal.signal(sig, ended)
    try:
        yield
    finally:
        for sig, previous in taken.items():
            signal.signal(sig, previous)


def run_relayed(
    cmd: list[str],
    stdin: bytes,
    out: Callable[[str], None],
    err: Callable[[str], None],
    on_spawn: Callable[[subprocess.Popen], None] | None = None,
) -> int:
    """Run ``cmd`` with ``stdin`` as its input, its two streams relayed line by line. An interrupt
    ends ssh, and the guest's client with it (the runner watches its session). ``on_spawn`` gets
    the process, started in a process group of its own so the caller can stop it."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_ssh_env(),
        process_group=0 if on_spawn else None,
    )
    if on_spawn:
        on_spawn(proc)
    assert proc.stdin is not None

    def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except OSError:  # ssh already gone: its exit code says why
            pass

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    rc = relay(proc, out, err)
    feeder.join(timeout=5)
    return rc


def relay(
    proc: subprocess.Popen,
    out: Callable[[str], None],
    err: Callable[[str], None],
    *,
    stop: Callable[[subprocess.Popen], None] | None = None,
) -> int:
    """``proc``'s piped stdout and stderr (whichever is piped) handed to ``out`` and ``err`` a line
    at a time (:func:`printable`) until it exits; its exit code. An interrupt here, even once it
    has exited and what it left behind is being drained, ends it before passing on: ``stop`` (its
    process group, when it has one of its own), else a terminate. A sink that failed (your end of
    the pipe gone) is raised once the process has ended: a ``BrokenPipeError`` is the quiet exit
    the CLI gives a closed pipe."""
    quit_ = threading.Event()
    failed: list[BaseException] = []
    pumps: list[_Pump] = []
    try:
        for source, sink in ((proc.stdout, out), (proc.stderr, err)):
            if source is not None:
                pumps.append(_Pump(source, sink, quit_, failed))
        rc = proc.wait()
        for pump in pumps:
            pump.drain()
    except BaseException:
        # Nothing more is printed, and the threads are done before this passes on: a daemon
        # thread mid-print as the interpreter shuts down aborts it ("could not acquire lock for
        # <stdout> at interpreter shutdown").
        quit_.set()
        try:
            if stop is not None:
                stop(proc)
            else:
                proc.terminate()
                proc.wait()
        finally:
            for pump in pumps:
                pump.thread.join(timeout=1)
        raise
    quit_.set()  # one still waiting (a grandchild holds the pipe) prints nothing more
    if failed:
        raise failed[0]
    return rc


def stream_to(
    ctx,
    args: list[str],
    emit: Callable[[str], None],
    on_spawn: Callable[[subprocess.Popen], None] | None = None,
) -> int:
    """Like :func:`stream`, its two streams merged and handed to ``emit`` a line at a time (the
    posture reconcile, whose output must not bleed over the TUI)."""
    from . import devmode

    command = _command("compose", [*ctx.compose[2:], *args], ctx.env, str(ctx.main))
    if command is None:
        emit(f"✗ {_no_route()}")
        return NO_ROUTE
    cmd, stdin = command
    return devmode.run_stream(
        cmd,
        lambda line: emit(printable(line)),
        env=_ssh_env(),
        stdin=stdin,
        on_spawn=on_spawn,
    )
