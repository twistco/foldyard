"""Worktrees a box makes: ``fy worktree add <name>``, run in a box.

On your computer that verb sets up a worktree's own box, stack, ports and adopted config, which only
your computer can do. A change that needs none of that needs a checkout and somewhere to install,
and in a box the same verb makes both, with nobody asked. The checkout goes in the worktrees root,
where every worktree is, on its own branch; each ``[box].shadow_volumes`` dir in it gets a volume,
so an install there runs at VM-disk speed and leaves nothing on your computer's disk (a plain ``git
worktree add`` puts all of it there, through the share). The same command on your computer later
registers it, like any existing worktree, and gives it the rest.

* **The volume is mounted into the running box** by a one-off container (:func:`_helper`,
  ``assets/boxworktree/attach.py``) through the engine socket the box already holds. It shares the
  box's pid namespace, drops every capability and takes back the three the mount needs, never
  ``--privileged`` (pinned by ``tests/test_boxworktree.py``), and runs the BOX's python, as its own
  image needn't have one. Nothing on your computer runs, and nothing is asked of it.
* **Before anything installs.** ``git worktree add`` runs with the repo's hooks off, the volumes go
  on, and only then does the post-checkout hook run (a hook that installs is the usual one).
* **The dir underneath is read-only** (0555), for when the volume isn't mounted: the box was
  restarted, or it runs under gVisor. A safety net, not a wall: it stops the installs that write
  into the dir (npm, uv, pnpm's hoisted linker; measured 2026-11-23), and the box's root can
  ``chmod`` it, or replace it, as pnpm's isolated linker does. The window is short: a box
  mounts its own worktrees' volumes again as it starts (:data:`START`), and ``fy box up`` and the
  verb do too.
* **The volume is named as that worktree's own box would name it**, so promoting the worktree
  (``fy worktree add <name>`` on your computer) moves nothing: its box finds them as they are, and
  ``fy worktree remove`` sweeps them. Registering it also masks those dirs in this box, read-only
  on top of the volume, like any registered worktree's (:mod:`foldyard.crossmask`).

``fy worktree remove`` in a box takes those, and a plain ``git worktree add`` one your computer
never registered (:func:`owner`).

What makes one box-made is :data:`MARKER` in its git admin dir, inside the shared ``.git`` the box
writes: reporting, and the in-box refusals below. Your computer decides nothing from it."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import config, crossmask, stack, worktree, worktree_published

MARKER = "fy-box-made"
HELPER = Path(__file__).resolve().parent / "assets" / "boxworktree" / "attach.py"
CAPS = ("sys_admin", "sys_chroot", "sys_ptrace")  # setns + mount, its chroot, reading /proc/1/ns
TIMEOUT_S = 60.0
_LOCK = "/tmp/fy-worktree-mount.lock"  # the box's own /tmp
_OWN_MOUNTINFO = Path("/proc/self/mountinfo")
_NULL_OID = "0" * 40
_STATUSES = {
    "busy": "something else is mounted there",
    "symlink": "a symlink on the path (refused, never followed)",
    "missing": "the dir isn't there",
    "not-a-dir": "not a directory",
    "invalid": "not a path a box can mount",
}


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


# ── what a box-made worktree is ──────────────────────────────────────────────────────────


def _marker(main: Path, wt_dir: Path) -> Path | None:
    """Where :data:`MARKER` goes for ``wt_dir``: its admin dir, only where that is one directory
    directly under ``main``'s ``.git/worktrees`` (its ``.git`` file is box-written)."""
    admin = worktree._own_admin_dir(main, wt_dir)
    return None if admin is None else admin / MARKER


def is_box_made(main: Path, wt_dir: Path) -> bool:
    """Whether a box made worktree ``wt_dir`` (and it isn't registered since). REPORTING, and the
    in-box refusals: the marker is box-written."""
    marker = _marker(main, wt_dir)
    try:
        return marker is not None and stat.S_ISREG(marker.lstat().st_mode)
    except OSError:
        return False


def _maker(main: Path, wt_dir: Path) -> str:
    """The name of the box that made ``wt_dir``, by its marker; ``""`` when there is none."""
    marker = _marker(main, wt_dir)
    try:
        text = marker.read_text() if marker is not None else ""
    except (OSError, ValueError):
        return ""
    return next((ln.removeprefix(_MAKER) for ln in text.splitlines() if ln.startswith(_MAKER)), "")


def names(main: Path) -> list[str]:
    """The box-made worktrees under the worktrees root. REPORTING only."""
    root = config.worktrees_root(main)
    try:
        dirs = sorted(d for d in root.iterdir() if d.is_dir() and not d.is_symlink())
    except OSError:
        return []
    return [d.name for d in dirs if is_box_made(main, d)]


def volumes(wt_dir: Path, name: str) -> dict[str, str]:
    """``target path → volume`` for each declared dependency dir of worktree ``name``, named as
    its own box would (``box.shadow_volume``)."""
    from . import box

    owner = f"{config.project_prefix()}-{name}-devbox"
    out: dict[str, str] = {}
    for d in config.box_shadow_volumes():
        # `box up` mounts the entry as written: a shape crossmask can't name either is left out.
        if rel := crossmask._norm(d):
            out[f"{wt_dir}/{rel}"] = box.shadow_volume(owner, d)
    return {
        t: v
        for t, v in out.items()
        if not crossmask._UNSAFE & set(t)
        # One inside another declared dir is on that one's volume already.
        and not any(t.startswith(f"{other}/") for other in out)
    }


def attached(paths: dict[str, str]) -> set[str]:
    """Of ``paths`` (target → volume), those with the volume on, by this box's own mount table."""
    try:
        text = _OWN_MOUNTINFO.read_text()
    except OSError:
        return set()
    top: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 6:  # later lines are mounted on top of earlier ones
            top[crossmask._unescape(fields[4])] = crossmask._unescape(fields[3])
    return {t for t, v in paths.items() if top.get(t, "").endswith(f"/volumes/{v}/_data")}


# ── the volumes ───────────────────────────────────────────────────────────────────────


def _placeholder(root: Path, target: str) -> str:
    """Make ``target`` (under ``root``) a read-only empty dir for the volume to sit on; why not, as
    a sentence fragment, else ``""``. Follows no link below ``root``."""
    from . import mountwrite

    rel = os.path.relpath(target, root)
    try:
        mountwrite.makedirs(root, rel, 0o755)
        if mountwrite.listdir(root, rel):
            return "it already holds files"
        mountwrite.chmod_dir(root, rel, 0o555)
    except (OSError, ValueError) as e:
        return f"a symlink or a file on the path ({e})"
    return ""


def _helper(
    engine: str, env: dict, box: str, image: str, mode: str, vols: dict[str, str]
) -> tuple[str, dict[str, str]]:
    """``(refusal, results)`` from one run of the helper for ``vols`` (target → volume)."""
    mounts = [a for n, v in enumerate(vols.values()) for a in ("-v", f"{v}:/fy/{n}")]
    cmd = [
        *(engine, "run", "--rm", "-i", "--pull", "never", "--user", "0"),
        *("--cap-drop", "all", *(a for c in CAPS for a in ("--cap-add", c))),
        *("--pid", f"container:{box}", "--network", "none"),
        *("--security-opt", "label=disable", *mounts, "--entrypoint", "sh", image),
        *("-c", 'exec 3</proc/1/ns/mnt 4</fy && exec chroot /proc/1/root "$@"', "sh"),
        *(os.path.realpath(sys.executable), "-I", "-", mode, *vols),
    ]
    _err("+ " + " ".join(shlex.quote(c) for c in cmd))
    try:
        res = subprocess.run(
            cmd, env=env, input=HELPER.read_bytes(), capture_output=True, timeout=TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return f"the helper container didn't finish within {int(TIMEOUT_S)}s", {}
    except OSError as e:
        return f"the helper container couldn't start ({e})", {}
    out, err = (b.decode(errors="replace").strip().splitlines() for b in (res.stdout, res.stderr))
    try:
        answer = json.loads(out[-1])
    except (ValueError, IndexError):
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("results"), dict):
        why = (err or out or [f"exit {res.returncode}"])[-1]
        return f"the helper container failed ({why})", {}
    if answer.get("refused"):
        return str(answer["refused"]), {}
    return "", {str(k): str(v) for k, v in answer["results"].items()}


def _this_box() -> tuple[str, dict, str, str] | str:
    """``(engine, env, this box's name, its image)``, or why volumes can't be mounted into it."""
    from . import agents, box, sandbox

    if os.environ.get(sandbox.BAKED_ENV) == "gvisor":  # what this box runs under, not the config
        return "this box runs under gVisor, which has no way to add one to a running box"
    engine, env = box.engine_env()
    name = agents.this_box().name
    out = subprocess.run(
        [engine, "inspect", name, "--format", "{{.Image}}"],
        env=env,
        capture_output=True,
        text=True,
    )
    image = out.stdout.strip()
    if out.returncode != 0 or not image:
        return f"the engine doesn't know this box ({name})"
    return engine, env, name, image


def _why(status: str) -> str:
    return _STATUSES.get(status, status.removeprefix("error: "))


def mount(wt_dir: Path, name: str) -> bool:
    """:func:`_mount`, one at a time in this box: its start and its bootstrap may both be at it, and
    two would put the same volume on a dir twice."""
    with open(_LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        return _mount(wt_dir, name)


def _mount(wt_dir: Path, name: str) -> bool:
    """Put worktree ``name``'s volumes on its dependency dirs in this box, saying what happened.
    True when every one is there. Where one isn't, its dir stays read-only (the net above)."""
    vols = volumes(wt_dir, name)
    if not vols:
        return True
    root = wt_dir.parent
    here = attached(vols)
    left: dict[str, str] = {}
    for t in vols:
        if t not in here and (why := _placeholder(root, t)):
            left[t] = why
    todo = {t: v for t, v in vols.items() if t not in here and t not in left}
    if todo:
        facts = _this_box()
        refused = facts if isinstance(facts, str) else ""
        results: dict[str, str] = {}
        if not isinstance(facts, str):
            engine, env, box, image = facts
            for v in todo.values():  # named here, so `run` never makes one with the wrong driver
                subprocess.run(
                    [engine, "volume", "create", "--ignore", v], env=env, capture_output=True
                )
            refused, results = _helper(engine, env, box, image, "attach", todo)
        for t in todo:
            status = results.get(t, "no answer")
            if refused or status not in ("attached", "already"):
                left[t] = refused or _why(status)
    for t in vols:
        if t not in left and t not in here:
            print(f"✓ {os.path.relpath(t, wt_dir)} is on a volume in this box")
    for why in dict.fromkeys(left.values()):
        dirs = ", ".join(os.path.relpath(t, wt_dir) for t, w in left.items() if w == why)
        _err(f"⚠ no volume on {dirs}: {why}.")
    if left:
        _err("  An install there fails or lands on your computer's disk. Give it a box of its own:")
        _err(f"  `fy worktree add {shlex.quote(name)} --box`, on the operator's computer.")
    return not left


# What a box runs instead of a bare `sleep infinity` (its arguments): the remount, beside it. A box
# restarted outside foldyard (Podman Desktop, `podman restart`) else waits for someone to notice.
# Nothing here may hold the box up: no foldyard in it yet (the first start), or one that hangs. The
# PATH is the bootstrap's (box._PATH_PREPEND_SNIPPET, bash's): its foldyard, or the image's own.
START = (
    '{ PATH="$HOME/.local/bin:/opt/fy-tools/bin:$PATH"; '
    "command -v foldyard && foldyard worktree remount --wait; } "
    "</dev/null >/tmp/fy-remount.log 2>&1 & "
    'exec "$@"'
)


def start_argv(*command: str) -> list[str]:
    """``command`` as a box's main process, with :data:`START` beside it."""
    return ["sh", "-c", START, "sh", *command]  # the second `sh` is its $0: "$@" starts after it


_TRIES, _PAUSE_S = 8, 2.0  # --wait: the engine may not call a box running as it starts


def mount_all(wait: bool = False) -> int:
    """The volumes of every worktree THIS box made, after a restart dropped them: the box's start
    (``wait``, :data:`START`), its bootstrap and ``fy box up`` run it, as the hidden ``fy worktree
    remount``. Best-effort: a report, never a failure."""
    from . import agents, sandbox

    main = stack.main_repo()
    root = config.worktrees_root(main)
    mine = agents.this_box().name
    # A marker that names no box was made before markers did: any box's.
    todo = [n for n in names(main) if _maker(main, root / n) in (mine, "")]
    if os.environ.get(sandbox.BAKED_ENV) == "gvisor":
        wait = False  # nothing to wait for
    for left in range(_TRIES if wait else 1, 0, -1):
        todo = [n for n in todo if not mount(root / n, n)]
        if not todo or left == 1:
            break
        time.sleep(_PAUSE_S)
    return 0


# ── the verbs ─────────────────────────────────────────────────────────────────────────


def _git(where: Path, *args: str) -> int:
    cmd = ["git", "-C", str(where), *args]
    _err("+ " + " ".join(shlex.quote(c) for c in cmd))
    # The box's git shim sends a plain `git worktree add` here (FY_WORKTREE_ADD: this is it). Not a
    # hook's: what the repo runs is told like anything else.
    own = {} if args[0] == "hook" else {"FY_WORKTREE_ADD": "1"}
    return subprocess.run(cmd, env={**os.environ, **own}).returncode


def _add_argv(main: Path, wt_dir: Path, branch: str, base: str) -> list[str] | None:
    """The ``git worktree add`` arguments, by the same rules as ``fy worktree add``: an existing
    branch is checked out, a new one forks off the default branch (or ``base``), untracked."""
    if worktree._git_has_ref(main, f"refs/heads/{branch}"):
        return ["worktree", "add", str(wt_dir), branch]
    if worktree._git_has_ref(main, f"refs/remotes/origin/{branch}"):
        # --no-track: an agent's later push goes where it says, never to what it started from.
        return ["worktree", "add", "--no-track", "-b", branch, str(wt_dir), f"origin/{branch}"]
    if base and not worktree._git_resolves(main, base):
        _err(f"✗ --from base '{base}' doesn't resolve in {main}")
        return None
    ref = base or worktree._default_base_ref(main)
    return ["worktree", "add", "--no-track", "-b", branch, str(wt_dir), *([ref] if ref else [])]


_MADE = "made by `fy worktree add` in a box; the same on your computer registers it\n"
_MAKER = "box: "
_HOOK_PENDING = "post-checkout hook: not run yet\n"


def _marker_text() -> str:
    from . import agents

    return f"{_MADE}{_MAKER}{agents.this_box().name}\n"


def _hook_is_pending(main: Path, wt_dir: Path) -> bool:
    marker = _marker(main, wt_dir)
    return marker is not None and _HOOK_PENDING in marker.read_text()


def _run_hook(main: Path, wt_dir: Path) -> None:
    """The repo's post-checkout hook, as ``git worktree add`` would call it, once."""
    head = worktree._git_read(wt_dir, "rev-parse", "HEAD", capture_output=True, text=True).stdout
    hook = ("hook", "run", "--ignore-missing", "post-checkout", "--")
    if _git(wt_dir, *hook, _NULL_OID, head.strip(), "1") != 0:
        _err("⚠ the repo's post-checkout hook failed (above); the worktree is fine")
    if (marker := _marker(main, wt_dir)) is not None:
        marker.write_text(_marker_text())


def add(name: str, branch: str = "", base: str = "", box_up: bool = False) -> int:
    """``fy worktree add`` in a box: make worktree ``name``, or (one it made) mount its volumes
    again. ``box_up`` (``--box``) is your computer's to do, so it is refused."""
    if box_up:
        _err("✗ --box: a box can't start another worktree's. Without it, this makes a worktree to")
        _err("  use from this box; with it, on the operator's computer, one with a box of its own:")
        _err(f"  `fy worktree add {shlex.quote(name)} --box`.")
        return 1
    if worktree.refuse_name(name, branch):
        return 1
    main = stack.main_repo()
    root = config.worktrees_root(main)
    wt_dir = root / name
    if name in (crossmask.baked_names() or ()):
        _err(f"✗ '{name}' is a worktree registered on the operator's computer, with its own box:")
        _err(f"  `WORKTREE={shlex.quote(name)} fy box exec '<command>'`.")
        return 1
    if wt_dir.is_symlink() or wt_dir.exists():
        if not is_box_made(main, wt_dir):
            _err(f"✗ {wt_dir} already exists, and no box made it with `fy worktree add`")
            return 1
        if not mount(wt_dir, name):
            return 1
        if _hook_is_pending(main, wt_dir):
            _run_hook(main, wt_dir)
        return 0
    inside = str(root).startswith(f"{main}/")  # then the checkout's own mount covers it
    if not root.is_dir() or not (inside or os.path.ismount(root)):
        _err(f"✗ the worktrees root ({root}) isn't mounted in this box, so your computer would")
        _err("  never see the checkout. `fy box recreate` on your computer mounts it.")
        return 1
    branch = branch or f"wt/{name}"
    argv = _add_argv(main, wt_dir, branch, base)
    if argv is None:
        return 1
    # Hooks off for the checkout: the volumes must be there before one installs anything.
    if (rc := _git(main, "-c", "core.hooksPath=/dev/null", *argv)) != 0:
        _err("✗ git worktree add failed")
        return rc
    if (marker := _marker(main, wt_dir)) is not None:
        marker.write_text(_marker_text() + _HOOK_PENDING)
    if mount(wt_dir, name):
        _run_hook(main, wt_dir)
    else:
        _err("  The repo's post-checkout hook wasn't run (it may install): it will be once the")
        _err(f"  volumes are on, by `fy worktree add {shlex.quote(name)}` again.")
    print(f"\n✓ worktree ready: {wt_dir}  (branch {branch})")
    print("  It has no stack, ports or mode of its own: commands in it run in this box.")
    print(f"  Done with it:  fy worktree remove {shlex.quote(name)}")
    print(
        f"  Needs a stack: `fy worktree add {shlex.quote(name)} --box` on the operator's computer."
    )
    return 0


def _uncommitted(wt_dir: Path, name: str, *, save: bool) -> tuple[str, str] | None:
    """``("clean"|"dirty"|"saved", commit id or "")``: the check ``fy worktree remove`` makes on
    your computer (``worktree._UNCOMMITTED_SH``), run here, in the box. None when it fails."""
    who, email = worktree._PARKED_BY
    env = {
        f"GIT_{role}_{k}": v
        for role in ("AUTHOR", "COMMITTER")
        for k, v in (("NAME", who), ("EMAIL", email))
    }
    message = f"fy: uncommitted work in worktree '{name}' at removal"
    cmd = [
        "sh",
        "-c",
        worktree._UNCOMMITTED_SH,
        "fy-uncommitted",
        "save" if save else "check",
        message,
    ]
    got = subprocess.run(cmd, cwd=wt_dir, env={**os.environ, **env}, stdout=subprocess.PIPE)
    return worktree._verdict(got.stdout or b"", save=save) if got.returncode == 0 else None


def _settled(main: Path, wt_dir: Path, name: str, *, force: bool) -> bool:
    """Whether ``wt_dir`` may go: nothing uncommitted in it, or (``force``) it is saved on a ref."""
    from datetime import datetime

    verdict = _uncommitted(wt_dir, name, save=force)
    if verdict is None:
        _err(f"✗ not removing '{name}': its uncommitted work couldn't be checked for or saved.")
        _err("  --discard removes it without: anything uncommitted is lost.")
        return False
    kind, oid = verdict
    if kind == "dirty":
        _err(f"✗ not removing '{name}': it has uncommitted work. Commit it, or pass --force, which")
        _err("  saves it on refs/fy/removed/ first.")
        return False
    if kind == "saved":
        ref = f"refs/fy/removed/{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        if _git(main, "update-ref", ref, oid, "") != 0:  # create-only
            _err(f"✗ not removing '{name}': couldn't save its uncommitted work on {ref} (above).")
            return False
        worktree._say_parked(name, ref)
    return True


def owner(main: Path, name: str, published: frozenset[str] | None) -> str:
    """In a box, whose worktree ``name`` is: ``registered`` (your computer's), ``box`` (a box made
    it), ``plain`` (``git worktree add``, and your computer has no record of it), ``unknown``
    (plain, with no list to tell by) or ``none`` (no worktree of this repository). By ``published``,
    the list your computer publishes (:func:`worktree_published.read`), else by what the box was
    created with, which misses one registered since. It guides this box's verbs; a box's word
    decides nothing on your computer."""
    if name in (published if published is not None else crossmask.baked_names() or ()):
        return "registered"
    wt_dir = config.worktrees_root(main) / name
    if wt_dir.is_symlink() or (marker := _marker(main, wt_dir)) is None:
        return "none"
    if not marker.parent.is_dir():
        return "none"
    if is_box_made(main, wt_dir):
        return "box"
    return "unknown" if published is None else "plain"


def _refuse_removal(name: str, whose: str) -> None:
    there = f"`fy worktree remove {shlex.quote(name)}`"
    if whose == "registered":
        _err(f"✗ '{name}' is registered on your computer, which has its box, stack and adopted")
        _err(f"  config. Remove it there: {there}.")
    elif whose == "unknown":
        _err(f"✗ no box made '{name}', and this box can't tell if your computer registered it:")
        _err("  there is no list in the to-box (no to-box, or an older foldyard there). The")
        _err(f"  supervisor publishes it: `fy host restart`. On your computer: {there}.")
    else:
        _err(f"✗ no worktree '{name}' of this repository in the worktrees root")


def remove(name: str, force: bool = False, discard: bool = False) -> int:
    """``fy worktree remove`` in a box, for one a box made (its checkout and its volumes) or a plain
    ``git worktree add`` one your computer never registered (its checkout). The branch stays.
    Uncommitted work refuses; ``force`` saves it first, ``discard`` doesn't look."""
    if worktree.refuse_name(name):
        return 1
    main = stack.main_repo()
    wt_dir = config.worktrees_root(main) / name
    whose = owner(main, name, worktree_published.read(main))
    if whose not in ("box", "plain"):
        _refuse_removal(name, whose)
        return 1
    if not discard:
        if worktree._refuse_nested(name, worktree._nested_worktrees(main, wt_dir)):
            return 1
        if not _settled(main, wt_dir, name, force=force):
            return 1
    if whose == "plain" and (now := owner(main, name, worktree_published.read(main))) != whose:
        _refuse_removal(name, now)  # registered while its work was checked
        return 1
    vols = volumes(wt_dir, name) if whose == "box" else {}  # a plain one has none
    here = attached(vols)
    facts = _this_box() if vols else ""
    if here and isinstance(facts, str):
        _err(f"✗ not removing '{name}': its volumes can't be unmounted here ({facts})")
        return 1
    if here and not isinstance(facts, str):
        on = {t: v for t, v in vols.items() if t in here}
        refused, results = _helper(*facts, "detach", on)
        stuck = [t for t in on if results.get(t) not in ("detached", "absent", "missing")]
        if refused or stuck:
            _err(f"✗ not removing '{name}': {refused or 'a volume is in use: ' + ', '.join(stuck)}")
            return 1
    # --force either way: git's own check reads the worktree's SHARED index, which a commit made in
    # a box leaves behind (the shim's index is the box's), and the check above read the right one.
    if (rc := _git(main, "worktree", "remove", "--force", str(wt_dir))) != 0:
        if whose == "box":
            mount(wt_dir, name)
        return rc
    if vols and not isinstance(facts, str):
        engine, env, _, _ = facts
        gone = subprocess.run(
            [engine, "volume", "rm", *vols.values()], env=env, capture_output=True, text=True
        )
        if gone.returncode != 0:
            _err(f"⚠ some of its volumes stay ({gone.stderr.strip()})")
    print(f"✓ worktree '{name}' removed (its branch stays)")
    return 0


def refuse_stack(main: Path, name: str, verb: str) -> bool:
    """Refuse (True) a stack verb in box-made worktree ``name``, in a box: no ports or adopted
    config of its own, so the stack would be one nothing on your computer manages. Never on your
    computer: the marker is the box's word (planted in a registered worktree's admin dir, it would
    stop that worktree's `fy up`), and an unregistered worktree has no adopted config to run."""
    if not config.in_box() or not name or not is_box_made(main, config.worktrees_root(main) / name):
        return False
    _err(f"✗ `{verb}`: a box made '{name}', so it has no stack of its own. Register it on")
    _err(f"  your computer first: `fy worktree add {shlex.quote(name)} --box`.")
    return True


def promoted(main: Path, wt_dir: Path) -> None:
    """On your computer, once ``wt_dir`` is registered: an ordinary worktree now, and the read-only
    dirs its volumes sat on are ordinary ones again (an install of yours there works, as in any
    worktree). Every path below ``main`` and the worktrees root is box-writable: no link is
    followed. Best-effort."""
    from . import mountwrite

    marker = _marker(main, wt_dir)
    if marker is None or not is_box_made(main, wt_dir):
        return
    for t in volumes(wt_dir, wt_dir.name):
        try:
            mountwrite.chmod_dir(wt_dir.parent, os.path.relpath(t, wt_dir.parent), 0o755)
        except (OSError, ValueError):
            pass
    try:
        mountwrite.unlink(main, marker.relative_to(main))
    except (OSError, ValueError) as e:
        _err(f"⚠ couldn't clear the box-made marker ({e}): `fy up` in a box refuses it")
