"""The subprocess census — a REPORT of every process the suite spawns, not a gate (the gate is
conftest's hermetic guard, which refuses a host tool at execute time; this says what the
allowlist still lets through, and what the opt-in e2e tiers reach).

Two halves in one module:

* a pytest plugin (``-p tools.census --census=DIR``, with ``tests/`` on PYTHONPATH — the just
  recipe does both): ``subprocess.Popen`` —
  the choke point ``run``/``check_output``/``check_call``/``call`` all funnel through, so
  wrapping it alone counts each spawn once — is wrapped ONCE per session (the hermetic guard's
  per-test monkeypatch layers on top and restores to this wrapper), every spawn recorded as
  ``basename(argv[0])`` (or ``executable`` when given — that is what runs) keyed by the test
  pytest says is current, and one JSON per xdist worker written to ``DIR`` at session end (the
  xdist controller records nothing: its only spawns are the workers);
* an aggregator (``python tests/tools/census.py DIR``): the workers' files merged into one
  table, binary → spawn count, test count, the tests — what ``just census`` prints.

A shell string (``shell=True``) is recorded under its first word with a ``shell:`` prefix — a
whole program the census can't see into, exactly as the guard treats it. ATTEMPTS are counted,
before the spawn succeeds or fails: a bare host tool the scrubbed PATH turns into "not found"
still shows (that is the row saying the code under test reached for it). The ``<outside a
test>`` rows are each xdist worker's own startup (``platform``'s ``uname``/``file`` probes).
Nothing here spawns.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

PHASES = (" (setup)", " (call)", " (teardown)")


def _current_test() -> str:
    """``PYTEST_CURRENT_TEST`` minus its phase suffix; ``<outside a test>`` for session-level
    spawns (a session fixture building the shim dir, say)."""
    name = os.environ.get("PYTEST_CURRENT_TEST", "")
    for phase in PHASES:
        name = name.removesuffix(phase)
    return name or "<outside a test>"


def _binary(cmd, args: tuple, kwargs: dict) -> str:
    """What is about to run, as a short name: ``executable`` when given (Popen's third
    positional, after ``bufsize``, or the kwarg), else argv[0] — a string command is a whole
    program, named by its first word."""
    executable = args[1] if len(args) > 1 else kwargs.get("executable")
    if executable is not None:
        return os.path.basename(os.fsdecode(executable))
    if isinstance(cmd, (str, bytes, os.PathLike)):
        text = os.fsdecode(cmd)
        first = text.split()[0] if text.split() else text
        return ("shell:" if kwargs.get("shell") else "") + os.path.basename(first)
    if not cmd:
        return "<empty argv>"
    return os.path.basename(os.fsdecode(cmd[0]))


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--census",
        metavar="DIR",
        default=None,
        help="record every process the suite spawns (binary × test), one JSON per xdist worker "
        "under DIR — a report over conftest's hermetic guard, not a gate; aggregate with "
        "`python tests/tools/census.py DIR` (`just census`)",
    )


def pytest_configure(config) -> None:
    out = config.getoption("--census")
    if out and not config.pluginmanager.hasplugin("dsession"):  # not the xdist controller
        config.pluginmanager.register(Census(Path(out)), "foldyard-census")


class Census:
    """The recorder: one instance per pytest process that runs tests."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.spawns: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # `stack`'s image build runs independent specs in a ThreadPoolExecutor, each spawning
        # through this same wrapper: the nested increment is read-modify-write, so it is locked.
        self._lock = threading.Lock()
        self._real_popen = None

    def pytest_sessionstart(self, session) -> None:
        real = subprocess.Popen
        census = self

        class CountingPopen(real):  # type: ignore[misc,valid-type]
            def __init__(self, cmd, *args, **kwargs):
                with census._lock:
                    census.spawns[_binary(cmd, args, kwargs)][_current_test()] += 1
                super().__init__(cmd, *args, **kwargs)

        CountingPopen.__name__ = CountingPopen.__qualname__ = "Popen"
        self._real_popen = real
        cast(Any, subprocess).Popen = CountingPopen

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        if self._real_popen is not None:
            cast(Any, subprocess).Popen = self._real_popen
        if not self.spawns:
            return  # the xdist controller spawns nothing itself
        self.out_dir.mkdir(parents=True, exist_ok=True)
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        path = self.out_dir / f"census-{worker}.json"
        path.write_text(json.dumps(self.spawns, indent=1, sort_keys=True))


def aggregate(out_dir: Path) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for path in sorted(out_dir.glob("census-*.json")):
        for binary, tests in json.loads(path.read_text()).items():
            for test, n in tests.items():
                merged[binary][test] += n
    return merged


def render(merged: dict[str, dict[str, int]], *, list_tests: bool) -> str:
    if not merged:
        return "(no spawns recorded)"
    lines = [f"{'binary':<24} {'spawns':>7} {'tests':>6}", "-" * 39]
    for binary, tests in sorted(merged.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])):
        lines.append(f"{binary:<24} {sum(tests.values()):>7} {len(tests):>6}")
        if list_tests:
            lines.extend(f"    {n:>4}  {test}" for test, n in sorted(tests.items()))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python tests/tools/census.py DIR [--tests]", file=sys.stderr)
        return 2
    print(render(aggregate(Path(argv[0])), list_tests="--tests" in argv[1:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
