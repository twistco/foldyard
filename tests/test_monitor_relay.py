"""assets/monitor/relay.py — the guest monitor's root relay (ADR-0031), its pure parts on a fake
cgroup tree and a fake /proc. The live loop (tailing Tetragon, systemd, the real cgroup2 mount) is
the host-tier e2e's (tests/test_monitor_e2e.py)."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "src/foldyard/assets/monitor/relay.py"
_spec = importlib.util.spec_from_file_location("fy_monitor_relay", _PATH)
assert _spec and _spec.loader
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

BOX = "a" * 64
SIB = "b" * 64
KEY = b"k" * 32


def test_a_spool_line_carries_seq_and_an_hmac_over_boot_seq_and_body():
    line = relay.spool_line(KEY, "boot1", 7, {"k": "ev", "e": {"x": 1}})
    seq, mac, body = line.rstrip("\n").split(" ", 2)
    assert seq == "7"
    want = hmac.new(KEY, f"boot1:7:{body}".encode(), hashlib.sha256).hexdigest()
    assert mac == want
    assert json.loads(body) == {"e": {"x": 1}, "k": "ev"}
    # the boot and seq are under the mac: a line replayed into another boot or slot fails
    assert relay.mac(KEY, "boot2", 7, body) != mac
    assert relay.mac(KEY, "boot1", 8, body) != mac


def test_mnt_inode_reads_the_process_namespace_of_any_event_kind():
    ev = {"process_exec": {"process": {"ns": {"mnt": {"inum": 4026532977}}}}, "time": "t"}
    assert relay.mnt_inode(ev) == 4026532977
    kp = {"process_kprobe": {"process": {"ns": {"mnt": {"inum": 5}}}}}
    assert relay.mnt_inode(kp) == 5
    assert relay.mnt_inode({"process_exec": {"process": {}}}) is None  # ns not enabled


def _cgroup(root: Path, rel: str, pids: list[int] = ()) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))


def _proc(root: Path, pid: int, inode: int) -> None:
    ns = root / str(pid) / "ns"
    ns.mkdir(parents=True)
    (ns / "mnt").symlink_to(f"mnt:[{inode}]")


@pytest.fixture
def tree(tmp_path):
    """A user@<uid>.service subtree and a /proc: the box (payload in the `container`
    sub-cgroup), a sibling, a FORGER whose sub-cgroup is named `libpod-<box id>.scope`, and one
    under a client-chosen --cgroup-parent slice."""
    user, proc = tmp_path / "user", tmp_path / "proc"
    _cgroup(user, f"user.slice/libpod-{BOX}.scope/container", [10, 11])
    _cgroup(user, f"user.slice/libpod-{SIB}.scope/container", [20])
    forger = "c" * 64
    _cgroup(user, f"user.slice/libpod-{forger}.scope/libpod-{BOX}.scope", [30])
    other = "d" * 64
    _cgroup(user, f"fy.slice/fy-forge.slice/libpod-{other}.scope/container", [40])
    _cgroup(user, "app.slice/podman.service", [50])  # not a container
    for pid, inode in ((1, 100), (10, 200), (11, 200), (20, 300), (30, 400), (40, 500), (50, 100)):
        _proc(proc, pid, inode)
    return user, proc, forger, other


def test_scopes_takes_the_outermost_podman_scope_only(tree):
    user, _proc_root, forger, other = tree
    found = relay.scopes(str(user))
    assert sorted(found.values()) == sorted([BOX, SIB, forger, other])
    # the forger's nested `libpod-<box id>.scope` is never read as a scope of its own
    assert all(Path(p).parent.name in ("user.slice", "fy-forge.slice") for p in found)


def test_snapshot_maps_inodes_to_their_own_containers_even_when_forged(tree):
    user, proc, forger, other = tree
    snap = relay.snapshot(str(user), str(proc))
    assert snap["vm"] == 100
    assert snap["map"] == {"200": [BOX], "300": [SIB], "400": [forger], "500": [other]}


def test_snapshot_reports_a_shared_namespace_as_is_never_picking_one(tree):
    user, proc, _forger, _other = tree
    (proc / "20" / "ns" / "mnt").unlink()
    (proc / "20" / "ns" / "mnt").symlink_to("mnt:[200]")  # the sibling shares the box's
    assert relay.snapshot(str(user), str(proc))["map"]["200"] == sorted([BOX, SIB])


def test_snapshot_skips_a_process_that_exited_mid_walk(tree):
    user, proc, _forger, _other = tree
    (proc / "11" / "ns" / "mnt").unlink()
    assert relay.snapshot(str(user), str(proc))["map"]["200"] == [BOX]


def test_the_spool_rolls_segments_and_prunes_the_oldest(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    monkeypatch.setattr(relay, "SPOOL", str(spool))
    monkeypatch.setattr(relay, "SEGMENT_BYTES", 600)
    monkeypatch.setattr(relay, "SPOOL_BYTES", 2000)
    old = spool / "0000000001-oldboot"
    old.mkdir(parents=True)
    (old / "000000000000.jsonl").write_text("x" * 900)
    s = relay.Spool(KEY, "boot1", "0000000002-boot1", 0)
    seqs = [s.write({"k": "ev", "e": {"n": i, "pad": "p" * 100}}) for i in range(20)]
    s.flush()
    assert seqs == list(range(20))
    segs = sorted((spool / "0000000002-boot1").iterdir())
    assert len(segs) > 1
    # segment names are their first seq, so the host can order and resume by name
    assert segs[0].name.endswith(".jsonl") and int(segs[-1].stem) > 0
    total = sum(p.stat().st_size for p in spool.rglob("*.jsonl"))
    assert total <= 2000 + 600  # bounded: the cap plus the segment being written
    assert not old.exists()  # the oldest boot went first, and its empty dir with it
    for p in spool.rglob("*.jsonl"):
        assert oct(p.stat().st_mode & 0o777) == "0o644"  # the VM user can read, not write


def test_a_resumed_spool_appends_to_its_newest_segment(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "SPOOL", str(tmp_path / "spool"))
    s = relay.Spool(KEY, "boot1", "d", 0)
    s.write({"k": "start"})
    s.flush()
    s2 = relay.Spool(KEY, "boot1", "d", 1)
    s2.write({"k": "start"})
    s2.flush()
    files = list((tmp_path / "spool" / "d").iterdir())
    assert len(files) == 1
    assert [ln.split()[0] for ln in files[0].read_text().splitlines()] == ["0", "1"]
