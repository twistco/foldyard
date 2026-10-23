"""monitorhooks.py — the operator's hooks over the guest monitor's events (docs/monitor-pipeline.md
§6): host-only config and its validation, settled-only delivery, the cursor, at-least-once with
backoff and a bounded give-up, and a command's environment never being the supervisor's. No
command or endpoint is really reached: ``subprocess.run`` and ``urlopen`` are stubbed."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from foldyard import monitorhooks as mh
from foldyard import monitorlog, monitorstore

BOOT, BOOT2 = "boot-1", "boot-2"
BOX = "a" * 64


def ev(seq: int, inode: int = 200, binary: str = "/bin/cat", boot: str = BOOT, recv: float = 0):
    proc = {"binary": binary, "pid": 7, "uid": 1, "ns": {"mnt": {"inum": inode}}}
    return {"k": "ev", "boot": boot, "seq": seq, "recv": recv,
            "e": {"process_exec": {"process": proc}, "time": f"t{seq}"}}  # fmt: skip


def snap(seq: int, boot: str = BOOT, mapping=None):
    return {"k": "scopes", "boot": boot, "seq": seq, "vm": 1,
            "map": {"200": [BOX]} if mapping is None else mapping}  # fmt: skip


@pytest.fixture(autouse=True)
def project(monkeypatch, tmp_path):
    monkeypatch.setattr(mh.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(mh.config, "project", lambda: "fyex")
    monkeypatch.setattr(mh.config, "project_prefix", lambda: "fyex")
    return tmp_path


# ── config ──────────────────────────────────────────────────────────────────────────────


GOOD = """
[[hook]]
name = "score"
command = ["/usr/local/bin/fy-score", "--json"]
kinds = ["exec", "connect"]

[[hook]]
name = "notify"
url = "https://hooks.example.org/fy"
headers = { Authorization = "Bearer x" }
who = ["container"]
timeout = 5
"""


def test_parse_accepts_command_and_url_hooks():
    hooks, errors = mh.parse(GOOD)
    assert errors == []
    assert [h.name for h in hooks] == ["score", "notify"]
    assert hooks[0].command == ("/usr/local/bin/fy-score", "--json")
    assert hooks[1].headers == (("Authorization", "Bearer x"),) and hooks[1].timeout == 5


@pytest.mark.parametrize(
    "body, why",
    [
        ('name = "x"\ncommand = ["fy-score"]', "absolute path"),  # no PATH lookup
        ('name = "x"\ncommand = ["/bin/x"]\nurl = "https://h"', "exactly one"),
        ('name = "x"', "exactly one"),
        ('name = "x"\nurl = "file:///etc/passwd"', "http(s)"),
        ('name = "x"\ncommand = "/bin/x --flag"', "list of strings"),  # never a shell string
        ('name = "x"\ncommand = ["/bin/x"]\nstage = "decided"', "stage"),
        ('name = "x"\ncommand = ["/bin/x"]\nheaders = { a = "b" }', "headers"),
        ('name = "x"\ncommand = ["/bin/x"]\ntimeout = 0', "timeout"),
        ('command = ["/bin/x"]', "name"),
    ],
)
def test_parse_refuses_a_bad_hook_and_keeps_the_rest(body, why):
    hooks, errors = mh.parse(f'[[hook]]\n{body}\n\n[[hook]]\nname = "ok"\ncommand = ["/bin/ok"]\n')
    assert [h.name for h in hooks] == ["ok"]
    assert len(errors) == 1 and why in errors[0]


def test_parse_refuses_duplicate_names():
    _, errors = mh.parse('[[hook]]\nname="a"\ncommand=["/x"]\n[[hook]]\nname="a"\ncommand=["/y"]\n')
    assert "duplicate" in errors[0]


def test_the_hooks_file_is_host_only(project):
    # it lives in the host state dir, outside the checkout the box can write — never in the repo
    assert mh.hooks_file() == project / "monitor-hooks.toml"
    assert mh.load() == ([], [])  # absent = no hooks, not an error


# ── settled + cursor ────────────────────────────────────────────────────────────────────


def test_only_settled_events_are_delivered():
    records = [snap(0), ev(1, recv=1000), snap(2), ev(3, recv=1000), ev(4, recv=1100)]
    # 1 has a snapshot after it; 3 and 4 don't — 3 is old enough, 4 isn't yet
    got = [e.seq for e in mh.settled(records, now=1000 + mh.SETTLE_SECONDS + 1)]
    assert got == [1, 3]


def test_settled_stops_at_the_first_unsettled_event():
    # A VM restarted inside the settle window: the old boot's tail has no snapshot after it and is
    # recent; the new boot's first snapshot settles its events at once. Delivering those would put
    # the cursor on the new boot and rank the old tail behind it — never delivered.
    records = [snap(0), ev(1, recv=900), ev(2, recv=990)]
    records += [snap(0, boot=BOOT2), ev(1, boot=BOOT2, recv=995), snap(2, boot=BOOT2)]
    got = [(e.boot, e.seq) for e in mh.settled(records, now=1000)]
    assert got == []
    got = [(e.boot, e.seq) for e in mh.settled(records, now=990 + mh.SETTLE_SECONDS)]
    assert got == [(BOOT, 1), (BOOT, 2), (BOOT2, 1)]


def test_after_follows_the_cursor_across_boots():
    events = monitorlog.attribute(
        [snap(0), ev(1), ev(2), snap(0, boot=BOOT2), ev(1, boot=BOOT2)], "fyex"
    )
    assert [(e.boot, e.seq) for e in mh.after(events, [BOOT, 1])] == [(BOOT, 2), (BOOT2, 1)]
    assert mh.after(events, []) == events
    assert mh.after(events, ["gone-boot", 99]) == events  # older than the whole window


# ── delivery ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def ran(monkeypatch):
    calls: list[dict] = []
    result = {"rc": 0}

    def fake_run(argv, input, capture_output, timeout, env):
        calls.append({"argv": argv, "body": json.loads(input), "env": env, "timeout": timeout})
        return subprocess.CompletedProcess(argv, result["rc"], b"", b"boom\n")

    monkeypatch.setattr(mh.subprocess, "run", fake_run)
    return calls, result


def _events(n: int):
    return monitorlog.attribute([snap(0), *[ev(i) for i in range(1, n + 1)], snap(n + 1)], "fyex")


def test_a_command_gets_the_batch_on_stdin_and_the_cursor_advances(ran):
    calls, _ = ran
    hook = mh.Hook(name="score", command=("/bin/score",))
    st = mh.HookState()
    assert mh.deliver(hook, st, _events(3), now=10) == ""
    body = calls[0]["body"]
    assert body["schema"] == mh.SCHEMA and body["hook"] == "score" and body["project"] == "fyex"
    assert [e["seq"] for e in body["events"]] == [1, 2, 3]
    assert body["events"][0]["who"] == "container" and body["events"][0]["container"] == BOX
    assert st.last == [BOOT, 3] and st.delivered == 3
    assert mh.deliver(hook, st, _events(3), now=11) == ""  # nothing new: nothing sent
    assert len(calls) == 1


def test_a_commands_environment_is_never_the_supervisors(ran, monkeypatch):
    calls, _ = ran
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "secret")  # host.env material lives here
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    mh.deliver(mh.Hook(name="score", command=("/bin/score",)), mh.HookState(), _events(1), now=1)
    env = calls[0]["env"]
    assert "GITHUB_APP_PRIVATE_KEY" not in env
    assert set(env) <= {"PATH", "HOME", "LANG", "TZ", "FY_PROJECT", "FY_HOOK"}
    assert env["FY_HOOK"] == "score"


def test_filters_pass_over_unwanted_events_without_sending(ran):
    calls, _ = ran
    hook = mh.Hook(name="net", command=("/bin/n",), kinds=("connect",))
    st = mh.HookState()
    mh.deliver(hook, st, _events(3), now=1)
    assert calls == [] and st.last == [BOOT, 3]


def test_a_failure_retries_with_backoff_then_skips_and_counts(ran):
    calls, result = ran
    result["rc"] = 1
    hook = mh.Hook(name="score", command=("/bin/score",))
    st = mh.HookState()
    now = 100.0
    msg = mh.deliver(hook, st, _events(2), now=now)
    assert "retrying" in msg and "exit 1: boom" in msg and st.last == []
    assert mh.deliver(hook, st, _events(2), now=now + 1) == ""  # backing off: not even tried
    assert len(calls) == 1
    for _ in range(mh.MAX_ATTEMPTS - 1):
        now = st.next_try
        msg = mh.deliver(hook, st, _events(2), now=now)
    assert "skipped 2 events" in msg
    assert st.last == [BOOT, 2] and st.dropped == 2 and st.delivered == 0


def test_a_narrow_hook_skips_a_long_unwanted_backlog_in_one_round(ran, monkeypatch):
    calls, _ = ran
    monkeypatch.setattr(mh, "BATCH_MAX", 2)  # scans of 8 events
    records = [snap(0), *[ev(i, inode=999) for i in range(1, 30)], ev(30), snap(31)]
    events = monitorlog.attribute(records, "fyex")
    hook = mh.Hook(name="ctr", command=("/bin/c",), who=("container",))
    st = mh.HookState()
    mh.deliver(hook, st, events, now=1)
    assert [[e["seq"] for e in c["body"]["events"]] for c in calls] == [[30]]


def test_a_url_hook_posts_json_with_its_headers(monkeypatch):
    sent = {}

    class _Resp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        sent.update(url=req.full_url, body=json.loads(req.data), headers=dict(req.header_items()))
        return _Resp()

    monkeypatch.setattr(mh.urllib.request, "urlopen", fake_urlopen)
    hook = mh.Hook(name="n", url="https://h.example/x", headers=(("Authorization", "Bearer t"),))
    st = mh.HookState()
    assert mh.deliver(hook, st, _events(1), now=1) == ""
    assert sent["url"] == "https://h.example/x" and sent["body"]["events"][0]["seq"] == 1
    assert sent["headers"]["Authorization"] == "Bearer t"
    assert st.last == [BOOT, 1]


def test_a_full_batch_stops_at_its_last_event(ran, monkeypatch):
    calls, _ = ran
    monkeypatch.setattr(mh, "BATCH_MAX", 2)
    hook = mh.Hook(name="score", command=("/bin/score",))
    st = mh.HookState()
    mh.deliver(hook, st, _events(5), now=1)
    mh.deliver(hook, st, _events(5), now=2)
    mh.deliver(hook, st, _events(5), now=3)
    assert [[e["seq"] for e in c["body"]["events"]] for c in calls] == [[1, 2], [3, 4], [5]]


def test_round_once_reads_the_store_and_saves_each_hooks_state(ran, project):
    calls, _ = ran
    (project / "monitor-hooks.toml").write_text('[[hook]]\nname = "s"\ncommand = ["/bin/s"]\n')
    monitorlog._append([snap(0), ev(1), ev(2), snap(3)])
    mh.round_once(now=50)
    state = json.loads((project / "monitor-hooks-state.json").read_text())
    assert state["s"]["last"] == [BOOT, 2] and state["s"]["delivered"] == 2
    assert state["s"]["pos"][0] == monitorstore.segments()[-1].id  # the segment, not an inode
    mh.round_once(now=60)
    assert len(calls) == 1  # the saved cursor holds across rounds


# ── reading from the hook's own position ────────────────────────────────────────────────


def _hook(project, name="s"):
    (project / "monitor-hooks.toml").write_text(
        f'[[hook]]\nname = "{name}"\ncommand = ["/bin/s"]\n'
    )


def _delivered(calls) -> list[int]:
    return [e["seq"] for c in calls for e in c["body"]["events"]]


def test_a_hook_further_behind_than_one_read_still_gets_every_event(ran, project, monkeypatch):
    # A catch-up pull stores up to 8 MB at once and a failing hook backs off for minutes: the
    # backlog outgrows any fixed tail. Read from the hook's position, nothing in it is skipped.
    calls, _ = ran
    _hook(project)
    monitorlog._append([snap(0), ev(1), snap(2)])
    mh.round_once(now=10)  # the hook is caught up: delivered 1, its position is after it
    monkeypatch.setattr(mh, "READ_BYTES", 600)  # a window a few records wide
    monitorlog._append([*[ev(i) for i in range(3, 40)], snap(40)])
    for t in range(20, 400, 10):
        mh.round_once(now=t)
    assert _delivered(calls) == [1, *range(3, 40)]


def test_a_position_the_store_rotated_away_is_said_and_reading_resumes(ran, project, monkeypatch):
    calls, _ = ran
    _hook(project)
    monitorlog._append([snap(0), ev(1), snap(2)])
    mh.round_once(now=10)
    monkeypatch.setattr(monitorstore, "SEGMENT_BYTES", 100)
    monkeypatch.setattr(monitorstore, "budget", lambda: 1)  # keeps only the live segment
    for i in range(3, 9, 2):  # each append seals the last segment, and the budget removes it
        monitorlog._append([ev(i), snap(i + 1)])
    logged: list[str] = []
    mh.round_once(now=500, log=logged.append)
    assert any("rotated past its position" in m for m in logged)
    assert _delivered(calls)[-1] == 7  # what the store still holds is delivered
    (gap,) = calls[-1]["body"]["gaps"]  # …and the batch says what it couldn't
    assert gap["why"] == "rotated" and gap["after"] == [BOOT, 1] and gap["before"] == [BOOT, 7]


def test_an_older_foldyards_position_is_found_again_by_the_cursor(ran, project):
    # it named an inode, which compression changes and ext4 hands out again: the cursor's
    # (boot, seq) finds the place instead — no re-reading from the oldest byte, no gap
    calls, _ = ran
    _hook(project)
    monitorlog._append([snap(0), ev(1), ev(2), snap(3)])
    (project / "monitor-hooks-state.json").write_text(
        json.dumps({"s": {"last": [BOOT, 1], "pos": [64240151, 399]}})
    )
    logged: list[str] = []
    mh.round_once(now=20, log=logged.append)
    assert logged == [] and _delivered(calls) == [2] and calls[0]["body"]["gaps"] == []


def test_a_new_hook_is_anchored_where_it_first_reads(ran, project, monkeypatch):
    # nothing settled yet the first time it looks: it must not slide along with the tail
    calls, _ = ran
    _hook(project)
    monkeypatch.setattr(mh, "READ_BYTES", 300)
    monitorlog._append([snap(0), ev(1, recv=100)])
    mh.round_once(now=110)  # 1 has no snapshot after it and is recent: nothing to send
    assert calls == []
    monitorlog._append([*(ev(i, recv=120) for i in range(2, 30)), snap(30)])
    for t in range(130, 400, 10):
        mh.round_once(now=t)
    assert _delivered(calls) == list(range(1, 30))


def test_a_far_snapshot_and_a_far_claim_still_attribute_the_events_between(
    ran, project, monkeypatch
):
    # The 2026-10-21 loss: in a storm two snapshots are megabytes apart, more than any read. The
    # events between must still be joined to both — and named by a claim made long before.
    calls, _ = ran
    (project / "monitor-hooks.toml").write_text(
        '[[hook]]\nname = "s"\ncommand = ["/bin/s"]\nwho = ["container"]\n'
    )
    monkeypatch.setattr(mh, "READ_BYTES", 400)  # a few events a read
    meta = {"name": "fyex-devbox", "project": "", "service": ""}
    monitorlog._append([{"k": "engine", "containers": {BOX: meta}}, snap(0), ev(1)])
    mh.round_once(now=10)
    monitorlog._append([*(ev(i) for i in range(2, 60)), snap(60), ev(61, recv=1000)])
    for t in range(20, 300, 10):
        mh.round_once(now=t)
    got = [e for c in calls for e in c["body"]["events"]]
    assert [e["seq"] for e in got] == list(range(1, 60))
    assert {(e["who"], e["name"], e["mnt"]) for e in got} == {("container", "fyex-devbox", 200)}
    assert all(c["body"]["gaps"] == [] for c in calls)


def test_a_later_snapshot_beyond_the_read_settles_events_at_once(ran, project, monkeypatch):
    # the snapshot after them exists — just further on than one read: no settle-window wait
    calls, _ = ran
    _hook(project)
    monkeypatch.setattr(mh, "READ_BYTES", 300)
    monitorlog._append([snap(0), ev(1, recv=100)])
    mh.round_once(now=101)
    monitorlog._append([*(ev(i, recv=100) for i in range(2, 30)), snap(30)])
    mh.round_once(now=102)
    assert _delivered(calls)[:2] == [1, 2]


def test_events_released_without_a_later_snapshot_are_said(ran, project):
    calls, _ = ran
    _hook(project)
    monitorlog._append([snap(0), ev(1, recv=100), ev(2, inode=999, recv=100)])
    mh.round_once(now=100 + mh.SETTLE_SECONDS + 1)  # the relay went quiet: released on age
    assert _delivered(calls) == [1, 2]
    (gap,) = calls[0]["body"]["gaps"]
    assert (gap["why"], gap["after"], gap["before"]) == ("unsettled", [BOOT, 0], [BOOT, 3])


def test_ambiguous_events_a_who_filter_passes_over_are_said(ran, project):
    # a namespace number two snapshots name different containers for, the containers' inits not
    # saying where it changed hands: events a `who = ["container"]` hook can't be sent — and must
    # not read as "the container did nothing"
    calls, _ = ran
    (project / "monitor-hooks.toml").write_text(
        '[[hook]]\nname = "c"\ncommand = ["/bin/c"]\nwho = ["container"]\n'
        '[[hook]]\nname = "both"\ncommand = ["/bin/b"]\nwho = ["container", "ambiguous"]\n'
        '[[hook]]\nname = "net"\ncommand = ["/bin/n"]\nwho = ["container"]\nkinds = ["connect"]\n'
        '[[hook]]\nname = "vm"\ncommand = ["/bin/v"]\nwho = ["vm"]\n'
    )
    other = "c" * 64
    monitorlog._append([snap(0), ev(1), ev(2, inode=999), ev(3), snap(4, mapping={"200": [other]})])
    mh.round_once(now=50)
    by = {c["env"]["FY_HOOK"]: c["body"] for c in calls}
    (gap,) = by["c"]["gaps"]
    assert (gap["why"], gap["after"], gap["before"], gap["mnt"]) == (
        "ambiguous",
        [BOOT, 0],
        [BOOT, 4],
        200,
    )
    assert (gap["since"], gap["until"]) == ("t1", "t3") and "2 events" in gap["detail"]
    assert by["both"]["gaps"] == [] and len(by["both"]["events"]) == 2  # it takes them
    assert "net" not in by  # it wanted none of them anyway: connections only
    assert "vm" not in by  # …nor this one: none of them could be the VM's
    mh.round_once(now=60)
    assert len(calls) == 2  # said once


def test_transport_and_relay_gaps_reach_the_hook_even_when_it_wants_nothing_there(ran, project):
    calls, _ = ran
    (project / "monitor-hooks.toml").write_text(
        '[[hook]]\nname = "s"\ncommand = ["/bin/s"]\nwho = ["container"]\n'
    )
    relay = {
        "k": "gap",
        "boot": BOOT,
        "seq": 3,
        "t": 1.0,
        "why": "tetragon's export rotated unseen",
    }
    lost = {"k": "gap", "boot": BOOT, "from": 5, "to": 6}
    monitorlog._append([snap(0), ev(1, inode=1), ev(2, inode=1), relay, ev(4, inode=1), lost,
                        ev(7, inode=1), snap(8)])  # fmt: skip
    mh.round_once(now=50)
    assert len(calls) == 1 and calls[0]["body"]["events"] == []  # the vm's: not this hook's
    assert calls[0]["body"]["first"] is None
    gaps = calls[0]["body"]["gaps"]
    assert [(g["why"], g["after"], g["before"]) for g in gaps] == [
        ("relay", [BOOT, 2], [BOOT, 4]),
        ("transport", [BOOT, 4], [BOOT, 7]),
    ]
    mh.round_once(now=60)
    assert len(calls) == 1  # said once


def test_a_skipped_batch_is_owed_to_the_next_one_as_a_gap(ran):
    calls, result = ran
    hook = mh.Hook(name="s", command=("/bin/s",))
    st = mh.HookState()
    result["rc"] = 1
    for t in range(mh.MAX_ATTEMPTS):
        mh.deliver(hook, st, _events(2), now=st.next_try + t)
    assert st.dropped == 2 and [g["why"] for g in st.gaps] == ["skipped"]
    result["rc"] = 0
    mh.deliver(hook, st, _events(3), now=st.next_try + 1)
    assert [g["why"] for g in calls[-1]["body"]["gaps"]] == ["skipped"] and st.gaps == []


def test_round_once_logs_config_errors_and_carries_on(project, monkeypatch):
    (project / "monitor-hooks.toml").write_text('[[hook]]\nname = "s"\ncommand = ["s"]\n')
    logged = []
    mh.round_once(now=1, log=logged.append)
    assert logged and "absolute path" in logged[0]


# ── the file sink ───────────────────────────────────────────────────────────────────────


def test_parse_refuses_two_hooks_writing_one_file():
    hooks, errors = mh.parse(
        '[[hook]]\nname = "a"\nfile = "/e.jsonl"\n[[hook]]\nname = "b"\nfile = "/e.jsonl"\n'
    )
    assert [h.name for h in hooks] == ["a"] and len(errors) == 1 and "'a'" in errors[0], errors


def test_parse_accepts_a_file_hook():
    hooks, errors = mh.parse(
        '[[hook]]\nname = "ca"\nfile = "/srv/ca/events.jsonl"\nwho = ["container"]\n'
    )
    assert errors == []
    assert hooks[0].file == "/srv/ca/events.jsonl" and hooks[0].who == ("container",)


@pytest.mark.parametrize(
    "body, why",
    [
        ('name = "x"\nfile = "logs/e.jsonl"', "absolute"),
        ('name = "x"\nfile = "/a/../e.jsonl"', "normal"),
        ('name = "x"\nfile = "/a/./e.jsonl"', "normal"),
        ('name = "x"\nfile = "/a/"', "normal"),
        ('name = "x"\nfile = 3', "absolute"),
        ('name = "x"\nfile = "/e"\ncommand = ["/bin/x"]', "exactly one"),
        ('name = "x"\nfile = "/e"\nheaders = { a = "b" }', "headers"),
    ],
)
def test_parse_refuses_a_bad_file_hook(body, why):
    hooks, errors = mh.parse(f"[[hook]]\n{body}\n")
    assert hooks == [] and len(errors) == 1 and why in errors[0], errors


def _sink_hook(tmp_path, name="ca", **kw):
    (tmp_path / "logs").mkdir(exist_ok=True)
    return mh.Hook(name=name, file=str(tmp_path / "logs" / "events.jsonl"), **kw)


def _check(hook, st):
    from foldyard import monitorsink

    data, key = monitorsink.read(hook.file), monitorsink.load_key()
    assert key is not None
    return monitorsink.verify(data, key, hook.name, (st.sink_seq, st.sink_mac))


def test_a_file_hook_appends_signed_chained_lines(project, monkeypatch, tmp_path):
    monkeypatch.setattr(mh, "BATCH_MAX", 2)
    hook = _sink_hook(tmp_path)
    st = mh.HookState()
    assert mh.deliver(hook, st, _events(3), now=1) == ""
    assert mh.deliver(hook, st, _events(3), now=2) == ""
    lines = [json.loads(x) for x in open(hook.file, "rb")]
    assert [[e["seq"] for e in x["events"]] for x in lines] == [[1, 2], [3]]
    assert [x["sink"]["seq"] for x in lines] == [1, 2] and lines[1]["sink"]["prev"] == lines[0][
        "sink"
    ]["mac"]
    assert lines[0]["hook"] == "ca" and lines[0]["schema"] == mh.SCHEMA
    assert st.sink_seq == 2 and st.sink_mac == lines[1]["sink"]["mac"] and st.last == [BOOT, 3]
    assert _check(hook, st).problem == ""
    assert (project / "monitor-sink.key").exists()  # made on first use, in the state dir


def test_a_refused_file_is_a_failed_delivery_and_nothing_is_written(project, tmp_path):
    victim = tmp_path / "zshrc"
    victim.write_text("keep\n")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "events.jsonl").symlink_to(victim)
    hook = mh.Hook(name="ca", file=str(tmp_path / "logs" / "events.jsonl"))
    st = mh.HookState()
    msg = mh.deliver(hook, st, _events(2), now=1)
    assert "retrying" in msg and "symlink" in msg
    assert st.last == [] and st.sink_seq == 0 and victim.read_text() == "keep\n"


def test_a_write_torn_part_way_is_undone_retried_and_the_file_checks(
    project, monkeypatch, tmp_path
):
    from foldyard import monitorsink

    hook = _sink_hook(tmp_path)
    st = mh.HookState()
    mh.deliver(hook, st, _events(1), now=1)
    real_write = monitorsink.os.write

    def tear(fd, data):  # 30 bytes reach the file, then the disk fills
        real_write(fd, bytes(data[:30]))
        monkeypatch.setattr(monitorsink.os, "write", real_write)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(monitorsink.os, "write", tear)
    msg = mh.deliver(hook, st, _events(2), now=2)
    assert "retrying" in msg and st.sink_seq == 1
    assert mh.deliver(hook, st, _events(2), now=st.next_try) == ""
    got = _check(hook, st)
    assert got.problem == "" and got.last_seq == 2 and got.lines == 2 and got.notes == []


def test_a_hook_pointed_at_another_file_starts_a_fresh_chain_there(project, tmp_path):
    # the chain belongs to the file, not the name: re-adding `ca` with another --file (or editing
    # `file =`) must not carry seq/prev from the old file into the new one
    hook = _sink_hook(tmp_path)
    st = mh.HookState()
    assert mh.deliver(hook, st, _events(1), now=1) == ""
    moved = mh.Hook(name="ca", file=str(tmp_path / "logs" / "other.jsonl"))
    assert mh.deliver(moved, st, _events(2), now=2) == ""
    first = json.loads(open(moved.file, "rb").readline())
    assert first["sink"]["seq"] == 1 and first["sink"]["prev"] == ""
    assert st.sink_file == moved.file and _check(moved, st).problem == ""


def test_a_missing_file_at_the_same_path_keeps_its_chain(project, tmp_path):
    # a box deleting the file must keep failing the check: the next line is NOT a fresh seq 1
    hook = _sink_hook(tmp_path)
    st = mh.HookState()
    assert mh.deliver(hook, st, _events(1), now=1) == ""
    os.unlink(hook.file)
    assert mh.deliver(hook, st, _events(2), now=2) == ""
    assert json.loads(open(hook.file, "rb").readline())["sink"]["seq"] == 2
    assert "seq" in _check(hook, st).problem


def test_round_once_records_the_sinks_sequence(project, tmp_path):
    hook = _sink_hook(tmp_path)
    (project / "monitor-hooks.toml").write_text(f'[[hook]]\nname = "ca"\nfile = "{hook.file}"\n')
    monitorlog._append([snap(0), ev(1), ev(2), snap(3)])
    mh.round_once(now=50)
    state = json.loads((project / "monitor-hooks-state.json").read_text())
    assert state["ca"]["sink_seq"] == 1 and len(state["ca"]["sink_mac"]) == 64


def test_state_from_a_newer_foldyard_keeps_what_it_knows(project):
    # an unknown field must not throw every hook's cursor away (HookState(**v) would)
    (project / "monitor-hooks-state.json").write_text(
        json.dumps({"s": {"last": [BOOT, 4], "delivered": 4, "from_the_future": 1}})
    )
    assert mh._load_state()["s"].last == [BOOT, 4]


# ── editing monitor-hooks.toml (`fy monitor hook add|remove`) ───────────────────────────


def test_add_creates_the_file_owner_only(project):
    mh.add(mh.Hook(name="ca", file="/srv/ca/events.jsonl", who=("container",)))
    path = project / "monitor-hooks.toml"
    assert oct(path.stat().st_mode & 0o777) == "0o600"  # a url hook's headers may hold a token
    hooks, errors = mh.load()
    assert errors == [] and hooks == [
        mh.Hook(name="ca", file="/srv/ca/events.jsonl", who=("container",))
    ]


HAND = """\
# my hooks — hand-written
[[hook]]
name = "score"   # the scorer
command = ["/usr/local/bin/fy-score", "--json"]

[[hook]]
name = "notify"
url = "https://hooks.example.org/fy"
[hook.headers]
Authorization = "Bearer t"

# trailing comment
"""


def test_add_appends_and_keeps_every_hand_edit(project):
    (project / "monitor-hooks.toml").write_text(HAND)
    odd = mh.Hook(name='a "quoted" \\ hook', command=("/bin/sh", "-c", 'echo "$1"\n', "é"))
    mh.add(odd)
    text = (project / "monitor-hooks.toml").read_text()
    assert text.startswith(HAND)
    hooks, errors = mh.load()
    assert errors == [] and [h.name for h in hooks] == ["score", "notify", odd.name]
    assert hooks[-1] == odd


def test_add_refuses_a_taken_name_even_an_invalid_hooks(project):
    (project / "monitor-hooks.toml").write_text('[[hook]]\nname = "ca"\ncommand = ["rel"]\n')
    with pytest.raises(ValueError, match="already"):
        mh.add(mh.Hook(name="ca", file="/srv/e.jsonl"))


def test_add_refuses_what_the_supervisor_would_and_writes_nothing(project):
    with pytest.raises(ValueError, match="absolute"):
        mh.add(mh.Hook(name="ca", command=("fy-score",)))
    assert not (project / "monitor-hooks.toml").exists()


def test_remove_drops_one_block_and_keeps_the_rest(project):
    (project / "monitor-hooks.toml").write_text(HAND)
    mh.remove("notify")  # its [hook.headers] sub-table goes with it
    text = (project / "monitor-hooks.toml").read_text()
    assert "# my hooks — hand-written" in text and "# the scorer" in text and "Bearer" not in text
    assert [h.name for h in mh.load()[0]] == ["score"]
    mh.remove("score")
    assert mh.load() == ([], [])


def test_remove_refuses_an_unknown_name(project):
    (project / "monitor-hooks.toml").write_text(HAND)
    with pytest.raises(ValueError, match="no hook"):
        mh.remove("nope")


def test_remove_refuses_a_file_it_cant_edit_exactly_and_leaves_it(project):
    inline = 'hook = [{ name = "a", command = ["/bin/a"] }, { name = "b", command = ["/bin/b"] }]\n'
    (project / "monitor-hooks.toml").write_text(inline)
    with pytest.raises(ValueError, match="by hand"):
        mh.remove("a")
    assert (project / "monitor-hooks.toml").read_text() == inline


# ── listing (`fy monitor hook`) ─────────────────────────────────────────────────────────


def test_listing_shows_each_hook_its_filters_and_its_health(project):
    (project / "monitor-hooks.toml").write_text(
        HAND + '\n[[hook]]\nname = "ca"\nfile = "/srv/ca/e.jsonl"\nwho = ["container"]\n'
        '\n[[hook]]\nname = "bad"\ncommand = ["rel"]\n'
    )
    mh._save_state({
        "score": mh.HookState(delivered=12, ok_at=1000.0),
        "ca": mh.HookState(attempts=2, error="refused: a symlink", dropped=5, next_try=1040.0),
    })  # fmt: skip
    text = "\n".join(mh.listing(now=1010.0))
    assert "score" in text and "command" in text and "/usr/local/bin/fy-score --json" in text
    assert "12 events delivered" in text and "10s ago" in text
    assert "Bearer" not in text and "Authorization" in text  # header names, never their values
    assert "file" in text and "/srv/ca/e.jsonl" in text and "who=container" in text
    assert "failing" in text and "refused: a symlink" in text and "5 events skipped" in text
    assert "bad" in text and "absolute path" in text  # a hook the supervisor skips is said


# ── `fy monitor hook add NAME --to-box`: the sink in ADR-0033's to-box ─────────────────────


@pytest.fixture
def to_box(monkeypatch):
    from foldyard import machine

    why = {"value": None}
    monkeypatch.setattr(machine, "to_box_unavailable", lambda: why["value"])
    monkeypatch.setattr(mh, "_box_writable_roots", lambda: [])
    return why


def test_add_to_box_puts_the_signed_sink_in_the_to_box(project, to_box, capsys):
    assert mh.cli_add("experiment", to_box=True) == 0
    path = project / "to-box" / "monitor" / "experiment.jsonl"
    hooks, errors = mh.load()
    assert errors == [] and hooks == [mh.Hook(name="experiment", file=str(path))]
    assert path.parent.is_dir() and path.parent.stat().st_mode & 0o077 == 0
    out = capsys.readouterr().out
    assert "/run/foldyard/to-box/monitor/experiment.jsonl" in out  # where the box reads it
    assert "fy monitor check" in out


def test_add_to_box_without_one_says_why_and_points_at_file(project, to_box, capsys):
    to_box["value"] = "the podman backend has no to-box: …"
    assert mh.cli_add("experiment", to_box=True) == 1
    out = capsys.readouterr().out
    assert "podman backend has no to-box" in out and "--file PATH" in out
    assert not (project / "monitor-hooks.toml").exists()
    assert not (project / "to-box").exists()


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden"])
def test_add_to_box_refuses_a_name_that_isnt_a_file_name(project, to_box, name):
    assert mh.cli_add(name, to_box=True) == 1
    assert not (project / "monitor-hooks.toml").exists()


def test_add_to_box_and_file_together_is_refused(project, to_box, capsys):
    assert mh.cli_add("x", to_box=True, file=str(project / "e.jsonl")) == 1
    assert "exactly one of `--to-box`" in capsys.readouterr().out


def test_a_file_in_a_box_writable_tree_gets_a_warning(project, monkeypatch, capsys, tmp_path):
    checkout = tmp_path / "repo"
    (checkout / "logs").mkdir(parents=True)
    monkeypatch.setattr(mh, "_box_writable_roots", lambda: [(checkout, "the checkout")])
    assert mh.cli_add("ca", file=str(checkout / "logs" / "e.jsonl")) == 0
    out = capsys.readouterr().out
    assert "it is in the checkout, which the box can write" in out and "--to-box" in out
    assert mh.cli_add("cb", file=str(tmp_path / "e.jsonl")) == 0
    assert "which the box can write" not in capsys.readouterr().out


# ── 0.6.1's upgrade: a false "rotated", then a hook stuck behind its own cursor ─────────────


def _older_foldyards_store(project, live_from: int, live_to: int):
    """A 0.6.0 store: a dated backup and a live file, neither with a first line naming it."""
    logs = project / "logs"
    logs.mkdir(exist_ok=True)
    dump = lambda *rs: "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rs)  # noqa: E731
    (logs / "monitor.20261001T000000Z.jsonl").write_text(dump(snap(0), *map(ev, range(1, 5))))
    (logs / "monitor.jsonl").write_text(dump(*map(ev, range(live_from, live_to)), snap(live_to)))


def test_a_segment_sealed_while_the_catalogue_caught_up_is_not_taken_for_lost(
    ran, project, monkeypatch
):
    # cabash, 2026-10-22: the new supervisor's first hook round listed the store, the pull then
    # sealed the older foldyard's live file under a new id, and the catalogue — caught up with
    # the listing from before — had no span for it. The cursor was in it; the hook was told
    # "rotated" and sent back to the oldest segment.
    calls, _ = ran
    _hook(project)
    _older_foldyards_store(project, 5, 12)
    (project / "monitor-hooks-state.json").write_text(
        json.dumps({"s": {"last": [BOOT, 8], "pos": [64240151, 399]}})  # an inode
    )
    real = monitorlog.catalogue

    def pull_seals_under_the_round(segs=None):
        before = monitorstore.segments()
        monitorlog._append([ev(13), snap(14)])  # the pull's first append seals the legacy file
        return real(before)

    monkeypatch.setattr(monitorlog, "catalogue", pull_seals_under_the_round)
    logged: list[str] = []
    mh.round_once(now=50, log=logged.append)
    monkeypatch.setattr(monitorlog, "catalogue", real)
    mh.round_once(now=60, log=logged.append)
    assert logged == []
    assert _delivered(calls) == [9, 10, 11, 13]
    assert all(c["body"]["gaps"] == [] for c in calls)


def test_a_round_reads_with_the_catalogue_caught_up_with_what_it_reads(ran, project, monkeypatch):
    # the round's catalogue came up to date with a listing from before a segment appeared under
    # it (the pull sealing with its own catalogue locked): that segment's snapshot — too far
    # back for the read — must still attribute the events after it
    calls, _ = ran
    (project / "monitor-hooks.toml").write_text(
        '[[hook]]\nname = "s"\ncommand = ["/bin/s"]\nwho = ["container"]\n'
    )
    monkeypatch.setattr(mh, "READ_BYTES", 400)
    monitorlog._append([snap(0), ev(1)])
    mh.round_once(now=10 + mh.SETTLE_SECONDS)  # delivered 1, on age
    monitorlog._append([*(ev(i) for i in range(2, 12)), snap(12, mapping={})])
    for p in (project / "logs").glob("monitor-index.sqlite*"):
        p.unlink()  # as if no catalogue had seen any of it
    monkeypatch.setattr(monitorlog, "catalogue", lambda segs=None: monitorstore.Catalogue.open())
    for t in range(500, 700, 10):
        mh.round_once(now=t)
    assert _delivered(calls) == list(range(1, 12))


def test_a_hook_left_behind_its_cursor_resumes_right_after_it(ran, project, monkeypatch):
    # what that false "rotated" left: the position at the oldest segment's start, the cursor
    # in a later one. Every read was all events before the cursor — nothing to send, nothing
    # passed, the position never moved: silent, for good.
    calls, _ = ran
    _hook(project)
    monkeypatch.setattr(monitorstore, "SEGMENT_BYTES", 300)
    monkeypatch.setattr(mh, "READ_BYTES", 300)
    for i in range(0, 40, 4):
        monitorlog._append([snap(i), ev(i + 1), ev(i + 2), ev(i + 3)])
    oldest = monitorstore.segments()[0].id
    (project / "monitor-hooks-state.json").write_text(
        json.dumps({"s": {"last": [BOOT, 30], "pos": [oldest, 0], "delivered": 7}})
    )
    monitorlog._append([snap(40)])
    logged: list[str] = []
    for t in range(50, 100, 5):
        mh.round_once(now=t, log=logged.append)
    assert logged == []
    assert _delivered(calls) == [31, 33, 34, 35, 37, 38, 39]
    assert all(c["body"]["gaps"] == [] for c in calls)


def test_a_hook_behind_a_cursor_the_store_cant_find_walks_on_to_it(ran, project, monkeypatch):
    # the cursor's own event isn't there to jump to: the position walks forward, read by read
    calls, _ = ran
    _hook(project)
    monkeypatch.setattr(monitorstore, "SEGMENT_BYTES", 300)
    monkeypatch.setattr(mh, "READ_BYTES", 300)
    for i in range(0, 40, 4):
        monitorlog._append([snap(i), *(ev(j) for j in range(i + 1, i + 4) if j != 30)])
    oldest = monitorstore.segments()[0].id
    (project / "monitor-hooks-state.json").write_text(
        json.dumps({"s": {"last": [BOOT, 30], "pos": [oldest, 0]}})
    )
    monitorlog._append([snap(40)])
    for t in range(50, 100, 5):
        mh.round_once(now=t)
    assert _delivered(calls) == [31, 33, 34, 35, 37, 38, 39]


def test_a_new_hooks_position_past_its_segments_end_is_said_not_waited_on(ran, project):
    # no cursor to find it by, and the segment it names is there but shorter: never "look again"
    # for good (nothing would say so — a hook that never delivered has no progress to compare)
    calls, _ = ran
    _hook(project)
    monitorlog._append([snap(0), ev(1), ev(2), snap(3)])
    live = monitorstore.segments()[-1].id
    (project / "monitor-hooks-state.json").write_text(json.dumps({"s": {"pos": [live, 1 << 30]}}))
    logged: list[str] = []
    mh.round_once(now=20, log=logged.append)
    assert any("rotated past its position" in m for m in logged) and _delivered(calls) == [1, 2]


def test_a_new_hooks_segment_sealed_under_the_listing_is_looked_at_again_not_lost(
    ran, project, monkeypatch
):
    # the listing still shows its segment live, but the live file is a new, shorter one now:
    # the position reads as past the end for a moment, which proves nothing
    calls, _ = ran
    _hook(project)
    monitorlog._append([snap(0), ev(1), *(ev(i, inode=999) for i in range(2, 9))])
    monkeypatch.setattr(mh, "READ_BYTES", 500)  # anchored well into the segment
    mh.round_once(now=20)  # nothing settled yet
    assert calls == [] and mh.load_state()["s"].pos[1] > 600
    stale = monitorstore.segments()
    monkeypatch.setattr(monitorstore, "SEGMENT_BYTES", 10)
    monitorlog._append([snap(9)])  # seals the live segment, starts a new one
    real = monitorstore.segments
    listings = iter([real(), stale])  # the round's catalogue lists first, then the hook's read
    monkeypatch.setattr(monitorstore, "segments", lambda: next(listings, None) or real())
    logged: list[str] = []
    mh.round_once(now=30, log=logged.append)
    mh.round_once(now=40, log=logged.append)
    assert logged == [] and calls and set(_delivered(calls)) <= set(range(2, 9))
    assert all(c["body"]["gaps"] == [] for c in calls)


def test_a_store_that_moves_under_the_search_is_looked_at_again_not_lost(ran, project, monkeypatch):
    # no catalogue (locked), and a listing from before the seal: the legacy file the search
    # reads has just been renamed. Nothing proves the cursor's events gone: look again.
    calls, _ = ran
    _hook(project)
    _older_foldyards_store(project, 5, 12)
    (project / "monitor-hooks-state.json").write_text(
        json.dumps({"s": {"last": [BOOT, 8], "pos": [64240151, 399]}})
    )
    monkeypatch.setattr(monitorlog, "catalogue", lambda segs=None: None)
    stale = monitorstore.segments()
    monitorlog._append([ev(13), snap(14)])
    real = monitorstore.segments
    listings = iter([stale])
    monkeypatch.setattr(monitorstore, "segments", lambda: next(listings, None) or real())
    logged: list[str] = []
    mh.round_once(now=50, log=logged.append)
    mh.round_once(now=60, log=logged.append)
    assert logged == []
    assert _delivered(calls) == [9, 10, 11, 13]
    assert all(c["body"]["gaps"] == [] for c in calls)


def test_a_hook_making_no_progress_while_events_arrive_says_so(project):
    # it used to read ✓ — the stuck hook above was found by its file going quiet
    _hook(project)
    monitorlog._append([snap(0), ev(1, recv=5000.0), snap(2)])  # snapshots alone aren't progress
    st = mh.HookState(last=[BOOT, 1], delivered=3, ok_at=1400.0, moved_at=1400.0)
    (why,) = mh.problems(st, now=5001.0)
    assert "no progress for 60 min" in why and f"{BOOT[:8]}:1" in why
    assert mh.problems(mh.HookState(moved_at=4990.0), now=5001.0) == []  # moving
    assert mh.problems(mh.HookState(ok_at=1400.0), now=9000.0, grew=1500.0) == []  # nothing new
    mh._save_state({"s": st})
    assert any("⚠" in line and "no progress" in line for line in mh.listing(now=5001.0))


# ── who a hook gets by default, and what it passed over ────────────────────────────────


def entered(seq: int, inode: int = 7) -> dict:
    """podman entering its own namespace from the VM's: every event there after it is podman's."""
    rec = ev(seq, inode=inode, binary="/usr/bin/conmon")
    body = rec["e"]["process_exec"]
    body["process"]["ns"]["user"] = {"inum": 4026532575}
    body["parent"] = {"binary": "/usr/bin/podman", "ns": {"mnt": {"inum": 1, "is_host": True}}}
    return rec


def _mixed():
    # a container's event, the VM's, podman's, and one nothing names
    return monitorlog.attribute(
        [snap(0), ev(1), ev(2, inode=1), entered(3), ev(4, inode=7), ev(5, inode=999), snap(6)],
        "fyex",
    )


def test_a_hook_without_who_skips_the_vm_and_podman_and_counts_what_it_passed_over(ran):
    calls, _ = ran
    assert [e.who for e in _mixed()] == ["container", "vm", "podman", "podman", "unattributed"]
    st = mh.HookState()
    mh.deliver(mh.Hook(name="all", command=("/bin/a",)), st, _mixed(), now=1)
    body = calls[0]["body"]
    assert [e["seq"] for e in body["events"]] == [1, 5]
    assert body["passed"] == {"vm": 1, "podman": 2}  # never silently: said in the batch
    assert st.passed == {} and st.last == [BOOT, 5]


def test_what_a_hook_passed_over_rides_with_its_next_batch(ran):
    calls, result = ran
    hook = mh.Hook(name="c", command=("/bin/c",), who=("container",))
    st = mh.HookState()
    quiet = [e for e in _mixed() if e.who != "container"]
    mh.deliver(hook, st, quiet, now=1)
    assert calls == [] and st.passed == {"vm": 1, "podman": 2, "unattributed": 1}
    later = monitorlog.attribute([snap(0), ev(6), snap(7)], "fyex")
    result["rc"] = 1  # a failed attempt counts nothing twice
    mh.deliver(hook, st, later, now=2)
    result["rc"] = 0
    mh.deliver(hook, st, later, now=st.next_try)
    assert [c["body"]["passed"] for c in calls] == [
        {"vm": 1, "podman": 2, "unattributed": 1},
        {"vm": 1, "podman": 2, "unattributed": 1},
    ]
    assert st.passed == {}


def test_the_categories_no_hook_gets_unless_it_asks_are_the_operators_to_set(project):
    hooks, errors = mh.parse(
        'skip_unless_asked = ["vm", "podman", "build"]\n'
        '[[hook]]\nname = "all"\ncommand = ["/bin/a"]\n'
        '[[hook]]\nname = "b"\ncommand = ["/bin/b"]\nwho = ["build"]\n'
    )
    assert errors == []
    build = monitorlog.Event(time="t", boot=BOOT, seq=1, kind="exec", binary="/bin/sh", args="",
                             detail="", uid=0, pid=1, who="build")  # fmt: skip
    assert not hooks[0].matches(build) and hooks[1].matches(build)
    hooks, errors = mh.parse(
        'skip_unless_asked = ["podmen"]\n[[hook]]\nname = "a"\ncommand = ["/bin/a"]\n'
    )
    assert "podmen" in errors[0]
    assert hooks[0].unless_asked == mh.SKIP_UNLESS_ASKED  # a typo never widens or narrows it


def test_skip_unless_asked_after_a_hook_is_reported_never_silently_ignored(project):
    # TOML gives a key written after `[[hook]]` to that hook's table: the setting would then do
    # nothing, without a word — the default stays, and the operator is told where the key belongs
    hooks, errors = mh.parse('[[hook]]\nname = "a"\ncommand = ["/bin/a"]\nskip_unless_asked = []\n')
    assert [h.name for h in hooks] == ["a"]  # the hook itself still runs
    assert hooks[0].unless_asked == mh.SKIP_UNLESS_ASKED
    assert len(errors) == 1 and "skip_unless_asked" in errors[0] and "top" in errors[0]
    _, errors = mh.parse(
        'skip_unless_asked = []\n[[hook]]\nname = "a"\ncommand = ["/bin/a"]\nskip_unless_asked = []\n'
    )
    assert "nothing skipped kept" in errors[0]  # what is in force, said even when it is nothing


def test_an_event_that_crossed_out_of_a_container_reaches_every_hook(ran):
    calls, _ = ran
    crossed = monitorlog.attribute(
        [snap(0), entered(1), {**ev(2, inode=7), "e": {"process_exec": {
            "process": {"binary": "/bin/sh", "pid": 9, "ns": {"mnt": {"inum": 7}}},
            "parent": {"ns": {"mnt": {"inum": 200}}}}, "time": "t2"}}, snap(3)],
        "fyex",
    )  # fmt: skip
    assert (crossed[1].who, crossed[1].crossed) == ("container", True)
    mh.deliver(mh.Hook(name="b", command=("/bin/b",), who=("build",)), mh.HookState(), crossed, 1)
    assert [e["seq"] for e in calls[0]["body"]["events"]] == [2]


def test_listing_says_what_a_hook_skips_unless_asked_and_what_it_passed_over(project):
    (project / "monitor-hooks.toml").write_text('[[hook]]\nname = "all"\ncommand = ["/bin/a"]\n')
    mh._save_state({"all": mh.HookState(passed={"podman": 40, "vm": 2})})
    text = "\n".join(mh.listing(now=10.0))
    assert "all events but vm, podman" in text
    assert "passed over since its last batch: podman 40, vm 2" in text


# ── a crossing unfilters the rest of its boot ────────────────────────────────────────────


def crossing(seq: int, inode: int = 1, parent: int = 200, boot: str = BOOT) -> dict:
    """A process in the VM's mount namespace whose parent was in a container's (200)."""
    rec = ev(seq, inode=inode, binary="/bin/sh", boot=boot)
    body = rec["e"]["process_exec"]
    body["process"]["ns"]["mnt"]["is_host"] = True
    body["parent"] = {"binary": "/bin/sh", "ns": {"mnt": {"inum": parent}}}
    return rec


def _crossed_boot():
    # the VM's and podman's events before a crossing and after it, in one boot
    return [snap(0), ev(1, inode=1), entered(2), ev(3), crossing(4), ev(5, inode=1),
            ev(6, inode=7), ev(7), snap(8)]  # fmt: skip


def test_after_a_crossing_a_default_hook_gets_the_vm_and_podman_and_says_why(ran):
    calls, _ = ran
    events = monitorlog.attribute(_crossed_boot(), "fyex")
    first = mh.crossings(events)
    assert first == {BOOT: mh.Crossing(BOOT, 4, "t4", BOX)}
    st = mh.HookState()
    mh.deliver(mh.Hook(name="all", command=("/bin/a",)), st, events, now=1, first_crossed=first)
    body = calls[0]["body"]
    assert [e["seq"] for e in body["events"]] == [3, 4, 5, 6, 7]  # before 4: as without it
    assert body["passed"] == {"vm": 1, "podman": 1}
    (u,) = body["unfiltered"]
    assert u["boot"] == BOOT and u["since"] == [BOOT, 4] and u["container"] == BOX
    assert u["time"] == "t4" and "crossed out of a container" in u["why"]


def test_after_a_crossing_an_explicit_who_stops_filtering_unless_the_hook_keeps_it(ran):
    calls, _ = ran
    events = monitorlog.attribute(_crossed_boot(), "fyex")
    first = mh.crossings(events)
    c = mh.Hook(name="c", command=("/bin/c",), who=("container",))
    mh.deliver(c, mh.HookState(), events, now=1, first_crossed=first)
    assert [e["seq"] for e in calls[0]["body"]["events"]] == [3, 4, 5, 6, 7]
    kept = mh.Hook(name="k", command=("/bin/k",), who=("container",), keep_who_after_crossing=True)
    mh.deliver(kept, mh.HookState(), events, now=1, first_crossed=first)
    body = calls[1]["body"]
    assert [e["seq"] for e in body["events"]] == [3, 4, 7]  # the crossing itself still reaches it
    assert body["unfiltered"] == [] and body["passed"] == {"vm": 2, "podman": 2}


def test_a_crossing_only_unfilters_its_own_boot(ran):
    calls, _ = ran
    events = monitorlog.attribute(
        [*_crossed_boot(), snap(0, boot=BOOT2), ev(1, inode=1, boot=BOOT2), snap(2, boot=BOOT2)],
        "fyex",
    )
    mh.deliver(mh.Hook(name="all", command=("/bin/a",)), mh.HookState(), events, now=1,
               first_crossed=mh.crossings(events))  # fmt: skip
    body = calls[0]["body"]
    assert [(e["boot"], e["seq"]) for e in body["events"]][-1] == (BOOT, 7)
    assert [u["boot"] for u in body["unfiltered"]] == [BOOT]


def test_a_crossing_long_before_the_hooks_read_still_unfilters_the_boot(ran, project, monkeypatch):
    # the crossing's record is far behind the hook's window, its segment even gone: the catalogue
    # holds the boot's first crossing, so every hook agrees whatever it read
    calls, _ = ran
    _hook(project)
    monkeypatch.setattr(monitorstore, "SEGMENT_BYTES", 10)
    monitorlog._append([snap(0), crossing(1), snap(2)])
    mh.round_once(now=5)  # the hook's cursor is past the crossing: it never reads it again
    monitorlog._append([ev(3)])
    with monitorstore.Catalogue.open() as cat:
        cat.drop([monitorstore.segments()[0].id])
    monitorlog._append([ev(4, inode=1), snap(5)])
    mh.round_once(now=10)
    assert _delivered(calls) == [1, 3, 4]
    assert calls[-1]["body"]["unfiltered"][0]["since"] == [BOOT, 1]


def test_a_candidate_the_catalogue_could_not_judge_counts_as_a_crossing(ran, project, monkeypatch):
    # a busy catalogue left it unjudged though settled: unfiltered rather than missed
    calls, _ = ran
    _hook(project)
    monkeypatch.setattr(monitorstore.Catalogue, "judge", lambda self: 1 / 0)
    monitorlog._append([snap(0), ev(1), snap(2)])
    mh.round_once(now=10)
    monitorlog._append([crossing(3), ev(4, inode=1), snap(5)])
    mh.round_once(now=20)
    assert _delivered(calls) == [1, 3, 4]
    assert calls[-1]["body"]["unfiltered"][0]["since"] == [BOOT, 3]


def test_keep_who_after_crossing_is_parsed_rendered_and_listed(project):
    hooks, errors = mh.parse(
        '[[hook]]\nname = "k"\ncommand = ["/bin/k"]\nwho = ["container"]\n'
        "keep_who_after_crossing = true\n"
        '[[hook]]\nname = "bad"\ncommand = ["/bin/b"]\nkeep_who_after_crossing = "yes"\n'
    )
    assert [h.name for h in hooks] == ["k"] and hooks[0].keep_who_after_crossing
    assert "keep_who_after_crossing" in errors[0]
    assert mh.parse(mh.render(hooks[0])) == (hooks, [])
    assert "keep_who_after_crossing" not in mh.render(mh.Hook(name="d", command=("/bin/d",)))
    mh.add(hooks[0])
    assert "keep_who_after_crossing" in "\n".join(mh.listing(now=1.0))
