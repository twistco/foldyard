"""monitorlog.py — the guest monitor's events on the host (ADR-0031, slice 2): the pull's
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


def snap(mapping: dict[int, list[str]], vm: int = 1) -> dict:
    return {"k": "scopes", "vm": vm, "map": {str(k): v for k, v in mapping.items()}}


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
    stored = [json.loads(x) for x in (tmp / "logs" / "monitor.jsonl").read_text().splitlines()]
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


def test_the_store_rotates_and_keeps_a_bounded_number_of_backups(pulled, monkeypatch):
    _state, _f, _a, tmp = pulled
    monkeypatch.setattr(monitorlog, "STORE_BYTES", 100)
    stamps = iter(f"2026100{i}T000000Z" for i in range(1, 9))

    class _Now:
        @staticmethod
        def now(tz):
            class _S:
                @staticmethod
                def strftime(fmt):
                    return next(stamps)

            return _S

    monkeypatch.setattr(monitorlog, "datetime", _Now)
    for _ in range(8):
        monitorlog._append([{"k": "x", "pad": "p" * 120}])
    backups = sorted((tmp / "logs").glob("monitor.*.jsonl"))
    assert len(backups) == monitorlog.STORE_BACKUPS


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


def _records(*recs: dict, boot: str = BOOT) -> list[dict]:
    return [{**r, "boot": boot, "seq": i} for i, r in enumerate(recs)]


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
