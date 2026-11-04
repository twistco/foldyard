"""foldyard's clients on your computer carry no registry logins, unless the project opted in.

A ``podman`` client doesn't pull or build on your computer: it asks the API service in the VM, and
sends the logins from its auth file along. A build sends every login in the file, whatever it needs,
and the service writes them to a temp file while it runs, as the VM user. A box can bind-mount that
directory, so any box could read your registry logins during any build started from your computer.

So :func:`clamp`, first thing in ``cli.main``, points ``REGISTRY_AUTH_FILE`` at a file with no
logins, and every child inherits that, whichever engine call it makes. The variable wins over
podman's default paths (``$XDG_RUNTIME_DIR/containers/auth.json`` on Linux,
``~/.config/containers/auth.json``, ``~/.docker/config.json``), so those are never read.

A project that really needs a login takes two steps: you export :data:`OPT_IN` at a file made for
it, and the adopted ``[project] compose_env`` names :data:`OPT_IN` (``fy config widenings`` lists
it). Only then does the compose client (:func:`for_compose`) get that file. The name exists for this
alone, never ``REGISTRY_AUTH_FILE`` itself: plenty of people export that at their whole auth.json,
and a box can commit one line to ``compose_env``, which reads as harmless when it's adopted. So a
general export is never consent, and neither step alone gives anything. What's in that file reaches
the VM, so a box can read it during a build: make it hold one narrow login.

The docker CLI has the same habit and its own file: it reads ``$DOCKER_CONFIG/config.json``,
whatever ``REGISTRY_AUTH_FILE`` says, and a build sends every login there. ``[engine] cli =
"docker"`` is one line a box can propose, so :func:`clamp` points ``DOCKER_CONFIG`` at a clean
directory too.
That directory keeps what lives beside the logins (plugins, or ``docker compose`` is an unknown
command; contexts; the current context), and there is no opt-in on that engine.
``DOCKER_AUTH_CONFIG``, a login in a variable, beats any config file, so it's removed as well.

Inside a box nothing is touched: its logins are its own. Stdlib only: this runs on the hot path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Collection, Mapping, MutableMapping
from pathlib import Path

from . import config

AUTH = "REGISTRY_AUTH_FILE"
DOCKER = "DOCKER_CONFIG"
# A login in a variable, on docker CLIs that read it, which beats any config file.
DOCKER_AUTH = "DOCKER_AUTH_CONFIG"
# The opt-in: the one auth file you offered this project's compose client (see above).
OPT_IN = "FY_REGISTRY_AUTH_FILE"
# Your own REGISTRY_AUTH_FILE, remembered across the clamp only to hand back to your editor
# (:func:`operators_env`). Not a name `stack._host_passthrough` lets through.
SAVED = "FY_SAVED_REGISTRY_AUTH_FILE"
SAVED_DOCKER = "FY_SAVED_DOCKER_CONFIG"
SAVED_DOCKER_AUTH = "FY_SAVED_DOCKER_AUTH_CONFIG"
# What the file holds. Anything else in it gets written over: whatever inherits the clamp and runs
# `podman login` writes there, and every later build would send that login.
_EMPTY = '{"auths": {}}\n'
# See :func:`_docker_config`. `.invalid` never resolves (RFC 6761), so no image's registry is it.
_PLACEHOLDER = "no-registry-logins.invalid"


def _path() -> Path:
    return Path.home() / ".foldyard" / "no-registry-logins.json"


def _docker_dir() -> Path:
    return _path().with_name("no-registry-logins-docker")


def _write(path: Path, content: str = _EMPTY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(content)
    tmp.replace(path)


def no_logins_file() -> Path:
    """The auth file with no logins, made on first use and put right if it holds anything else. If
    that can't be written, the answer is a new path with nothing there, never one holding logins: a
    client pointed at a missing file carries none and doesn't fall back to the default paths
    (tests/test_registrylogins_e2e.py). It doesn't refuse either, and this doesn't stop `fy`. New
    each time, because the client can write what it's pointed at (`podman login`)."""
    path = _path()
    try:
        if not _holds(path, _EMPTY):
            _write(path)
    except OSError:
        if path.exists() and not _holds(path, _EMPTY):
            return _fallback(path)
    return path


def _fallback(path: Path) -> Path:
    return path.with_name(f"{path.name}.unwritable.{os.urandom(8).hex()}")


def _own_docker_dir(env: Mapping[str, str]) -> Path:
    """Your docker config directory: remembered, exported, or the default."""
    saved = env.get(SAVED_DOCKER) or env.get(DOCKER, "")
    return Path(saved) if saved and not _ours(saved) else Path.home() / ".docker"


def _docker_config(own: Path) -> str:
    """The clean ``config.json``: your plugins and current context, and nothing else of yours. Only
    those keys are read from your file, which also holds your logins.

    ``auths`` holds one empty entry on purpose. With no auth at all the docker CLI looks for the
    platform's credential store (``DetectDefaultStore``: the keychain on a computer with Docker
    Desktop), and a build then sends every login in it. This names a host, with nothing to send."""
    try:
        mine = json.loads((own / "config.json").read_text())
        current, extra = mine.get("currentContext"), mine.get("cliPluginsExtraDirs")
    except (OSError, ValueError, AttributeError):
        current, extra = None, None
    plugins = [str(own / "cli-plugins")]
    if isinstance(extra, list):
        plugins += [d for d in extra if isinstance(d, str) and d not in plugins]
    config_json: dict[str, object] = {
        "auths": {_PLACEHOLDER: {}},
        "cliPluginsExtraDirs": plugins,
    }
    if isinstance(current, str):
        config_json["currentContext"] = current
    return json.dumps(config_json, sort_keys=True) + "\n"


def _link_contexts(clean: Path, own: Path) -> None:
    """Your docker contexts, by a link: they hold endpoints, not registry logins."""
    link = clean / "contexts"
    try:
        if link.is_symlink() and os.readlink(link) == str(own / "contexts"):
            return
        link.unlink(missing_ok=True)
        if (own / "contexts").is_dir():
            link.symlink_to(own / "contexts")
    except OSError:
        pass


def no_logins_docker_config(environ: Mapping[str, str] | None = None) -> Path:
    """The docker config directory with no logins, kept as :func:`no_logins_file` keeps its file. A
    new directory with nothing there is the answer if it can't be put right: docker runs without
    one, and without logins."""
    env = os.environ if environ is None else environ
    own = _own_docker_dir(env)
    clean = _docker_dir()
    config_json = clean / "config.json"
    want = _docker_config(own)
    try:
        if not _holds(config_json, want):
            _write(config_json, want)
    except OSError:
        if config_json.exists() and not _holds(config_json, want):
            return _fallback(clean)
        return clean
    _link_contexts(clean, own)
    return clean


def _ours(path: str | Path) -> bool:
    """Whether ``path`` is a no-logins file or directory, or a fallback: never a login of yours.
    Beside the file, by name: a file of yours called something similar elsewhere stays yours."""
    p = Path(path)
    return p.parent == _path().parent and p.name.startswith("no-registry-logins")


def _holds(path: Path, content: str) -> bool:
    try:
        return path.read_text() == content
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
    own_docker = env.get(DOCKER)
    if own_docker and not _ours(own_docker):
        env[SAVED_DOCKER] = own_docker
    if DOCKER_AUTH in env:
        env[SAVED_DOCKER_AUTH] = env.pop(DOCKER_AUTH)
    env[AUTH] = str(no_logins_file())
    env[DOCKER] = str(no_logins_docker_config(env))


def _offered(declared: Collection[str]) -> str:
    """The file a project opted in with, which takes both steps; ``""`` otherwise."""
    return os.environ.get(OPT_IN, "") if OPT_IN in declared else ""


def for_compose(declared: set[str]) -> str:
    """``REGISTRY_AUTH_FILE`` for the compose client and the builds, on your computer: the file
    you exported as :data:`OPT_IN`, if the adopted ``[project] compose_env`` names it, else none."""
    return _offered(declared) or str(no_logins_file())


def apply_to(env: dict[str, str], declared: Collection[str]) -> None:
    """Put right what an engine client on your computer reads for logins, in the env foldyard hands
    it. Last, after any merge: `[ports]`, `compose_env` and the mode env can name anything."""
    env[AUTH] = for_compose(set(declared))
    env[DOCKER] = str(no_logins_docker_config())
    env.pop(DOCKER_AUTH, None)
    env.pop(SAVED_DOCKER_AUTH, None)


def operators_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` for something of YOURS foldyard launches (your editor), not a client of foldyard's:
    your own setting back, or none."""
    out = dict(env)
    for name, remembered in (
        (AUTH, SAVED),
        (DOCKER, SAVED_DOCKER),
        (DOCKER_AUTH, SAVED_DOCKER_AUTH),
    ):
        # From the process too: `env` is usually the compose client's, which never carries it.
        saved = out.pop(remembered, "") or os.environ.get(remembered, "")
        if saved:
            out[name] = saved
        else:
            out.pop(name, None)
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
    silent by construction, so it must be said. Only to someone who has logins here and hasn't
    opted in, which takes the export as well as the config line."""
    if config.in_box() or _offered(config.compose_env()) or not _your_logins():
        return []
    return [
        "  If that was a pull from a private registry: foldyard doesn't hand your registry logins",
        "  to the VM, where a box could read them. For one this project needs, make a file with",
        f"  only that login, export {OPT_IN} to it, and add that name to [project] compose_env",
        "  (`fy docs stack`, Private images).",
    ]
