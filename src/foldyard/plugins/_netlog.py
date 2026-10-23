"""The Network Log's index over the egress log: every host in the RETAINED history (the live
``egress.jsonl`` + its dated backups, ``config.rotated_logs``), with whole-history tallies per host
and only its newest rows kept for display.

The panel repaints every second and the retained history runs to ~30 MB (``PROXY_LOG_MAX_BYTES`` ×
the live file + ``PROXY_LOG_BACKUPS``), so the index is built once and then fed only the bytes
appended since. Files are tracked by identity (device + inode), never by name: the proxy rotates by
RENAMING the live file to a backup, so the rows already read stay counted once and the renamed
file's unread tail is read under its new name. A file whose first bytes no longer match what was
read (truncated, or an inode reused) starts over; a file that's gone (a pruned backup) drops out.

Stdlib only — the registry imports ``proxy`` on the hot path, which imports this.
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..attribution import origin_key

KEEP = 50  # a host's newest rows kept for display; its tallies count everything
KEEP_BLOCKED = 20  # older refusals kept beyond those — a refusal is the row the operator acts on
KEEP_ORIGINS = 64  # who used a host (ADR-0034): the most recent this many stamps, per host and file
_HEAD = 64  # how much of a file's start identifies its content (see _File.head)


def _read_from(f, at: int) -> bytes:
    """``f``'s bytes from ``at`` to its end (a seam: the tests count the reads)."""
    f.seek(at)
    return f.read()


@dataclass
class _Host:
    """One host's rows within ONE file."""

    requests: int = 0
    injected: int = 0
    blocked: int = 0
    refused: int = 0  # the blocked rows the WALL refused — a grant's to answer, unlike a mismatch
    would_block: int = 0
    held: int = 0
    errors: int = 0  # 4xx/5xx the proxy didn't answer itself (a refusal or a held dummy is not one)
    last_ts: str = ""
    recent: deque[dict] = field(default_factory=lambda: deque(maxlen=KEEP))  # oldest→newest
    refusals: deque[dict] = field(default_factory=lambda: deque(maxlen=KEEP_BLOCKED))
    # who used it: a row's stamp identity (attribution.origin_key) → [rows, last ts, refused rows]
    origins: dict[str, list] = field(default_factory=dict)

    def add(self, e: dict) -> None:
        self.requests += 1
        self.injected += bool(e.get("injected"))
        self.would_block += bool(e.get("would_block"))
        self.held += bool(e.get("held"))
        if e.get("blocked"):
            self.blocked += 1
            self.refused += not e.get("mismatch")
            self.refusals.append(e)
        elif not e.get("held") and _status(e) >= 400:
            self.errors += 1
        self.last_ts = max(self.last_ts, str(e.get("ts", "")))
        self.recent.append(e)
        key = origin_key(e.get("origin"))
        if key is not None:
            seen = self.origins.setdefault(key, [0, "", 0])
            seen[0] += 1
            seen[1] = max(seen[1], str(e.get("ts", "")))
            seen[2] += bool(e.get("blocked") and not e.get("mismatch"))
            if len(self.origins) > KEEP_ORIGINS:  # the box decides how many it makes up
                del self.origins[min(self.origins, key=lambda k: self.origins[k][1])]


def _status(e: dict) -> int:
    status = e.get("status", 0)
    return status if isinstance(status, int) else 0


@dataclass
class _File:
    """What has been read of one log file: up to ``offset`` (always just past a newline)."""

    head: bytes = b""  # its first bytes as read — a mismatch means it's another file now
    offset: int = 0
    first_ts: str = ""
    hosts: dict[str, _Host] = field(default_factory=dict)

    def feed(self, data: bytes) -> None:
        for line in data.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            host = e.get("host") or "—"
            self.hosts.setdefault(str(host), _Host()).add(e)
            if not self.first_ts and e.get("ts"):
                self.first_ts = str(e["ts"])


@dataclass(frozen=True)
class HostView:
    """One host across every retained file, for the panel to render."""

    host: str
    requests: int
    injected: int
    blocked: int
    refused: int
    would_block: int
    held: int
    errors: int
    last_ts: str
    rows: list[dict]  # newest first: the newest KEEP, then older refusals not among them
    # who used it (ADR-0034): stamp identity → (rows, last ts, refused rows), across every file
    origins: dict[str, tuple[int, str, int]] = field(default_factory=dict)

    @property
    def earlier(self) -> int:
        """How many of its requests aren't in :attr:`rows`."""
        return self.requests - len(self.rows)


@dataclass(frozen=True)
class Snapshot:
    hosts: list[HostView]  # unordered — the panel orders them
    requests: int
    injected: int
    since: str  # the oldest retained row's timestamp, '' when there are none


class NetworkIndex:
    """The incremental index over one egress log (see the module docstring)."""

    def __init__(self, log: Path) -> None:
        self.log = log
        self._files: dict[tuple[int, int], _File] = {}

    def refresh(self) -> Snapshot:
        order: list[_File] = []  # oldest→newest, as the files are named
        for path in [*config.rotated_logs(self.log), self.log]:
            seen = self._read(path)
            # A rename between listing and opening could show one file under two names.
            if seen is not None and all(seen is not o for o in order):
                order.append(seen)
        live = {id(f) for f in order}
        self._files = {k: f for k, f in self._files.items() if id(f) in live}
        return _merge(order)

    def _read(self, path: Path) -> _File | None:
        try:
            with path.open("rb") as f:
                st = os.fstat(f.fileno())
                key = (st.st_dev, st.st_ino)
                seen = self._files.get(key)
                if seen is not None and (st.st_size < seen.offset or _head(f, seen) != seen.head):
                    seen = None  # truncated or replaced: not the file we read
                if seen is None:
                    seen = self._files[key] = _File()
                if st.st_size > seen.offset:
                    data = _read_from(f, seen.offset)
                    whole = data[: data.rfind(b"\n") + 1]  # a half-written last line waits
                    if seen.offset == 0:
                        seen.head = whole[:_HEAD]
                    seen.offset += len(whole)
                    seen.feed(whole)
                return seen
        except OSError:
            return None


def _head(f, seen: _File) -> bytes:
    f.seek(0)
    return f.read(len(seen.head))


def _merge(files: list[_File]) -> Snapshot:
    """Every file's hosts as one view per host. Rows are taken newest file first, so a host's
    newest rows win the :data:`KEEP` slots wherever the rotation split them."""
    newest_first = list(reversed(files))
    names = {h for f in files for h in f.hosts}
    views = []
    for name in names:
        parts = [f.hosts[name] for f in newest_first if name in f.hosts]
        rows = [e for p in parts for e in reversed(p.recent)][:KEEP]
        shown = {id(e) for e in rows}
        refusals = [e for p in parts for e in reversed(p.refusals) if id(e) not in shown]
        views.append(
            HostView(
                host=name,
                requests=sum(p.requests for p in parts),
                injected=sum(p.injected for p in parts),
                blocked=sum(p.blocked for p in parts),
                refused=sum(p.refused for p in parts),
                would_block=sum(p.would_block for p in parts),
                held=sum(p.held for p in parts),
                errors=sum(p.errors for p in parts),
                last_ts=max(p.last_ts for p in parts),
                rows=rows + refusals[:KEEP_BLOCKED],
                origins=_merge_origins(parts),
            )
        )
    return Snapshot(
        hosts=views,
        requests=sum(v.requests for v in views),
        injected=sum(v.injected for v in views),
        since=min((f.first_ts for f in files if f.first_ts), default=""),
    )


def _merge_origins(parts: list[_Host]) -> dict[str, tuple[int, str, int]]:
    out: dict[str, tuple[int, str, int]] = {}
    for p in parts:
        for key, (rows, last, refused) in p.origins.items():
            had = out.get(key, (0, "", 0))
            out[key] = (had[0] + rows, max(had[1], last), had[2] + refused)
    return out
