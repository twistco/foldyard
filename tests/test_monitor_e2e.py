"""The guest monitor (`[machine] monitor = "observe"`, ADR-0031) on a real VM.

`fy up` with the monitor on records its boot script beside the wall's (the VM is stopped first, so
the recording applies), boots, and — the guest having no release yet — downloads the pinned
Tetragon on the host, streams it into the guest's inbox, and waits for the root `.path` unit to
install it and the policy to load. Then the guest is examined AS THE VM USER, which is the box's
uid: it can't stop the collector, reach its admin socket, read its log or touch its policy, and a
container can't reach its health port. Turning the monitor back off removes it at the next boot.

What this pins that only a real guest can: the packaged policy LOADS on the shipped kernel (one
invalid policy stops Tetragon entirely — spike finding 3), the units and the installer work under
SELinux, and the health server is bound where the box can't reach it (spike finding 4). Validated
by hand on Fedora 44 / kernel 6.19.10 / Tetragon v1.7.1 under QEMU TCG (2026-10-04) before this
module ran in CI.

Host tier (tests/e2e_host.py). The VM is left RUNNING with the monitor's `off` recording, which
every later module's `fy up` (monitor off) already matches — so no restart is forced on them.
"""

from __future__ import annotations

import pytest

from e2e_host import (
    LIMA_DIR,
    _adopt_on_host,
    engine,
    ensure_vm,
    example_copy,
    fy,
    fy_ok,
    host_tier,
    lima_shell,
)

pytestmark = host_tier

ON = {"MACHINE_MONITOR": "observe"}


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("monitor"))
    _adopt_on_host(r)
    ensure_vm(r)
    fy_ok(["machine", "stop"], r, timeout=300)  # the recording applies at boot
    up = fy_ok(["up"], r, timeout=1800, env_extra=ON)
    assert "✓ guest monitor" in up.out, up.out
    yield r
    fy(["down"], r, timeout=180, env_extra=ON)
    fy(["machine", "stop"], r, timeout=300)
    fy(["up"], r, timeout=1800)  # monitor off: the `off` rendering removes it at boot
    fy(["down"], r, timeout=180)


def _guest(cmd: str) -> str:
    r = lima_shell("bash", "-c", cmd, timeout=120)
    return (r.stdout + r.stderr).strip()


def test_both_boot_scripts_are_recorded_under_their_own_markers(repo):
    text = (LIMA_DIR / "lima.yaml").read_text()
    assert "# fy-provision " in text and "# fy-monitor " in text


def test_the_guest_reports_tetragon_observing_with_the_policy_loaded(repo):
    report = _guest("cat /run/fy-monitor/artifact /run/fy-monitor/policy")
    assert "installed v" in report and "loaded fy-observe" in report, report
    assert _guest("systemctl is-active tetragon.service") == "active"


def test_doctor_shows_the_monitor_observing(repo):
    doc = fy(["doctor"], repo, timeout=300, env_extra=ON)
    assert "guest monitor" in doc.out and "observing" in doc.out, doc.out


def test_the_box_uid_cannot_administer_the_collector(repo):
    # The VM user IS the box's uid. Each of these is a door the monitor must keep shut.
    assert "Access denied" in _guest("systemctl stop tetragon.service 2>&1")
    assert _guest("systemctl is-active tetragon.service") == "active"
    assert "permission denied" in _guest("/usr/local/bin/tetra status 2>&1")
    assert "Permission denied" in _guest("cat /var/log/tetragon/tetragon.log 2>&1 >/dev/null")
    assert "Permission denied" in _guest("touch /etc/tetragon/tetragon.tp.d/x.yaml 2>&1")
    assert "password is required" in _guest("sudo -n true 2>&1")


def test_the_health_port_is_out_of_a_containers_reach(repo):
    # Spike finding 4: Tetragon's health server listens on every interface by default, and a
    # container reaches the VM's own address as host.containers.internal.
    assert "127.0.0.1:6789" in _guest("ss -ltn")
    probe = engine(
        "run", "--rm", "docker.io/library/alpine",
        "sh", "-c", "wget -q -T 4 -O /dev/null http://host.containers.internal:6789/ && echo OPEN",
        timeout=120,
    )  # fmt: skip
    assert "OPEN" not in probe.stdout, probe.stdout + probe.stderr


def test_a_containers_activity_reaches_fy_monitor_attributed_to_it(repo):
    # Slice 2 end to end: Tetragon → the root relay's signed spool → the supervisor's pull over
    # ssh → the host-side join. A long-lived container reads a watched file; its event must come
    # back attributed to THAT container's id (from the kernel, not a name or label).
    import json
    import time

    run = engine(
        "run", "-d", "docker.io/library/alpine", "sh", "-c", "sleep 3; cat /etc/shadow; sleep 300",
        timeout=120,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    cid = run.stdout.strip()
    try:
        deadline = time.time() + 180
        hit = None
        out = ""
        while time.time() < deadline and hit is None:
            out = fy(["monitor", "--json", "-n", "5000"], repo, timeout=120, env_extra=ON).out
            for raw in out.splitlines():
                if not raw.startswith("{"):
                    continue
                e = json.loads(raw)
                if e["kind"] == "file" and "/etc/shadow" in e["detail"] and e["who"] == "container":
                    if e["container"] == cid:
                        hit = e
            time.sleep(5)
        assert hit is not None, f"no attributed /etc/shadow read from {cid[:12]}:\n{out[-3000:]}"
        doc = fy(["doctor"], repo, timeout=300, env_extra=ON).out
        assert "monitor events" in doc and "0 failed the signature" in doc, doc
    finally:
        engine("rm", "-f", cid, timeout=60)


def test_the_box_uid_cannot_write_the_spool_or_read_the_key(repo):
    assert "Permission denied" in _guest("touch /var/lib/fy-monitor/spool/x 2>&1")
    assert "Permission denied" in _guest("cat /etc/fy-monitor/relay.key 2>&1")
    assert "Access denied" in _guest("systemctl stop fy-monitor-relay.service 2>&1")


def test_a_junk_delivery_is_rejected_beside_the_running_install(repo):
    # Anything holding the inbox's uid can drop a file there. It must never be installed, and
    # never turn a healthy monitor's report red.
    _guest(
        "echo junk > /var/lib/fy-monitor/inbox/tetragon.tar.gz.part && "
        "mv /var/lib/fy-monitor/inbox/tetragon.tar.gz.part /var/lib/fy-monitor/inbox/tetragon.tar.gz"
    )
    import time

    deadline = time.time() + 60
    while time.time() < deadline and "mismatch" not in _guest("cat /run/fy-monitor/rejected"):
        time.sleep(2)
    assert "checksum mismatch" in _guest("cat /run/fy-monitor/rejected")
    assert _guest("cat /run/fy-monitor/artifact").startswith("installed")
    assert _guest("systemctl is-active tetragon.service") == "active"
