"""Would `up` recreate it? — the compose provider's own per-service config hash.

Compose stamps every container with a hash of its service's RENDERED config (overlays merged,
profiles resolved, ``${VAR}``s interpolated) and ``up`` recreates exactly the containers whose
label differs from a fresh render. Comparing the two is therefore the drift check that cannot
disagree with ``up``: an env-only posture change shows for precisely the services that
interpolate it, and a service no change touched never shows, whatever its ``config_files``
label says (#33's follow-up — the overlay comparison could see neither).

Each provider is asked in its own terms, and neither needs the engine:

- **podman** (the product path): the bundled podman-compose has no hash command, so a
  subprocess on foldyard's own interpreter (``python -m foldyard.confighash``) drives its
  parser and ``config_hash`` — the very code its ``up`` compares with. A subprocess, never
  in-process: podman-compose parses through a module-level singleton that reads
  ``os.environ`` and the working directory. It leans on podman-compose internals, so it
  degrades to "unavailable" (never to a guess) if those move; tests/test_confighash.py pins
  the contract against the installed version.
- **docker-compose** (the CI fallback engine, ``config.engine``, or an explicit
  ``PODMAN_COMPOSE_PROVIDER`` naming it on podman): ``<engine> compose config --hash '*'``.
  Which provider is active is ``stack._podman_compose_active`` — the same test the foreign-
  container sweep uses, so the hash, the label and the sweep can never disagree about it.

Stdlib-only at import; reconcile imports this lazily for ``fy state``.
"""

from __future__ import annotations

import json
import subprocess
import sys

_DOCKER_LABEL = "com.docker.compose.config-hash"
_PODMAN_LABEL = "io.podman.compose.config-hash"


def label(ctx) -> str:
    """The container label ``ctx``'s active compose provider records its config hash under."""
    from . import stack

    return _PODMAN_LABEL if stack._podman_compose_active(ctx) else _DOCKER_LABEL


def desired(ctx, extra_profiles: list[str] | None = None, timeout: float = 20.0):
    """``({service: hash}, "")`` for the services ``ctx``'s compose renders — spanning
    ``extra_profiles`` too, so running profile-gated services are rendered — or
    ``(None, reason)`` when the provider can't say. Never raises. Unavailable is never an empty
    map: an empty render would read as "nothing to compare" and hide every drift."""
    from . import stack

    flags = stack._profile_flags(ctx, extra_profiles)
    docker = not stack._podman_compose_active(ctx)
    if docker:
        cmd = [*ctx.compose, *flags, "config", "--hash", "*"]
    else:
        cmd = [sys.executable, "-m", __name__, *ctx.compose[2:], *flags]
    try:
        proc = subprocess.run(
            cmd, env=ctx.env, cwd=str(ctx.main), capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        lines = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
        return None, lines[-1] if lines else f"exited {proc.returncode}"
    hashes = _parse(proc.stdout, docker=docker)
    return (hashes, "") if hashes else (None, "no service hashes in the render")


def _parse(stdout: str, *, docker: bool) -> dict[str, str] | None:
    if docker:  # one "<service> <hash>" per line
        pairs = [line.split() for line in stdout.splitlines() if line.strip()]
        if not pairs or any(len(p) != 2 for p in pairs):
            return None
        return dict(pairs)
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return None
    ok = isinstance(parsed, dict) and all(isinstance(v, str) for v in parsed.values())
    return parsed if ok else None


def _render_podman(argv: list[str]) -> dict[str, str]:
    """podman-compose's own hashes for the services ``argv`` (its global args: ``-f``s and
    ``--profile``s) renders. Runs in the subprocess only — it mutates podman-compose's
    module-level instance."""
    import podman_compose

    pc = podman_compose.podman_compose
    pc.global_args = pc._parse_args([*argv, "config"])
    pc._parse_compose_file()
    return {name: pc.config_hash(service) for name, service in pc.services.items()}


if __name__ == "__main__":
    print(json.dumps(_render_podman(sys.argv[1:])))
