"""attribution.py — resolving a proxy row's stamp to who sent it (ADR-0034), and the Network Log
naming who used each host and who was refused. The engine is never reached: ``attribution._engine``
is stubbed with canned podman output."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from foldyard import attribution, config
from foldyard.plugins import _netlog, proxy

A, B = "a" * 64, "b" * 64
T0 = 1_800_000_000.0  # a snapshot time; ISO rows below sit around it


def _iso(t: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


def _snap(t: float, endpoints, containers=None) -> dict:
    return {"t": t, "endpoints": [list(e) for e in endpoints], "containers": containers or {}}


BRIDGE = {"via": "bridge", "src": "10.89.0.6:41000", "net": "podman1", "boot": "b", "seq": "1"}


# ── the read-time rule ─────────────────────────────────────────────────────────────────


def test_a_row_takes_the_snapshot_before_it_when_that_holds_the_address():
    r = attribution.Resolver(
        [
            _snap(
                T0, [("podman1", "10.89.0.6", A)], {A: {"name": "fyex-api-1", "project": "fyex"}}
            ),
            _snap(T0 + 60, []),
        ]
    )
    who = r.who(BRIDGE, _iso(T0 + 30))
    assert who is not None and (who.kind, who.cid, who.label) == ("container", A, "fyex-api-1")
    assert who.project == "fyex"


def test_else_the_snapshot_after_it():
    # A container that started between two snapshots: the next one is the first to see it.
    r = attribution.Resolver([_snap(T0, []), _snap(T0 + 5, [("podman1", "10.89.0.6", B)])])
    who = r.who(BRIDGE, _iso(T0 + 2))
    assert who is not None and (who.kind, who.cid) == ("container", B)
    assert who.label == f"container {B[:12]}"  # no claims recorded: the id, never a guess


def test_two_snapshots_that_disagree_are_ambiguous_never_a_pick():
    r = attribution.Resolver(
        [_snap(T0, [("podman1", "10.89.0.6", A)]), _snap(T0 + 5, [("podman1", "10.89.0.6", B)])]
    )
    who = r.who(BRIDGE, _iso(T0 + 2))
    assert who is not None and who.kind == "ambiguous" and not who.cid


def test_a_mapping_is_never_carried_past_a_snapshot_that_dropped_it():
    # A held 10.89.0.6, a snapshot then saw it gone; a row after that (and before anyone else is
    # seen there) is unresolved — the address is free, and A is not its owner any more.
    r = attribution.Resolver(
        [_snap(T0, [("podman1", "10.89.0.6", A)]), _snap(T0 + 5, []), _snap(T0 + 10, [])]
    )
    who = r.who(BRIDGE, _iso(T0 + 7))
    assert who is not None and who.kind == "unresolved" and who.label == "10.89.0.6 on podman1"


def test_the_same_address_on_another_bridge_is_another_container():
    r = attribution.Resolver([_snap(T0, [("podman2", "10.89.0.6", A)])])
    who = r.who(BRIDGE, _iso(T0 + 1))
    assert who is not None and who.kind == "unresolved"


@pytest.mark.parametrize(
    "origin, kind, label",
    [
        ({"via": "vm", "who": "container", "ctr": A}, "container", "fyex-api-1"),
        ({"via": "vm", "who": "container", "ctr": B}, "container", f"container {B[:12]}"),
        ({"via": "vm", "who": "build"}, "build", "a build step"),
        ({"via": "vm", "who": "vm"}, "vm", "the VM itself (an image pull, the VM user)"),
        ("unattributed", "unattributed", "unattributed"),
        ("forged", "forged", "a stamp that didn't verify"),
        ({"via": "elsewhere"}, "forged", "a stamp of an unknown shape"),
    ],
)
def test_the_vm_listeners_stamps_need_no_address_lookup(origin, kind, label):
    r = attribution.Resolver([_snap(T0, [], {A: {"name": "fyex-api-1"}})])
    who = r.who(origin, _iso(T0 + 999_999))  # long after every snapshot: an id is never reused
    assert who is not None and (who.kind, who.label) == (kind, label)


def test_a_row_without_an_origin_names_no_one():
    assert attribution.Resolver([]).who(None, _iso(T0)) is None


def test_the_stamp_identity_round_trips_for_the_index():
    for origin in (
        BRIDGE,
        {"via": "vm", "who": "container", "ctr": A},
        {"via": "vm", "who": "build"},
        {"via": "vm", "who": "vm"},
        "unattributed",
        "forged",
    ):
        key = attribution.origin_key(origin)
        assert key is not None
        assert attribution.origin_key(attribution.origin_of_key(key)) == key
    assert attribution.origin_key(None) is None
    assert attribution.origin_key(BRIDGE) == "bridge podman1 10.89.0.6"  # the port is not identity


def test_the_resolver_rereads_the_files_only_when_they_change(tmp_path):
    path = tmp_path / "origin-snapshots.jsonl"
    path.write_text(json.dumps(_snap(T0, [("podman1", "10.89.0.6", A)])) + "\nnot json\n")
    first = attribution.resolver(path)
    assert attribution.resolver(path) is first
    with path.open("a") as fh:
        fh.write(json.dumps(_snap(T0 + 5, [("podman1", "10.89.0.6", B)])) + "\n")
    again = attribution.resolver(path)
    assert again is not first and again.times == [T0, T0 + 5]


# ── the snapshots: the engine's word ───────────────────────────────────────────────────


class Engine:
    """Canned podman: ``ps -q``, ``network ls --format json``, ``inspect``."""

    def __init__(self) -> None:
        self.ids = [A]
        self.calls: list[tuple[str, ...]] = []
        self.fail = ""
        self.after_inspect: list[str] | None = None  # the running set once an inspect has run

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        if args[0] == "inspect" and self.after_inspect is not None:
            self.ids, self.after_inspect = self.after_inspect, None
        if self.fail and args[0] == self.fail:
            return subprocess.CompletedProcess(args, 125, "", "boom")
        if args[0] == "ps":
            return subprocess.CompletedProcess(args, 0, "".join(i + "\n" for i in self.ids), "")
        if args[0] == "network":
            nets = [{"name": "podman", "network_interface": "podman0"},
                    {"name": "fyex_default", "network_interface": "podman1"}]  # fmt: skip
            return subprocess.CompletedProcess(args, 0, json.dumps(nets), "")
        found = [
            {
                "Id": cid,
                "Name": f"/c-{cid[0]}",  # docker's spelling has the slash
                "Config": {"Labels": {"com.docker.compose.project": "fyex"}},
                "NetworkSettings": {
                    "Networks": {"fyex_default": {"IPAddress": f"10.89.0.{ord(cid[0]) - 90}"}}
                },
            }
            for cid in args[1:]
        ]
        return subprocess.CompletedProcess(args, 0, json.dumps(found), "")


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    monkeypatch.setattr(attribution, "_engine", e)
    return e


def test_a_snapshot_records_each_containers_address_on_its_bridge(engine, tmp_path):
    clock = [T0]
    snap = attribution.Snapshotter(tmp_path / "s.jsonl", now=lambda: clock[0])
    assert snap.poll() is True
    [rec] = [json.loads(x) for x in (tmp_path / "s.jsonl").read_text().splitlines()]
    assert rec["endpoints"] == [["podman1", "10.89.0.7", A]]
    assert rec["containers"][A] == {"name": "c-a", "project": "fyex", "service": ""}


def test_snapshots_are_written_on_change_and_at_least_every_minute(engine, tmp_path):
    clock = [T0]
    path = tmp_path / "s.jsonl"
    snap = attribution.Snapshotter(path, now=lambda: clock[0])
    snap.poll()
    clock[0] += 5
    assert snap.poll() is False  # nothing changed: not even an inspect
    assert [c[0] for c in engine.calls].count("inspect") == 1
    engine.ids = [A, B]
    clock[0] += 5
    assert snap.poll() is True  # a new container: written at once
    clock[0] += attribution.SNAPSHOT_EVERY + 1
    assert snap.poll() is True  # unchanged, but a minute on: written, so "before" stays fresh
    assert len(path.read_text().splitlines()) == 3


def test_an_engine_that_doesnt_answer_writes_nothing(engine, tmp_path):
    snap = attribution.Snapshotter(tmp_path / "s.jsonl", now=lambda: T0)
    engine.fail = "ps"
    with pytest.raises(OSError, match="`ps`"):
        snap.poll()
    engine.fail = "network"
    with pytest.raises(OSError, match="networks"):
        snap.poll()
    engine.fail = "inspect"
    with pytest.raises(OSError, match="describe"):
        snap.poll()
    assert not (tmp_path / "s.jsonl").exists()


def test_a_ps_that_fails_when_asked_again_is_not_taken_for_the_race(engine, monkeypatch, tmp_path):
    # The race is told apart by a SECOND `ps`; if that one fails too, nothing says a container
    # went, so it is the engine failing — never a silent skip.
    answers = iter([[A], None])
    monkeypatch.setattr(attribution, "running_ids", lambda: next(answers))
    engine.fail = "inspect"
    snap = attribution.Snapshotter(tmp_path / "s.jsonl", now=lambda: T0)
    with pytest.raises(OSError, match="`ps`"):
        snap.poll()


def test_a_container_stopping_between_ps_and_inspect_is_not_a_failure(engine, tmp_path):
    # A project that churns containers (an eval harness's sandboxes) hits this every few hours:
    # `inspect` refuses the whole call for the one id that just went. Asking `ps` again tells the
    # race from an engine that can't describe what it runs, which still raises (above).
    path = tmp_path / "s.jsonl"
    snap = attribution.Snapshotter(path, now=lambda: T0)
    engine.ids, engine.after_inspect, engine.fail = [A, B], [A], "inspect"
    assert snap.poll() is False
    assert not path.exists()
    engine.fail = ""
    assert snap.poll() is True  # the next poll describes the set that runs now
    [rec] = [json.loads(x) for x in path.read_text().splitlines()]
    assert list(rec["containers"]) == [A]


def test_the_snapshot_thread_idles_while_attribution_is_off(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(attribution.config, "machine_attribution", lambda: "off")
    monkeypatch.setattr(attribution, "snapshots_file", lambda: tmp_path / "s.jsonl")
    ticks = iter([False, True])
    attribution.run(lambda m: None, lambda: next(ticks))
    assert engine.calls == []


# ── the Network Log: who used each host, who was refused ───────────────────────────────


def _row(t: float, host: str, origin, **extra) -> str:
    return json.dumps({"ts": _iso(t), "method": "GET", "host": host, "path": "/x",
                       "status": extra.pop("status", 200), "origin": origin, **extra})  # fmt: skip


@pytest.fixture
def netlog(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path)
    proxy._NET_INDEXES.clear()
    snaps = tmp_path / "logs" / "origin-snapshots.jsonl"
    snaps.parent.mkdir()
    snaps.write_text(
        json.dumps(
            _snap(
                T0,
                [("podman1", "10.89.0.6", A), ("podman1", "10.89.0.7", B)],
                {A: {"name": "fyex-box"}, B: {"name": "sandbox-3"}},
            )
        )
        + "\n"
    )
    return tmp_path


def test_the_network_log_names_who_used_a_host_and_who_was_refused(netlog):
    box = {**BRIDGE}
    sandbox = {**BRIDGE, "src": "10.89.0.7:5000"}
    rows = [_row(T0 + 1, "api.example", box) for _ in range(3)]
    rows += [_row(T0 + 2, "api.example", sandbox, blocked=True, status=403)]
    rows += [_row(T0 + 3, "api.example", {"via": "vm", "who": "build"})]
    (netlog / "egress.jsonl").write_text("\n".join(rows) + "\n")
    [group] = proxy._network_panel_tree().groups
    assert "by fyex-box ×3, sandbox-3 ×1, a build step ×1" in group.label
    assert "refused for sandbox-3" in group.label
    refused = [c for c in group.children if "403" in c]
    assert refused and "asked by sandbox-3" in refused[0]
    assert any("by fyex-box" in c for c in group.children)


def test_a_row_with_no_stamp_shows_as_before(netlog):
    (netlog / "egress.jsonl").write_text(
        json.dumps({"ts": _iso(T0), "method": "GET", "host": "a.com", "path": "/", "status": 200})
        + "\n"
    )
    [group] = proxy._network_panel_tree().groups
    assert " by " not in group.label and " by " not in group.children[0]


def test_the_index_keeps_a_bounded_set_of_stamps_per_host(netlog):
    rows = [
        _row(T0 + i, "a.com", {**BRIDGE, "src": f"10.89.{i // 250}.{i % 250}:1"})
        for i in range(_netlog.KEEP_ORIGINS + 40)
    ]
    (netlog / "egress.jsonl").write_text("\n".join(rows) + "\n")
    snap = _netlog.NetworkIndex(netlog / "egress.jsonl").refresh()
    [view] = snap.hosts
    assert len(view.origins) == _netlog.KEEP_ORIGINS  # the box decides how many it makes up
    assert view.requests == _netlog.KEEP_ORIGINS + 40  # …the tallies still count every row


def test_the_header_lists_the_top_three_then_how_many_more(netlog):
    rows = []
    for i in range(5):
        rows += [_row(T0 + 1, "a.com", {"via": "vm", "who": "container", "ctr": f"{i}" * 64})] * (
            5 - i
        )
    (netlog / "egress.jsonl").write_text("\n".join(rows) + "\n")
    [group] = proxy._network_panel_tree().groups
    assert f"container {'0' * 12} ×5" in group.label and "(+2)" in group.label


def test_snapshots_file_lives_beside_the_projects_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path)
    assert attribution.snapshots_file() == Path(tmp_path) / "logs" / "origin-snapshots.jsonl"
