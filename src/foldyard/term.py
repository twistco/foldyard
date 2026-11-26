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

What a box's compose prints is never painted that way (the relay writes past the wrapper), and
its own colour codes are stripped: coloured text would let it draw a ✓ line that looks like
foldyard's. :func:`compose_line` gives it colour back from what the line says instead, from a
fixed set foldyard chooses: a colour per service prefix, the level words, a dimmed stamp.
"""

from __future__ import annotations

import os
import re
import sys
import zlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

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


def printable(text: str) -> str:
    """``text`` with whatever a terminal would act on shown as its escape (``\\x1b``, ``\\x07``…).

    For text a box can choose that foldyard prints on your computer — a ``foldyard.toml`` value,
    a key name: kept visible rather than dropped, since it is part of what was written, and inert.
    The same rule as ``monitorlog.printable``."""
    return "".join(c if c.isprintable() else ascii(c)[1:-1] for c in text)


def printable_lines(text: str) -> str:
    """:func:`printable` for text that runs over lines — a log tail, a command's output, a panel's
    markup: its line breaks and tabs are layout, so they stay; anything else is shown as its
    escape. Every escape ends in a letter or digit, so a markup string stays the markup it was."""
    return "".join(c if c.isprintable() or c in "\n\t" else ascii(c)[1:-1] for c in text)


def paint(text: str) -> str:
    """``text`` with every status token wrapped in its ANSI color (reset after each)."""
    return _TOKEN.sub(lambda m: f"\x1b[{_COLORS[m.group(0)]}m{m.group(0)}\x1b[0m", text)


_CYAN, _DIM = "36", "2"


_DIFF_ANSI = {"dim": _DIM, "cyan": _CYAN, "green": _GREEN, "red": _RED}

# What a line of a config diff IS (``configpin.diff_rows``): a file header, a hunk's table locator,
# an addition or a removal.
DiffKind = Literal["header", "hunk", "add", "remove"]
_DIFF_STYLES: dict[str, str] = {"header": "dim", "hunk": "cyan", "add": "green", "remove": "red"}


def diff_style(kind: DiffKind) -> str:
    """The style of one diff line's ``kind``: ``dim`` (file headers) · ``cyan`` (hunk locators) ·
    ``green`` (additions) · ``red`` (removals).

    By the kind the diff gave the line, never by its text: the text after a ``+``/``-`` is the
    box's to write, and a changed line whose text starts with ``--``/``++`` (a bare key like
    ``--x``, or ``++ tree/foldyard.toml`` in a multi-line string) looks like a file header once
    its sign is in front. Shared because the TUI renders the same diffs, and
    two renderers classifying lines differently is how one of them ends up painting an addition
    as a removal."""
    return _DIFF_STYLES[kind]


def paint_diff(rows: Iterable[tuple[DiffKind, str]], *, color: bool) -> str:
    """A config diff's ``(kind, text)`` rows as text, each line in the color every diff tool
    gives it (:func:`diff_style`) when ``color``, else exactly the plain text. Callers decide
    ``color`` at the print site (:func:`color_enabled`), where the stream is known."""
    if not color:
        return "\n".join(text for _kind, text in rows)
    return "\n".join(f"\x1b[{_DIFF_ANSI[diff_style(kind)]}m{text}\x1b[0m" for kind, text in rows)


_YELLOW = "33"

# A compose service's prefix colour, as compose gives each service one. Never red, green or
# yellow: those say broken, done and warning, in foldyard's own lines and in the level words.
_SERVICE_COLORS = ("36", "35", "34", "96", "95", "94")
_LEVEL_COLORS = {
    "ERROR": _RED,
    "FATAL": _RED,
    "CRITICAL": _RED,
    "WARNING": _YELLOW,
    "WARN": _YELLOW,
}
_LEVEL = re.compile(r"\b(?:ERROR|FATAL|CRITICAL|WARNING|WARN)\b")
# A compose log line's service prefix: podman-compose's `[web]   | `, compose's `web-1  | `. The
# name only from compose's own service-name alphabet.
_SERVICE = re.compile(r"(\[[A-Za-z0-9._-]+\]|[A-Za-z0-9][A-Za-z0-9._-]*)( *\|)( ?)")
# What `logs --timestamps` puts first: RFC 3339, to the nanosecond from podman, `Z` or an offset.
# A scaled service's lines name their container first (podman's 12-character short id).
_LOG_STAMP = re.compile(
    r"((?:[0-9a-f]{12} )?)(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d) "
)


def _sgr(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


def service_color(name: str) -> str:
    """The colour code of a compose service's prefix: chosen by a hash of its name (not Python's
    ``hash``, which changes per process), so a service keeps its colour from one run to the
    next."""
    return _SERVICE_COLORS[zlib.crc32(name.encode()) % len(_SERVICE_COLORS)]


def compose_line(
    text: str, *, terminal: bool, color: bool, stamps: bool = False, now: datetime | None = None
) -> str:
    """One line of what compose printed, already cleaned (``guestcompose.printable``: no control
    character is left), as foldyard shows it. Not to a ``terminal`` (a file, a pipe), exactly as
    compose printed it: ``fy logs > file`` keeps the whole stamp.

    ``stamps``: the line came from ``logs --timestamps``, so on a terminal the stamp after the
    service prefix (and a scaled service's container id) is moved in front of it, in your time
    (:func:`stamp`). With ``color`` (a terminal that wants it), foldyard adds its own
    colour from what the line says, never from what it carried: the stamp dimmed, the service
    prefix in its colour (:func:`service_color`), and each whole ``ERROR``/``FATAL``/``CRITICAL``
    word red, ``WARN``/``WARNING`` yellow. Each span is closed where it ends, and the ✓ ✗ ⚠ glyphs
    are never painted: a box line must not look like one of foldyard's own. Without ``color``,
    the same text with no escape at all."""
    if not terminal:
        return text
    rest = text
    name = bar = gap = ""
    if m := _SERVICE.match(text):
        name, bar, gap = m.groups()
        rest = text[m.end() :]
    when = ""
    if stamps and name and (s := _LOG_STAMP.match(rest)):
        when = stamp(s.group(2) + s.group(3).replace("Z", "+00:00"), now)
        rest = s.group(1) + rest[s.end() :]
    if not color:
        return (f"{when} " if when else "") + (f"{name}{bar}{gap}" if name else "") + rest
    painted = _LEVEL.sub(lambda w: _sgr(_LEVEL_COLORS[w.group(0)], w.group(0)), rest)
    if not name:
        return painted
    shown = _sgr(service_color(name.strip("[]")), name + bar) + gap + painted
    return f"{_sgr(_DIM, when)} {shown}" if when else shown


def is_terminal(stream=None) -> bool:
    """Is ``stream`` (default stdout) a real TTY, as opposed to a file or a pipe?"""
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def color_enabled(stream=None) -> bool:
    """Is color wanted on ``stream`` (default stdout)? The same gate :func:`install` applies —
    honour ``NO_COLOR``/``TERM=dumb`` and require a real TTY — exposed so a call site that renders
    its own color (the config diff) can't drift from the glyph painter's rules."""
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    return is_terminal(stream)


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
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        local = when.astimezone(now.tzinfo) if now is not None else when.astimezone()
    except (ValueError, OverflowError, TypeError):  # a host-written log, but never crash a panel
        return iso[11:19] if isinstance(iso, str) else ""
    # Today from the clock, never through ``local.tzinfo``: that is the offset the EVENT had, and
    # across a DST change it would put "now" an hour off — on yesterday, around midnight.
    today = now.date() if now is not None else datetime.now().date()
    fmt = "%H:%M:%S" if local.date() == today else "%Y-%m-%d %H:%M:%S"
    return local.strftime(fmt)
