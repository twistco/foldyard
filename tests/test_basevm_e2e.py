"""The base VM, live: `fy machine base bake` on a real host, then a project VM created as a CLONE
of it (`fy machine recreate`) that boots, applies foldyard's provisioning and runs containers.

Host tier (tests/e2e_host.py). Which base it drives depends on the session:

* **A session base** (both `FY_BASE_VM_NAME` and `FY_BASE_VM_STAMP` set — the CI host tiers, which
  bake `fy-ci-base` before pytest and clone every project VM in the job from it): this module
  CHECKS that base — stopped, stamped, `fy machine base status` content — instead of baking a
  second one, and recreates the project VM as a clone of it. The trade-off, accepted to save a
  template boot per run (~2 min on WSL2): the module no longer proves a bake from scratch on its
  own; the job's setup step is that proof (it runs the same `fy machine base bake`, and a failed
  bake fails the job before any module). `rm` is then proven on a stand-in — a stamped instance
  created but never booted — because removing the session's base would leave every later module's
  recreate to the template.
* **No session base** (a local run): it bakes its OWN (`fy-e2e-base`, a per-module stamp), clones
  it, and removes it with `fy machine base rm` at the end — never touching the operator's
  `foldyard-base`. The modules after it then recreate from the template (the live CLI modules run
  with FOLDYARD_BASE_VM=0: conftest's isolated_base_vm_stamp).

Spiked on macOS (Lima 2.1.3, vz, APFS copy-on-write) on 2026-10-12; this is the Linux/WSL2 record:
Lima 2.2.0's QEMU driver, where a clone is a plain copy of the disk.
"""

from __future__ import annotations

import json
import os
import shutil
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
    lima_list,
    lima_shell,
    lima_status,
    session_base_vm,
)

pytestmark = host_tier

# Read at import, as every session switch is (before any fixture touches the environment).
SESSION = session_base_vm(os.environ)
BASE = SESSION.name if SESSION else "fy-e2e-base"
STAND_IN = "fy-e2e-base-rm"  # the session-base run's `rm` subject (see the docstring)
BASE_BOX_TAG = "foldyard-base-devbox:latest"  # boximage.BASE_BOX_TAG
TEMPLATE = "template://podman"  # machine_backend.LimaBackend.TEMPLATE


def _env(name: str, stamp: Path) -> dict[str, str]:
    return {"FY_BASE_VM_NAME": name, "FY_BASE_VM_STAMP": str(stamp), "FOLDYARD_BASE_VM": "1"}


@pytest.fixture(scope="module")
def base_env(tmp_path_factory):
    if SESSION:
        yield _env(SESSION.name, SESSION.stamp)  # the session's: never removed here
        return
    stamp = tmp_path_factory.mktemp("base-vm") / "base-vm.json"
    yield _env(BASE, stamp)
    subprocess.run(["limactl", "delete", "--force", BASE], capture_output=True, timeout=300)


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("basevm"))
    _adopt_on_host(r)
    return r


def _status(name: str) -> str:
    for inst in lima_list():
        if inst.get("name") == name:
            return str(inst.get("status", ""))
    return ""


def test_bake_leaves_a_stopped_stamped_base(repo, base_env):
    if not SESSION:  # a session base was baked by the job's setup step, the same verb
        run = fy_ok(["machine", "base", "bake"], repo, timeout=1800, env_extra=base_env)
        assert "baked" in run.out, run.out
    assert _status(BASE) == "Stopped"
    stamp = json.loads(Path(base_env["FY_BASE_VM_STAMP"]).read_text())
    assert stamp["inputs"]["template"] and stamp["inputs"]["vmtype"], stamp
    # The packaged box image built in it — or the stamp says it isn't (an empty box input).
    assert stamp["inputs"]["box"], f"the box image build failed during the bake: {stamp}"
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


@pytest.fixture
def rm_env(base_env, tmp_path):
    """What `rm` removes. Without a session base: this module's own. With one: a stand-in —
    created from the template but never booted (no disk is made until a start), stamped with a
    copy of the session's stamp — so `rm` runs its real path (ours by the stamp, stop, delete,
    unstamp) while the session's base stays for the modules after this one."""
    if not SESSION:
        yield base_env
        return
    created = subprocess.run(
        ["limactl", "create", "--tty=false", f"--name={STAND_IN}", TEMPLATE],
        capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert created.returncode == 0, created.stdout + created.stderr
    stamp = tmp_path / "stand-in-base-vm.json"
    shutil.copyfile(SESSION.stamp, stamp)
    yield _env(STAND_IN, stamp)
    subprocess.run(["limactl", "delete", "--force", STAND_IN], capture_output=True, timeout=300)


def test_rm_removes_the_base_and_leaves_the_clone(repo, rm_env):
    name = rm_env["FY_BASE_VM_NAME"]
    assert _status(name), f"no '{name}' to remove"
    fy_ok(["machine", "base", "rm"], repo, env_extra=rm_env)
    assert _status(name) == ""
    assert not Path(rm_env["FY_BASE_VM_STAMP"]).exists()
    # The project VM is an independent disk: still running, still serving.
    assert lima_status() == "Running"
    assert fy(["machine", "ensure"], repo, env_extra=rm_env).rc == 0
    if SESSION:  # and the session's base is untouched, for the modules after this one
        assert _status(SESSION.name) == "Stopped"
        assert SESSION.stamp.exists()
