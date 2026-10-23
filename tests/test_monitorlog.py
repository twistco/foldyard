"""monitorlog.py — the guest monitor's events on the host (ADR-0032, slice 2): the pull's
verification and cursor, and the join that attributes events to containers. Spool lines are made
with the relay's OWN signing code (assets/monitor/relay.py), so the two sides can't drift apart
silently. The fetch script itself runs only in the guest (it needs find/stat/tail, which the
hermetic PATH lacks) — `bash -n` here, a real run in tests/test_monitor_e2e.py."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from foldyard import monitorlog

_PATH = Path(__file__).resolve().parents[1] / "src/foldyard/assets/monitor/relay.py"
_spec = importlib.util.spec_from_file_location("fy_monitor_relay_for_log", _PATH)
assert _spec and _spec.loader
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

KEY = "ab" * 32
BOOT = (
    "6cd62655-18eb-4152-a498-afd889806b8d"  # boot ids carry hyphens: the dir parse must keep them
)
DIR = f"1791100000-{BOOT}"
BOX, SIB, OTHER = "a" * 64, "b" * 64, "c" * 64


def line(seq: int, rec: dict, key: str = KEY, boot: str = BOOT) -> bytes:
    return relay.spool_line(key.encode(), boot, seq, rec).encode()


def chunk(path: str, off: int, data: bytes) -> bytes:
    return f"@@ {path} {off} {len(data)}\n".encode() + data


def ev(
    inode: int,
    binary: str = "/bin/cat",
    kind: str = "process_exec",
    ret: int | None = None,
    **extra,
) -> dict:
    proc = {"binary": binary, "pid": 7, "uid": 30033, "ns": {"mnt": {"inum": inode}}}
    if ret is not None:
        extra["return"] = {"int_arg": ret}
    return {"k": "ev", "e": {kind: {"process": proc, **extra}, "time": "2026-10-04T10:00:00Z"}}


def snap(mapping: dict[int, list[str]], vm: int = 1, t: float | None = None) -> dict:
    rec = {"k": "scopes", "vm": vm, "map": {str(k): v for k, v in mapping.items()}}
    return rec if t is None else {**rec, "t": t}


T_BEFORE = 1791106200.0  # 2026-10-04T09:30:00Z: after the box's init started, before the next's


# ── the fetch ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cursor", [("", 0), (f"{DIR}/000000000000.jsonl", 1234)])
def test_the_fetch_script_is_valid_bash(cursor):
    if shutil.which("bash") is None:
        pytest.skip("no bash on this host")
    script = monitorlog.fetch_script(*cursor)
    res = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr


def test_the_fetch_script_only_reads():
    script = monitorlog.fetch_script("", 0)
    for verb in ("rm ", "mv ", ">", "tee", "truncate", "chmod"):
        assert verb not in script.replace("2>/dev/null", ""), verb


def test_the_pull_reuses_one_ssh_connection(monkeypatch, tmp_path):
    # a fresh login per pull is itself watched activity in the guest (sshd, PAM): multiplex
    monkeypatch.setattr(monitorlog.Path, "home", staticmethod(lambda: tmp_path))
    from foldyard.machine_backend import SshTarget

    argv = monitorlog._fetch_argv(SshTarget(user="u", port=2222, identity="/k"))
    assert argv[-1] == "u@127.0.0.1"
    joined = " ".join(argv)
    assert "ControlMaster=auto" in joined and "ControlPersist=300" in joined
    assert f"ControlPath={tmp_path}/.foldyard/cm-%C" in joined


def test_parse_fetch_takes_exactly_the_promised_bytes_even_multibyte():
    a = line(0, ev(5, binary="/tmp/naïve-Δ"))  # non-ASCII: byte counts, never character counts
    b = line(1, {"k": "start"})
    out = chunk(f"{DIR}/000000000000.jsonl", 0, a) + chunk(f"{DIR}/000000000001.jsonl", 0, b)
    assert monitorlog.parse_fetch(out) == [
        (f"{DIR}/000000000000.jsonl", 0, a),
        (f"{DIR}/000000000001.jsonl", 0, b),
    ]


# ── verify + ingest ─────────────────────────────────────────────────────────────────────


def test_ingest_stores_verified_records_and_advances_past_complete_lines():
    cur = monitorlog.Cursor()
    data = line(0, {"k": "start"}) + line(1, ev(5)) + b"2 deadbeef {"  # a half-written third
    recs = monitorlog.ingest(cur, KEY.encode(), [(f"{DIR}/000000000000.jsonl", 0, data)], 10.0)
    assert [r["k"] for r in recs] == ["start", "ev"]
    assert all(r["boot"] == BOOT and r["recv"] == 10.0 for r in recs)
    assert cur.path == f"{DIR}/000000000000.jsonl"
    assert cur.off == len(line(0, {"k": "start"}) + line(1, ev(5)))  # not past the half-line
    assert cur.last == {BOOT: 1}


def test_ingest_records_a_gap_and_skips_what_it_already_stored():
    cur = monitorlog.Cursor(last={BOOT: 1})
    data = line(1, {"k": "start"}) + line(4, ev(5))
    recs = monitorlog.ingest(cur, KEY.encode(), [(f"{DIR}/000000000000.jsonl", 0, data)], 1.0)
    assert [r["k"] for r in recs] == ["gap", "ev"]
    assert recs[0]["from"] == 2 and recs[0]["to"] == 3
    assert cur.gaps == 2


@pytest.mark.parametrize(
    "bad",
    [
        line(0, ev(5), key="cd" * 32),  # signed with another key
        line(0, ev(5), boot="another-boot"),  # replayed from another boot
        line(0, ev(5)).replace(b"/bin/cat", b"/bin/sh"),  # altered in transit
        b"0 nothex {}\n",
    ],
)
def test_a_line_that_fails_its_signature_is_never_stored(bad):
    cur = monitorlog.Cursor()
    recs = monitorlog.ingest(cur, KEY.encode(), [(f"{DIR}/000000000000.jsonl", 0, bad)], 1.0)
    assert recs == [{"k": "forged", "boot": BOOT, "path": f"{DIR}/000000000000.jsonl", "recv": 1.0}]
    assert cur.forged == 1 and cur.last == {}


def test_a_line_moved_to_another_seq_fails_too():
    good = line(3, ev(5))
    moved = b"4" + good[1:]  # same body and mac, claimed at seq 4
    cur = monitorlog.Cursor()
    recs = monitorlog.ingest(cur, KEY.encode(), [(f"{DIR}/000000000000.jsonl", 0, moved)], 1.0)
    assert recs[0]["k"] == "forged"


# ── pull_once (fetch and engine stubbed) ────────────────────────────────────────────────


class _Backend:
    name = "lima"

    def ssh_target(self, name):
        from foldyard.machine_backend import SshTarget

        return SshTarget(user="u", port=22, identity="/k")


@pytest.fixture
def pulled(monkeypatch, tmp_path):
    monkeypatch.setattr(monitorlog.config, "state_dir", lambda: tmp_path)
    fetched: list[str] = []
    asked: list[list[str]] = []
    state = {"out": b""}

    def fake_fetch(target, script):
        fetched.append(script)
        return state["out"]

    def fake_describe(ids):
        asked.append(ids)
        return {i: ({"name": "fyex-devbox", "project": "", "service": ""} if i == BOX else None)
                for i in ids}  # fmt: skip

    monkeypatch.setattr(monitorlog, "_fetch", fake_fetch)
    monkeypatch.setattr(monitorlog, "_engine_describe", fake_describe)
    return state, fetched, asked, tmp_path


def test_pull_once_stores_records_describes_new_containers_once_and_resumes(pulled):
    state, fetched, asked, tmp = pulled
    data = line(0, {"k": "start"}) + line(1, snap({200: [BOX], 300: [SIB]})) + line(2, ev(200))
    state["out"] = chunk(f"{DIR}/000000000000.jsonl", 0, data)
    assert monitorlog.pull_once(_Backend(), "fymon", KEY, now=lambda: 5.0) == 4  # + engine
    assert asked == [[BOX, SIB]]
    head, *stored = [
        json.loads(x) for x in (tmp / "logs" / "monitor.jsonl").read_text().splitlines()
    ]
    assert head["k"] == "segment"  # the store's own: which segment this is
    assert [r["k"] for r in stored] == ["start", "scopes", "ev", "engine"]
    assert stored[-1]["containers"][SIB] is None  # already gone: recorded as such
    # the next pull asks from where this one stopped, and never re-describes a container
    state["out"] = chunk(f"{DIR}/000000000000.jsonl", len(data), line(3, snap({200: [BOX]})))
    monitorlog.pull_once(_Backend(), "fymon", KEY, now=lambda: 6.0)
    assert f"off={len(data)}" in fetched[-1] and f"{DIR}/000000000000.jsonl" in fetched[-1]
    assert asked == [[BOX, SIB]]
    cur = monitorlog.Cursor.load()
    assert cur.pulled == 6.0 and cur.error == "" and cur.last == {BOOT: 3}


def test_an_unreachable_engine_is_asked_again_never_recorded_as_gone(pulled, monkeypatch):
    state, _fetched, asked, tmp = pulled
    answers = iter([None, {BOX: {"name": "fyex-devbox", "project": "", "service": ""}}])
    monkeypatch.setattr(
        monitorlog, "_engine_describe", lambda ids: asked.append(ids) or next(answers)
    )
    state["out"] = chunk(f"{DIR}/000000000000.jsonl", 0, line(0, snap({200: [BOX]})))
    monitorlog.pull_once(_Backend(), "fymon", KEY)
    store = tmp / "logs" / "monitor.jsonl"
    assert '"engine"' not in store.read_text()  # nothing recorded for an engine that wasn't asked
    state["out"] = b""
    monitorlog.pull_once(_Backend(), "fymon", KEY)  # …and asked again on the next pull
    assert asked == [[BOX], [BOX]]
    assert '"fyex-devbox"' in store.read_text()


def test_engine_describe_treats_an_empty_listing_as_the_wrong_engine(monkeypatch):
    from foldyard import devmode

    monkeypatch.setattr(devmode, "_engine_env", lambda: {})
    ok = subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(monitorlog.subprocess, "run", lambda *a, **k: ok)
    assert monitorlog._engine_describe([BOX]) is None
    listed = subprocess.CompletedProcess([], 0, f"{SIB}\tfyex-api-1\t{{}}\n", "")
    monkeypatch.setattr(monitorlog.subprocess, "run", lambda *a, **k: listed)
    assert monitorlog._engine_describe([BOX, SIB]) == {
        BOX: None,  # really gone: the engine answered, and doesn't have it
        SIB: {"name": "fyex-api-1", "project": "", "service": ""},
    }


def test_pull_once_records_a_failure_on_the_cursor(pulled, monkeypatch):
    def boom(target, script):
        raise OSError("ssh: connect to host 127.0.0.1 port 22: Connection refused")

    monkeypatch.setattr(monitorlog, "_fetch", boom)
    with pytest.raises(OSError):
        monitorlog.pull_once(_Backend(), "fymon", KEY)
    assert "Connection refused" in monitorlog.Cursor.load().error


def test_run_idles_while_the_monitor_is_off(monkeypatch):
    from foldyard import machine, monitor

    pulls = []
    monkeypatch.setattr(monitor, "wanted", lambda: False)
    monkeypatch.setattr(machine, "state", lambda: "running")
    monkeypatch.setattr(monitorlog, "pull_once", lambda *a, **k: pulls.append(a))
    calls = iter([False, True])
    monitorlog.run(lambda m: None, lambda: next(calls, True))
    assert pulls == []


# ── the join ────────────────────────────────────────────────────────────────────────────


# podman's rootless namespace (where conmon runs: the inits below come from it), as the catalogue
# records it — podman entering it straight from the VM's own, as every podman command does
PODMAN_NS = 7
PODMAN = {"k": "fact", "inode": str(PODMAN_NS), "what": "entry", "start": "", "outside": "1",
          "by": ""}  # fmt: skip


def _records(*recs: dict, boot: str = BOOT) -> list[dict]:
    return [{**PODMAN, "boot": boot, "seq": -1}] + [
        {**r, "boot": boot, "seq": i} for i, r in enumerate(recs)
    ]


def _who(events):
    return [(e.who, e.container[:1]) for e in events]


def test_join_uses_the_snapshot_before_or_the_one_after():
    recs = _records(
        snap({200: [BOX]}),
        ev(200),  # mapped by the snapshot before
        ev(300),  # a container that started after it…
        snap({200: [BOX], 300: [SIB]}),  # …named by the next one (the relay's new-inode snapshot)
    )
    assert _who(monitorlog.attribute(recs, "fyex")) == [("container", "a"), ("container", "b")]


def test_join_never_carries_a_namespace_number_past_a_snapshot_that_dropped_it():
    # inode 200 was the box's; the box went; the number was handed to another container
    recs = _records(
        snap({200: [BOX]}),
        ev(200),
        snap({}),  # 200 gone
        ev(200),  # NOT the box: the snapshot before no longer has it…
        snap({200: [OTHER]}),  # …the one after names the new owner
    )
    assert _who(monitorlog.attribute(recs, "fyex")) == [("container", "a"), ("container", "c")]


def test_join_calls_disagreeing_snapshots_ambiguous_never_a_guess():
    recs = _records(snap({200: [BOX]}), ev(200), snap({200: [OTHER]}))
    # before has it as BOX and after as OTHER — reuse inside one window: no way to know which
    assert monitorlog.attribute(recs, "fyex")[0].who == "ambiguous"
    shared = _records(snap({200: [BOX, SIB]}), ev(200))
    assert monitorlog.attribute(shared, "fyex")[0].who == "ambiguous"


def init(
    inode: int,
    kind: str = "process_exec",
    start: str = "2026-10-04T10:00:00.5Z",
    by: str = "/usr/bin/conmon",
) -> dict:
    """A container's init entering (exec) or leaving (exit) its mount namespace: in the init tree,
    its parent (conmon) outside the namespace — the mark the join reads a reused number by."""
    parent = {"binary": by, "ns": {"mnt": {"inum": PODMAN_NS}}}
    e = ev(inode, binary="/run/podman-init", kind=kind, parent=parent)
    e["e"][kind]["process"] |= {"in_init_tree": True, "start_time": start}
    e["e"][kind]["process"]["ns"]["pid"] = {"inum": 5000 + inode}  # its container's own
    return e


HOST_PID = {"inum": 4026531836, "is_host": True}  # the VM's own pid namespace


def proc(inode: int, start: str, kind: str = "process_exec", host_pid: bool = False) -> dict:
    """A process in mount namespace ``inode``: a container's own (its own pid namespace), or —
    ``host_pid`` — one in the VM's pid namespace, as podman's tools are (netavark, nft, crun)."""
    e = ev(inode, kind=kind)
    e["e"][kind]["process"]["start_time"] = start
    e["e"][kind]["process"]["ns"]["pid"] = HOST_PID if host_pid else {"inum": 5000 + inode}
    return e


def test_a_reused_number_is_read_by_the_containers_inits_leaving_and_entering():
    # the box's number handed straight to the next container, no snapshot in between (cabash,
    # 2026-10-23): the box's last commands and the new container's first, told apart by the marks
    recs = _records(
        snap({200: [BOX]}, t=T_BEFORE),
        proc(200, "2026-10-04T09:59:59Z"),  # the box's — before its init left
        init(200, "process_exit", start="2026-10-04T09:00:00Z"),  # the box's init leaving
        init(200, start="2026-10-04T10:00:01.25Z"),  # the next container's init entering
        proc(200, "2026-10-04T10:00:02Z"),  # the new container's first command
        snap({200: [OTHER]}),
    )
    assert _who(monitorlog.attribute(recs, "fyex")) == [
        ("container", "a"),
        ("container", "a"),
        ("container", "c"),
        ("container", "c"),
    ]


def test_between_the_old_init_leaving_and_the_new_one_entering_its_still_ambiguous():
    # podman's own short-lived namespace (netavark) can hold the number in between, and the old
    # container's children die after its init — neither side can be claimed
    recs = _records(
        snap({200: [BOX]}, t=T_BEFORE),
        init(200, "process_exit", start="2026-10-04T09:00:00Z"),
        proc(200, "2026-10-04T10:00:00.75Z"),
        init(200, start="2026-10-04T10:00:01Z"),
        snap({200: [OTHER]}),
    )
    assert [e.who for e in monitorlog.attribute(recs, "fyex")][1] == "ambiguous"


def test_a_late_event_of_the_dead_container_is_never_given_to_the_new_one():
    # Tetragon's per-CPU buffers can deliver an event a moment late: its process started long
    # before the new container's init did, so it isn't the new container's
    recs = _records(
        snap({200: [BOX]}, t=T_BEFORE),
        init(200, start="2026-10-04T10:00:01Z"),
        proc(200, "2026-10-04T09:59:00Z", kind="process_exit"),
        snap({200: [OTHER]}),
    )
    assert [e.who for e in monitorlog.attribute(recs, "fyex")] == ["container", "ambiguous"]


def test_the_marks_choose_only_between_the_containers_the_snapshots_name():
    # a container came and went inside one window, its marks both seen; the box's exit and the
    # next container's start never reached the stream. Its events are neither the box's (a start
    # came before the first exit) nor the next one's (an exit came after the last start).
    recs = _records(
        snap({200: [BOX]}, t=T_BEFORE),
        init(200, start="2026-10-04T10:00:01Z"),
        proc(200, "2026-10-04T10:00:02Z"),
        init(200, "process_exit", start="2026-10-04T10:00:01Z"),
        snap({200: [OTHER]}),
    )
    assert [e.who for e in monitorlog.attribute(recs, "fyex")][1] == "ambiguous"
    # one snapshot naming two containers for the number: the marks don't pick between them
    shared = _records(snap({200: [BOX, SIB]}, t=T_BEFORE), init(200), proc(200, "2026-10-04T10:00:02Z"),
                      snap({200: [OTHER]}))  # fmt: skip
    assert [e.who for e in monitorlog.attribute(shared, "fyex")] == ["ambiguous", "ambiguous"]


def test_a_mark_the_snapshot_before_could_have_seen_decides_nothing():
    # the rules assume Tetragon delivers a marked container's marks; when one is lost at random,
    # an init that started before the snapshot before was taken is the one that snapshot saw (the
    # relay reads the export a moment behind) — never a new holder
    seen_start = _records(
        snap({200: [BOX]}, t=T_BEFORE),
        init(200, start="2026-10-04T09:29:59.9Z"),  # BOX's own init, read after the snapshot
        proc(200, "2026-10-04T10:00:02Z"),
        snap({200: [OTHER]}),  # BOX's exit and OTHER's start both lost
    )
    assert [e.who for e in monitorlog.attribute(seen_start, "fyex")][1] == "ambiguous"
    # …and an init exit is the snapshot before's container's only if that init started before it
    later_exit = _records(
        snap({200: [BOX]}, t=T_BEFORE),
        proc(200, "2026-10-04T10:00:02Z"),
        init(200, "process_exit", start="2026-10-04T10:00:01Z"),  # someone whose start was lost
        snap({200: [OTHER]}),
    )
    assert monitorlog.attribute(later_exit, "fyex")[0].who == "ambiguous"
    # Tetragon's clock and the relay's are the guest's, but compared across two processes: an init
    # that looks older than the snapshot before yet entered after it (a start mark before its
    # exit, in the window) still isn't that snapshot's container
    skewed = _records(
        snap({200: [BOX]}, t=T_BEFORE),
        init(200, start="2026-10-04T09:29:59Z"),
        proc(200, "2026-10-04T10:00:02Z"),
        init(200, "process_exit", start="2026-10-04T09:29:59Z"),
        snap({200: [OTHER]}),
    )
    assert monitorlog.attribute(skewed, "fyex")[1].who == "ambiguous"


def test_a_stretch_is_joined_with_the_marks_outside_it(monkeypatch, tmp_path):
    # the new container's init in an earlier read than its first command: the catalogue keeps the
    # marks, as it keeps the snapshots, so a hook's window reads it as the whole store does
    from foldyard import monitorstore

    monkeypatch.setattr(monitorstore.config, "state_dir", lambda: tmp_path)
    recs = [
        {**r, "boot": BOOT, "seq": i}
        for i, r in enumerate(
            [
                entered(),  # podman's namespace, where conmon starts the inits from
                snap({200: [BOX]}, t=T_BEFORE),
                init(200, "process_exit", start="2026-10-04T09:00:00Z"),
                init(200, start="2026-10-04T10:00:01Z"),
                proc(200, "2026-10-04T10:00:02Z"),
                snap({200: [OTHER]}),
            ]
        )
    ]
    monitorstore.append(recs)
    cat = monitorlog.catalogue()
    assert cat is not None
    try:
        (e,) = monitorlog.join([recs[4]], cat, "fyex")
    finally:
        cat.close()
    assert (e.who, e.container) == ("container", OTHER)


def entered(inode: int = PODMAN_NS, binary: str = "/usr/bin/conmon") -> dict:
    """podman entering mount namespace ``inode`` straight from the VM's own: a process there, its
    parent in the VM's namespace (Tetragon's ``is_host``), outside the VM's user namespace."""
    parent = {"binary": "/usr/bin/podman", "ns": {"mnt": {"inum": 1, "is_host": True}}}
    e = ev(inode, binary=binary, parent=parent)
    e["e"]["process_exec"]["process"]["ns"]["user"] = {"inum": 4026532575}
    return e


def _whos(events):
    return [(e.who, e.container[:1], e.crossed) for e in events]


def test_podmans_own_namespace_is_podman():
    # the namespace podman enters from the VM's own, on every command: conmon, pasta, crun,
    # netavark live there — 403k of cabash's 560k `unattributed` events (2026-10-23 00:30–02:10)
    stream = [snap({}), entered(), ev(PODMAN_NS, "/usr/bin/pasta"), ev(PODMAN_NS, "/usr/bin/rpm"),
              ev(999), snap({})]  # fmt: skip
    recs = [{**r, "boot": BOOT, "seq": i} for i, r in enumerate(stream)]
    assert [e.who for e in monitorlog.attribute(recs, "fyex")] == [
        "podman", "podman", "podman", "unattributed"
    ]  # fmt: skip


def test_a_vm_service_in_a_mount_namespace_of_its_own_is_the_vm():
    # systemd's sandboxed services (userdbd, coredump, hostnamed): the VM's own user namespace,
    # which nothing in a rootless container can be in
    service = ev(400, binary="/usr/lib/systemd/systemd-userwork")
    service["e"]["process_exec"]["process"]["ns"]["user"] = {"inum": 4026531837, "is_host": True}
    vm_mnt = ev(4026531832, binary="/usr/bin/grep")
    vm_mnt["e"]["process_exec"]["process"]["ns"]["mnt"]["is_host"] = True  # no snapshot near it
    assert [e.who for e in monitorlog.attribute(_records(service, vm_mnt), "fyex")] == ["vm", "vm"]


def test_an_image_build_step_is_a_build():
    # buildah's RUN steps: an init in its own pid namespace, started by podman itself in its own
    # namespace (not by conmon) — never in a podman scope, so no snapshot names it
    recs = _records(
        snap({}),
        init(500, start="2026-10-04T10:00:01Z", by="/proc/self/exe"),
        proc(500, "2026-10-04T10:00:02Z"),
        init(500, "process_exit", start="2026-10-04T10:00:01Z", by="/proc/self/exe"),
        proc(500, "2026-10-04T10:00:02Z", kind="process_exit"),  # the step's own, dying after it
        proc(500, "2026-10-04T10:00:03Z", host_pid=True),  # podman's teardown on the number
        snap({}),
    )
    assert [e.who for e in monitorlog.attribute(recs, "fyex")] == [
        "build", "build", "build", "build", "unattributed"
    ]  # fmt: skip


def test_an_init_from_inside_a_container_is_neither_a_containers_nor_a_builds():
    # any container can make an init of its own (`unshare -Urpm --fork`): it comes from the
    # container's namespace, not podman's, so it is no mark of a container or a build — the box
    # could otherwise end its own container's life early, or pass its work off as a build
    recs = _records(
        snap({200: [BOX]}, t=T_BEFORE),
        init(600, start="2026-10-04T10:00:01Z"),
        proc(600, "2026-10-04T10:00:02Z"),
        snap({200: [BOX]}),
    )
    recs[2]["e"]["process_exec"]["parent"]["ns"]["mnt"]["inum"] = 200
    assert [e.who for e in monitorlog.attribute(recs, "fyex")] == ["unattributed", "unattributed"]
    forged_exit = _records(
        snap({200: [BOX]}, t=T_BEFORE),
        init(200, "process_exit", start="2026-10-04T10:00:01Z"),
        proc(200, "2026-10-04T10:00:02Z", host_pid=True),
        snap({}),
    )
    forged_exit[2]["e"]["process_exit"]["parent"]["ns"]["mnt"]["inum"] = 600  # from inside BOX
    assert _who(monitorlog.attribute(forged_exit, "fyex"))[1] == ("container", "a")


def test_podmans_tools_before_a_containers_init_are_not_the_containers():
    # netavark and nft in a short-lived namespace of their own, carrying the number the container
    # takes next: the snapshot after names the container, but its init hadn't entered yet
    recs = _records(
        snap({}, t=T_BEFORE),
        proc(300, "2026-10-04T10:00:00Z", host_pid=True),  # netavark's nft
        init(300, start="2026-10-04T10:00:01Z"),
        proc(300, "2026-10-04T10:00:02Z"),
        snap({300: [OTHER]}),
    )
    assert _who(monitorlog.attribute(recs, "fyex")) == [
        ("unattributed", ""), ("container", "c"), ("container", "c")
    ]  # fmt: skip


def test_after_a_containers_init_left_only_its_own_processes_are_its():
    # the kernel kills the rest of the container when its init exits: their exits are still the
    # container's (its pid namespace); podman's teardown in the VM's pid namespace is not
    recs = _records(
        snap({300: [BOX]}, t=T_BEFORE),
        proc(300, "2026-10-04T10:00:00Z"),
        init(300, "process_exit", start="2026-10-04T09:00:00Z"),
        proc(300, "2026-10-04T10:00:00Z", kind="process_exit"),  # a child killed after it
        proc(300, "2026-10-04T10:00:03Z", host_pid=True),  # netavark's nft on the freed number
        snap({}),
    )
    assert _who(monitorlog.attribute(recs, "fyex")) == [
        ("container", "a"), ("container", "a"), ("container", "a"), ("unattributed", "")
    ]  # fmt: skip


def test_a_container_the_snapshot_before_saw_being_set_up_keeps_its_life():
    # a container's scope holds the runtime in its namespace before its init's exec, and the relay
    # reads the export a moment behind: its snapshot can name a container whose init's exec comes
    # after it in the stream (cabash: 31 ms) — that life is the one it saw, even if it's over by the
    # next snapshot. Not once another init left the number in between: that was someone else's.
    recs = _records(
        snap({300: [BOX]}, t=T_BEFORE),
        init(300, start="2026-10-04T10:00:01Z"),  # after the snapshot was taken
        proc(300, "2026-10-04T10:00:02Z"),
        init(300, "process_exit", start="2026-10-04T10:00:01Z"),
        snap({}),
    )
    assert _who(monitorlog.attribute(recs, "fyex")) == [("container", "a")] * 3
    after_another = _records(
        snap({300: [BOX]}, t=T_BEFORE),
        init(300, "process_exit", start="2026-10-04T09:00:00Z"),
        init(300, start="2026-10-04T10:00:01Z"),
        proc(300, "2026-10-04T10:00:02Z"),
        snap({}),
    )
    assert _who(monitorlog.attribute(after_another, "fyex"))[1:] == [("unattributed", "")] * 2


def test_a_number_free_since_before_the_window_is_never_given_for_podmans_tools():
    # the old container left long ago; a container Tetragon never marks (say one in the VM's pid
    # namespace) holds the number now: its own processes are its, podman's tools in the VM's pid
    # namespace are not
    recs = _records(
        snap({}, t=T_BEFORE),
        init(300, start="2026-10-04T09:00:00Z"),
        init(300, "process_exit", start="2026-10-04T09:00:00Z"),
        snap({300: [OTHER]}),
        proc(300, "2026-10-04T10:00:02Z"),
        proc(300, "2026-10-04T10:00:03Z", host_pid=True),
        snap({300: [OTHER]}),
    )
    assert _who(monitorlog.attribute(recs, "fyex"))[2:] == [
        ("container", "c"),
        ("unattributed", ""),
    ]


def test_a_build_between_two_containers_is_the_builds_not_ambiguous():
    # the box's number went to an image build step, then to the next container: the step's
    # events are neither container's, and the facts say whose they are
    recs = _records(
        snap({200: [BOX]}, t=T_BEFORE),
        init(200, "process_exit", start="2026-10-04T09:00:00Z"),
        init(200, start="2026-10-04T10:00:01Z", by="/proc/self/exe"),
        proc(200, "2026-10-04T10:00:02Z"),
        init(200, "process_exit", start="2026-10-04T10:00:01Z", by="/proc/self/exe"),
        init(200, start="2026-10-04T10:00:03Z"),
        snap({200: [OTHER]}),
    )
    assert [e.who for e in monitorlog.attribute(recs, "fyex")][2] == "build"


def test_a_stretch_reads_an_old_init_by_where_it_came_from_then(monkeypatch, tmp_path):
    # a hook's read holds only podman's tool on a number whose last container left before the
    # snapshot before: the join must fetch where that container's init came from (podman's
    # namespace, entered long before) to know it was a container's init at all
    from foldyard import monitorstore

    monkeypatch.setattr(monitorstore.config, "state_dir", lambda: tmp_path)
    stream = [entered(), snap({}, t=T_BEFORE), init(300, start="2026-10-04T09:00:00Z"),
              init(300, "process_exit", start="2026-10-04T09:00:00Z"), snap({300: [OTHER]}),
              proc(300, "2026-10-04T10:00:03Z", host_pid=True), snap({300: [OTHER]})]  # fmt: skip
    recs = [{**r, "boot": BOOT, "seq": i} for i, r in enumerate(stream)]
    monitorstore.append(recs)
    cat = monitorlog.catalogue()
    assert cat is not None
    try:
        (e,) = monitorlog.join([recs[5]], cat, "fyex")
    finally:
        cat.close()
    assert e.who == "unattributed"


def test_a_container_holding_a_process_in_the_vms_or_podmans_namespace_is_ambiguous():
    # a container's scope with a process in podman's own namespace (a privileged container can
    # `nsenter` there; crun joins a scope a moment before it leaves podman's namespace): neither
    # side is claimed, and a container-only hook is told — never filed under podman, nor the VM
    recs = _records(snap({PODMAN_NS: [BOX], 1: [SIB]}), ev(PODMAN_NS), ev(1), snap({}))
    assert [e.who for e in monitorlog.attribute(recs, "fyex")] == ["ambiguous", "ambiguous"]


def test_a_process_crossing_into_the_vms_or_podmans_namespace_is_the_containers_it_came_from():
    # the kernel fact of an escape: a process in a system namespace whose parent was in a
    # container's — never filtered with the system's events, named after where it came from
    into_podman = ev(PODMAN_NS, binary="/bin/sh", parent={"ns": {"mnt": {"inum": 200}}})
    into_vm = ev(1, binary="/bin/sh", parent={"ns": {"mnt": {"inum": 300}}})
    recs = _records(snap({200: [BOX]}), into_podman, into_vm, ev(PODMAN_NS), snap({200: [BOX]}))
    assert _whos(monitorlog.attribute(recs, "fyex")) == [
        ("container", "a", True), ("unattributed", "", True), ("podman", "", False)
    ]  # fmt: skip


def test_a_stretch_knows_podmans_namespace_however_long_ago_it_was_entered(monkeypatch, tmp_path):
    from foldyard import monitorstore

    monkeypatch.setattr(monitorstore.config, "state_dir", lambda: tmp_path)
    recs = [
        {**r, "boot": BOOT, "seq": i}
        for i, r in enumerate([snap({}), entered(), snap({}), ev(400), snap({}), ev(PODMAN_NS)])
    ]
    monitorstore.append(recs)
    cat = monitorlog.catalogue()
    assert cat is not None
    try:
        (e,) = monitorlog.join([recs[5]], cat, "fyex")
    finally:
        cat.close()
    assert e.who == "podman"


def test_join_vm_and_unattributed():
    recs = _records(snap({200: [BOX]}, vm=1), ev(1, binary="/usr/bin/pasta"), ev(999), snap({}))
    assert [e.who for e in monitorlog.attribute(recs, "fyex")] == ["vm", "unattributed"]


def test_join_names_and_worktrees_come_from_the_engine_claims():
    recs = [
        *_records(snap({200: [BOX], 300: [SIB], 400: [OTHER]}), ev(200), ev(300), ev(400)),
        {"k": "engine", "containers": {
            BOX: {"name": "fyex-feat-devbox", "project": "", "service": ""},
            SIB: {"name": "fyex-api-1", "project": "fyex", "service": "api"},
            OTHER: None,
        }},
    ]  # fmt: skip
    events = monitorlog.attribute(recs, "fyex")
    assert [(e.worktree, e.name) for e in events] == [
        ("feat", "fyex-feat-devbox"),
        ("main", "fyex-api-1"),
        ("", ""),  # gone before the host asked
    ]


def test_join_counts_a_re_read_record_once():
    recs = _records(snap({200: [BOX]}), ev(200))
    assert len(monitorlog.attribute(recs + recs, "fyex")) == 1


def test_join_describes_connections_and_file_access():
    connect = ev(200, kind="process_kprobe", function_name="tcp_connect",
                 args=[{"sock_arg": {"daddr": "1.1.1.1", "dport": 443}}])  # fmt: skip
    refused = ev(200, kind="process_kprobe", function_name="security_file_permission",
                 args=[{"file_arg": {"path": "/etc/shadow"}}, {"int_arg": 4}],
                 ret=-13)  # fmt: skip
    events = monitorlog.attribute(_records(snap({200: [BOX]}), connect, refused), "fyex")
    assert [(e.kind, e.detail) for e in events] == [
        ("connect", "1.1.1.1:443"),
        ("file", "read /etc/shadow → -13"),
    ]


# BashArena's firewall daemon reloading iptables, as Tetragon reports it: the parent hop and the
# exec ids are what tell a container's own daemon from a command a session ran.
_DAEMON = {"binary": "/bin/bash", "arguments": "/usr/local/bin/firewall-daemon", "pid": 41,
           "exec_id": "OjE0MDA6NDE="}  # fmt: skip


def _ancestry(e) -> tuple:
    return (e.ppid, e.parent, e.parent_args, e.exec_id, e.parent_exec_id)


def test_join_carries_the_parent_process_and_the_exec_ids():
    flush = ev(200, binary="/usr/sbin/iptables", parent=_DAEMON)
    proc = flush["e"]["process_exec"]["process"]
    proc |= {"arguments": "-F", "exec_id": "OjE0MDE6NDI=", "parent_exec_id": "OjE0MDA6NDE="}
    (e,) = monitorlog.attribute(_records(snap({200: [BOX]}), flush), "fyex")
    assert (e.binary, e.args) == ("/usr/sbin/iptables", "-F")
    assert _ancestry(e) == (
        41,
        "/bin/bash",
        "/usr/local/bin/firewall-daemon",
        "OjE0MDE6NDI=",
        "OjE0MDA6NDE=",
    )


def test_a_kprobe_event_carries_its_parent_too_and_the_parents_exec_id_stands_in():
    connect = ev(200, kind="process_kprobe", function_name="tcp_connect", parent=_DAEMON,
                 args=[{"sock_arg": {"daddr": "1.1.1.1", "dport": 443}}])  # fmt: skip
    connect["e"]["process_kprobe"]["process"]["exec_id"] = "OjE0MDE6NDI="  # no parent_exec_id
    (e,) = monitorlog.attribute(_records(snap({200: [BOX]}), connect), "fyex")
    assert (e.kind, e.detail) == ("connect", "1.1.1.1:443")
    assert _ancestry(e) == (
        41,
        "/bin/bash",
        "/usr/local/bin/firewall-daemon",
        "OjE0MDE6NDI=",
        "OjE0MDA6NDE=",  # parent.exec_id
    )


def test_a_record_without_ancestry_takes_the_defaults():
    (e,) = monitorlog.attribute(_records(snap({200: [BOX]}), ev(200)), "fyex")
    assert _ancestry(e) == (None, "", "", "", "")


# ── fy monitor ──────────────────────────────────────────────────────────────────────────


def test_show_prints_attributed_events_and_the_pull_state(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(monitorlog.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(monitorlog.config, "in_box", lambda: False)
    monkeypatch.setattr(monitorlog.config, "project_prefix", lambda: "fyex")
    monitorlog._append([*_records(snap({200: [BOX]}), ev(200), ev(999)),
                        {"k": "engine", "containers": {BOX: {"name": "fyex-devbox",
                                                             "project": "", "service": ""}}}])  # fmt: skip
    monitorlog.Cursor(pulled=1.0, stored=4).save()
    assert monitorlog.show(limit=10) == 0
    out = capsys.readouterr().out
    assert "main:fyex-devbox" in out and "unattributed" in out
    assert "claims" in out


def test_show_refuses_in_the_box(monkeypatch, capsys):
    monkeypatch.setattr(monitorlog.config, "in_box", lambda: True)
    assert monitorlog.show() == 1


def test_when_reads_iso_times_as_utc_and_spans_back_from_now():
    assert monitorlog.when("2026-10-21T19:55") == "2026-10-21T19:55:00.000000000Z"
    assert monitorlog.when("2026-10-21T20:55+01:00") == "2026-10-21T19:55:00.000000000Z"
    now = 1_792_620_000.0  # 2026-10-21T22:00:00Z
    assert monitorlog.when("90m", now=now) == "2026-10-21T20:30:00.000000000Z"
    assert monitorlog.when("1d", now=now) == "2026-10-20T22:00:00.000000000Z"
    for nonsense in ("yesterday", "99999999999d"):
        with pytest.raises(ValueError):
            monitorlog.when(nonsense)


def _at(e: dict, t: str) -> dict:
    e["e"]["time"] = t
    return e


def test_show_since_reads_the_whole_store_joined_and_says_whats_missing(
    monkeypatch, tmp_path, capsys
):
    from foldyard import monitorstore

    monkeypatch.setattr(monitorlog.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(monitorlog.config, "in_box", lambda: False)
    monkeypatch.setattr(monitorlog.config, "project_prefix", lambda: "fyex")
    monkeypatch.setattr(monitorstore, "SEGMENT_BYTES", 400)  # every append seals the last
    claim = {
        "k": "engine",
        "containers": {BOX: {"name": "fyex-devbox", "project": "", "service": ""}},
    }
    monitorlog._append([claim, *_records(snap({200: [BOX]}), _at(ev(200), "2026-10-21T19:00:00Z"))])
    early = _records(snap({}), _at(ev(200, binary="/bin/early"), "2026-10-21T19:59:00Z"))
    monitorlog._append([{**r, "seq": r["seq"] + 2} for r in early])
    inside = [_at(ev(200, binary=f"/bin/b{i}"), f"2026-10-21T20:0{i}:00Z") for i in range(3)]
    monitorlog._append(
        [{**r, "seq": r["seq"] + 4} for r in _records(snap({200: [OTHER]}), *inside)]
    )
    monitorlog._append([{"k": "gap", "boot": BOOT, "from": 8, "to": 9, "recv": 1_792_612_900.0}])
    assert any(s.gz for s in monitorstore.segments())  # sealed and compressed on the way
    assert monitorlog.show(limit=None, since="2026-10-21T20:00", until="2026-10-21T20:02") == 0
    out = capsys.readouterr().out
    assert (
        "/bin/b0" in out and "/bin/b1" in out and "/bin/b2" not in out and "/bin/early" not in out
    )
    assert "2 events lost between the VM and here" in out
    assert "store:" in out
    # a range is joined like the whole store: the claim and snapshot from segments before it
    monitorlog.show(limit=None, since="2026-10-21T19:59", as_json=True)
    rows = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [(r["binary"], r["who"], r["name"]) for r in rows][:2] == [
        ("/bin/early", "container", ""),  # the snapshot before dropped 200; the one after: OTHER
        ("/bin/b0", "container", ""),
    ]
    assert rows[0]["container"] == OTHER and rows[0]["mnt"] == 200


def test_show_refuses_a_time_it_cant_read(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(monitorlog.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(monitorlog.config, "in_box", lambda: False)
    assert monitorlog.show(since="last tuesday") == 1
    assert "span back from now" in capsys.readouterr().out
