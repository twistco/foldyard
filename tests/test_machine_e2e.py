"""`fy machine` — the VM lifecycle, live: `ensure` is idempotent; `stop` stops the supervisor with
the VM (the heartbeat goes stale) and keeps everything inside it; a VM whose hypervisor died
underneath its lifecycle flag is recovered by `ensure` (DEVELOPMENT.md: `machine.state()` is a
flag, not liveness — `ensure` must pair it with a real connect and restart, never hand out a
socket nothing serves); `recreate` boots a fresh VM.

Host tier (tests/e2e_host.py). Lima 2.2.0's QEMU driver on Linux: the hypervisor is
`qemu-system-x86_64`, its pid in `~/.lima/<name>/qemu.pid`; killing it is the "host crash /
battery death" the revive path exists for. The module ends with the VM running and un-walled.
"""

from __future__ import annotations

import os
import signal
import time

import pytest

from e2e_host import (
    LIMA_DIR,
    _adopt_on_host,
    engine,
    ensure_vm,
    example_copy,
    fy,
    fy_ok,
    heartbeat_age,
    heartbeat_file,
    host_tier,
    lima_status,
    socket_alive,
    wait_for,
)

pytestmark = host_tier


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("machine"))
    _adopt_on_host(r)
    ensure_vm(r)
    yield r
    ensure_vm(r)  # whatever a failing test left: the next module expects a running VM
    fy(["down"], r, timeout=180)


def test_ensure_is_idempotent_on_a_running_vm(repo):
    first = fy_ok(["machine", "ensure"], repo, timeout=300)
    assert "initialising" not in first.out, first.out  # it already exists
    second = fy_ok(["machine", "ensure"], repo, timeout=300)
    assert "initialising" not in second.out and "starting" not in second.out, second.out
    assert lima_status() == "Running"
    assert socket_alive()


def test_stop_stops_the_supervisor_too_and_keeps_the_vm(repo):
    fy_ok(["up"], repo)
    wait_for(
        lambda: (a := heartbeat_age()) is not None and a < 15,
        timeout=60,
        what="a fresh supervisor heartbeat after up",
    )
    stopped = fy_ok(["machine", "stop"], repo, timeout=300)
    assert "host supervisor stopped" in stopped.out, stopped.out
    assert lima_status() == "Stopped", lima_status()
    assert not socket_alive()
    # No supervisor ⇒ the heartbeat file stops moving.
    before = heartbeat_file().stat().st_mtime
    time.sleep(6)
    assert heartbeat_file().stat().st_mtime == before, "heartbeat still ticking after machine stop"
    ensure_vm(repo)
    assert lima_status() == "Running"
    assert engine("info").returncode == 0
    fy_ok(["down"], repo, timeout=180)


def test_ensure_recovers_a_vm_whose_hypervisor_died(repo):
    pid = int((LIMA_DIR / "qemu.pid").read_text().strip())
    os.kill(pid, signal.SIGKILL)
    # Lima's hostagent usually notices the driver exit and tears its half down (flag → Stopped,
    # sockets gone). Give it a moment, but do NOT require it: the case `ensure` exists for is the
    # one where the flag still says running — it must pair the flag with a real probe and restart.
    deadline = time.time() + 30
    while time.time() < deadline and lima_status() == "Running" and socket_alive():
        time.sleep(1)
    recovered = fy_ok(["machine", "ensure"], repo, timeout=600)
    assert lima_status() == "Running", recovered.out
    assert socket_alive(), recovered.out
    info = engine("info")
    assert info.returncode == 0, f"engine dead after ensure:\n{info.stderr}\n{recovered.out}"


def test_recreate_boots_a_fresh_vm(repo):
    recreated = fy_ok(["machine", "recreate", "--yes"], repo, timeout=900)
    assert lima_status() == "Running", recreated.out
    assert socket_alive()
    info = engine("info")
    assert info.returncode == 0, info.stderr
    # Fresh disk: the example's images are gone — the next `fy up` rebuilds and re-pulls.
    images = engine("images", "--format", "{{.Repository}}")
    assert "fyex_api" not in images.stdout, images.stdout
