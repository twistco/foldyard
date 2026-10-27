"""A stand-in for the yard ``fy worktree add`` and ``remove`` run git in (a one-off container).

The engine is never reached: ``box.engine_env`` names a dead socket, and ``subprocess.run`` is
wrapped so an ``<engine> run`` is recorded and answered here. Two runs are recognised:

* ``add``'s checkout (``--entrypoint git``), recorded in ``yard["runs"]``;
* ``remove``'s uncommitted-work check (``--entrypoint sh`` … ``-c <script>``), recorded in
  ``yard["checks"]``.

What it then does is ``yard["mode"]``:

* ``"none"`` — the checkout: record what the worktree held when handed over (``yard["seen"]``: its
  files, and its admin dir's), leave an empty index where the checkout would have written one (so
  ``add`` sees it reach your computer) unless ``yard["index"]`` is False, and return
  ``yard["rc"]``. The check: answer ``yard["out"]`` on stdout with ``yard["rc"]``, running nothing;
* ``"local"`` — run the container's argv HERE (git, or ``sh -c <script>``), in its ``-w`` dir with
  its ``-e`` env plus ``FY_YARD=1`` (so a rig's filter can say where it ran): the yard without a
  container, so what it writes exists afterwards. A test running the check this way runs a
  program under ``sh``, so it is marked ``@pytest.mark.spawns("sh")``;
* ``"partial"`` — the checkout writes a file into the ``-w`` dir, then fails.

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


def _env(cmd: list[str]) -> dict[str, str]:
    env = dict(os.environ)
    for flag, value in itertools.pairwise(cmd):
        if flag == "-e":
            key, _, val = value.partition("=")
            env[key] = val
    env["FY_YARD"] = "1"
    return env


def install(monkeypatch, *, mode: str = "local") -> dict:
    yard: dict = {
        "mode": mode,
        "rc": 0,
        "image": True,
        "index": True,
        "out": "fy-clean\n",
        "runs": [],
        "checks": [],
        "seen": {},
    }
    monkeypatch.setattr(config, "box_image", lambda: {"tag": IMAGE})
    monkeypatch.setattr(
        box, "engine_env", lambda: (ENGINE, {"DOCKER_HOST": "unix:///nonexistent/fy-test"})
    )
    monkeypatch.setattr(box, "image_exists", lambda *a: yard["image"])
    before = subprocess.run

    def run(cmd, *a, **kw):
        if list(cmd[:2]) != [ENGINE, "run"] or IMAGE not in cmd or "--entrypoint" not in cmd:
            return before(cmd, *a, **kw)
        entrypoint = cmd[cmd.index("--entrypoint") + 1]
        argv = cmd[cmd.index(IMAGE) + 1 :]
        workdir = Path(cmd[cmd.index("-w") + 1])
        if entrypoint == "sh" and argv[:1] == ["-c"]:
            yard["checks"].append(list(cmd))
            if yard["mode"] == "local":  # the engine's env (a dead socket) is the engine's own
                kw = {k: v for k, v in kw.items() if k not in ("env", "cwd")}
                return before(["sh", *argv], *a, cwd=workdir, env=_env(cmd), **kw)
            out = yard["out"]
            return subprocess.CompletedProcess(cmd, yard["rc"], out.encode(), None)
        if entrypoint != "git":
            return before(cmd, *a, **kw)  # not the checkout (worktree_init's run, say)
        yard["runs"].append(list(cmd))
        if yard["mode"] == "local":
            return before(["git", *argv], cwd=workdir, env=_env(cmd))
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
