"""transcripts.py — `foldyard transcripts`. The engine is mocked; the copy is REAL (into tmp dirs):
we assert the host-only guard, the bound-out-dir vs box-cp staging choice, the additive copy, and
that nothing the box can write makes the host follow a link out of the transcript tree."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from foldyard import config, stack, transcripts


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _archived(root: Path) -> dict[str, str]:
    """Every regular file under ``root`` → its text, links never followed."""
    found: dict[str, str] = {}
    for here, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(here) / name
            if path.is_symlink() or not path.is_file():
                found[str(path.relative_to(root))] = "<not a regular file>"
            else:
                found[str(path.relative_to(root))] = path.read_text()
    return found


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """Pretend we're on the host; stack.resolve mocked; engine calls recorded; HOME is a tmp dir
    so the default archives (``~/.claude/projects``, ``~/.codex/sessions``) land in the test's
    tree. Returns the recorded commands + the tmp checkout + the archive roots."""
    checkout = tmp_path / "repo"
    checkout.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(config, "in_box", lambda: False)
    ctx = stack.Context(
        main=checkout,
        env={"FOLDYARD_CHECKOUT": str(checkout), "HERE": "dev-stack"},
        compose=[],
        app="app",
        project="tangible-podman",
        worktree="",
    )
    resolved: list[dict] = []
    monkeypatch.setattr(transcripts.stack, "resolve", lambda *a, **k: resolved.append(k) or ctx)
    monkeypatch.setattr(transcripts.stack, "machine_down", lambda: None)
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
    return {
        "checkout": checkout,
        "calls": calls,
        "ctx": ctx,
        "resolved": resolved,
        "claude_archive": home / ".claude/projects",
        "codex_archive": home / ".codex/sessions",
        "src": checkout / "dev-stack/.devbox-claude/projects",
        "codex_src": checkout / "dev-stack/.devbox-codex/sessions",
    }


def _session(src: Path, rel: str = "proj/session.jsonl", text: str = "{}") -> Path:
    path = src / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_refuses_in_box(monkeypatch, capsys):
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert transcripts.transcripts() == 1
    assert "ON YOUR COMPUTER" in capsys.readouterr().err


def test_bound_out_dir_is_archived_into_the_hosts_history(mac):
    _session(mac["src"])
    assert transcripts.transcripts() == 0
    assert _archived(mac["claude_archive"]) == {"proj/session.jsonl": "{}"}
    assert mac["calls"] == []  # host-local: no engine, no rsync, nothing spawned


def test_empty_source_no_box_is_error(mac, capsys):
    # No bound-out dir + the box-running probe returns empty ⇒ nothing to copy.
    assert transcripts.transcripts() == 1
    assert "nothing to copy" in capsys.readouterr().err
    assert not mac["claude_archive"].exists()


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
            (Path(cmd[-1]) / "s.jsonl").write_text("{}")
            return _Proc(0)
        return _Proc(0)

    monkeypatch.setattr(transcripts.subprocess, "run", fake_run)
    assert transcripts.transcripts() == 0
    assert any(c[1:2] == ["cp"] for c in seq)
    assert _archived(mac["claude_archive"]) == {"s.jsonl": "{}"}


def test_box_cp_fallback_does_not_follow_links_the_box_copied_out(mac, monkeypatch, tmp_path):
    """`<engine> cp` reproduces a symlink in the box's projects/ as a symlink in the host's tmp
    stage, pointing at a HOST path — the copy out of the stage must not follow it either."""
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("TOPSECRET")

    def fake_run(cmd, **kw):
        if cmd[1:2] == ["ps"]:
            return _Proc(0, "deadbeef\n")
        if cmd[1:2] == ["cp"]:
            stage = Path(cmd[-1])
            (stage / "s.jsonl").write_text("{}")
            (stage / "keys").symlink_to(secret)
            (stage / "key.jsonl").symlink_to(secret / "id_rsa")
        return _Proc(0)

    monkeypatch.setattr(transcripts.subprocess, "run", fake_run)
    assert transcripts.transcripts() == 0
    assert _archived(mac["claude_archive"]) == {"s.jsonl": "{}"}


def test_box_fallback_talks_to_the_projects_engine(mac, monkeypatch):
    # The probe/exec/cp must carry the resolved env (the machine's socket) — without it, host-side
    # podman asks its DEFAULT connection, which need not be this project's VM at all.
    envs = []

    def fake_run(cmd, **kw):
        envs.append(kw.get("env"))
        if cmd[1:2] == ["ps"]:
            return _Proc(0, "deadbeef\n")
        if cmd[1:2] == ["cp"]:
            (Path(cmd[-1]) / "s.jsonl").write_text("{}")
        return _Proc(0)

    monkeypatch.setattr(transcripts.subprocess, "run", fake_run)
    assert transcripts.transcripts() == 0
    assert len(envs) == 3 and all(e is mac["ctx"].env for e in envs)  # ps, exec, cp


def test_a_stopped_machine_is_not_booted_for_host_side_transcripts(mac, monkeypatch, capsys):
    # The bound-out dir is on the host; the engine is only the fallback for a RUNNING box, which a
    # stopped VM rules out — so resolve without the machine and ask no engine anything.
    monkeypatch.setattr(transcripts.stack, "machine_down", lambda: "lima machine 'x' is stopped")
    session = _session(mac["src"])
    assert transcripts.transcripts() == 0
    assert mac["resolved"] == [{"no_machine": True}]
    assert mac["calls"] == []
    # …and with nothing bound out, the box isn't asked either: it can't be running.
    session.unlink()
    session.parent.rmdir()
    assert transcripts.transcripts() == 1
    assert mac["calls"] == [] and "nothing to copy" in capsys.readouterr().err


def test_dry_run_writes_nothing(mac, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "1")
    _session(mac["src"])
    assert transcripts.transcripts() == 0
    assert not mac["claude_archive"].exists()
    assert "would copy 1 file" in capsys.readouterr().out


def test_codex_sessions_are_archived_only_when_configured(mac, monkeypatch):
    _session(mac["src"], "proj/claude.jsonl")
    _session(mac["codex_src"], "2026/10/21/codex.jsonl")

    monkeypatch.setattr(config, "codex_enabled", lambda: False)
    assert transcripts.transcripts() == 0
    assert not mac["codex_archive"].exists()

    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    assert transcripts.transcripts() == 0
    assert _archived(mac["codex_archive"]) == {"2026/10/21/codex.jsonl": "{}"}
    assert _archived(mac["claude_archive"]) == {"proj/claude.jsonl": "{}"}


def test_codex_only_project_does_not_require_claude_source(mac, monkeypatch):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    _session(mac["codex_src"], "codex.jsonl")

    assert transcripts.transcripts() == 0
    assert _archived(mac["codex_archive"]) == {"codex.jsonl": "{}"}
    assert not mac["claude_archive"].exists()


# ── sync_archive / sync_current: the reusable destructive-op guard ───────────────────


def test_sync_archive_noop_in_box(tmp_path, monkeypatch):
    """Inside the box the host archive is unreachable — never attempt a copy (success no-op)."""
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(tmp_path / "archive"))
    src = tmp_path / "projects"
    src.mkdir()
    (src / "s.jsonl").write_text("{}")
    assert transcripts.sync_archive(src, anchor=tmp_path) == 0
    assert not (tmp_path / "archive").exists()


def test_sync_archive_noop_when_missing_or_empty(mac, monkeypatch):
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(mac["checkout"] / "archive"))
    co = mac["checkout"]
    assert transcripts.sync_archive(co / "nope/projects", anchor=co) == 0  # missing
    empty = co / "empty/projects"
    empty.mkdir(parents=True)
    assert transcripts.sync_archive(empty, anchor=co) == 0  # present but empty
    assert not (co / "archive").exists()


def test_sync_archive_copies_additively(mac, monkeypatch, tmp_path):
    """``-u`` without ``--delete``, as the rsync it replaces: a newer archive copy wins, an older
    one is replaced, an archive-only file stays, and the copy carries the source's mtime."""
    archive = tmp_path / "archive"
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(archive))
    src = mac["checkout"] / "wt/.devbox-claude/projects"
    fresh = _session(src, "p/fresh.jsonl", "source-fresh")
    stale = _session(src, "p/stale.jsonl", "source-older")
    _session(src, "p/new.jsonl", "brand-new")
    os.utime(fresh, ns=(1_000_000_000_000, 2_000_000_000_000))
    os.utime(stale, ns=(1_000_000_000_000, 1_000_000_000_000))
    held_fresh = _session(archive, "p/fresh.jsonl", "archive-older")
    os.utime(held_fresh, ns=(1_000_000_000_000, 1_500_000_000_000))
    held_stale = _session(archive, "p/stale.jsonl", "archive-newer")
    os.utime(held_stale, ns=(1_000_000_000_000, 3_000_000_000_000))
    _session(archive, "p/only-here.jsonl", "keep me")

    assert transcripts.sync_archive(src, anchor=mac["checkout"], what="wt feat") == 0
    assert _archived(archive) == {
        "p/fresh.jsonl": "source-fresh",  # source newer ⇒ replaced
        "p/stale.jsonl": "archive-newer",  # archive newer ⇒ kept (-u)
        "p/new.jsonl": "brand-new",
        "p/only-here.jsonl": "keep me",  # never deleted
    }
    assert (archive / "p/fresh.jsonl").stat().st_mtime_ns == 2_000_000_000_000
    # A transcript is private: the archive's new files are the operator's alone.
    assert stat.S_IMODE((archive / "p/new.jsonl").stat().st_mode) == 0o600


def test_same_mtime_different_size_is_recopied(mac, monkeypatch, tmp_path):
    """rsync's quick check: equal mtimes but a different size is not "up to date"."""
    archive = tmp_path / "archive"
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(archive))
    src = mac["checkout"] / "projects"
    grown = _session(src, "s.jsonl", "line1\nline2\n")
    held = _session(archive, "s.jsonl", "line1\n")
    for path in (grown, held):
        os.utime(path, ns=(1_000_000_000_000, 1_000_000_000_000))
    assert transcripts.sync_archive(src, anchor=mac["checkout"]) == 0
    assert (archive / "s.jsonl").read_text() == "line1\nline2\n"


def test_sync_archive_reports_a_failed_copy(mac, monkeypatch, tmp_path, capsys):
    """A copy that couldn't complete surfaces non-zero, so `worktree remove` can refuse."""
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(tmp_path / "archive"))
    monkeypatch.setattr(transcripts, "MAX_FILE_BYTES", 3)
    src = mac["checkout"] / "projects"
    _session(src, "big.jsonl", "far more than three bytes")
    assert transcripts.sync_archive(src, anchor=mac["checkout"]) == transcripts.RC_PARTIAL
    assert "big.jsonl" in capsys.readouterr().err
    assert _archived(tmp_path / "archive") == {}


def test_a_copy_that_would_fill_the_disk_is_refused(mac, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(tmp_path / "archive"))
    monkeypatch.setattr(transcripts, "MIN_FREE_BYTES", 1 << 62)
    src = mac["checkout"] / "projects"
    _session(src, "s.jsonl")
    assert transcripts.sync_archive(src, anchor=mac["checkout"]) == transcripts.RC_PARTIAL
    assert "free" in capsys.readouterr().err
    assert _archived(tmp_path / "archive") == {}


def test_sync_current_uses_bound_out_dir(mac):
    _session(mac["src"], "s.jsonl")
    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 0
    assert _archived(mac["claude_archive"]) == {"s.jsonl": "{}"}


def test_sync_current_honours_devbox_transcripts(mac, monkeypatch):
    alt = mac["checkout"] / "alt-projects"
    _session(alt, "s.jsonl", "alt")
    monkeypatch.setenv("DEVBOX_TRANSCRIPTS", str(alt))
    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 0
    assert _archived(mac["claude_archive"]) == {"s.jsonl": "alt"}


def test_an_override_outside_the_checkout_still_refuses_its_own_link(mac, monkeypatch, tmp_path):
    """``DEVBOX_TRANSCRIPTS`` may name a dir outside the checkout: its parent is the operator's
    choice (followed), but the dir the box writes into is still opened without following."""
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("TOPSECRET")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "projects").symlink_to(secret)
    monkeypatch.setenv("DEVBOX_TRANSCRIPTS", str(outside / "projects"))
    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) != 0
    assert "TOPSECRET" not in str(_archived(mac["claude_archive"]))


def test_sync_current_archives_configured_codex_to_native_destination(mac, monkeypatch, capsys):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    _session(mac["src"], "claude.jsonl")
    _session(mac["codex_src"], "codex.jsonl")

    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 0

    assert _archived(mac["claude_archive"]) == {"claude.jsonl": "{}"}
    assert _archived(mac["codex_archive"]) == {"codex.jsonl": "{}"}
    assert "archiving Codex transcripts" in capsys.readouterr().out


@pytest.mark.parametrize("archive_from_env", [False, True])
def test_sync_current_codex_uses_child_of_explicit_archive(mac, monkeypatch, archive_from_env):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    _session(mac["codex_src"], "codex.jsonl")
    archive = mac["checkout"].parent / "archive"
    if archive_from_env:
        monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(archive))
        dest = ""
    else:
        dest = str(archive)

    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env, dest=dest) == 0
    assert _archived(archive) == {"codex-sessions/codex.jsonl": "{}"}


def test_sync_current_honours_devbox_codex_transcripts(mac, monkeypatch):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    archive = mac["checkout"].parent / "archive"
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(archive))
    alt = mac["checkout"] / "alt-codex-sessions"
    _session(alt, "codex.jsonl")
    monkeypatch.setenv("DEVBOX_CODEX_TRANSCRIPTS", str(alt))

    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 0
    assert _archived(archive) == {"codex-sessions/codex.jsonl": "{}"}


def test_sync_current_ignores_codex_when_unconfigured(mac):
    _session(mac["codex_src"], "codex.jsonl")

    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 0
    assert not mac["codex_archive"].exists()


def test_sync_current_attempts_codex_after_claude_archive_failure(mac, monkeypatch):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    seen: list[tuple[Path, Path, str]] = []

    def fake_sync(src, *, anchor, dest="", **_kwargs):
        seen.append((src, anchor, dest))
        return 7 if ".devbox-claude" in str(src) else 0

    monkeypatch.setattr(transcripts, "sync_archive", fake_sync)
    env = {"FOLDYARD_CHECKOUT": str(mac["checkout"]), "HERE": "dev-stack"}
    assert transcripts.sync_current(env) == 7
    assert [(str(src), anchor) for src, anchor, _dest in seen] == [
        (str(mac["src"]), mac["checkout"]),
        (str(mac["codex_src"]), mac["checkout"]),
    ]


# ── the box writes the source tree: no link in it is ever followed ────────────────────
#
# The bound-out dirs live in the checkout, which the box can write. A link planted anywhere on the
# way to them, or inside them, would otherwise make the HOST copy whatever it points at (your ssh
# keys, cloud credentials, another checkout) into the archive — where host tools read it.


@pytest.fixture
def planted(mac, tmp_path, monkeypatch):
    """A host directory holding something secret, and an archive to check it never reaches."""
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("TOPSECRET")
    archive = tmp_path / "archive"
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(archive))
    return {**mac, "secret": secret, "archive": archive}


def _leaked(planted) -> bool:
    return (
        "TOPSECRET" in str(_archived(planted["archive"])) if planted["archive"].exists() else False
    )


def test_a_symlinked_source_root_is_refused(planted, capsys):
    src = planted["src"]
    src.parent.mkdir(parents=True)
    src.symlink_to(planted["secret"])
    assert transcripts.sync_archive(src, anchor=planted["checkout"]) == transcripts.RC_REFUSED
    assert not _leaked(planted)
    err = capsys.readouterr().err
    assert str(src) in err and "symlink" in err


def test_a_symlinked_parent_is_refused(planted, capsys, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "projects").mkdir(parents=True)
    (elsewhere / "projects/id_rsa").write_text("TOPSECRET")
    (planted["checkout"] / "dev-stack").mkdir()
    link = planted["checkout"] / "dev-stack/.devbox-claude"
    link.symlink_to(elsewhere)
    assert transcripts.sync_archive(planted["src"], anchor=planted["checkout"]) != 0
    assert not _leaked(planted)
    assert str(link) in capsys.readouterr().err


def test_a_symlinked_dev_vm_dir_is_refused(planted, tmp_path):
    """``dev_vm_dir`` is inside the checkout too — the box can swap it for a link as well."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".devbox-claude/projects").mkdir(parents=True)
    (elsewhere / ".devbox-claude/projects/id_rsa").write_text("TOPSECRET")
    (planted["checkout"] / "dev-stack").symlink_to(elsewhere)
    assert transcripts.sync_archive(planted["src"], anchor=planted["checkout"]) != 0
    assert not _leaked(planted)


def test_a_source_root_that_is_a_file_is_refused(planted):
    planted["src"].parent.mkdir(parents=True)
    planted["src"].write_text("not a dir")
    assert transcripts.sync_archive(planted["src"], anchor=planted["checkout"]) != 0
    assert not planted["archive"].exists()


def test_links_inside_the_tree_are_skipped_not_followed(planted, capsys):
    src = planted["src"]
    _session(src, "proj/a.jsonl", "real")
    (src / "proj/keys").symlink_to(planted["secret"])  # a symlinked directory
    (src / "proj/key.jsonl").symlink_to(planted["secret"] / "id_rsa")  # a symlinked file
    assert transcripts.sync_archive(src, anchor=planted["checkout"]) == 0
    assert _archived(planted["archive"]) == {"proj/a.jsonl": "real"}
    assert "skipped 2" in capsys.readouterr().err


def test_a_fifo_is_skipped_without_hanging(planted):
    """Opening a FIFO for reading blocks until a writer appears — the box would never write."""
    src = planted["src"]
    _session(src, "proj/a.jsonl", "real")
    os.mkfifo(src / "proj/pipe.jsonl")
    result: list[int] = []
    worker = threading.Thread(
        target=lambda: result.append(transcripts.sync_archive(src, anchor=planted["checkout"])),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "the archive copy hung on a FIFO"
    assert result == [0]
    assert _archived(planted["archive"]) == {"proj/a.jsonl": "real"}


def _lstat_lies(monkeypatch, name: str, mode: int) -> None:
    """The box swapped ``name`` for a link AFTER the walk looked at it: make the walk's look
    report ``mode`` while the entry on disk is the link — what's left is the open itself."""
    real = transcripts._lstat_at

    def lying(dir_fd: int, entry: str):
        st = real(dir_fd, entry)
        if entry != name:
            return st
        return os.stat_result((mode | 0o600, *tuple(st)[1:]))

    monkeypatch.setattr(transcripts, "_lstat_at", lying)


def test_a_dir_swapped_for_a_link_mid_walk_is_not_followed(planted, monkeypatch):
    src = planted["src"]
    _session(src, "proj/a.jsonl", "real")
    (src / "proj/keys").symlink_to(planted["secret"])
    _lstat_lies(monkeypatch, "keys", stat.S_IFDIR)
    assert transcripts.sync_archive(src, anchor=planted["checkout"]) == 0
    assert not _leaked(planted)


def test_a_file_swapped_for_a_link_mid_walk_is_not_followed(planted, monkeypatch):
    src = planted["src"]
    _session(src, "proj/a.jsonl", "real")
    (src / "proj/key.jsonl").symlink_to(planted["secret"] / "id_rsa")
    _lstat_lies(monkeypatch, "key.jsonl", stat.S_IFREG)
    assert transcripts.sync_archive(src, anchor=planted["checkout"]) == 0
    assert not _leaked(planted)


def test_a_file_swapped_for_a_fifo_mid_walk_does_not_hang(planted, monkeypatch):
    src = planted["src"]
    _session(src, "proj/a.jsonl", "real")
    os.mkfifo(src / "proj/pipe.jsonl")
    _lstat_lies(monkeypatch, "pipe.jsonl", stat.S_IFREG)
    worker = threading.Thread(
        target=lambda: transcripts.sync_archive(src, anchor=planted["checkout"]), daemon=True
    )
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "the archive copy hung on a FIFO"
    assert _archived(planted["archive"]) == {"proj/a.jsonl": "real"}


def test_a_file_that_vanishes_mid_walk_is_tolerated(planted, monkeypatch):
    """A live tree: an agent rotates a file between the listing and the copy."""
    src = planted["src"]
    _session(src, "proj/a.jsonl", "real")
    gone = _session(src, "proj/gone.jsonl", "rotated")
    real = transcripts._lstat_at

    def vanishing(dir_fd: int, entry: str):
        st = real(dir_fd, entry)
        if entry == "gone.jsonl":
            gone.unlink()
        return st

    monkeypatch.setattr(transcripts, "_lstat_at", vanishing)
    assert transcripts.sync_archive(src, anchor=planted["checkout"]) == transcripts.RC_VANISHED
    assert _archived(planted["archive"]) == {"proj/a.jsonl": "real"}


def test_the_archive_is_not_written_through_a_link_either(planted, tmp_path):
    """The archive is the host's own, but the NAMES in it come from the box: an existing link
    there under a name the box chose must not become a door out of the archive."""
    outside = tmp_path / "outside"
    outside.mkdir()
    planted["archive"].mkdir()
    (planted["archive"] / "proj").symlink_to(outside)
    (planted["archive"] / "held.jsonl").symlink_to(outside / "held.jsonl")
    src = planted["src"]
    _session(src, "proj/a.jsonl", "real")
    _session(src, "held.jsonl", "real")
    assert transcripts.sync_archive(src, anchor=planted["checkout"]) == transcripts.RC_PARTIAL
    assert list(outside.iterdir()) == []


def test_transcripts_refuses_a_planted_link(mac, tmp_path, capsys):
    """`fy transcripts` (the by-hand sync) goes through the same walk."""
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("TOPSECRET")
    mac["src"].parent.mkdir(parents=True)
    mac["src"].symlink_to(secret)
    assert transcripts.transcripts() != 0
    assert not mac["claude_archive"].exists() or "TOPSECRET" not in str(
        _archived(mac["claude_archive"])
    )
    assert "symlink" in capsys.readouterr().err
    assert mac["calls"] == []  # a refused bound-out dir is not "empty": no fallback to the box


# ── the supervisor's per-interval auto-sync (`sweep`) ────────────────────────────────


@pytest.fixture
def sweeper(mac, monkeypatch, tmp_path):
    """A bound checkout with Claude auto-sync on, a populated bound-out dir, and the copy stubbed
    to a settable outcome. Returns the knobs + the log/notify sinks the sweep reports through."""
    transcripts._sweep_state.clear()
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(tmp_path / "archive"))
    monkeypatch.setattr(config, "repo_root", lambda: mac["checkout"])
    monkeypatch.setattr(config, "dev_vm_rel", lambda: "dev-stack")
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_transcript_sync_seconds", lambda: 15.0)
    src = mac["src"]
    _session(src, "session.jsonl")

    box = {"rc": 0}
    copies: list[Path] = []
    logs: list[str] = []
    notes: list[tuple[str, str]] = []

    def fake_mirror(src_fd, dest_dir, dry):
        copies.append(dest_dir)
        rc = box["rc"]
        if rc == transcripts.RC_VANISHED:
            return transcripts._Tally(vanished=1)
        if rc == transcripts.RC_PARTIAL:
            return transcripts._Tally(errors=["session.jsonl: Permission denied"])
        if rc:
            raise OSError(rc, "disk on fire")
        return transcripts._Tally(copied=1)

    monkeypatch.setattr(transcripts, "_mirror", fake_mirror)
    return {
        "box": box,
        "copies": copies,
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
    monkeypatch.setattr(config, "repo_root", lambda: mac["checkout"])
    _session(mac["src"])
    transcripts._sweep_state.clear()
    transcripts.sweep(lambda m: None, "")
    assert not mac["claude_archive"].exists()
    assert transcripts._sweep_state == {}


def test_sweep_noop_in_box(monkeypatch):
    """The archive is in the host's home — the box must never try (and must not keep state)."""
    monkeypatch.setattr(config, "in_box", lambda: True)
    transcripts._sweep_state.clear()
    transcripts.sweep(lambda m: None, "")
    assert transcripts._sweep_state == {}


def test_sweep_copies_for_real(mac, monkeypatch, tmp_path):
    """The unstubbed sweep: a host-local copy of the bound-out dir into the archive."""
    transcripts._sweep_state.clear()
    archive = tmp_path / "archive"
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(archive))
    monkeypatch.setattr(config, "repo_root", lambda: mac["checkout"])
    monkeypatch.setattr(config, "dev_vm_rel", lambda: "dev-stack")
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_transcript_sync_seconds", lambda: 15.0)
    _session(mac["src"], "s.jsonl", "live")
    logs: list[str] = []
    transcripts.sweep(logs.append, "")
    assert _archived(archive) == {"s.jsonl": "live"}
    assert logs == []


def test_sweep_logs_a_planted_link_naming_it(mac, monkeypatch, tmp_path):
    transcripts._sweep_state.clear()
    monkeypatch.setenv("FOLDYARD_TRANSCRIPTS_ARCHIVE", str(tmp_path / "archive"))
    monkeypatch.setattr(config, "repo_root", lambda: mac["checkout"])
    monkeypatch.setattr(config, "dev_vm_rel", lambda: "dev-stack")
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_transcript_sync_seconds", lambda: 15.0)
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("TOPSECRET")
    mac["src"].parent.mkdir(parents=True)
    mac["src"].symlink_to(secret)
    logs: list[str] = []
    transcripts.sweep(logs.append, "")
    assert len(logs) == 1 and "FAILED" in logs[0] and str(mac["src"]) in logs[0]
    assert not (tmp_path / "archive/id_rsa").exists()


def test_sweep_interval_floors_to_tick_multiples(sweeper):
    """15s on a 2s tick = 7 ticks = 14s effective — floored, per the config's contract."""
    for _ in range(15):
        sweeper["run"]()
    assert len(sweeper["copies"]) == 3  # ticks 1, 8, 15


def test_sweep_interval_below_one_tick_runs_every_tick(sweeper, monkeypatch):
    """A sub-tick interval floors to 1, never 0 — the tick rate is the floor, not a hot loop."""
    monkeypatch.setattr(config, "claude_transcript_sync_seconds", lambda: 0.5)
    for _ in range(3):
        sweeper["run"]()
    assert len(sweeper["copies"]) == 3


def test_sweep_rearms_when_interval_changes(sweeper, monkeypatch):
    """An adopted-config edit takes effect on the next tick rather than after the old countdown."""
    sweeper["run"]()  # runs, then arms 6 more ticks of waiting
    sweeper["run"]()  # would be a wait...
    assert len(sweeper["copies"]) == 1
    monkeypatch.setattr(config, "claude_transcript_sync_seconds", lambda: 4.0)
    sweeper["run"]()  # ...but the new interval re-arms from zero
    assert len(sweeper["copies"]) == 2


def test_sweep_is_quiet_when_healthy(sweeper, capsys):
    """A healthy sweep says NOTHING: it runs forever and the supervisor tees stdout to the log."""
    for _ in range(15):
        sweeper["run"]()
    assert sweeper["logs"] == []
    assert sweeper["notes"] == []
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_sweep_tolerates_live_tree_churn(sweeper):
    """A file vanishing mid-copy is routine against a tree the agent is writing — reporting it
    would make the notification noise."""
    sweeper["box"]["rc"] = transcripts.RC_VANISHED
    for _ in range(15):
        sweeper["run"]()
    assert sweeper["logs"] == []
    assert sweeper["notes"] == []


def test_sweep_reports_files_it_could_not_copy(sweeper):
    """Unlike rsync's rc 23, a partial copy no longer hides live-tree churn — it is a real file
    left behind (unreadable, over the size cap, no room), so the sweep says so, once."""
    sweeper["box"]["rc"] = transcripts.RC_PARTIAL
    for _ in range(15):
        sweeper["run"]()
    assert len(sweeper["logs"]) == 1 and "Permission denied" in sweeper["logs"][0]


def test_sweep_reports_failure_on_the_edge_only(sweeper):
    """One line when it breaks, one when it heals — not one per interval."""
    sweeper["box"]["rc"] = 12
    for _ in range(15):  # three due passes, all failing
        sweeper["run"]()
    assert len(sweeper["logs"]) == 1
    assert "FAILED" in sweeper["logs"][0] and "disk on fire" in sweeper["logs"][0]

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


def test_sweep_keys_state_per_worktree(sweeper):
    """Two checkouts sweep independently — one failing must not mark the other."""
    logs: list[str] = []
    transcripts.sweep(logs.append, "", tick_seconds=2.0)
    sweeper["box"]["rc"] = 12
    transcripts.sweep(logs.append, "feat", tick_seconds=2.0)
    assert transcripts._sweep_state[("", "Claude")]["failing"] is False
    assert transcripts._sweep_state[("feat", "Claude")]["failing"] is True
    assert len(logs) == 1 and "(feat)" in logs[0]


def test_sweep_covers_codex_independently(sweeper, mac, monkeypatch):
    """Both agents get their own interval, state and destination."""
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    monkeypatch.setattr(config, "codex_transcript_sync_seconds", lambda: 4.0)
    _session(mac["codex_src"], "s.jsonl")
    sweeper["run"]()
    archive = Path(os.environ["FOLDYARD_TRANSCRIPTS_ARCHIVE"])
    assert sweeper["copies"] == [archive, archive / "codex-sessions"]
    assert {k[1] for k in transcripts._sweep_state} == {"Claude", "Codex"}


def test_sweep_never_raises(sweeper, monkeypatch):
    """One agent's hiccup must not wedge the supervisor's reconcile loop."""

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(transcripts, "_archive", boom)
    sweeper["run"]()
    assert len(sweeper["logs"]) == 1 and "disk full" in sweeper["logs"][0]
