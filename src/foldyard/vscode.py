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
import tempfile
from pathlib import Path
from typing import IO

from . import config, stack

# The Dev Containers extension id whose globalStorage holds attached-container configs.
_REMOTE_CONTAINERS = "ms-vscode-remote.remote-containers"
# The ONLY keys foldyard will write into an attached-container config, and the only nested
# `customizations.vscode` keys — an ALLOWLIST, deliberately, because the document is produced from
# repo content. VS Code's attached-config schema includes lifecycle hooks, and `initializeCommand`
# runs ON THE HOST: honouring one would hand the repo the very host-execution path the packaged
# minters closed (ADR-0023). A denylist would fail OPEN the day the
# schema grows another hook, so unknown keys are dropped and named instead.
_ALLOWED_CONFIG_KEYS = frozenset(
    {"_generatedBy", "workspaceFolder", "remoteUser", "extensions", "settings", "customizations"}
)
_ALLOWED_CUSTOMIZATION_KEYS = frozenset({"extensions", "settings"})
_GENERATED_MARKER_KEY = "_generatedBy"
# Stamped by US, never copied from the document: the marker is how the NEXT run recognises a config
# as foldyard's rather than one you took ownership of. A generator that omitted it would otherwise
# lock us out of our own file forever.
_GENERATED_MARKER = "foldyard fy code"
# A marketplace extension id: `<publisher>.<name>`. Anything else in the list is dropped rather
# than handed to VS Code.
_EXT_ID = re.compile(r"^[A-Za-z0-9][\w-]*\.[A-Za-z0-9][\w-]*$")
# Bounds on the in-box generator (see `_generate_attached_config`): it reads a handful of
# `.vscode/extensions.json` files and prints a few KB, so anything past these is a broken or
# hostile generator, not a big project.
_GENERATOR_TIMEOUT = 120
_MAX_DOC_BYTES = 1 << 20

# How the generator is INVOKED in the box. Not a bare `python3`: the box contract promises git + uv
# + an engine client, never python (the packaged image is uv-first — debian:trixie-slim has no
# python3), so on a generic box that spelling made every attached-config generation fail. uv's
# managed interpreter is already there (the box bootstrap's foldyard install provisions one).
# `exec` so the generator keeps THIS pid — the timeout + stream caps below must land on it, not on
# a shell wrapping it; `--no-project` so the checkout's pyproject can't turn this into a sync.
_PY_SHIM = (
    'if command -v python3 >/dev/null 2>&1; then exec python3 "$@"; '
    'else exec uv run --no-project --quiet python "$@"; fi'
)
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
    user-data-dir. So when we relocate the user-data-dir, the config generator must target
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
# `update.mode` — this instance is per-worktree scaffolding, not the operator's daily editor, so an
# update prompt on each attach is pure interruption (and updating a running attached instance is
# worse than deferring it). The operator's own VS Code is a separate install and is unaffected.
_PORTS_ATTRIBUTES = "remote.portsAttributes"
_UPDATE_MODE = "update.mode"


def _host_daemon_ports() -> list[int]:
    """Host ports the box connects OUT to, which must therefore never be forwarded back INTO it.
    The proxy is listed for the same reason as the minter, with a wider blast radius: shadowing it
    doesn't break one credential, it cuts all egress."""
    return [config.gcp_minter_port(), config.proxy_port()]


def _pin_ports(settings: dict) -> dict:
    """Merge the no-auto-forward pin into ``settings``, preserving every other key and every other
    port's attributes. The pin WINS over an existing value: it guards against a failure that
    presents as broken machinery rather than as a setting, so "the document asked for it" is not a
    reason to honour it — same rule as ``workspaceFolder``."""
    attrs = settings.get(_PORTS_ATTRIBUTES)
    merged = {str(k): v for k, v in attrs.items()} if isinstance(attrs, dict) else {}
    for port in _host_daemon_ports():
        current = merged.get(str(port))
        # Override the one attribute that is the guard; a label or protocol the generator set for
        # that port is its business and survives.
        kept = current if isinstance(current, dict) else {}
        merged[str(port)] = {**kept, "onAutoForward": "ignore"}
    return {**settings, _PORTS_ATTRIBUTES: merged}


def _pinned_settings(settings: dict) -> dict:
    """The isolated instance's pins: the port guard plus the update mode. Applied to the
    user-data-dir's own settings.json, which foldyard always writes — the attached config carries
    the port guard too, but that file is skipped once an operator takes ownership of it, and a
    protection that disappears when someone customises an unrelated key is not a protection."""
    return {**_pin_ports(settings), _UPDATE_MODE: "manual"}


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
    """The extension dirs already present in the box's server (so the generator only flags
    genuine newcomers). Empty string if the box/dir can't be read — same as the recipe."""
    out = subprocess.run(
        [engine, "exec", box, "sh", "-c", 'ls "$HOME/.vscode-server/extensions" 2>/dev/null'],
        env=env,
        capture_output=True,
        text=True,
    )
    return out.stdout if out.returncode == 0 else ""


def _capped_text(sink: IO[bytes]) -> str:
    """The first :data:`_MAX_DOC_BYTES` of a captured stream, decoded with replacement. Never the
    whole file: the cap is what keeps a runaway generator's output off the heap, and
    ``errors="replace"`` is what keeps its invalid UTF-8 from becoming a traceback (we only ever
    json-parse or print this)."""
    sink.seek(0)
    return sink.read(_MAX_DOC_BYTES).decode("utf-8", errors="replace")


def _generate_attached_config(
    engine: str, box: str, script: Path, checkout: str, env: dict
) -> dict | None:
    """Run the consumer's attached-config generator INSIDE THE BOX and parse the JSON document it
    prints. ``None`` when it can't run or didn't produce a document.

    The generator is a repo file. Running it host-side made `fy code` execute whatever the checkout
    contained, as the operator — the same hole the minters had. It doesn't need the host: everything
    it READS (the projects' `.vscode/extensions.json`) and everything it WRITES ITSELF (the repo's
    gitignored `.vscode/settings.json`, the multi-root workspace file) is in the mount. Only the
    final write — into the Mac's VS Code globalStorage — is host-side, and that's foldyard's to do,
    from a validated document (:func:`_sanitize_attached_config`). `fy code` already requires a
    running box, so exec'ing in it costs nothing extra.

    Progress goes to the generator's stderr (relayed, so the human sees it); stdout is the
    document. Everything about that output is treated as UNTRUSTED, because it is repo code:

    * one that hangs (a stray `input()`, a wedged import) must not wedge `fy code` — hence the
      timeout;
    * one that prints a gigabyte must be REFUSED rather than parsed, and must not be held in MEMORY
      while it does — hence the temp-file sinks (the bytes land on disk and we read back at most the
      cap) and a COMBINED limit, which output split across the two streams can't slip past;
    * one that prints invalid UTF-8 must be a skipped config, not a `UnicodeDecodeError` out of
      `fy code` — hence decoding ourselves, with replacement, after the fact.
    """
    with tempfile.TemporaryFile() as sink, tempfile.TemporaryFile() as errsink:
        try:
            proc = subprocess.run(
                # "_" fills sh's $0 slot, so the generator's own args start at $1.
                [
                    engine,
                    "exec",
                    "-w",
                    checkout,
                    box,
                    "sh",
                    "-c",
                    _PY_SHIM,
                    "_",
                    str(script),
                    box,
                    checkout,
                ],
                env=env,
                stdout=sink,
                stderr=errsink,
                timeout=_GENERATOR_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            _err(
                f"⚠ the attached-config generator didn't finish in {_GENERATOR_TIMEOUT}s "
                "— attaching anyway."
            )
            return None
        total = sink.tell() + errsink.tell()
        stderr, stdout = _capped_text(errsink), _capped_text(sink)
    if stderr.strip():
        print(stderr.rstrip())
    if total > _MAX_DOC_BYTES:
        _err(
            f"⚠ the attached-config generator printed more than {_MAX_DOC_BYTES} bytes "
            "— skipping (an attached config is a few KB)."
        )
        return None
    if proc.returncode != 0:
        _err(f"⚠ the attached-config generator failed (exit {proc.returncode}) — attaching anyway.")
        return None
    try:
        doc = json.loads(stdout)
    except ValueError as e:
        _err(f"⚠ the attached-config generator didn't print a JSON document ({e}) — skipping.")
        return None
    return doc if isinstance(doc, dict) else None


def _sanitize_attached_config(doc: dict, checkout: str) -> dict:
    """Keep only the keys foldyard understands (see :data:`_ALLOWED_CONFIG_KEYS`), and pin the ones
    whose value has host consequences. What's dropped is NAMED, so a consumer adding a key doesn't
    silently lose it — they get told foldyard won't write it.

    Pinned rather than trusted: ``workspaceFolder`` must be the checkout we asked about (a different
    path would point the attach elsewhere), ``extensions`` must be plain marketplace ids, and
    ``settings`` must be a JSON object. Settings can't execute; lifecycle hooks can, which is why
    they aren't in the allowlist at all."""
    clean: dict = {}
    for key, value in doc.items():
        if key not in _ALLOWED_CONFIG_KEYS:
            _err(f"  (dropped unsupported attached-config key: {key})")
            continue
        if key == "customizations":
            vs = (value or {}).get("vscode") if isinstance(value, dict) else None
            if isinstance(vs, dict):
                kept = {k: v for k, v in vs.items() if k in _ALLOWED_CUSTOMIZATION_KEYS}
                for dropped in set(vs) - _ALLOWED_CUSTOMIZATION_KEYS:
                    _err(f"  (dropped unsupported customizations.vscode key: {dropped})")
                clean[key] = {"vscode": kept}
            continue
        clean[key] = value
    clean["workspaceFolder"] = checkout
    clean[_GENERATED_MARKER_KEY] = _GENERATED_MARKER
    raw_exts = clean.get("extensions")
    # A null/scalar `extensions` is a malformed document, not a crash: iterate only a real list.
    exts = (
        [e for e in raw_exts if isinstance(e, str) and _EXT_ID.match(e)]
        if isinstance(raw_exts, list)
        else []
    )
    clean["extensions"] = exts
    raw_settings = clean.get("settings")
    clean["settings"] = _pin_ports(raw_settings if isinstance(raw_settings, dict) else {})
    vs = clean.get("customizations", {}).get("vscode") if "customizations" in clean else None
    if isinstance(vs, dict):
        vs["extensions"] = exts
        if not isinstance(vs.get("settings"), dict):
            vs.pop("settings", None)
    return clean


def _write_attached_config(path: Path, cfg: dict) -> bool:
    """Write the sanitized config into the isolated instance's globalStorage. Returns False unless
    the existing config is OURS — i.e. its ``_generatedBy`` is exactly our marker. Anything else is
    somebody's file to keep: the user took ownership by removing the marker, or another tool wrote
    its own. (The check lives here, host-side, because the file does.)"""
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, ValueError):
            existing = None  # unreadable/garbage — safe to replace with ours
        # A non-dict document (`null`, a list) is malformed, not owned — and `in`/`.get` on it
        # would be a TypeError out of `fy code`, hence the isinstance check rather than a lookup.
        if isinstance(existing, dict) and existing.get(_GENERATED_MARKER_KEY) != _GENERATED_MARKER:
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


def _reset_install_marker(engine: str, box: str, env: dict) -> None:
    """VS Code applies an attached-config's extensions only ONCE per server install (gated by
    .installExtensionsMarker). Delete it so the NEXT attach installs the newcomers."""
    subprocess.run(
        [
            engine,
            "exec",
            box,
            "sh",
            "-c",
            'rm -f "$HOME/.vscode-server/data/Machine/.installExtensionsMarker"',
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def code() -> int:
    """Resolve the (worktree's) box, ensure it's running, refresh its extensions config, then
    launch VS Code attached to it in the isolated user-data-dir. Mac only."""
    if not config.vscode_enabled():
        _err("✗ VS Code support is off — add a [vscode] table to foldyard.toml, then `fy box up`")
        _err("  (it mounts the persisted vscode-server volume the attach reuses).")
        return 1
    ctx = stack.resolve()
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

    # Auto-install our extensions on attach (Attach to Running Container ignores
    # devcontainer.json — the supported lever is an attached-container config, keyed by the
    # box name, UNDER this user-data-dir's globalStorage).
    script = ctx.main / config.dev_vm_rel() / "vscode-attached-config.py"
    if script.is_file():
        doc = _generate_attached_config(engine, box, script, checkout, env)
        if doc is not None:
            cfg = _sanitize_attached_config(doc, checkout)
            cfg_path = _globalstorage(udd) / "nameConfigs" / f"{box}.json"
            if _write_attached_config(cfg_path, cfg):
                missing = _missing_extensions(
                    cfg.get("extensions", []), _installed_exts(engine, box, env)
                )
                if missing:
                    _reset_install_marker(engine, box, env)
                    print(f"▶ will install on attach: {' '.join(missing)}")
    else:
        print(f"  (no {script.name} alongside the recipe — extensions auto-install skipped)")

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

    # Multi-root workspace attach: when the consumer declares `[vscode] workspace_file` and
    # the file exists (the attached-config generator above may have just written it), open it
    # via --file-uri instead of the folder. Checked AFTER the generator so a fresh checkout's
    # first `fy code` already gets the workspace.
    open_flag = "--folder-uri"
    ws_rel = config.vscode_workspace_file()
    if ws_rel:
        if (host_checkout / ws_rel).is_file():
            open_flag, uri = "--file-uri", _uri(box, f"{checkout}/{ws_rel}")
            print(f"▶ workspace: {ws_rel}  (multi-root)")
        else:
            print(f"  ({ws_rel} not in the checkout — folder attach instead)")

    print(f"▶ launching VS Code (DOCKER_HOST={env.get('DOCKER_HOST', '')})…")
    # `env` (incl. DOCKER_HOST) reaches ONLY this launched, isolated instance — never the
    # user's shell or their default VS Code. `|| true` parity: a non-zero `code` is non-fatal.
    subprocess.run([code_cli, "--user-data-dir", str(udd), open_flag, uri], env=env)
    print("✓ launched. If it didn't attach, check that VS Code's Docker context points at the")
    print("  same socket as DOCKER_HOST (`fy docs quickstart`).")
    return 0
