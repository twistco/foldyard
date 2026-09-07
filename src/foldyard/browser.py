"""Open the selected stack's localhost app URL in the host browser."""

from __future__ import annotations

import sys
import webbrowser

from . import config, stack


def _err(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def open_app() -> int:
    """Resolve the active worktree's app port and open it in the host browser."""
    if config.in_box():
        _err("✗ `fy open` must run on the host — the dev box cannot launch the host browser.")
        return 1

    ctx = stack.resolve(no_machine=True)
    port_key = config.app_port_key()
    if not port_key:
        _err(
            "✗ no app port configured — set [project].app_port to the app's [ports] key "
            "in foldyard.toml."
        )
        return 1

    raw_port = ctx.env.get(port_key)
    if not raw_port:
        _err(f"✗ [project].app_port names {port_key!r}, but [ports].{port_key} is not configured.")
        return 1

    url = f"http://localhost:{raw_port}"
    print(f"▶ opening {url}")
    if not webbrowser.open(url, new=2):
        _err(f"✗ could not launch a browser. Open {url} manually.")
        return 1
    return 0
