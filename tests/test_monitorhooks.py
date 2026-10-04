"""monitorhooks.py — the operator's hooks over the guest monitor's events (docs/monitor-pipeline.md
§6): host-only config and its validation, settled-only delivery, the cursor, at-least-once with
backoff and a bounded give-up, and a command's environment never being the supervisor's. No
command or endpoint is really reached: ``subprocess.run`` and ``urlopen`` are stubbed."""

from __future__ import annotations

import json
import subprocess

import pytest

from foldyard import monitorhooks as mh
from foldyard import monitorlog

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


def test_round_once_reads_the_store_and_saves_each_hooks_state(ran, project, monkeypatch):
    calls, _ = ran
    (project / "monitor-hooks.toml").write_text('[[hook]]\nname = "s"\ncommand = ["/bin/s"]\n')
    monkeypatch.setattr(mh.monitorlog, "_tail", lambda n: [snap(0), ev(1), ev(2), snap(3)])
    mh.round_once(now=50)
    state = json.loads((project / "monitor-hooks-state.json").read_text())
    assert state["s"]["last"] == [BOOT, 2] and state["s"]["delivered"] == 2
    mh.round_once(now=60)
    assert len(calls) == 1  # the saved cursor holds across rounds


def test_round_once_logs_config_errors_and_carries_on(project, monkeypatch):
    (project / "monitor-hooks.toml").write_text('[[hook]]\nname = "s"\ncommand = ["s"]\n')
    logged = []
    mh.round_once(now=1, log=logged.append)
    assert logged and "absolute path" in logged[0]
