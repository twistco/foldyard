"""transcripts.py — `foldyard transcripts`. The engine + rsync are mocked; we assert the
Mac-only guard, the bound-out-dir vs box-cp staging choice, and the additive rsync."""

from __future__ import annotations

from pathlib import Path

import pytest

from foldyard import config, stack, transcripts


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """Pretend we're on the Mac with rsync present; stack.resolve mocked; subprocess.run
    recorded. Returns the recorded commands + the tmp checkout."""
    checkout = tmp_path / "repo"
    checkout.mkdir()
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(transcripts, "which", lambda c: "/usr/bin/rsync")
    ctx = stack.Context(
        main=checkout,
        env={"FOLDYARD_CHECKOUT": str(checkout), "HERE": "dev-stack"},
        compose=[],
        app="app",
        project="tangible-podman",
        worktree="",
    )
    monkeypatch.setattr(transcripts.stack, "resolve", lambda *a, **k: ctx)
    monkeypatch.setattr(config, "codex_enabled", lambda: False)
    monkeypatch.delenv("DEVBOX_TRANSCRIPTS", raising=False)
    monkeypatch.delenv("DEVBOX_CODEX_TRANSCRIPTS", raising=False)
    monkeypatch.delenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # `<engine> ps` is the box-running probe — empty stdout ⇒ not running.
        return _Proc(0, "")

    monkeypatch.setattr(transcripts.subprocess, "run", fake_run)
    return {"checkout": checkout, "calls": calls}


def test_refuses_in_box(monkeypatch, capsys):
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert transcripts.transcripts() == 1
    assert "ON THE MAC" in capsys.readouterr().err


def test_missing_rsync_fails(mac, monkeypatch):
    monkeypatch.setattr(transcripts, "which", lambda c: None)
    assert transcripts.transcripts() == 1


def test_bound_out_dir_rsynced(mac):
    # Populate the default bound-out dir so it's used as the staging source.
    src = mac["checkout"] / "dev-stack/.devbox-claude/projects"
    src.mkdir(parents=True)
    (src / "session.jsonl").write_text("{}")
    assert transcripts.transcripts() == 0
    rsyncs = [c for c in mac["calls"] if c and c[0] == "rsync"]
    assert rsyncs and "-rtu" in rsyncs[0] and "--no-perms" in rsyncs[0]
    assert "--delete" not in rsyncs[0]  # additive: the archive only grows


def test_empty_source_no_box_is_error(mac, capsys):
    # No bound-out dir + the box-running probe returns empty ⇒ nothing to copy.
    assert transcripts.transcripts() == 1
    assert "nothing to copy" in capsys.readouterr().err
    assert not any(c and c[0] == "rsync" for c in mac["calls"])


def test_box_cp_fallback(mac, monkeypatch):
    """Empty bound-out dir but the box is running ⇒ `<engine> cp` projects/. into a tmp."""
    seq = []

    def fake_run(cmd, **kw):
        seq.append(cmd)
        if cmd[1:2] == ["ps"]:
            return _Proc(0, "deadbeef\n")  # box IS running
        if cmd[1:2] == ["exec"]:
            return _Proc(0, "/home/vscode/.claude")
        if cmd[1:2] == ["cp"]:
            # Drop a file into the cp destination (the tmp staging dir) so it's non-empty.
            import pathlib

            (pathlib.Path(cmd[-1]) / "s.jsonl").write_text("{}")
            return _Proc(0)
        return _Proc(0)

    monkeypatch.setattr(transcripts.subprocess, "run", fake_run)
    assert transcripts.transcripts() == 0
    assert any(c[1:2] == ["cp"] for c in seq)
    assert any(c and c[0] == "rsync" for c in seq)


def test_dry_run_passes_flag(mac, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    src = mac["checkout"] / "dev-stack/.devbox-claude/projects"
    src.mkdir(parents=True)
    (src / "session.jsonl").write_text("{}")
    assert transcripts.transcripts() == 0
    rsync = next(c for c in mac["calls"] if c and c[0] == "rsync")
    assert "--dry-run" in rsync


def test_codex_sessions_are_archived_only_when_configured(mac, monkeypatch):
    claude = mac["checkout"] / "dev-stack/.devbox-claude/projects"
    claude.mkdir(parents=True)
    (claude / "claude.jsonl").write_text("{}")
    codex = mac["checkout"] / "dev-stack/.devbox-codex/sessions"
    codex.mkdir(parents=True)
    (codex / "codex.jsonl").write_text("{}")

    monkeypatch.setattr(config, "codex_enabled", lambda: False)
    assert transcripts.transcripts() == 0
    assert len([c for c in mac["calls"] if c and c[0] == "rsync"]) == 1

    mac["calls"].clear()
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    assert transcripts.transcripts() == 0
    rsyncs = [c for c in mac["calls"] if c and c[0] == "rsync"]
    assert len(rsyncs) == 2
    assert rsyncs[1][-2] == f"{codex}/"
    assert rsyncs[1][-1] == f"{Path.home() / '.codex/sessions'}/"


def test_codex_only_project_does_not_require_claude_source(mac, monkeypatch):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    codex = mac["checkout"] / "dev-stack/.devbox-codex/sessions"
    codex.mkdir(parents=True)
    (codex / "codex.jsonl").write_text("{}")

    assert transcripts.transcripts() == 0
    rsyncs = [c for c in mac["calls"] if c and c[0] == "rsync"]
    assert len(rsyncs) == 1
    assert rsyncs[0][-2] == f"{codex}/"


# ── sync_archive / sync_current: the reusable destructive-op guard ───────────────────


def test_sync_archive_noop_in_box(tmp_path, monkeypatch):
    """Inside the box the Mac archive is unreachable — never attempt a copy (success no-op)."""
    monkeypatch.setattr(config, "in_box", lambda: True)
    src = tmp_path / "projects"
    src.mkdir()
    (src / "s.jsonl").write_text("{}")
    assert transcripts.sync_archive(src) == 0


def test_sync_archive_noop_when_missing_or_empty(mac):
    assert transcripts.sync_archive(mac["checkout"] / "nope/projects") == 0  # missing
    empty = mac["checkout"] / "empty/projects"
    empty.mkdir(parents=True)
    assert transcripts.sync_archive(empty) == 0  # present but empty
    assert not any(c and c[0] == "rsync" for c in mac["calls"])


def test_sync_archive_copies_additively(mac, monkeypatch):
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(mac["checkout"] / "archive"))
    src = mac["checkout"] / "wt/.devbox-claude/projects"
    src.mkdir(parents=True)
    (src / "s.jsonl").write_text("{}")
    assert transcripts.sync_archive(src, what="wt feat") == 0
    rsync = next(c for c in mac["calls"] if c and c[0] == "rsync")
    assert "-rtu" in rsync and "--no-perms" in rsync and "--delete" not in rsync
    assert rsync[-2] == f"{src}/"


def test_sync_archive_returns_rsync_rc(mac, monkeypatch):
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(mac["checkout"] / "archive"))
    src = mac["checkout"] / "wt/.devbox-claude/projects"
    src.mkdir(parents=True)
    (src / "s.jsonl").write_text("{}")
    monkeypatch.setattr(transcripts.subprocess, "run", lambda cmd, **kw: _Proc(23))
    assert transcripts.sync_archive(src) == 23  # failure surfaces so callers can refuse


def test_sync_current_uses_bound_out_dir(mac, monkeypatch):
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(mac["checkout"] / "archive"))
    src = mac["checkout"] / "dev-stack/.devbox-claude/projects"
    src.mkdir(parents=True)
    (src / "s.jsonl").write_text("{}")
    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 0
    rsync = next(c for c in mac["calls"] if c and c[0] == "rsync")
    assert rsync[-2] == f"{src}/"


def test_sync_current_honours_devbox_transcripts(mac, monkeypatch):
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(mac["checkout"] / "archive"))
    alt = mac["checkout"] / "alt-projects"
    alt.mkdir()
    (alt / "s.jsonl").write_text("{}")
    monkeypatch.setenv("DEVBOX_TRANSCRIPTS", str(alt))
    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 0
    rsync = next(c for c in mac["calls"] if c and c[0] == "rsync")
    assert rsync[-2] == f"{alt}/"


def test_sync_current_archives_configured_codex_to_native_destination(mac, monkeypatch, capsys):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    claude = mac["checkout"] / "dev-stack/.devbox-claude/projects"
    claude.mkdir(parents=True)
    (claude / "claude.jsonl").write_text("{}")
    codex = mac["checkout"] / "dev-stack/.devbox-codex/sessions"
    codex.mkdir(parents=True)
    (codex / "codex.jsonl").write_text("{}")

    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 0

    rsyncs = [c for c in mac["calls"] if c and c[0] == "rsync"]
    assert len(rsyncs) == 2
    assert rsyncs[1][-2] == f"{codex}/"
    assert rsyncs[1][-1] == f"{Path.home() / '.codex/sessions'}/"
    assert "archiving Codex transcripts" in capsys.readouterr().out


@pytest.mark.parametrize("archive_from_env", [False, True])
def test_sync_current_codex_uses_child_of_explicit_archive(mac, monkeypatch, archive_from_env):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    codex = mac["checkout"] / "dev-stack/.devbox-codex/sessions"
    codex.mkdir(parents=True)
    (codex / "codex.jsonl").write_text("{}")
    archive = mac["checkout"] / "archive"
    if archive_from_env:
        monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(archive))
        dest = ""
    else:
        dest = str(archive)

    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env, dest=dest) == 0

    rsync = next(c for c in mac["calls"] if c and c[0] == "rsync")
    assert rsync[-2] == f"{codex}/"
    assert rsync[-1] == f"{archive / 'codex-sessions'}/"


def test_sync_current_honours_devbox_codex_transcripts(mac, monkeypatch):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(mac["checkout"] / "archive"))
    alt = mac["checkout"] / "alt-codex-sessions"
    alt.mkdir()
    (alt / "codex.jsonl").write_text("{}")
    monkeypatch.setenv("DEVBOX_CODEX_TRANSCRIPTS", str(alt))

    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 0

    rsync = next(c for c in mac["calls"] if c and c[0] == "rsync")
    assert rsync[-2] == f"{alt}/"
    assert rsync[-1] == f"{mac['checkout'] / 'archive/codex-sessions'}/"


def test_sync_current_ignores_codex_when_unconfigured(mac):
    codex = mac["checkout"] / "dev-stack/.devbox-codex/sessions"
    codex.mkdir(parents=True)
    (codex / "codex.jsonl").write_text("{}")

    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 0
    assert not any(c and c[0] == "rsync" for c in mac["calls"])


def test_sync_current_attempts_codex_after_claude_archive_failure(mac, monkeypatch):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    seen: list[tuple[Path, str]] = []

    def fake_sync(src, *, dest="", **_kwargs):
        seen.append((src, dest))
        return 7 if ".devbox-claude" in str(src) else 0

    monkeypatch.setattr(transcripts, "sync_archive", fake_sync)
    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 7
    assert [str(src) for src, _dest in seen] == [
        str(mac["checkout"] / "dev-stack/.devbox-claude/projects"),
        str(mac["checkout"] / "dev-stack/.devbox-codex/sessions"),
    ]


# ── the supervisor's per-interval auto-sync (`sweep`) ────────────────────────────────


@pytest.fixture
def sweeper(mac, monkeypatch, tmp_path):
    """A bound checkout with Claude auto-sync on, a populated bound-out dir, and rsync stubbed to
    a settable rc. Returns the knobs + the log/notify sinks the sweep reports through."""
    transcripts._sweep_state.clear()
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(tmp_path / "archive"))
    monkeypatch.setattr(config, "repo_root", lambda: mac["checkout"])
    monkeypatch.setattr(config, "dev_vm_rel", lambda: "dev-stack")
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_transcript_sync_seconds", lambda: 15.0)
    src = mac["checkout"] / "dev-stack/.devbox-claude/projects"
    src.mkdir(parents=True)
    (src / "session.jsonl").write_text("{}")

    box = {"rc": 0}
    logs: list[str] = []
    notes: list[tuple[str, str]] = []

    def fake_run(cmd, **_kw):
        mac["calls"].append(cmd)
        return _Proc(box["rc"])

    monkeypatch.setattr(transcripts.subprocess, "run", fake_run)
    return {
        "box": box,
        "logs": logs,
        "notes": notes,
        "src": src,
        "run": lambda: transcripts.sweep(
            logs.append, "", tick_seconds=2.0, notify=lambda t, b: notes.append((t, b))
        ),
    }


def test_sweep_off_by_default(mac, monkeypatch):
    """No `transcript_sync_seconds` ⇒ the sweep does nothing at all (the default posture)."""
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_transcript_sync_seconds", lambda: 0.0)
    transcripts._sweep_state.clear()
    transcripts.sweep(lambda m: None, "")
    assert not any(c and c[0] == "rsync" for c in mac["calls"])
    assert transcripts._sweep_state == {}


def test_sweep_noop_in_box(monkeypatch):
    """The archive is in the host's home — the box must never try (and must not keep state)."""
    monkeypatch.setattr(config, "in_box", lambda: True)
    transcripts._sweep_state.clear()
    transcripts.sweep(lambda m: None, "")
    assert transcripts._sweep_state == {}


def test_sweep_interval_floors_to_tick_multiples(sweeper, mac):
    """15s on a 2s tick = 7 ticks = 14s effective — floored, per the config's contract."""
    for _ in range(15):
        sweeper["run"]()
    rsyncs = [c for c in mac["calls"] if c and c[0] == "rsync"]
    assert len(rsyncs) == 3  # ticks 1, 8, 15


def test_sweep_interval_below_one_tick_runs_every_tick(sweeper, mac, monkeypatch):
    """A sub-tick interval floors to 1, never 0 — the tick rate is the floor, not a hot loop."""
    monkeypatch.setattr(config, "claude_transcript_sync_seconds", lambda: 0.5)
    for _ in range(3):
        sweeper["run"]()
    assert len([c for c in mac["calls"] if c and c[0] == "rsync"]) == 3


def test_sweep_rearms_when_interval_changes(sweeper, mac, monkeypatch):
    """An adopted-config edit takes effect on the next tick rather than after the old countdown."""
    sweeper["run"]()  # runs, then arms 6 more ticks of waiting
    sweeper["run"]()  # would be a wait...
    assert len([c for c in mac["calls"] if c and c[0] == "rsync"]) == 1
    monkeypatch.setattr(config, "claude_transcript_sync_seconds", lambda: 4.0)
    sweeper["run"]()  # ...but the new interval re-arms from zero
    assert len([c for c in mac["calls"] if c and c[0] == "rsync"]) == 2


def test_sweep_is_quiet_when_healthy(sweeper, mac):
    """A healthy sweep says NOTHING: it runs forever and the supervisor tees stdout to the log."""
    for _ in range(15):
        sweeper["run"]()
    assert sweeper["logs"] == []
    assert sweeper["notes"] == []
    rsync = next(c for c in mac["calls"] if c and c[0] == "rsync")
    assert "--stats" not in rsync  # quiet drops the stats block too


def test_sweep_tolerates_live_tree_rsync_codes(sweeper):
    """rc 24 (files vanished mid-transfer) is routine against a tree the agent is writing —
    reporting it would make the notification noise."""
    for rc in (24, 23):
        sweeper["box"]["rc"] = rc
        sweeper["run"]()
        for _ in range(6):  # burn the countdown to the next due tick
            sweeper["run"]()
    assert sweeper["logs"] == []
    assert sweeper["notes"] == []


def test_sweep_reports_failure_on_the_edge_only(sweeper):
    """One line when it breaks, one when it heals — not one per interval."""
    sweeper["box"]["rc"] = 12
    for _ in range(15):  # three due passes, all failing
        sweeper["run"]()
    assert len(sweeper["logs"]) == 1
    assert "FAILED" in sweeper["logs"][0] and "rc=12" in sweeper["logs"][0]

    sweeper["box"]["rc"] = 0
    for _ in range(7):
        sweeper["run"]()
    assert len(sweeper["logs"]) == 2
    assert "recovered" in sweeper["logs"][1]


def test_sweep_notifies_once_after_a_persistent_failure(sweeper):
    """Escalates to the push surface only if it STAYS broken, and only once."""
    sweeper["box"]["rc"] = 12
    for _ in range(7 * transcripts.NOTIFY_AFTER_FAILURES * 2):
        sweeper["run"]()
    assert len(sweeper["notes"]) == 1
    assert "transcript sync" in sweeper["notes"][0][0]
    assert "fy transcripts" in sweeper["notes"][0][1]


def test_sweep_keys_state_per_worktree(sweeper, mac, monkeypatch):
    """Two checkouts sweep independently — one failing must not mark the other."""
    logs: list[str] = []
    transcripts.sweep(logs.append, "", tick_seconds=2.0)
    sweeper["box"]["rc"] = 12
    transcripts.sweep(logs.append, "feat", tick_seconds=2.0)
    assert transcripts._sweep_state[("", "Claude")]["failing"] is False
    assert transcripts._sweep_state[("feat", "Claude")]["failing"] is True
    assert len(logs) == 1 and "(feat)" in logs[0]


def test_sweep_covers_codex_independently(sweeper, mac, monkeypatch):
    """Both agents get their own interval, state and source dir."""
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    monkeypatch.setattr(config, "codex_transcript_sync_seconds", lambda: 4.0)
    codex = mac["checkout"] / "dev-stack/.devbox-codex/sessions"
    codex.mkdir(parents=True)
    (codex / "s.jsonl").write_text("{}")
    sweeper["run"]()
    sources = [c[-2] for c in mac["calls"] if c and c[0] == "rsync"]
    assert sources == [f"{sweeper['src']}/", f"{codex}/"]
    assert {k[1] for k in transcripts._sweep_state} == {"Claude", "Codex"}


def test_sweep_never_raises(sweeper, monkeypatch):
    """One agent's hiccup must not wedge the supervisor's reconcile loop."""

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(transcripts, "sync_archive", boom)
    sweeper["run"]()
    assert len(sweeper["logs"]) == 1 and "disk full" in sweeper["logs"][0]
