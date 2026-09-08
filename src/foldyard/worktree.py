"""Worktrees — `foldyard worktree add|list|remove` (core foldyard).

A worktree is a host-sibling checkout under the worktrees root that shares the ONE
rootless machine, so a single branch can span every sub-project (platform/data/infra)
for cross-cutting changes. `worktree add` creates it (git worktree + the machine's
worktrees mount + the per-project init hook); `worktree list` shows main + the
worktrees with their branches; `worktree remove` tears down the worktree's dev box +
stack, archives its Claude transcripts to the durable Mac store, then drops the git
worktree and its per-worktree posture/VS Code state (after a confirmation prompt).

Faithful port of the `worktree-add` recipe. The project-specific init step is
config-driven (`[project].worktree_init`, `config.worktree_init()`) so foldyard stays
generic — a fresh consumer with no init script just gets the bare git worktree. The
`wt <name> <recipe>` dispatcher stays a `just` recipe (it re-invokes `just` with
WORKTREE=<name>); it's a just-level concern, not a foldyard verb.

Mac-side: touches git + the rootless machine. Stdlib only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config, machine, stack


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _host_only(verb: str) -> int | None:
    """Worktree add/remove are host-only. The box shares the real ``.git`` (bind-mounted) but
    its worktrees root is a container-local dir the Mac can't see — so an in-box
    ``git worktree add/remove/prune`` edits SHARED metadata against paths that exist on only
    one side, orphaning Mac-side checkouts (dir present, metadata gone: ``git worktree
    remove`` then refuses it forever). None when OK to proceed."""
    if config.in_box():
        _err(f"✗ run on the host (Mac) — in the box, `worktree {verb}` would edit the shared")
        _err("  .git against a box-local worktrees dir and orphan the Mac-side checkout.")
        return 1
    return None


def _registered_worktree(main: Path, wt_dir: Path) -> bool:
    """True iff git still tracks ``wt_dir`` as a linked worktree of ``main``. False means an
    ORPHAN: the checkout dir survives but its ``.git/worktrees`` metadata is gone (e.g. pruned
    from a context that couldn't see the checkout), so ``git worktree remove`` refuses it
    forever ("not a working tree") — the caller must delete the dir + prune instead."""
    out = subprocess.run(
        ["git", "-C", str(main), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return True  # can't tell — proceed as registered and let `git worktree remove` report
    trees = {
        line.removeprefix("worktree ").strip()
        for line in out.stdout.splitlines()
        if line.startswith("worktree ")
    }
    return str(wt_dir) in trees or str(wt_dir.resolve()) in trees


def _snapshot_uncommitted(wt_dir: Path, name: str) -> str | None:
    """Park a worktree's uncommitted work on a ref in MAIN before the checkout is deleted.

    ``worktree remove --force`` (and the orphan path's ``rmtree``, which ALWAYS needs --force)
    discard tracked edits and untracked files with no trace: nothing was ever written to the
    object database, so there is not even a dangling blob to recover. Transcripts are archived
    one step above precisely because destruction is coming; the code they describe was not.

    Written as a commit whose tree is built through a THROWAWAY index, so neither the worktree's
    index nor its HEAD is touched on the way out — this must not be able to change what is being
    removed. ``git add -A`` honours .gitignore, which is what keeps it cheap (no node_modules)
    and is also its one blind spot: a gitignored-but-precious file is not in here.

    The ref lands in the SHARED store (linked worktrees share objects and refs with main), so it
    outlives ``worktree remove``, ``worktree prune`` and a ``gc --prune=now`` — the ref keeps the
    objects reachable. Returns the ref name, or None when there was nothing to save or git could
    not be driven (a pruned orphan's ``.git`` file dangles, and that is exactly the case that
    cannot be rescued — say so rather than pretend).
    """
    import tempfile
    from datetime import datetime

    def _git(*args: str, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(wt_dir), *args], capture_output=True, text=True, **kw
        )

    head = _git("rev-parse", "HEAD")
    if head.returncode != 0:
        return None
    idx = Path(tempfile.mkdtemp(prefix="fy-wt-snap-")) / "index"
    try:
        env = {**os.environ, "GIT_INDEX_FILE": str(idx)}
        if _git("read-tree", "HEAD", env=env).returncode != 0:
            return None
        if _git("add", "-A", env=env).returncode != 0:
            return None
        tree = _git("write-tree", env=env)
        if tree.returncode != 0:
            return None
        # An unchanged tree means there was nothing uncommitted to lose — no ref, no noise.
        if tree.stdout.strip() == _git("rev-parse", "HEAD^{tree}").stdout.strip():
            return None
        made = _git(
            "commit-tree",
            tree.stdout.strip(),
            "-p",
            head.stdout.strip(),
            "-m",
            f"fy: uncommitted work in worktree '{name}' at removal",
        )
        if made.returncode != 0:
            return None
        ref = f"refs/fy/removed/{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        if _git("update-ref", ref, made.stdout.strip()).returncode != 0:
            return None
        return ref
    finally:
        shutil.rmtree(idx.parent, ignore_errors=True)


def _git_has_ref(repo: Path, ref: str) -> bool:
    return (
        subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", ref]).returncode
        == 0
    )


def _branch_of(path: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def _local_state_roots() -> tuple[Path, ...]:
    """The dirs holding per-worktree local state (posture + isolated VS Code), one child per
    worktree name. NOTE the namespace collision these create: ``vscode/worktrees`` (the legacy
    layout's root) is also ``vscode``'s child for a worktree literally named "worktrees" — which
    is why that name is reserved in :func:`add` and guarded in :func:`_remove_local_state`."""
    state = config.state_dir()
    return (
        state / "worktrees",
        state / "vscode",
        state / "vscode" / "worktrees",  # legacy path used before per-worktree dirs were flattened
    )


def _has_local_state(name: str) -> bool:
    return any((root / name).exists() for root in _local_state_roots())


def _remove_local_state(name: str) -> bool:
    """Remove the per-worktree posture and VS Code state after git removes the checkout."""
    roots = _local_state_roots()
    ok = True
    for root in roots:
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or any(
            path == other.resolve() for other in roots
        ):
            # Outside the root (a path-y name), or ON another root (name "worktrees" makes
            # vscode/<name> the legacy layout's root — deleting it would take EVERY worktree's
            # legacy state). Refuse rather than guess.
            _err(f"✗ refusing to remove worktree state outside {root}: {path}")
            ok = False
            continue
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            _err(f"✗ couldn't remove worktree state {path}: {e}")
            ok = False
    return ok


def _git_resolves(repo: Path, ref: str) -> bool:
    """True iff ``ref`` names an existing commit in ``repo`` (any form: ``main``, ``origin/main``,
    a sha …). Distinct from :func:`_git_has_ref`, which needs a full refname to ``show-ref``."""
    return (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
        ).returncode
        == 0
    )


def _default_base_ref(repo: Path) -> str | None:
    """The ref a NEW worktree branch should fork from: the project's DEFAULT branch, not the
    primary checkout's current HEAD (which is usually some other in-flight branch — forking off it
    silently dragged that branch's commits into every new worktree). An explicit
    ``[machine].worktree_base`` / ``FOLDYARD_WORKTREE_BASE`` wins; otherwise auto-detect: the
    remote's advertised default via ``origin/HEAD``, then ``main``/``master`` (remote-or-local).
    Returns None when nothing resolves — the caller then falls back to git's default (current
    HEAD), so an unusual repo (no origin, exotic default branch) still works."""
    override = config.worktree_base()
    if override:
        if _git_resolves(repo, override):
            return override
        # A configured override that no longer resolves must NOT be dropped silently — that would
        # fork the new worktree off the current HEAD (the exact footgun this function exists to
        # avoid). Warn, then fall through to auto-detection (origin/HEAD, main/master) rather than
        # straight to HEAD, mirroring how an unresolvable explicit `--from` is surfaced in add().
        _err(
            f"⚠ configured worktree base '{override}' doesn't resolve in {repo} — "
            "auto-detecting the default branch instead "
            "(fix [machine].worktree_base / FOLDYARD_WORKTREE_BASE to silence this)."
        )
    head = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
    )
    if head.returncode == 0:
        ref = head.stdout.strip().removeprefix("refs/remotes/")
        if ref:
            return ref  # e.g. "origin/main" — the remote's declared default branch
    for cand in ("origin/main", "main", "origin/master", "master"):
        if _git_resolves(repo, cand):
            return cand
    return None


def _seed_keyless_posture(name: str) -> None:
    """Mirror the main checkout's KEYLESS AI-assistant posture (the ``claude``/``codex`` axes) into
    the fresh worktree's posture file, so a keyless box comes up injectable right after
    ``fy worktree add``.

    Why this is needed: posture is per-worktree and a fresh worktree has no ``dev-mode.json``, so
    every axis reads its rung-0 default (off). With ``claude``/``codex`` off, the worktree's own
    egress proxy installs NO token-inject rule on its per-worktree port — so codex/claude in the
    worktree box send the far-future *dummy* token, 401, then try to refresh with the dummy refresh
    token and fail with *"Your access token could not be refreshed."* The real creds are already
    project-shared host-side (the proxy mints them from the Mac's ``~/.codex/auth.json`` /
    ``host.env``); the only missing piece is this worktree's posture being ON. So we seed posture —
    NOT creds (copying the rotating ``auth.json`` into the box would spawn a second, unsynchronised
    refresher and corrupt the token).

    Scope + safety: only the keyless-configured assistant axes, mirroring main's ACTUAL values (off
    stays off), so this never escalates a worktree beyond what main already runs, and never touches
    the credentialed cloud axes (gcp/storage/github/…). A non-keyless developer (no ``[claude]``/
    ``[codex]`` ``keyless`` in their local toml) gets a no-op. Best-effort — a hiccup here must not
    trap the (already-created) worktree."""
    from . import devmode

    try:
        wt_cfg = devmode.worktree_config(name)
        with config.using(wt_cfg):
            configured = (("claude", config.claude_keyless()), ("codex", config.codex_keyless()))
            keyless_axes = [axis for axis, on in configured if on]
        if not keyless_axes:
            return
        with config.using(devmode.worktree_config("")):
            main_mode = devmode.read(apply_expiry=True)["mode"]
        updates = {a: main_mode[a] for a in keyless_axes if main_mode.get(a, "off") != "off"}
        if not updates:
            return
        with config.using(wt_cfg):
            devmode.set_mode(updates, reconcile=False)  # box up applies it; nothing's running yet
        print(
            "▶ seeded keyless posture from main: "
            + ", ".join(f"{a}={v}" for a, v in updates.items())
            + f"  (its box will inject on `WORKTREE={name} fy box up`)"
        )
    except Exception as e:  # posture seeding is a best-effort convenience, never fatal
        print(f"  (couldn't seed keyless posture — set it manually with `fy mode`: {e})")


def add(name: str, branch: str = "", base: str = "") -> int:
    """Create a host-sibling worktree under the worktrees root + init its config.

    A brand-new branch forks off ``base`` (an explicit ``--from``), else the project's default
    branch (:func:`_default_base_ref`) — NOT the primary checkout's current HEAD."""
    if (rc := _host_only("add")) is not None:
        return rc
    if name == "main":
        # "main" is the positional label for the PRIMARY checkout (the repo root) in the workspaces
        # view + its posture dir; a worktree named "main" would show as a second "main" card (and
        # used to collide in posture state). Reserve it so the two are always distinct.
        _err("✗ 'main' is reserved for the primary checkout — pick another worktree name")
        return 1
    if name == "worktrees":
        # "worktrees" collides with the legacy VS Code state layout: vscode/<name> for this name
        # IS the legacy root (see _local_state_roots), so removing the worktree's state would
        # delete every worktree's legacy state. Reserve it outright.
        _err("✗ 'worktrees' is reserved (collides with foldyard's state layout) — pick another")
        return 1
    path = Path(name)
    if path.is_absolute() or len(path.parts) != 1 or name in ("..", "."):
        # Must be a single plain relative path component: devmode.worktree_keys() discovers
        # worktrees via one level of wt_root.iterdir(), so a nested name like "feat/x" would
        # silently never be discovered (the intermediate "feat" dir has no .git of its own) —
        # the worktree would exist on disk but every posture/status/devbox-up check would treat
        # it as absent. vscode.py's _user_data_dir() independently rejects the same shape for
        # the same single-path-component reason. An absolute name (e.g. "/", "/etc") is worse:
        # Path("/").parts has length 1, and `wt_root / name` for an absolute `name` DISCARDS
        # wt_root entirely (pathlib joins an absolute right-hand side by replacing the base) —
        # so this would point git worktree operations at an arbitrary filesystem path.
        _err(f"✗ '{name}' must be a single relative path component — pick another")
        return 1
    main = stack.main_repo()
    wt_root = config.worktrees_root(main)
    branch = branch or f"wt/{name}"
    wt_dir = wt_root / name
    if wt_dir.exists():
        _err(f"✗ already exists: {wt_dir}")
        return 1
    wt_root.mkdir(parents=True, exist_ok=True)
    # Reproduce the recipe's machine-mount warning: worktree stacks need the rootless
    # machine to mount the worktrees root (no-op in the box / where there's no podman).
    machine.ensure(main, wt_root)

    print(f"▶ git worktree add {wt_dir} (branch {branch})…")
    # New branch if it doesn't exist yet, else check out the existing one (local or remote).
    # `--from`/base only applies to the new-branch path; if the branch already exists we're
    # checking it out, so a passed base can't take effect — say so instead of dropping it silently.
    if base and (
        _git_has_ref(main, f"refs/heads/{branch}")
        or _git_has_ref(main, f"refs/remotes/origin/{branch}")
    ):
        print(f"  note: --from {base} ignored — branch '{branch}' already exists (checking it out)")
    if _git_has_ref(main, f"refs/heads/{branch}"):
        argv = ["worktree", "add", str(wt_dir), branch]
    elif _git_has_ref(main, f"refs/remotes/origin/{branch}"):
        argv = ["worktree", "add", "-b", branch, str(wt_dir), f"origin/{branch}"]
    else:
        # New branch → fork off the DEFAULT branch (or an explicit --from), not the primary
        # checkout's current HEAD (which is usually some other in-flight branch).
        if base and not _git_resolves(main, base):
            _err(f"✗ --from base '{base}' doesn't resolve in {main}")
            return 1
        ref = base or _default_base_ref(main)
        # --no-track: forking off origin/<default> must NOT record it as the new branch's
        # upstream (git's autoSetupMerge does when the start point is remote-tracking).
        # Git GUIs implement "push" as push-to-tracking-branch, so a tracked origin/main
        # turns a routine branch push into a direct push TO main (bitten twice).
        argv = ["worktree", "add", "--no-track", "-b", branch, str(wt_dir)] + ([ref] if ref else [])
        print(f"  new branch off {ref or f'current HEAD ({_branch_of(main)})'}")
    rc = subprocess.run(["git", "-C", str(main), *argv]).returncode
    if rc != 0:
        _err("✗ git worktree add failed")
        return rc

    _init_in_yard(main, wt_dir)

    _seed_keyless_posture(name)

    print(f"\n✓ worktree ready: {wt_dir}  (branch {branch})")
    print(f"  bring its stack up:  WORKTREE={name} fy up")
    return 0


def init_config(name: str) -> int:
    """Run the consumer's ``[project].worktree_init`` script for ``name`` — INSIDE THE YARD.

    The script's whole job is copying each project's gitignored local config (env files, editor
    and agent settings) from the source checkout into the new one. Both live in the mount, so it
    needs no host privileges at all — and it must not have them: it's a repo file (Tangible's fans
    out to a `.mjs` and another `.sh`, each free to read more), so running it on the host made
    `fy worktree add` a host-code-execution trigger for whatever the checkout happened to contain.
    A container with only the mount gives it exactly the reach its job needs
    (ADR-0023).

    Returns 0 when it ran (or there's nothing to run). Non-fatal by design: a config-copy hiccup
    must not strand the already-created worktree, and when the engine or box image isn't available
    yet this SKIPS with the command to run later rather than falling back to the host."""
    main = stack.main_repo()
    wt_dir = config.worktrees_root(main) / name
    if not wt_dir.is_dir():
        _err(f"✗ no worktree at {wt_dir} — `fy worktree add {name}` first")
        return 1
    return _init_in_yard(main, wt_dir)


def _init_in_yard(main: Path, wt_dir: Path) -> int:
    """The container run behind :func:`init_config` — shared with ``add``, which has just created
    ``wt_dir`` via git and so needs no existence check."""
    init = config.worktree_init()
    if not init:
        return 0
    script = Path(init)
    if not script.is_absolute():
        script = main / script
    # The container sees only the repo + worktrees root, so a path outside them can't resolve there
    # anyway — but failing HERE turns a baffling in-container error into the actual problem, and
    # keeps the invariant explicit: `worktree_init` names a file in the repo, nothing else.
    if not script.resolve().is_relative_to(main.resolve()):
        _err(f"✗ [project].worktree_init must point inside the repo, not {script}")
        return 1
    if not script.is_file():
        print(f"  (no {script} — nothing to initialise)")
        return 0

    from . import box

    engine, env = box.engine_env()
    image = config.box_image()["tag"]
    if not box.image_exists(engine, image, env):
        _err(f"⚠ worktree config not initialised: no {image} image to run it in.")
        _err(f"    build it, then re-run:  fy box build && fy worktree init {wt_dir.name}")
        return 0
    wt_root = config.worktrees_root(main)
    print(f"▶ initialising worktree config in the yard ({image})…")
    cmd = [
        engine,
        "run",
        "--rm",
        "-v",
        f"{main}:{main}",
        "-v",
        f"{wt_root}:{wt_root}",
        "-w",
        str(wt_dir),
        # The script is `sh`, and the image's own entrypoint (a login shell, a supervisor) would
        # mangle its args — so name the interpreter explicitly.
        "--entrypoint",
        "sh",
        image,
        str(script),
        "--source",
        str(main),
    ]
    print("  $ " + " ".join(cmd))
    if subprocess.run(cmd, env=env).returncode != 0:
        print("  (worktree init reported problems — continuing)")
    return 0


def remove(name: str, force: bool = False, assume_yes: bool = False) -> int:
    """Remove a host-sibling worktree and ALL its local state: tear down its dev box and its
    stack (compose down + drop the worktree's volumes; shared caches kept), archive its configured
    agents' transcripts to the durable Mac store, then ``git worktree remove`` the checkout.
    Interactive confirmation unless ``assume_yes``/``force``. ``--force`` also removes when
    archiving fails or the tree is dirty/locked (passed through to git). Transcripts are archived
    BEFORE git deletes the worktree, since that takes the fragile bound-out transcript dirs with it.

    Also handles the ORPHANED shape (checkout dir present, git metadata gone — see
    :func:`_registered_worktree`): under ``--force`` the dir is deleted directly +
    ``git worktree prune``, since ``git worktree remove`` can't remove what git no longer
    tracks."""
    from . import box, transcripts

    if (rc := _host_only("remove")) is not None:
        return rc
    main = stack.main_repo()
    wt_root = config.worktrees_root(main)
    wt_dir = wt_root / name
    if not (wt_dir / ".git").exists():
        # A previous remove got the checkout but left local state and/or the box's shadow
        # volumes behind (or the tree was deleted out-of-band). Make the removal retryable:
        # finish the cleanup instead of erroring out with the leftovers stuck forever.
        leftovers = False
        if _has_local_state(name):
            print(f"▶ no checkout at {wt_dir} — removing leftover local state for '{name}'…")
            if not _remove_local_state(name):
                return 1
            leftovers = True
        # resolve() refuses a missing worktree dir, so name the project explicitly.
        if stack.remove_devbox_volumes(project=f"{config.project_prefix()}-{name}"):
            leftovers = True
        if leftovers:
            print(f"✓ worktree '{name}' leftovers deleted.")
            return 0
        _err(f"✗ no worktree named '{name}' under {wt_root}")
        return 1

    project = f"{config.project_prefix()}-{name}"
    branch = _branch_of(wt_dir)
    orphaned = not _registered_worktree(main, wt_dir)
    if orphaned and not force:
        _err(f"✗ '{name}' is ORPHANED — the checkout exists at {wt_dir} but git no longer")
        _err("  tracks it as a worktree (metadata pruned?), so `git worktree remove` can't")
        _err("  take it. Re-run with --force to delete the directory instead (any uncommitted")
        _err("  changes in it are lost).")
        return 1

    print(f"This PERMANENTLY removes worktree '{name}' at {wt_dir}:")
    print(f"  1. stop + remove its dev box container ({project}-devbox) + its shadow volumes")
    print(f"  2. tear down its stack ({project}): compose down + drop its volumes")
    print("       (postgres + GCS/BigQuery emulator data; SHARED pnpm/Playwright caches are kept)")
    print("  3. archive its configured agent transcripts to the durable Mac store")
    if orphaned:
        print("  4. delete the ORPHANED checkout dir (git no longer tracks it) + worktree prune")
    else:
        print("  4. git worktree remove — the checkout directory is deleted")
    print("  5. delete its local posture + isolated VS Code state")
    if branch:
        print(f"  The branch '{branch}' is KEPT (delete it later with: git branch -D {branch}).")
    if not (assume_yes or force):
        try:
            answer = input("Proceed? (y/N): ").strip()
        except EOFError:
            answer = ""
        if answer not in ("y", "Y"):
            print("Aborted.")
            return 0

    # Target this worktree for the box/stack teardown — both resolve via the WORKTREE env var.
    # The checkout still exists here (we git-remove last), so resolve() finds it.
    os.environ["WORKTREE"] = name

    # 1. Dev box first: it holds the worktree's bind mount, so git can't remove the tree out
    #    from under a running box. Best-effort — a teardown hiccup shouldn't trap the worktree.
    box.main("down")
    # 2. Stack: compose down + drop this worktree's prefixed volumes (keeps the shared caches).
    stack.nuke()
    # The box's per-box shadow volumes survive `box down` by design (warm restarts) and carry
    # a hyphenated `{project}-devbox-` prefix nuke's `{project}_` compose filter misses — on
    # REMOVAL they'd leak forever, so drop them explicitly.
    stack.remove_devbox_volumes()

    # 3. Save configured agents' transcripts BEFORE git deletes their bound-out dirs.
    archive_env = {"FOLDYARD_CHECKOUT": str(wt_dir), "HERE": config.dev_vm_rel()}
    rc = transcripts.sync_current(archive_env, what=f"worktree '{name}' transcripts")
    if rc != 0 and not force:
        _err(f"✗ refusing to remove worktree '{name}' — its transcripts were NOT archived.")
        _err("  Resolve the issue, or re-run with --force to remove anyway (loses them).")
        return rc

    # 3b. Park any uncommitted work on a ref in main BEFORE the checkout is deleted. Best-effort
    #     and never blocking, unlike the transcript archive above: both paths that reach step 4
    #     with work still in the tree are explicit --force (git itself refuses the non-force
    #     removal of a dirty worktree), so the operator has already accepted the loss — this is a
    #     net under them, not a promise they were given.
    saved = _snapshot_uncommitted(wt_dir, name)
    if saved:
        print(f"▶ uncommitted work in '{name}' saved to {saved}")
        print(f"     inspect:  git -C {main} show --stat {saved}")
        print(f"     restore:  git -C {main} checkout {saved} -- .")
    # 4. git worktree remove — or, for an orphan git no longer tracks, delete the dir + prune
    #    the stale metadata so a re-add of the same name starts clean.
    if orphaned:
        print(f"▶ deleting orphaned checkout {wt_dir} + git worktree prune…")
        try:
            shutil.rmtree(wt_dir)
        except OSError as e:
            _err(f"✗ couldn't delete {wt_dir}: {e}")
            return 1
        rc = subprocess.run(["git", "-C", str(main), "worktree", "prune"]).returncode
        if rc != 0:
            _err("✗ git worktree prune failed — the checkout dir was deleted, but stale")
            _err(f"  worktree metadata may remain (re-run: git -C {main} worktree prune).")
            return rc
    else:
        print(f"▶ git worktree remove {wt_dir}…")
        argv = ["worktree", "remove", *(["--force"] if force else []), str(wt_dir)]
        rc = subprocess.run(["git", "-C", str(main), *argv]).returncode
        if rc != 0:
            _err("✗ git worktree remove failed (uncommitted changes? use --force).")
            return rc
    # 5. The checkout is gone, so its posture and isolated VS Code process state are stale too.
    # Keep shared identity/allow state and the main checkout's VS Code profile untouched.
    if not _remove_local_state(name):
        _err(f"✗ worktree '{name}' was removed, but some local state could not be deleted.")
        return 1
    print(
        f"✓ worktree '{name}' removed "
        "(box + stack torn down, transcripts archived, local state deleted)."
    )
    if saved:
        print(f"  Its uncommitted work is on {saved} — see above to restore.")
    return 0


def list_() -> int:
    """List main + each worktree with its branch (lightweight; no engine needed)."""
    main = stack.main_repo()
    wt_root = config.worktrees_root(main)
    print(f"  {'main':<18} {_branch_of(main):<28} {main}")
    if wt_root.is_dir():
        for d in sorted(wt_root.iterdir()):
            if d.is_dir() and (d / ".git").exists():
                print(f"  {d.name:<18} {_branch_of(d):<28} {d}")
    return 0
