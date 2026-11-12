"""`fy paste` — an image from your computer into a dev box, where an agent can read it.

Claude Code reads a pasted image from the clipboard, and the box has no clipboard of yours. So this
verb, run on your computer, takes the image (the clipboard's, or a file you name), checks that it IS
an image and not too large, and streams it into the box over the engine socket
(``<engine> exec -i <box> sh -c 'cat > …'``) under :data:`IN_BOX_DIR`. Then it hands you the path:
typed into an agent's tmux session as a bracketed paste (``--session``), else copied to your
clipboard so you paste it yourself.

Why not the to-box (ADR-0033): it is Lima-only, and every box of the project reads all of it. A
screenshot can show anything that was on your screen, so it goes to the one box you named, nowhere
else, and lives in its ``/tmp`` — gone with the box. docs/fleet-scope.md's image relay is the same
transfer, run by the hosted relay instead of a CLI.

**A file argument is read without following a link the box could have made.** The path may name a
file in the checkout, which the box writes as you: a symlink planted there would send whatever it
points at (a key, or one of your other images). So it is opened by ``monitorsink``'s walk from
``/``, which follows only links root owns in directories you can't write (macOS's ``/tmp``), and
refuses a FIFO or any other non-regular file. Then only bytes that start like PNG, JPEG, GIF or
WebP are sent, and nothing past :data:`MAX_BYTES`.

**Every pipe is bounded** (:func:`_bounded`). The box controls the ``sh`` and ``cat`` the write
runs, so its output is drained and only a short prefix kept; a clipboard reader is stopped once it
passes the limit, rather than read in full and refused after. Nothing the box says comes back to
your computer except the exit status and that prefix, cut to printable characters.

The clipboard readers are found by what's installed, never by platform: ``osascript`` (macOS, via
AppKit's pasteboard — no extra install), then ``wl-paste`` under Wayland, then ``xclip`` under X11.
Stdlib-only; imported lazily by the CLI.
"""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from . import config

IN_BOX_DIR = "/tmp/fy-paste"
MAX_BYTES = 20 * 1024 * 1024  # a 5K-display screenshot as PNG is ~10 MB
_CLIPBOARD_TIMEOUT = 15
_EXEC_TIMEOUT = 60
_ERR_KEEP = 4096  # what is kept of a child's stderr (the box's, for the write)
_LIST_LIMIT = 64 * 1024  # a clipboard's list of types
# The macOS reader prints the image as base64: 4 characters per 3 bytes, plus a newline.
_B64_LIMIT = 4 * ((MAX_BYTES + 2) // 3) + 2

# AppKit's pasteboard, read as PNG: a screenshot copied with ⌃⇧⌘4 is `public.png`; an image
# copied from most apps also offers `public.tiff`, converted here (NSBitmapImageFileTypePNG is 4).
# The result is base64 on stdout, empty when the clipboard holds no image.
_JXA = """
ObjC.import('AppKit');
const pb = $.NSPasteboard.generalPasteboard;
let d = pb.dataForType('public.png');
if (d.isNil()) {
  const t = pb.dataForType('public.tiff');
  if (!t.isNil()) {
    const rep = $.NSBitmapImageRep.imageRepWithData(t);
    if (!rep.isNil()) d = rep.representationUsingTypeProperties(4, $({}));
  }
}
(d.isNil() ? '' : d.base64EncodedStringWithOptions(0).js);
"""


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def image_kind(data: bytes) -> str | None:
    """The file extension for ``data``'s image format, from its leading bytes; None if it isn't one
    of the formats Claude reads (PNG, JPEG, GIF, WebP)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# ── where the image comes from ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Ran:
    rc: int | None  # None: killed — it ran past the timeout, or its stdout past the limit
    out: bytes
    err: bytes  # at most _ERR_KEEP bytes of it
    over: bool  # stdout went past ``out_limit``


def _bounded(
    argv: list[str],
    *,
    data: bytes | None = None,
    out_limit: int = 0,
    stop_over: bool = False,
    timeout: float,
    env: dict | None = None,
) -> _Ran | None:
    """Run ``argv`` with ``data`` on its stdin, keeping at most ``out_limit`` bytes of stdout and
    :data:`_ERR_KEEP` of stderr — both drained to the end either way, so the child never stalls
    on a full pipe — and killing it at ``timeout``, or once stdout passes the limit when
    ``stop_over``. None when it can't start. The write into the box runs here: the box controls
    the programs it runs, and must not be able to fill your computer's memory through their
    output."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except OSError:
        return None
    out, err = bytearray(), bytearray()
    over, killed = threading.Event(), threading.Event()

    def kill() -> None:
        killed.set()
        proc.kill()

    def drain(pipe, buf: bytearray, limit: int, stop: bool) -> None:
        while chunk := os.read(pipe.fileno(), 1 << 16):
            room = limit - len(buf)
            buf.extend(chunk[: max(room, 0)])
            if len(chunk) > room and pipe is proc.stdout:
                over.set()
                if stop:
                    kill()
                    return

    def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(data or b"")
            proc.stdin.close()
        except OSError:  # it stopped reading (exited, or was killed)
            pass

    threads = [
        threading.Thread(target=drain, args=(proc.stdout, out, out_limit, stop_over), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, err, _ERR_KEEP, False), daemon=True),
    ]
    if data is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    killer = threading.Timer(timeout, kill)
    killer.start()
    for t in threads:
        t.start()
    try:
        rc = proc.wait()
    finally:
        killer.cancel()
    for t in threads:
        t.join(5)
    return _Ran(None if killed.is_set() else rc, bytes(out), bytes(err), over.is_set())


class _TooLarge(Exception):
    """The clipboard's image is past :data:`MAX_BYTES`."""


def _capture(argv: list[str], limit: int) -> bytes | None:
    """``argv``'s stdout when it succeeds; :class:`_TooLarge` when it passes ``limit``."""
    ran = _bounded(argv, out_limit=limit, stop_over=True, timeout=_CLIPBOARD_TIMEOUT)
    if ran is not None and ran.over:
        raise _TooLarge
    return ran.out if ran is not None and ran.rc == 0 else None


def _offers_png(argv: list[str]) -> bool:
    try:
        listed = _capture(argv, _LIST_LIMIT)
    except _TooLarge:
        return False
    return listed is not None and b"image/png" in listed.split()


def _osascript() -> bytes | None:
    out = _capture(["osascript", "-l", "JavaScript", "-e", _JXA], _B64_LIMIT)
    if not out or not out.strip():
        return None
    try:
        return base64.b64decode(out.strip(), validate=True)
    except ValueError:
        return None


def _wl_paste() -> bytes | None:
    if not _offers_png(["wl-paste", "--list-types"]):
        return None
    return _capture(["wl-paste", "--no-newline", "--type", "image/png"], MAX_BYTES)


def _xclip() -> bytes | None:
    if not _offers_png(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]):
        return None
    return _capture(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], MAX_BYTES)


def _readers(env=os.environ) -> list[tuple[str, Callable[[], bytes | None]]]:
    """The clipboard readers this computer has, in the order they're tried."""
    found: list[tuple[str, Callable[[], bytes | None]]] = []
    if shutil.which("osascript"):
        found.append(("osascript", _osascript))
    if env.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        found.append(("wl-paste", _wl_paste))
    if env.get("DISPLAY") and shutil.which("xclip"):
        found.append(("xclip", _xclip))
    return found


def from_clipboard() -> tuple[bytes | None, str]:
    """``(the clipboard's image, "")`` or ``(None, why not)``."""
    readers = _readers()
    if not readers:
        return None, (
            "no way to read the clipboard here (osascript on macOS, wl-paste under Wayland, xclip "
            "under X11) — save the image and run `fy paste <file>`"
        )
    for _, read in readers:
        try:
            data = read()
        except _TooLarge:
            return None, f"the clipboard's image is too large: over {MAX_BYTES // (1024 * 1024)} MB"
        if data:
            return data, ""
    return None, "the clipboard holds no image — copy one (or a screenshot), or name a file"


def from_file(path: str) -> tuple[bytes | None, str]:
    """``(the file's bytes, "")`` or ``(None, why not)``. A regular file, reached without following
    a link the box could have made (module doc), and no more than :data:`MAX_BYTES` + 1 bytes read
    (so an oversized one is reported, not loaded)."""
    from . import monitorsink

    full = os.path.abspath(path)
    if not os.path.lexists(full):
        return None, f"can't open {path}: no such file"
    try:
        fd = monitorsink.open_for_reading(full)
    except monitorsink.Refused as e:
        return None, f"won't read {e}"
    try:
        chunks, left = [], MAX_BYTES + 1
        while left > 0 and (chunk := os.read(fd, min(left, 1 << 20))):
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks), ""
    finally:
        os.close(fd)


def check(data: bytes) -> tuple[str | None, str]:
    """``(extension, "")`` for an image this verb will send, else ``(None, why not)``."""
    if len(data) > MAX_BYTES:
        return None, f"too large: over {MAX_BYTES // (1024 * 1024)} MB"
    kind = image_kind(data)
    if kind is None:
        return None, "not a PNG, JPEG, GIF or WebP image — only those are sent into the box"
    return kind, ""


# ── into the box ────────────────────────────────────────────────────────────────────────────────


def box_path(kind: str, now: datetime | None = None) -> str:
    """A fresh path under :data:`IN_BOX_DIR`: time-ordered, unique, only safe characters."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{IN_BOX_DIR}/{stamp}-{secrets.token_hex(3)}.{kind}"


# `$1` is the path, never interpolated into the script. Written beside the target and renamed, so
# an agent never reads half an image.
_WRITE = 'umask 077 && mkdir -p "${1%/*}" && cat > "$1.part" && mv -f "$1.part" "$1"'


def write_argv(engine: str, box: str, path: str) -> list[str]:
    return [engine, "exec", "-i", box, "sh", "-c", _WRITE, "sh", path]


def paste_argv(session: str, text: str) -> list[str]:
    """Type ``text`` into ``session``'s pane as ONE bracketed paste (``-p``) — what a terminal sends
    for a dragged-in file, which Claude Code turns into an attached image — then drop the buffer
    (``-d``). Nothing is pressed after it: you send the message."""
    from . import agents

    buf = "fy-paste"
    return agents._tmux(
        *agents._chain(
            ["set-buffer", "-b", buf, text],
            ["paste-buffer", "-p", "-d", "-b", buf, "-t", f"={session}:"],
        )
    )


def _copy_argv(env=os.environ) -> list[str] | None:
    """How to put text on this computer's clipboard, if it can."""
    if shutil.which("pbcopy"):
        return ["pbcopy"]
    if env.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"]
    if env.get("DISPLAY") and shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-i"]
    return None


def _copy(text: str) -> bool:
    argv = _copy_argv()
    if argv is None:
        return False
    # No pipe but stdin: xclip and wl-copy fork a child that keeps serving the clipboard, holding
    # any stdout/stderr pipe open — so a capture would wait out the timeout, then call it failed.
    try:
        out = subprocess.run(
            argv,
            input=text,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


def _target(session: str):
    """``(the box, the session's tmux name or "")`` to paste into, or ``(None, "")`` after saying
    why. With ``session``: the box holding that `fy agent` session, among this project's boxes.
    Without: the active worktree's running box."""
    from . import agents, box, stack

    wt = config.active_worktree()
    if not stack.engine_reachable("paste into", box._start_hint(wt)):
        return None, ""
    if session:
        boxes, reason = agents.project_boxes()
        found = agents._one([s for b in boxes for s in agents.sessions_in(b)], session)
        if found is None:
            if reason:
                _err(f"({reason})")
            _err(f"✗ no agent session {session!r} in this project — `fy agent ls` lists them.")
            return None, ""
        return next(b for b in boxes if b.name == found.box), found.name
    ctx = box._ctx()
    name, _ = box._names(ctx)
    engine = config.engine()
    if not box._running(engine, name, ctx.env):
        _err(f"✗ dev box {name} not running — {box._start_it(ctx.worktree)}")
        return None, ""
    return agents.Box(name, wt, engine, ctx.env), ""


def paste(file: str = "", *, session: str = "", copy: bool = True) -> int:
    from .agents import _printable

    if config.in_box():
        _err("✗ run `fy paste` on your computer — the clipboard is there, and the box has none.")
        return 1
    data, why = from_file(file) if file else from_clipboard()
    if data is None:
        _err(f"✗ {why}")
        return 1
    kind, why = check(data)
    if kind is None:
        _err(f"✗ {file or 'the clipboard'}: {why}")
        return 1
    target, pane = _target(session)
    if target is None:
        return 1
    path = box_path(kind)
    ran = _bounded(
        write_argv(target.engine or config.engine(), target.name, path),
        data=data,
        timeout=_EXEC_TIMEOUT,
        env=target.env,
    )
    if ran is None or ran.rc != 0:
        detail = (
            "couldn't run the engine"
            if ran is None
            else f"timed out after {_EXEC_TIMEOUT}s"
            if ran.rc is None
            else _printable(ran.err.decode(errors="replace").strip(), 200)
        )
        _err(f"✗ couldn't write into {target.name}: {detail}")
        return 1
    size = f"{len(data) / 1024:.0f} KB"
    if pane:
        try:
            typed = target.run(paste_argv(pane, path))
            why = "" if typed.returncode == 0 else _printable(typed.stderr.strip(), 200)
        except (OSError, subprocess.TimeoutExpired) as e:
            why = _printable(str(e), 200)
        if why:
            _err(f"✓ {path} in {target.name} ({size}), but typing it into {pane} failed: {why}")
            _err("  give the agent that path")
            return 1
        print(f"✓ {path} in {target.name} ({size}) — typed into {pane}; send it when ready")
        return 0
    print(f"✓ {path} in {target.name} ({size})")
    if copy and _copy(path):
        print("  path copied — paste it into the agent in the box")
    else:
        print("  give the agent that path")
    return 0
