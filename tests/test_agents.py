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
    name="claude-3f2c9a1e",
    sid=SID,
    agent="claude",
    dead="0",
    status="",
    attached="0",
    ended="",
    conv="",
):
    fields = [name, sid, agent, "1761900000", "1761900600", attached, dead, status, ended, conv]
    return agents._SEP.join(fields)


# ── the tmux argv ────────────────────────────────────────────────────────────────────────────────


def test_start_argv_sets_posture_before_the_session_and_tags_it():
    argv = agents.start_argv("s1", SID, "claude", "/repo", ["bash", "-lc", "exec fy claude"])
    assert argv[:5] == ["tmux", "-L", "fy", "-f", "/dev/null"]
    commands = " ".join(argv[5:]).split(" ; ")
    assert commands[0] == "start-server"
    assert "set-option -g allow-passthrough off" in commands
    assert "set-option -s set-clipboard external" in commands
    assert "set-option -s focus-events on" in commands
    new = next(i for i, c in enumerate(commands) if c.startswith("new-session"))
    # Every option lands before the session exists, so the agent never runs without them.
    assert all(c.startswith("set-option -") for c in commands[1:new])
    assert argv[argv.index("new-session") :][:8] == [
        "new-session", "-d", "-s", "s1", "-c", "/repo", "-e", "COLORTERM=truecolor",
    ]  # fmt: skip
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
    assert agents.attach_argv("s1") == ["tmux", "-L", "fy", "-u", "attach-session", "-t", "=s1"]
    assert agents.attach_argv("s1", rgb=True)[3:6] == ["-u", "-T", "RGB"]
    assert agents.switch_argv("s1") == ["tmux", "-L", "fy", "switch-client", "-t", "=s1"]
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
    sid, out, claudes = agents.session_args(agent, args)
    assert agents._UUID.fullmatch(sid)
    if passed_id:
        assert out == ["--session-id", sid, *args]
    else:
        assert out == args
    assert (sid == SID) is same_id
    assert claudes is (passed_id or same_id)  # Claude resumes the session by this id


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
    assert tty[8].rstrip().endswith("exec tmux -L fy -u attach-session -t =s1")
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
    assert start[-5:] == ["set-option", "-t", f"={name}:", "@fy_conversation", sid]


def test_start_tags_a_session_claude_picks_as_having_no_conversation(in_box, monkeypatch, tmp_path):
    calls, replies = in_box
    monkeypatch.chdir(tmp_path)
    replies["-V"] = _Proc(0, "tmux 3.5a\n")
    assert agents.start("claude", "", ["--continue"], attach_after=False) == 0
    assert calls[-1][-2:] == ["@fy_conversation", agents.NO_CONVERSATION]
    assert agents.start("codex", "", [], attach_after=False) == 0
    assert "@fy_conversation" not in calls[-1]  # Codex's comes from the pane's watcher


def test_resume_refuses_a_session_claude_picked_and_removes_nothing(in_box, capsys):
    calls, replies = in_box
    replies["-V"] = _Proc(0, "tmux 3.5a\n")
    replies["list-panes"] = _Proc(0, _pane(dead="1", conv=agents.NO_CONVERSATION) + "\n")
    assert agents.resume(SID, "", [], attach_after=False) == 1
    assert "Claude picked it itself" in capsys.readouterr().err
    assert not any("kill-session" in c or "new-session" in c for c in calls)


def test_truecolor_and_own_tmux_read_the_environment():
    assert agents.truecolor({"COLORTERM": "truecolor"}) and agents.truecolor({"COLORTERM": "24bit"})
    assert not agents.truecolor({}) and not agents.truecolor({"COLORTERM": "yes"})
    assert agents.in_own_tmux({"TMUX": "/tmp/tmux-0/fy,1678,0"})
    assert not agents.in_own_tmux({"TMUX": "/tmp/tmux-0/default,1678,0"})
    assert not agents.in_own_tmux({})


def test_attach_in_a_box_execs_tmux_with_utf8_and_the_terminals_colour(in_box, monkeypatch):
    _, replies = in_box
    replies["list-panes"] = _Proc(0, _pane() + "\n")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    execs: list[list[str]] = []
    monkeypatch.setattr(agents.os, "execvp", lambda f, argv: execs.append(argv))
    agents.attach(SID, here=False)
    assert execs == [agents.attach_argv("claude-3f2c9a1e", rgb=True)]


def test_attach_from_inside_an_agent_pane_switches_instead_of_nesting(in_box, monkeypatch):
    calls, replies = in_box
    replies["list-panes"] = _Proc(0, _pane() + "\n")
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/fy,1678,0")
    monkeypatch.setattr(agents.os, "execvp", lambda *a: pytest.fail("attached inside tmux"))
    assert agents.attach(SID, here=False) == 0
    assert calls[-1] == agents.switch_argv("claude-3f2c9a1e")


def _resumed(start: list[str]) -> tuple[str, str, str]:
    """(session name, @fy_session, the pane's script) of a start argv."""
    new = start.index("new-session")
    script = next(a for a in start if "exec fy" in a)
    return start[new + 3], start[start.index("@fy_session") + 1], script


def test_resume_with_no_session_left_takes_the_full_id(in_box, monkeypatch, tmp_path):
    calls, replies = in_box
    monkeypatch.chdir(tmp_path)
    replies["-V"] = _Proc(0, "tmux 3.5a\n")
    assert agents.resume(SID, "", ["--model", "opus"], attach_after=False) == 0
    name, sid, script = _resumed(calls[-1])
    assert (name, sid) == (f"claude-{SID[:8]}", SID)
    assert script.endswith(f"exec fy claude --resume {SID} --model opus")
    assert not any("kill-session" in c for c in calls)


def test_resume_replaces_the_exited_session_that_held_the_id(in_box, tmp_path, monkeypatch):
    calls, replies = in_box
    monkeypatch.chdir(tmp_path)
    replies["-V"] = _Proc(0, "tmux 3.5a\n")
    replies["list-panes"] = _Proc(0, _pane(name="mine", dead="1") + "\n")
    assert agents.resume(SID[:8], "", [], attach_after=False) == 0  # a prefix finds it
    kill = calls.index(agents.stop_argv("mine"))
    assert kill < len(calls) - 1  # removed BEFORE the new one, which takes its name
    assert _resumed(calls[-1])[:2] == ("mine", SID)


def test_resume_leaves_a_running_session_alone(in_box, capsys):
    calls, replies = in_box
    replies["-V"] = _Proc(0, "tmux 3.5a\n")
    replies["list-panes"] = _Proc(0, _pane() + "\n")
    assert agents.resume(SID, "", [], attach_after=False) == 1
    assert f"fy agent attach {SID}" in capsys.readouterr().err
    assert not any("kill-session" in c or "new-session" in c for c in calls)


def test_resume_refuses_a_short_id_with_nothing_to_match(in_box, capsys):
    calls, replies = in_box
    replies["-V"] = _Proc(0, "tmux 3.5a\n")
    assert agents.resume(SID[:8], "", [], attach_after=False) == 1
    assert "full id" in capsys.readouterr().err
    assert agents.resume("a-name", "", [], attach_after=False) == 1  # Claude has no names
    assert not any("kill-session" in c or "new-session" in c for c in calls)


THREAD = "01a1a283-e253-7d51-b1d9-68916285c824"  # Codex's (a UUIDv7, as 0.166 makes them)


def test_resume_a_codex_session_by_its_thread_keeping_its_name_and_id(
    in_box, monkeypatch, tmp_path
):
    calls, replies = in_box
    monkeypatch.chdir(tmp_path)
    replies["-V"] = _Proc(0, "tmux 3.5a\n")
    replies["list-panes"] = _Proc(0, _pane(name="rev", agent="codex", dead="1", conv=THREAD) + "\n")
    assert agents.resume(SID[:8], "", ["--model", "o9"], attach_after=False) == 0
    assert agents.stop_argv("rev") in calls
    name, sid, script = _resumed(calls[-1])
    assert (name, sid) == ("rev", SID)  # `fy agent attach <the same id>` still finds it
    assert script.endswith(f"exec fy codex resume {THREAD} --model o9")
    assert "fy_codex_thread $$" in script  # the new session records its thread too
    # Codex's own id finds the session as well — in full only (its ids are time-ordered).
    assert agents.resume(THREAD, "", [], attach_after=False) == 0
    assert agents.resume(THREAD[:8], "", [], attach_after=False) == 1


def test_resume_a_codex_session_with_no_thread_yet_says_why(in_box, capsys):
    calls, replies = in_box
    replies["-V"] = _Proc(0, "tmux 3.5a\n")
    replies["list-panes"] = _Proc(0, _pane(agent="codex", dead="1") + "\n")
    assert agents.resume(SID, "", [], attach_after=False) == 1
    assert "first message" in capsys.readouterr().err
    assert agents.resume(SID, "", [], agent="claude", attach_after=False) == 1  # not a Claude one
    assert "not claude" in capsys.readouterr().err
    assert not any("kill-session" in c or "new-session" in c for c in calls)


def test_resume_codex_with_no_session_left_takes_its_id_or_a_name(in_box, monkeypatch, tmp_path):
    calls, replies = in_box
    monkeypatch.chdir(tmp_path)
    replies["-V"] = _Proc(0, "tmux 3.5a\n")
    assert agents.resume(THREAD, "", [], agent="codex", attach_after=False) == 0
    assert _resumed(calls[-1])[2].endswith(f"exec fy codex resume {THREAD}")
    assert agents.resume("my thread", "", [], agent="codex", attach_after=False) == 0  # /rename's
    assert _resumed(calls[-1])[2].endswith("exec fy codex resume 'my thread'")
    assert agents.resume("--last", "", [], agent="codex", attach_after=False) == 1  # not a flag
    assert agents.resume(THREAD, "", [], agent="gemini", attach_after=False) == 2


def test_parse_panes_reads_the_conversation_tag():
    rows = agents.parse_panes(
        "\n".join(
            [
                _pane(name="tagged", conv=SID),
                _pane(name="picked", conv=agents.NO_CONVERSATION),  # Claude chose the session
                _pane(name="older"),  # untagged: started before the tag, its id taken as Claude's
                _pane(name="forged", conv="../../etc/passwd"),
                _pane(name="codex-new", agent="codex"),  # no thread saved yet
            ]
        )
    )
    assert [s.conversation for s in rows] == [SID, "", SID, "", ""]
    codex = agents.parse_panes(_pane(agent="codex", conv=THREAD))[0]
    assert codex.conversation == THREAD
    listing = agents.ProjectListing("acme", "/w/acme", [codex], "")
    back = agents.from_json(agents.to_json(listing))
    assert back is not None and back.sessions[0].conversation == THREAD


def test_only_a_codex_pane_starts_the_thread_watcher():
    assert "fy_codex_thread" not in agents.pane_command("claude", [])[2]
    assert "fy_codex_thread $$" in agents.pane_command("codex", [])[2]


@pytest.mark.spawns("bash")
@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="reads /proc, as in the box")
# a child: npm's codex, a Node wrapper; both: a native Codex and a tool reading another rollout
@pytest.mark.parametrize("holder", ["itself", "a child", "both"])
def test_the_thread_watcher_tags_the_session_with_the_rollout_codex_holds(tmp_path, holder):
    """The watcher, under real bash against a real /proc: this shell plays Codex, holding fd 8
    open; `readlink` reports a rollout for it (the suite's PATH has no coreutils, so `readlink`,
    `sleep` and `tmux` are functions). Tagged once with the thread id, whatever else is open, and
    never with a rollout whose name doesn't end in one. With "both", a child (one of a native
    Codex's tools) holds ANOTHER thread's rollout, and Codex's own still wins."""
    import subprocess

    out = tmp_path / "tmux"
    OTHER = "01a1a283-0000-7000-8000-000000000000"
    stubs = f"""
readlink() {{ case $1 in
  */fd/8) echo /h/.codex/sessions/2026/11/03/rollout-2026-11-03T17-45-14-{THREAD}.jsonl ;;
  */fd/9) echo /h/.codex/sessions/rollout-2026-11-03T17-45-14-not-a-thread-id-at-all-x.jsonl ;;
  */fd/6) echo /h/.codex/sessions/2026/11/02/rollout-2026-11-02T09-00-00-{OTHER}.jsonl ;;
  *) echo /dev/null ;;
esac; }}
sleep() {{ :; }}
tmux() {{ printf '%s\n' "$*" >> {out}; }}
"""

    # The holder opens fds 8 and 9, then waits (≤ 10 s, builtins only: a read on a pipe that
    # never delivers) for the watcher this shell started to tag the session.
    def hold(fds: str) -> str:
        return f"""
exec {fds} 7<> <(:)
for ((i = 0; i < 200; i++)); do [ -s {out} ] && break; read -t 0.05 -u 7 || :; done
"""

    wait = {
        "itself": hold("8</dev/null 9</dev/null"),
        "a child": f"( {hold('8</dev/null 9</dev/null')} ) & wait",
        # The child closes the 8 it inherits, so only Codex holds THREAD, the child OTHER.
        "both": f"exec 8</dev/null; ( exec 8<&-; {hold('6</dev/null 9</dev/null')} ) & wait",
    }[holder]
    proc = subprocess.run(
        ["bash", "-c", stubs + agents._CODEX_THREAD_SNIPPET + wait],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "TMUX_PANE": "%9"},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.read_text().splitlines() == [f"set-option -t %9 @fy_conversation {THREAD}"]


def test_start_refuses_an_old_or_missing_tmux(in_box, capsys):
    calls, replies = in_box
    replies["-V"] = _Proc(0, "tmux 3.2a\n")
    assert agents.start("claude", "", [], attach_after=False) == 1
    assert "3.3 or newer" in capsys.readouterr().err
    replies["-V"] = _Proc(127, "", "not found")
    assert agents.start("claude", "", [], attach_after=False) == 1
    assert "fy box down && fy box up" in capsys.readouterr().err
    assert not any("new-session" in c for c in calls)


@pytest.fixture
def no_tmux(in_box, monkeypatch, tmp_path):
    """A box with no tmux, in a checkout at ``tmp_path``; returns the questions asked."""
    _, replies = in_box
    replies["-V"] = _Proc(127, "", "not found")
    monkeypatch.setattr(config, "repo_root", lambda: tmp_path)
    asked: list[str] = []
    monkeypatch.setattr(agents, "_ask", lambda q: asked.append(q) or False)
    return asked


def _own_image(monkeypatch):
    monkeypatch.setattr(config, "box_image", lambda: {"dockerfile": "box.Dockerfile", "tag": "t"})


def test_a_stock_image_without_tmux_is_an_old_box_never_an_offer(no_tmux, capsys):
    assert agents.start("claude", "", [], attach_after=False) == 1
    err = capsys.readouterr().err
    assert "packaged box image has it" in err
    assert "on your computer, `fy box down && fy box up`" in err  # in a box: the host's verb
    assert no_tmux == []


def test_an_own_image_that_already_lists_tmux_is_an_old_box_never_an_offer(
    no_tmux, monkeypatch, capsys, tmp_path
):
    _own_image(monkeypatch)
    (tmp_path / "foldyard.toml").write_text('[box]\npackages = ["tmux"]\n')
    assert agents.start("claude", "", [], attach_after=False) == 1
    assert "[box].packages lists it" in capsys.readouterr().err
    assert no_tmux == []


def test_an_own_image_without_tmux_is_offered_and_a_no_changes_nothing(
    no_tmux, monkeypatch, capsys, tmp_path
):
    _own_image(monkeypatch)
    (tmp_path / "foldyard.toml").write_text('[box]\npackages = ["just"]\n')
    assert agents.start("claude", "", [], attach_after=False) == 1
    assert len(no_tmux) == 1 and "(y/N)" in no_tmux[0]
    assert 'packages = ["tmux"]' in capsys.readouterr().err  # the line to add by hand
    assert (tmp_path / "foldyard.toml").read_text() == '[box]\npackages = ["just"]\n'


def test_the_question_names_the_local_file_when_that_is_the_one_edited(
    no_tmux, monkeypatch, capsys, tmp_path
):
    # An array in foldyard.local.toml replaces the shared one, so that's the file a yes edits —
    # and the one the question and the hand-edit hint have to name.
    _own_image(monkeypatch)
    (tmp_path / "foldyard.toml").write_text('[box]\npackages = ["just"]\n')
    (tmp_path / "foldyard.local.toml").write_text('[box]\npackages = ["git"]\n')
    assert agents.start("claude", "", [], attach_after=False) == 1
    assert len(no_tmux) == 1 and "foldyard.local.toml?" in no_tmux[0]
    assert "under [box] in foldyard.local.toml" in capsys.readouterr().err


def test_a_yes_adds_it_and_says_what_comes_next(no_tmux, monkeypatch, capsys, tmp_path):
    _own_image(monkeypatch)
    (tmp_path / "foldyard.toml").write_text('[box]\npackages = ["just"]\n')
    monkeypatch.setattr(agents, "_ask", lambda q: True)
    assert agents.start("claude", "", [], attach_after=False) == 1  # still no tmux in THIS box
    err = capsys.readouterr().err
    assert (tmp_path / "foldyard.toml").read_text() == '[box]\npackages = ["just", "tmux"]\n'
    assert "✓ added tmux" in err and "adopt the config when asked" in err


def test_a_yes_the_edit_cant_make_falls_back_to_the_hint(no_tmux, monkeypatch, capsys, tmp_path):
    _own_image(monkeypatch)
    (tmp_path / "foldyard.toml").write_text('box.packages = ["just"]\n')
    monkeypatch.setattr(agents, "_ask", lambda q: True)
    assert agents.start("claude", "", [], attach_after=False) == 1
    err = capsys.readouterr().err
    assert "couldn't add it" in err and 'packages = ["tmux"]' in err
    assert (tmp_path / "foldyard.toml").read_text() == 'box.packages = ["just"]\n'


def test_the_question_is_never_asked_without_a_terminal(monkeypatch):
    monkeypatch.setattr(agents.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("read the terminal"))
    assert agents._ask("? ") is False


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
        assert cmd[1:] == ["-P", "-m", "foldyard", "agent", "ls", "--here", "--json"]
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
    assert cmd[1:] == ["-P", "-m", "foldyard", "agent", "attach", "--here", "--", SID]
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
    want = ["-P", "-m", "foldyard", "agent", "clean", "--here", "--older-than", "7d", "--dry-run"]
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


def test_a_package_planted_in_a_checkout_never_runs(monkeypatch):
    # The children run IN a checkout, which the box can write. Plain `python -m` would import a
    # planted `foldyard/` from the working directory before the installed one (the review
    # finding on #156); `-P` drops that entry. This runs the real child (the interpreter is
    # allowlisted), not a stub. Its directory must be outside every project (pytest's tmp_path
    # can sit inside this checkout under --basetemp), or the child finds a foldyard.toml above it
    # and never gives the refusal asserted below.
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        if any((p / "foldyard.toml").exists() for p in (tmp_path, *tmp_path.parents)):
            pytest.skip("the system temp dir is inside a foldyard project")
        _planted_never_runs(tmp_path)


def _planted_never_runs(tmp_path: Path) -> None:
    planted = tmp_path / "foldyard"
    planted.mkdir()
    marker = tmp_path / "planted-code-ran"
    (planted / "__init__.py").write_text("")
    (planted / "__main__.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    listing = agents._list_checkout(tmp_path)
    assert not marker.exists()
    # …and the child got as far as the INSTALLED foldyard: its own refusal (no project here) is
    # what came back, so the absence above isn't a child that never started.
    assert "needs a foldyard project" in listing.unavailable
    # The control: the same child WITHOUT -P does run the planted code, so the assertion above is
    # one the test can fail (the review's positive control on #156).
    argv = agents._self("--version")
    env = agents._child_env()
    env.pop("PYTHONSAFEPATH", None)  # it would leave the working directory off too
    control = [a for a in argv if a != "-P"]
    subprocess.run(control, cwd=tmp_path, env=env, capture_output=True, timeout=60)
    assert marker.exists()


def test_a_handle_starting_with_a_dash_reaches_attach_after_the_separator(monkeypatch):
    # The hand-over passes a box-written name; `--` keeps a leading `-` from parsing as an option.
    monkeypatch.setattr(config, "in_box", lambda: True)
    got: list = []
    monkeypatch.setattr(agents, "attach", lambda t, **k: got.append((t, k)) or 0)
    result = CliRunner().invoke(cli.app, ["agent", "attach", "--here", "--", "-sneaky"])
    assert result.exit_code == 0, result.output
    assert got == [("-sneaky", {"here": True})]


def test_a_childs_env_pins_relative_pythonpath_where_this_process_resolves_it(
    monkeypatch, tmp_path
):
    # The children run in ANOTHER checkout: a relative entry left as is would resolve there.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", "src")
    monkeypatch.setenv("WORKTREE", "feat")
    env = agents._child_env()
    assert env["PYTHONPATH"] == str(tmp_path / "src")
    assert "WORKTREE" not in env
