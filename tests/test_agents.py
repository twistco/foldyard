"""agents.py — `fy agent`: the tmux argv it emits (golden), what it reads back from a box (always
box-written: sanitised, validated), the host fan-out across projects, and the teardown warnings.
No tmux and no engine run: subprocess.run is stubbed in every test that would reach one."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foldyard import agents, box, cli, config, configpin

SID = "3f2c9a1e-0b4d-4c3e-9f1a-2b3c4d5e6f70"
OTHER = "3f2c9a1e-ffff-4c3e-9f1a-2b3c4d5e6f70"


class _Proc:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _pane(
    name="claude-3f2c9a1e", sid=SID, agent="claude", dead="0", status="", attached="0", ended=""
):
    fields = [name, sid, agent, "1761900000", "1761900600", attached, dead, status, ended]
    return agents._SEP.join(fields)


# ── the tmux argv ────────────────────────────────────────────────────────────────────────────────


def test_start_argv_sets_posture_before_the_session_and_tags_it():
    argv = agents.start_argv("s1", SID, "claude", "/repo", ["bash", "-lc", "exec fy claude"])
    assert argv[:5] == ["tmux", "-L", "fy", "-f", "/dev/null"]
    commands = " ".join(argv[5:]).split(" ; ")
    assert commands[0] == "start-server"
    assert "set-option -g allow-passthrough off" in commands
    assert "set-option -s set-clipboard external" in commands
    new = next(i for i, c in enumerate(commands) if c.startswith("new-session"))
    # Every option lands before the session exists, so the agent never runs without them.
    assert all(c.startswith("set-option -") for c in commands[1:new])
    assert argv[argv.index("new-session") :][:6] == ["new-session", "-d", "-s", "s1", "-c", "/repo"]
    assert commands[new + 1 :] == [
        f"set-option -t =s1: @fy_session {SID}",
        "set-option -t =s1: @fy_agent claude",
    ]
    # The pane's command is argv (tmux runs it, no shell): the script stays ONE argument.
    assert "exec fy claude" in argv


def test_a_tmux_argument_ending_in_a_semicolon_is_refused():
    # tmux would split its argv there and run the rest as another command.
    with pytest.raises(ValueError):
        agents.start_argv("s1", SID, "claude", "/repo;", ["true"])


def test_attach_and_stop_target_the_exact_session_name():
    assert agents.attach_argv("s1") == ["tmux", "-L", "fy", "attach-session", "-t", "=s1"]
    assert agents.stop_argv("s1") == ["tmux", "-L", "fy", "kill-session", "-t", "=s1"]
    assert agents.list_argv()[:5] == ["tmux", "-L", "fy", "list-panes", "-a"]


def test_pane_command_is_a_login_shell_execing_fy():
    argv = agents.pane_command("claude", ["--session-id", SID, "--model", "a b"])
    assert argv[:2] == ["bash", "-lc"]
    assert argv[2].rstrip().endswith(f"exec fy claude --session-id {SID} --model 'a b'")
    assert "fy_term_known" in argv[2]  # the TERM fallback `fy box shell` runs


@pytest.mark.parametrize(
    ("agent", "args", "passed_id", "same_id"),
    [
        ("claude", [], True, False),
        ("claude", ["--model", "opus"], True, False),
        ("claude", ["--resume", SID.upper()], False, True),
        ("claude", [f"--resume={SID}"], False, True),
        ("claude", ["--session-id", SID], False, True),
        ("claude", ["--continue"], False, False),
        ("claude", ["--resume"], False, False),
        ("claude", ["--resume", SID, "--fork-session"], False, False),
        ("codex", [], False, False),
    ],
)
def test_session_args(agent, args, passed_id, same_id):
    sid, out = agents.session_args(agent, args)
    assert agents._UUID.fullmatch(sid)
    if passed_id:
        assert out == ["--session-id", sid, *args]
    else:
        assert out == args
    assert (sid == SID) is same_id


@pytest.mark.parametrize(
    ("raw", "want"),
    [("tmux 3.5a\n", (3, 5)), ("tmux 3.3", (3, 3)), ("tmux 2.9a", (2, 9)), ("tmux next-3.6", None)],
)
def test_tmux_version(raw, want):
    assert agents.tmux_version(raw) == want


# ── reading a box's tmux (box-written) ────────────────────────────────────────────────────────────


def test_parse_panes_reads_state_and_validates_what_the_box_wrote():
    out = "\n".join(
        [
            _pane(),
            _pane(name="dead", sid="not-a-uuid", agent="sh", dead="1", status="2"),
            _pane(name="two", attached="2"),
            "short\tline",
            _pane(name=f"forged{agents._SEP}{SID}"),  # the separator in a name: too many fields
            _pane(name="\x1b]52;c;cGF3bmVk\x07evil\x1b[2J"),
        ]
    )
    got = {s.name: s for s in agents.parse_panes(out)}
    assert got["claude-3f2c9a1e"].state == "running" and got["claude-3f2c9a1e"].id == SID
    assert got["claude-3f2c9a1e"].created == "2025-10-31T08:40:00+00:00"
    assert got["dead"].id == "" and got["dead"].agent == "" and got["dead"].state == "exited (2)"
    assert got["two"].state == "attached ×2"
    # Escape sequences can't reach your terminal through a session name.
    evil = next(n for n in got if "evil" in n)
    assert "\x1b" not in evil and "\x07" not in evil
    assert len(got) == 4  # the forged line skipped, not shifted into another session's fields


def test_the_format_is_printable():
    # tmux writes a control character in `-F` output as `_`: a tab separator came back as one
    # field per line, and `ls` listed nothing (seen live, tmux 3.5a).
    assert all(c.isprintable() for c in agents._FORMAT)


def test_sessions_in_a_box_without_tmux_or_server_is_empty(monkeypatch):
    b = agents.Box("p-devbox", "", "podman", {})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(1, "", "no server running"))
    assert agents.sessions_in(b) == []

    def missing(*a, **k):
        raise FileNotFoundError("podman")

    monkeypatch.setattr(subprocess, "run", missing)
    assert agents.sessions_in(b) == []


def test_host_box_argv_wraps_in_exec(monkeypatch):
    monkeypatch.setattr(box, "_terminal_env", lambda: ["-e", "COLORTERM=truecolor"])
    b = agents.Box("p-devbox", "", "podman", {})
    assert b.argv(["tmux", "-V"]) == ["podman", "exec", "p-devbox", "tmux", "-V"]
    tty = b.argv(agents.attach_argv("s1"), tty=True)
    assert tty[:6] == ["podman", "exec", "-it", "-e", "COLORTERM=truecolor", "p-devbox"]
    assert tty[6:8] == ["bash", "-lc"]
    assert tty[8].rstrip().endswith("exec tmux -L fy attach-session -t =s1")
    # In the box itself there's nothing to wrap.
    assert agents.Box("p-devbox", "").argv(["tmux", "-V"]) == ["tmux", "-V"]


# ── the box-side verbs ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def in_box(monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setattr(config, "project", lambda: "acme")
    monkeypatch.setattr(config, "project_prefix", lambda: "acme")
    monkeypatch.setenv("WORKTREE", "feat")
    calls: list[list[str]] = []
    replies: dict[str, _Proc] = {}

    def run(cmd, **kw):
        calls.append(cmd)
        for key, proc in replies.items():
            if key in cmd:
                return proc
        return _Proc(0, "")

    monkeypatch.setattr(subprocess, "run", run)
    return calls, replies


def test_ls_in_a_box_lists_its_own_sessions(in_box, capsys):
    calls, replies = in_box
    replies["list-panes"] = _Proc(0, _pane() + "\n")
    assert agents.ls(here=False, as_json=True) == 0
    doc = json.loads(capsys.readouterr().out)
    assert calls == [agents.list_argv()]  # straight to tmux, no engine
    assert doc["project"] == "acme"
    assert doc["sessions"][0]["box"] == "acme-feat-devbox"
    assert doc["sessions"][0]["worktree"] == "feat"


def test_start_in_a_box(in_box, monkeypatch, capsys, tmp_path):
    calls, replies = in_box
    monkeypatch.chdir(tmp_path)
    replies["-V"] = _Proc(0, "tmux 3.5a\n")
    assert agents.start("claude", "", [], attach_after=False) == 0
    start = calls[-1]
    assert start[:5] == ["tmux", "-L", "fy", "-f", "/dev/null"]
    new = start.index("new-session")
    name, sid = start[new + 3], start[start.index("@fy_session") + 1]
    assert name == f"claude-{sid[:8]}"
    assert start[new + 4 : new + 6] == ["-c", str(tmp_path)]
    assert f"exec fy claude --session-id {sid}" in next(a for a in start if "exec fy" in a)
    assert sid in capsys.readouterr().out


def test_start_refuses_an_old_or_missing_tmux(in_box, capsys):
    calls, replies = in_box
    replies["-V"] = _Proc(0, "tmux 3.2a\n")
    assert agents.start("claude", "", [], attach_after=False) == 1
    assert "3.3 or newer" in capsys.readouterr().err
    replies["-V"] = _Proc(127, "", "not found")
    assert agents.start("claude", "", [], attach_after=False) == 1
    assert 'packages = ["tmux"]' in capsys.readouterr().err
    assert not any("new-session" in c for c in calls)


def test_start_refuses_bad_names_and_agents(in_box):
    assert agents.start("bash", "", [], attach_after=False) == 2
    assert agents.start("claude", "-x", [], attach_after=False) == 2
    assert agents.start("claude", "a;b", [], attach_after=False) == 2


def test_stop_in_a_box_by_id_prefix(in_box, capsys):
    calls, replies = in_box
    replies["list-panes"] = _Proc(0, _pane() + "\n")
    assert agents.stop(SID[:8], here=False) == 0
    assert calls[-1] == agents.stop_argv("claude-3f2c9a1e")


def test_an_ambiguous_or_short_prefix_matches_nothing(in_box, capsys):
    calls, replies = in_box
    replies["list-panes"] = _Proc(0, _pane() + "\n" + _pane(name="b", sid=OTHER) + "\n")
    assert agents.stop(SID[:8], here=False) == 1
    assert "matches 2 sessions" in capsys.readouterr().err
    assert agents.stop(SID[:4], here=False) == 1  # too short to be a handle
    assert not any("kill-session" in c for c in calls)


# ── the host fan-out ──────────────────────────────────────────────────────────────────────────────


def _listing(project: str, checkout: str, *sessions: dict) -> str:
    rows = [
        {
            "name": "claude-3f2c9a1e",
            "id": SID,
            "agent": "claude",
            "created": "",
            "activity": "",
            "attached": 0,
            "alive": True,
            "exit_status": None,
            "project": project,
            "worktree": "",
            "box": f"{project}-devbox",
            "checkout": checkout,
            **s,
        }
        for s in sessions
    ]
    return json.dumps(
        {"project": project, "checkout": checkout, "unavailable": "", "sessions": rows}
    )


@pytest.fixture
def host(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "in_project", lambda: False)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr(configpin, "adopted_checkouts", lambda: [a, b])
    monkeypatch.setenv("WORKTREE", "pinned-by-the-parent")
    monkeypatch.setenv("FOLDYARD_REPO", str(a))
    calls: list[tuple[list[str], dict]] = []
    children = {
        str(a): _Proc(0, _listing("alpha", str(a), {})),
        str(b): _Proc(1, "", "✗ something\n(VM stopped — nothing to list)\n"),
    }

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return children.get(str(kw.get("cwd")), _Proc(0, ""))

    monkeypatch.setattr(subprocess, "run", run)
    return calls, a, b


def test_ls_on_the_host_asks_every_adopted_project_in_its_own_checkout(host, capsys):
    calls, a, b = host
    assert agents.ls(here=False, as_json=False) == 0
    out = capsys.readouterr().out
    for cmd, kw in calls:
        assert cmd[1:] == ["-m", "foldyard", "agent", "ls", "--here", "--json"]
        # Neither the parent's worktree pin nor an explicit repo may follow the child elsewhere.
        assert "WORKTREE" not in kw["env"] and "FOLDYARD_REPO" not in kw["env"]
    assert {str(kw["cwd"]) for _, kw in calls} == {str(a), str(b)}
    assert "alpha" in out and SID in out
    assert "Not listed" in out and "(VM stopped — nothing to list)" in out
    assert "fy agent attach <id>" in out


def test_a_childs_listing_is_sanitised_again(host):
    _, a, _ = host
    raw = _listing("alpha", str(a), {"name": "x\x1b[2Jy", "id": "nope", "agent": "rm"})
    got = agents.from_json(raw)
    assert got is not None
    s = got.sessions[0]
    assert s.name == "x[2Jy" and s.id == "" and s.agent == ""
    assert agents.from_json("not json") is None
    assert agents.from_json(json.dumps({"sessions": [1]})) is None


def test_attach_by_id_from_outside_a_project_hands_over_to_its_project(host):
    calls, a, _ = host
    assert agents.attach(SID, here=False) == 0
    cmd, kw = calls[-1]
    assert cmd[1:] == ["-m", "foldyard", "agent", "attach", "--here", SID]
    assert str(kw["cwd"]) == str(a) and "WORKTREE" not in kw["env"]


def test_attach_to_nothing_says_so(host, capsys):
    assert agents.attach(OTHER, here=False) == 1
    assert "no agent session" in capsys.readouterr().err


def test_adopted_checkouts_reads_the_host_markers(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(state))
    main, wt, gone, bare = (tmp_path / n for n in ("main", "wt", "gone", "bare"))
    for d in (main, wt, bare):
        d.mkdir()
    (main / "foldyard.toml").write_text("")
    (wt / "foldyard.toml").write_text("")

    def pin(name: str, checkout: Path, worktree: str = "") -> None:
        d = state / "adopted" / name
        d.mkdir(parents=True)
        doc = {"checkout": str(checkout), "worktree": worktree}
        (d / configpin.ADOPTED_MARKER).write_text(json.dumps(doc))

    pin("main-1", main)
    pin("wt-2", wt, worktree="feat")  # a worktree's pin: its project lists it
    pin("gone-3", gone)
    pin("bare-4", bare)  # no foldyard.toml any more
    (state / "adopted" / "junk").mkdir()
    assert configpin.adopted_checkouts() == [main]


# ── clean: exited sessions past an age, never a running one ───────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "seconds"), [("30m", 1800), ("12h", 43200), ("7d", 604800), ("2W", 1209600)]
)
def test_parse_age(text, seconds):
    assert agents.parse_age(text) == seconds


@pytest.mark.parametrize("text", ["7", "0d", "-1d", "1y", "", "1.5h"])
def test_parse_age_wants_a_unit_and_a_positive_count(text):
    # A bare number would read as seconds, and `--older-than 7` would remove everything exited.
    with pytest.raises(ValueError):
        agents.parse_age(text)


def test_clean_in_a_box_removes_only_exited_sessions_past_the_age(in_box, capsys):
    calls, replies = in_box
    now = int(agents.datetime.now(agents.UTC).timestamp())
    replies["list-panes"] = _Proc(
        0,
        "\n".join(
            [
                _pane(name="old", dead="1", status="0", ended=str(now - 3 * 86400)),
                _pane(name="recent", dead="1", status="1", ended=str(now - 600)),
                _pane(name="running"),  # its activity is years ago: still never removed
                _pane(name="old-no-dead-time", dead="1"),  # falls back to the activity (2025)
                _pane(name="unreadable", dead="1", ended="soon"),  # → its activity
            ]
        ),
    )
    assert agents.clean("1d", here=False, dry_run=True) == 0
    assert "would remove" in capsys.readouterr().out
    assert not any("kill-session" in c for c in calls)
    assert agents.clean("1d", here=False, dry_run=False) == 0
    killed = [c[-1] for c in calls if "kill-session" in c]
    # An unreadable exit time falls back to the last activity, as a tmux without it does.
    assert killed == ["=old", "=old-no-dead-time", "=unreadable"]


def test_clean_refuses_a_bare_number(in_box, capsys):
    assert agents.clean("7", here=False, dry_run=False) == 2
    assert "isn't an age" in capsys.readouterr().err


def test_clean_on_the_host_runs_in_each_project(host):
    calls, a, b = host
    assert agents.clean("7d", here=False, dry_run=True) == 1  # b's child failed: said so
    argvs = [(cmd[1:], str(kw["cwd"])) for cmd, kw in calls]
    want = ["-m", "foldyard", "agent", "clean", "--here", "--older-than", "7d", "--dry-run"]
    assert argvs == [(want, str(a)), (want, str(b))]


# ── teardown names what it kills ──────────────────────────────────────────────────────────────────


def test_box_down_names_live_agents_and_asks_on_a_terminal(monkeypatch, capsys):
    live = [agents.Session("claude-3f2c9a1e", SID, "claude", "", "", 0, True, None)]
    monkeypatch.setattr(agents, "live_in", lambda *a: live)
    monkeypatch.setattr(box.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert box._agents_may_stop("podman", "p-devbox", {}, []) is False
    err = capsys.readouterr().err
    assert "1 agent session(s) still running in p-devbox" in err and SID in err
    assert box._agents_may_stop("podman", "p-devbox", {}, ["--yes"]) is True
    monkeypatch.setattr(box.sys.stdin, "isatty", lambda: False)
    assert box._agents_may_stop("podman", "p-devbox", {}, []) is True  # the TUI asked already
    monkeypatch.setattr(agents, "live_in", lambda *a: [])
    monkeypatch.setattr(box.sys.stdin, "isatty", lambda: True)
    assert box._agents_may_stop("podman", "p-devbox", {}, []) is True  # nothing to ask about


def test_live_in_workspace_never_raises(monkeypatch):
    def boom():
        raise SystemExit("no engine")

    monkeypatch.setattr(box, "engine_env", boom)
    assert agents.live_in_workspace("p") == []


# ── the CLI ───────────────────────────────────────────────────────────────────────────────────────


def test_agent_ls_attach_stop_run_outside_a_project_start_does_not(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FOLDYARD_REPO", raising=False)
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "in_project", lambda: False)
    seen: list[tuple] = []
    monkeypatch.setattr(agents, "ls", lambda **k: seen.append(("ls", k)) or 0)
    monkeypatch.setattr(agents, "attach", lambda t, **k: seen.append(("attach", t)) or 0)
    monkeypatch.setattr(agents, "stop", lambda t, **k: seen.append(("stop", t)) or 0)
    monkeypatch.setattr(agents, "start", lambda *a, **k: seen.append(("start",)) or 0)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["agent", "ls", "--json"]).exit_code == 0
    assert runner.invoke(cli.app, ["agent", "attach", SID]).exit_code == 0
    assert runner.invoke(cli.app, ["agent", "stop", SID]).exit_code == 0
    assert runner.invoke(cli.app, ["agent", "start"]).exit_code == 1  # needs a project
    assert seen == [("ls", {"here": False, "as_json": True}), ("attach", SID), ("stop", SID)]


def test_agent_start_passes_args_after_the_separator(monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    got: list = []
    monkeypatch.setattr(agents, "start", lambda *a, **k: got.append((a, k)) or 0)
    result = CliRunner().invoke(
        cli.app, ["agent", "start", "codex", "-n", "rev", "-a", "--", "--model", "x"]
    )
    assert result.exit_code == 0, result.output
    assert got == [(("codex", "rev", ["--model", "x"]), {"attach_after": True})]


@pytest.mark.parametrize(
    "verb",
    [
        lambda: agents.ls(here=False, as_json=False),
        lambda: agents.attach(OTHER, here=False),
        lambda: agents.stop(OTHER, here=False),
        lambda: agents.clean("1d", here=False, dry_run=True),
    ],
)
def test_outside_a_project_no_verb_imports_stack(host, monkeypatch, verb):
    # Importing stack imports devmode, which binds the machine name from the working directory:
    # from `/` (an empty name) that refused `fy agent attach` outright (seen live). Outside a
    # project nothing here may need it.
    import builtins

    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if (
            level
            and "stack" in (fromlist or ())
            and (globals or {}).get("__name__") == "foldyard.agents"
        ):
            raise AssertionError("agents imported stack outside a project")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)
    verb()


def test_box_calls_never_read_your_terminal(monkeypatch):
    # Inherited, a child took the answer typed at `fy box down`'s question (seen live).
    seen: list[dict] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append(kw) or _Proc(0, ""))
    agents.sessions_in(agents.Box("p-devbox", "", "podman", {}))
    assert seen and seen[0]["stdin"] is subprocess.DEVNULL
