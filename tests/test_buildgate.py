"""The build gate: wall refusals during an image build are reported, offered, and retried.

A build's tool reports the URL it ASKED for; the wall refused whatever host that led to — often a
redirect target the proxy can't see inside the build's tunnel (cdn.playwright.dev → GCS, seen
live). Before this the only way to name the real host was to read egress.jsonl. The gate reads
the build-attributed refusals after the build and turns them into one question per host.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from foldyard import allowlist, buildgate, config


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "foldyard.toml").write_text("[proxy]\ndefault_deny = true\n[machine]\nwall = true\n")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("FOLDYARD_REPO", str(repo))
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(state))
    monkeypatch.delenv("FOLDYARD_LOG_DIR", raising=False)
    monkeypatch.setattr(config, "in_box", lambda: False)
    config.clear_caches()
    yield {"log": buildgate.build_log()}
    config.clear_caches()


def _row(host: str, *, build: bool = True, blocked: bool = True, ago: float = 0.0, ua: str = ""):
    """A log row, time-stamped when it is WRITTEN (see ``_stamped``): a row stamped when the test
    builds its steps can land a second before the build starts, and read as an older build's."""
    row = {"_ago": ago, "method": "", "host": host, "path": "", "status": 403 if blocked else 0}
    if blocked:
        row["blocked"] = True
    if build:
        row["build"] = True
    if ua:
        row["ua"] = ua
    return row


def _stamped(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k != "_ago"}
    ts = datetime.now(UTC) - timedelta(seconds=row.get("_ago", 0.0))
    return {"ts": ts.isoformat(timespec="seconds"), **out}


def _append(log, *rows) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        for row in rows:
            f.write(json.dumps(_stamped(row)) + "\n")


class _Build:
    """A fake build: each call appends the next batch of log rows and returns the next rc."""

    def __init__(self, log, steps: list[tuple[int, list[dict]]]) -> None:
        self.log, self.steps, self.calls = log, list(steps), 0

    def __call__(self) -> int:
        rc, rows = self.steps[self.calls]
        self.calls += 1
        _append(self.log, *rows)
        return rc


def _answers(*replies: str):
    asked: list[str] = []
    queue = list(replies)

    def prompt(question: str) -> str:
        asked.append(question)
        return queue.pop(0)

    return prompt, asked


def test_build_log_is_the_main_listeners(env, monkeypatch):
    # A build reaches the MAIN proxy port (building isn't per-worktree), so its rows land in the
    # main checkout's log even when `fy up` runs from a worktree.
    monkeypatch.setattr(config, "active_worktree", lambda: "feature")
    assert buildgate.build_log() == env["log"]
    assert env["log"].parent.name == "logs" and env["log"].parent.parent.name == "main"


def test_refusals_are_build_attributed_recent_ungranted_and_counted(env):
    since = datetime.now(UTC).replace(microsecond=0)
    raw = [
        _row("storage.googleapis.com", ua="node"),
        _row("storage.googleapis.com"),
        _row("box.example.com", build=False),  # a box session's refusal, not the build's
        _row("old.example.com", ago=3600),  # before this build
        _row("tunnel.example.com", blocked=False),  # a tunnel, not a refusal
        _row("granted.example.com"),
    ]
    rows = [_stamped(r) for r in raw]
    allowlist.grant("granted.example.com", "session")
    refused = allowlist.build_refusals(rows, since)
    assert [(e["host"], e["count"], e["uas"]) for e in refused] == [
        ("storage.googleapis.com", 2, ["node"])
    ]


def test_a_clean_build_asks_nothing(env):
    build = _Build(env["log"], [(0, [])])
    prompt, asked = _answers()
    assert buildgate.run(build, what="box image", interactive=True, prompt=prompt) == 0
    assert build.calls == 1 and asked == []


def test_a_refused_build_offers_the_host_and_retries_once_granted(env, capsys):
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com", ua="node")]), (0, [])])
    prompt, asked = _answers("o")
    assert buildgate.run(build, what="box image", interactive=True, prompt=prompt) == 0
    assert build.calls == 2
    assert "storage.googleapis.com" in asked[0]
    grant = next(g for g in allowlist.grants() if g["host"] == "storage.googleapis.com")
    assert grant["level"] == "once"
    out = capsys.readouterr().out
    assert "refused storage.googleapis.com" in out and "retrying" in out


def test_once_lasts_long_enough_for_a_build(env):
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com")]), (0, [])])
    prompt, _ = _answers("o")
    before = datetime.now(UTC)
    buildgate.run(build, what="box image", interactive=True, prompt=prompt)
    grant = next(g for g in allowlist.grants() if g["host"] == "storage.googleapis.com")
    expires = datetime.fromisoformat(grant["expires"])
    assert expires - before >= timedelta(seconds=buildgate.ONCE_TTL_SECONDS - 5)


def test_declining_returns_the_failure_without_retrying(env):
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com")])])
    prompt, _ = _answers("")  # the default is no
    assert buildgate.run(build, what="box image", interactive=True, prompt=prompt) == 1
    assert build.calls == 1
    assert allowlist.live_hosts() == []


def test_a_redirect_chain_is_walked_one_refusal_at_a_time(env):
    # Each retry gets one hop further: the CDN, then where it redirects.
    build = _Build(
        env["log"],
        [(1, [_row("cdn.example.com")]), (1, [_row("storage.example.com")]), (0, [])],
    )
    prompt, asked = _answers("s", "s")
    assert buildgate.run(build, what="box image", interactive=True, prompt=prompt) == 0
    assert build.calls == 3 and len(asked) == 2


def test_the_retries_are_bounded(env):
    steps = [(1, [_row(f"hop{i}.example.com")]) for i in range(buildgate.MAX_ATTEMPTS + 2)]
    build = _Build(env["log"], steps)
    prompt, _ = _answers(*["s"] * (buildgate.MAX_ATTEMPTS + 2))
    assert buildgate.run(build, what="box image", interactive=True, prompt=prompt) == 1
    assert build.calls == buildgate.MAX_ATTEMPTS


def test_a_failure_with_no_refusal_is_just_a_failure(env, capsys):
    build = _Build(env["log"], [(2, [_row("box.example.com", build=False)])])
    prompt, asked = _answers()
    assert buildgate.run(build, what="box image", interactive=True, prompt=prompt) == 2
    assert asked == [] and "refused" not in capsys.readouterr().out


def test_without_a_terminal_it_names_the_hosts_and_the_command(env, capsys):
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com")])])
    prompt, asked = _answers()
    assert buildgate.run(build, what="box image", interactive=False, prompt=prompt) == 1
    out = capsys.readouterr().out
    assert asked == []
    assert "fy allow add storage.googleapis.com" in out


def test_a_build_that_recovered_still_says_what_was_refused(env, capsys):
    # A tool that falls back to a mirror succeeds; the refusal is worth a line, not a question.
    build = _Build(env["log"], [(0, [_row("mirror.example.com")])])
    prompt, asked = _answers()
    assert buildgate.run(build, what="box image", interactive=True, prompt=prompt) == 0
    assert asked == [] and "mirror.example.com" in capsys.readouterr().out


def test_granted_hosts_come_back_as_recommend_lines(env, capsys):
    # Option 4: what one operator granted for a build, the team is offered at their own `fy up`.
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com", ua="node/22")]), (0, [])])
    prompt, _ = _answers("p")
    buildgate.run(build, what="box image", interactive=True, prompt=prompt)
    out = capsys.readouterr().out
    assert "[proxy]" in out and "recommend = [" in out
    assert '{ host = "storage.googleapis.com", why = "observed: node/22' in out


def test_in_the_box_the_gate_stays_out_of_the_way(env, monkeypatch):
    # Grants are host-side and the log is outside the mount: the box just runs the build.
    monkeypatch.setattr(config, "in_box", lambda: True)
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com")])])
    prompt, asked = _answers()
    assert buildgate.run(build, what="box image", interactive=True, prompt=prompt) == 1
    assert build.calls == 1 and asked == []


def test_an_unwalled_build_is_not_gated(env, monkeypatch):
    # No wall ⇒ builds egress directly; the proxy log says nothing about them.
    monkeypatch.setattr(config, "machine_wall", lambda: False)
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com")])])
    prompt, asked = _answers()
    assert buildgate.run(build, what="box image", interactive=True, prompt=prompt) == 1
    assert asked == []
