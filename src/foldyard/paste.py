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

**The type check is also the guard.** A file argument is opened on your computer, and a path in the
checkout may be a symlink the box planted (at ``~/.ssh/id_ed25519``, say). Only bytes that start
like PNG, JPEG, GIF or WebP are sent, a non-regular file (a FIFO would block) is refused, and
nothing is sent past :data:`MAX_BYTES`. Nothing the box says comes back to your computer except the
exit status and the tmux error text, which is cut to printable characters.

The clipboard readers are found by what's installed, never by platform: ``osascript`` (macOS, via
AppKit's pasteboard — no extra install), then ``wl-paste`` under Wayland, then ``xclip`` under X11.
Stdlib-only; imported lazily by the CLI.
"""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime

from . import config

IN_BOX_DIR = "/tmp/fy-paste"
MAX_BYTES = 20 * 1024 * 1024  # a 5K-display screenshot as PNG is ~10 MB
_CLIPBOARD_TIMEOUT = 15
_EXEC_TIMEOUT = 60

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


def _run(argv: list[str], *, text: bool = False) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=text,
            timeout=_CLIPBOARD_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _osascript() -> bytes | None:
    out = _run(["osascript", "-l", "JavaScript", "-e", _JXA], text=True)
    if out is None or out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return base64.b64decode(out.stdout.strip(), validate=True)
    except ValueError:
        return None


def _wl_paste() -> bytes | None:
    types = _run(["wl-paste", "--list-types"], text=True)
    if types is None or types.returncode != 0 or "image/png" not in types.stdout.split():
        return None
    out = _run(["wl-paste", "--no-newline", "--type", "image/png"])
    return out.stdout if out is not None and out.returncode == 0 else None


def _xclip() -> bytes | None:
    targets = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"], text=True)
    if targets is None or targets.returncode != 0 or "image/png" not in targets.stdout.split():
        return None
    out = _run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
    return out.stdout if out is not None and out.returncode == 0 else None


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
        data = read()
        if data:
            return data, ""
    return None, "the clipboard holds no image — copy one (or a screenshot), or name a file"


def from_file(path: str) -> tuple[bytes | None, str]:
    """``(the file's bytes, "")`` or ``(None, why not)``. A regular file only, and no more than
    :data:`MAX_BYTES` + 1 bytes read (so an oversized one is reported, not loaded)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        return None, f"can't open {path}: {e.strerror}"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, f"{path} isn't a regular file"
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
    try:
        out = subprocess.run(argv, input=text, text=True, capture_output=True, timeout=10)
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
    try:
        out = subprocess.run(
            write_argv(target.engine or config.engine(), target.name, path),
            input=data,
            env=target.env,
            capture_output=True,
            timeout=_EXEC_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _err(f"✗ couldn't write into {target.name}: {e}")
        return 1
    if out.returncode != 0:
        detail = _printable(out.stderr.decode(errors="replace").strip(), 200)
        _err(f"✗ couldn't write into {target.name}: {detail}")
        return 1
    size = f"{len(data) / 1024:.0f} KB"
    if pane:
        typed = target.run(paste_argv(pane, path))
        if typed.returncode != 0:
            _err(f"✓ {path} in {target.name} ({size}), but typing it into {pane} failed: "
                 f"{_printable(typed.stderr.strip(), 200)}")  # fmt: skip
            return 1
        print(f"✓ {path} in {target.name} ({size}) — typed into {pane}; send it when ready")
        return 0
    print(f"✓ {path} in {target.name} ({size})")
    if copy and _copy(path):
        print("  path copied — paste it into the agent in the box")
    else:
        print("  give the agent that path")
    return 0
