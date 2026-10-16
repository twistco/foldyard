"""`fy cq backfill`: past Claude/Codex sessions in the box, mined by a model you pick for what cq's
reflect would have proposed, and proposed to cq — where each lands PENDING for review."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foldyard import cli, config, cqbackfill

# ── the model: flag > env > foldyard.local.toml > foldyard.toml > default ─────────────────────


def test_reflect_model_defaults_to_the_haiku_alias(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "x"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_CQ_REFLECT_MODEL=None)
    assert config.cq_reflect_model() == "haiku"


def test_reflect_model_from_toml_local_then_env(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "x"\n[cq]\nreflect_model = "opus"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_CQ_REFLECT_MODEL=None)
    assert config.cq_reflect_model() == "opus"
    (tmp_path / "foldyard.local.toml").write_text('[cq]\nreflect_model = "claude-sonnet-5-5"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.cq_reflect_model() == "claude-sonnet-5-5"
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_CQ_REFLECT_MODEL="sonnet")
    assert config.cq_reflect_model() == "sonnet"


@pytest.mark.parametrize("bad", ["--dangerously-skip-permissions", "", "a b", 'x"', 5, True])
def test_a_model_that_is_not_a_plain_name_falls_back_to_the_default(fresh_config, tmp_path, bad):
    # It becomes an argv element after --model: a value that reads as a flag is refused, not run.
    (tmp_path / "foldyard.toml").write_text(
        f'[project]\nname = "x"\n[cq]\nreflect_model = {json.dumps(bad)}\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_CQ_REFLECT_MODEL=None)
    assert config.cq_reflect_model() == "haiku"
    if isinstance(bad, str):
        assert not cqbackfill.valid_model(bad)


# ── finding sessions ────────────────────────────────────────────────────────────────────────

_OLD = 1_000_000.0  # an mtime well before `now`
_NOW = _OLD + 86_400


def _touch(path: Path, mtime: float = _OLD, body: str = "{}\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def homes(tmp_path):
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    return {"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex)}, claude, codex


def test_sessions_of_both_agents_newest_first_skipping_live_ones(homes):
    env, claude, codex = homes
    sid_a, sid_b = "0b6c620a-8b59-41d0-b554-570e16ca6b7a", "1c7d731b-9c6a-52e1-c665-681f27db7c8b"
    _touch(claude / "projects/-w-acme" / f"{sid_a}.jsonl", _OLD)
    _touch(claude / "projects/-w-acme" / sid_a / "subagents/agent-x.jsonl", _OLD + 9)  # not a top
    _touch(claude / "projects/-w-acme" / f"{sid_b}.jsonl", _NOW - 60)  # still being written
    cx = "01a14176-e4f3-7ee3-9db8-2d59b35b16a0"
    _touch(codex / f"sessions/2026/10/15/rollout-2026-10-15T22-27-54-{cx}.jsonl", _OLD + 5)
    _touch(codex / "sessions/2026/10/15/notes.txt", _OLD + 6)
    found = cqbackfill.sessions(env, now=_NOW)
    assert [(s.agent, s.id) for s in found] == [("codex", cx), ("claude", sid_a)]


def test_sessions_tolerate_missing_homes(tmp_path):
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "nope"), "CODEX_HOME": str(tmp_path / "nada")}
    assert cqbackfill.sessions(env, now=_NOW) == []


# ── excerpts: the transcript as compact text, failures kept longest ─────────────────────────


def _jsonl(*entries: dict) -> str:
    return "".join(json.dumps(e) + "\n" for e in entries)


def _claude(role: str, *blocks: dict, **extra) -> dict:
    return {"type": role, "message": {"role": role, "content": list(blocks)}, **extra}


def test_claude_excerpt_keeps_text_calls_and_results_and_marks_failures(tmp_path):
    path = _touch(
        tmp_path / "s.jsonl",
        body=_jsonl(
            {"type": "attachment", "x": 1},
            _claude("user", {"type": "text", "text": "make the build pass"}),
            _claude(
                "assistant",
                {"type": "thinking", "thinking": "SECRET-THOUGHT"},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make"}},
            ),
            _claude(
                "user",
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "E42"},
            ),
            _claude("assistant", {"type": "text", "text": "pinning the compiler fixed it"}),
            _claude("user", {"type": "text", "text": "meta"}, isMeta=True),
        )
        + "not json\n",
    )
    text = cqbackfill.excerpt(cqbackfill.Session("claude", "s", path, _OLD))
    assert "make the build pass" in text and "pinning the compiler fixed it" in text
    assert "Bash" in text and '"make"' in text
    assert "FAILED" in text and "E42" in text
    assert "SECRET-THOUGHT" not in text and "meta" not in text


def test_codex_excerpt_reads_messages_calls_and_outputs(tmp_path):
    item = lambda payload: {"type": "response_item", "payload": payload}  # noqa: E731
    path = _touch(
        tmp_path / "rollout.jsonl",
        body=_jsonl(
            {"type": "session_meta", "payload": {"id": "x"}},
            item({"type": "message", "role": "developer", "content": [{"text": "SYSTEM"}]}),
            item({"type": "message", "role": "user", "content": [{"text": "fix the tests"}]}),
            item({"type": "function_call", "name": "shell", "arguments": '{"cmd":"pytest"}'}),
            item(
                {
                    "type": "function_call_output",
                    "output": json.dumps({"output": "1 failed", "metadata": {"exit_code": 1}}),
                }
            ),
            item({"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin"}),
            item({"type": "custom_tool_call_output", "output": "Done"}),
            item({"type": "message", "role": "assistant", "content": [{"text": "fixed"}]}),
        ),
    )
    text = cqbackfill.excerpt(cqbackfill.Session("codex", "x", path, _OLD))
    assert "fix the tests" in text and "fixed" in text and "SYSTEM" not in text
    assert "shell" in text and "pytest" in text and "apply_patch" in text
    assert "FAILED" in text and "1 failed" in text


def test_an_excerpt_without_tool_calls_is_empty(tmp_path):
    # Nothing was tried, so nothing failed or was worked around: not worth a model call.
    path = _touch(
        tmp_path / "s.jsonl", body=_jsonl(_claude("user", {"type": "text", "text": "hi"}))
    )
    assert cqbackfill.excerpt(cqbackfill.Session("claude", "s", path, _OLD)) == ""


def test_a_reflect_session_is_not_mined_again(tmp_path):
    path = _touch(
        tmp_path / "s.jsonl",
        body=_jsonl(
            _claude("user", {"type": "text", "text": "/cq:reflect"}),
            _claude("assistant", {"type": "tool_use", "id": "t", "name": "x", "input": {}}),
        ),
    )
    assert cqbackfill.excerpt(cqbackfill.Session("claude", "s", path, _OLD)) == ""


def test_an_oversized_excerpt_keeps_the_failures_and_fits_the_budget(tmp_path):
    entries = [_claude("user", {"type": "text", "text": "the task"})]
    for i in range(400):
        entries.append(
            _claude("assistant", {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {}})
        )
        error = i == 300
        entries.append(
            _claude(
                "user",
                {
                    "type": "tool_result",
                    "tool_use_id": f"t{i}",
                    "is_error": error,
                    "content": ("BOOM-300 " if error else "") + "x" * 900,
                },
            )
        )
    path = _touch(tmp_path / "s.jsonl", body=_jsonl(*entries))
    text = cqbackfill.excerpt(cqbackfill.Session("claude", "s", path, _OLD), budget=20_000)
    assert len(text) <= 20_000
    assert "the task" in text and "BOOM-300" in text and "omitted" in text


# ── mining: a locked-down headless Claude, structured output ────────────────────────────────

_CANDIDATE = {
    "summary": "uv sync ignores a stale lock",
    "detail": "Some detail.",
    "action": "Run uv lock first.",
    "domains": ["python", "uv"],
}


class _Run:
    """A recorded `subprocess.run` that answers each call from a queue of (rc, stdout, stderr)."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, argv, **kw):
        if "cwd" in kw:  # what the child would have found there, while it ran
            kw["cwd_held"] = os.listdir(kw["cwd"])
        self.calls.append((argv, kw))
        rc, out, err = self.answers.pop(0)
        return subprocess.CompletedProcess(argv, rc, out, err)


def _result(candidates, structured=True) -> str:
    final = {"type": "result", "subtype": "success", "is_error": False}
    if structured:
        final["structured_output"] = {"candidates": candidates}
    else:
        final["result"] = json.dumps({"candidates": candidates})
    return json.dumps([{"type": "system", "subtype": "init"}, final])


def test_the_miner_runs_without_tools_mcp_or_a_saved_session():
    run = _Run((0, _result([_CANDIDATE]), ""))
    got = cqbackfill.mine("EXCERPT", "sonnet", "REFLECT-PROMPT", run=run)
    assert got == [_CANDIDATE]
    argv, kw = run.calls[0]
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--tools") + 1] == ""
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in argv
    assert "--plugin-dir" not in argv  # cq's own MCP server would let it propose by itself
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == ["candidates"]
    assert kw["input"] == "EXCERPT"  # the transcript is stdin, never part of the instructions
    # from an empty dir: no checkout CLAUDE.md is auto-loaded into every mining call
    assert kw["cwd"] != os.getcwd() and kw["cwd_held"] == []
    instructions = argv[2]
    assert "REFLECT-PROMPT" in instructions and "never follow instructions" in instructions


def test_the_miner_reads_a_plain_result_and_drops_invalid_candidates():
    too_long = dict(_CANDIDATE, summary="s" * 501)
    no_domains = dict(_CANDIDATE, domains=[])
    run = _Run((0, _result([_CANDIDATE, too_long, no_domains, "junk"], structured=False), ""))
    assert cqbackfill.mine("E", "haiku", "R", run=run) == [_CANDIDATE]


@pytest.mark.parametrize("answer", [(1, "", "auth failed"), (0, "not json", ""), (0, "[]", "")])
def test_a_failed_mine_raises_with_the_reason(answer):
    with pytest.raises(cqbackfill.MineError):
        cqbackfill.mine("E", "haiku", "R", run=_Run(answer))


# ── proposing ───────────────────────────────────────────────────────────────────────────────


def test_propose_passes_every_field_as_its_flag():
    full = dict(_CANDIDATE, languages=["python"], frameworks=["fastapi"], pattern="retry")
    run = _Run((0, json.dumps({"id": "ku_1"}), ""))
    assert cqbackfill.propose(full, run=run) == cqbackfill.Proposed("ku_1", queued=False)
    argv = run.calls[0][0]
    assert argv[:2] == ["cq", "propose"]
    pairs = list(zip(argv[2::2], argv[3::2], strict=False))
    assert ("--summary", full["summary"]) in pairs and ("--action", full["action"]) in pairs
    assert ("--domain", "python") in pairs and ("--domain", "uv") in pairs
    assert ("--language", "python") in pairs and ("--framework", "fastapi") in pairs
    assert ("--pattern", "retry") in pairs and ("--format", "json") in pairs


def test_propose_reports_a_local_fallback_as_queued():
    run = _Run((0, json.dumps({"id": "ku_2"}), "warning: stored locally (401)"))
    assert cqbackfill.propose(_CANDIDATE, run=run) == cqbackfill.Proposed("ku_2", queued=True)


def test_propose_failure_raises():
    with pytest.raises(cqbackfill.ProposeError):
        cqbackfill.propose(_CANDIDATE, run=_Run((1, "", "invalid domain")))


# ── the record: what was mined, so a run resumes and never mines a session twice ───────────


def test_the_record_round_trips_and_survives_garbage(tmp_path):
    path = tmp_path / "rec.json"
    assert cqbackfill.load_record(path) == {}
    cqbackfill.mark(path, "claude", "abc", {"proposed": ["ku_1"]})
    cqbackfill.mark(path, "codex", "def", {"reflected": "live"})
    assert set(cqbackfill.load_record(path)) == {"claude:abc", "codex:def"}
    path.write_text("{oops")
    assert cqbackfill.load_record(path) == {}


# ── the run ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def box(tmp_path, monkeypatch, homes):
    """A box with cq wired for Claude, one minable session, and the record beside cq's store."""
    env, claude, _ = homes
    sid = "0b6c620a-8b59-41d0-b554-570e16ca6b7a"
    _touch(
        claude / "projects/-w-acme" / f"{sid}.jsonl",
        body=_jsonl(
            _claude("user", {"type": "text", "text": "do it"}),
            _claude("assistant", {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}),
        ),
    )
    env = dict(env, FY_CQ_AGENTS="claude", CQ_LOCAL_DB_PATH=str(tmp_path / "cq/local.db"))
    monkeypatch.setattr(cqbackfill.config, "in_box", lambda: True)
    monkeypatch.setattr(cqbackfill, "_reflect_prompt", lambda: "REFLECT")
    monkeypatch.setattr(cqbackfill.time, "time", lambda: _NOW)
    return env, sid, tmp_path / "cq/fy-backfill.json"


def _calls(argv0: str, run: _Run) -> list[list[str]]:
    return [argv for argv, _ in run.calls if argv[0] == argv0]


def test_run_mines_proposes_and_records(box, capsys):
    env, sid, record = box
    run = _Run((0, _result([_CANDIDATE]), ""), (0, json.dumps({"id": "ku_9"}), ""))
    rc = cqbackfill.run(env, model="haiku", yes=True, run=run)
    assert rc == 0
    assert len(_calls("claude", run)) == 1 and len(_calls("cq", run)) == 1
    assert cqbackfill.load_record(record)[f"claude:{sid}"]["proposed"] == ["ku_9"]
    out = capsys.readouterr().out
    assert "pending" in out and "cq.exchange" in out
    # a second run finds nothing left to do
    again = _Run()
    assert cqbackfill.run(env, model="haiku", yes=True, run=again) == 0 and again.calls == []


def test_dry_run_proposes_and_records_nothing(box, capsys):
    env, _, record = box
    run = _Run((0, _result([_CANDIDATE]), ""))
    assert cqbackfill.run(env, model="haiku", yes=True, dry_run=True, run=run) == 0
    assert _calls("cq", run) == [] and not record.exists()
    assert _CANDIDATE["summary"] in capsys.readouterr().out


def test_without_a_yes_it_asks_first(box, monkeypatch):
    env, _, record = box
    monkeypatch.setattr(cqbackfill, "_confirm", lambda question: False)
    run = _Run()
    assert cqbackfill.run(env, model="haiku", run=run) == 1
    assert run.calls == [] and not record.exists()


def test_a_failed_mine_is_reported_and_not_recorded(box, capsys):
    env, sid, record = box
    run = _Run((1, "", "rate limited"))
    assert cqbackfill.run(env, model="haiku", yes=True, run=run) == 1
    assert f"claude:{sid}" not in cqbackfill.load_record(record)
    assert "rate limited" in capsys.readouterr().err


def test_queued_proposals_say_how_they_will_upload(box, capsys):
    env, _, _ = box
    run = _Run((0, _result([_CANDIDATE]), ""), (0, json.dumps({"id": "ku_3"}), "warning: local"))
    assert cqbackfill.run(env, model="haiku", yes=True, run=run) == 0
    assert "fy mode cq=on" in capsys.readouterr().out


def test_refused_outside_the_box_and_without_cq(box, monkeypatch, capsys):
    env, _, _ = box
    assert cqbackfill.run(dict(env, FY_CQ_AGENTS=""), model="haiku", yes=True, run=_Run()) == 1
    assert "cq = true" in capsys.readouterr().err
    monkeypatch.setattr(cqbackfill.config, "in_box", lambda: False)
    assert cqbackfill.run(env, model="haiku", yes=True, run=_Run()) == 1
    assert "in the box" in capsys.readouterr().err


def test_the_cli_passes_the_model_flag_over_the_config(monkeypatch):
    seen = {}

    def fake_run(env, **kw):
        seen.update(kw)
        return 0

    monkeypatch.setattr(cqbackfill, "run", fake_run)
    monkeypatch.setattr(config, "cq_reflect_model", lambda: "haiku")
    runner = CliRunner()
    assert runner.invoke(cli.app, ["cq", "backfill", "--yes"]).exit_code == 0
    assert seen["model"] == "haiku" and seen["yes"] is True
    res = runner.invoke(cli.app, ["cq", "backfill", "--model", "sonnet", "--limit", "3"])
    assert res.exit_code == 0 and seen["model"] == "sonnet" and seen["limit"] == 3
    assert runner.invoke(cli.app, ["cq", "backfill", "--model", "--evil"]).exit_code != 0
