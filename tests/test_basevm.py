"""basevm.py — the baked base VM a new project's VM is CLONED from.

`limactl` and the engine are mocked: these pin the stamp, the clone-eligibility rules, the exact
command shapes (clone, bake) and the fall-backs. The live behaviour (a clone booting with the
project's mounts, skipping the template's dnf step, carrying the box image) was spiked on a macOS
host on 2026-10-12 — see docs/base-vm.md — and is the host tier's to keep proving.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foldyard import basevm, box, config
from foldyard import machine_backend as mb


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
INPUTS = {"template": "t" * 64, "vmtype": "vz", "box": "b" * 64}


def _stamp(**over) -> dict:
    stamp = {
        "inputs": dict(INPUTS),
        "baked_at": (NOW - timedelta(days=2)).isoformat(),
        "disk_gib": basevm.DISK_GIB,
        "foldyard": "0.4.0",
        "lima": "2.1.3",
    }
    stamp.update(over)
    return stamp


class _FakeLima(mb.LimaBackend):
    """A LimaBackend whose instance table is a dict: name → state."""

    def __init__(self, instances: dict[str, str] | None = None):
        self.instances = dict(instances or {})
        self.removed: list[str] = []

    def exists(self, name):
        return name in self.instances

    def state(self, name):
        return self.instances.get(name, "")

    def preferred_vmtype(self):
        return "vz"

    def remove(self, name):
        self.removed.append(name)
        self.instances.pop(name, None)
        return True


@pytest.fixture
def wired(monkeypatch):
    """A fresh, stopped, stamped base whose inputs match the host's."""
    monkeypatch.setattr(basevm, "_now", lambda: NOW)
    monkeypatch.setattr(basevm, "current_inputs", lambda backend: dict(INPUTS))
    basevm.write_stamp(_stamp())
    return _FakeLima({basevm.NAME: "stopped"})


# ── the stamp ─────────────────────────────────────────────────────────────────────────────


def test_stamp_lives_outside_the_repo_and_is_isolated_per_test(tmp_path):
    # conftest points FY_BASE_VM_STAMP at a per-test file: no test reads the real one.
    assert basevm.stamp_path().is_relative_to(tmp_path.parent)


def test_stamp_round_trips_and_a_corrupt_one_reads_as_absent():
    assert basevm.read_stamp() is None
    basevm.write_stamp(_stamp())
    assert basevm.read_stamp() == _stamp()
    basevm.stamp_path().write_text("{not json")
    assert basevm.read_stamp() is None


def test_template_digest_hashes_the_fully_resolved_template(monkeypatch):
    seen = {}

    def fake_run(cmd, timeout=None):
        seen["cmd"] = cmd
        return _Proc(0, "images: [fedora-44]\n")

    monkeypatch.setattr(basevm, "_run", fake_run)
    digest = basevm.template_digest()
    # --embed-all: the base templates (and so the image digests) are part of what's hashed.
    assert seen["cmd"][:4] == ["limactl", "template", "copy", "--embed-all"]
    assert mb.LimaBackend.TEMPLATE in seen["cmd"]
    assert len(digest) == 64
    monkeypatch.setattr(basevm, "_run", lambda cmd, timeout=None: _Proc(1))
    assert basevm.template_digest() == ""


# ── staleness: the inputs decide, age only warns ─────────────────────────────────────────


def test_matching_inputs_are_fresh():
    assert basevm.stale_reasons(_stamp(), INPUTS) == []


@pytest.mark.parametrize(
    ("key", "says"),
    [("template", "Lima's podman template"), ("vmtype", "hypervisor"), ("box", "box image")],
)
def test_each_changed_input_is_named(key, says):
    now = dict(INPUTS, **{key: "changed"})
    reasons = basevm.stale_reasons(_stamp(), now)
    assert len(reasons) == 1 and says in reasons[0]


def test_an_unrenderable_template_is_not_called_stale():
    # limactl couldn't render the template (offline? older Lima?) — unknown is not "changed".
    assert basevm.stale_reasons(_stamp(), dict(INPUTS, template="")) == []


def test_age_is_reported_in_days(monkeypatch):
    monkeypatch.setattr(basevm, "_now", lambda: NOW)
    assert basevm.age_days(_stamp()) == pytest.approx(2.0)
    assert basevm.age_days(_stamp(baked_at="garbage")) is None


# ── clone eligibility ─────────────────────────────────────────────────────────────────────


def test_a_fresh_stopped_base_is_the_clone_source(wired):
    src, why = basevm.clone_source(wired, vmtype="vz", disk_gib=60)
    assert src == basevm.NAME, why


def test_no_stamp_means_no_clone(wired):
    basevm.stamp_path().unlink()
    src, why = basevm.clone_source(wired, vmtype="vz", disk_gib=60)
    assert src is None and "no base VM" in why


def test_a_stamp_without_its_instance_means_no_clone(wired):
    wired.instances.clear()
    src, why = basevm.clone_source(wired, vmtype="vz", disk_gib=60)
    assert src is None and "missing" in why


def test_a_running_base_is_never_cloned(wired):
    # The base is only ever cloned stopped: a live disk mid-write is not a consistent copy.
    wired.instances[basevm.NAME] = "running"
    src, why = basevm.clone_source(wired, vmtype="vz", disk_gib=60)
    assert src is None and "running" in why


def test_a_different_hypervisor_means_no_clone(wired):
    src, why = basevm.clone_source(wired, vmtype="krunkit", disk_gib=60)
    assert src is None and "krunkit" in why


def test_a_disk_smaller_than_the_base_means_no_clone(wired):
    # A clone can grow its disk, never shrink it.
    src, why = basevm.clone_source(wired, vmtype="vz", disk_gib=basevm.DISK_GIB - 1)
    assert src is None and "disk" in why


def test_a_changed_template_means_no_clone(wired, monkeypatch):
    monkeypatch.setattr(basevm, "current_inputs", lambda backend: dict(INPUTS, template="new"))
    src, why = basevm.clone_source(wired, vmtype="vz", disk_gib=60)
    assert src is None and "template" in why


def test_a_stale_box_image_alone_still_clones(wired, monkeypatch):
    # The VM half is still good; `box up` just builds the box image as it would have anyway.
    monkeypatch.setattr(basevm, "current_inputs", lambda backend: dict(INPUTS, box="new"))
    src, _ = basevm.clone_source(wired, vmtype="vz", disk_gib=60)
    assert src == basevm.NAME


def test_the_opt_out_disables_cloning(wired, monkeypatch):
    monkeypatch.setenv(basevm.OPT_OUT, "0")
    src, why = basevm.clone_source(wired, vmtype="vz", disk_gib=60)
    assert src is None and basevm.OPT_OUT in why


# ── the clone itself ──────────────────────────────────────────────────────────────────────


def _lima_home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr(basevm.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path / ".lima"


def test_clone_runs_limactl_clone_with_the_projects_set_expr(monkeypatch, tmp_path):
    lima = _lima_home(monkeypatch, tmp_path)
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        (lima / "acme" / "sock").mkdir(parents=True)
        (lima / "acme" / "sock").chmod(0)
        return _Proc(0)

    monkeypatch.setattr(basevm.subprocess, "run", fake_run)
    assert basevm.clone(_FakeLima(), "acme", ".cpus = 4 | .mounts = []") is True
    assert seen["cmd"] == [
        "limactl",
        "clone",
        "--tty=false",
        basevm.NAME,
        "acme",
        "--set",
        ".cpus = 4 | .mounts = []",
    ]


def test_clone_repairs_the_sock_dir_lima_creates_unreadable(monkeypatch, tmp_path):
    # Lima 2.1.3's `clone` makes <instance>/sock mode 0000, so the hostagent can't forward the
    # podman socket into it and the VM comes up with no engine. Spiked live 2026-10-12.
    lima = _lima_home(monkeypatch, tmp_path)

    def fake_run(cmd, *a, **k):
        (lima / "acme" / "sock").mkdir(parents=True)
        (lima / "acme" / "sock").chmod(0)
        return _Proc(0)

    monkeypatch.setattr(basevm.subprocess, "run", fake_run)
    basevm.clone(_FakeLima(), "acme", "")
    assert stat.S_IMODE((lima / "acme" / "sock").stat().st_mode) == 0o750


def test_a_failed_clone_cleans_up_its_partial_instance(monkeypatch, tmp_path):
    _lima_home(monkeypatch, tmp_path)
    backend = _FakeLima()

    def fake_run(cmd, *a, **k):
        backend.instances["acme"] = "stopped"  # limactl got as far as the instance dir
        return _Proc(1)

    monkeypatch.setattr(basevm.subprocess, "run", fake_run)
    assert basevm.clone(backend, "acme", "") is False
    assert backend.removed == ["acme"]


# ── LimaBackend.create: clone when it can, the template otherwise ────────────────────────


def test_create_clones_from_a_usable_base(monkeypatch, fresh_config):
    calls = []
    monkeypatch.setattr(basevm, "clone_source", lambda be, vmtype, disk_gib: (basevm.NAME, ""))
    monkeypatch.setattr(basevm, "clone", lambda be, name, expr: calls.append(name) or True)
    monkeypatch.setattr(basevm, "describe_clone", lambda: "baked 2 days ago")
    monkeypatch.setattr(mb.LimaBackend, "resolve_vmtype", lambda self: "vz")
    monkeypatch.setattr(mb.subprocess, "run", lambda *a, **k: pytest.fail("template path ran"))
    ok = mb.LimaBackend().create("acme", {"cpus": "4", "memory": "8192", "disk": "60"}, [])
    assert ok is True and calls == ["acme"]


def test_create_falls_back_to_the_template_when_the_clone_fails(monkeypatch, fresh_config):
    seen = {}
    monkeypatch.setattr(basevm, "clone_source", lambda be, vmtype, disk_gib: (basevm.NAME, ""))
    monkeypatch.setattr(basevm, "clone", lambda be, name, expr: False)
    monkeypatch.setattr(basevm, "describe_clone", lambda: "")
    monkeypatch.setattr(mb.LimaBackend, "resolve_vmtype", lambda self: "vz")

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _Proc(0)

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    assert mb.LimaBackend().create("acme", {"cpus": "4", "memory": "8192", "disk": "60"}, [])
    assert mb.LimaBackend.TEMPLATE in seen["cmd"]


def test_create_says_how_to_make_the_next_one_fast(monkeypatch, fresh_config, capsys):
    monkeypatch.setattr(
        basevm, "clone_source", lambda be, vmtype, disk_gib: (None, "no base VM baked")
    )
    monkeypatch.setattr(mb.LimaBackend, "resolve_vmtype", lambda self: "vz")
    monkeypatch.setattr(mb.subprocess, "run", lambda *a, **k: _Proc(0))
    mb.LimaBackend().create("acme", {"cpus": "4", "memory": "8192", "disk": "60"}, [])
    err = capsys.readouterr().err
    assert "downloads the VM image" in err and "fy machine base bake" in err


# ── bake ──────────────────────────────────────────────────────────────────────────────────


class _BakeLima(_FakeLima):
    def __init__(self, instances=None, *, start_ok=True):
        super().__init__(instances)
        self.log: list[str] = []
        self.start_ok = start_ok

    def available(self):
        return True

    def create_template(self, name, resources, volumes, vmtype):
        self.log.append(f"create {name} mounts={volumes} disk={resources['disk']} {vmtype}")
        self.instances[name] = "stopped"
        return True

    def start(self, name, prefix=()):
        self.log.append(f"start {name}")
        self.instances[name] = "running"
        return self.start_ok

    def stop(self, name):
        self.log.append(f"stop {name}")
        self.instances[name] = "stopped"
        return True

    def socket(self, name):
        return f"unix:///lima/{name}/sock/podman.sock"


@pytest.fixture
def baking(monkeypatch):
    monkeypatch.setattr(basevm, "_now", lambda: NOW)
    monkeypatch.setattr(basevm, "current_inputs", lambda backend: dict(INPUTS))
    monkeypatch.setattr(basevm, "_lima_version", lambda: "2.1.3")
    monkeypatch.setattr(config, "in_box", lambda: False)
    builds = []

    def fake_build(engine, env, tag):
        builds.append((engine, env.get("CONTAINER_HOST"), tag))
        return 0

    monkeypatch.setattr(box, "build_packaged_image", fake_build)
    return builds


def test_bake_creates_an_unmounted_vm_builds_the_box_and_stamps_it(baking):
    backend = _BakeLima()
    assert basevm.bake(backend) == 0
    assert backend.log == [
        f"create {basevm.NAME} mounts=[] disk={basevm.DISK_GIB} vz",
        f"start {basevm.NAME}",
        f"stop {basevm.NAME}",
    ]
    # The box image is built in the base, over the base's own socket, under the base's tag.
    assert baking == [("podman", f"unix:///lima/{basevm.NAME}/sock/podman.sock", box.BASE_BOX_TAG)]
    stamp = basevm.read_stamp()
    assert stamp is not None
    assert stamp["inputs"] == INPUTS and stamp["disk_gib"] == basevm.DISK_GIB
    assert stamp["baked_at"] == NOW.isoformat()


def test_rebake_replaces_its_own_base(baking):
    basevm.write_stamp(_stamp())
    backend = _BakeLima({basevm.NAME: "stopped"})
    assert basevm.bake(backend) == 0
    assert backend.removed == [basevm.NAME]


def test_bake_never_deletes_an_instance_it_did_not_bake(baking, capsys):
    # No stamp but an instance of that name: someone else's VM. Refuse, touch nothing.
    backend = _BakeLima({basevm.NAME: "running"})
    assert basevm.bake(backend) == 1
    assert backend.removed == [] and backend.log == []
    assert "didn't bake" in capsys.readouterr().err


def test_a_failed_bake_leaves_no_stamp(baking):
    backend = _BakeLima(start_ok=False)
    assert basevm.bake(backend) == 1
    assert basevm.read_stamp() is None


def test_a_failed_box_build_still_stamps_the_vm_half(baking, monkeypatch, capsys):
    # The VM is the part that saves the dnf + boot; the box image is a bonus a stale fingerprint
    # already handles. A build failure is reported, and the base still stands.
    monkeypatch.setattr(box, "build_packaged_image", lambda engine, env, tag: 1)
    backend = _BakeLima()
    assert basevm.bake(backend) == 0
    stamp = basevm.read_stamp()
    assert stamp is not None and stamp["inputs"]["box"] == ""
    assert "box image" in capsys.readouterr().err


def test_bake_refuses_in_the_box(baking, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    backend = _BakeLima()
    assert basevm.bake(backend) == 1
    assert backend.log == []


# ── rm ────────────────────────────────────────────────────────────────────────────────────


def test_rm_deletes_the_base_and_its_stamp(wired, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(_FakeLima, "stop", lambda self, name: True, raising=False)
    assert basevm.rm(wired) == 0
    assert wired.removed == [basevm.NAME] and basevm.read_stamp() is None


def test_rm_leaves_an_unstamped_instance_alone(monkeypatch, capsys):
    monkeypatch.setattr(config, "in_box", lambda: False)
    backend = _FakeLima({basevm.NAME: "stopped"})
    assert basevm.rm(backend) == 1
    assert backend.removed == []


# ── status ────────────────────────────────────────────────────────────────────────────────


def test_status_of_a_fresh_base(wired):
    st = basevm.status(wired)
    assert st.level == "ok" and "2 days" in st.detail


def test_status_of_an_old_base_warns(wired):
    basevm.write_stamp(_stamp(baked_at=(NOW - timedelta(days=45)).isoformat()))
    st = basevm.status(wired)
    assert st.level == "warn" and "45 days" in st.detail and "bake" in st.fix


def test_status_without_a_base_is_optional(wired):
    basevm.stamp_path().unlink()
    wired.instances.clear()
    st = basevm.status(wired)
    assert st.level == "optional" and "fy machine base bake" in st.fix


def test_status_of_an_instance_we_did_not_bake_warns(wired):
    basevm.stamp_path().unlink()
    st = basevm.status(wired)
    assert st.level == "warn" and "didn't bake" in st.detail


def test_status_json_is_plain_data(wired):
    json.dumps(basevm.status(wired).__dict__)  # rendered by prereq + `fy machine base`
