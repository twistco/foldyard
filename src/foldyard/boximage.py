"""foldyard's packaged generic box image: its fingerprint, and the build shape a project's box and
the base VM's share.

Split out of box.py so the base VM's bake (basevm.py — `fy machine base bake`, `fy prereq
--fetch`) can compute and build it without importing box, and through it machine: machine's
``MACHINE`` binds at import from the project name, which outside a project is the working
directory's name — and a project-less verb must not care where it is run from. Imports only
config (stdlib otherwise).
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

from . import config

# The label every dev-box image carries: the fingerprint of the inputs it was built from.
FINGERPRINT_LABEL = "io.foldyard.box-fingerprint"


def _echo(cmd: list[str]) -> None:
    print("+ " + " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)


def packaged_fingerprint() -> str:
    """The fingerprint of foldyard's packaged generic box — what a project without a
    ``[box].image`` expects, whatever its name (the tag is not part of it)."""
    return image_fingerprint(config.packaged_box_image(BASE_BOX_TAG), Path("/"))


def image_fingerprint(img: dict, main: Path) -> str:
    configured_context = img.get("context")
    context = Path(configured_context) if configured_context else main
    if not context.is_absolute():
        context = main / context
    dockerfile = Path(img["dockerfile"])
    if not dockerfile.is_absolute():
        dockerfile = context / dockerfile
    payload = {
        "build_args": img.get("build_args") or {},
        "context": str(configured_context or "."),
        "dockerfile_sha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest(),
        "target": img.get("target"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_parts(engine: str, img: dict, fingerprint: str, context: str) -> tuple[list, list]:
    """The build command around where the proxy build-args go: ``(head, tail)``. One shape for
    a project's box and the base VM's, so the two images are built the same way."""
    cmd = [engine, "build"]
    # podman needs --format docker so the Dockerfile's SHELL [...] directive is honoured.
    if engine == "podman":
        cmd += ["--format", "docker"]
    cmd += ["--label", f"{FINGERPRINT_LABEL}={fingerprint}"]
    if img.get("target"):
        cmd += ["--target", str(img["target"])]
    tail = []
    for key, value in (img.get("build_args") or {}).items():
        tail += ["--build-arg", f"{key}={value}"]
    tail += ["-f", img["dockerfile"], "-t", img["tag"], context]
    return cmd, tail


# The packaged box's tag in the base VM (basevm.py). A project's `box up` re-tags it as its own
# when the fingerprints match (`box._from_base_image`), instead of building.
BASE_BOX_TAG = "foldyard-base-devbox:latest"


def build_packaged_image(engine: str, env: dict, tag: str) -> int:
    """Build foldyard's packaged generic box under ``tag``, fingerprinted as a project's would be
    — the base VM's bake (basevm.py). Direct, never through the build gate: the base is
    project-less and unwalled, and this Dockerfile is foldyard's own, never the repo's."""
    img = config.packaged_box_image(tag)
    head, tail = build_parts(engine, img, packaged_fingerprint(), img["context"])
    full = [*head, *tail]
    _echo(full)
    return subprocess.run(full, env=env, cwd=img["context"]).returncode
