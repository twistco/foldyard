"""Add a package to ``[box].packages`` in a checkout's own config — `fy agent start`'s offer when
the box's image has no tmux, never anything foldyard does unasked.

**Textual, and proven by re-parsing** (the stdlib's ``tomllib`` only reads). The file keeps its
comments and layout: the name is appended inside a one-line ``packages = [...]``, or such a line
goes under the ``[box]`` header, or a ``[box]`` table is appended; a multi-line array is refused.
The result is written only when it parses to exactly the old document with the package appended,
and the checkout's merged config then lists it; anything else is :class:`Refused` with the
reason, and the caller prints the line to add by hand.

**Which file.** ``foldyard.local.toml`` when it sets ``[box].packages`` (an array in the local file
replaces the shared one wholesale, so an edit to ``foldyard.toml`` would change nothing), else
``foldyard.toml``.

**The file is box-writable.** It is read with ``O_NOFOLLOW`` — a symlink planted in its place must
not copy a file of yours into the checkout — and written through :mod:`mountwrite` (a rename that
replaces a link rather than writing through it). Writing it changes nothing on your computer by
itself: the host runs the ADOPTED config (ADR-0022), so the change takes effect at the next
`fy config adopt`, which `fy box up` asks for.

Stdlib-only."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from pathlib import Path

from . import config, mountwrite

SHARED, LOCAL = "foldyard.toml", "foldyard.local.toml"
_MAX_BYTES = 1 << 20  # a config file, not a place to stream something large from

_BOX_HEADER = re.compile(r"^[ \t]*\[[ \t]*box[ \t]*\][ \t]*(#.*)?$", re.MULTILINE)
_ANY_HEADER = re.compile(r"^[ \t]*\[", re.MULTILINE)
_PACKAGES_KEY = re.compile(r"^[ \t]*packages[ \t]*=[ \t]*\[", re.MULTILINE)
# The whole array on its key's line, a comment after it allowed. Only that shape is edited: a
# multi-line array is refused with the line to add by hand (a `]` in a string or the comment
# that fools the match makes an edit the re-parse in :func:`add` refuses too).
_ONE_LINE = re.compile(r"[ \t]*packages[ \t]*=[ \t]*\[(?P<items>[^\n]*?)\][ \t]*(#[^\n]*)?$", re.M)


class Refused(Exception):
    """The edit can't be made exactly; the message says why."""


def _read(root: Path, name: str) -> str | None:
    """``root/name``'s text, ``None`` when it doesn't exist. Never follows a symlink, and reads only
    a regular file (a FIFO would hang the read)."""
    try:
        fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Refused(f"{name} can't be opened ({e.strerror or e}) — is it a symlink?") from e
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{name} isn't a regular file.")
        if st.st_size > _MAX_BYTES:
            raise Refused(f"{name} is larger than a config file should be.")
        data = fh.read(_MAX_BYTES + 1)
    try:
        return data.decode()
    except UnicodeDecodeError as e:
        raise Refused(f"{name} isn't UTF-8.") from e


def _parse(name: str, text: str | None) -> dict:
    if text is None:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise Refused(f"{name} doesn't parse ({e}).") from e


def _packages(doc: dict) -> list | None:
    box = doc.get("box")
    return box.get("packages") if isinstance(box, dict) else None


def listed(root: Path) -> list[str]:
    """The packages the checkout's own files declare (merged, as the box reads them); ``[]`` when
    they can't be read."""
    try:
        shared, local = _parse(SHARED, _read(root, SHARED)), _parse(LOCAL, _read(root, LOCAL))
    except Refused:
        return []
    found = _packages(config.merge_config(shared, local))
    return [p for p in found if isinstance(p, str)] if isinstance(found, list) else []


def _edited(text: str, package: str) -> str:
    """``text`` with ``package`` added to ``[box].packages`` — unproven; :func:`add` checks it."""
    item = f'"{package}"'
    header = _BOX_HEADER.search(text)
    if header:
        nxt = _ANY_HEADER.search(text, header.end())
        section_end = nxt.start() if nxt else len(text)
        key = _PACKAGES_KEY.search(text, header.end(), section_end)
        if key:
            line = _ONE_LINE.match(text, key.start(), section_end)
            if not line:
                raise Refused("[box].packages spans several lines.")
            items = line["items"].rstrip()
            at = line.start("items") + len(items)
            sep = "" if not items else " " if items.endswith(",") else ", "
            return text[:at] + sep + item + text[at:]
        line_end = text.find("\n", header.end())
        line_end = len(text) if line_end < 0 else line_end + 1
        newline = "" if text[:line_end].endswith("\n") else "\n"
        return text[:line_end] + newline + f"packages = [{item}]\n" + text[line_end:]
    tail = "" if not text or text.endswith("\n") else "\n"
    return text + tail + ("\n" if text else "") + f"[box]\npackages = [{item}]\n"


def _target(docs: dict[str, dict]) -> str:
    return LOCAL if _packages(docs[LOCAL]) is not None else SHARED


def target(root: Path) -> str:
    """The file :func:`add` would edit in ``root`` — for the question that asks first. The shared
    file when the local one can't be read: :func:`add` then refuses with the reason anyway."""
    try:
        return _target({name: _parse(name, _read(root, name)) for name in (SHARED, LOCAL)})
    except Refused:
        return SHARED


def add(root: Path, package: str) -> Path:
    """Add ``package`` to ``[box].packages`` in ``root``'s config; the file written. Raises
    :class:`Refused` when the edit can't be made exactly, leaving every file as it was."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._-]*", package):
        raise Refused(f"{package!r} isn't a package name.")
    texts = {name: _read(root, name) for name in (SHARED, LOCAL)}
    docs = {name: _parse(name, text) for name, text in texts.items()}
    name = _target(docs)
    old_doc, old_text = docs[name], texts[name] or ""
    current = _packages(old_doc)
    if current is not None and not isinstance(current, list):
        raise Refused(f"[box].packages in {name} isn't a list.")
    if package in (current or []):
        raise Refused(f"{name} already lists {package} in [box].packages.")

    new_text = _edited(old_text, package)
    try:
        new_doc = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise Refused(f"the edit wouldn't parse ({e}).") from e
    expected = dict(old_doc)
    expected["box"] = {**old_doc.get("box", {}), "packages": [*(current or []), package]}
    if new_doc != expected:
        raise Refused(f"{name} is laid out in a way the edit can't change exactly.")
    merged = config.merge_config(*(new_doc if n == name else docs[n] for n in (SHARED, LOCAL)))
    if package not in (_packages(merged) or []):
        raise Refused(f"the merged config still wouldn't list {package} (a `disabled` entry?).")
    mountwrite.write(root, name, new_text.encode())
    return root / name
