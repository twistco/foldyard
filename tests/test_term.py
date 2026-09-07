"""term.py — the glyph-colorizing stdout/stderr wrapper installed by the CLI entry point.
Everything here runs against fake streams; the real terminal is never touched."""

from __future__ import annotations

import io
import sys

import pytest

from foldyard import term


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _color_friendly_env(monkeypatch):
    """A TTY-ish environment regardless of where the suite runs (CI exports TERM=dumb and
    sometimes NO_COLOR; both must not leak into the wrap/no-wrap assertions)."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")


def test_paint_colors_each_status_token():
    assert term.paint("✓ done") == "\x1b[32m✓\x1b[0m done"
    assert term.paint("✗ broke") == "\x1b[31m✗\x1b[0m broke"
    assert term.paint("▶ building…") == "\x1b[34m▶\x1b[0m building…"
    # ⚠ and the shouty status word next to it both go red; mid-line tokens count too.
    assert term.paint("gcp sa   ⚠ DEGRADED — lapse") == (
        "gcp sa   \x1b[31m⚠\x1b[0m \x1b[31mDEGRADED\x1b[0m — lapse"
    )
    assert term.paint("plain text") == "plain text"


def test_colorstream_writes_painted_but_reports_original_length():
    raw = _FakeTTY()
    stream = term.ColorStream(raw)
    msg = "✓ stack up\n"
    assert stream.write(msg) == len(msg)  # a well-behaved TextIO write() contract
    assert raw.getvalue() == "\x1b[32m✓\x1b[0m stack up\n"


def test_colorstream_passes_escape_laden_chunks_untouched():
    # A chunk already carrying ESC is someone else's rendering (Textual frames, a child's
    # colored output echoed through) — rewriting inside it could corrupt the sequences.
    raw = _FakeTTY()
    term.ColorStream(raw).write("\x1b[2J✓ inside a frame")
    assert raw.getvalue() == "\x1b[2J✓ inside a frame"


def test_colorstream_delegates_everything_but_write():
    raw = _FakeTTY()
    stream = term.ColorStream(raw)
    assert stream.isatty() is True
    stream.flush()  # delegated, no error


def test_install_wraps_ttys_only_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _FakeTTY())
    monkeypatch.setattr(sys, "stderr", io.StringIO())  # not a tty (a pipe/redirect)
    term.install()
    assert isinstance(sys.stdout, term.ColorStream)
    assert not isinstance(sys.stderr, term.ColorStream)  # per-stream gating
    wrapped = sys.stdout
    term.install()
    assert sys.stdout is wrapped  # no double wrap
    term.uninstall()
    assert not isinstance(sys.stdout, term.ColorStream)


@pytest.mark.parametrize("env", [{"NO_COLOR": "1"}, {"TERM": "dumb"}])
def test_install_honours_no_color_and_dumb_terminals(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "stdout", _FakeTTY())
    term.install()
    assert not isinstance(sys.stdout, term.ColorStream)


DIFF = """--- adopted/foldyard.toml
+++ tree/foldyard.toml
@@ -2,4 +2,4 @@
 [proxy]
-passthrough = ["@all"]
+passthrough = ["@all", "collector.example"]
 default_deny = true"""


def test_paint_diff_colors_by_line_role():
    painted = term.paint_diff(DIFF).splitlines()

    assert painted[0] == "\x1b[2m--- adopted/foldyard.toml\x1b[0m"  # file headers dim…
    assert painted[1] == "\x1b[2m+++ tree/foldyard.toml\x1b[0m"  # …NOT read as a +/- line
    assert painted[2] == "\x1b[36m@@ -2,4 +2,4 @@\x1b[0m"
    assert painted[3] == " [proxy]"  # context untouched
    assert painted[4].startswith("\x1b[31m-passthrough")
    assert painted[5].startswith("\x1b[32m+passthrough")


def test_paint_diff_leaves_prose_alone():
    """It runs over whole echoed bodies (the adopt prompt), not just the hunks."""
    assert term.paint_diff("nothing to see") == "nothing to see"


def test_color_enabled_follows_the_same_rules_as_install(monkeypatch):
    assert term.color_enabled(_FakeTTY()) is True
    assert term.color_enabled(io.StringIO()) is False  # not a TTY (piped, captured, redirected)

    monkeypatch.setenv("NO_COLOR", "1")
    assert term.color_enabled(_FakeTTY()) is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert term.color_enabled(_FakeTTY()) is False
