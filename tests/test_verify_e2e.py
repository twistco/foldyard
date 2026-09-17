"""`fy verify` — the isolation battery, the product's credibility check — against a REAL VM.

Two claims, both only provable on a VM: the boundary foldyard builds passes (rootless engine, the
`--privileged --pid=host` escape refused, PID 1's mount table free of the operator's paths beyond
the repo mounts), and — the negative `docs/verify-false-pass.md` owed and the rig proved — a VM
that mounts the operator's WHOLE home makes `verify` FAIL, then PASS again once the mount is gone.
Without the negative, a verify that always passes is indistinguishable from one that checks
nothing.

Host tier (see tests/e2e_host.py). Versions this was written against: Lima 2.2.0 (`qemu` on a
Linux host, `vz` on macOS), the podman template's Fedora 44 guest (podman 5.8.4), host podman CLI
4.9.3 (ubuntu-24.04). The mount is added with `limactl edit --set` on the STOPPED instance — mount
sets are init-only in Lima — and removed the same way, in a fixture, so a failing assertion never
leaves the VM leaking the home into the next module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from e2e_host import _adopt_on_host, ensure_vm, example_copy, fy, fy_ok, host_tier, lima_edit

pytestmark = host_tier

HOME = str(Path.home())


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("verify"))
    _adopt_on_host(r)
    ensure_vm(r)
    yield r
    fy(["down"], r, timeout=180)


def test_verify_all_pass_on_the_vm(repo):
    run = fy_ok(["verify"], repo, timeout=300)
    assert "ALL PASS" in run.out, run.out
    for check in (
        "engine is rootless",
        "escape refused",
        "free of host home/paths beyond the repo mounts",
    ):
        assert check in run.out, f"missing {check!r} in:\n{run.out}"


@pytest.fixture
def home_mounted_vm(repo):
    """The VM restarted with the operator's whole home mounted (read-only is enough — the leak
    is the exposure, not the write); restored on the way out whatever the test did."""
    fy_ok(["machine", "stop"], repo, timeout=300)
    lima_edit(f'.mounts += [{{"location": {json.dumps(HOME)}, "writable": false}}]')
    try:
        ensure_vm(repo)
        yield
    finally:
        fy(["machine", "stop"], repo, timeout=300)
        lima_edit(f"del(.mounts[] | select(.location == {json.dumps(HOME)}))")
        ensure_vm(repo)


def test_verify_fails_when_the_vm_mounts_the_operators_home(repo, home_mounted_vm):
    run = fy(["verify"], repo, timeout=300)
    assert run.rc != 0, f"verify PASSED against a VM mounting {HOME}:\n{run.out}"
    assert "VM exposes host paths" in run.out, run.out
    assert HOME in run.out, run.out
    assert "ALL PASS" not in run.out


def test_verify_passes_again_once_the_home_mount_is_gone(repo):
    # Runs after the fixture above restored the mount set — the same battery is green again,
    # which is what makes the FAIL above a property of the VM, not of the run.
    run = fy_ok(["verify"], repo, timeout=300)
    assert "ALL PASS" in run.out, run.out
