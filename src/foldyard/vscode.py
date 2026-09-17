"""`foldyard code` — open VS Code attached to the running (per-worktree) dev box.

Faithful port of the `code` recipe, with ONE deliberate fix: the editor is launched in a
DEDICATED, per-worktree ``--user-data-dir`` (under ``config.state_dir()/vscode``) instead of the
user's default one.

WHY. VS Code is single-instance *per ``--user-data-dir``*. The old recipe launched `code`
with no such flag while `DOCKER_HOST` pointed at the box's podman machine socket — so the
box-attached window became (or merged into) the user's DEFAULT VS Code singleton and every
later window, for any repo, inherited that podman `DOCKER_HOST`. In an unrelated repo on
Docker Desktop + Dev Containers, that silently routed the Dev Containers extension at podman.
A dedicated user-data-dir makes this a SEPARATE process whose env (incl. the `DOCKER_HOST`
this window legitimately needs) can never touch the default instance. Extensions stay shared
(VS Code's default ``~/.vscode/extensions``): Dev Containers + Claude are already installed
there, so the attach works immediately with no re-download.

Each worktree gets its own isolated instance. Besides preventing settings (notably the local
terminal's host checkout) from bleeding between worktrees, that keeps each window bound to the
environment and attached-container config it was launched with.

RUN ON THE MAC. The attach is over the Docker API ("Attach to Running Container"), so the
launched VS Code's `DOCKER_HOST` must point at the same machine socket — which is exactly,
and only, this isolated instance's env.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import config, configpin, devmode, stack

# The Dev Containers extension id whose globalStorage holds attached-container configs.
_REMOTE_CONTAINERS = "ms-vscode-remote.remote-containers"
# The attached-container config is AUTHORED by foldyard, from the ADOPTED `[vscode]` table — never
# from a document the repo produces, and never from mount data such as `.vscode/extensions.json`.
# Two reasons: VS Code's attached-config schema includes lifecycle hooks, and `initializeCommand`
# runs ON THE HOST, so the only key set that can reach that file is one foldyard names itself
# (ADR-0023); and the `extensions` list decides what the host INSTALLS — a UI-kind extension lands
# in the operator's shared ~/.vscode/extensions — so it has to come from where the adopt gate
# reviews it, not from a file the box can write (ADR-0026).
_GENERATED_MARKER_KEY = "_generatedBy"
# Stamped so the NEXT run recognises a config as foldyard's rather than one you took ownership of
# (remove the key to keep your own edits).
_GENERATED_MARKER = "foldyard fy code"
# The attach must match how `fy shell`/`fy claude` exec into the box (`--user 0`): attaching as the
# image's baked `vscode` user (uid 1000) is what once split file ownership between VS Code-created
# files and agent/root ones, and made VS Code tooling hit permission errors on the root-owned
# socket and volumes. A foldyard fact, so a pin — not a consumer key.
_REMOTE_USER = "root"
# A marketplace extension id: `<publisher>.<name>`. Anything else in a recommendations file is
# dropped rather than handed to VS Code.
_EXT_ID = re.compile(r"^[A-Za-z0-9][\w-]*\.[A-Za-z0-9][\w-]*$")
# Dev Containers applies an attached config's `extensions` and `settings` ONCE per server install,
# each gated by its own marker under the box's ~/.vscode-server/data/Machine; a change only lands
# once the matching marker is gone. The settings write has a SECOND gate (extension source,
# 0.469: `if (markerCreated && !exists(Machine/settings.json)) write`): it never rewrites an
# existing Machine/settings.json, marker or no marker — so a settings change must remove that
# file too, or it silently never applies to a box whose server has been attached once (found on
# a consumer whose Machine settings were two months stale with the marker being reset on every
# `fy code`). The file is the extension's rendering of OUR config plus its own additions
# (Copilot instructions, port attributes), all of which it regenerates; an operator's hand edits
# in the "Remote [Attached Container]" settings tab are the one thing lost, and those belong in
# `[vscode.settings]` anyway.
_INSTALL_EXTENSIONS_MARKER = ".installExtensionsMarker"
_WRITE_MACHINE_SETTINGS_MARKER = ".writeMachineSettingsMarker"
_MACHINE_SETTINGS_FILE = "settings.json"
_LOCAL_TERMINAL_PROFILE = "Foldyard Local"
_TERMINAL_PROFILES_OSX = "terminal.integrated.profiles.osx"
_TERMINAL_DEFAULT_PROFILE_OSX = "terminal.integrated.defaultProfile.osx"
_TERMINAL_ENV_OSX = "terminal.integrated.env.osx"
_OBSOLETE_TERMINAL_CWD = "terminal.integrated.cwd"
_LOCAL_TERMINAL_CWD_ENV = "FOLDYARD_LOCAL_TERMINAL_CWD"
# A Unix-domain socket path is capped by `sockaddr_un.sun_path` — 104 bytes on macOS, so 103
# usable. VS Code's main process claims its single-instance lock by binding
# `<user-data-dir>/<version>-main.sock`; past the cap `listen` fails EINVAL inside
# `claimInstance` and the process dies BEFORE the logger flushes. What you see is an empty
# `logs/<stamp>/` directory, a `code` that still exits 0, and `fy code`'s "✓ launched" followed
# by no window ever appearing — so the leaf has to be FITTED here, not left to fail silently
# there. `~/.foldyard/<project>/vscode` leaves ~49 for the leaf, which a branch-shaped worktree
# name clears easily (a 53-char one is what surfaced this).
_SUN_PATH_MAX = 103
# `/<major>.<minor>-main.sock`, sized for a three-digit minor so 1.99 → 1.100 doesn't reintroduce
# the bug the day VS Code rolls over.
_IPC_SOCKET_BUDGET = len("/1.999-main.sock")
_LEAF_HASH_LEN = 8
# Below this we stop shortening: the ROOT alone has already spent the budget, so mangling the
# leaf buys nothing — that's a `FOLDYARD_STATE_DIR` problem, and VS Code's own "try a shorter
# --user-data-dir" warning names it. Shortening anyway would only rename every worktree's state
# dir to a hash for no gain.
_MIN_LEAF_BUDGET = 24
_ORIGINAL_ZDOTDIR_ENV = "FOLDYARD_ORIGINAL_ZDOTDIR"
_FOLDYARD_ZDOTDIR_ENV = "FOLDYARD_ZDOTDIR"


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _uri(box: str, checkout: str) -> str:
    """The `attached-container` folder URI VS Code opens: hex(container-name) + in-box path
    (mirrors the recipe's `printf %s "$BOX" | od -An -tx1`)."""
    return f"vscode-remote://attached-container+{box.encode().hex()}{checkout}"


def _fit_leaf(name: str, budget: int) -> str:
    """``name``, shortened to ``budget`` **encoded bytes** only if it doesn't already fit.

    Bytes, not characters: ``sun_path`` is a fixed-size byte buffer, so the kernel measures the
    UTF-8 encoding of the path. A name of emoji is one character and four bytes each — budgeting
    in ``len(name)`` would pass a leaf up to 4× over the real cap and reintroduce exactly the
    silent-exit bug this fitting exists to prevent.

    Truncate-plus-digest rather than a plain truncation: two worktrees cut from the same branch
    prefix (``feat/checkout-…`` twice over) would otherwise collapse onto ONE user-data-dir and
    silently share window state. The digest is of the FULL name, so the mapping is stable across
    runs and distinct for any two names — while the readable head keeps `ps` output and a stray
    state directory identifiable. The head is cut on a CHARACTER boundary (never mid-codepoint,
    which would leave an undecodable path), so it may come in under its byte budget.
    """
    if len(os.fsencode(name)) <= budget:
        return name
    digest = hashlib.sha256(os.fsencode(name)).hexdigest()[:_LEAF_HASH_LEN]
    head, used, room = "", 0, budget - _LEAF_HASH_LEN - 1  # -1 for the "-" joining head to digest
    for ch in name:
        width = len(os.fsencode(ch))
        if used + width > room:
            break
        head, used = head + ch, used + width
    head = head.rstrip("-")
    return f"{head}-{digest}" if head else digest


def _user_data_dir(worktree: str = "") -> Path:
    """The DEDICATED, per-worktree VS Code user-data-dir for box attaches.

    ``main`` is reserved as the primary checkout label by ``fy worktree add``, so sibling names
    can safely live beside it and map directly to ``vscode/<worktree>``. The name must be a
    single plain path component — an absolute or ``..``-carrying name would resolve OUTSIDE
    ``state_dir()/vscode`` (pathlib's ``/`` discards the base on an absolute right-hand side).

    A long name is FITTED (see :data:`_SUN_PATH_MAX`) rather than passed through, because VS Code
    derives its instance socket from this path and a name a few chars too long makes the editor
    exit before it can say why.
    """
    leaf = Path(worktree or "main")
    if len(leaf.parts) != 1 or leaf.parts[0] in ("..", "."):
        raise ValueError(f"unsafe worktree name for VS Code state: {worktree!r}")
    root = config.state_dir() / "vscode"
    # Encoded bytes throughout — `sun_path` is a byte buffer, and the ROOT can be non-ASCII too
    # (a state dir under a non-ASCII home).
    root_bytes = len(os.fsencode(str(root)))
    budget = max(_MIN_LEAF_BUDGET, _SUN_PATH_MAX - root_bytes - 1 - _IPC_SOCKET_BUDGET)
    return root / _fit_leaf(leaf.parts[0], budget)


def _globalstorage(udd: Path) -> Path:
    """Where the Dev Containers extension reads attached-container configs: UNDER the
    user-data-dir. So when we relocate the user-data-dir, the config write must target
    THIS path — not VS Code's default globalStorage — or the isolated instance never sees it."""
    return udd / "User" / "globalStorage" / _REMOTE_CONTAINERS


def _user_settings(udd: Path) -> Path:
    return udd / "User" / "settings.json"


# Settings foldyard PINS on the isolated instance every `fy code`.
#
# `remote.portsAttributes` — never auto-forward a port the box DIALS OUT to on the host. VS Code's
# auto-forwarding binds the forwarded port on the host's loopback, and for connections to
# 127.0.0.1 a loopback bind wins over the daemon's wildcard one, so forwarding a host-daemon port
# silently shadows the daemon behind it: the socket accepts, nothing ever answers. It found ours
# by reading `[minter] :8188` out of `fy host`'s own startup line, and every mint from the box
# then hung — the credential path dead with every posture surface still green.
#
# The same key guards the mirror case: a port the host PUBLISHES into the stack (`[ports]`). Under
# podman machine a publish of `127.0.0.1:P` is a gvproxy bind on the host's loopback, so if VS Code
# holds P first — it auto-forwarded 4400 off a `ps` line while the stack was down after a reboot,
# and `remote.restoreForwardedPorts` re-bound it on every reopen — the container can never start
# (`bind: address already in use` on every `up`, nothing in the box to see it with).
#
# `update.mode` — this instance is per-worktree scaffolding, not the operator's daily editor, so an
# update prompt on each attach is pure interruption (and updating a running attached instance is
# worse than deferring it). The operator's own VS Code is a separate install and is unaffected.
#
# `git.terminalAuthentication` / `git.useIntegratedAskPass` — off. On, the git extension running IN
# the box sets `GIT_ASKPASS` (+ its IPC socket) in every terminal it opens, so a `git push` there
# asks the HOST — the VS Code GitHub session, the keychain — for a credential: an HTTPS push path
# into a box whose posture is "never push", seen live 2026-09-17. Off, the bridge is never
# installed. Pinned at user scope (the instance's own settings.json, always written) AND machine
# scope (the attached config), like the port guard — but this is a DEFAULT, not enforcement:
# WORKSPACE settings win, and `.vscode/settings.json` is mount data the box can write. The
# enforcement for the git bridge is in-box (box._HARDEN_SNIPPET: the vars unset in every shell,
# the IPC socket reaped) and `fy verify`, which fails a checkout that flips these back on. Only
# the SSH side is by construction (`_empty_agent`): no setting chooses what agent is forwarded.
_PORTS_ATTRIBUTES = "remote.portsAttributes"
_UPDATE_MODE = "update.mode"
_GIT_BRIDGE_PINS = {"git.terminalAuthentication": False, "git.useIntegratedAskPass": False}
# Widest default worktree offset (`stack._offset`: cksum % 89 + 1).
_MAX_WORKTREE_OFFSET = 89


def _host_daemon_ports() -> list[int]:
    """Host ports the box connects OUT to, which must therefore never be forwarded back INTO it.
    The proxy is listed for the same reason as the minter, with a wider blast radius: shadowing it
    doesn't break one credential, it cuts all egress."""
    return [config.gcp_minter_port(), config.proxy_port()]


def _published_port_ranges() -> list[str]:
    """Every host port a `[ports]` base can publish on, as VS Code port-range keys
    (``"3000-3089"``). A range rather than this instance's own offset because any instance can
    forward any worktree's port — the main one picked up a worktree's ``APP_PORT+1`` from
    ``worktree add`` output. A pinned offset above the default span is not covered."""
    return [f"{base}-{base + _MAX_WORKTREE_OFFSET}" for base in config.port_bases().values()]


def _pin_ports(settings: dict) -> dict:
    """Merge the no-auto-forward pin into ``settings``, preserving every other key and every other
    port's attributes. The pin WINS over an existing value: it guards against a failure that
    presents as broken machinery rather than as a setting, so "the document asked for it" is not a
    reason to honour it — same rule as ``workspaceFolder``."""
    attrs = settings.get(_PORTS_ATTRIBUTES)
    merged = {str(k): v for k, v in attrs.items()} if isinstance(attrs, dict) else {}
    pinned = [str(p) for p in _host_daemon_ports()] + _published_port_ranges()
    for key in pinned:
        current = merged.get(key)
        # Override the one attribute that is the guard; a label or protocol the consumer set for
        # that port is its business and survives.
        kept = current if isinstance(current, dict) else {}
        merged[key] = {**kept, "onAutoForward": "ignore"}
    return {**settings, _PORTS_ATTRIBUTES: merged}


def _pinned_settings(settings: dict) -> dict:
    """The isolated instance's pins: the port guard plus the update mode. Applied to the
    user-data-dir's own settings.json, which foldyard always writes — the attached config carries
    the port guard too, but that file is skipped once an operator takes ownership of it, and a
    protection that disappears when someone customises an unrelated key is not a protection."""
    return {**_pin_ports(settings), **_GIT_BRIDGE_PINS, _UPDATE_MODE: "manual"}


def _local_terminal_zdotdir(udd: Path) -> Path:
    return udd / "foldyard-zdotdir"


def _write_local_terminal_cwd(
    udd: Path, host_checkout: Path, original_zdotdir: str | None = None
) -> bool:
    """Make VS Code's explicit *local terminal* command enter the host checkout.

    In a remote window, ``Create New Integrated Terminal (Local)`` explicitly launches the shell
    with the Mac user's home as its cwd. That launch argument takes precedence over
    ``terminal.integrated.cwd``; VS Code also deliberately bypasses configured terminal profiles
    for this local-in-remote case. It does apply ``terminal.integrated.env.osx``, so point zsh's
    ``ZDOTDIR`` at generated startup shims. They source the user's real dotfiles, restore our
    ``ZDOTDIR`` after each one, then cd after .zshrc. Everything stays in foldyard's isolated
    ``--user-data-dir``; the user's dotfiles are untouched.
    """
    path = _user_settings(udd)
    settings: dict[str, object] = {}
    if path.exists() and path.stat().st_size:
        try:
            loaded = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            _err(f"⚠ couldn't configure local terminal cwd in {path}:")
            _err("  settings.json is not strict JSON")
            _err(f"  ({e.msg} at line {e.lineno}, column {e.colno}).")
            _err(f"  Make Terminal: Create New Integrated Terminal (Local) cd to: {host_checkout}")
            return False
        except (OSError, UnicodeDecodeError) as e:
            _err(f"⚠ couldn't read {path}: {e}")
            return False
        if not isinstance(loaded, dict):
            _err(f"⚠ couldn't configure local terminal cwd in {path}:")
            _err("  top-level JSON is not an object")
            return False
        settings = loaded

    loaded_terminal_env = settings.get(_TERMINAL_ENV_OSX, {})
    if not isinstance(loaded_terminal_env, dict):
        _err(f"⚠ couldn't configure local terminal cwd: {_TERMINAL_ENV_OSX} is not an object")
        return False
    terminal_env: dict[str, object] = {
        str(key): value for key, value in loaded_terminal_env.items()
    }

    zdotdir = _local_terminal_zdotdir(udd)
    terminal_env["ZDOTDIR"] = str(zdotdir)
    terminal_env[_FOLDYARD_ZDOTDIR_ENV] = str(zdotdir)
    terminal_env[_LOCAL_TERMINAL_CWD_ENV] = str(host_checkout)
    terminal_env[_ORIGINAL_ZDOTDIR_ENV] = original_zdotdir or ""
    settings[_TERMINAL_ENV_OSX] = terminal_env
    # This is the one writer of the isolated instance's settings.json, so the instance pins ride
    # along with it rather than racing a second read-modify-write of the same file.
    settings = _pinned_settings(settings)

    # Migrate both ineffective approaches written by earlier versions. Preserve any unrelated
    # profiles the user added to this isolated settings file.
    settings.pop(_OBSOLETE_TERMINAL_CWD, None)
    if settings.get(_TERMINAL_DEFAULT_PROFILE_OSX) == _LOCAL_TERMINAL_PROFILE:
        settings.pop(_TERMINAL_DEFAULT_PROFILE_OSX)
    loaded_profiles = settings.get(_TERMINAL_PROFILES_OSX)
    if isinstance(loaded_profiles, dict) and _LOCAL_TERMINAL_PROFILE in loaded_profiles:
        profiles = {str(key): value for key, value in loaded_profiles.items()}
        profiles.pop(_LOCAL_TERMINAL_PROFILE, None)
        if profiles:
            settings[_TERMINAL_PROFILES_OSX] = profiles
        else:
            settings.pop(_TERMINAL_PROFILES_OSX)

    source_original = """_fy_original_zdotdir=${FOLDYARD_ORIGINAL_ZDOTDIR:-$HOME}
if [[ $_fy_original_zdotdir != $FOLDYARD_ZDOTDIR && -r "$_fy_original_zdotdir/{name}" ]]; then
  source "$_fy_original_zdotdir/{name}"
fi
export ZDOTDIR="$FOLDYARD_ZDOTDIR"
unset _fy_original_zdotdir
"""
    try:
        zdotdir.mkdir(parents=True, exist_ok=True)
        for name in (".zshenv", ".zprofile", ".zlogin", ".zlogout"):
            (zdotdir / name).write_text(source_original.replace("{name}", name))
        (zdotdir / ".zshrc").write_text(
            """if [[ ! -o login ]]; then
"""
            + source_original.replace("{name}", ".zprofile")
            + """fi
"""
            + source_original.replace("{name}", ".zshrc")
            + """if [[ -n $FOLDYARD_LOCAL_TERMINAL_CWD && -d $FOLDYARD_LOCAL_TERMINAL_CWD ]]; then
  builtin cd -- "$FOLDYARD_LOCAL_TERMINAL_CWD"
fi
"""
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n")
    except OSError as e:
        _err(f"⚠ couldn't write {path}: {e}")
        return False
    return True


def _running(engine: str, box: str, env: dict) -> bool:
    out = subprocess.run(
        [engine, "ps", "-q", "-f", f"name=^{box}$", "-f", "status=running"],
        env=env,
        capture_output=True,
        text=True,
    )
    return bool(out.stdout.strip())


def _installed_exts(engine: str, box: str, env: dict) -> str:
    """The extension dirs already present in the box's server (so the marker reset only fires
    for genuine newcomers). Empty string if the box/dir can't be read — same as the recipe."""
    out = subprocess.run(
        [engine, "exec", box, "sh", "-c", 'ls "$HOME/.vscode-server/extensions" 2>/dev/null'],
        env=env,
        capture_output=True,
        text=True,
    )
    return out.stdout if out.returncode == 0 else ""


def _valid_extensions(ids: list[str]) -> list[str]:
    """``ids`` as VS Code will get them: shape-validated (a marketplace id, nothing else),
    de-duplicated in order, and without the Dev Containers extension — meaningless inside the
    container it attached through."""
    exts: list[str] = []
    for e in ids:
        if _EXT_ID.match(e) and e != _REMOTE_CONTAINERS and e not in exts:
            exts.append(e)
    return exts


def _attached_config(checkout: str, exts: list[str], settings: dict) -> dict:
    """The attached-container config for ``checkout``: the consumer's `[vscode.settings]` table
    with the daemon-port pin merged in (the pin WINS — same rule as ``workspaceFolder``), and the
    extensions in BOTH schemas so they are *installed* on attach, not merely recommended —
    ``customizations.vscode.{extensions,settings}`` is the unified form newer Dev Containers
    versions honour, top-level ``extensions``/``settings`` the legacy one older versions read."""
    pinned = {**_pin_ports(settings), **_GIT_BRIDGE_PINS}
    return {
        _GENERATED_MARKER_KEY: _GENERATED_MARKER,
        "workspaceFolder": checkout,
        "remoteUser": _REMOTE_USER,
        "extensions": exts,
        "settings": pinned,
        "customizations": {"vscode": {"extensions": exts, "settings": pinned}},
    }


def _empty_agent(udd: Path) -> str | None:
    """The SSH agent the launched VS Code gets: foldyard's OWN, holding no identities — so what the
    Dev Containers attach forwards into the box is empty by construction. The attach forwards
    whatever ``SSH_AUTH_SOCK`` the VS Code process holds (shown 2026-09-17: the process keeps the
    LAUNCH env's value verbatim; only an UNSET var makes the extension go and find the host's own
    agent — the one with the operator's keys). Per user-data-dir, so per worktree instance; started
    once and reused while it answers. Refuses (``None``) if it ever holds a key: someone ran
    `ssh-add` against it, and an agent that is not empty has no business being forwarded."""
    sock = udd / "fy-empty-agent.sock"
    agent, add = shutil.which("ssh-agent"), shutil.which("ssh-add")
    if not agent or not add:
        # No OpenSSH on the host: a SET-but-dead path still beats unset (unset is the fallback
        # that finds the real agent), and `fy verify` in the box reports whatever got forwarded.
        return str(udd / "no-agent.sock")
    env = {**os.environ, "SSH_AUTH_SOCK": str(sock)}
    listed = subprocess.run([add, "-l"], env=env, capture_output=True, text=True)
    if listed.returncode == 0:  # identities present — never forward those
        _err(f"✗ the isolated VS Code's SSH agent at {sock} holds identities; it must stay empty.")
        _err(f"  Remove them: SSH_AUTH_SOCK={sock} ssh-add -D  (or delete the socket), then retry.")
        return None
    if listed.returncode != 1:  # 1 = alive and empty; anything else = not answering → (re)start
        udd.mkdir(parents=True, exist_ok=True)
        sock.unlink(missing_ok=True)
        subprocess.run([agent, "-a", str(sock)], stdout=subprocess.DEVNULL, check=False)
    return str(sock)


class _Unreadable:
    """What :func:`_read_config` answers when the file is there but could not be READ (permissions,
    a transient I/O error). Distinct from ``None`` — "nothing to preserve" — because a file whose
    contents are unknown might be the operator's, and the safe answer to "may I overwrite it?" is
    no."""


_UNREADABLE = _Unreadable()


def _read_config(path: Path) -> dict | _Unreadable | None:
    """The attached config currently on disk: ``None`` when there isn't a JSON object there
    (missing, garbage, or a non-dict document — all "nothing to preserve"), :data:`_UNREADABLE`
    when there is a file but reading it failed."""
    if not path.exists():
        return None
    try:
        existing = json.loads(path.read_text())
    except OSError:
        return _UNREADABLE
    except ValueError:
        return None
    return existing if isinstance(existing, dict) else None


def _write_attached_config(path: Path, cfg: dict, existing: dict | _Unreadable | None) -> bool:
    """Write the config into the isolated instance's globalStorage. Returns False unless
    ``existing`` (what :func:`_read_config` found at ``path``) is OURS — i.e. its ``_generatedBy``
    is exactly our marker. Anything else is somebody's file to keep: the user took ownership by
    removing the marker, or another tool wrote its own. A malformed document (``null``, a list,
    garbage) reads as ``None`` — not owned, safe to replace; one we could not read at all is kept,
    since we cannot tell whose it is."""
    if isinstance(existing, _Unreadable):
        print(f"  (kept {path}: could not read it to check whether it is foldyard's)")
        return False
    if existing is not None and existing.get(_GENERATED_MARKER_KEY) != _GENERATED_MARKER:
        print(f"  (kept your customised {path})")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"▶ extensions: {len(cfg.get('extensions', []))} → {path}")
    return True


def _missing_extensions(exts: list[str], installed: str) -> list[str]:
    """Configured ids the box's server doesn't have yet. ``installed`` is its extensions dir
    listing, whose entries are ``<publisher>.<name>-<version>`` — so match on the id prefix."""
    have = [line.strip().lower() for line in installed.splitlines() if line.strip()]
    return [e for e in exts if not any(d.startswith(e.lower() + "-") for d in have)]


def _reset_markers(engine: str, box: str, env: dict, markers: list[str]) -> None:
    """Delete the given once-per-install markers (and, for settings, the rendered file — see
    :data:`_MACHINE_SETTINGS_FILE`) in the box so the NEXT attach re-applies the matching part of
    the config (see :data:`_INSTALL_EXTENSIONS_MARKER`)."""
    if not markers:
        return
    paths = " ".join(f'"$HOME/.vscode-server/data/Machine/{m}"' for m in markers)
    subprocess.run(
        [engine, "exec", box, "sh", "-c", f"rm -f {paths}"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def code() -> int:
    """Resolve the (worktree's) box, ensure it's running, refresh its extensions config, then
    launch VS Code attached to it in the isolated user-data-dir. Mac only."""
    # `[vscode]` decides what the HOST installs and applies, so it is read from the checkout's
    # ADOPTED config, never the working tree (ADR-0022: the channel, not the field) — and the
    # adopt/revert/ignore gate runs first, before `stack.resolve()` can so much as provision the
    # machine, so a drifted table meets the operator here, before the host acts on anything.
    # With a pin in place every outcome is safe to proceed on (`ignored`/`unresolved` keep the
    # ADOPTED copy in force — `fy up` proceeds on them for the same reason). Two are not: the
    # gate itself failing (state unknown; `effective()` degrades to the tree on an unreadable
    # state dir), and NO pin at all after an operator declined to adopt — there `effective()`
    # falls back to the working tree, which for this verb means the tree chose what the host
    # installs. So `fy code` refuses both rather than inheriting `fy up`'s keep-going default.
    status = configpin.gate("fy code")
    if status == "error":
        _err("✗ fy code: couldn't check foldyard.toml against the adopted copy — not launching.")
        return 1
    cfg = devmode.worktree_config(config.active_worktree())
    if not config.in_box() and not configpin.inspect(cfg).pinned_exists:
        _err("✗ fy code: nothing adopted for this checkout, and [vscode] is read ONLY from the")
        _err("  adopted copy. Run `fy config adopt` (`fy config diff` first), then retry.")
        return 1
    ctx = stack.resolve()
    if not cfg.vscode_enabled():
        _err("✗ VS Code support is off — add a [vscode] table to foldyard.toml, then `fy box up`")
        _err("  (it mounts the persisted vscode-server volume the attach reuses).")
        return 1
    engine = config.engine()
    env = ctx.env
    box = f"{ctx.project}-devbox"

    if not _running(engine, box, env):
        hint = f"WORKTREE={ctx.worktree} fy box up" if ctx.worktree else "fy box up"
        _err(f"✗ dev box {box} not running. Start it first: {hint}")
        return 1

    checkout = env.get("FOLDYARD_CHECKOUT") or str(ctx.main)
    host_checkout = Path(checkout)
    uri = _uri(box, checkout)
    udd = _user_data_dir(ctx.worktree)
    print(f"▶ box:    {box}")
    print(f"▶ folder: {checkout}  (inside the box)")
    print(f"▶ uri:    {uri}")
    print(f"▶ isolated VS Code: --user-data-dir {udd}  (your default VS Code is untouched)")
    if _write_local_terminal_cwd(udd, host_checkout, env.get("ZDOTDIR")):
        print(f"▶ local terminal cwd: {host_checkout}")

    # Auto-apply extensions + settings on attach (Attach to Running Container ignores
    # devcontainer.json — the supported lever is an attached-container config, keyed by the box
    # name, UNDER this user-data-dir's globalStorage). Built here from the adopted `[vscode]`
    # table — nothing under the mount is read; the config file is the only host-side write.
    exts = _valid_extensions(cfg.vscode_extensions())
    attached = _attached_config(checkout, exts, cfg.vscode_settings())
    cfg_path = _globalstorage(udd) / "nameConfigs" / f"{box}.json"
    previous = _read_config(cfg_path)
    if _write_attached_config(cfg_path, attached, previous):
        markers = []
        missing = _missing_extensions(exts, _installed_exts(engine, box, env))
        if missing:
            markers.append(_INSTALL_EXTENSIONS_MARKER)
            print(f"▶ will install on attach: {' '.join(missing)}")
        # Settings have no in-box listing to diff against, so the last config WE wrote is the
        # record of what the box's Machine settings hold; any difference (a first write included)
        # needs the marker gone or the change never lands on an already-attached box.
        if not isinstance(previous, dict) or previous.get("settings") != attached["settings"]:
            markers += [_WRITE_MACHINE_SETTINGS_MARKER, _MACHINE_SETTINGS_FILE]
        _reset_markers(engine, box, env, markers)

    code_cli = shutil.which("code")
    if not code_cli:
        if config.in_box():
            # `fy code` launches the *host's* VS Code (attaches it back to this box over the
            # engine socket) — there's no box→host bridge, and the box image has no `code`.
            run_hint = f"WORKTREE={ctx.worktree} fy code" if ctx.worktree else "fy code"
            _err("✗ `fy code` has to run on your Mac, not inside the dev box — it launches the")
            _err("  host's VS Code and attaches it back to this box. Open a terminal on the Mac")
            _err(f"  (in your checkout) and run `{run_hint}` there.")
            return 1
        _err("✗ `code` (the VS Code CLI) isn't on your PATH. Install it: open VS Code, then")
        _err("  Cmd-Shift-P → \"Shell Command: Install 'code' command in PATH\",")
        _err("  and re-run `fy code`. Manual fallback: in VS Code, Cmd-Shift-P →")
        _err(f"  'Dev Containers: Attach to Running Container…' → {box}.")
        return 1

    # The attach is about to forward the host's SSH agent + git credentials into the box (Dev
    # Containers does, unswitchably); make sure the in-box hygiene that neutralises them is in
    # place and its socket reaper running BEFORE the server lands — see box._HARDEN_SNIPPET.
    from . import box as box_mod  # lazy: box.py pulls keyless/machine/sandbox, not needed above

    if not box_mod.ensure_harden(engine, box, env):
        _err(f"✗ couldn't apply the in-box editor-attach hygiene to {box} (bash in the box failed)")
        _err("  — not attaching: the attach would forward host credentials into an unguarded box.")
        _err("  `fy box shell` to look; `fy box down && fy box up` rebuilds it.")
        return 1
    agent = _empty_agent(udd)
    if agent is None:
        return 1
    print(f"▶ launching VS Code (DOCKER_HOST={env.get('DOCKER_HOST', '')})…")
    # `env` (incl. DOCKER_HOST) reaches ONLY this launched, isolated instance — never the
    # user's shell or their default VS Code. `|| true` parity: a non-zero `code` is non-fatal.
    # SSH_AUTH_SOCK is foldyard's empty agent (`_empty_agent`), never the operator's.
    subprocess.run(
        [code_cli, "--user-data-dir", str(udd), "--folder-uri", uri],
        env={**env, "SSH_AUTH_SOCK": agent},
    )
    print("✓ launched. If it didn't attach, check that VS Code's Docker context points at the")
    print("  same socket as DOCKER_HOST (`fy docs quickstart`).")
    return 0
