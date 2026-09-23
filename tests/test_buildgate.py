"""The build gate: wall refusals during an image build are reported, offered, and retried.

A build's tool reports the URL it ASKED for; the wall refused whatever host that led to — often a
redirect target the proxy can't see inside the build's tunnel (cdn.playwright.dev → GCS, seen
live). Before this the only way to name the real host was to read egress.jsonl. The gate reads
the build-attributed refusals after the build and turns them into one question per host.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
        self.urls: list[str | None] = []

    def __call__(self, proxy_url: str | None) -> int:
        self.urls.append(proxy_url)
        rc, rows = self.steps[self.calls]
        self.calls += 1
        _append(self.log, *rows)
        return rc


def _answers(*replies: str):
    asked: list[str] = []
    queue = list(replies)

    def prompt(question: str) -> str:
        asked.append(question)
        return queue.pop(0) if queue else ""  # past the scripted replies: the default, no

    return prompt, asked


def test_build_log_is_the_main_listeners(env, monkeypatch):
    # A build reaches the MAIN proxy port (building isn't per-worktree), so its rows land in the
    # main checkout's log even when `fy up` runs from a worktree.
    monkeypatch.setattr(config, "active_worktree", lambda: "feature")
    assert buildgate.build_log() == env["log"]


def test_build_tokens_never_touch_the_real_state_dir(env):
    # conftest pins FOLDYARD_BUILD_TOKENS per test; a stray one was once written to ~/.foldyard.
    assert str(buildgate.tokens_file()).startswith(str(env["log"].parents[3]))
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
    assert build.calls == 3
    assert [q.split("?")[0] for q in asked[:2]] == [
        "  allow cdn.example.com (1×) for builds",
        "  allow storage.example.com (1×) for builds",
    ]


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


def _checkout_toml(env) -> Path:
    return config.current().repo_root / "foldyard.toml"


def test_the_gate_offers_to_write_the_recommend_lines_and_readopts_a_clean_checkout(env, capsys):
    from foldyard import configpin

    cfg = config.current()
    configpin.adopt(
        cfg
    )  # the checkout matches what the host runs: the edit will be the only change
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com", ua="node")]), (0, [])])
    prompt, asked = _answers("s", "y")
    assert buildgate.run(build, what="box image", interactive=True, prompt=prompt) == 0
    assert "foldyard.toml" in asked[-1]
    text = _checkout_toml(env).read_text()
    assert 'host = "storage.googleapis.com"' in text
    drift = configpin.inspect(cfg)
    assert not drift.changed  # re-adopted: the operator's own edit needs no second review
    assert "adopted" in capsys.readouterr().out


def test_a_drifted_checkout_is_edited_but_left_to_the_adoption_gate(env, capsys):
    from foldyard import configpin

    cfg = config.current()
    configpin.adopt(cfg)
    toml = _checkout_toml(env)
    toml.write_text(toml.read_text() + "\n[project]\nname = 'edited-in-the-box'\n")
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com")]), (0, [])])
    prompt, _ = _answers("s", "y")
    buildgate.run(build, what="box image", interactive=True, prompt=prompt)
    assert 'host = "storage.googleapis.com"' in toml.read_text()
    assert configpin.inspect(
        cfg
    ).changed  # NOT adopted: an edit the operator didn't make rides in it
    assert "fy up" in capsys.readouterr().out


def test_declining_the_write_leaves_the_file_alone_and_prints_the_block(env, capsys):
    before = _checkout_toml(env).read_text()
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com")]), (0, [])])
    prompt, _ = _answers("s", "")
    buildgate.run(build, what="box image", interactive=True, prompt=prompt)
    assert _checkout_toml(env).read_text() == before
    assert "recommend = [" in capsys.readouterr().out


# ── build-scoped grants: the gate grants for builds, proven by a per-build secret ─────────


def _tokens(env) -> dict:
    path = buildgate.tokens_file()
    return json.loads(path.read_text())["tokens"] if path.exists() else {}


def test_a_build_carries_a_secret_the_host_recorded_and_revokes_after(env):
    # The fixed `fy-build` marker is readable by anyone, the box included; a build-only grant
    # needs proof the box can't simply present. The gate mints one per build.
    import hashlib
    from urllib.parse import urlsplit

    seen: dict = {}

    def build(url: str | None) -> int:
        parts = urlsplit(url or "")
        seen["user"], seen["secret"] = parts.username, parts.password
        seen["recorded"] = dict(_tokens(env))
        return 0

    assert buildgate.run(build, what="box image", interactive=True, prompt=lambda q: "") == 0
    assert seen["user"] == "fy-build" and seen["secret"] not in ("", "fy-build", None)
    digest = hashlib.sha256(seen["secret"].encode()).hexdigest()
    assert list(seen["recorded"]) == [digest]  # the file holds a hash, never the secret
    assert seen["secret"] not in buildgate.tokens_file().read_text()
    assert _tokens(env) == {}  # revoked once the build ended


def test_the_secret_is_revoked_even_when_the_build_raises(env):
    def build(url):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        buildgate.run(build, what="box image", interactive=True, prompt=lambda q: "")
    assert _tokens(env) == {}


def test_the_gate_grants_for_builds_only(env):
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com")]), (0, [])])
    prompt, asked = _answers("s")
    assert buildgate.run(build, what="box image", interactive=True, prompt=prompt) == 0
    assert "for builds" in asked[0]
    eff = allowlist.effective()
    assert eff["build_allow"] == ["storage.googleapis.com"] and eff["allow"] == []


def test_the_gate_shows_a_build_recommendations_why(env):
    toml = config.current().repo_root / "foldyard.toml"
    toml.write_text(
        toml.read_text().replace(
            "[proxy]\n",
            '[proxy]\nrecommend = [{ host = "storage.googleapis.com", why = "CfT downloads",'
            ' when = "build" }]\n',
        )
    )
    config.clear_caches()
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com")]), (0, [])])
    prompt, asked = _answers("o")
    buildgate.run(build, what="box image", interactive=True, prompt=prompt)
    assert "CfT downloads" in asked[0]


def test_the_shared_lines_are_build_recommendations(env):
    from foldyard import configpin

    configpin.adopt(config.current())
    build = _Build(env["log"], [(1, [_row("storage.googleapis.com")]), (0, [])])
    prompt, _ = _answers("s", "y")
    buildgate.run(build, what="box image", interactive=True, prompt=prompt)
    assert 'when = "build"' in _checkout_toml(env).read_text()


def test_in_the_box_a_build_gets_the_marker_but_no_secret(env, monkeypatch):
    # It still tunnels (the build has no CA), but it can't use build-only grants.
    monkeypatch.setattr(config, "in_box", lambda: True)
    build = _Build(env["log"], [(0, [])])
    buildgate.run(build, what="box image", interactive=True, prompt=lambda q: "")
    assert build.urls[0] is not None and "fy-build:fy-build@" in build.urls[0]
    assert _tokens(env) == {}


def test_an_unwalled_build_gets_no_proxy(env, monkeypatch):
    monkeypatch.setattr(config, "machine_wall", lambda: False)
    build = _Build(env["log"], [(0, [])])
    buildgate.run(build, what="box image", interactive=True, prompt=lambda q: "")
    assert build.urls == [None]
