"""The base VM — a project-less Lima VM a new project's VM is CLONED from (`fy machine base`).

A project's first `fy up`/`fy box up` used to create its VM from Lima's podman template: download
the Fedora cloud image, boot it, `dnf install podman` in the guest, then build the box image — every
project, every time, all of it inside the new VM's own disk where the next project can't reach it.
The base VM is that work done ONCE: created from the same template with NOTHING mounted, booted
(so the template's provisioning has run), the packaged generic box image built in it, stopped. A
new project's VM is then a `limactl clone` of it — copy-on-write on APFS, so it takes seconds —
with the project's own `--set` (sizing, ONLY its mounts, the vmType) applied exactly as the
template path would. Everything after the create — foldyard's boot provisioning, the wall, the
guest-report check — runs unchanged, so a clone and a template VM can't be told apart by it.

Rules this module keeps:

* **A clone happens only at creation.** An existing project VM is never re-cloned: its store holds
  the project's images and volumes (logins, databases). Refreshing the base changes only what NEW
  VMs start from. `fy machine recreate` already discards the VM, so it may clone.
* **The base is baked only here, never from a project's VM.** A project VM has run a box, and a
  box runs agents; anything that escaped a container there would be copied into every later
  project. The base is touched only by Lima's own provisioning and foldyard's own packaged build,
  with nothing mounted.
* **Stale is decided by INPUTS; age only warns.** The stamp records what the base was built from:
  the fully resolved template (its image digests included), the vmType, the packaged box's
  fingerprint. A changed template or vmType means no clone (template path, as before); a changed
  box fingerprint alone still clones — `box up` just builds, as it would have anyway. Nothing in
  the base depends on foldyard's own boot script, which is applied per project at each boot.
* **Never delete what we didn't bake.** An instance named :data:`NAME` with no stamp is someone
  else's; bake and rm refuse to touch it.

The stamp lives host-side (``~/.foldyard/base-vm.json``), outside every repo mount. Spiked live on
macOS (Lima 2.1.3, vz) on 2026-10-12: docs/base-vm.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import config
from .machine_backend import LimaBackend, _run

# `FY_BASE_VM_NAME` is a test seam (the host-tier e2e bakes its own, never the operator's).
NAME = os.environ.get("FY_BASE_VM_NAME") or "foldyard-base"
# The base's own size. Small, because a clone can GROW its disk but never shrink it: a project
# asking for less than this is created from the template instead.
DISK_GIB = 10
CPUS = 2
MEMORY_MIB = 4096
STALE_AFTER_DAYS = 30
# `FOLDYARD_BASE_VM=0` turns cloning off (every VM from the template, as before the base).
OPT_OUT = "FOLDYARD_BASE_VM"


def _err(*a: object) -> None:
    print(*a, file=sys.stderr)


def _now() -> datetime:
    return datetime.now(UTC)


# ── the stamp ─────────────────────────────────────────────────────────────────────────────


def stamp_path() -> Path:
    env = os.environ.get("FY_BASE_VM_STAMP")
    return Path(env).expanduser() if env else Path.home() / ".foldyard" / "base-vm.json"


def read_stamp() -> dict | None:
    try:
        data = json.loads(stamp_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("inputs"), dict) else None


def write_stamp(stamp: dict) -> None:
    path = stamp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _clear_stamp() -> None:
    with contextlib.suppress(FileNotFoundError):
        stamp_path().unlink()


def template_digest() -> str:
    """sha256 of the podman template as Lima resolves it, its base templates embedded (so the
    cloud image's location + digest are part of it). ``""`` when limactl can't render it."""
    res = _run(["limactl", "template", "copy", "--embed-all", LimaBackend.TEMPLATE, "-"])
    if res.returncode != 0 or not res.stdout:
        return ""
    return hashlib.sha256(res.stdout.encode()).hexdigest()


def current_inputs(backend: LimaBackend) -> dict:
    """What a base baked NOW would be built from."""
    from . import box

    return {
        "template": template_digest(),
        "vmtype": backend.preferred_vmtype(),
        "box": box.packaged_fingerprint(),
    }


def _lima_version() -> str:
    res = _run(["limactl", "--version"])
    m = re.search(r"(\d+\.\d+\.\d+)", res.stdout or "")
    return m.group(1) if m else ""


_INPUT_NAMES = {
    "template": "Lima's podman template (or its cloud image) changed",
    "vmtype": "the preferred hypervisor changed",
    "box": "foldyard's packaged box image changed",
}


def stale_reasons(stamp: dict, now: dict) -> list[str]:
    """Which of the stamp's inputs differ from ``now``. An input that can't be computed now
    (``""``) is unknown, not changed."""
    baked = stamp.get("inputs", {})
    return [
        f"{_INPUT_NAMES[key]} since it was baked"
        for key in _INPUT_NAMES
        if now.get(key) and baked.get(key) != now[key]
    ]


def age_days(stamp: dict) -> float | None:
    try:
        baked = datetime.fromisoformat(stamp["baked_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (_now() - baked).total_seconds() / 86400


def _age_text(stamp: dict) -> str:
    days = age_days(stamp)
    if days is None:
        return "age unknown"
    whole = int(days)
    return "baked today" if whole == 0 else f"baked {whole} day{'s' * (whole != 1)} ago"


def describe_clone() -> str:
    stamp = read_stamp()
    return _age_text(stamp) if stamp else ""


# ── cloning ───────────────────────────────────────────────────────────────────────────────


def clone_source(backend: LimaBackend, vmtype: str, disk_gib: int) -> tuple[str | None, str]:
    """``(NAME, "")`` when a new VM with this vmType and disk can be cloned from the base, else
    ``(None, why)``. Reads the stamp + the instance + the template; never changes anything."""
    if os.environ.get(OPT_OUT) == "0":
        return None, f"{OPT_OUT}=0"
    stamp = read_stamp()
    if stamp is None:
        return None, "no base VM baked"
    if not backend.exists(NAME):
        return None, f"the base VM '{NAME}' is missing (`fy machine base bake`)"
    state = backend.state(NAME)
    if state != "stopped":
        return None, f"the base VM is {state or 'in an unknown state'}, not stopped"
    baked_vmtype = stamp["inputs"].get("vmtype", "")
    if vmtype != baked_vmtype:
        return None, f"this project wants vmType '{vmtype}', the base is '{baked_vmtype}'"
    if disk_gib < int(stamp.get("disk_gib", DISK_GIB)):
        return None, f"a {disk_gib} GiB disk is smaller than the base's"
    # Only the template decides here: a changed box fingerprint still clones (box up builds).
    vm_stale = stale_reasons(stamp, {"template": current_inputs(backend)["template"]})
    if vm_stale:
        return None, f"{vm_stale[0]} — `fy machine base bake` refreshes it"
    return NAME, ""


def _sock_dir(name: str) -> Path:
    return Path.home() / ".lima" / name / "sock"


def clone(backend: LimaBackend, name: str, set_expr: str) -> bool:
    """`limactl clone` the base into ``name`` with the project's ``--set``; on failure remove the
    partial instance (it did not exist before — ``create`` is only called for a missing VM)."""
    cmd = ["limactl", "clone", "--tty=false", NAME, name, "--set", set_expr]
    if subprocess.run(cmd).returncode != 0:
        if backend.exists(name):
            backend.remove(name)
        return False
    # Lima 2.1.3's clone creates the instance's `sock/` dir mode 0000: the hostagent then can't
    # forward the podman socket into it ("permission denied") and the VM boots with no engine.
    # The template path makes it 0750; match that.
    sock = _sock_dir(name)
    if sock.is_dir() and (sock.stat().st_mode & 0o700) != 0o700:
        sock.chmod(0o750)
    return True


# ── bake / rm / status ────────────────────────────────────────────────────────────────────


def _refuse_in_box() -> bool:
    if config.in_box():
        _err("✗ the base VM is managed from your computer, not from inside the box.")
        return True
    return False


def _not_ours(backend: LimaBackend) -> bool:
    """An instance named NAME that foldyard has no stamp for."""
    return read_stamp() is None and backend.exists(NAME)


def bake(backend: LimaBackend | None = None) -> int:
    """Create (or re-create) the base VM: from the template, nothing mounted, booted once so the
    template's provisioning runs, the packaged box image built in it, stopped, stamped. Direct
    egress, by design: it runs no repo code and mounts nothing — only Lima's provisioning and
    foldyard's own Dockerfile."""
    from . import box

    if _refuse_in_box():
        return 1
    backend = backend or LimaBackend()
    if not backend.available():
        _err(f"✗ the base VM needs `limactl` ({backend.install_hint}).")
        return 1
    if _not_ours(backend):
        _err(f"✗ a Lima instance named '{NAME}' exists that foldyard didn't bake — leaving it")
        _err("  alone. Rename or delete it yourself (`limactl delete`) to let foldyard bake one.")
        return 1
    inputs = current_inputs(backend)
    if backend.exists(NAME):
        _err(f"▶ replacing the base VM '{NAME}'…")
        backend.stop(NAME)
        backend.remove(NAME)
    _clear_stamp()

    resources = {"cpus": str(CPUS), "memory": str(MEMORY_MIB), "disk": str(DISK_GIB)}
    _err(f"▶ baking the base VM '{NAME}' (downloads the VM image if Lima hasn't cached it)…")
    if not backend.create_template(NAME, resources, [], inputs["vmtype"]):
        _err("✗ creating the base VM failed — see the output above.")
        return 1
    if not backend.start(NAME):
        _err("✗ the base VM didn't start — see the output above.")
        backend.stop(NAME)
        return 1

    sock = backend.socket(NAME)
    env = dict(os.environ, CONTAINER_HOST=sock, DOCKER_HOST=sock)
    _err("▶ building foldyard's generic box image in it…")
    if box.build_packaged_image("podman", env, box.BASE_BOX_TAG) != 0:
        _err("⚠ the box image build failed — the base VM still saves the VM's first boot;")
        _err("  each project's `box up` builds its box image as before.")
        inputs["box"] = ""
    backend.stop(NAME)

    from . import __version__

    write_stamp(
        {
            "inputs": inputs,
            "baked_at": _now().isoformat(),
            "disk_gib": DISK_GIB,
            "foldyard": __version__,
            "lima": _lima_version(),
        }
    )
    print(f"✓ base VM '{NAME}' baked: a new project's VM is now a clone of it.")
    return 0


def rm(backend: LimaBackend | None = None) -> int:
    """Delete the base VM and its stamp. Project VMs cloned from it are independent disks and
    are not affected."""
    if _refuse_in_box():
        return 1
    backend = backend or LimaBackend()
    if _not_ours(backend):
        _err(f"✗ the Lima instance '{NAME}' wasn't baked by foldyard — leaving it alone.")
        return 1
    if backend.exists(NAME):
        backend.stop(NAME)
        backend.remove(NAME)
    _clear_stamp()
    print(f"✓ base VM '{NAME}' removed (VMs cloned from it are unaffected).")
    return 0


@dataclass
class Status:
    """One row for `fy prereq` / `fy machine base`: level is ok | warn | optional."""

    level: str
    detail: str
    fix: str = ""


def status(backend: LimaBackend | None = None) -> Status:
    backend = backend or LimaBackend()
    stamp = read_stamp()
    bake_hint = "`fy machine base bake` (or `fy prereq --fetch`)"
    if stamp is None:
        if backend.exists(NAME):
            return Status("warn", f"a Lima instance '{NAME}' exists that foldyard didn't bake")
        return Status(
            "optional",
            "none — each new project's VM downloads + provisions from scratch",
            f"{bake_hint} makes it a clone in seconds",
        )
    if not backend.exists(NAME):
        return Status("warn", "stamped, but the VM is missing", bake_hint)
    reasons = stale_reasons(stamp, current_inputs(backend))
    age = _age_text(stamp)
    if reasons:
        return Status("warn", f"{age}; {'; '.join(reasons)}", f"rebake: {bake_hint}")
    days = age_days(stamp)
    if days is not None and days > STALE_AFTER_DAYS:
        return Status(
            "warn",
            f"{age} — its packages miss the security updates since",
            f"rebake: {bake_hint}",
        )
    return Status("ok", f"{age} ({stamp['inputs'].get('vmtype') or 'Lima default'})")
