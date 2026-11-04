"""foldyard's clients on your computer carry no registry logins, unless the project opted in.

A ``podman`` client doesn't pull or build on your computer: it asks the API service in the VM, and
sends the logins from its auth file along. A build sends every login in the file, whatever it needs,
and the service writes them to a temp file while it runs, as the VM user. A box can bind-mount that
directory, so any box could read your registry logins during any build started from your computer.

So :func:`clamp`, first thing in ``cli.main``, points ``REGISTRY_AUTH_FILE`` at a file with no
logins, and every child inherits that, whichever engine call it makes. The variable wins over
podman's default paths (``$XDG_RUNTIME_DIR/containers/auth.json`` on Linux,
``~/.config/containers/auth.json``, ``~/.docker/config.json``), so those are never read.

A project that really needs a login names ``REGISTRY_AUTH_FILE`` in ``[project] compose_env``, which
is adopted and listed by ``fy config widenings``, and exports a file made for it: only then does the
compose client's environment (:func:`for_compose`) get that file. Naming it without exporting one is
still no logins, never the default paths, so the whole of your auth.json is never one config line
away. What's in that file reaches the VM, so a box can read it during a build: make it hold one
narrow login.

Inside a box nothing is touched: its logins are its own. Stdlib only: this runs on the hot path.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

from . import config

AUTH = "REGISTRY_AUTH_FILE"
# Your own export, remembered across the clamp for :func:`for_compose` and :func:`operators_env`.
# Not a name `stack._host_passthrough` lets through.
SAVED = "FY_REGISTRY_AUTH_FILE"


def _path() -> Path:
    return Path.home() / ".foldyard" / "no-registry-logins.json"


def _write(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write('{"auths": {}}\n')
    tmp.replace(path)


def no_logins_file() -> Path:
    """The auth file with no logins, made on first use. If it can't be written the path is still
    the answer: a client pointed at a missing file refuses, loudly, and never falls back to the
    default paths."""
    path = _path()
    try:
        if not path.is_file():
            _write(path)
    except OSError:
        pass
    return path


def clamp(environ: MutableMapping[str, str] | None = None) -> None:
    """Point this process and its children at :func:`no_logins_file`, remembering your own export.
    Idempotent: a child ``fy`` (the supervisor, say) runs it again under the clamped value."""
    env = os.environ if environ is None else environ
    if config.in_box():
        return
    empty = str(no_logins_file())
    own = env.get(AUTH)
    if own and own != empty:
        env[SAVED] = own
    env[AUTH] = empty


def _own_export() -> str:
    """The auth file you exported, whether or not :func:`clamp` has run in this process."""
    empty = str(no_logins_file())
    own = os.environ.get(SAVED) or os.environ.get(AUTH) or ""
    return "" if own == empty else own


def for_compose(declared: set[str]) -> str:
    """``REGISTRY_AUTH_FILE`` for the compose client and the builds, on your computer: your export
    if the adopted ``[project] compose_env`` names it, else no logins."""
    own = _own_export()
    return own if AUTH in declared and own else str(no_logins_file())


def operators_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` for something of YOURS foldyard launches (your editor), not a client of foldyard's:
    your own setting back, or none."""
    out = dict(env)
    saved = out.pop(SAVED, "")
    if saved:
        out[AUTH] = saved
    else:
        out.pop(AUTH, None)
    return out


def _your_logins() -> list[Path]:
    """Auth files podman would have read on your computer. Asks only whether they exist."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    home = Path.home()
    paths = [home / ".config/containers/auth.json", home / ".docker/config.json"]
    if runtime:
        paths.insert(0, Path(runtime) / "containers/auth.json")
    if own := _own_export():
        paths.insert(0, Path(own))
    return [p for p in paths if p.is_file()]


def failure_hint() -> list[str]:
    """What to say after a failed build or start, where a pull may be why: pinning makes this
    silent by construction, so it must be said. Only to someone who has logins here and hasn't
    opted in."""
    if config.in_box() or AUTH in config.compose_env() or not _your_logins():
        return []
    return [
        "  If that was a pull from a private registry: foldyard doesn't hand your registry logins",
        "  to the VM, where a box could read them. For one this project needs, make a file with",
        f"  only that login, export {AUTH} to it, and add it to [project] compose_env",
        "  (`fy docs stack`, Private images).",
    ]
