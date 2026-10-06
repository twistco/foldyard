"""Your computer's clock settings, read on the host so the dev box shows times the same way.

The box shares the VM kernel's clock, so its time is already right; what differs is the ZONE and
the FORMAT. Two facts carry them, both passed at box creation (``box._clock_env``):

- the IANA zone name (``TZ``) — summer time follows from the name, so it is read once per box;
- the time locale (``LC_TIME``) — what decides a 24-hour clock. Claude Code reads
  ``LC_ALL`` → ``LC_TIME`` → ``LANG`` for its own timestamps, and glibc tools read ``LC_TIME``;
  ``LANG``/``LC_ALL`` would also change messages and collation for every tool in the box.

Both are host facts, not repo config, so the adopted-config rule (ADR-0022) doesn't apply. Each
value ends up in a container argument, so each is checked against a strict pattern and dropped
(no variable passed) when it doesn't fit. Stdlib-only.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

# An IANA name: slash-separated components of letters, digits, `_`, `+`, `-` (`Etc/GMT+1`).
_ZONE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")
# A POSIX locale name: language[_territory][.codeset][@modifier] (`en_GB.UTF-8`, `es_419.UTF-8`).
_LOCALE = re.compile(
    r"[A-Za-z]{2,3}"  # language
    r"(?:_[A-Za-z]{2}|_[0-9]{3})?"  # territory
    r"(?:\.[A-Za-z0-9-]+)?"  # codeset
    r"(?:@[A-Za-z0-9]+)?"  # modifier
)
_ZONEINFO = "zoneinfo/"
# Alternative rule trees under zoneinfo/ that name the same zones (ICU knows only the bare name).
_RULE_TREES = ("posix/", "right/")


def zone(
    environ: Mapping[str, str] | None = None,
    localtime: Path = Path("/etc/localtime"),
    timezone_file: Path = Path("/etc/timezone"),
) -> str | None:
    """The IANA zone your computer shows times in, or None when nothing names one.

    An exported ``TZ`` first (what your own tools obey), then the ``/etc/localtime`` symlink —
    the part after ``zoneinfo/``, on macOS, Linux and WSL2 alike — then Debian's
    ``/etc/timezone``."""
    env = os.environ if environ is None else environ
    for name in (_from_tz(env.get("TZ", "")), _from_link(localtime), _from_file(timezone_file)):
        if name:
            return name
    return None


def time_locale(environ: Mapping[str, str] | None = None) -> str | None:
    """The locale that formats times on your computer — ``LC_ALL``, else ``LC_TIME``, else
    ``LANG``, the order both glibc and Claude Code use — or None for none, ``C``/``POSIX``
    (nothing to carry over) or a value outside the locale-name pattern."""
    env = os.environ if environ is None else environ
    name = env.get("LC_ALL") or env.get("LC_TIME") or env.get("LANG") or ""
    if name in ("C", "POSIX") or name.startswith("C."):
        return None
    return name if _LOCALE.fullmatch(name) else None


def locale_available(name: str, listed: list[str]) -> bool:
    """Whether ``name`` is one of the locales ``locale -a`` listed, comparing the codeset the way
    glibc does (``UTF-8`` and ``utf8`` are one codeset)."""
    want = _normalise(name)
    return any(_normalise(entry) == want for entry in listed)


def _normalise(name: str) -> tuple[str, str, str]:
    base, _, modifier = name.strip().partition("@")
    base, _, codeset = base.partition(".")
    return base, re.sub(r"[^a-z0-9]", "", codeset.lower()), modifier


def _valid_zone(name: str) -> str | None:
    for tree in _RULE_TREES:
        name = name.removeprefix(tree)
    return name if _ZONE.fullmatch(name) else None


def _from_tz(value: str) -> str | None:
    # A leading `:` is POSIX for "a zone file name"; anything else not shaped like a name (a
    # rule string such as `GMT0BST,M3.5.0/1,M10.5.0`) falls through to the system zone.
    return _valid_zone(value.removeprefix(":")) if value else None


def _from_link(localtime: Path) -> str | None:
    # The link as written first (macOS's names the zone before its versioned store), then the
    # fully resolved path for a chain whose first hop names no zoneinfo.
    try:
        targets = [os.readlink(localtime), str(localtime.resolve())]
    except OSError:
        return None
    for target in targets:
        _, sep, name = target.rpartition(_ZONEINFO)
        if sep and (valid := _valid_zone(name)):
            return valid
    return None


def _from_file(timezone_file: Path) -> str | None:
    try:
        text = timezone_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return _valid_zone(text)
