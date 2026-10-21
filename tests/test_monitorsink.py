"""monitorsink.py — the hooks' built-in file sink (docs/monitor-pipeline.md §6): an append that
never follows a symlink anything running as the operator could have placed (the box writes the
checkout through its mounts, as the operator), the host-only signing key, the signed and chained
line, and the verifier `fy monitor check` runs over a tampered file. Real files under tmp_path,
no subprocess."""

from __future__ import annotations

import json
import os
import stat

import pytest

from foldyard import monitorsink as ms

KEY = bytes(range(32))


@pytest.fixture(autouse=True)
def state_dir(monkeypatch, tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setattr(ms.config, "state_dir", lambda: home)
    return home


# ── the append: never through a symlink ────────────────────────────────────────────────


def test_append_creates_the_file_and_writes_the_line(tmp_path):
    target = tmp_path / "logs" / "events.jsonl"
    target.parent.mkdir()
    ms.append(str(target), b'{"a":1}\n')
    ms.append(str(target), b'{"a":2}\n')
    assert target.read_bytes() == b'{"a":1}\n{"a":2}\n'
    assert stat.S_IMODE(target.stat().st_mode) & 0o022 == 0  # 0644 under any umask


def test_a_symlinked_file_is_refused_and_its_target_untouched(tmp_path):
    victim = tmp_path / "zshrc"
    victim.write_text("export SAFE=1\n")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "events.jsonl").symlink_to(victim)
    with pytest.raises(ms.Refused, match="symlink"):
        ms.append(str(tmp_path / "logs" / "events.jsonl"), b"x\n")
    assert victim.read_text() == "export SAFE=1\n"


def test_a_dangling_symlink_is_never_created_through(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "events.jsonl").symlink_to(tmp_path / "planted")
    with pytest.raises(ms.Refused):
        ms.append(str(tmp_path / "logs" / "events.jsonl"), b"x\n")
    assert not (tmp_path / "planted").exists()


def test_a_symlinked_directory_on_the_way_is_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "logs").symlink_to(home)  # `logs → ~`, planted by the box
    with pytest.raises(ms.Refused, match="symlink"):
        ms.append(str(tmp_path / "repo" / "logs" / "events.jsonl"), b"x\n")
    assert list(home.iterdir()) == []


def test_a_missing_directory_is_refused_not_created(tmp_path):
    with pytest.raises(ms.Refused, match="doesn't exist"):
        ms.append(str(tmp_path / "nope" / "events.jsonl"), b"x\n")
    assert not (tmp_path / "nope").exists()


def test_a_fifo_is_refused_without_hanging(tmp_path):
    fifo = tmp_path / "events.jsonl"
    os.mkfifo(fifo)
    with pytest.raises(ms.Refused, match="not a regular file"):
        ms.append(str(fifo), b"x\n")


def test_a_hard_linked_file_is_refused(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    os.link(victim, tmp_path / "events.jsonl")
    with pytest.raises(ms.Refused, match="hard link"):
        ms.append(str(tmp_path / "events.jsonl"), b"x\n")
    assert victim.read_text() == "keep\n"


def test_a_relative_path_is_refused(tmp_path):
    with pytest.raises(ms.Refused, match="absolute"):
        ms.append("logs/events.jsonl", b"x\n")


def _trusting_me(monkeypatch):
    # A trusted link is ROOT's, in a directory this user can't write: no process running as the
    # operator (the box's writes reach the host as the operator) could have placed or swapped it.
    # Tests run unprivileged, so "root" is this uid, and the directory is made read-only.
    monkeypatch.setattr(ms, "_TRUSTED_UID", os.getuid())


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write any directory")
def test_a_root_link_in_an_unwritable_directory_is_followed(tmp_path, monkeypatch):
    _trusting_me(monkeypatch)
    real = tmp_path / "private" / "tmp"
    real.mkdir(parents=True)
    sys_dir = tmp_path / "sys"
    sys_dir.mkdir()
    (sys_dir / "tmp").symlink_to("../private/tmp")  # relative, like macOS's /tmp → private/tmp
    sys_dir.chmod(0o555)
    try:
        ms.append(str(sys_dir / "tmp" / "events.jsonl"), b"x\n")
    finally:
        sys_dir.chmod(0o755)
    assert (real / "events.jsonl").read_bytes() == b"x\n"


def test_the_same_link_in_a_writable_directory_is_refused(tmp_path, monkeypatch):
    _trusting_me(monkeypatch)
    real = tmp_path / "private"
    real.mkdir()
    (tmp_path / "sys").mkdir()
    (tmp_path / "sys" / "tmp").symlink_to(real)
    with pytest.raises(ms.Refused, match="symlink"):
        ms.append(str(tmp_path / "sys" / "tmp" / "events.jsonl"), b"x\n")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write any directory")
def test_a_link_owned_by_someone_else_than_root_is_refused(tmp_path):
    # unprivileged, so the link is ours — never root's — however locked its directory is
    (tmp_path / "private").mkdir()
    (tmp_path / "sys").mkdir()
    (tmp_path / "sys" / "tmp").symlink_to(tmp_path / "private")
    (tmp_path / "sys").chmod(0o555)
    try:
        with pytest.raises(ms.Refused, match="symlink"):
            ms.append(str(tmp_path / "sys" / "tmp" / "events.jsonl"), b"x\n")
    finally:
        (tmp_path / "sys").chmod(0o755)


def test_a_refusal_suggests_the_real_path(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    with pytest.raises(ms.Refused) as e:
        ms.append(str(tmp_path / "alias" / "events.jsonl"), b"x\n")
    assert os.path.realpath(tmp_path / "real") in str(e.value)


def test_a_torn_tail_is_ended_before_the_next_line(tmp_path):
    target = tmp_path / "events.jsonl"
    target.write_bytes(b'{"sink":{"seq":1,"pr')  # a write that failed half-way
    ms.append(str(target), b'{"b":1}\n')
    assert target.read_bytes() == b'{"sink":{"seq":1,"pr\n{"b":1}\n'


def test_short_writes_still_write_the_whole_line(tmp_path, monkeypatch):
    real_write = os.write
    monkeypatch.setattr(ms.os, "write", lambda fd, data: real_write(fd, bytes(data[:3])))
    target = tmp_path / "events.jsonl"
    ms.append(str(target), b'{"a":"0123456789"}\n')
    assert target.read_bytes() == b'{"a":"0123456789"}\n'


def test_read_refuses_a_symlink_too(tmp_path):
    (tmp_path / "victim").write_text("secret\n")
    (tmp_path / "events.jsonl").symlink_to(tmp_path / "victim")
    with pytest.raises(ms.Refused):
        ms.read(str(tmp_path / "events.jsonl"))


# ── the key ────────────────────────────────────────────────────────────────────────────


def test_the_key_is_made_once_host_only_and_never_the_relays(state_dir):
    assert ms.load_key() is None  # reading never creates it
    key = ms.ensure_key()
    assert len(key) == 32 and ms.ensure_key() == key == ms.load_key()
    path = state_dir / "monitor-sink.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ms.key_file().name != "monitor-relay.key"  # the guest holds that one


# ── the signed line ────────────────────────────────────────────────────────────────────


def _body(hook: str = "ca", n: int = 1) -> dict:
    return {"schema": "fy.monitor.events/1", "project": "fyex", "hook": hook,
            "first": ["b", n], "last": ["b", n], "events": [{"seq": n, "binary": "/bin/cat"}]}  # fmt: skip


def _file(n: int, hook: str = "ca", key: bytes = KEY) -> tuple[list[bytes], list[str]]:
    lines, macs, prev = [], [], ""
    for i in range(1, n + 1):
        line, mac = ms.signed_line(key, _body(hook, i), i, prev)
        lines.append(line)
        macs.append(mac)
        prev = mac
    return lines, macs


def test_a_line_leads_with_its_sequence_and_verifies():
    line, mac = ms.signed_line(KEY, _body(), 1, "")
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert line.startswith(b'{"sink":{"seq":1,"prev":"","mac":"')  # a torn line still names it
    doc = json.loads(line)
    assert doc["sink"] == {"seq": 1, "prev": "", "mac": mac}
    assert doc["events"] == _body()["events"]


def test_a_clean_file_passes():
    lines, macs = _file(3)
    got = ms.verify(b"".join(lines), KEY, "ca", (3, macs[-1]))
    assert got.problem == "" and got.last_seq == 3 and got.lines == 3 and got.events == 3


def _fails(data: bytes, line: int, why: str, state=None, hook="ca"):
    got = ms.verify(data, KEY, hook, state)
    assert got.problem, "expected a problem"
    assert got.line == line, got.problem
    assert why in got.problem, got.problem


def test_an_edited_line_fails_its_signature():
    lines, macs = _file(3)
    lines[1] = lines[1].replace(b"/bin/cat", b"/bin/dog")
    _fails(b"".join(lines), 2, "signature", (3, macs[-1]))


def test_a_field_added_to_the_sink_object_fails_its_signature():
    # the mac covers the whole line but `mac` itself — the sink object's other fields included
    lines, macs = _file(2)
    doc = json.loads(lines[1])
    doc["sink"]["note"] = "fine"
    lines[1] = json.dumps(doc).encode() + b"\n"
    _fails(b"".join(lines), 2, "signature", (2, macs[-1]))


def test_a_deleted_middle_line_breaks_the_sequence():
    lines, macs = _file(3)
    del lines[1]
    _fails(b"".join(lines), 2, "seq", (3, macs[-1]))


def test_reordered_lines_fail():
    lines, macs = _file(3)
    lines[1], lines[2] = lines[2], lines[1]
    _fails(b"".join(lines), 2, "seq", (3, macs[-1]))


def test_a_line_from_another_chain_fails_its_prev():
    # Two hooks' (or two files') lines verify under the same key; splicing one in breaks the chain
    lines, macs = _file(3)
    other, _ = ms.signed_line(KEY, _body(), 2, "f" * 64)
    lines[1] = other
    _fails(b"".join(lines), 2, "chain", (3, macs[-1]))


def test_a_skipped_seq_fails_even_with_an_intact_chain():
    # Only the sink signs, so this is the sink's own state lost or edited — still not the file
    # the hook's history says it is.
    lines, macs = _file(1)
    jump, mac3 = ms.signed_line(KEY, _body(n=3), 3, macs[0])
    _fails(lines[0] + jump, 2, "seq", (3, mac3))


def test_a_re_sent_seq_must_chain_from_the_same_line():
    lines, macs = _file(2)
    stray, mac = ms.signed_line(KEY, _body(n=2), 2, "e" * 64)  # seq 2 of some other chain
    _fails(lines[0] + lines[1] + stray, 3, "seq 2 where 3", (2, mac))


def test_a_truncated_tail_fails_against_the_hook_state():
    lines, macs = _file(3)
    _fails(b"".join(lines[:2]), 2, "end", (3, macs[-1]))


def test_a_forged_line_appended_fails():
    lines, macs = _file(2)
    forged, _ = ms.signed_line(b"\x00" * 32, _body(n=3), 3, macs[-1])
    _fails(b"".join([*lines, forged]), 3, "signature", (3, "whatever"))


def test_a_line_for_another_hook_fails():
    lines, macs = _file(2, hook="other")
    _fails(b"".join(lines), 1, "hook", (2, macs[-1]))


def test_lines_missing_from_the_start_fail():
    lines, macs = _file(3)
    _fails(b"".join(lines[1:]), 1, "seq", (3, macs[-1]))


def test_no_hook_state_means_the_tail_cannot_be_confirmed():
    lines, _ = _file(2)
    got = ms.verify(b"".join(lines), KEY, "ca", None)
    assert got.problem and "state" in got.problem


def test_junk_between_lines_fails():
    lines, macs = _file(2)
    data = lines[0] + b"not json at all\n" + lines[1]
    _fails(data, 2, "not a line", (2, macs[-1]))


def test_a_torn_line_then_its_rewrite_passes_with_a_note():
    lines, macs = _file(3)
    torn = lines[1][:40] + b"\n"  # the write failed after 40 bytes; the sink ended it with \n
    data = lines[0] + torn + lines[1] + lines[2]
    got = ms.verify(data, KEY, "ca", (3, macs[-1]))
    assert got.problem == "" and got.last_seq == 3
    assert any("line 2" in n and "torn" in n for n in got.notes)


def test_a_torn_line_not_followed_by_its_rewrite_fails():
    lines, macs = _file(3)
    data = lines[0] + lines[1][:40] + b"\n" + lines[2]
    _fails(data, 3, "seq", (3, macs[-1]))


def test_a_torn_trailing_fragment_passes_with_a_note():
    lines, macs = _file(2)
    torn, _ = ms.signed_line(KEY, _body(n=3), 3, macs[-1])
    got = ms.verify(b"".join(lines) + torn[:30], KEY, "ca", (2, macs[-1]))
    assert got.problem == "" and any("torn" in n for n in got.notes)


def test_a_fragment_that_isnt_the_next_lines_start_fails():
    lines, macs = _file(2)
    _fails(b"".join(lines) + b'{"sink":{"seq":9', 3, "not a line", (2, macs[-1]))


def test_a_redelivery_of_the_same_seq_passes_and_the_tail_is_the_later_one():
    # The supervisor stopped between writing seq 2 and saving its state: seq 2 is written again
    # (possibly with more events), and seq 3 chains from the re-written one.
    lines, macs = _file(1)
    b2a, _ = ms.signed_line(KEY, _body(n=2), 2, macs[0])
    b2b, mac2b = ms.signed_line(KEY, {**_body(n=2), "last": ["b", 3]}, 2, macs[0])
    b3, mac3 = ms.signed_line(KEY, _body(n=3), 3, mac2b)
    got = ms.verify(lines[0] + b2a + b2b + b3, KEY, "ca", (3, mac3))
    assert got.problem == "" and any("re-sent" in n for n in got.notes)
    _fails(lines[0] + b2a + b2b, 3, "end", (3, mac3))  # seq 3 cut off
    # …and so is dropping the later copy at the end: the state names the line written last
    _fails(lines[0] + b2a, 2, "last line", (2, mac2b))


def test_a_line_written_but_not_yet_recorded_is_a_note():
    lines, macs = _file(3)
    got = ms.verify(b"".join(lines), KEY, "ca", (2, macs[1]))
    assert got.problem == "" and any("not yet recorded" in n for n in got.notes)


def test_an_empty_file_matches_a_hook_that_wrote_nothing():
    assert ms.verify(b"", KEY, "ca", (0, "")).problem == ""
    _fails(b"", 0, "end", (1, "x"))
