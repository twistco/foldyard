"""foldyard's clients on your computer carry no registry logins.

A ``podman`` client doesn't pull or build on your computer: it asks the API service in the VM, and
sends the logins from its auth file along. A build sends every login in the file, whatever it needs,
and the service writes them to a temp file while it runs, as the VM user. A box can bind-mount that
directory, so any box could read your registry logins during any build started from your computer.

So :func:`clamp`, first thing in ``cli.main``, points ``REGISTRY_AUTH_FILE`` at a file with no
logins, and every child inherits that, whichever engine call it makes. The variable wins over
podman's default paths (``$XDG_RUNTIME_DIR/containers/auth.json`` on Linux,
``~/.config/containers/auth.json``, ``~/.docker/config.json``), so those are never read.

Until 0.16.0 a project could opt in: you exported :data:`OPT_IN` at a file holding one narrow
login, the adopted ``[project] compose_env`` named it, and the compose client on your computer got
that file. Since 0.16.0 the compose client and the stack's builds run in the VM
(:mod:`foldyard.guestcompose`), where no file on your computer reaches, so the opt-in has no route
and no effect: ``fy doctor`` says so when ``compose_env`` still names it
(``exposure._ignored``), and :func:`failure_hint` says what to do instead. Its route is a later
change, never what a compose file names (or the box would choose what your login fetches).

``stack._host_passthrough`` pins the no-logins file in the env of every engine call made here, and
``stack.resolve`` pins it again after its merges, because ``[ports]`` and the mode env can name
anything.

This is podman's alone. The docker CLI has no one switch (``config.json``, the platform's credential
store, ``DOCKER_AUTH_CONFIG``), so ``config.engine`` refuses it instead.

Inside a box nothing is touched: its logins are its own. Stdlib only: this runs on the hot path.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

from . import config

AUTH = "REGISTRY_AUTH_FILE"
# The old opt-in (see above): the one auth file you offered this project's compose client, when it
# ran on your computer. Named still, to say it does nothing now.
OPT_IN = "FY_REGISTRY_AUTH_FILE"
# Your own REGISTRY_AUTH_FILE, remembered across the clamp only to hand back to your editor
# (:func:`operators_env`). Not a name `stack._host_passthrough` lets through.
SAVED = "FY_SAVED_REGISTRY_AUTH_FILE"
# What the file holds. Anything else in it gets written over: whatever inherits the clamp and runs
# `podman login` writes there, and every later build would send that login.
_EMPTY = '{"auths": {}}\n'


def _path() -> Path:
    return Path.home() / ".foldyard" / "no-registry-logins.json"


def _write(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(_EMPTY)
    tmp.replace(path)


def no_logins_file() -> Path:
    """The auth file with no logins, made on first use and put right if it holds anything else. If
    that can't be written, the answer is a new path with nothing there, never one holding logins: a
    client pointed at a missing file carries none and doesn't fall back to the default paths
    (tests/test_registrylogins_e2e.py). It doesn't refuse either, and this doesn't stop `fy`. New
    each time, because the client can write what it's pointed at (`podman login`)."""
    path = _path()
    try:
        if not _holds_no_logins(path):
            _write(path)
    except OSError:
        if path.exists() and not _holds_no_logins(path):
            return path.with_name(f"{path.name}.unwritable.{os.urandom(8).hex()}")
    return path


def _ours(path: str | Path) -> bool:
    """Whether ``path`` is the no-logins file or a fallback: never a login of yours. Beside the
    file, by name: a file of yours called something similar elsewhere stays yours."""
    p = Path(path)
    return p.parent == _path().parent and p.name.startswith(_path().name)


def _holds_no_logins(path: Path) -> bool:
    try:
        return path.read_text() == _EMPTY
    except (OSError, ValueError):
        return False


def clamp(environ: MutableMapping[str, str] | None = None) -> None:
    """Point this process and its children at :func:`no_logins_file`, remembering your own export.
    Idempotent: a child ``fy`` (the supervisor, say) runs it again under the clamped value."""
    env = os.environ if environ is None else environ
    if config.in_box():
        return
    own = env.get(AUTH)
    if own and not _ours(own):
        env[SAVED] = own
    env[AUTH] = str(no_logins_file())


def operators_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` for something of YOURS foldyard launches (your editor), not a client of foldyard's:
    your own setting back, or none."""
    out = dict(env)
    # From the process too: `env` is usually the compose client's, which never carries it.
    saved = out.pop(SAVED, "") or os.environ.get(SAVED, "")
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
    if own := os.environ.get(SAVED) or os.environ.get(AUTH):
        paths.insert(0, Path(own))
    return [p for p in paths if p.is_file() and not _ours(p)]


def failure_hint() -> list[str]:
    """What to say after a failed build or start, where a pull may be why: pinning makes this
    silent by construction, so it must be said. Only to someone with logins here (or the old
    opt-in exported): compose runs in the VM, where none of them reaches."""
    if config.in_box():
        return []
    if not (_your_logins() or os.environ.get(OPT_IN)):
        return []
    return [
        "  If that was a pull from a private registry: compose runs in the VM, and no registry",
        "  login on your computer reaches it. An image already in the VM needs none, so pull it",
        "  there once yourself (`fy docs stack`, Private images).",
    ]
