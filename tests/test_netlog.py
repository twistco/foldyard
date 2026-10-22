"""The Network Log's index over the egress log (``plugins/_netlog.py``): the whole retained
history, read once and then only what was appended — the panel repaints every second."""

from __future__ import annotations

import json

from foldyard.plugins import _netlog


def _append(path, *hosts: str, raw: str = "") -> None:
    with path.open("a") as f:
        f.writelines(
            json.dumps({"ts": "2026-10-22T20:00:00+00:00", "host": h}) + "\n" for h in hosts
        )
        f.write(raw)


def _requests(index: _netlog.NetworkIndex) -> dict[str, int]:
    return {view.host: view.requests for view in index.refresh().hosts}


def test_a_refresh_reads_only_what_was_appended(tmp_path, monkeypatch):
    log = tmp_path / "egress.jsonl"
    _append(log, "a.com", "a.com")
    index = _netlog.NetworkIndex(log)
    assert _requests(index) == {"a.com": 2}
    read_so_far = log.stat().st_size
    reads: list[int] = []
    real = _netlog._read_from
    monkeypatch.setattr(_netlog, "_read_from", lambda f, at: reads.append(at) or real(f, at))
    _append(log, "b.com")
    assert _requests(index) == {"a.com": 2, "b.com": 1}
    assert reads == [read_so_far]  # one read, from where the last refresh stopped


def test_a_half_written_line_waits_for_its_newline(tmp_path):
    log = tmp_path / "egress.jsonl"
    _append(log, "a.com", raw='{"ts": "2026-10-22T20:00:01+00:00", "host": "b.c')
    index = _netlog.NetworkIndex(log)
    assert _requests(index) == {"a.com": 1}
    _append(log, raw='om"}\n')
    assert _requests(index) == {"a.com": 1, "b.com": 1}


def test_a_rotation_neither_loses_nor_double_counts(tmp_path):
    # The proxy rotates by renaming the live file to a dated backup and starting a new one: the
    # rows already read stay counted once, and the renamed file's unread tail is still read.
    log = tmp_path / "egress.jsonl"
    _append(log, "a.com")
    index = _netlog.NetworkIndex(log)
    assert _requests(index) == {"a.com": 1}
    _append(log, "a.com")  # written just before the rotation, not yet read
    log.replace(tmp_path / "egress.20261022T200000Z.jsonl")
    _append(log, "b.com")
    assert _requests(index) == {"a.com": 2, "b.com": 1}


def test_a_pruned_backup_drops_out(tmp_path):
    log = tmp_path / "egress.jsonl"
    backup = tmp_path / "egress.20261022T200000Z.jsonl"
    _append(backup, "old.example")
    _append(log, "new.example")
    index = _netlog.NetworkIndex(log)
    assert _requests(index) == {"old.example": 1, "new.example": 1}
    backup.unlink()
    assert _requests(index) == {"new.example": 1}


def test_a_file_rewritten_in_place_is_read_again(tmp_path):
    # Same path, different content (a truncate, or an inode reused for a new file): the head of
    # the file no longer matches what was read, so the index starts that file over.
    log = tmp_path / "egress.jsonl"
    _append(log, "a.com", "a.com")
    index = _netlog.NetworkIndex(log)
    assert _requests(index) == {"a.com": 2}
    log.write_text(json.dumps({"ts": "2026-10-22T20:00:00+00:00", "host": "zz.com"}) + "\n" * 200)
    assert _requests(index) == {"zz.com": 1}


def test_junk_lines_are_skipped(tmp_path):
    log = tmp_path / "egress.jsonl"
    _append(log, "a.com", raw="not json\n[1, 2]\n{}\n")
    assert _requests(_netlog.NetworkIndex(log)) == {"a.com": 1, "—": 1}
