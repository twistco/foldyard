"""Not a live test — it runs everywhere, as a module conftest classifies as LIVE: one that drives
the real CLI (it imports test_e2e's runner, as every host-tier module does through e2e_host). It
pins that conftest's per-test isolation env never reaches such a module.

Its CLI subprocesses, and the supervisor the first `fy up` leaves running, inherit the test
process's env. A var pointing at a per-test tmp dir therefore splits one host's state across the
tests of a module: `fy worktree add` registered a worktree in one test's registry and the next
test's `fy up` read another (the worktree registry), and the example project was handed the first
port band of an empty registry — on a host with real projects, another project's VM ssh port.
The value pins (the engine, the test clock) are host state the same way."""

from __future__ import annotations

import os

from conftest import SESSION_ENV
from test_e2e import _foldyard  # noqa: F401  (what makes this module a live, CLI-driving one)

PINS = ("FOLDYARD_ENGINE", "FOLDYARD_CLOCK_OFFSET")


def test_no_per_test_state_reaches_a_live_module(tmp_path_factory):
    base = os.path.realpath(tmp_path_factory.getbasetemp())
    per_test = {
        name: value
        for name, value in os.environ.items()
        if os.path.isabs(value) and os.path.realpath(value).startswith(base + os.sep)
    }
    assert per_test == {}


def test_no_value_pin_reaches_a_live_module():
    assert {name: os.environ.get(name) for name in PINS} == {
        name: SESSION_ENV.get(name) for name in PINS
    }
