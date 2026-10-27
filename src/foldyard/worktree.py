"""Worktrees — `foldyard worktree add|list|remove` (core foldyard).

A worktree is a host-sibling checkout under the worktrees root that shares the ONE
rootless machine, so a single branch can span every sub-project (platform/data/infra)
for cross-cutting changes. `worktree add` creates it (git worktree + the machine's
worktrees mount + the per-project init hook); `worktree list` shows main + the
worktrees with their branches; `worktree remove` tears down the worktree's dev box +
stack, archives its Claude transcripts to the durable host store, then drops the git
worktree and its per-worktree posture/VS Code state (after a confirmation prompt).

Faithful port of the `worktree-add` recipe. The project-specific init step is
config-driven (`[project].worktree_init`, `config.worktree_init()`) so foldyard stays
generic — a fresh consumer with no init script just gets the bare git worktree. The
`wt <name> <recipe>` dispatcher stays a `just` recipe (it re-invokes `just` with
WORKTREE=<name>); it's a just-level concern, not a foldyard verb.

Host-side: touches git + the rootless machine. Stdlib only.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import boxmounts, config, machine, stack, worktree_registry


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _host_only(verb: str) -> int | None:
    """Worktree add/remove are host-only. The box shares the real ``.git`` (bind-mounted) but
    its worktrees root is a container-local dir the host can't see — so an in-box
    ``git worktree add/remove/prune`` edits SHARED metadata against paths that exist on only
    one side, orphaning host-side checkouts (dir present, metadata gone: ``git worktree
    remove`` then refuses it forever). None when OK to proceed."""
    if config.in_box():
        _err(f"✗ run on your computer — in the box, `worktree {verb}` would edit the shared")
        _err("  .git against a box-local worktrees dir and orphan the checkout on your computer.")
        return 1
    return None


# ── foldyard's own git on your computer runs none of the repo's code ──────────────────
#
# The repo's hooks live in the SHARED `.git` every box can write: `.git/hooks/*` and, since git
# 2.54, `[hook "<name>"]` entries in `.git/config`. So a host-side `git worktree add` ran a
# consumer's post-checkout hooks on the host, in the new checkout, before it was registered.
# `core.hooksPath=/dev/null` turns off the hooks DIRECTORY only — git's docs still call it "all
# hooks", but a config hook runs straight through it — so each config hook is also disabled by
# name (`hook.<name>.enabled=false`) — the names from every included file, whatever its condition
# (:func:`_config_hooks`). Those go through GIT_CONFIG_COUNT rather than `-c`, whose
# `key=value` splits at the first `=`, which a hook's name may contain. `core.fsmonitor=` (EMPTY,
# not `false`: a git before 2.35.2 reads `false` as a hook path, and one before 2.26 then reports
# no changes, which `worktree remove`'s dirty check would believe) stops the command the shared
# config can name for every index refresh. Filter drivers (`filter.<d>.smudge` / `clean` /
# `process`, LFS's among them) have no such switch, and turning them off would change what a
# checkout contains — so this git never checks files out: `add` does the metadata step here and
# the checkout in a container (`_checkout_in_yard`). They still run wherever this git HASHES a
# file of the checkout (`remove`'s dirty check, the snapshot's `add -A`). See docs/git.md.
_NO_REPO_CODE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=")


class HookScanError(Exception):
    """A config file git would read that foldyard couldn't check for hooks — so it runs no git."""


_MAX_INCLUDE_DEPTH = 10  # git's own: deeper than this, git itself refuses the config
_REPO_SCOPES = ("local", "worktree")  # the config files in the repo's .git, which the box writes


def _git_config_lines(*args: str) -> list[str]:
    """``git config -z <args…> --list``'s NUL-separated fields. Plain ``git config`` runs nothing
    from the config it reads. Raises :class:`HookScanError` when git can't read it."""
    got = subprocess.run(["git", *args, "-z", "--list"], capture_output=True, text=True)
    if got.returncode != 0:
        raise HookScanError(got.stderr.strip() or f"`git {' '.join(args)} --list` failed")
    return got.stdout.split("\0")


def _add_hook(hooks: dict[str, set[str]], key: str, value: str) -> None:
    if not key.startswith("hook."):
        return
    name, _, var = key.removeprefix("hook.").rpartition(".")
    if not name or var not in ("command", "event"):
        return
    events = hooks.setdefault(name, set())
    if var == "event" and value:
        events.add(value)


def _include_target(raw: str, includer: Path) -> Path | None:
    """Where git would look for an include's ``path`` value: ``~/`` expanded, a relative path
    taken from the INCLUDING file's directory. None for a form not resolved here (``%(prefix)/``,
    an unknown ``~user``)."""
    path = os.path.expanduser(raw)
    if path.startswith(("~", "%(")):
        return None
    return Path(path) if os.path.isabs(path) else includer.parent / path


def _scan_file(
    path: Path, repo_scope: bool, hooks: dict[str, set[str]], seen: set[str], depth: int = 0
) -> None:
    """``path``'s hooks into ``hooks``, then every file it includes, WHATEVER the include's
    condition: a child git meets conditions the host's own git doesn't (`gitdir:` under
    ``.git/worktrees/<name>``, ``onbranch:`` on the new branch). A file in the repo's own config
    chain that can't be read refuses (:class:`HookScanError`): it is one the box could fill in
    the moment this check has passed. One the operator's own config names is git's to ignore."""
    real = os.path.realpath(path)
    if real in seen:
        return
    if depth > _MAX_INCLUDE_DEPTH:
        raise HookScanError(f"git config includes nest deeper than {_MAX_INCLUDE_DEPTH} at {path}")
    if not os.path.isfile(real) or not os.access(real, os.R_OK):
        if repo_scope:
            raise HookScanError(f"the repo's git config includes {path}, which can't be read")
        return
    seen.add(real)
    for entry in _git_config_lines("config", "-f", str(path), "--no-includes"):
        key, _, value = entry.partition("\n")
        _add_hook(hooks, key, value)
        if value and (
            key == "include.path" or (key.startswith("includeif.") and key.endswith(".path"))
        ):
            target = _include_target(value, path)
            if target is None:
                if repo_scope:
                    raise HookScanError(f"the repo's git config includes {value!r}, from {path}")
                continue
            _scan_file(target, repo_scope, hooks, seen, depth + 1)


def _config_hooks(repo: Path) -> dict[str, set[str]]:
    """Every config-defined hook git could run for ``repo`` — in its own git, or in a child git it
    starts in another context (``git worktree add``'s, in the new worktree): name → the events
    it is set for. Each file git reads there (``--show-origin``), then every include target in
    them whatever its condition (:func:`_scan_file`). Raises :class:`HookScanError` rather than
    miss one. Empty on a git without config hooks — and harmless to over-count: disabling a name
    that never runs changes nothing."""
    try:
        fields = _git_config_lines(
            "-C", str(repo), "config", "--show-scope", "--show-origin", "--name-only"
        )
    except HookScanError as e:
        if "show-scope" not in str(e):
            raise
        raise HookScanError(
            "this git can't say which config files it reads (`--show-scope`), so they can't be "
            "checked for hooks — it needs git 2.26 or newer"
        ) from e
    hooks: dict[str, set[str]] = {}
    files: dict[Path, bool] = {}
    for scope, origin, key in zip(fields[0::3], fields[1::3], fields[2::3], strict=False):
        _add_hook(hooks, key, "")  # names from every scope, the command line's included
        if origin.startswith("file:"):
            path = Path(origin.removeprefix("file:"))
            path = path if path.is_absolute() else repo / path
            files[path] = files.get(path, False) or scope in _REPO_SCOPES
    seen: set[str] = set()
    for path, repo_scope in files.items():
        _scan_file(path, repo_scope, hooks, seen)
    return hooks


def _hookless_env(repo: Path, hooks: dict[str, set[str]] | None = None) -> dict[str, str]:
    """The environment for a host git run in ``repo`` with each of its config hooks disabled —
    appended after any GIT_CONFIG_COUNT entries the caller's own environment already carries.
    Children inherit it, so a hook only a child would see is off there too."""
    env = dict(os.environ)
    count = env.get("GIT_CONFIG_COUNT", "")
    n = int(count) if count.isdigit() else 0
    for name in sorted(_config_hooks(repo) if hooks is None else hooks):
        env[f"GIT_CONFIG_KEY_{n}"] = f"hook.{name}.enabled"
        env[f"GIT_CONFIG_VALUE_{n}"] = "false"
        n += 1
    env["GIT_CONFIG_COUNT"] = str(n)
    return env


def _host_git(
    repo: Path, *args: str, env: dict[str, str] | None = None, **kw
) -> subprocess.CompletedProcess:
    """``git -C repo args…`` with none of the repo's hooks or fsmonitor run (see above). For every
    host-side git here that can reach one: a checkout, a ref update, an index refresh. ``env``
    defaults to :func:`_hookless_env`; a caller passing its own builds it from that. When the
    repo's config can't be checked for hooks, git doesn't run: a failed run (1), the reason said."""
    if env is None:
        try:
            env = _hookless_env(repo)
        except HookScanError as e:
            _refuse_unscanned(e)
            return subprocess.CompletedProcess(args, 1, "", "")
    return subprocess.run(["git", *_NO_REPO_CODE, "-C", str(repo), *args], env=env, **kw)


def _refuse_unscanned(e: HookScanError) -> None:
    _err(f"✗ not running git on your computer: {e}.")
    _err("  foldyard checks every git config file it would read for hooks, and couldn't check")
    _err("  that one. Fix or remove that include in .git/config, then retry.")


def _hooks_skipped(repo: Path, events: tuple[str, ...], hooks: dict[str, set[str]]) -> list[str]:
    """Which of ``events`` git would have run a hook for in ``repo`` — an executable file in its
    hooks directory, or one of the config ``hooks`` — so skipping them can be SAID. A report."""
    try:
        where = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-path", "hooks"],
            capture_output=True,
            text=True,
        )
        hooks_dir = Path(where.stdout.strip()) if where.returncode == 0 else None
    except OSError:
        hooks_dir = None
    configured = set().union(*hooks.values())
    return [
        event
        for event in events
        if event in configured
        or (
            hooks_dir is not None
            and os.access(hooks_dir / event, os.X_OK)
            and (hooks_dir / event).is_file()
        )
    ]


def _git_worktree_add(main: Path, argv: list[str]) -> int:
    """``git worktree add --no-checkout`` with the repo's hooks off, saying so when it had any:
    the METADATA alone — the branch (and its upstream, for a remote one), the worktree's admin dir
    and HEAD. No file is written, no index, so no filter driver can run; the checkout is
    :func:`_checkout_in_yard`'s."""
    argv = [*argv[:2], "--no-checkout", *argv[2:]]
    try:
        hooks = _config_hooks(main)  # once: what is disabled is what is reported
    except HookScanError as e:
        _refuse_unscanned(e)
        return 1
    skipped = _hooks_skipped(main, ("post-checkout", "reference-transaction"), hooks)
    rc = _host_git(main, *argv, env=_hookless_env(main, hooks)).returncode
    if rc == 0 and skipped:
        print(
            f"  skipped the repo's git hooks ({', '.join(skipped)}): they live in the .git every "
            "box can write, so foldyard's git on your computer runs none of them. Git in the "
            "worktree's box runs them as usual; setup every new worktree needs belongs in "
            "[project].worktree_init, which runs in a container."
        )
    return rc


# ── the checkout runs in the yard ─────────────────────────────────────────────────────────────
#
# A checkout writes files, and git runs the filter drivers the shared config names (through
# `.gitattributes` or `.git/info/attributes`) for each one it writes — a command the box can plant,
# which no switch turns off. So the host's `git worktree add` stops at the metadata
# (`_git_worktree_add`) and the files + index come from the box image's own git in a one-off
# container, where those commands are the yard's to run — the same yard `[project].worktree_init`
# runs in. The image's git, not foldyard's shim (that is installed into a running box, never into
# the image): it writes the worktree's SHARED index, `<main>/.git/worktrees/<name>/index`, the one
# the box's shim seeds its own index-box from on first use (ADR-0021). That index carries the VM's
# stat data, so the first `git status` on your computer re-reads each file once and refreshes it;
# the box starts with an index that already matches its view of the files.
#
# The commit is resolved HERE and handed over: the VM can read a ref your computer just replaced
# as older or absent (ADR-0021's visibility record), and `read-tree` moves no ref at all, so the
# container never decides what is checked out and never writes a ref or `.git/config`. A checkout
# that fails takes the worktree (and a branch `add` just made) with it: nothing is half-made.


def _add_command(name: str, branch: str, base: str) -> str:
    """The ``fy worktree add`` the operator ran, to say how to run it again."""
    return shlex.join(
        [
            "fy",
            "worktree",
            "add",
            name,
            *([branch] if branch else []),
            *(["--from", base] if base else []),
        ]
    )


def _checkout_yard(again: str) -> tuple[str, dict, str] | None:
    """``(engine, env, image)`` to run the checkout in — or None, said, when there is no image to
    run it in yet. Asked BEFORE any git runs, so a refusal leaves nothing behind. Deliberately not a
    fallback to a checkout here: that is the filter drivers running on your computer again."""
    from . import box

    engine, env = box.engine_env()
    image = config.box_image()["tag"]
    if box.image_exists(engine, image, env):
        return engine, env, image
    _err(f"✗ no {image} image in the VM to check the new worktree out in.")
    _err("  foldyard checks a worktree's files out in a container, never on your computer: the")
    _err("  shared .git can name filter drivers, and git runs them for every file it checks out.")
    _err(f"  Build the image (or start the VM), then re-run:  fy box build && {again}")
    return None


def _checkout_in_yard(
    main: Path, wt_dir: Path, branch: str, yard: tuple[str, dict, str], again: str, *, made: bool
) -> int:
    """Populate ``wt_dir`` — its files and its index — from ``branch``'s commit, in a container
    (see above). On failure, undo the metadata step (and the branch, when ``made`` by it)."""
    engine, env, image = yard
    ref = f"refs/heads/{branch}^{{commit}}"
    got = subprocess.run(
        ["git", "-C", str(main), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
    )
    oid = got.stdout.strip() if got.returncode == 0 else ""
    rc = 1
    if oid:
        cmd = _checkout_cmd(engine, image, main, wt_dir, oid)
        print(f"▶ checking the worktree out in the VM ({image}, commit {oid[:12]})…")
        print("  $ " + shlex.join(cmd))
        rc = subprocess.run(cmd, env=env).returncode
    if rc == 0 and _index_on_host(wt_dir):
        _say_lfs(main, oid)
        return 0
    if not oid:
        _err(f"✗ couldn't read the commit branch '{branch}' points at")
    elif rc in (126, 127):
        _err(f"✗ the checkout couldn't start git in {image}: no git there? [box].image must")
        _err("  provide git — the box needs it too (ADR-0014).")
    elif rc == 0:
        # An engine that makes a missing bind source (Docker's does) checked out onto the VM's
        # own disk: nothing reached your computer.
        _err(f"✗ the checkout ran in the VM, but its files never reached {wt_dir}: does the VM")
        _err(f"  mount {config.worktrees_root(main)}? (`fy doctor` says; `fy machine recreate`)")
    else:
        _err("✗ checking the worktree out in the VM failed (above).")
    _undo_add(main, wt_dir, branch, oid if made else "")
    _err(f"  Nothing was kept. Once that's fixed, run it again:  {again}")
    return 1


def _checkout_cmd(engine: str, image: str, main: Path, wt_dir: Path, oid: str) -> list[str]:
    """The one-off container that writes ``wt_dir``'s files and index from ``oid``: the image's
    own git, the two mounts ``worktree_init`` gets (at the paths your computer uses, which the
    worktree's ``.git`` file names), the box's uid (the VM user, so the files are owned as the
    box's are), no capabilities, no network. ``read-tree --reset -u`` moves no ref and runs no
    checkout hook — it writes the files and the index, nothing else."""
    wt_root = config.worktrees_root(main)
    return [
        engine,
        "run",
        "--rm",
        "--user",
        "0",
        "--cap-drop",
        "ALL",
        "--network",
        "none",
        "--security-opt",
        "label=disable",
        "-v",
        f"{main}:{main}",
        "-v",
        f"{wt_root}:{wt_root}",
        "-w",
        str(wt_dir),
        # No network, so Git LFS writes its pointer files (`git lfs pull` in the box fetches the
        # content), and an image without git-lfs doesn't fail the checkout.
        "-e",
        "GIT_LFS_SKIP_SMUDGE=1",
        "--entrypoint",
        "git",
        image,
        "-c",
        "filter.lfs.required=false",
        "read-tree",
        "--reset",
        "-u",
        oid,
    ]


def _index_on_host(wt_dir: Path) -> bool:
    """Whether the worktree's index — the checkout's last write — is there on YOUR side of the
    mount. Its admin dir comes from the ``.git`` file git just wrote, read without following a
    link or blocking (the worktrees root is box-writable: a FIFO there must not hang the add)."""
    try:
        fd = os.open(wt_dir / ".git", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        text = os.read(fd, 4096).decode(errors="replace")
    finally:
        os.close(fd)
    gitdir = Path(text.removeprefix("gitdir:").strip())
    return (wt_dir / gitdir / "index").is_file()  # `/` keeps an absolute gitdir as it is


def _undo_add(main: Path, wt_dir: Path, branch: str, made_at: str) -> None:
    """Drop the worktree the metadata step made — ``--force``, which skips the dirty check (a
    ``git status``, which would hash files) — and the branch it made, while that is still where it
    was made (``made_at``). Says what it couldn't."""
    if _host_git(main, "worktree", "remove", "--force", str(wt_dir)).returncode != 0:
        _err("  ✗ couldn't remove the half-made worktree:")
        _err(f"    git -C {main} worktree remove --force {wt_dir}")
    if not made_at:
        return
    now = subprocess.run(
        ["git", "-C", str(main), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if now != made_at or _host_git(main, "branch", "-q", "-D", branch).returncode != 0:
        _err(f"  ✗ kept the branch '{branch}' it had made (delete it: git branch -D {branch})")


def _say_lfs(main: Path, oid: str) -> None:
    """Name the Git LFS pointers the checkout left (it had no network), when the commit's root
    ``.gitattributes`` routes anything through LFS. Read as a blob: ``cat-file`` runs no filter."""
    attrs = subprocess.run(
        ["git", "-C", str(main), "cat-file", "blob", f"{oid}:.gitattributes"],
        capture_output=True,
        text=True,
    )
    if attrs.returncode == 0 and "filter=lfs" in attrs.stdout:
        print("  Git LFS files are pointers for now (the checkout ran without network): fetch them")
        print("  in the worktree's box with `git lfs pull`.")


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

    try:  # `add -A` refreshes an index, `update-ref` is a ref transaction
        quiet = _hookless_env(wt_dir)
    except HookScanError as e:
        # The scan here is the WORKTREE's context (its config.worktree too), while `git worktree
        # remove` scans from main — and the orphan path deletes the tree with no git at all — so
        # a `--force` removal can still go ahead. Say the net isn't there.
        _err(f"⚠ uncommitted work in '{name}' NOT saved: {e} — foldyard runs no git there.")
        return None

    def _git(*args: str, env: dict[str, str] = quiet) -> subprocess.CompletedProcess:
        return _host_git(wt_dir, *args, env=env, capture_output=True, text=True)

    head = _git("rev-parse", "HEAD")
    if head.returncode != 0:
        return None
    idx = Path(tempfile.mkdtemp(prefix="fy-wt-snap-")) / "index"
    try:
        env = {**quiet, "GIT_INDEX_FILE": str(idx)}
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
    return worktree_registry.is_recorded(stack.main_repo(), name) or any(
        (root / name).exists() for root in _local_state_roots()
    )


def _remove_local_state(name: str) -> bool:
    """Remove the per-worktree posture and VS Code state — and the host's record of the worktree —
    after git removes the checkout."""
    worktree_registry.unregister(stack.main_repo(), name)
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


def _has_from_box_slot(name: str) -> bool:
    try:
        slot = boxmounts.from_box_dir() / boxmounts.slot_rel(name)
    except ValueError:
        return False
    return os.path.lexists(slot)


def _remove_from_box_slot(name: str) -> bool:
    """Delete the worktree's from-box slot, never through a link a box planted
    (:func:`boxmounts.remove_slot`). Its transcripts were archived first."""
    try:
        why = boxmounts.remove_slot(name)
    except ValueError:
        return True  # a name no slot can carry never had one
    if why:
        _err(f"✗ couldn't remove the worktree's from-box slot: {why}")
        return False
    return True


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
    project-shared host-side (the proxy mints them from the host's ``~/.codex/auth.json`` /
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
            "▶ seeded keyless mode from main: "
            + ", ".join(f"{a}={v}" for a, v in updates.items())
            + f"  (its box will inject on `WORKTREE={name} fy box up`)"
        )
    except Exception as e:  # posture seeding is a best-effort convenience, never fatal
        print(f"  (couldn't seed keyless mode — set it manually with `fy mode`: {e})")


def _branch_name_ok(name: str) -> bool:
    """Whether git would take ``name`` as a branch (`git check-ref-format --branch`'s rules),
    checked in-process: it only gates a hint, so it costs no git call."""
    parts = name.split("/")
    return not (
        not name
        or name == "HEAD"
        or name.startswith("-")
        or name.endswith(".")
        or re.search(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{", name)
        or any(not p or p.startswith(".") or p.endswith(".lock") for p in parts)
    )


def add(name: str, branch: str = "", base: str = "", assume_yes: bool = False) -> int:
    """Create a host-sibling worktree under the worktrees root, record it in the host's
    worktree registry, and init its config.

    A brand-new branch forks off ``base`` (an explicit ``--from``), else the project's default
    branch (:func:`_default_base_ref`) — NOT the primary checkout's current HEAD.

    On a worktree that ALREADY exists (one from before the registry, or made with plain ``git
    worktree add``) it registers instead, once the operator has confirmed its real path — the one
    way a worktree comes (back) under host management. See :mod:`foldyard.worktree_registry`."""
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
        # The usual slip is a branch name ("sec/x") where the DIR name goes: suggest both. The
        # branch is the name's own parts rejoined ("a//b/" → "a/b"), and only one git would
        # accept is ever suggested: a hint that can't succeed is worse than none.
        if not path.is_absolute() and not {".", ".."} & set(name.split("/")):
            dashed = "-".join(path.parts)
            suggested = branch or "/".join(path.parts)
            # A dir starting with "-" would be read as an option, so that hint couldn't run either.
            if _branch_name_ok(suggested) and not dashed.startswith("-"):
                hint = f"fy worktree add {shlex.quote(dashed)} {shlex.quote(suggested)}"
                _err(f"  the first argument is the worktree's directory — did you mean `{hint}`?")
        return 1
    main = stack.main_repo()
    wt_root = config.worktrees_root(main)
    again = _add_command(name, branch, base)
    branch = branch or f"wt/{name}"
    wt_dir = wt_root / name
    if wt_dir.exists() or wt_dir.is_symlink():
        return _register_existing(main, name, wt_dir, assume_yes)
    wt_root.mkdir(parents=True, exist_ok=True)
    # Reproduce the recipe's machine-mount warning: worktree stacks need the rootless
    # machine to mount the worktrees root (no-op in the box / where there's no podman).
    machine.ensure(main, wt_root)
    if (yard := _checkout_yard(again)) is None:
        return 1

    print(f"▶ git worktree add --no-checkout {wt_dir} (branch {branch})…")
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
    rc = _git_worktree_add(main, argv)
    if rc != 0:
        _err("✗ git worktree add failed")
        return rc
    if (rc := _checkout_in_yard(main, wt_dir, branch, yard, again, made="-b" in argv)) != 0:
        return rc
    worktree_registry.register(main, name, wt_dir)

    _init_in_yard(main, wt_dir)

    _seed_keyless_posture(name)

    print(f"\n✓ worktree ready: {wt_dir}  (branch {branch})")
    print(f"  bring its stack up:  WORKTREE={name} fy up")
    _say_unmasked(main, name, fresh=True)
    return 0


def _say_unmasked(main: Path, name: str, *, fresh: bool) -> None:
    """Name the running boxes that don't mask the new worktree's ``[box].shadow_volumes`` dirs
    (a box's mounts are fixed at create — see :mod:`foldyard.crossmask`), and the two ways out.
    Says it; never recreates a box. Best-effort: a report must not fail the add."""
    from . import box

    try:
        late = box.boxes_not_masking(main, name, fresh=fresh)
    except (Exception, SystemExit):
        return
    if not late:
        return
    print(
        f"\n⚠ running dev boxes created before '{name}' don't mask its dependency dirs "
        f"([box].shadow_volumes): {', '.join(late)}."
    )
    print(f"  An install in '{name}' from one of them would land on your computer's disk. Either")
    print(f"  run it in '{name}''s own box — `WORKTREE={name} fy box up`, then `fy box exec` or")
    print(f"  `fy box shell` with WORKTREE={name} — or recreate a box so it masks '{name}':")
    print("  `fy box down && fy box up` (prefixed WORKTREE=<its name> for a worktree's box).")


def _register_existing(main: Path, name: str, wt_dir: Path, assume_yes: bool) -> int:
    """``add`` on a checkout that already exists: record it, after the operator has seen what it
    really is. Only a real directory that git tracks as a worktree of ``main`` qualifies — a dir
    the box planted has no worktree metadata, and a symlink would register someone else's
    checkout."""
    if wt_dir.is_symlink() or not (wt_dir / ".git").exists():
        _err(f"✗ already exists, and is not a worktree checkout: {wt_dir}")
        return 1
    if not _registered_worktree(main, wt_dir):
        _err(f"✗ already exists, but git doesn't track it as a worktree of {main}: {wt_dir}")
        return 1
    if worktree_registry.checkouts(main).get(name) == Path(os.path.realpath(wt_dir)):
        print(f"✓ worktree '{name}' is already registered ({wt_dir})")
        return 0
    print(f"'{name}' already exists: {os.path.realpath(wt_dir)} (branch {_branch_of(wt_dir)}).")
    print("  Registering it lets your computer act on it: run its daemons, heal its git index,")
    print("  and reconcile it once its foldyard.toml is adopted.")
    if not assume_yes:
        try:
            answer = input("Register it? (y/N): ").strip()
        except EOFError:
            answer = ""
        if answer not in ("y", "Y"):
            print("Not registered.")
            return 1
    try:
        worktree_registry.register(main, name, wt_dir)
    except ValueError as e:
        _err(f"✗ {e}")
        return 1
    print(f"✓ worktree '{name}' registered.  Bring its stack up:  WORKTREE={name} fy up")
    _say_unmasked(main, name, fresh=False)
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
    print(f"▶ initialising worktree config in the VM ({image})…")
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
    agents' transcripts to the durable host store, then ``git worktree remove`` the checkout.
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
        if _has_from_box_slot(name):
            # Its transcripts may never have been archived (the checkout went out-of-band).
            archive_env = {
                "FOLDYARD_CHECKOUT": str(wt_dir),
                "HERE": config.dev_vm_rel(),
                "WORKTREE": name,
            }
            rc = transcripts.sync_current(archive_env, what=f"worktree '{name}' transcripts")
            if rc != 0 and not force:
                _err(
                    f"✗ not deleting '{name}''s from-box slot — its transcripts were NOT archived."
                )
                _err(
                    "  Resolve the issue, or re-run with --force to delete it anyway (loses them)."
                )
                return rc
            if not _remove_from_box_slot(name):
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
    print("  3. archive its configured agent transcripts to the durable store on your computer")
    if orphaned:
        print("  4. delete the ORPHANED checkout dir (git no longer tracks it) + worktree prune")
    else:
        print("  4. git worktree remove — the checkout directory is deleted")
    print("  5. delete its local mode + isolated VS Code state")
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

    # 3. Save configured agents' transcripts BEFORE git deletes their bound-out dirs (the legacy
    #    ones live in the checkout) and step 5 deletes the worktree's from-box slot.
    archive_env = {"FOLDYARD_CHECKOUT": str(wt_dir), "HERE": config.dev_vm_rel(), "WORKTREE": name}
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
        rc = _host_git(main, "worktree", "prune").returncode
        if rc != 0:
            _err("✗ git worktree prune failed — the checkout dir was deleted, but stale")
            _err(f"  worktree metadata may remain (re-run: git -C {main} worktree prune).")
            return rc
    else:
        print(f"▶ git worktree remove {wt_dir}…")
        argv = ["worktree", "remove", *(["--force"] if force else []), str(wt_dir)]
        rc = _host_git(main, *argv).returncode  # its dirty check runs `git status`: fsmonitor
        if rc != 0:
            _err("✗ git worktree remove failed (uncommitted changes? use --force).")
            return rc
    # 5. The checkout is gone, so its posture and isolated VS Code process state are stale too —
    # and its from-box slot (ADR-0033), whose transcripts step 3 archived. Keep shared
    # identity/allow state and the main checkout's VS Code profile untouched.
    if not _remove_local_state(name) or not _remove_from_box_slot(name):
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
    """List main + each worktree with its branch (lightweight; no engine needed). On your
    computer, a checkout the host has no record of is marked, with the verb that registers it."""
    main = stack.main_repo()
    wt_root = config.worktrees_root(main)
    known = set() if config.in_box() else set(worktree_registry.checkouts(main))
    print(f"  {'main':<18} {_branch_of(main):<28} {main}")
    if wt_root.is_dir():
        for d in sorted(wt_root.iterdir()):
            if d.is_dir() and (d / ".git").exists():
                note = "" if config.in_box() or d.name in known else "  (not registered)"
                print(f"  {d.name:<18} {_branch_of(d):<28} {d}{note}")
    if not config.in_box() and (stray := worktree_registry.unregistered(main, wt_root)):
        print(f"\n  Not registered — your computer runs nothing for: {', '.join(stray)}.")
        print("  If one is yours: `fy worktree add <name>` (asks before registering).")
    return 0
