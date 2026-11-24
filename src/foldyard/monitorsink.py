"""monitorsink.py — the hooks' built-in file sink: each batch appended to a file as one signed,
chained JSON line, and the check that a file still holds exactly what was written
(docs/monitor-pipeline.md §6).

**Why a sink and not ``cat >> path``.** The file an experiment scores usually lives in the
checkout, so the box can read it — the point — but it can also write there. A shell append follows
symlinks: the box plants ``logs → ~`` or ``logs/events.jsonl → ~/.zshrc`` and the host appends
wherever it aimed, with content partly shaped by what ran in the box. So :func:`append` walks the
path itself, one component at a time from ``/``, never following a symlink that anything running as
the operator could have placed (see :func:`_open_parent`), and refuses a final component that is a
symlink, a hard link or anything but a regular file. And the box can edit or cut the file, so each
line carries an HMAC under a key only the host holds (:func:`ensure_key` — not the guest relay's
key, which the VM has), a per-hook sequence and the previous line's mac: :func:`verify` finds an
edited, added, removed, reordered or forged line, and a cut tail against the hook's recorded state.

Directories are never created: a missing one is most often a mistyped path, and evidence written
somewhere nobody looks is worse than a refusal the supervisor log and ``fy doctor`` report.
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import config

# A symlink on the way is followed only when this uid owns it AND this user can't write the
# directory holding it — nothing running as the operator (the box's writes reach the host as the
# operator) could have placed or swapped it. That admits the system's own links (macOS's
# `/tmp → private/tmp`, `/var`, `/etc`; Linux's `/var/run`) and nothing a box could plant. A test
# seam: unprivileged tests can't make root's links.
_TRUSTED_UID = 0
_MAX_HOPS = 32
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


CHUNK = 1 << 20  # read at a time
# The longest line the sink could write is far below this (500 events a batch): past it a line is
# not the sink's, and holding it would let whatever can write the file fill this computer's memory.
MAX_LINE = 256 << 20


class Refused(Exception):
    """The sink won't touch this path (a symlink, a missing directory, not a regular file)."""


class Unreadable(Exception):
    """What comes next in the file can't be a line of the sink's (:func:`lines`)."""


# ── the key ────────────────────────────────────────────────────────────────────────────


def key_file() -> Path:
    return config.state_dir() / "monitor-sink.key"


def load_key() -> bytes | None:
    """The sink's key, or None when there isn't one (nothing was ever signed). Never creates it."""
    try:
        key = bytes.fromhex(key_file().read_text().strip())
    except (OSError, ValueError):
        return None
    return key if len(key) == 32 else None


def ensure_key() -> bytes:
    """The key, made on first use: 32 random bytes, hex, 0600 from the create, host-only. A key
    already there is never replaced — every line signed with it would stop verifying."""
    key = load_key()
    if key is not None:
        return key
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(secrets.token_hex(32) + "\n")
    try:
        os.link(tmp, path)  # never over a key another writer made first
    except FileExistsError:
        pass
    finally:
        tmp.unlink()
    key = load_key()
    if key is None:
        raise Refused(f"{path} is not a sink key — move it aside and the next delivery makes one")
    return key


# ── the path walk ──────────────────────────────────────────────────────────────────────


def _parts(path: str) -> list[str]:
    return [c for c in path.split("/") if c not in ("", ".")]


def _follow(fd: int, comp: str, path: str) -> str:
    """Why ``comp`` (in the directory ``fd``) couldn't be opened as a directory without following
    a link: the target of a trusted link, else :class:`Refused` saying why."""
    try:
        st = os.stat(comp, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        raise Refused(
            f"{path}: '{comp}' doesn't exist — create the directory first (the sink never makes "
            "directories)"
        ) from None
    except OSError as e:
        raise Refused(f"{path}: can't reach '{comp}': {e.strerror}") from None
    if not stat.S_ISLNK(st.st_mode):
        raise Refused(f"{path}: '{comp}' is not a directory")
    if st.st_uid != _TRUSTED_UID or os.access(".", os.W_OK, dir_fd=fd):
        raise Refused(
            f"{path}: '{comp}' is a symlink that something running as you could have made — the "
            "box writes the checkout as you, so foldyard never follows one. Give the real path: "
            f"{os.path.realpath(path)}"
        )
    return os.readlink(comp, dir_fd=fd)


def _open_parent(path: str) -> tuple[int, str]:
    """The directory holding ``path``'s last component, opened by a walk from ``/`` that opens
    each component relative to the one before and never follows a symlink unless
    :func:`_follow` trusts it — and that last component's name.

    Not ``realpath`` then open: resolving first and opening later is a race the box wins by
    swapping a directory for a link in between, and resolving through a link the box planted
    before the hook was even added would aim the writes wherever it chose. Each step here holds
    the directory it is in, so what is checked is what is used."""
    if not path.startswith("/"):
        raise Refused(f"{path}: not an absolute path")
    *todo, name = _parts(path) or [""]
    if name in ("", ".."):
        raise Refused(f"{path}: not a file path")
    fd = os.open("/", _DIR)
    hops = 0
    try:
        while todo:
            comp = todo.pop(0)
            try:
                nxt = os.open(comp, _DIR, dir_fd=fd)
            except OSError:
                target = _follow(fd, comp, path)
                hops += 1
                if hops > _MAX_HOPS:
                    raise Refused(f"{path}: too many symlinks") from None
                if target.startswith("/"):
                    os.close(fd)
                    fd = os.open("/", _DIR)
                todo[:0] = _parts(target)
                continue
            os.close(fd)
            fd = nxt
        return fd, name
    except BaseException:
        os.close(fd)
        raise


def _open(path: str, flags: int, *, writing: bool) -> int:
    parent, name = _open_parent(path)
    try:
        fd = os.open(
            name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o644, dir_fd=parent
        )
    except OSError as e:
        try:
            link = stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode)
        except OSError:
            link = False
        if link or e.errno == errno.ELOOP:
            how = "writes" if writing else "reads"
            raise Refused(f"{path} is a symlink — foldyard never {how} through one") from None
        raise Refused(f"{path}: {e.strerror}") from None
    finally:
        os.close(parent)
    st = os.fstat(fd)
    why = (
        "is not a regular file"
        if not stat.S_ISREG(st.st_mode)
        else "has other hard links — the sink writes only a file of its own"
        if writing and st.st_nlink != 1
        else ""
    )
    if why:
        os.close(fd)
        raise Refused(f"{path} {why}")
    return fd


def check_path(path: str) -> None:
    """:class:`Refused` if the sink couldn't write ``path`` (``fy monitor hook add``'s preflight)
    — its directory unreachable without an untrusted link, or the file itself not one it would
    append to. Creates nothing."""
    parent, name = _open_parent(path)
    try:
        try:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise Refused(f"{path} is a symlink — the sink never writes through one")
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{path} is not a regular file")
        if st.st_nlink != 1:
            raise Refused(f"{path} has other hard links — the sink writes only a file of its own")
    finally:
        os.close(parent)


def append(path: str, line: bytes) -> None:
    """Append ``line`` to ``path`` (created 0644 if absent). One write call, repeated only for a
    short write. A write that fails part-way is UNDONE — the file is cut back to where it ended —
    so the sink never leaves a torn line: any shape :func:`verify` excused, the box could write.
    A file that doesn't end in a newline (something else wrote it) is ended first, so the sink's
    line stays a line of its own. A path ending in ``.gz`` gets each line as a gzip member of its
    own: the file is an ordinary gzip file, grown and undone a line at a time like any other."""
    packed = compressed(path)
    if packed:
        line = gzip.compress(line, 6, mtime=0)
    fd = _open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, writing=True)
    try:
        size = os.fstat(fd).st_size
        if not packed and size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line
        view = memoryview(line)
        try:
            while view:
                view = view[os.write(fd, view) :]
        except BaseException as e:
            try:
                os.ftruncate(fd, size)
            except OSError as cut:
                raise Refused(
                    f"{path}: a write failed part-way ({e}) and the part written couldn't be cut "
                    f"off ({cut.strerror}) — `fy monitor check` will report that line"
                ) from None
            raise
    finally:
        os.close(fd)


def open_for_reading(path: str) -> int:
    """``path`` (absolute) opened read-only by the same walk: a link anywhere on the way that
    something running as you could have made, or a FIFO or other non-regular file, is
    :class:`Refused`. For `fy paste`'s file argument, which may name a file in the box-writable
    checkout. The caller closes the descriptor."""
    return _open(path, os.O_RDONLY, writing=False)


def read(path: str) -> bytes:
    """``path``'s bytes, opened the same no-follow way (a planted link or FIFO is refused, not
    read or waited on)."""
    return b"".join(_chunks(path))


def _chunks(path: str) -> Iterator[bytes]:
    fd = _open(path, os.O_RDONLY, writing=False)
    try:
        while chunk := os.read(fd, CHUNK):
            yield chunk
    finally:
        os.close(fd)


def compressed(path: str) -> bool:
    """Whether the file's lines are gzip members: its name says so."""
    return path.endswith(".gz")


def lines(path: str) -> Iterator[bytes]:
    """``path``'s lines one at a time, newlines kept, opened the same no-follow way: never the
    whole file in memory (a 25 GB one took `check` some 50). A last line without a newline is a
    fragment. :class:`Unreadable` where what comes next can't be a line of the sink's."""
    return _members(_chunks(path)) if compressed(path) else _split(_chunks(path))


def _too_long() -> Unreadable:
    return Unreadable(f"longer than any line the sink writes ({MAX_LINE >> 20} MiB)")


def _split(chunks: Iterable[bytes]) -> Iterator[bytes]:
    parts: list[bytes] = []
    held = 0
    for chunk in chunks:
        *ended, rest = chunk.split(b"\n")
        for piece in ended:
            if held + len(piece) > MAX_LINE:
                raise _too_long()
            yield b"".join([*parts, piece, b"\n"])
            parts, held = [], 0
        parts.append(rest)
        held += len(rest)
        if held > MAX_LINE:
            raise _too_long()
    if fragment := b"".join(parts):
        yield fragment


def _members(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Each gzip member's one line. Stricter than a gzip reader, which skips padding and joins
    members: what it would read past, the sink didn't write."""
    source = iter(chunks)
    data = next(source, b"")
    while data:
        member = zlib.decompressobj(wbits=31)
        out: list[bytes] = []
        size = 0
        while not member.eof:
            if not data and not (data := next(source, b"")):
                raise Unreadable(
                    "cut off part-way through a gzip member — if the supervisor was writing just "
                    "then, check again"
                )
            try:
                piece = member.decompress(data, MAX_LINE + 1 - size)
            except zlib.error:
                raise Unreadable("not gzip the sink wrote") from None
            out.append(piece)
            size += len(piece)
            if size > MAX_LINE:
                raise _too_long()
            data = member.unused_data if member.eof else member.unconsumed_tail
        line = b"".join(out)
        if line.count(b"\n") != 1 or not line.endswith(b"\n"):
            raise Unreadable("a gzip member that isn't one line")
        yield line
        data = data or next(source, b"")


def size(path: str) -> int:
    """The file's size, 0 when there is none; never through a link."""
    parent, name = _open_parent(path)
    try:
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return 0
    finally:
        os.close(parent)
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{path} is not a regular file")
    return st.st_size


def exists(path: str) -> bool:
    """Whether anything is at ``path``, a dangling link included."""
    parent, name = _open_parent(path)
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    finally:
        os.close(parent)
    return True


def move(path: str, name: str) -> None:
    """Rename the sink's file to ``name`` in its own directory, never over anything there."""
    if "/" in name or name in ("", ".", ".."):
        raise Refused(f"{name!r} is not a file name")
    check_path(path)
    if exists(os.path.join(os.path.dirname(path), name)):
        raise Refused(f"{os.path.dirname(path)}/{name} already exists")
    parent, old = _open_parent(path)
    try:
        os.rename(old, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        os.close(parent)


# ── the signed line ────────────────────────────────────────────────────────────────────


def _canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def _head(seq: int, prev: str) -> bytes:
    return b'{"sink":{"seq":%d,"prev":"%s","mac":"' % (seq, prev.encode())


def signed_line(key: bytes, body: dict, seq: int, prev: str) -> tuple[bytes, str]:
    """``body`` as one line, with ``"sink": {"seq", "prev", "mac"}``, and its mac: HMAC-SHA256
    over the canonical JSON (sorted keys, no spaces) of the line without ``mac``. The sink object
    is written first, so the line leads with the sequence it is. Deterministic: :func:`verify`
    rebuilds it and holds the file's line to these exact bytes."""
    if not body or "sink" in body:
        raise ValueError("a sink line needs a body without its own `sink`")
    mac = hmac.new(key, _canonical({**body, "sink": {"seq": seq, "prev": prev}}), hashlib.sha256)
    digest = mac.hexdigest()
    return _head(seq, prev) + digest.encode() + b'"},' + _canonical(body)[1:] + b"\n", digest


_MAC = re.compile(r"[0-9a-f]{64}")


def _parse(raw: bytes) -> dict | None:
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):  # RecursionError: nesting deeper than the parser's stack
        return None
    sink = doc.get("sink") if isinstance(doc, dict) else None
    if not (
        isinstance(sink, dict)
        and type(sink.get("seq")) is int
        and isinstance(sink.get("prev"), str)
        and isinstance(sink.get("mac"), str)
        and _MAC.fullmatch(sink["mac"])  # else hmac.compare_digest raises on a non-ASCII one
    ):
        return None
    return doc


def _mac_of(key: bytes, doc: dict) -> str:
    sink = {k: v for k, v in doc["sink"].items() if k != "mac"}
    return hmac.new(key, _canonical({**doc, "sink": sink}), hashlib.sha256).hexdigest()


@dataclass
class Check:
    lines: int = 0
    events: int = 0
    first_seq: int = 1
    last_seq: int = 0
    problem: str = ""  # the first one; "" = the file holds what the sink wrote
    line: int = 0  # where it is (1-based; the last line for a problem with the tail)
    notes: list[str] = field(default_factory=list)


def verify(
    data: bytes | Iterable[bytes],
    key: bytes,
    hook: str,
    state: tuple[int, str] | None,
    start: tuple[int, str] = (0, ""),
) -> Check:
    """Does ``data`` (a file's bytes, or its :func:`lines`) hold exactly the lines the sink wrote
    for ``hook``? Every line's mac, the sequence with no gap, each line's ``prev`` naming the line
    before, and the end against ``state`` — the hook's last written ``(seq, mac)`` as the
    supervisor recorded it, which is what catches lines cut from the end. The chain starts after
    ``start``: the last ``(seq, mac)`` of the file before, for one rotation started.

    Every line must be the exact bytes :func:`signed_line` writes for its content: a line that
    parses to the same thing but reads differently (a duplicate key, which ``json.loads`` resolves
    last-wins and another reader may not) is not the sink's. Nothing unparseable is excused — the
    sink undoes a failed write (:func:`append`), so a fragment is never its own.

    At-least-once delivery leaves one shape that is NOT tampering, and passes with a note: a
    **re-sent seq** — a line written but not recorded before the supervisor stopped: the same seq
    again, chained from the same ``prev``, possibly with more events. The next line chains from the
    re-sent one, and so does the recorded state.
    """
    out = Check(first_seq=start[0] + 1)
    source = iter(_split([data]) if isinstance(data, bytes) else data)
    (seq, mac), parent = start, ""  # the chain's head: its seq, its mac, the `prev` it named
    n = 0  # lines read

    def fail(n: int, why: str) -> Check:
        out.problem, out.line = why, n
        return out

    while True:
        try:
            line = next(source)
        except StopIteration:
            break
        except Unreadable as e:
            return fail(n + 1, f"not a line the sink wrote ({e})")
        if not line.endswith(b"\n"):
            return fail(
                n + 1,
                "not a line the sink wrote (an unterminated fragment — if the supervisor was "
                "writing just then, check again)",
            )
        n += 1
        doc = _parse(line)
        if doc is None:
            return fail(n, "not a line the sink wrote (not JSON, or no `sink` object)")
        sink = doc["sink"]
        if not hmac.compare_digest(sink["mac"], _mac_of(key, doc)):
            return fail(
                n,
                "signature doesn't match — the line was altered, or not written by this "
                "computer's sink",
            )
        body = {k: v for k, v in doc.items() if k != "sink"}
        if not body or signed_line(key, body, sink["seq"], sink["prev"])[0] != line:
            return fail(
                n,
                "signed content, but not the bytes the sink wrote (reformatted, or a key given "
                "twice — a reader that takes the first would see something else)",
            )
        if doc.get("hook") != hook:
            return fail(n, f"belongs to hook '{doc.get('hook')}', not '{hook}'")
        if sink["seq"] == seq + 1 and sink["prev"] == mac:
            parent, seq, mac = mac, seq + 1, sink["mac"]
        elif seq > start[0] and sink["seq"] == seq and sink["prev"] == parent:
            out.notes.append(
                f"line {n}: seq {seq} re-sent (written, but not recorded before the supervisor "
                "stopped)"
            )
            mac = sink["mac"]
        elif sink["seq"] != seq + 1:
            return fail(
                n, f"seq {sink['seq']} where {seq + 1} was due — lines missing, moved or repeated"
            )
        else:
            return fail(n, f"seq {seq + 1} doesn't chain from the line before it (`prev`)")
        out.events += len(doc.get("events") or [])
    out.lines, out.last_seq = n, seq
    if state is None:
        return fail(
            out.lines,
            f"no delivery state for hook '{hook}' on this computer — can't confirm nothing was "
            "cut from the end",
        )
    want_seq, want_mac = state
    if seq < want_seq:
        return fail(
            out.lines,
            f"the file ends at seq {seq}, but the sink wrote up to {want_seq} — lines were cut "
            "from the end",
        )
    if seq == want_seq and seq and not hmac.compare_digest(mac, want_mac):
        return fail(out.lines, f"the last line isn't the one the sink wrote last (seq {seq})")
    if seq > want_seq:
        out.notes.append(
            f"seq {want_seq + 1}–{seq}: written, not yet recorded in the hook's state (it is at "
            "the end of the supervisor's round; one that stopped first re-sends it)"
        )
    return out
