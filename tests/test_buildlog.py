"""buildlog.BuildLog — an image build's output into a log on your computer: cleaned, stamped, one
per kind of build, and on your terminal only its path, a heartbeat, and its tail when it fails."""

from __future__ import annotations

import errno
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from foldyard import buildlog

_STAMPED = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00 ")
_HOSTILE = "\x1b]52;c;ZXZpbA==\x07\x1b[2K\x9b\u202e✓ all good"


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(buildlog, "_builds_live", False)


def _lines(name: str) -> list[str]:
    return buildlog.build_log_path(name).read_text().splitlines()


def test_each_line_is_cleaned_and_stamped(capsys):
    log = buildlog.open_build_log("box-build")
    log.line("STEP 1/2: FROM debian")
    log.line(_HOSTILE)
    log.close()
    lines = _lines("box-build")
    assert all(_STAMPED.match(line) for line in lines), lines
    assert [_STAMPED.sub("", line) for line in lines] == [
        "STEP 1/2: FROM debian",
        "]52;c;ZXZpbA==✓ all good",
    ]
    err = capsys.readouterr().err
    assert f"▶ build output → {buildlog.build_log_path('box-build')}" in err
    assert "STEP 1/2" not in err  # the build itself stays off your terminal


def test_each_build_empties_its_log():
    first = buildlog.open_build_log("box-build")
    first.line("from the first build")
    first.close()
    second = buildlog.open_build_log("box-build")
    second.line("from the second")
    second.close()
    assert [_STAMPED.sub("", line) for line in _lines("box-build")] == ["from the second"]


def test_a_failed_build_shows_its_tail_without_stamps_and_its_path(capsys):
    log = buildlog.open_build_log("box-build")
    for i in range(40):
        log.line(f"line {i}")
    log.line(_HOSTILE)
    log.said(2, "box image build")
    log.close()
    err = capsys.readouterr().err
    assert f"✗ box image build failed (exit 2) — last {buildlog.BUILD_TAIL_LINES} log lines:" in err
    assert "  line 39\n" in err and "  line 15\n" not in err
    assert "  ]52;c;ZXZpbA==✓ all good\n" in err  # through the cleaner, the stamp left behind
    assert f"  full log: cat {buildlog.build_log_path('box-build')}" in err
    assert "\x1b" not in err


def test_a_build_that_worked_says_where_its_log_is(capsys):
    log = buildlog.open_build_log("box-build")
    log.line("STEP 1/1")
    log.said(0, "box image build")
    log.close()
    err = capsys.readouterr().err
    assert f"✓ box image build done (log: {buildlog.build_log_path('box-build')})" in err
    assert "STEP 1/1" not in err


def test_live_the_build_is_shown_as_it_comes_and_still_logged(monkeypatch, capsys):
    monkeypatch.setattr(buildlog, "_builds_live", True)
    log = buildlog.open_build_log("box-build")
    log.line(_HOSTILE)
    log.said(1, "box image build")
    log.close()
    err = capsys.readouterr().err
    assert "]52;c;ZXZpbA==✓ all good\n" in err and "\x1b" not in err
    assert "its output is above" in err and "last 25" not in err
    assert "✓ all good" in _lines("box-build")[0]


def _returns_at_once(open_, where):
    """``open_()``, which must return at once: a FIFO with no reader is refused, not waited on.
    One that waits is let go (opened for reading) so the suite doesn't hang on it."""
    import threading

    got = {}
    thread = threading.Thread(target=lambda: got.setdefault("log", open_()), daemon=True)
    thread.start()
    thread.join(5)
    if thread.is_alive():
        os.close(os.open(where, os.O_RDONLY | os.O_NONBLOCK))
        thread.join(5)
        pytest.fail("the build log's open waited on a FIFO for a reader")
    return got["log"]


@pytest.mark.parametrize("plant", ["symlink", "fifo"])
def test_a_log_that_isnt_a_plain_file_is_never_written_and_the_build_is_shown(
    tmp_path, capsys, plant
):
    where = buildlog.build_log_path("box-build")
    where.parent.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.write_text("keep me\n")
    if plant == "symlink":
        where.symlink_to(target)
    else:
        os.mkfifo(where)
    log = _returns_at_once(lambda: buildlog.open_build_log("box-build"), where)
    log.line("STEP 1/1")
    log.said(0, "box image build")
    log.close()
    assert target.read_text() == "keep me\n"
    err = capsys.readouterr().err
    assert "⚠ can't write the build log" in err
    assert "STEP 1/1" in err  # shown instead
    assert "done (log:" not in err


def test_a_pipe_hands_each_line_a_process_writes_to_the_log_cleaned():
    log = buildlog.open_build_log("build-acme")
    prog = (
        "import sys\n"
        "sys.stdout.buffer.write(b'out \\x1b[31mred\\x1b[0m\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.buffer.write(b'err \\x1b]0;title\\x07\\xff\\n')\n"
    )
    with log.pipe() as out:
        out.write("+ podman build .\n")  # foldyard's own echo keeps its place
        subprocess.run([sys.executable, "-c", prog], stdout=out, stderr=subprocess.STDOUT)
    log.close()
    assert [_STAMPED.sub("", line) for line in _lines("build-acme")] == [
        "+ podman build .",
        "out red",
        "err ]0;title\ufffd",
    ]


def test_a_quiet_build_says_it_is_still_going(capsys):
    log = buildlog.open_build_log("box-build")
    import threading

    beats = threading.Event()
    with buildlog.build_heartbeat("box image build", log, every=0.02):
        log.line("x" * 2000)
        beats.wait(0.3)
    log.close()
    err = capsys.readouterr().err
    said = [line for line in err.splitlines() if "still running" in line]
    assert said and all(
        re.fullmatch(
            r"… the box image build is still running — \d+:\d\d elapsed \((log \+\d+ (B|KB)|no new output)\)",
            s,
        )
        for s in said
    ), said
    assert any("no new output" in s for s in said)


def test_run_logs_the_command_then_the_build_and_says_how_it_ended(capsys):
    def build(line):
        line("STEP 1/1: RUN false")
        return 3

    assert buildlog.run_build_logged("box-build", "box image build", "+ podman build .", build) == 3
    assert [_STAMPED.sub("", line) for line in _lines("box-build")] == [
        "+ podman build .",
        "STEP 1/1: RUN false",
    ]
    assert "✗ box image build failed (exit 3)" in capsys.readouterr().err


def test_only_a_regular_file_is_ever_emptied_for_a_log(monkeypatch, tmp_path):
    # A device (or a FIFO) where the log goes is refused before anything truncates it, whatever
    # the platform's ftruncate would make of it (macOS truncates /dev/null without a word).
    truncated: list[int] = []
    monkeypatch.setattr(buildlog.os, "ftruncate", lambda fd, n: truncated.append(fd))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # so a writer's open succeeds
    try:
        for where in (Path("/dev/null"), fifo):
            with pytest.raises(OSError, match="isn't a regular file"):
                buildlog._open_build_log_file(where)
    finally:
        os.close(reader)
    assert truncated == []


class _Full:
    """A log file whose disk filled up: every write from now on fails as ENOSPC does."""

    def __init__(self, real):
        self.real = real

    def write(self, data):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_a_log_that_stops_taking_lines_hands_the_rest_to_your_terminal_and_says_so(
    capsys, monkeypatch
):
    log = buildlog.open_build_log("box-build")
    log.line("before the disk filled")
    monkeypatch.setattr(log, "_file", _Full(log._file))
    log.line("STEP 2/3: \x1b[31mafter")
    log.line("STEP 3/3: after")
    log.said(1, "box image build")
    log.close()
    err = capsys.readouterr().err
    assert err.count("⚠ the build log") == 1 and os.strerror(errno.ENOSPC) in err
    assert "STEP 2/3: after\n" in err and "STEP 3/3: after\n" in err  # none of it lost
    assert "✗ box image build failed (exit 1): its output since then is above" in err
    assert f"the log up to then: cat {buildlog.build_log_path('box-build')}" in err
    assert "log lines:" not in err  # no tail from a log that stopped
    assert "before the disk filled" in buildlog.build_log_path("box-build").read_text()


def test_a_failed_build_that_printed_nothing_says_so(capsys):
    log = buildlog.open_build_log("box-build")
    log.said(1, "box image build")
    log.close()
    err = capsys.readouterr().err
    assert "✗ box image build failed (exit 1), and printed nothing" in err
    assert "log lines:" not in err


def test_the_tail_is_whole_lines_however_long_they_are(capsys):
    log = buildlog.open_build_log("box-build")
    for i in range(40):
        log.line(f"{i:02d} " + "x" * 60_000)
    log.said(1, "box image build")
    log.close()
    shown = [line for line in capsys.readouterr().err.splitlines() if line.endswith("x" * 100)]
    assert len(shown) == buildlog.BUILD_TAIL_LINES
    assert shown[0].startswith("  15 ") and all(len(line) == 60_005 for line in shown)


def test_when_your_terminal_goes_away_a_live_build_carries_on_into_its_log(monkeypatch):
    from foldyard import guestcompose

    monkeypatch.setattr(buildlog, "_builds_live", True)
    log = buildlog.open_build_log("box-build")

    def gone(text, to="stderr"):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    monkeypatch.setattr(guestcompose, "show", gone)
    log.line("STEP 1/2")
    log.line("STEP 2/2")  # no raise: a build isn't killed because `| head` stopped reading
    log.close()
    logged = buildlog.build_log_path("box-build").read_text()
    assert "STEP 1/2" in logged and "STEP 2/2" in logged
    assert "your terminal stopped taking the build's output" in logged


def test_a_failed_builds_header_reaches_you_inert(capsys):
    # The stack's header names the services that failed: the render's, the box's to choose.
    log = buildlog.open_build_log("build-p")
    log.failed(f"✗ build failed for {_HOSTILE} (exit 1)")
    log.close()
    err = capsys.readouterr().err
    assert "build failed for" in err and "\x1b" not in err and "\x9b" not in err
