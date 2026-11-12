"""Terminal color for the CLI's glyph convention (✓ ✗ ▶ ⚠ ℹ ● ○).

foldyard's status output leads with a small set of glyphs, from a couple hundred print
sites across the CLI surface. Rather than threading a style helper through every one,
:func:`install` wraps ``sys.stdout``/``sys.stderr`` ONCE (the console-script entry point
calls it) with a stream that colors the glyphs as text passes through: green for done
(✓ ●), red for broken (✗ ⚠ ○, and the shouty DEGRADED/EMERGENCY words that ride next to
them), blue for progress/info (▶ ℹ).

Strictly cosmetic and strictly gated: only when the stream is a real TTY — so
``eval "$(fy shellenv)"`` (command substitution), piped output, the background
supervisor's redirected log, and pytest capture all stay byte-clean — honoring ``NO_COLOR``
and ``TERM=dumb``, and any chunk already carrying an escape sequence (the Textual TUI's
frames, a child's colored output echoed through) passes through untouched.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime

_GREEN, _RED, _BLUE = "32", "31", "34"

_COLORS = {
    "✓": _GREEN,
    "●": _GREEN,
    "✗": _RED,
    "⚠": _RED,
    "○": _RED,
    "▶": _BLUE,
    "ℹ": _BLUE,
    "DEGRADED": _RED,
    "EMERGENCY": _RED,
}

_TOKEN = re.compile("|".join(re.escape(t) for t in _COLORS))


def paint(text: str) -> str:
    """``text`` with every status token wrapped in its ANSI color (reset after each)."""
    return _TOKEN.sub(lambda m: f"\x1b[{_COLORS[m.group(0)]}m{m.group(0)}\x1b[0m", text)


_CYAN, _DIM = "36", "2"


_DIFF_ANSI = {"dim": _DIM, "cyan": _CYAN, "green": _GREEN, "red": _RED}


def diff_style(line: str) -> str:
    """One diff line's style name — ``dim`` (file headers) · ``cyan`` (hunk locators) ·
    ``green`` (additions) · ``red`` (removals) · ``""`` (anything else).

    LINE-scoped rather than another entry in :data:`_COLORS`: a ``-`` mid-sentence is a hyphen, and
    the ``---``/``+++`` headers start with the same characters as the lines they introduce (hence
    the header test first). Split out from :func:`paint_diff` because the TUI renders the same
    diffs as Rich markup, and two renderers classifying lines differently is how one of them ends
    up painting an addition as a removal."""
    if line.startswith(("---", "+++")):
        return "dim"
    if line.startswith("@@"):
        return "cyan"
    if line.startswith("+"):
        return "green"
    if line.startswith("-"):
        return "red"
    return ""


def paint_diff(text: str) -> str:
    """A unified diff, colored the way every diff tool colors one (:func:`diff_style`). Callers
    apply it at the print site, where the stream is known; the diff itself stays plain text so the
    gate's prompt and the tests compare byte-exact strings."""
    out = []
    for line in text.splitlines():
        style = diff_style(line)
        out.append(f"\x1b[{_DIFF_ANSI[style]}m{line}\x1b[0m" if style else line)
    return "\n".join(out)


def color_enabled(stream=None) -> bool:
    """Is color wanted on ``stream`` (default stdout)? The same gate :func:`install` applies —
    honour ``NO_COLOR``/``TERM=dumb`` and require a real TTY — exposed so a call site that renders
    its own color (the config diff) can't drift from the glyph painter's rules."""
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


class ColorStream:
    """A write-path-only wrapper: colors status tokens on the way through, delegates
    everything else (flush/fileno/isatty/encoding/buffer/…) to the wrapped stream — so
    subprocesses inheriting the fd and code probing the stream still see the real one."""

    def __init__(self, raw):
        self.raw = raw

    def write(self, s: str) -> int:
        # A chunk already carrying ESC is someone else's rendering — never rewrite inside it.
        self.raw.write(s if "\x1b" in s else paint(s))
        return len(s)

    def __getattr__(self, name):
        return getattr(self.raw, name)


def install() -> None:
    """Wrap stdout/stderr when they are real TTYs. Idempotent; a no-op under ``NO_COLOR``,
    ``TERM=dumb``, or redirection (each stream is gated on its OWN isatty)."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if not isinstance(stream, ColorStream) and color_enabled(stream):
            setattr(sys, name, ColorStream(stream))


def uninstall() -> None:
    """Restore the raw streams — the Textual TUI takes the terminal over wholesale, and its
    renderer must not run through a rewriting wrapper."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if isinstance(stream, ColorStream):
            setattr(sys, name, stream.raw)


def stamp(iso: str | None, now: datetime | None = None) -> str:
    """A log row's ISO timestamp as a panel shows it: in YOUR timezone (the logs write UTC), the
    time alone when it falls today, else the date too — ``14:03:07`` beside ``2026-11-10
    14:03:07``, so a row from yesterday never reads as this afternoon's. A stamp without a zone is
    read as UTC, which is what every foldyard log writes; one that doesn't parse keeps the old
    time-of-day slice. ``now`` (aware) is for tests: its zone stands in for the local one."""
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return iso[11:19]
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    local = when.astimezone(now.tzinfo) if now is not None else when.astimezone()
    today = (now or datetime.now(local.tzinfo)).astimezone(local.tzinfo).date()
    fmt = "%H:%M:%S" if local.date() == today else "%Y-%m-%d %H:%M:%S"
    return local.strftime(fmt)
