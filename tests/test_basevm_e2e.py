"""The base VM, live: `fy machine base bake` on a real host, then a project VM created as a CLONE
of it (`fy machine recreate`) that boots, applies foldyard's provisioning and runs containers.

Host tier (tests/e2e_host.py). Run right after test_e2e.py, so the modules after it start from a
cloned VM (each one that recreates the VM again does so from the template — the live CLI modules
run with FOLDYARD_BASE_VM=0 — or, where the session names a base of its own as CI does, from that
one: conftest's isolated_base_vm_stamp). This module bakes its OWN base (`FY_BASE_VM_NAME`, a
per-module stamp) and removes it at the end, so on an operator's machine it never touches their
`foldyard-base`, nor CI's session base.

Spiked on macOS (Lima 2.1.3, vz, APFS copy-on-write) on 2026-10-12; this is the Linux/WSL2 record:
Lima 2.2.0's QEMU driver, where a clone is a plain copy of the disk.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from e2e_host import (
    LIMA_DIR,
    _adopt_on_host,
    engine,
    ensure_vm,
    example_copy,
    export_vm_socket,
    fy,
    fy_ok,
    host_tier,
    lima_shell,
    lima_status,
)

pytestmark = host_tier

BASE = "fy-e2e-base"
BASE_BOX_TAG = "foldyard-base-devbox:latest"  # box.BASE_BOX_TAG


@pytest.fixture(scope="module")
def base_env(tmp_path_factory):
    stamp = tmp_path_factory.mktemp("base-vm") / "base-vm.json"
    env = {"FY_BASE_VM_NAME": BASE, "FY_BASE_VM_STAMP": str(stamp), "FOLDYARD_BASE_VM": "1"}
    yield env
    subprocess.run(["limactl", "delete", "--force", BASE], capture_output=True, timeout=300)


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("basevm"))
    _adopt_on_host(r)
    return r


def _base_status() -> str:
    out = subprocess.run(
        ["limactl", "list", "--json"], capture_output=True, text=True, timeout=30
    ).stdout
    for line in out.splitlines():
        if line.strip() and json.loads(line).get("name") == BASE:
            return str(json.loads(line).get("status", ""))
    return ""


def test_bake_leaves_a_stopped_stamped_base(repo, base_env):
    run = fy_ok(["machine", "base", "bake"], repo, timeout=1800, env_extra=base_env)
    assert "baked" in run.out, run.out
    assert _base_status() == "Stopped"
    stamp = json.loads(Path(base_env["FY_BASE_VM_STAMP"]).read_text())
    assert stamp["inputs"]["template"] and stamp["inputs"]["vmtype"], stamp
    # The packaged box image built in it — or the stamp says it isn't (an empty box input).
    assert stamp["inputs"]["box"], f"the box image build failed during the bake:\n{run.out}"
    status = fy_ok(["machine", "base", "status"], repo, env_extra=base_env)
    assert "✓" in status.out, status.out


def test_a_recreated_vm_is_a_clone_that_works(repo, base_env):
    run = fy_ok(["machine", "recreate", "--yes"], repo, timeout=1800, env_extra=base_env)
    # Recreate ran foldyard's provisioning + its guest-report check on the clone (rc 0 says both
    # passed); the line says it cloned rather than fell back to the template.
    assert f"cloning the base VM '{BASE}'" in run.out, run.out
    assert "creating from the template instead" not in run.out, run.out
    assert lima_status() == "Running"
    # Lima 2.1.3 left the clone's sock/ mode 0000 (no engine); foldyard resets it.
    assert stat.S_IMODE((LIMA_DIR / "sock").stat().st_mode) & 0o700 == 0o700

    # The template's `dnf install podman` did NOT run on this VM: its marker predates this boot,
    # because it came with the base's disk. Read without root (/proc/uptime, stat).
    probe = lima_shell(
        "sh",
        "-c",
        "echo $(( $(date +%s) - $(cut -d. -f1 /proc/uptime) )) $(stat -c %Y /etc/lima-podman)",
    )
    booted_at, marker = probe.stdout.split()
    assert int(marker) < int(booted_at), f"the podman install ran on this boot: {probe.stdout}"

    ensure_vm(repo)  # the clone's engine runs containers
    export_vm_socket()
    assert engine("image", "exists", BASE_BOX_TAG).returncode == 0, "the base's box image"


def test_rm_removes_the_base_and_leaves_the_clone(repo, base_env):
    fy_ok(["machine", "base", "rm"], repo, env_extra=base_env)
    assert _base_status() == ""
    assert not Path(base_env["FY_BASE_VM_STAMP"]).exists()
    # The project VM is an independent disk: still running, still serving.
    assert lima_status() == "Running"
    assert fy(["machine", "ensure"], repo, env_extra=base_env).rc == 0
