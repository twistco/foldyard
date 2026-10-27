"""A stand-in for the yard ``fy worktree add`` checks a new worktree out in (a one-off container).

The engine is never reached: ``box.engine_env`` names a dead socket, and ``subprocess.run`` is
wrapped so an ``<engine> run`` is recorded and answered here. What it then does is ``yard["mode"]``:

* ``"none"`` — record what the worktree held when handed over (``yard["seen"]``: its files, and
  its admin dir's), leave an empty index where the checkout would have written one (so ``add``
  sees it reach your computer) unless ``yard["index"]`` is False, and return ``yard["rc"]``;
* ``"local"`` — run the container's git argv HERE, in its ``-w`` dir with its ``-e`` env: the
  yard's checkout without a container, so the files and index exist afterwards;
* ``"partial"`` — write a file into the ``-w`` dir, then fail.

``yard["image"]`` is what ``box.image_exists`` answers. Every other ``subprocess.run`` goes
through to whatever ran before (the real one, or a test's own fake installed earlier).
"""

from __future__ import annotations

import itertools
import os
import subprocess
from pathlib import Path

from foldyard import box, config

ENGINE = "podman"
IMAGE = "proj-devbox:latest"


def install(monkeypatch, *, mode: str = "local") -> dict:
    yard: dict = {"mode": mode, "rc": 0, "image": True, "index": True, "runs": [], "seen": {}}
    monkeypatch.setattr(config, "box_image", lambda: {"tag": IMAGE})
    monkeypatch.setattr(
        box, "engine_env", lambda: (ENGINE, {"DOCKER_HOST": "unix:///nonexistent/fy-test"})
    )
    monkeypatch.setattr(box, "image_exists", lambda *a: yard["image"])
    before = subprocess.run

    def run(cmd, *a, **kw):
        if list(cmd[:2]) != [ENGINE, "run"] or IMAGE not in cmd or "--entrypoint" not in cmd:
            return before(cmd, *a, **kw)
        if cmd[cmd.index("--entrypoint") + 1] != "git":
            return before(cmd, *a, **kw)  # not the checkout (worktree_init's run, say)
        yard["runs"].append(list(cmd))
        workdir = Path(cmd[cmd.index("-w") + 1])
        if yard["mode"] == "local":
            env = dict(os.environ)
            for flag, value in itertools.pairwise(cmd):
                if flag == "-e":
                    key, _, val = value.partition("=")
                    env[key] = val
            return before(["git", *cmd[cmd.index(IMAGE) + 1 :]], cwd=workdir, env=env)
        if yard["mode"] == "partial":
            (workdir / "x").write_text("half\n")
            return subprocess.CompletedProcess(cmd, 1)
        admin = workdir / (workdir / ".git").read_text().removeprefix("gitdir:").strip()
        yard["seen"] = {
            "worktree": sorted(p.name for p in workdir.iterdir()),
            "admin": sorted(p.name for p in admin.iterdir()) if admin.is_dir() else [],
        }
        if yard["index"]:
            admin.mkdir(parents=True, exist_ok=True)
            (admin / "index").touch()
        return subprocess.CompletedProcess(cmd, yard["rc"])

    monkeypatch.setattr(subprocess, "run", run)
    return yard
