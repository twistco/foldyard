"""The compose client, in the guest (``[machine] compose = "vm"``, docs/compose-in-vm-scope.md).

Run IN THE GUEST, as the VM user, never on your computer. ``foldyard.guestcompose`` sends one
payload per call over the backend's ssh route: a zip of this module, foldyard's own pinned
podman-compose and its two pure-Python dependencies, plus one request, all on ``python3 -I -``'s
stdin. Nothing is installed in the guest and no file there is read for code, so the client is
always the version foldyard was tested with.

A request is ``{"op", "argv", "env", "cwd", "socket"}``:

- ``compose`` — podman-compose with ``argv`` (its global args, then the command), exactly as
  ``podman compose`` would run it, its exit code ours;
- ``hash`` — podman-compose's own per-service config hash for the services ``argv`` renders
  (what ``foldyard.confighash`` reads), printed as JSON;
- ``podman`` — ``podman <argv>`` (the builds and tags of the stack build).

The environment is the request's, never the guest login's, except for what podman itself needs to
run as this user (``_GUEST``). ``CONTAINER_HOST`` is the guest socket the request names, so podman
runs in remote mode against the same API service your computer's client uses, whatever the
posture's runtime selection there.

What this prints is box-influenced data: the compose files are the box's, and so is the VM user's
home. Your computer treats it as a claim, as it treats engine output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# What podman needs from the guest's own login environment to run as this user. Everything else
# comes from the request.
_GUEST = ("PATH", "HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR")


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


def _compose(argv: list[str]) -> int:
    import podman_compose  # after the env and cwd are set: its parser reads both

    sys.argv = ["podman-compose", *argv]
    try:
        podman_compose.main()
    except SystemExit as e:
        code = e.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


def _hash(argv: list[str]) -> int:
    import podman_compose

    pc = podman_compose.podman_compose
    pc.global_args = pc._parse_args([*argv, "config"])
    pc._parse_compose_file()
    print(json.dumps({name: pc.config_hash(svc) for name, svc in pc.services.items()}))
    return 0


def main(request: dict) -> int:
    op = request.get("op")
    argv = [str(a) for a in request.get("argv") or []]
    env = _environ(request)
    os.environ.clear()
    os.environ.update(env)
    try:
        os.chdir(str(request.get("cwd") or "/"))
    except OSError as e:
        print(
            f"✗ foldyard's compose runner: can't enter {request.get('cwd')!r} in the VM ({e})",
            file=sys.stderr,
        )
        return 125
    if op == "compose":
        return _compose(argv)
    if op == "hash":
        return _hash(argv)
    if op == "podman":
        return subprocess.run(["podman", *argv], env=env).returncode
    print(f"✗ foldyard's compose runner: unknown op {op!r}", file=sys.stderr)
    return 125
