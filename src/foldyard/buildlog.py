"""Image build logs: one build's output into a log on your computer, cleaned and stamped.

An image build prints hundreds of lines (layers, package installs), and a warm one scrolled them
past the lines that matter. So each kind of build writes to one log per project under the state
dir (``config.state_dir``, which no box mounts), overwritten by its next build so the logs don't
build up: the stack's (``build-<compose project>.log``, ``stack._podman_build``), the dev box
image's (``box-build.log``) and its ``[box].packages`` layer's (``box-packages-build.log``, both
``box._gated_build``). The base VM's bake (``boximage.build_packaged_image``) has no project, and
builds foldyard's own Dockerfile in front of you.

The Dockerfile and its context are the box's, so everything a build prints is too: each line is
cleaned on its way into the file (``guestcompose.printable``), so ``cat`` on the log later never
plays the box's escape sequences, and stamped with when it came (ISO 8601 UTC, as the
supervisor's log is). You see where the log is as the build starts, a heartbeat while it runs,
and at the end one line naming the log or, when it failed, its last :data:`BUILD_TAIL_LINES`
lines and its path. ``--verbose`` on the verbs that build shows each line live as well
(:func:`stream_builds_live`). :class:`BuildLog` also keeps the build's tail in memory, so a
failure shows it even when the file stopped taking lines.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from . import config


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


# The lines of a failed build's log shown on your terminal: kept as they come, so a log that stopped
# taking lines, or lines too long to read back cheaply, still give the whole of each.
BUILD_TAIL_LINES = 25
# How often a running build says it is still going. Its output goes to the build log, not the
# terminal, so without this a cold build is minutes of silence that reads as a hang.
_BUILD_HEARTBEAT = 30.0

_builds_live = False


def stream_builds_live(on: bool = True) -> None:
    """Show image build output as it comes, as well as logging it (``--verbose``)."""
    global _builds_live
    _builds_live = on


def build_log_path(name: str) -> Path:
    """Where the build log ``name`` lives: ``<state_dir>/<name>.log``."""
    return config.state_dir() / f"{name}.log"


def _mmss(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _grew(new: int) -> str:
    """How much a log grew since the last heartbeat: "no new output" is how a build that really
    is stuck shows itself."""
    if new <= 0:
        return "no new output"
    return f"log +{new} B" if new < 1024 else f"log +{new // 1024} KB"


class BuildLog:
    """One build's log, open for the build (:func:`open_build_log`). :meth:`line` is safe from
    several threads at once (a relay's two streams, concurrent builds).

    ``live``: each line is shown on your terminal too (``--verbose``, or a log that couldn't be
    opened). A log that stops taking lines part way (a full disk) turns live from there, and says
    so; a terminal that stops taking them (``| head`` gone) leaves the build to the log, and the
    log says so."""

    def __init__(self, where: Path, file: IO[bytes] | None, *, live: bool) -> None:
        self.path = where
        self.live = live
        self._file = file
        self._lock = threading.Lock()
        self._tail: deque[str] = deque(maxlen=BUILD_TAIL_LINES)
        self._stopped: str | None = None  # why the file stopped taking lines part way

    @property
    def written(self) -> bool:
        """Whether the build's output reaches the file (not when it couldn't be opened)."""
        return self._file is not None

    def line(self, text: str) -> None:
        """One line of the build's output: cleaned, stamped, into the file; on your terminal too
        when live. Never raises: neither a log nor a terminal that stops taking lines fails a
        build."""
        from . import guestcompose

        text = guestcompose.printable(text)
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        with self._lock:
            self._tail.append(text)
            if self._file is not None and self._stopped is None:
                try:
                    self._file.write(f"{stamp} {text}\n".encode(errors="backslashreplace"))
                    self._file.flush()
                except OSError as e:
                    self._stopped = e.strerror or str(e)
                    self.live = True
                    _err(
                        f"⚠ the build log {guestcompose.printable(str(self.path))} stopped taking "
                        f"lines ({self._stopped}): the rest of the build is shown here instead"
                    )
                except ValueError:  # closed while a late line came in
                    return
            if self.live:
                try:
                    guestcompose.show(text)
                except OSError:  # your end of the terminal's pipe is gone: the log has it all
                    self.live = False
                    if self._file is not None and self._stopped is None:
                        note = "(foldyard: your terminal stopped taking the build's output)"
                        with suppress(OSError, ValueError):
                            self._file.write(f"{stamp} {note}\n".encode())
                            self._file.flush()

    def tail(self) -> list[str]:
        """The build's last :data:`BUILD_TAIL_LINES` lines, cleaned, without stamps."""
        with self._lock:
            return list(self._tail)

    def size(self) -> int:
        try:
            return os.stat(self.path).st_size
        except OSError:
            return 0

    def pipe(self) -> AbstractContextManager[IO[str]]:
        """A file processes can write to (``stdout=``), each line of it handed to :meth:`line`."""
        from . import guestcompose

        return guestcompose.pipe_to(self.line)

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                with suppress(OSError):
                    self._file.close()
                self._file = None

    def failed(self, header: str) -> None:
        """A failed build on your terminal: ``header`` (``✗ … failed (exit N)``), then what you
        haven't seen of its output, and where its log is. ``header`` is shown inert: the stack's
        names the services that failed, the render's (the box's) to choose."""
        from . import guestcompose
        from .term import printable

        header = printable(header)
        lines = self.tail()
        if self.live:
            _err(f"{header}: its output {'since then ' if self._stopped else ''}is above")
        elif not lines:
            _err(f"{header}, and printed nothing")
        else:
            _err(f"{header} — last {len(lines)} log lines:")
            for text in lines:
                guestcompose.show("  " + text)  # what the build printed: the box's
        if self._stopped:
            _err(f"  the log up to then: cat {self.path}")
        elif self.written:
            _err(f"  full log: cat {self.path}")

    def said(self, rc: int, what: str) -> None:
        """How the build ended, on your terminal: the log's path, and on failure :meth:`failed`."""
        if rc == 0:
            if self.written and not self._stopped:
                _err(f"✓ {what} done (log: {self.path})")
            return
        self.failed(f"✗ {what} failed (exit {rc})")


def _open_build_log_file(where: Path) -> IO[bytes]:
    """``where`` opened to be written from its start: never through a link, and only a regular
    file (a FIFO would hold the build until something reads it)."""
    where.parent.mkdir(parents=True, exist_ok=True)
    # Write-only and non-blocking: a FIFO with no reader is refused (ENXIO), never waited on.
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(where, flags, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{where} isn't a regular file")
        os.ftruncate(fd, 0)  # after the check: never truncate what isn't a regular file
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def open_build_log(name: str) -> BuildLog:
    """The build log ``name``, emptied for this build, its path said on your terminal. One that
    can't be opened is said too, and the build is shown live instead."""
    where = build_log_path(name)
    try:
        file = _open_build_log_file(where)
    except OSError as e:
        from .term import printable

        _err(
            f"⚠ can't write the build log {printable(str(where))} ({e.strerror or e}): the build's "
            "output is shown here instead"
        )
        return BuildLog(where, None, live=True)
    log = BuildLog(where, file, live=_builds_live)
    how = (
        "and shown here as it comes" if log.live else "tail -f to watch; shown here only on failure"
    )
    _err(f"▶ build output → {where} ({how})")
    return log


@contextmanager
def build_heartbeat(what: str, log: BuildLog, every: float | None = None) -> Iterator[None]:
    """While it lasts, a line every :data:`_BUILD_HEARTBEAT` seconds saying ``what`` is still
    building and whether its log grew; nothing when its output is shown live."""
    if log.live:
        yield
        return
    period = _BUILD_HEARTBEAT if every is None else every
    done = threading.Event()
    start = time.monotonic()

    def beat() -> None:
        seen = log.size()
        while not done.wait(period):
            size = log.size()
            if not log.live:  # turned live part way: its lines say it is still going
                _err(
                    f"… the {what} is still running — {_mmss(time.monotonic() - start)} elapsed "
                    f"({_grew(size - seen)})"
                )
            seen = size

    ticker = threading.Thread(target=beat, daemon=True)
    ticker.start()
    try:
        yield
    finally:
        done.set()
        ticker.join(timeout=1)


def run_build_logged(
    name: str, what: str, echoed: str, build: Callable[[Callable[[str], None]], int]
) -> int:
    """One build into the build log ``name``: ``echoed`` (its command line, as shown) first, then
    what ``build`` hands the sink it is given, a line at a time. Its exit code."""
    log = open_build_log(name)
    try:
        log.line(echoed)
        with build_heartbeat(what, log):
            rc = build(log.line)
        log.said(rc, what)
        return rc
    finally:
        log.close()


def _await_builds(futures: dict[Future, str], log_path: Path) -> list[tuple[str, int]]:
    """Wait for every build, returning the failed ones as (service, exit code). Every
    `_BUILD_HEARTBEAT` seconds with nothing finished it prints one line naming what is still
    building and whether the log grew since the last line — "no new output" is how a build that
    really is stuck shows itself."""
    start = time.monotonic()
    seen = 0
    failures: list[tuple[str, int]] = []
    pending = set(futures)
    while pending:
        done, pending = wait(pending, timeout=_BUILD_HEARTBEAT, return_when=FIRST_COMPLETED)
        failures += [r for f in done if (r := f.result())[1]]
        if done or not pending:
            continue
        try:
            size = log_path.stat().st_size
        except OSError:
            size = seen
        grew = _grew(size - seen)
        seen = size
        running = ", ".join(sorted(futures[f] for f in pending))
        finished = len(futures) - len(pending)
        _err(
            f"… still building {running} — {finished}/{len(futures)} done, "
            f"{_mmss(time.monotonic() - start)} elapsed ({grew})"
        )
    return failures
