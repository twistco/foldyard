"""A FIFO where git expects a file, and a bounded wait for a call that git may block on.

The shared ``.git`` is box-writable, and git opens a FIFO it finds there and waits for a writer
that never comes. conftest's ``fifos`` fixture is a :class:`Fifos`: ``plant`` makes one, and
``returns_within`` checks a host read returns anyway without the test itself hanging when it
doesn't (the call runs on a thread). At teardown — before ``monkeypatch`` undoes anything, so no
call carries on against the real home, state or engine — every planted FIFO is released (opened
for writing and closed, over and over: a git opens its config more than once) until every call
has finished.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_RELEASE_AT_LEAST = 1.0  # seconds of releasing, however soon the calls finish
_RELEASE_AT_MOST = 10.0


class Fifos:
    def __init__(self) -> None:
        self._planted: list[Path] = []
        self._calls: list[threading.Thread] = []

    def plant(self, path: Path) -> Path:
        """Replace ``path`` with a FIFO; returns it."""
        path.unlink(missing_ok=True)
        os.mkfifo(path)
        self._planted.append(path)
        return path

    def returns_within(self, seconds: float, fn: Callable[[], Any]) -> tuple[bool, Any]:
        """``(returned, value)``: whether ``fn()`` returned within ``seconds``, and what. What it
        raised in that time is raised here."""
        got: dict[str, Any] = {}

        def call() -> None:
            try:
                got["value"] = fn()
            except BaseException as e:
                got["error"] = e

        thread = threading.Thread(target=call, daemon=True)
        self._calls.append(thread)
        thread.start()
        thread.join(seconds)
        if thread.is_alive():
            return False, None
        if "error" in got:
            raise got["error"]
        return True, got.get("value")

    def release(self) -> None:
        """Release every planted FIFO until each call has finished; raise if one hasn't."""
        start = time.monotonic()
        while True:
            for path in self._planted:
                try:
                    os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:  # no reader on it right now
                    pass
            elapsed = time.monotonic() - start
            busy = any(t.is_alive() for t in self._calls)
            if elapsed >= _RELEASE_AT_MOST or (elapsed >= _RELEASE_AT_LEAST and not busy):
                break
            time.sleep(0.01)
        if busy:
            raise RuntimeError("a call is still blocked on a planted FIFO after its release")
