"""The compose client, in the guest (``[machine] compose = "vm"``, docs/compose-in-vm-scope.md).

Run IN THE GUEST, as the VM user, never on your computer. ``foldyard.guestcompose`` sends one
payload per call over the backend's ssh route: a zip of this module, foldyard's own pinned
podman-compose and its two pure-Python dependencies, plus one request, all on ``python3 -I -``'s
stdin and imported from memory. Nothing is installed in the guest and no file there is read for
code, so the client is always the version foldyard was tested with.

A request is ``{"op", "argv", "env", "cwd", "socket"}``:

- ``compose`` — podman-compose with ``argv`` (its global args, then the command), exactly as
  ``podman compose`` would run it, its exit code ours;
- ``hash`` — podman-compose's own per-service config hash for the services ``argv`` renders
  (what ``foldyard.confighash`` reads), printed as JSON;
- ``services`` — each file in ``argv`` parsed on its own, as ``{file: {service: [profile, …]}}``,
  ``null`` for one that can't be read as a compose file;
- ``podman`` — ``podman <argv>`` (the builds and tags).

The environment is the request's, never the guest login's, except for what podman itself needs to
run as this user (``_GUEST``). ``CONTAINER_HOST`` is the guest socket the request names, so podman
runs in remote mode against the same API service your computer's client uses, and what it starts
belongs to that service, not to this ssh session.

What this prints is box-influenced data: the compose files are the box's, and so is the VM user's
home. Your computer treats it as a claim, as it treats engine output.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

# What podman needs from the guest's own login environment to run as this user. Everything else
# comes from the request.
_GUEST = ("PATH", "HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR")

# A file your computer just replaced can read as missing from the VM for a moment: up to about 1 s
# on Lima's virtiofs, 5 s on libkrun's (ADR-0021's visibility record). So a compose file that isn't
# there is waited for this long before compose gets to call it missing. Your computer has already
# refused one that is really gone (`config.missing_compose_files`), so this is rarely paid.
_SETTLE = 6.0


def _environ(request: dict) -> dict[str, str]:
    env = {str(k): str(v) for k, v in (request.get("env") or {}).items()}
    for key in _GUEST:
        if key in os.environ:
            env[key] = os.environ[key]
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    socket = str(request.get("socket") or "")
    if socket:
        env["CONTAINER_HOST"] = env["DOCKER_HOST"] = f"unix://{socket}"
    return env


def _die_with_the_session() -> None:
    """End this process group when the ssh session goes. There is no terminal to carry a hangup,
    and stdin is already at its end (it was the program), so an interrupted `fy logs -f` or a
    timed-out `fy up` would otherwise leave the client running in the guest."""
    parent = os.getppid()
    try:
        os.setsid()
    except OSError:  # sshd already made this process a session leader
        pass
    # This process outlives its own SIGTERM long enough to send the SIGKILL. A handler, not SIG_IGN:
    # a handler resets on exec, so the children it starts still die of the SIGTERM.
    signal.signal(signal.SIGTERM, lambda *_: None)

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(0.5)
        os.killpg(0, signal.SIGTERM)
        time.sleep(5)
        os.killpg(0, signal.SIGKILL)

    threading.Thread(target=watch, daemon=True).start()


def _settle(argv: list[str]) -> None:
    deadline = time.monotonic() + _SETTLE
    for i, arg in enumerate(argv[:-1]):
        if arg == "-f":
            while not os.path.exists(argv[i + 1]) and time.monotonic() < deadline:
                time.sleep(0.1)


def _compose(argv: list[str]) -> int:
    import podman_compose  # after the env and cwd are set: its parser reads both

    _settle(argv)
    sys.argv = ["podman-compose", *argv]
    try:
        podman_compose.main()
    except SystemExit as e:
        code = e.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


def _hash(argv: list[str]) -> int:
    import podman_compose

    _settle(argv)
    pc = podman_compose.podman_compose
    pc.global_args = pc._parse_args([*argv, "config"])
    pc._parse_compose_file()
    print(json.dumps({name: pc.config_hash(svc) for name, svc in pc.services.items()}))
    return 0


def _one_file(path: str) -> dict[str, list[str]] | None:
    import yaml

    deadline = time.monotonic() + _SETTLE
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.1)
    try:
        with open(path) as f:
            services = (yaml.safe_load(f) or {}).get("services") or {}
    except (OSError, ValueError, AttributeError, yaml.YAMLError):
        return None
    if not isinstance(services, dict):
        return None
    return {
        str(name): [str(p) for p in spec.get("profiles") or []] if isinstance(spec, dict) else []
        for name, spec in services.items()
    }


def main(request: dict) -> int:
    _die_with_the_session()
    op = request.get("op")
    argv = [str(a) for a in request.get("argv") or []]
    env = _environ(request)
    os.environ.clear()
    os.environ.update(env)
    try:
        os.chdir(str(request.get("cwd") or "/"))
    except OSError as e:
        # The compose files and build contexts are read in the VM, so the checkout must be there.
        print(
            f"✗ foldyard's compose client can't enter {request.get('cwd')!r} in the VM ({e}): does "
            "the VM mount this checkout? (`fy doctor` says; `fy machine recreate`)",
            file=sys.stderr,
        )
        return 125
    if op == "compose":
        return _compose(argv)
    if op == "hash":
        return _hash(argv)
    if op == "services":
        print(json.dumps({path: _one_file(path) for path in argv}))
        return 0
    if op == "podman":
        sys.stdout.flush()
        return subprocess.run(["podman", *argv], env=env).returncode
    print(f"✗ foldyard's compose runner: unknown op {op!r}", file=sys.stderr)
    return 125
