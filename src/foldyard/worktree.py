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

import functools
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
# no changes, which a dirty check would believe) stops the command the shared config can name for
# every index refresh. Filter drivers (`filter.<d>.smudge` / `clean` / `process`, LFS's among
# them) have no such switch, and turning them off would change what a checkout contains (or what
# a hash says) — so this git never checks a file out or hashes one: `add` does the metadata step
# here and the checkout in a container (`_checkout_in_yard`), and `remove` asks a container
# whether there is uncommitted work, and to park it (`_uncommitted_in_yard`). See docs/git.md.
#
# Nor does it FETCH. The shared config can make the repo a partial clone
# (`extensions.partialClone` + a promisor remote), whose missing objects git fetches on demand
# over a transport that config chooses — `core.sshCommand`, `ext::` — so reading an object the
# box deleted, or an id the yard handed over, ran a command on your computer (PR #153's review).
# Two switches, both on every git here: `GIT_NO_LAZY_FETCH` (git 2.44+), and every transport's
# policy set to `never` on the command line, which outranks the repo's own (a bare
# `protocol.allow=never` doesn't: a repo-level `protocol.ssh.allow=always` beats it). Nothing
# foldyard runs here needs the network. A remote helper no policy names (`foo::…`) is the variable's
# alone, so on a git too old for it a partial clone is refused (:func:`_fetch_refusal`).
_PROTOCOLS = ("file", "git", "ssh", "http", "https", "ext")
_NO_FETCH_CONFIG = (
    "-c",
    "protocol.allow=never",
    *(arg for p in _PROTOCOLS for arg in ("-c", f"protocol.{p}.allow=never")),
)
_NO_FETCH_ENV = {"GIT_NO_LAZY_FETCH": "1", "GIT_PROTOCOL_FROM_USER": "0"}
_NO_REPO_CODE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=", *_NO_FETCH_CONFIG)


class HookScanError(Exception):
    """A config file git would read that foldyard couldn't check for hooks — so it runs no git."""


class LazyFetchError(HookScanError):
    """A partial clone, on a git that can't be told not to fetch its missing objects."""


@functools.cache
def _git_can_refuse_lazy_fetch() -> bool:
    """Whether this git honours ``GIT_NO_LAZY_FETCH`` — it came with ``git --no-lazy-fetch``
    (2.44), so that option is the probe. Asked only of a repo that is a partial clone."""
    try:
        got = subprocess.run(["git", "--no-lazy-fetch", "version"], capture_output=True)
    except OSError:
        return False
    return got.returncode == 0


def _note_fetchy(fetchy: set[str], key: str, value: str) -> None:
    """Record ``key`` if it makes git fetch missing objects on demand: ``extensions.partialClone``,
    or ``remote.<name>.promisor`` (on its own enough to register a promisor remote) set to anything
    but an explicit false. Over-counting only refuses an old git."""
    if key == "extensions.partialclone" and value.strip():
        fetchy.add(key)
    elif (
        key.startswith("remote.")
        and key.endswith(".promisor")
        and value.strip().lower() not in ("false", "no", "off", "0")
    ):
        fetchy.add(key)


def _lazy_fetch_error(fetchy: set[str]) -> LazyFetchError:
    return LazyFetchError(
        f"this repo can fetch missing objects on demand ({', '.join(sorted(fetchy))}), and this "
        "git can't be told not to — over a transport the shared config chooses. It needs git "
        "2.44 or newer"
    )


def _check_fetch(repo: Path) -> None:
    """Raise unless git can run in ``repo`` here without risking a fetch: on a git older than
    2.44 (no ``GIT_NO_LAZY_FETCH``, so a remote helper no transport policy names stays reachable),
    a repo whose config — every file and include git could read, whatever the include's condition
    (:func:`_scan_config`) — names a promisor remote raises :class:`LazyFetchError`, and one that
    can't be checked :class:`HookScanError`. Free on a newer git."""
    if _git_can_refuse_lazy_fetch():
        return
    _, fetchy = _scan_config(repo)
    if fetchy:
        raise _lazy_fetch_error(fetchy)


def _no_fetch_env(base: dict[str, str] | None = None) -> dict[str, str]:
    return {**(os.environ if base is None else base), **_NO_FETCH_ENV}


def _git_read(repo: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    """``git -C repo args…`` for a READ here (a ref, an object, config): no fetch
    (:data:`_NO_FETCH_CONFIG`). A repo this git can't keep from fetching (:func:`_check_fetch`)
    runs nothing: a failed run (1), the reason said."""
    try:
        _check_fetch(repo)
    except HookScanError as e:
        _refuse_unscanned(e)
        return subprocess.CompletedProcess(args, 1, "", "")
    return subprocess.run(
        ["git", *_NO_FETCH_CONFIG, "-C", str(repo), *args], env=_no_fetch_env(), **kw
    )


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
    path: Path,
    repo_scope: bool,
    hooks: dict[str, set[str]],
    seen: set[str],
    depth: int = 0,
    fetchy: set[str] | None = None,
) -> None:
    """``path``'s hooks into ``hooks`` (and its fetch-enabling keys into ``fetchy``,
    :func:`_note_fetchy`), then every file it includes, WHATEVER the include's
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
        if fetchy is not None:
            _note_fetchy(fetchy, key, value)
        if value and (
            key == "include.path" or (key.startswith("includeif.") and key.endswith(".path"))
        ):
            target = _include_target(value, path)
            if target is None:
                if repo_scope:
                    raise HookScanError(f"the repo's git config includes {value!r}, from {path}")
                continue
            _scan_file(target, repo_scope, hooks, seen, depth + 1, fetchy)


def _config_hooks(repo: Path) -> dict[str, set[str]]:
    """Every config-defined hook git could run for ``repo`` — in its own git, or in a child git it
    starts in another context (``git worktree add``'s, in the new worktree): name → the events
    it is set for (:func:`_scan_config`). Raises :class:`HookScanError` rather than miss one.
    Empty on a git without config hooks — and harmless to over-count: disabling a name that never
    runs changes nothing. A repo this git can't keep from fetching raises :class:`LazyFetchError`
    (:func:`_check_fetch`): every git run on this scan is refused."""
    hooks, fetchy = _scan_config(repo)
    if fetchy and not _git_can_refuse_lazy_fetch():
        raise _lazy_fetch_error(fetchy)
    return hooks


def _scan_config(repo: Path) -> tuple[dict[str, set[str]], set[str]]:
    """``(hooks, fetch-enabling keys)`` from each config file git reads for ``repo``
    (``--show-origin``), then every include target in them whatever its condition
    (:func:`_scan_file`). Raises :class:`HookScanError` when one can't be read."""
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
    fetchy: set[str] = set()
    for path, repo_scope in files.items():
        _scan_file(path, repo_scope, hooks, seen, fetchy=fetchy)
    return hooks, fetchy


def _hookless_env(repo: Path, hooks: dict[str, set[str]] | None = None) -> dict[str, str]:
    """The environment for a host git run in ``repo`` with each of its config hooks disabled —
    appended after any GIT_CONFIG_COUNT entries the caller's own environment already carries.
    Children inherit it, so a hook only a child would see is off there too — and so is a fetch
    (:data:`_NO_FETCH_ENV`)."""
    env = _no_fetch_env()
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
    """``git -C repo args…`` with none of the repo's hooks or fsmonitor run, and no fetch (see
    above). For every host-side git here that can reach one: a checkout, a ref update, an index
    refresh. ``env`` defaults to :func:`_hookless_env`; a caller passing its own builds it from
    that (the no-fetch variables are set on it whatever it holds). When the repo's config can't be
    checked for hooks, git doesn't run: a failed run (1), the reason said."""
    if env is None:
        try:
            env = _hookless_env(repo)
        except HookScanError as e:
            _refuse_unscanned(e)
            return subprocess.CompletedProcess(args, 1, "", "")
    cmd = ["git", *_NO_REPO_CODE, "-C", str(repo), *args]
    return subprocess.run(cmd, env=_no_fetch_env(env), **kw)


def _refuse_unscanned(e: HookScanError) -> None:
    _err(f"✗ not running git on your computer: {e}.")
    if isinstance(e, LazyFetchError):
        _err(
            "  Upgrade git on your computer (`git --no-lazy-fetch version` must work), then retry."
        )
        return
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


def _yard() -> tuple[str, dict, str, bool]:
    """``(engine, env, image, present)``: the one-off container git runs in for ``add`` and
    ``remove``, and whether its image is there to run (no image yet, or the VM is down)."""
    from . import box

    engine, env = box.engine_env()
    image = config.box_image()["tag"]
    return engine, env, image, box.image_exists(engine, image, env)


def _checkout_yard(again: str) -> tuple[str, dict, str] | None:
    """``(engine, env, image)`` to run the checkout in — or None, said, when there is no image to
    run it in yet. Asked BEFORE any git runs, so a refusal leaves nothing behind. Deliberately not a
    fallback to a checkout here: that is the filter drivers running on your computer again."""
    engine, env, image, present = _yard()
    if present:
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
    got = _git_read(main, "rev-parse", "--verify", "--quiet", ref, capture_output=True, text=True)
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
    own git, in :func:`_yard_run`'s container. ``read-tree --reset -u`` moves no ref and runs no
    checkout hook — it writes the files and the index, nothing else."""
    return [
        *_yard_run(engine, main, wt_dir),
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


def _yard_run(engine: str, main: Path, wt_dir: Path) -> list[str]:
    """``<engine> run`` up to the container's own settings, for a git run in ``wt_dir``: the two
    mounts ``worktree_init`` gets (at the paths your computer uses, which the worktree's ``.git``
    file names), the box's uid (container root, which rootless podman maps to the VM user, so what
    it writes is owned as the box's files are), no capabilities, no network."""
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
    ]


def _admin_dir(wt_dir: Path) -> Path | None:
    """The worktree's admin dir in the shared ``.git``, from its ``.git`` file, read without
    following a link or blocking (the worktrees root is box-writable: a FIFO there must not hang a
    verb). None when that file isn't a regular file there."""
    try:
        fd = os.open(wt_dir / ".git", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        text = os.read(fd, 4096).decode(errors="replace")
    finally:
        os.close(fd)
    return wt_dir / Path(text.removeprefix("gitdir:").strip())  # `/` keeps an absolute one as is


def _index_on_host(wt_dir: Path) -> bool:
    """Whether the worktree's index — the checkout's last write — is there on YOUR side of the
    mount (:func:`_admin_dir`, from the ``.git`` file git just wrote)."""
    admin = _admin_dir(wt_dir)
    return admin is not None and (admin / "index").is_file()


def _undo_add(main: Path, wt_dir: Path, branch: str, made_at: str) -> None:
    """Drop the worktree the metadata step made — ``--force``, which skips the dirty check (a
    ``git status``, which would hash files) — and the branch it made, while that is still where it
    was made (``made_at``). Says what it couldn't."""
    if _host_git(main, "worktree", "remove", "--force", str(wt_dir)).returncode != 0:
        _err("  ✗ couldn't remove the half-made worktree:")
        _err(f"    git -C {main} worktree remove --force {wt_dir}")
    if not made_at:
        return
    now = _git_read(
        main,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        capture_output=True,
        text=True,
    ).stdout.strip()
    if now != made_at or _host_git(main, "branch", "-q", "-D", branch).returncode != 0:
        _err(f"  ✗ kept the branch '{branch}' it had made (delete it: git branch -D {branch})")


def _say_lfs(main: Path, oid: str) -> None:
    """Name the Git LFS pointers the checkout left (it had no network), when the commit's root
    ``.gitattributes`` routes anything through LFS. Read as a blob: ``cat-file`` runs no filter."""
    attrs = _git_read(
        main, "cat-file", "blob", f"{oid}:.gitattributes", capture_output=True, text=True
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


# ── `remove` reads the worktree in the yard ───────────────────────────────────────────────────
#
# Whether a worktree holds uncommitted work is a `git status`, and parking that work on a ref is an
# `add -A`: both HASH the worktree's files, and git runs every `clean` filter the shared config
# names for each file it hashes — on a worktree checked out in the VM, whose stat data your
# computer never matches, every file. So both run in the same one-off container as the checkout
# (`_uncommitted_cmd`), and git on your computer does only what hashes nothing: check that the id
# it was handed names a commit (`cat-file -t` reads an object), write the ref, and delete the
# checkout with `--force` (which skips git's own `git status`; a lock still holds). Refs and
# `.git/config` are written only on your computer, as for the checkout (ADR-0021, issue #6).
#
# What the container prints is the box's to influence (it runs the box's filters over files the
# box writes), so it is DATA: one line, matched whole, an id by its shape and then by git. A lie
# there can only misreport the box's own work. A yard that can't be asked is never replaced by a
# check here: `remove` refuses, and `--discard` is the operator's explicit way past.

# The script the container runs (its image's own `sh` and git). $1: `check` (is anything
# uncommitted?) or `save` (park it, too); $2: the parking commit's message — both ARGUMENTS, never
# spliced in. "Uncommitted" means: after unstaging everything, is anything different from HEAD?
# The worktree's own index is deleted with it, so it isn't asked (git's check reads it, so a stale
# one — a box's commits leave the shared index behind, ADR-0021 — read as dirty). The files are
# compared with HEAD through a throwaway index in the container's own /tmp, which no box reaches:
# `read-tree HEAD`, `add -A` (every file not gitignored, untracked ones whatever
# `status.showUntrackedFiles` says), then `diff --cached` against HEAD — and, for a submodule's own
# uncommitted content, `diff` against that index (`--ignore-submodules=none` both times). So the
# real index and HEAD never change, and the tree compared is the tree the save commits: whatever
# the check calls uncommitted, `--force` parks. Work that is ONLY in the index (staged, then put
# back on disk) isn't counted or kept — unstaging discards it too. Gitignored files are left out,
# which keeps it cheap (no node_modules) and is its blind spot. An unborn HEAD, or any git failure,
# is exit 3.
_UNCOMMITTED_SH = """\
g() { git -c filter.lfs.required=false "$@"; }
head=$(g rev-parse --verify --quiet 'HEAD^{commit}') || exit 3
GIT_INDEX_FILE="${TMPDIR:-/tmp}/fy-uncommitted-index.$$"
export GIT_INDEX_FILE
g read-tree "$head" || exit 3
g add -A || exit 3
g diff --cached --quiet --no-ext-diff --ignore-submodules=none "$head"; rc=$?
if [ "$rc" = 0 ]; then g diff --quiet --no-ext-diff --ignore-submodules=none; rc=$?; fi
case "$rc" in 0) echo fy-clean; exit 0 ;; 1) ;; *) exit 3 ;; esac
if [ "$1" != save ]; then echo fy-dirty; exit 0; fi
tree=$(g write-tree) || exit 3
oid=$(g commit-tree --no-gpg-sign -p "$head" -m "$2" "$tree") || exit 3
echo "fy-saved $oid"
"""
_PARKED_BY = ("foldyard", "foldyard@localhost")  # commit-tree needs one; the work isn't theirs
_OID = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"  # sha1 or sha256
_VERDICT = re.compile(rf"fy-(clean|dirty)|fy-(saved) ({_OID})")
_SAYS = {False: ("clean", "dirty"), True: ("clean", "saved")}  # what each mode prints


def _uncommitted_cmd(
    engine: str, image: str, main: Path, wt_dir: Path, name: str, *, save: bool
) -> list[str]:
    """The one-off container that tells whether ``wt_dir`` holds uncommitted work — and, with
    ``save``, parks it in a commit (objects only: no ref, no index of the worktree's): the image's
    own ``sh`` and git running :data:`_UNCOMMITTED_SH`, in :func:`_yard_run`'s container."""
    who, email = _PARKED_BY
    return [
        *_yard_run(engine, main, wt_dir),
        "-e",
        f"GIT_AUTHOR_NAME={who}",
        "-e",
        f"GIT_AUTHOR_EMAIL={email}",
        "-e",
        f"GIT_COMMITTER_NAME={who}",
        "-e",
        f"GIT_COMMITTER_EMAIL={email}",
        "--entrypoint",
        "sh",
        image,
        "-c",
        _UNCOMMITTED_SH,
        "fy-uncommitted",
        "save" if save else "check",
        f"fy: uncommitted work in worktree '{name}' at removal",
    ]


def _verdict(out: bytes, *, save: bool) -> tuple[str, str] | None:
    """The container's one line, ``(kind, commit id or "")`` — None for anything else, or for a
    kind the mode it was asked in never prints (a ``save`` that says ``dirty`` saved nothing)."""
    text = out.decode("ascii", errors="replace")
    if text.count("\n") != 1 or not text.endswith("\n"):
        return None
    m = _VERDICT.fullmatch(text[:-1])
    if m is None or (kind := m[1] or m[2]) not in _SAYS[save]:
        return None
    return kind, m[3] or ""


def _uncommitted_in_yard(
    main: Path, wt_dir: Path, name: str, yard: tuple[str, dict, str], *, save: bool
) -> tuple[str, str] | None:
    """Ask the yard about ``wt_dir``'s uncommitted work (:func:`_uncommitted_cmd`):
    ``("clean"|"dirty"|"saved", commit id or "")``. None, said, when it can't be asked or its
    answer isn't one foldyard can read."""
    engine, env, image = yard
    cmd = _uncommitted_cmd(engine, image, main, wt_dir, name, save=save)
    script = cmd.index(_UNCOMMITTED_SH)
    shown = [*cmd[:script], "<foldyard's check>", *cmd[script + 1 :]]
    print(f"▶ checking '{name}' for uncommitted work in the VM ({image})…")
    print("  $ " + shlex.join(shown))
    got = subprocess.run(cmd, env=env, stdout=subprocess.PIPE)
    verdict = _verdict(got.stdout or b"", save=save) if got.returncode == 0 else None
    if verdict is not None:
        return verdict
    if got.returncode in (126, 127):
        _err(f"✗ the check couldn't start sh in {image}: [box].image must provide sh and git.")
    elif got.returncode != 0:
        _err("✗ checking the worktree for uncommitted work in the VM failed (above).")
    else:
        _err("✗ the check in the VM answered something foldyard can't read.")
    return None


def _park(main: Path, name: str, oid: str, env: dict[str, str]) -> str | None:
    """Point a new ``refs/fy/removed/<name>-<time>`` in MAIN at ``oid``, the commit the yard made
    of the worktree's uncommitted work — once git here says ``oid`` names a commit. The ref lands
    in the SHARED store, so it outlives ``worktree remove``, ``prune`` and a ``gc --prune=now``.
    Create-only (an existing ref of that name is never moved). None, said, when it can't."""
    from datetime import datetime

    kind = _host_git(main, "cat-file", "-t", oid, env=env, capture_output=True, text=True)
    if kind.returncode != 0 or kind.stdout.strip() != "commit":
        _err(f"✗ the check in the VM named {oid} as the parked work, which isn't a commit here.")
        return None
    ref = f"refs/fy/removed/{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    if _host_git(main, "update-ref", ref, oid, "", env=env).returncode != 0:
        _err(f"✗ couldn't write {ref} (above).")
        return None
    return ref


def _say_parked(name: str, ref: str) -> None:
    # The commands are for a box: git on your computer runs the filter and diff drivers the
    # shared .git names over what it checks out or shows.
    print(f"▶ uncommitted work in '{name}' saved to {ref}")
    print(f"     inspect, in a box:  git show --stat {ref}")
    print(f"     restore, in a box:  git checkout {ref} -- .")


def _remove_command(name: str, force: bool, assume_yes: bool) -> str:
    """The ``fy worktree remove`` the operator ran, to say how to run it again."""
    flags = [*(["--force"] if force else []), *(["--yes"] if assume_yes and not force else [])]
    return shlex.join(["fy", "worktree", "remove", name, *flags])


def _say_discard(again: str) -> None:
    _err("  Or remove it WITHOUT checking for or saving uncommitted work (anything uncommitted in")
    _err(f"  it is lost):  {again} --discard")


def _remove_yard(name: str, again: str) -> tuple[str, dict, str] | None:
    """``(engine, env, image)`` to ask about the worktree's uncommitted work in — or None, said,
    when there is no image to run it in. Asked BEFORE anything is torn down, so a refusal leaves
    the worktree, its box and its stack as they were. Never a fallback to a check here: that is
    the filter drivers running on your computer again."""
    engine, env, image, present = _yard()
    if present:
        return engine, env, image
    _err(f"✗ no {image} image in the VM to check '{name}' for uncommitted work in.")
    _err("  foldyard reads a worktree's files in a container, never on your computer: the shared")
    _err("  .git can name filter drivers, and git runs them for every file it reads to compare.")
    _err(f"  Build the image (or start the VM: fy machine ensure), then:  fy box build && {again}")
    _say_discard(again)
    return None


def _remove_checkout(
    main: Path,
    wt_dir: Path,
    name: str,
    yard: tuple[str, dict, str] | None,
    *,
    force: bool,
    again: str,
) -> tuple[int, str | None]:
    """Delete the registered checkout ``wt_dir`` — ``(rc, the ref its uncommitted work was parked
    on, if any)``. With ``yard``, first ask it whether there is uncommitted work: refuse if so,
    unless ``force``, which parks it (:func:`_park`) and refuses if that fails. ``yard`` None is
    ``--discard``: nothing asked, nothing parked. Then ``git worktree remove --force`` here.

    The repo's config is scanned for hooks before the yard is asked (a refusal then runs no
    container) and AGAIN once it has answered: the container mounts main's ``.git`` writable (the
    save writes objects there) and runs the box's filters, so a hook or include it adds must be
    seen by the scan that the ref write and the removal run under."""
    try:  # the ref write is a ref transaction
        quiet = _hookless_env(main)
    except HookScanError as e:
        _refuse_unscanned(e)
        return 1, None
    admin = _admin_dir(wt_dir)
    if not force and admin is not None and (admin / "modules").is_dir():
        # Git's own rule, which the --force below would skip — under --discard too: the admin dir
        # holds the submodules' repos, and removing it deletes their unpushed commits.
        _err("✗ working trees containing submodules cannot be moved or removed — nothing")
        _err("  removed. --force removes it with its submodules' repos.")
        return 1, None
    ref = None
    if yard is not None:
        verdict = _uncommitted_in_yard(main, wt_dir, name, yard, save=force)
        try:
            quiet = _hookless_env(main)
        except HookScanError as e:
            _refuse_unscanned(e)
            _err(f"  '{name}' was NOT removed.")
            return 1, None
        if verdict is not None and verdict[0] == "dirty":
            _err(f"✗ '{name}' has uncommitted work — nothing removed: files that differ from its")
            _err("  last commit (changed, deleted or untracked, not gitignored; a change only in")
            _err("  the stage doesn't count).")
            _err("  Commit them in its box, or re-run with --force to save them on a ref first:")
            _err(f"  {_remove_command(name, True, False)}")
            return 1, None
        if verdict is not None and verdict[0] == "saved":
            ref = _park(main, name, verdict[1], quiet)
        if verdict is None or (verdict[0] == "saved" and ref is None):
            _err(f"  '{name}' was NOT removed: its uncommitted work couldn't be checked or saved.")
            _err(f"  Once that's fixed, run it again:  {again}")
            _say_discard(again)
            return 1, None
        if ref:
            _say_parked(name, ref)
    print(f"▶ git worktree remove --force {wt_dir}…")
    rc = _host_git(main, "worktree", "remove", "--force", str(wt_dir), env=quiet).returncode
    if rc != 0:
        _err("✗ git worktree remove failed (above).")
    return rc, ref


def _git_has_ref(repo: Path, ref: str) -> bool:
    return _git_read(repo, "show-ref", "--verify", "--quiet", ref).returncode == 0


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


def _pin_config(main: Path, name: str) -> config.Config:
    """The handle on worktree ``name``'s ADOPTED-config pin (:func:`configpin.pin_dir`, keyed by
    checkout path) — the key ``add`` adopts under, without the checkout having to exist.

    The worktrees root is resolved, the last component never: that one is box-writable (every box
    binds the root), and following a ``feat`` the box swapped for a link to main would make
    ``remove feat`` drop MAIN's adoption. For a real directory it's the path the registry records
    (:func:`worktree_registry.register` — the realpath), hence the key ``add`` used."""
    root = Path(os.path.realpath(config.worktrees_root(main)))
    return config.Config(repo_root=root / name, worktree=name, toml={})


def _has_local_state(name: str) -> bool:
    from . import configpin

    main = stack.main_repo()
    return (
        worktree_registry.is_recorded(main, name)
        or any((root / name).exists() for root in _local_state_roots())
        or os.path.lexists(configpin.pin_dir(_pin_config(main, name)))
    )


def _remove_local_state(name: str) -> bool:
    """Remove the per-worktree posture and VS Code state, the host's record of the worktree, and
    its adopted config — after git removes the checkout. The adoption goes too because it is keyed
    by the checkout's path: left behind, the next worktree of this name would inherit it."""
    from . import configpin

    main = stack.main_repo()
    worktree_registry.unregister(main, name)
    roots = _local_state_roots()
    ok = True
    try:
        configpin.forget(_pin_config(main, name))
    except OSError as e:
        _err(f"✗ couldn't remove the worktree's adopted config: {e}")
        ok = False
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
        _git_read(
            repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", capture_output=True
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
    head = _git_read(
        repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", capture_output=True, text=True
    )
    if head.returncode == 0:
        ref = head.stdout.strip().removeprefix("refs/remotes/")
        if ref:
            return ref  # e.g. "origin/main" — the remote's declared default branch
    for cand in ("origin/main", "main", "origin/master", "master"):
        if _git_resolves(repo, cand):
            return cand
    return None


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


def _stdin_is_tty() -> bool:
    """Whether someone is at a terminal to answer the adoption prompt — the launch gate's test."""
    return sys.stdin.isatty()


def _adopt_config(name: str) -> bool:
    """Offer the new worktree's config for adoption, so the host runs it from the first
    ``fy box up`` (:mod:`foldyard.configpin`, ADR-0022). True when the host now runs exactly this
    tree (the ``resolve`` status says so — not merely that some pin exists).

    A tree byte-identical to main's ADOPTED copy is adopted without asking: it grants nothing main
    hasn't already been granted, so there is nothing to decide. Race-free, because every running
    box binds the whole worktrees root and can write this checkout while it is compared: the
    adoption carries the digest of the bytes compared AND the main pin they were compared against
    (``reviewed=``/``baseline=``), so a write landing in between is :class:`ReviewStale`, never
    adopted on main's say-so. Anything else goes through the first-adoption review the launch gate
    uses (:func:`configpin.resolve`): the diff against main, and a prompt only on a terminal — no
    terminal adopts nothing. ``-y`` plays no part: it answers the register question, and a config
    that differs from main's is only ever adopted by someone who was shown it.

    Best-effort: declining, or a failure here, leaves the worktree made and unadopted, never
    undone."""
    from . import configpin, devmode

    try:
        cfg = devmode.worktree_config(name)
        drift = configpin.inspect(cfg)
        if not drift.pinned_exists:
            base = configpin.main_baseline(drift)
            if base is not None and base.identical:
                try:
                    configpin.adopt(cfg, reviewed=drift.tree_digest(), baseline=base.digest)
                except configpin.ReviewStale:
                    pass  # it changed after it was compared: review what is there now, below
                else:
                    print(f"✓ config adopted — identical to main's ({base.digest}).", flush=True)
                    return True
        status = configpin.resolve(cfg, interactive=_stdin_is_tty(), prompt=input, echo=_err)
        # Ready only when the host would run THIS tree: adopted just now, or a pin matching it.
        # A pin the tree differs from (left unresolved, ignored) is not this checkout's config.
        return status in ("clean", "pinned", "adopted")
    except Exception as e:  # adoption is offered, never what fails the (already-made) worktree
        _err(f"  (couldn't offer its config for adoption: {e})")
        return False


def _box_up(name: str) -> int:
    """``WORKTREE=<name> fy box up``, as a FRESH process. Not box code in-process: this process
    bound its import-time constants (``machine.MACHINE``, ``devmode.AXES``…) and config caches
    before the adoption, which is exactly what the launch gate's own re-run
    (:func:`configpin._rerun_with_adopted_config`) exists to shed. Its env drops the gate's re-run
    marker, so the child's gate keeps its one re-run."""
    from . import configpin

    env = {k: v for k, v in os.environ.items() if k != configpin.RERUN_ENV}
    env["WORKTREE"] = name
    print(f"\n▶ WORKTREE={name} fy box up", flush=True)
    return subprocess.run([sys.executable, "-m", "foldyard", "box", "up"], env=env).returncode


def add(
    name: str, branch: str = "", base: str = "", assume_yes: bool = False, box_up: bool = False
) -> int:
    """Create a host-sibling worktree under the worktrees root, record it in the host's
    worktree registry, init its config, and offer that config for adoption (:func:`_adopt_config`)
    — a first adoption, wherever it happens, mirrors main's keyless posture into the worktree
    (:func:`foldyard.keyless.seed_posture`). ``box_up`` then starts its box (:func:`_box_up`) —
    only once its config is adopted, since the host would run nothing for it otherwise.

    A brand-new branch forks off ``base`` (an explicit ``--from``), else the project's default
    branch (:func:`_default_base_ref`) — NOT the primary checkout's current HEAD.

    On a worktree that ALREADY exists (one from before the registry, or made with plain ``git
    worktree add``) it registers instead, once the operator has confirmed its real path — the one
    way a worktree comes (back) under host management. See :mod:`foldyard.worktree_registry`.
    ``box_up`` starts it there too; that box's own launch gate asks for any adoption still owed."""
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
        rc = _register_existing(main, name, wt_dir, assume_yes)
        return _box_up(name) if box_up and rc == 0 else rc
    wt_root.mkdir(parents=True, exist_ok=True)
    # The checkout doesn't exist yet, so an adoption at its path is an earlier checkout's (removed
    # by hand, or by a `remove` that predates dropping it). Dropped BEFORE git makes the checkout
    # and it's registered, so the supervisor never serves the new one from the old branch's config.
    from . import configpin

    try:
        if configpin.forget(_pin_config(main, name)):
            print(f"▶ dropped a config adoption left at this path by an earlier '{name}'.")
    except OSError as e:
        # Going on would hand the new checkout the earlier one's adopted config.
        _err(f"✗ couldn't remove a config adoption left at this path by an earlier '{name}': {e}")
        return 1
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

    _init_in_yard(main, wt_dir)  # first: it copies foldyard.local.toml, which is adopted too

    # Its first adoption also mirrors main's keyless posture in (configpin.adopt), here or later.
    adopted = _adopt_config(name)

    print(f"\n✓ worktree ready: {wt_dir}  (branch {branch})")
    if adopted:
        if not box_up:
            print(f"  bring its box up:    WORKTREE={name} fy box up")
        print(f"  bring its stack up:  WORKTREE={name} fy up")
    else:
        print("  its config isn't adopted, so your computer runs nothing for it yet.")
        print(f"  Review it with `WORKTREE={name} fy config diff`, then")
        print(f"  `WORKTREE={name} fy config adopt` — or adopt at `WORKTREE={name} fy box up`.")
    _say_unmasked(main, name, fresh=True)
    if not box_up:
        return 0
    if not adopted:
        _err(f"✗ --box: not starting '{name}''s box — its config isn't adopted (see above).")
        return 1
    return _box_up(name)


def _say_unmasked(main: Path, name: str, *, fresh: bool) -> None:
    """Mask the new worktree's ``[box].shadow_volumes`` dirs in the running boxes that lack them
    (:func:`foldyard.box.mask_running_boxes`, no recreate), then name those it couldn't and the two
    ways out. Never recreates a box. Best-effort: a report must not fail the add."""
    from . import box

    try:
        late = box.mask_running_boxes(main, name, fresh=fresh)
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


def remove(name: str, force: bool = False, assume_yes: bool = False, discard: bool = False) -> int:
    """Remove a host-sibling worktree and ALL its local state: tear down its dev box and its
    stack (compose down + drop the worktree's volumes; shared caches kept), archive its configured
    agents' transcripts to the durable host store, then delete the checkout
    (:func:`_remove_checkout`). Interactive confirmation unless ``assume_yes``/``force``.
    Transcripts are archived BEFORE the checkout goes, since that takes the fragile bound-out
    transcript dirs with it.

    Uncommitted work is looked for in the yard, never on your computer (see "`remove` reads the
    worktree in the yard"): a worktree with any is refused, unless ``force``, which parks it on
    ``refs/fy/removed/<name>-<time>`` first. ``force`` also removes when archiving fails. With no
    yard to ask, it refuses before anything is torn down; ``discard`` removes it without asking or
    parking anything — the operator's explicit acceptance that uncommitted work is lost.

    Also handles the ORPHANED shape (checkout dir present, git metadata gone — see
    :func:`_registered_worktree`): under ``--force`` the dir is deleted directly +
    ``git worktree prune``, since ``git worktree remove`` can't remove what git no longer
    tracks. Git has nothing there to check or park, so no yard is needed."""
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
    # Asked BEFORE the plan and the teardown: with no yard to ask about uncommitted work, nothing
    # is torn down (an orphan has nothing git could check, and --discard says not to ask).
    again = _remove_command(name, force, assume_yes)
    yard = None
    if not (orphaned or discard):
        if (yard := _remove_yard(name, again)) is None:
            return 1

    print(f"This PERMANENTLY removes worktree '{name}' at {wt_dir}:")
    print(f"  1. stop + remove its dev box container ({project}-devbox) + its shadow volumes")
    print(f"  2. tear down its stack ({project}): compose down + drop its volumes")
    print("       (postgres + GCS/BigQuery emulator data; SHARED pnpm/Playwright caches are kept)")
    print("  3. archive its configured agent transcripts to the durable store on your computer")
    if orphaned:
        print("  4. delete the ORPHANED checkout dir (git no longer tracks it, so nothing")
        print("       uncommitted in it can be saved) + worktree prune")
    elif discard:
        print("  4. git worktree remove — the checkout directory is deleted, NOT checked for")
        print("       uncommitted work first, nor any saved (--discard): anything uncommitted")
        print("       in it is lost")
    elif force:
        print("  4. save any uncommitted work on refs/fy/removed/<name>-<time> (looked for in the")
        print("       VM), then git worktree remove — the checkout directory is deleted")
    else:
        print("  4. check it has no uncommitted work (in the VM), then git worktree remove — the")
        print("       checkout directory is deleted")
    print("  5. delete its local mode, isolated VS Code state + adopted config")
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

    # 4. Delete the checkout — asking the yard about uncommitted work right before (after the
    #    teardown, so its own box can't add more), parking it under --force — or, for an orphan
    #    git no longer tracks, delete the dir + prune the stale metadata so a re-add starts clean.
    saved = None
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
        rc, saved = _remove_checkout(main, wt_dir, name, yard, force=force, again=again)
        if rc != 0:
            if saved:
                _err(f"  Its uncommitted work is on {saved} all the same.")
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
