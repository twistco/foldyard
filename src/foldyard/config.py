"""foldyard configuration & path resolution (consumer-side).

Answers the rest of the package's "where/what/which" questions, project-agnostically:

  - **Where is the consumer repo?** (``repo_root``)
  - **What is the project called?** (``project`` — prefixes the host state dir)
  - **What names the stack?** (``project_prefix``, ``app_service``) and **which
    ports / compose files does it use?** (``port_bases``, ``compose_files``)
  - **Which container engine drives it?** (``engine`` — podman where present, else
    docker for the dev box)
  - **How is the rootless machine sized/named?** (``machine_name``, ``machine_resources``)
  - **Where does host-side posture state live?** (``~/.foldyard/<project>/`` —
    deliberately OUTSIDE the repo mount, so nothing in the VM/box can escalate its
    own posture; ADR-0006)
  - **Where do the dev-VM assets live?** (``dev_vm_dir`` — compose overrides and the
    gitignored mode mirror. DATA PATHS ONLY: the credential minters and the egress-proxy
    addon are packaged, because the host must never execute a file from the mount —
    ADR-0023.)

Every project-specific value is lifted out of the package and read from here, so
foldyard works for any consumer that ships a ``foldyard.toml`` (docs/configuration.md).
Sources, in priority order: explicit env vars (always win — the SPIKE rule that
explicit env beats mode-derived config), then ``foldyard.local.toml`` (gitignored,
per-developer; deep-merged over the next — and able to SUBTRACT a block with
``disabled = true``, see :func:`merge_config`), then ``foldyard.toml`` in the repo
root, then sensible defaults. Stdlib only — this runs on the Mac system python3 and
in the box, on the recipe hot path.
"""

from __future__ import annotations

import contextvars
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


def _git_toplevel(start: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except Exception:
        pass
    return None


# ── resolved-config object + ambient binding (per-consumer / per-worktree) ─────────────
# A ``Config`` is a foldyard config resolved for ONE consumer checkout + worktree: its repo
# root, its worktree key, and its parsed+merged ``foldyard.toml``. Today config is read through
# ~50 module-level functions backed by an lru-cached ambient parse; that works per-invocation
# (the recipes set ``FOLDYARD_REPO``/``WORKTREE`` for the active checkout) but a LONG-LIVED
# process — ``fy host``'s supervisor, the TUI — needs to resolve several worktrees' configs at
# once (the per-consumer registry + per-worktree posture; see the per-consumer-registry-plan doc).
#
# So a ``Config`` can be BOUND for the duration of a ``with config.using(cfg): …`` block: while
# bound, every module function (and therefore every plugin reading ``config.X()``) resolves
# against ``cfg`` instead of the ambient env/CWD. This keeps the module-function API (so plugins
# and the test suite's ``monkeypatch.setattr(config, "X", …)`` are unchanged) while letting the
# registry + state paths be an explicit function of a resolved config.


@dataclass(frozen=True)
class Config:
    """A foldyard config resolved for one consumer checkout + worktree (the unit the registry
    and posture state key on). Bind it with :func:`using`; build one with :func:`resolve`.
    Attribute access for any module-level config function (``cfg.gcp_project()``,
    ``cfg.mode_file()``) runs that function under this config's binding — one implementation,
    no duplication."""

    repo_root: Path
    worktree: str  # "" = the main checkout; otherwise the worktree dir name
    toml: dict

    def has_table(self, name: str) -> bool:
        """Does this config DECLARE the top-level table ``name`` (even empty)? The opt-in
        signal for config-gated plugins (the core/declared split, registry plan Step C)."""
        return isinstance(self.toml.get(name), dict)

    def __getattr__(self, name: str) -> Any:
        # Ergonomic delegation: cfg.<module-config-fn>(…) == that function under `with using(self)`.
        # Only public module-level callables qualify; everything else is a real attribute error.
        if not name.startswith("_"):
            fn = globals().get(name)
            if callable(fn):

                def bound(*args: Any, **kwargs: Any) -> Any:
                    with using(self):
                        return fn(*args, **kwargs)

                return bound
        raise AttributeError(name)


_BOUND: contextvars.ContextVar[Config | None] = contextvars.ContextVar(
    "foldyard_config", default=None
)


@contextmanager
def using(cfg: Config) -> Iterator[Config]:
    """Bind ``cfg`` as the active config for the duration of the block: every config function
    (and every plugin reading ``config.X()``) resolves against it. Restores the previous binding
    on exit, so it nests and is exception-safe."""
    token = _BOUND.set(cfg)
    try:
        yield cfg
    finally:
        _BOUND.reset(token)


def bound_config() -> Config | None:
    """The currently-bound :class:`Config`, or ``None`` when resolving ambiently."""
    return _BOUND.get()


def active_worktree() -> str:
    """The active worktree key — a bound config's wins; else the ``WORKTREE`` env var the
    recipes/box export ("" ⇒ the main checkout). Kept here (not just in ``stack``) so posture
    state + the registry can key on the same worktree the stack does."""
    bound = _BOUND.get()
    if bound is not None:
        return bound.worktree
    return os.environ.get("WORKTREE", "")


@lru_cache(maxsize=256)
def worktree_offset(name: str) -> int:
    """Deterministic 1..89 host-port offset for a worktree, 0 for the main checkout (``""``).
    Reuses ``stack``'s POSIX-cksum offset so a worktree's PER-WORKTREE DAEMON PORTS (proxy listener,
    gcp minter) line up with its stack/app ports (ADR-0016 — the proxy port is per-worktree).
    Memoized per name — the supervisor reconcile loop asks for it every tick."""
    if not name:
        return 0
    from . import stack  # lazy: stack imports config at module load

    return stack._offset(name)


def worktree_suffix() -> str:
    """``""`` for the main checkout, else ``"@<worktree>"`` — the per-worktree DAEMON-NAME suffix so
    the one supervisor can reconcile N proxy/minter listeners without name clashes (main stays the
    bare ``egress-proxy``/``gcp-minter`` name + base port, byte-identical to the single-worktree
    world)."""
    wt = active_worktree()
    return f"@{wt}" if wt else ""


def _daemon_port_base(env_var: str, legacy: int, slot: int) -> int:
    """A Mac daemon family's base port: explicit env override → the project's allocated band
    (``ports.project_base``) + the family's slot. Per-PROJECT (not just per-worktree) because
    every project's supervisor binds its daemons on the one Mac — a shared base made two
    projects' supervisors reap each other's proxies forever (see ports.py). In-box there's no
    registry (it lives in the Mac's ``~/.foldyard``), so ``box_up`` PINS both env vars into the
    box at create; the legacy pre-band base is only the fallback for boxes created before that."""
    env = os.environ.get(env_var)
    if env:
        return int(env)
    if in_box():
        return legacy
    from . import ports  # lazy: keep the module import-light (config is imported everywhere)

    return ports.project_base(project()) + slot


def proxy_port_base() -> int:
    """The egress-proxy base port for THIS PROJECT (``FY_PROXY_PORT`` env wins, else the
    allocated band's proxy slot). Worktrees offset from here (:func:`proxy_port`); the VM wall
    and its ``environment.d`` proxy env use the base directly."""
    from . import ports

    return _daemon_port_base("FY_PROXY_PORT", 8088, ports.PROXY_SLOT)


def gcp_minter_port_base() -> int:
    """The gcp-minter base port for THIS PROJECT (``GCP_MINTER_PORT`` env wins, else the
    allocated band's minter slot — a disjoint 90-port span, so no worktree's minter can land on
    another worktree's proxy port, which the old 9-apart 8079/8088 bases allowed)."""
    from . import ports

    return _daemon_port_base("GCP_MINTER_PORT", 8079, ports.MINTER_SLOT)


def proxy_port() -> int:
    """The egress-proxy listen port for the active worktree: :func:`proxy_port_base` +
    :func:`worktree_offset`. Per-worktree because a box can't be told apart by the proxy on a shared
    port — every box hits ``host.containers.internal`` — so per-branch posture REQUIRES per-worktree
    ports (ADR-0016). The box's ``FY_PROXY`` and the supervisor's listener both derive
    from this, so they always agree."""
    return proxy_port_base() + worktree_offset(active_worktree())


def gcp_minter_port() -> int:
    """The gcp SA-token minter listen port for the active worktree: :func:`gcp_minter_port_base` +
    :func:`worktree_offset`. Per-worktree so each worktree's metadata-emulator forwards to
    ITS OWN minter (gated by THAT worktree's gcp rung) — a ``gcp=off`` worktree simply has no minter
    on its port, so its emulator gets no token (zero secrets), with no leak from a sibling worktree
    that runs ``gcp=sa``."""
    return gcp_minter_port_base() + worktree_offset(active_worktree())


def resolve(worktree: str | None = None, repo: Path | None = None) -> Config:
    """Resolve a :class:`Config` for a checkout. With no args, snapshots the AMBIENT config
    (env/CWD-resolved repo root + parsed toml + active worktree) — i.e. ``current()`` when
    nothing is bound. Pass ``repo`` to read a SPECIFIC checkout's ``foldyard.toml`` (a worktree
    on another branch may declare different tables), and ``worktree`` to key its posture state."""
    if repo is not None:
        root = Path(repo).expanduser().resolve()
        toml = merge_config(
            _read_toml(root / "foldyard.toml"), _read_toml(root / "foldyard.local.toml")
        )
        return Config(repo_root=root, worktree=worktree or "", toml=toml)
    return Config(
        repo_root=_repo_root_ambient(),
        worktree=active_worktree() if worktree is None else worktree,
        toml=_toml_ambient(),
    )


def current() -> Config:
    """The active config: the bound one if inside a :func:`using` block, else a fresh ambient
    snapshot. The explicit handle the registry + per-worktree state thread instead of reading
    ambient process globals."""
    return _BOUND.get() or resolve()


@lru_cache(maxsize=1)
def _repo_root_ambient() -> Path:
    """The consumer repo root, resolved from the AMBIENT process env/CWD (memoized). The
    public :func:`repo_root` consults a bound :class:`Config` context first (so a long-lived
    ``fy host``/TUI can resolve a different checkout per worktree) and falls back here."""
    env = os.environ.get("FOLDYARD_REPO")
    if env:
        return Path(env).expanduser().resolve()
    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / "foldyard.toml").is_file():
            return d
    return (_git_toplevel(cur) or cur).resolve()


def repo_root() -> Path:
    """The consumer repo root. ``FOLDYARD_REPO`` wins (the recipes set it to
    ``justfile_directory()``); otherwise walk up from CWD to the ``foldyard.toml``
    marker, else fall back to the git top-level, else CWD. A bound :class:`Config`
    context (``with config.using(cfg): …``) overrides the ambient resolution."""
    bound = _BOUND.get()
    return bound.repo_root if bound is not None else _repo_root_ambient()


def clear_caches() -> None:
    """Drop the memoized AMBIENT resolution (repo root + parsed toml) so the next lookup
    re-resolves from env/CWD. Call after changing ``FOLDYARD_*`` env or writing ``foldyard.toml``
    (the allowlist + tests). A bound :class:`Config` is unaffected — it carries its own snapshot."""
    _repo_root_ambient.cache_clear()
    _toml_ambient.cache_clear()
    worktree_offset.cache_clear()
    # stack.main_repo is memoized for the same reason and invalidated by the same events (a test
    # relocating the repo, a rewritten foldyard.toml). Lazy + doubly guarded: config must not
    # import stack at module scope (stack imports config); a caller that never loaded stack has
    # nothing to clear; and tests routinely monkeypatch main_repo to a plain lambda, which has no
    # cache_clear — clearing a cache must never be what breaks their teardown.
    stack = sys.modules.get(f"{__package__}.stack")
    clear = getattr(getattr(stack, "main_repo", None), "cache_clear", None)
    if clear is not None:
        clear()


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge ``over`` INTO a copy of ``base`` (override wins). Nested tables merge
    key-by-key — so a local ``[claude].keyless`` lands BESIDE the committed
    ``[claude].system_prompt`` rather than replacing the whole table. Non-dict values (incl. arrays
    / arrays-of-tables) are replaced wholesale."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# The one key that SUBTRACTS. A deep merge can only ever add — which leaves no way to say "not for
# me" about a block the shared file declares, and several tables are PRESENCE-gated (`[claude]`,
# `[codex]`, `[vscode]`, every `[plugins.<name>]`): merely blanking their keys still leaves the
# table there, so the feature stays on. A colleague who doesn't have a Codex subscription would
# still get the CLI installed on box-up, a credential prompt at `fy box up`, and a codex row in
# `fy mode` — posture noise for a mechanism they don't use. `disabled = true` removes the block
# from the resolved document entirely, as if it had never been written.
DISABLE_KEY = "disabled"


def _prune_disabled(doc: dict) -> dict:
    """Drop every table carrying ``disabled = true`` (recursively, incl. entries in an
    array-of-tables), and the marker key itself from the tables that stay.

    Applied AFTER the merge, which is what makes the override direction work both ways: a local
    ``[codex] disabled = true`` deletes the shared table, and a local ``disabled = false`` puts a
    shared-disabled block back. Only a real TOML boolean counts, so a project that legitimately
    carries some other ``disabled`` value (a string, a number) keeps it."""
    out: dict = {}
    for key, value in doc.items():
        if key == DISABLE_KEY and isinstance(value, bool):
            continue  # the marker never survives into the resolved config
        if isinstance(value, dict):
            if value.get(DISABLE_KEY) is True:
                continue
            out[key] = _prune_disabled(value)
        elif isinstance(value, list) and any(isinstance(e, dict) for e in value):
            out[key] = [
                _prune_disabled(e) if isinstance(e, dict) else e
                for e in value
                if not (isinstance(e, dict) and e.get(DISABLE_KEY) is True)
            ]
        else:
            out[key] = value
    return out


def merge_config(shared: dict, local: dict) -> dict:
    """Resolve one checkout's ``(foldyard.toml, foldyard.local.toml)`` pair into the document the
    rest of foldyard reads: deep-merge the local overlay over the shared file, then apply
    :func:`_prune_disabled`.

    THE place the two files become one — the box/ambient parse here, the host's adopted snapshot in
    ``configpin.merged_toml`` — so the box, the supervisor and `fy config widenings` can never
    disagree about what a checkout declares."""
    return _prune_disabled(_deep_merge(shared, local))


def disabled_blocks(shared: dict, local: dict) -> list[tuple[str, bool]]:
    """``(dotted path, declared in the LOCAL file)`` for every block :func:`merge_config` removes.

    Subtraction is invisible in the resolved config by construction — the block simply isn't there —
    so the surfaces that report what a checkout declares (`fy config widenings`) ask for it here
    rather than trying to spot an absence."""
    found: list[tuple[str, bool]] = []

    def walk(merged: dict, over: dict, path: str) -> None:
        for key, value in merged.items():
            if not isinstance(value, dict):
                continue
            where = f"{path}.{key}" if path else key
            local_here = over.get(key) if isinstance(over, dict) else None
            if value.get(DISABLE_KEY) is True:
                found.append((where, isinstance(local_here, dict) and DISABLE_KEY in local_here))
                continue  # a disabled block's own sub-tables go with it — one line, not a tree
            walk(value, local_here if isinstance(local_here, dict) else {}, where)

    walk(_deep_merge(shared, local), local, "")
    return found


def _read_toml(path: Path) -> dict:
    """Parse one TOML file; best-effort ``{}`` if it's absent or malformed. tomllib is stdlib on
    3.11+ (the package's requires-python); if it's somehow absent, every file reads as ``{}`` and
    env / defaults carry."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover — <3.11 only
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):  # absent or malformed → contribute nothing
        return {}


@lru_cache(maxsize=1)
def _toml_ambient() -> dict:
    """Parse ``<repo>/foldyard.toml``, then deep-merge ``<repo>/foldyard.local.toml`` (gitignored,
    per-developer) OVER it — so personal posture (each dev's ``[claude]``/``[codex]`` ``keyless``,
    a private ``[[inject]]``, …) stays OUT of the shared, committed file and never imposes a
    credential prompt/warning on colleagues who don't use it — and drop what the merge
    ``disabled``s (:func:`merge_config`), the other direction of the same idea. Best-effort: a
    missing or malformed file contributes ``{}``; env vars still win downstream over both."""
    root = _repo_root_ambient()
    return merge_config(
        _read_toml(root / "foldyard.toml"), _read_toml(root / "foldyard.local.toml")
    )


def _toml() -> dict:
    """The resolved ``foldyard.toml`` (+ local override). Consults a bound :class:`Config`
    context first — so building/reading a registry for a specific consumer or worktree sees
    THAT config — and falls back to the ambient (env/CWD-resolved) parse otherwise."""
    bound = _BOUND.get()
    return bound.toml if bound is not None else _toml_ambient()


def _table(name: str) -> dict:
    """A top-level ``foldyard.toml`` table as a dict ({} if absent or malformed)."""
    table = _toml().get(name)
    return table if isinstance(table, dict) else {}


def _project_table() -> dict:
    return _table("project")


def project() -> str:
    """The project name — prefixes ``~/.foldyard/<project>/``. Resolved the SAME way
    under every python (env → toml → repo dir name) so the Mac's mode/host/tui all
    agree on one state dir."""
    return (
        os.environ.get("FOLDYARD_PROJECT")
        or _project_table().get("name")
        or repo_root().name.lower()
    )


def _version_declaration(key: str) -> str | None:
    """One of the ``[project]`` version-window keys, or ``None``.

    Non-strings are ignored rather than coerced: TOML parses ``min_foldyard_version = 0.4``
    as a float, and a float has no third component — silently reading it as "0.4" would make
    the floor mean something the author did not write. See :mod:`foldyard.compat`."""
    value = _project_table().get(key)
    return value if isinstance(value, str) and value else None


def min_foldyard_version() -> str | None:
    """``[project].min_foldyard_version`` — the floor below which fy refuses to run here."""
    return _version_declaration("min_foldyard_version")


def recommended_foldyard_version() -> str | None:
    """``[project].recommended_foldyard_version`` — nudge-only; never blocks."""
    return _version_declaration("recommended_foldyard_version")


def foldyard_version_reasons() -> dict[str, object]:
    """``[project.foldyard_version_reasons]`` — why this repo wanted each foldyard it adopted.

    A ledger, not a field: entries are appended and never rewritten, so no reason can drift out
    of date with the version it describes. Values are typed ``object`` because they are whatever
    the consumer's TOML held — :func:`foldyard.compat.reasons_between` does the narrowing, and
    annotating ``str`` here would only hide that it has to. See :mod:`foldyard.compat`."""
    table = _project_table().get("foldyard_version_reasons")
    return table if isinstance(table, dict) else {}


def dev_vm_rel() -> str:
    """The dev-VM asset dir as configured — the raw (usually relative) value.
    ``dev_vm_dir`` resolves it against ``repo_root``; ``foldyard.stack`` joins it onto
    the MAIN checkout instead (which differs from ``repo_root`` inside a worktree), so
    it needs the unresolved string. ``FOLDYARD_DEV_VM_DIR`` wins, then
    ``[project].dev_vm_dir``, then the repo root (``.``) — the project-agnostic default; a
    self-contained consumer keeps its (transitional) assets there, and sets ``dev_vm_dir``
    to relocate them (e.g. a project that tucks them under ``infra/...``)."""
    return os.environ.get("FOLDYARD_DEV_VM_DIR") or _project_table().get("dev_vm_dir") or "."


def dev_vm_dir() -> Path:
    """TRANSITIONAL: the dev-VM asset dir (minters, proxy addon, compose overrides,
    the gitignored ``.dev-mode.json`` mirror) resolved against the repo root. Becomes
    plugin-owned later (ADR-0015)."""
    p = Path(dev_vm_rel()).expanduser()
    return p if p.is_absolute() else (repo_root() / p)


# ── environment detection ────────────────────────────────────────────────────────────


def in_box() -> bool:
    """True inside the dev box (vs the host that manages the rootless machine/native engine).

    The box signature is explicit: ``IN_DEVBOX=1``. A socket-shaped ``DOCKER_HOST`` is not enough,
    because Linux/WSL2 hosts can legitimately export a local podman socket and still be the host.
    Guards that mean "am I on the host?" must use this, not ``which("podman")``."""
    return os.environ.get("IN_DEVBOX") == "1"


# ── container engine ───────────────────────────────────────────────────────────────


# The container engines foldyard drives. An override must name one of these — see `engine()`.
ENGINES = ("podman", "docker")


def engine() -> str:
    """The container-engine CLI that drives the compose stack — **podman** by default.

    podman IS foldyard's engine: the rootless machine is podman, and even the dev box's
    bind-mounted socket is that machine's podman socket. foldyard exports ``CONTAINER_HOST``
    = that socket (see ``foldyard.stack``) so plain ``podman``/``podman compose`` reach it
    everywhere — including the box, where podman's *local* mode would otherwise be broken
    (nested-podman). Override with ``FOLDYARD_ENGINE`` or ``[engine].cli``.

    **The docker fallback exists for CI, not as a second supported engine** (ADR-0010: "docker
    stays the CI fallback, not the engine"). GitHub-hosted runners ship docker and no podman, so
    ``engine()`` degrades there and ``docker compose`` runs the same stack — which is what gives
    ADR-0017 a real-engine e2e tier without KVM. On a developer host the fallback is not a
    configuration to aim for: machine lifecycle is podman-only either way (``podman machine`` has
    no docker equivalent, and the lima backend drives a podman service inside the VM), so a host
    with docker but no podman can run containers and still not have a machine. Read a resolved
    engine of ``docker`` outside CI as "podman is missing", not as "docker was chosen".

    NOTE this is a DIFFERENT binary from the machine backend's ``cli``
    (:func:`machine_backend`): the backend creates the VM, this drives the socket it hands out.
    Lima needs ``limactl`` *and* this.

    The override is a NAME from :data:`ENGINES`, never a path. `[engine].cli` is repo config, and
    this value is exec'd host-side by every engine verb (`fy up`, `fy box up`, the reconciler, the
    worktree init container) — so an unconstrained string would let anything that can write the
    checkout choose a binary the Mac then runs, which is the hole
    ADR-0023 closed everywhere else. Two engines exist; anything else
    is a typo or an attack, and both deserve the same loud refusal."""
    from shutil import which

    explicit = os.environ.get("FOLDYARD_ENGINE") or _table("engine").get("cli")
    if explicit:
        if explicit not in ENGINES:
            raise SystemExit(
                f"✗ engine {explicit!r} is not one of {', '.join(ENGINES)} — set [engine].cli "
                "(or FOLDYARD_ENGINE) to an engine NAME, not a path"
            )
        return explicit
    return "podman" if which("podman") else "docker"


def engine_compose() -> list[str]:
    """The engine's compose entry point, e.g. ``["podman", "compose"]``."""
    return [engine(), "compose"]


# ── stack naming / ports / compose files ─────────────────────────────────────────────


def project_prefix() -> str:
    """Prefix for container/volume/network names. ``FOLDYARD_PROJECT_PREFIX`` wins,
    then ``[project].prefix``, then the project name (a worktree appends its own
    suffix — see ``foldyard.stack``)."""
    return os.environ.get("FOLDYARD_PROJECT_PREFIX") or _project_table().get("prefix") or project()


def app_service() -> str:
    """The app's compose service name — target of ``shell``. ``FOLDYARD_APP`` wins,
    then ``[project].app``, then ``"app"``."""
    return os.environ.get("FOLDYARD_APP") or _project_table().get("app") or "app"


def app_port_key() -> str | None:
    """The ``[ports]`` key for the app's host-published port.

    This is explicit because port keys are consumer-defined: Foldyard must not assume names such
    as ``APP_PORT``. ``FOLDYARD_APP_PORT_KEY`` wins, then ``[project].app_port``; absent means the
    consumer has not configured a browsable app URL.
    """
    raw = os.environ.get("FOLDYARD_APP_PORT_KEY") or _project_table().get("app_port")
    return str(raw) if raw else None


def compose_files() -> list[str]:
    """Compose files in ``-f`` order, each relative to the active checkout (or
    absolute). ``[project].compose`` wins; defaults to ``<dev_vm_dir>/compose.podman.yml``."""
    raw = _project_table().get("compose")
    if isinstance(raw, list) and raw:
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw:
        return [raw]
    return [f"{dev_vm_rel().rstrip('/')}/compose.podman.yml"]


def has_compose_stack(base: Path) -> bool:
    """Does this project actually ship a compose stack — i.e. does a configured compose file
    exist on disk (checkout-relative to ``base``, or absolute)? ``compose_files()`` always
    returns a default path, so a stack-less project (no file authored) has none: ``up`` then
    has nothing to start (point the user at ``box up``), and ``box up`` must create the
    ``{project}_default`` network itself since no ``compose up`` will."""
    for f in compose_files():
        p = Path(f)
        if not p.is_absolute():
            p = Path(base) / f
        if p.exists():
            return True
    return False


def external_network() -> bool:
    """Does FOLDYARD own the ``{project}_default`` network's lifecycle (``[project].
    external_network``, false if absent)? Opt-in per consumer: the compose file must
    declare ``networks.default`` with ``external: true`` and an explicit ``name`` (e.g.
    ``${COMPOSE_PROJECT_NAME}_default``). foldyard then pre-creates the network on
    ``up``/``shellenv``/``box up`` and best-effort-removes it on ``nuke``. Why opt in:
    the dev box is a non-compose container permanently attached to that network, so a
    compose-owned network makes every ``down`` warn about the removal compose can't do,
    and ``box up`` before the first ``up`` dies on the network not existing yet.
    Consumers whose compose stack owns its own networks just leave this unset."""
    return bool(_project_table().get("external_network"))


def ensure_dirs() -> list[str]:
    """Bind-mount source dirs (relative to the checkout) the compose file expects to
    pre-exist — created empty on ``up``/``stubs``. From ``[project].ensure_dirs`` ([]
    if absent)."""
    raw = _project_table().get("ensure_dirs")
    return [str(x) for x in raw] if isinstance(raw, list) else []


def worktree_init() -> str | None:
    """Optional per-project init script ``foldyard worktree add`` runs in each new
    worktree, as ``sh <script> --source <main-repo>`` with cwd = the new worktree — it
    copies the project's gitignored local config (env files, editor/agent settings)
    across. The path is relative to ``repo_root`` (or absolute). ``FOLDYARD_WORKTREE_INIT``
    wins, then ``[project].worktree_init``, else None (foldyard just creates the bare
    git worktree)."""
    return os.environ.get("FOLDYARD_WORKTREE_INIT") or _project_table().get("worktree_init") or None


def port_bases() -> dict[str, int]:
    """Host-published port bases, keyed by the env-var name the compose file reads
    (e.g. ``APP_PORT``). A worktree adds a per-name offset to each. Empty when there's
    no ``[ports]`` table — then worktrees publish no offset ports (define ``[ports]``
    in ``foldyard.toml`` to enable second-stack-beside-main)."""
    out: dict[str, int] = {}
    for key, value in _table("ports").items():
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            continue
    return out


# ── egress proxy ────────────────────────────────────────────────────────────────────────


def proxy_enabled() -> bool:
    """True when the consumer opts INTO the egress proxy by declaring a ``[proxy]`` table (even
    an empty one). The proxy is the substrate github injection + capture ride on, so absent ⇒
    the dev box gets no proxy CA mount / ``HTTPS_PROXY``/``NO_PROXY`` env (a generic, stack-less
    consumer stays clean). An active injector (``GH_INJECT`` in the box env) lights it up too."""
    return _toml().get("proxy") is not None


def proxy_passthrough() -> list[str]:
    """``[proxy] passthrough`` — the TRUSTED hosts the egress proxy TLS-passes-through (does NOT
    MITM-decrypt) under ``capture=on``; everything else is decrypted + full-logged. Entries are
    exact hosts, ``*.suffix`` globs, or ``@bundle`` refs (``@all`` = every built-in bundle),
    expanded by the proxy plugin. Absent ⇒ ``["@all"]`` (trust the whole default toolchain); an
    explicit empty list ⇒ decrypt everything under capture=on."""
    raw = _table("proxy").get("passthrough")
    if raw is None:
        return ["@all"]
    return [str(x) for x in raw] if isinstance(raw, list) else []


def proxy_no_proxy() -> list[str]:
    """``[proxy] no_proxy`` — the consumer's OWN in-stack hostnames the dev box must reach
    directly, bypassing the egress proxy (they join the box's ``NO_PROXY``). The proxy runs on
    the Mac and can't resolve a stack-network name, so a proxied call to one 502s — or hangs
    first, which is how a proxied emulator read reads as a mysteriously slow test.

    ``localhost``/``127.0.0.1`` are always included, and foldyard's own plugins add their own
    services via ``Plugin.no_proxy_hosts`` — so this list is only the consumer's stack:
    ``no_proxy = ["{project}-postgres", "redis", "minio"]``. ``{project}`` expands to the compose
    project name (the prefix container names carry), which is what lets one entry work across
    worktrees. Absent/malformed ⇒ ``[]``.

    **Entries may not contain a dot or a ``*``, and that restriction is load-bearing.** This is
    repo config, so the box can write it, and NO_PROXY is a stronger exemption than the ``[proxy]
    passthrough`` hole ADR-0022 closed: a bypassed host isn't merely un-decrypted, it never reaches
    the proxy at all — no capture, and no ``default_deny`` refusal. Stack hostnames are single DNS
    labels, so refusing dots admits every legitimate use and structurally excludes every public
    host — and refusing ``*`` closes the same hole by the other door, since a lone ``NO_PROXY=*``
    (and every ``*``-bearing glob) bypasses the proxy wholesale without ever carrying a dot. Such
    an entry is a typo or an attack, and both get the same loud refusal as ``[engine].cli``."""
    raw = _table("proxy").get("no_proxy")
    if not isinstance(raw, list):
        return []
    out = [str(x).strip() for x in raw if str(x).strip()]
    if bad := [h for h in out if "." in h or "*" in h]:
        raise SystemExit(
            f"✗ [proxy] no_proxy may not contain dotted names or wildcards: {', '.join(bad)}\n"
            "  Only in-stack service/container names (single DNS labels) belong here — a dotted\n"
            "  or `*` entry would exempt PUBLIC hosts from capture and from the egress wall.\n"
            "  To reach an external host, grant it on the Mac instead: `fy allow add <host>`."
        )
    return out


def proxy_recommend() -> list[dict]:
    """``[proxy] recommend`` — hosts this repo RECOMMENDS operators grant through the egress wall,
    each ``{host, why}``. ADVISORY BY CONSTRUCTION: the proxy never reads it; the host offers to
    import entries into the allow-store at the adoption gate / ``fy allow sync`` / the TUI, and an
    operator answers per host. That is the difference from the removed ``[proxy] allow``: repo
    config (box-writable) can ASK for egress here, but only a Mac-side yes turns it into a grant —
    the same repo-proposes/host-decides shape as config adoption itself. This is also how a team
    SHARES its allowlist: the list travels with the branch, the grants stay host-owned.

    Entries are ``{ host = "pypi.org", why = "…" }`` tables or bare host strings (``why`` then
    empty). Malformed entries and invalid hosts are dropped rather than raising — this is read on
    prompt paths that must not crash — and the widenings report names what a config declares."""
    from .allowlist import valid_host  # stdlib-only, no cycle: allowlist never imports config-time

    raw = _table("proxy").get("recommend")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            host, why = entry, ""
        elif isinstance(entry, dict):
            host, why = str(entry.get("host") or ""), str(entry.get("why") or "")
        else:
            continue
        host = host.strip()
        if valid_host(host) and host not in {e["host"] for e in out}:
            out.append({"host": host, "why": why.strip()})
    return out


def proxy_default_deny() -> bool:
    """``[proxy] default_deny`` — when true the egress proxy ENFORCES the allowlist: any host
    that isn't allowed (by a live grant in the host-side allow-store, or as an injector host) is
    REFUSED (403 at CONNECT / on the request). Absent ⇒ false: capture/passthrough only, never
    blocks. This key only SEEDS the answer — enforcement is host-owned from then on
    (``fy allow wall``, see :func:`foldyard.allowlist.default_deny`), and the per-host grants
    live exclusively in the store (``fy allow add``): a ``[proxy] allow`` list is IGNORED
    (:data:`foldyard.exposure.IGNORED_KEYS`)."""
    return bool(_table("proxy").get("default_deny", False))


def inject_specs() -> list[dict]:
    """The ``[[inject]]`` array-of-tables — each entry is a generic egress-injector spec the
    ``inject`` plugin turns into an on/off mode axis + one proxy ``InjectRule`` (a header OR
    query-param rewrite on ``host`` with a host-side token). Absent/malformed ⇒ ``[]``; non-dict
    entries are skipped. See :mod:`foldyard.plugins.inject`."""
    raw = _toml().get("inject")
    if not isinstance(raw, list):
        return []
    return [dict(entry) for entry in raw if isinstance(entry, dict)]


# ── rootless machine ──────────────────────────────────────────────────────────────────


def worktrees_root(default_base: Path) -> Path:
    """Parent dir holding sibling worktree checkouts (an extra machine mount).
    ``FOLDYARD_WORKTREES_ROOT`` wins, then ``[machine].worktrees_root`` (relative
    values resolve as siblings of ``default_base``), then ``<default_base>-worktrees``."""
    env = os.environ.get("FOLDYARD_WORKTREES_ROOT")
    if env:
        return Path(env).expanduser()
    toml = _table("machine").get("worktrees_root")
    if toml:
        p = Path(str(toml)).expanduser()
        return p if p.is_absolute() else (Path(default_base) / p).resolve()
    return Path(f"{default_base}-worktrees")


def worktree_base() -> str | None:
    """Git ref a BRAND-NEW worktree branch forks from (``fy worktree add`` with a branch that
    doesn't exist yet). ``FOLDYARD_WORKTREE_BASE`` wins, then ``[machine].worktree_base``; None →
    foldyard auto-detects the repo's default branch (``origin/HEAD``, then ``main``/``master``).
    Set this only when the default branch isn't discoverable (no origin, or a non-standard name) —
    the point is that a new worktree forks off the shared trunk, NOT off whatever branch the
    primary checkout happens to have out."""
    return (
        os.environ.get("FOLDYARD_WORKTREE_BASE") or _table("machine").get("worktree_base") or None
    )


def machine_name() -> str:
    """The rootless podman machine's name. ``PODMAN_MACHINE`` wins, then
    ``[machine].name``, then the project name."""
    return os.environ.get("PODMAN_MACHINE") or _table("machine").get("name") or project()


def machine_backend_explicit() -> str:
    """The backend the consumer actually ASKED for (``MACHINE_BACKEND`` or ``[machine].backend``),
    or ``""`` when they said nothing and :func:`machine_backend` fell back to the default.

    The distinction matters wherever a missing backend CLI has to be classified: a CLI missing for
    a backend nobody chose is "this host doesn't do VMs" (skip quietly — a Linux/CI host, or the
    box managing its own VM), while one missing for a backend somebody NAMED is a misconfiguration
    that must fail loudly rather than silently fall back to a weaker profile."""
    return str(os.environ.get("MACHINE_BACKEND") or _table("machine").get("backend") or "").lower()


def machine_backend() -> str:
    """The engine backend. Explicit env/TOML wins; otherwise **lima**.

    Lima is the default because it is the only backend that delivers the full boundary: per-project
    VMs that run concurrently (``podman machine`` on macOS allows one at a time — upstream
    podman#26281), and the in-VM fail-closed egress wall (``[machine].wall``), which podman
    machine's CoreOS appliance can't be provisioned with. ``backend = "podman"`` remains supported
    and is the zero-extra-dependency floor. ``foldyard init`` has scaffolded ``lima`` + ``wall``
    since it shipped; this default just stops a hand-written config from silently getting less.

    Native Linux/WSL2 podman is available as an explicit opt-in (``backend = "native"``), but it is
    intentionally never the default because it drops the VM boundary entirely."""
    return machine_backend_explicit() or "lima"


def machine_wall() -> bool:
    """``[machine].wall`` — provision the in-VM nftables egress wall into the REAL lima machine, so
    the VM user's (and thus every container's) only way out is the Mac-side egress proxy at
    :data:`LIMA_HOST_GATEWAY` (fail-closed: egress that ignores the proxy env is REJECTED, not
    silently allowed). Only meaningful for ``backend = "lima"`` (podman-machine's immutable CoreOS
    appliance can't be provisioned like this; native has no VM) — preflight enforces the pairing.
    ``MACHINE_WALL`` env wins (1/true/on/yes ⇒ on)."""
    env = os.environ.get("MACHINE_WALL")
    if env is not None:
        return env.strip().lower() in ("1", "true", "on", "yes")
    return bool(_table("machine").get("wall", False))


# Lima's documented guest→host address: the user-mode network's host gateway, which the usernet
# forwards to the Mac (the same trick gvproxy plays with host.containers.internal, different
# constant). Lima also writes it into the guest's /etc/hosts as `host.lima.internal`, but that
# alias is invisible inside a podman CONTAINER in the guest — so foldyard emits the literal IP.
LIMA_HOST_GATEWAY = "192.168.5.2"


def host_alias() -> str:
    """The address a CONTAINER in the machine uses to reach the Mac-side foldyard daemons (egress
    proxy, gcp minter). Backend-dependent: under podman-machine, gvproxy publishes the Mac as
    ``host.containers.internal``, and native podman's netavark publishes the host under the same
    alias. Under LIMA that alias resolves to the VM's own gateway — NOT the Mac — so emit Lima's
    guest→host address instead (:data:`LIMA_HOST_GATEWAY`): container → pasta → VM → usernet → Mac.
    Emitting the literal IP sidesteps podman's alias machinery entirely (no containers.conf
    provisioning, works for already-created VMs). ``FY_HOST_ALIAS`` env is the escape hatch (e.g.
    a customised Lima network whose host gateway differs)."""
    explicit = os.environ.get("FY_HOST_ALIAS")
    if explicit:
        return explicit
    return LIMA_HOST_GATEWAY if machine_backend() == "lima" else "host.containers.internal"


def machine_vmtype() -> str:
    """``[machine].vmtype`` — the Lima DRIVER (hypervisor) the VM runs on, e.g. ``"vz"`` or
    ``"qemu"``. ``MACHINE_VMTYPE`` env wins. ``""`` (the default) means "resolve it from what
    this host actually offers" — see :meth:`machine_backend.LimaBackend.resolve_vmtype`.

    This exists because the VMM is a security decision and was previously an accident. Lima's
    own default is ``vz`` on macOS and ``qemu`` everywhere else, applied silently; QEMU is
    ~2M lines emulating decades of hardware and is where essentially every published VM-escape
    CVE lives, so which one you get should not depend on an unexamined ``runtime.GOOS`` branch
    inside a dependency. See docs/firecracker-and-microvm-backends.md.

    Lima-only, and **create-only** — like mounts and sizing, a live instance's vmType cannot be
    edited; changing it means ``fy machine recreate``."""
    return str(os.environ.get("MACHINE_VMTYPE") or _table("machine").get("vmtype") or "").lower()


def machine_resources() -> dict[str, str]:
    """``podman machine init`` sizing as strings. Env (``MACHINE_CPUS`` /
    ``MACHINE_MEMORY`` MiB / ``MACHINE_DISK`` GiB) wins, then ``[machine]``, then
    modest defaults."""
    m = _table("machine")
    return {
        "cpus": str(os.environ.get("MACHINE_CPUS") or m.get("cpus") or 4),
        "memory": str(os.environ.get("MACHINE_MEMORY") or m.get("memory_mib") or 8192),
        "disk": str(os.environ.get("MACHINE_DISK") or m.get("disk_gib") or 60),
    }


# ── dev box ([box]; ADR-0014) ──────────────────────────────────────────────────


def _box_table() -> dict:
    return _table("box")


def box_image() -> dict:
    """The dev-box base image: ``{dockerfile, tag, context?, target?, build_args?}`` (PLAN
    §7.4). When the consumer declares ``[box].image`` with a ``dockerfile`` it's repo-root-
    relative and the build context defaults to the repo root; ``tag`` defaults to
    ``<prefix>-devbox:latest``; ``target`` selects a multi-stage stage.

    When NO ``[box].image`` is declared, fall back to foldyard's **packaged generic box**
    (``src/foldyard/assets/box/Dockerfile`` — engine client + git + uv): an absolute
    ``dockerfile`` with the packaged dir as ``context`` (the generic box must not depend on
    repo contents). Lets a stack-less project ``foldyard box up`` with no Dockerfile to author
    (ADR-0013)."""
    img = _box_table().get("image")
    if isinstance(img, dict) and img.get("dockerfile"):
        img = dict(img)
        img.setdefault("tag", f"{project_prefix()}-devbox:latest")
        return img
    box_dir = Path(__file__).resolve().parent / "assets" / "box"
    return {
        "dockerfile": str(box_dir / "Dockerfile"),
        "context": str(box_dir),
        "tag": f"{project_prefix()}-devbox:latest",
    }


def box_sock_in_vm() -> str:
    """The rootless podman socket path INSIDE the VM (bind-mounted to the box's
    /var/run/docker.sock). ``PODMAN_SOCK_IN_VM`` wins, then ``[box].sock_in_vm``, then the
    active backend's guest-socket default (``/run/docker.sock`` for podman machine; Lima's
    rootless ``/run/user/<uid>/podman/podman.sock``)."""
    explicit = os.environ.get("PODMAN_SOCK_IN_VM") or _box_table().get("sock_in_vm")
    if explicit:
        return explicit
    from .machine_backend import get_backend  # lazy: machine_backend imports config

    return get_backend(machine_backend()).guest_socket()


def box_shadow_volumes() -> list[str]:
    """In-tree build-artifact dirs (relative to the checkout) shadowed with per-box named
    volumes — the bind-mount uid-squash workaround. From ``[box].shadow_volumes`` ([])."""
    raw = _box_table().get("shadow_volumes")
    return [str(x) for x in raw] if isinstance(raw, list) else []


def box_caches() -> list[dict]:
    """Shared caches mounted into the box: ``[{volume, path}]`` where ``path`` is relative
    to the box's HOME. From ``[box].caches`` ([])."""
    raw = _box_table().get("caches")
    out = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("volume") and item.get("path"):
                out.append({"volume": str(item["volume"]), "path": str(item["path"])})
    return out


def box_warmup() -> list[dict]:
    """Background dependency warm-up steps run after box-up: ``[{dir, run}]`` (each ``run``
    in ``<checkout>/<dir>``). From ``[box].warmup`` ([])."""
    raw = _box_table().get("warmup")
    out = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("dir") and item.get("run"):
                out.append({"dir": str(item["dir"]), "run": str(item["run"])})
    return out


def box_env() -> dict[str, str]:
    """Extra static env baked into the box (e.g. tool-cache pinning). ``~`` in a value is
    left for the box to expand against its HOME. From ``[box].env`` ({})."""
    raw = _box_table().get("env")
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def box_tools() -> list[dict]:
    """Consumer-defined one-time tool installs, run as MONITORED bootstrap steps:
    ``[{name, check?, install}]``. ``check`` is a shell guard (skip when it succeeds — defaults
    to ``command -v <name>``); ``install`` is the command. Keeps project-specific toolchain
    (e.g. pulumi) OUT of the foldyard package. From ``[[box.tools]]`` ([])."""
    raw = _box_table().get("tools")
    out = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name") and item.get("install"):
                step = {"name": str(item["name"]), "install": str(item["install"])}
                if item.get("check"):
                    step["check"] = str(item["check"])
                out.append(step)
    return out


def box_bootstrap() -> str:
    """Free-form shell the consumer wants run once per fresh box, after the structured steps —
    an escape hatch for setup that doesn't fit ``[[box.tools]]``. From ``[box].bootstrap`` ("")."""
    raw = _box_table().get("bootstrap")
    return str(raw) if isinstance(raw, str) else ""


def box_clean_docker_config() -> bool:
    """Point the box's ``DOCKER_CONFIG`` at a foldyard-owned clean config dir
    (``~/.docker-fy``, seeded ``{}`` by the bootstrap) instead of ``~/.docker``. Why: editor
    attaches (VS Code Dev Containers) write a ``credsStore`` credential helper into
    ``~/.docker/config.json`` that exits 255 under the box's root user, and the docker-compose
    provider calls it even for anonymous pulls — failing every image resolve. The clean config
    sidesteps that; ``docker login`` still works (it writes creds into the clean config).
    Opt out with ``[box] clean_docker_config = false`` to keep the image/editor-provided
    ``~/.docker`` (e.g. a setup that genuinely needs its credential helper). Default ON."""
    raw = _box_table().get("clean_docker_config")
    return raw if isinstance(raw, bool) else True


def box_git_index_split() -> bool:
    """Install the git index-split shim (``/usr/local/bin/git``) in the box bootstrap, so
    box-side git writes ``<gitdir>/index-box`` instead of racing host-side git on the shared
    checkout's ``.git/index`` (the two-kernel virtiofs backends' lockfile-atomicity gap — see
    docs/adrs/0021-per-kernel-git-index-split.md). Default ON; turning it off (or the install step
    failing) just leaves today's shared-index behaviour — nothing depends on the shim being
    present. From ``[box].git_index_split`` (true)."""
    raw = _box_table().get("git_index_split")
    return raw if isinstance(raw, bool) else True


# ── claude / vscode plugins ([claude] / [vscode]) ────────────────────────────
# Gated like [proxy]: the table's PRESENCE (even empty) opts the box into the feature, so a
# table-less consumer gets a plain shell box (no agent install, no agent/editor volumes).


def claude_enabled() -> bool:
    """True when the consumer declares a ``[claude]`` table — installs Claude Code on box-up and
    mounts its persisted config/native-version/transcripts volumes. Absent ⇒ shell-only box."""
    return _toml().get("claude") is not None


def claude_system_prompt() -> str:
    """Inline box-orientation prompt ``fy claude`` passes via ``--append-system-prompt``. From
    ``[claude].system_prompt`` (""). ``fy init`` seeds a generic default; edit it in place."""
    raw = _table("claude").get("system_prompt")
    return str(raw) if isinstance(raw, str) else ""


def claude_settings() -> dict:
    """``[claude.settings]`` — a settings table ``fy claude`` passes to ``--settings`` as JSON (the
    flag takes a file path OR a literal JSON string). Claude Code MERGES it into the settings
    hierarchy, so it overrides the box's ``~/.claude/settings.json`` per key rather than replacing
    it — the config-shaped sibling of ``system_prompt``, for the session knobs that aren't a prompt
    (``model``, ``env``, ``permissions``, …).

    Deep-merged like every table, so a personal ``[claude.settings] model`` in
    ``foldyard.local.toml`` lands beside the shared ones. Empty/absent ⇒ ``{}`` (no flag)."""
    raw = _table("claude").get("settings")
    return raw if isinstance(raw, dict) else {}


def claude_keyless() -> str:
    """``[claude].keyless`` — opt the box into KEYLESS Claude auth: NO real key/token in the box,
    the egress proxy injects the real one (held in ``host.env`` on the Mac) in flight, exactly like
    the github injector. Returns the mode the box's dummy + the proxy rule are derived from:

      - ``"api-key"`` (``true`` is an alias) — dummy ``ANTHROPIC_API_KEY``; rewrite ``x-api-key`` on
        ``api.anthropic.com`` from the real key in host.env.
      - ``"oauth"`` — dummy ``CLAUDE_CODE_OAUTH_TOKEN``; rewrite ``authorization`` with ``Bearer ``
        + the real ``sk-ant-oat…`` token (≈1yr, static — no refresh-minter needed).

    Absent/false/unrecognised ⇒ ``""`` (off — a normal login: real creds in the box's ~/.claude)."""
    raw = _table("claude").get("keyless")
    if raw is True or raw == "api-key":
        return "api-key"
    if raw == "oauth":
        return "oauth"
    return ""


def _transcript_sync_seconds(table: str) -> float:
    """``[<table>].transcript_sync_seconds`` — how often the supervisor promotes that agent's
    bound-out transcripts into its durable host archive while a session is running. ``0``/absent
    (the default) ⇒ off: transcripts are still archived, just only at ``box down``/``nuke``.

    Read HOST-side from the ADOPTED config (the supervisor binds ``devmode.worktree_config``), so a
    tree edit needs ``fy config adopt`` before it takes effect. Non-numeric, ``≤0`` and boolean
    values all read as off — this is an interval, and a config typo must not become a hot loop."""
    raw = _table(table).get("transcript_sync_seconds")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return 0.0
    return float(raw) if raw > 0 else 0.0


def claude_transcript_sync_seconds() -> float:
    """``[claude].transcript_sync_seconds`` — see :func:`_transcript_sync_seconds`."""
    return _transcript_sync_seconds("claude")


def codex_transcript_sync_seconds() -> float:
    """``[codex].transcript_sync_seconds`` — see :func:`_transcript_sync_seconds`."""
    return _transcript_sync_seconds("codex")


def vscode_enabled() -> bool:
    """True when the consumer declares a ``[vscode]`` table — mounts the vscode-server volume so
    ``fy code`` (VS Code attach) reuses its server across box recreations. Absent ⇒ no volume."""
    return _toml().get("vscode") is not None


def vscode_workspace_file() -> str:
    """Optional repo-relative ``.code-workspace`` path (``[vscode] workspace_file``). When set
    and present in the checkout, ``fy code`` opens THAT (a multi-root workspace) instead of the
    folder — the lever for per-folder editor settings in a monorepo (e.g. a different default
    formatter per sub-project, which a single-root workspace cannot express). Empty ⇒ folder
    attach."""
    raw = _table("vscode").get("workspace_file", "")
    return raw if isinstance(raw, str) else ""


def codex_enabled() -> bool:
    """True when the consumer declares a ``[codex]`` table — installs the OpenAI Codex CLI on
    box-up and mounts its persisted ``~/.codex`` (auth.json) volume. Absent ⇒ no Codex."""
    return _toml().get("codex") is not None


def codex_keyless() -> str:
    """``[codex].keyless`` — opt the box into KEYLESS Codex auth: NO real key in the box, the egress
    proxy injects the real ``OPENAI_API_KEY`` (held in ``host.env`` on the Mac) in flight, like the
    Claude/github injectors. Modes:

      - ``"api-key"`` (``true`` aliases it) — dummy ``OPENAI_API_KEY``; rewrite ``Authorization``
        with ``Bearer `` + the real key on ``api.openai.com``.
      - ``"chatgpt"`` — use your ChatGPT SUBSCRIPTION: the box holds a dummy ``~/.codex/auth.json``
        (far-future-exp JWT so it never refreshes), and the proxy injects the *current* access token
        (refreshed host-side from the Mac's real ``~/.codex/auth.json`` by the
        :mod:`~foldyard.plugins.codex_chatgpt_token` minter) onto ``chatgpt.com/backend-api/codex``.

    Absent/false/unrecognised ⇒ ``""`` (off — normal login)."""
    raw = _table("codex").get("keyless")
    if raw is True or raw == "api-key":
        return "api-key"
    if raw == "chatgpt":
        return "chatgpt"
    return ""


def codex_system_prompt() -> str:
    """Inline box-orientation prompt ``fy codex`` prepends to the session. From
    ``[codex].system_prompt`` (""), the same key and the same job as :func:`claude_system_prompt` —
    only the delivery differs, because Codex has no ``--append-system-prompt``: it goes in as a
    DEVELOPER INSTRUCTION (``codex -c developer_instructions=…``, see ``box._codex_argv``)."""
    raw = _table("codex").get("system_prompt")
    return str(raw) if isinstance(raw, str) else ""


def codex_config() -> dict:
    """``[codex.config]`` — config overrides ``fy codex`` passes as ``-c key=value``, the same job
    :func:`claude_settings` does for Claude and the same shape Codex's own ``~/.codex/config.toml``
    has, so a knob is written here exactly as you'd write it there::

        [codex.config]
        model_reasoning_summary = "auto"
        tui = { raw_output_mode = false }

    NESTED tables are fine: ``box._codex_overrides`` flattens them to Codex's dotted paths
    (``tui.raw_output_mode``), which override a single leaf rather than replacing the whole table.
    Empty/absent ⇒ ``{}`` (no ``-c`` beyond the prompt)."""
    raw = _table("codex").get("config")
    return raw if isinstance(raw, dict) else {}


# ── gcp-metadata plugin config ([plugins.gcp-metadata]) ────────────────────────────────


def _gcp_table() -> dict:
    plugins = _toml().get("plugins")
    table = plugins.get("gcp-metadata") if isinstance(plugins, dict) else None
    return table if isinstance(table, dict) else {}


def gcp_metadata_declared() -> bool:
    """True when the consumer declares ``[plugins.gcp-metadata]`` (even empty) — the opt-in that
    LOADS the gcp plugin (registry plan Step C). A generic repo that declares none never sees the
    gcp axis/daemon/box-wiring. The axis additionally self-gates on ``gcp_project`` (Step D)."""
    plugins = _toml().get("plugins")
    return isinstance(plugins, dict) and isinstance(plugins.get("gcp-metadata"), dict)


def gcp_project() -> str:
    """GCP project whose SAs the metadata-emulator minter impersonates. ``GCP_PROJECT``
    wins, then ``[plugins.gcp-metadata].project``, else empty (gcp modes need it set)."""
    return os.environ.get("GCP_PROJECT") or _gcp_table().get("project") or ""


def gcp_sa_labels() -> dict:
    """SA local-parts by role, e.g. ``{"app": "app-runtime", "box": "log-reader"}`` —
    from ``[plugins.gcp-metadata].sa_labels`` ({} if absent)."""
    labels = _gcp_table().get("sa_labels")
    return labels if isinstance(labels, dict) else {}


# ── github plugin config (the App identity behind the `github=app` rung) ───────────────


def _github_table() -> dict:
    plugins = _toml().get("plugins")
    table = plugins.get("github") if isinstance(plugins, dict) else None
    return table if isinstance(table, dict) else {}


def github_declared() -> bool:
    """True when the consumer declares ``[plugins.github]`` (even empty — the opt-in for the
    ``user`` emergency too, which needs no App fields). Mirrors :func:`gcp_metadata_declared`.
    Gates the WHOLE github surface — axis, doctor rows, box plumbing — per the registry's
    inert-until-declared contract (``plugins.load_plugins``): an undeclared consumer gets no
    github mode row in the TUI and no gh/App nags in doctor."""
    plugins = _toml().get("plugins")
    return isinstance(plugins, dict) and isinstance(plugins.get("github"), dict)


def github_app_id() -> str:
    """The PR-bot GitHub App's numeric id. ``GH_APP_ID`` wins, then
    ``[plugins.github].app_id``, else empty (github=app needs it set)."""
    return os.environ.get("GH_APP_ID") or str(_github_table().get("app_id") or "")


def github_installation_id() -> str:
    """The App's installation id on the org. ``GH_INSTALLATION_ID`` wins, then
    ``[plugins.github].installation_id``, else empty."""
    return os.environ.get("GH_INSTALLATION_ID") or str(_github_table().get("installation_id") or "")


def github_repo() -> str:
    """Bare repo name (not owner/repo) the App is scoped to. ``GH_REPO`` wins, then
    ``[plugins.github].repo``, else empty."""
    return os.environ.get("GH_REPO") or str(_github_table().get("repo") or "")


def github_permissions() -> str:
    """``[plugins.github].permissions`` as the JSON the ``github-app`` minter scopes its
    installation token down to (``GH_APP_PERMISSIONS`` wins). Empty ⇒ the minter's own default
    (``pull_requests``/``issues`` write — the PR-bot shape). Declaring it here lets a consumer
    NARROW or retarget the token without touching plugin code."""
    ambient = os.environ.get("GH_APP_PERMISSIONS")
    if ambient:
        return ambient
    table = _github_table().get("permissions")
    return json.dumps(table, sort_keys=True) if isinstance(table, dict) and table else ""


def _overlay_path(raw: object) -> Path | None:
    """A configured compose-override value → an absolute Path (checkout-relative resolves
    against the repo root), or None when unset/empty."""
    if not raw:
        return None
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (repo_root() / p)


def overlays_declared() -> list[dict]:
    """The ``[[overlay]]`` array-of-tables — each entry ``{file, when, [env]}`` declaratively
    layers a compose overlay onto the ``-f`` chain when the mode matches ``when``. This is the
    config-only replacement for the per-plugin ``compose_overlays`` hooks: a consumer expresses
    WHICH overlay layers under WHICH posture entirely in ``foldyard.toml``, no plugin code. Entry
    order = ``-f`` order (later overrides earlier — the compose stacking discipline). Non-dict
    rows are ignored."""
    raw = _toml().get("overlay")
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _when_matches(when: object, mode: dict) -> bool:
    """An ``[[overlay]]`` entry's ``when`` (a dict of ``axis = value`` or ``axis = [values]``)
    matches ``mode`` iff EVERY named axis holds one of its values — AND across keys, OR within a
    list. A missing/empty ``when`` always matches (an unconditional base overlay)."""
    if not when:
        return True
    if not isinstance(when, dict):
        return False
    for axis, expected in when.items():
        allowed = expected if isinstance(expected, list) else [expected]
        if mode.get(axis) not in allowed:
            return False
    return True


def when_matches(when: object, mode: dict) -> bool:
    """Public alias of :func:`_when_matches` — the SAME ``when`` semantics for every config table
    that gates on posture (``[[overlay]]``, ``[[secret]]``), so a consumer learns one rule."""
    return _when_matches(when, mode)


def secret_specs() -> list[dict]:
    """The ``[[secret]]`` array-of-tables — each entry declares a host-side secret a posture needs
    present in host.env: ``var`` (required), ``label``, ``how`` (the "where do I get this?" hint,
    PRINTED at the capture prompt and never executed), ``pattern`` (an fnmatch GLOB the value must
    match — a glob, not a regex, because this field is repo-controlled: see ``keyless.secret_ok``),
    ``base64`` (store encoded — host.env is single-line), ``when`` (posture gate, same semantics as
    ``[[overlay]]``). Absent/malformed ⇒ ``[]``; non-dict entries are skipped, and FIELD validation
    is the registry's (loud, at merge — see ``Registry.secrets``). See
    :class:`foldyard.plugins.Secret`."""
    raw = _toml().get("secret")
    if not isinstance(raw, list):
        return []
    return [dict(entry) for entry in raw if isinstance(entry, dict)]


def matching_overlays(mode: dict) -> list[Path]:
    """The ``[[overlay]]`` files whose ``when`` matches ``mode``, resolved to absolute paths in
    declaration (= ``-f``) order. Per entry the path is ``$<env>`` (when the entry names an ``env``
    escape-hatch var and it is set) else ``file``; a checkout-relative value resolves against the
    repo root. Unset/empty paths are skipped. The registry lays these down ahead of any overlay a
    plugin still adds programmatically (see :meth:`Registry.compose_overlays`)."""
    out: list[Path] = []
    for entry in overlays_declared():
        if not _when_matches(entry.get("when"), mode):
            continue
        env_var = entry.get("env")
        raw = (os.environ.get(env_var) if isinstance(env_var, str) else None) or entry.get("file")
        path = _overlay_path(raw)
        if path:
            out.append(path)
    return out


def requires_declared() -> list[dict]:
    """The ``[[require]]`` array-of-tables — each entry declares one cross-axis coherence
    requirement as consumer config: while ``axis`` sits at a rung in ``when``, ``needs`` must
    sit at a rung in ``accepts``. This is the config tier of :class:`~foldyard.plugins.Requires`
    (same evaluator, same semantics): a WIRING-dependent coupling — e.g. llm=record/live needs
    gcp=sa only because THIS consumer's ``[[overlay]]`` routes real LLM traffic through
    Vertex/ADC — lives here next to the overlays that cause it, and can reference config-defined
    axes (an ``[[inject]]`` credential) no plugin file could name. Intrinsic couplings stay
    in-code on the plugin's own :attr:`~foldyard.plugins.Axis.requires`. Non-dict rows are
    ignored (like ``[[overlay]]``); FIELD validation is the registry's (loud, at construction —
    see ``Registry.__init__``), since only it knows the loaded axes."""
    raw = _toml().get("require")
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def overlay_when_axes() -> set[str]:
    """Every axis name any ``[[overlay]]`` entry references in its ``when`` — an axis is "wired"
    (something actually layers on it) iff it appears here. A plugin self-gates an OPTIONAL axis on
    this: e.g. gcp's ``storage`` axis appears only when an overlay layers on ``storage=staging``,
    so a rung that would change nothing is never offered."""
    axes: set[str] = set()
    for entry in overlays_declared():
        when = entry.get("when")
        if isinstance(when, dict):
            axes.update(when)
    return axes


# ── auth0-sim plugin config ([plugins.auth0-sim]) ──────────────────────────────────────


def _auth0_sim_table() -> dict:
    plugins = _toml().get("plugins")
    table = plugins.get("auth0-sim") if isinstance(plugins, dict) else None
    return table if isinstance(table, dict) else {}


def auth0_sim_declared() -> bool:
    """True when the consumer declares ``[plugins.auth0-sim]`` (even empty) — the opt-in that
    LOADS the auth0-sim plugin (registry plan Step C), so a generic repo never gets a ``dump`` axis
    it can't back. The axis additionally self-gates on the same table (Step D)."""
    plugins = _toml().get("plugins")
    return isinstance(plugins, dict) and isinstance(plugins.get("auth0-sim"), dict)


def auth0_sim_dir() -> str:
    """Repo-relative dir holding the consumer's Auth0-simulator harness — its
    ``.certs-local/`` is where the mkcert localhost cert lands (the cert doctor checks read
    it). ``FOLDYARD_AUTH0_SIM_DIR`` wins, then ``[plugins.auth0-sim].sim_dir`` ("" if absent,
    which disables the cert checks)."""
    return os.environ.get("FOLDYARD_AUTH0_SIM_DIR") or _auth0_sim_table().get("sim_dir") or ""


def auth0_sim_container() -> str:
    """The simulator's compose-service / container suffix (full name = ``<prefix>-<suffix>``) —
    used to bounce it when serving a freshly-generated cert. ``FOLDYARD_AUTH0_SIM_CONTAINER``
    wins, then ``[plugins.auth0-sim].container``, default ``auth0-sim``."""
    return (
        os.environ.get("FOLDYARD_AUTH0_SIM_CONTAINER")
        or _auth0_sim_table().get("container")
        or "auth0-sim"
    )


# ── llm plugin config ([plugins.llm]) ────────────────────────────────────────────────


def llm_declared() -> bool:
    """True when the consumer declares ``[plugins.llm]`` (even empty) — the opt-in that LOADS
    the llm plugin (registry plan Step C), so a generic repo never gets an ``llm`` axis whose
    rungs change nothing."""
    plugins = _toml().get("plugins")
    return isinstance(plugins, dict) and isinstance(plugins.get("llm"), dict)


def fakecred_declared() -> bool:
    """True when the consumer declares ``[plugins.fakecred]`` (even empty) — the opt-in that
    LOADS the fake-credential TESTING plugin (docs/testing-modes.md): a zero-secret axis pair +
    fake minter + toggleable capability for exercising the whole mode/TTL/daemon/probe machinery
    on a real host without any real credential."""
    plugins = _toml().get("plugins")
    return isinstance(plugins, dict) and isinstance(plugins.get("fakecred"), dict)


def fakecred_port() -> int:
    """The fake minter's listen port: ``FAKECRED_PORT`` env wins, else the project band's
    headroom slot (band + 190) + the worktree offset FOLDED INTO the 10-port headroom. The real
    daemons get disjoint 90-port spans (ports.py); a testing daemon doesn't warrant widening the
    band, so two worktrees whose offsets collide mod 10 can collide here — the supervisor's
    port-conflict nag surfaces it, which is itself the machinery this plugin exists to
    exercise."""
    return _daemon_port_base("FAKECRED_PORT", 8290, 190) + (worktree_offset(active_worktree()) % 10)


def state_dir() -> Path:
    """The PROJECT-shared host state root, OUTSIDE the repo mount (ADR-0006 — nothing in the
    VM/box can escalate its own posture). Shared by every
    worktree: the one supervisor lock, the identity ``host.env``, and the egress allow-store all
    live here. Per-worktree POSTURE (mode, logs, mirror) lives under :func:`posture_dir`."""
    env = os.environ.get("FOLDYARD_STATE_DIR")
    return Path(env).expanduser().resolve() if env else Path.home() / ".foldyard" / project()


def posture_dir() -> Path:
    """The PER-WORKTREE posture state dir (ADR-0016 — posture state splits from
    identity state): ``<state_dir>/main`` for the primary checkout, ``<state_dir>/worktrees/<n>``
    for a worktree. So two worktrees on different branches hold different postures under the ONE
    supervisor — as they already have their own stack/box/ports. The supervisor binds each
    worktree's config and reads/writes here; the lock + ``host.env`` stay at :func:`state_dir`
    (identity is a machine fact, not a branch fact).

    Worktrees nest under ``worktrees/`` — NOT siblings of ``main`` — so a worktree literally NAMED
    ``main`` can't collide with the primary checkout's ``main/`` (that collision made a mode change
    to one silently change the other). The primary's label is ``main`` whatever BRANCH it has out:
    a positional name (the repo-root checkout), not a branch assertion."""
    wt = active_worktree()
    return state_dir() / ("main" if not wt else f"worktrees/{wt}")


def mode_file() -> Path:
    """This worktree's authoritative posture file. ``FOLDYARD_MODE_FILE`` wins; otherwise
    ``<posture_dir>/dev-mode.json``. LEGACY fallback (one-time, main only): a pre-per-worktree
    install kept this at ``<state_dir>/dev-mode.json`` — if the new per-worktree file doesn't exist
    yet but that legacy one does, keep using it, so a ``fy host`` upgrade doesn't silently reset the
    main checkout to offline. Worktrees always use the new path."""
    env = os.environ.get("FOLDYARD_MODE_FILE")
    if env:
        return Path(env).expanduser()
    new = posture_dir() / "dev-mode.json"
    if not new.exists() and not active_worktree():
        legacy = state_dir() / "dev-mode.json"
        if legacy.exists():
            return legacy
    return new


def host_env_file() -> Path:
    """The SHARED identity env (App IDs, PEM secret names) — one per project, not per worktree
    (the minters serving every branch read it). ``FOLDYARD_HOST_ENV`` wins, else
    ``<state_dir>/host.env``."""
    env = os.environ.get("FOLDYARD_HOST_ENV")
    return Path(env).expanduser() if env else state_dir() / "host.env"


def supervisor_log_file() -> Path:
    """The ONE supervisor's combined log — its stdout/stderr plus every child daemon's, teed by
    ``supervisor.tee_stdio_to_logfile``. PROJECT-shared (under :func:`state_dir`), NOT per-worktree:
    there's a single supervisor per project reconciling every worktree's daemons, so its own process
    log is a project fact. Keeping it here means the TUI's Mode pane finds the same file whichever
    worktree is in focus, and a bounce+relaunch from any worktree keeps appending to it instead of
    the log hopping between per-worktree dirs. (The per-daemon JSONLs — ``egress.jsonl`` /
    ``gcp-minter.jsonl`` — stay per-worktree under :func:`log_dir`.) ``FOLDYARD_SUPERVISOR_LOG``
    wins, else ``<state_dir>/host-supervisor.log``."""
    env = os.environ.get("FOLDYARD_SUPERVISOR_LOG")
    return Path(env).expanduser().resolve() if env else state_dir() / "host-supervisor.log"


def heartbeat_file() -> Path:
    """The supervisor's PROJECT-shared liveness stamp: it writes the current time here once per
    reconcile tick, and the launch paths (`fy up`, `fy host`) read its age to tell a healthy holder
    from a wedged one (``supervisor._holder_stale_reason``). Project-level, deliberately NOT a
    per-worktree ``.dev-mode.json`` mirror: the one supervisor only refreshes a worktree's mirror
    while THAT worktree's box is up, so a down/just-created worktree has a legitimately stale mirror
    — reading it as the liveness signal made a fresh `fy up` bounce a perfectly healthy supervisor.
    ``FOLDYARD_HEARTBEAT_FILE`` wins, else ``<state_dir>/host-supervisor.heartbeat``."""
    env = os.environ.get("FOLDYARD_HEARTBEAT_FILE")
    return Path(env).expanduser() if env else state_dir() / "host-supervisor.heartbeat"


def capabilities_file() -> Path:
    """The supervisor's PROJECT-shared capability-probe results (plugin
    ``capability_probes`` — "does the credential chain this posture promises actually work
    right now?"). Written once per tick when a probe ran, read by ``fy mode``/``fy state`` on
    the host (box sessions read the copy stamped into their mirror). Observed state only —
    nothing grants or blocks based on it. ``FOLDYARD_CAPABILITIES_FILE`` wins, else
    ``<state_dir>/capabilities.json``."""
    env = os.environ.get("FOLDYARD_CAPABILITIES_FILE")
    return Path(env).expanduser() if env else state_dir() / "capabilities.json"


def _host_table() -> dict:
    """The optional ``[host]`` table — Mac-side supervisor behaviour knobs."""
    raw = _toml().get("host")
    return raw if isinstance(raw, dict) else {}


def host_notifications() -> bool:
    """Should the supervisor post macOS notifications on capability transitions (a credential
    chain lapsing or healing)? Default ON — the push surface exists precisely for the operator
    who is in a browser/editor when the 12h PAM grant expires, not watching ``fy host``.
    ``[host] notifications = false`` opts out."""
    raw = _host_table().get("notifications")
    return True if raw is None else bool(raw)


def resnapshot_on_capability() -> dict[str, list[str]]:
    """``[resnapshot_on_capability]``: axis → compose services the supervisor restarts when that
    axis's capability probe flips failing→ok (mode-state consolidation proposal C). For a service
    that snapshots credentials ONCE at boot (a startup secret fetch frozen into import-time
    state), a healed chain is useless until it reboots — the restart re-runs the boot fetch under
    the recovered capability. Malformed entries are dropped, never raised: this is read on the
    supervisor tick."""
    raw = _toml().get("resnapshot_on_capability")
    if not isinstance(raw, dict):
        return {}
    return {
        axis: [str(s) for s in services]
        for axis, services in raw.items()
        if isinstance(services, list) and services
    }


def clock_offset_file() -> Path:
    """TESTING aid: seconds added to :func:`foldyard.devmode.now` (``fy clock``), so TTL
    machinery — expiry, auto-revert, the settle cascade — can be exercised live without
    waiting hours or touching the system clock. Host-side state (outside the repo mount), so
    nothing in the box can skew the host's view of time. Absent = no skew (the normal state).
    ``FOLDYARD_CLOCK_OFFSET`` (env, seconds) wins over the file for one-shot test runs."""
    return state_dir() / "clock-offset"


def allow_store_file() -> Path:
    """Authoritative live egress grants (once / until-restart), in the Mac home OUTSIDE the repo
    mount — so nothing in the box can grant its own egress (mirrors :func:`mode_file`)."""
    env = os.environ.get("FOLDYARD_ALLOW_STORE")
    return Path(env).expanduser() if env else state_dir() / "allow-store.json"


def allow_effective_file() -> Path:
    """The resolved effective allowlist the egress proxy daemon re-reads per request: the
    allow-store's live grants (once / session / permanent), expired entries dropped — repo config
    contributes nothing (a ``[proxy] allow`` list is IGNORED; grants are host-owned, see
    :mod:`foldyard.allowlist`). Written by the host; read by the standalone proxy addon (which
    can't import foldyard) via ``ALLOW_FILE``."""
    env = os.environ.get("FOLDYARD_ALLOW_FILE")
    return Path(env).expanduser() if env else state_dir() / "allow-effective.json"


def log_dir() -> Path:
    """This worktree's host-daemon logs (egress proxy, gcp minter, supervisor). Per-worktree so
    each branch's proxy listener logs separately. ``FOLDYARD_LOG_DIR`` wins, else
    ``<posture_dir>/logs``."""
    env = os.environ.get("FOLDYARD_LOG_DIR")
    return Path(env).expanduser().resolve() if env else posture_dir() / "logs"


# How many bytes to read from the END of each (rotated) JSONL log when a TUI panel tails it.
# The panels show at most a few hundred recent lines; at ~150–250 B/line this tail comfortably
# covers >1000 lines, so we never re-read the whole multi-MB file (esp. under capture=on) just
# to slice the last N. Overridable for tests / very wide lines.
LOG_TAIL_BYTES = int(os.environ.get("FOLDYARD_LOG_TAIL_BYTES", str(256 * 1024)))


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    """The trailing non-blank lines of ``path``, reading at most ``max_bytes`` from its end (so a
    huge log isn't fully read). The first line of a mid-file read is likely partial, so it's
    dropped when we didn't start at byte 0."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # partial first line from seeking into the middle of a record
    return [ln for ln in lines if ln.strip()]


def rotated_logs(log: Path) -> list[Path]:
    """The dated backups of ``log`` (``<stem>.<UTC-stamp><suffix>``, e.g.
    ``egress-proxy.20260616T141530Z.jsonl``), oldest→newest. The stamp is a sortable basic-ISO time,
    so lexical sort is chronological. The live ``<stem><suffix>`` (one dot) is never matched."""
    try:
        return sorted(log.parent.glob(f"{log.stem}.*{log.suffix}"))
    except OSError:
        return []


def tail_jsonl(log: Path, limit: int) -> list[dict]:
    """The last ``limit`` parsed JSON objects across the live log + its dated backups (see
    :func:`rotated_logs`), oldest→newest. Reads only the TAIL of each file (see
    :data:`LOG_TAIL_BYTES`) and parses at most ``limit`` lines, so the per-tick cost is bounded no
    matter how big the log has grown — the fix for the TUI panels re-parsing a multi-MB capture log
    every second. Backups are only touched when the live file is short (e.g. just after a
    rotation). Malformed lines are skipped. Shared by the proxy + gcp panels so they can't drift."""
    lines = _tail_lines(log, LOG_TAIL_BYTES)
    if len(lines) < limit:  # live file short → prepend dated backups, newest first, until enough
        for backup in reversed(rotated_logs(log)):
            lines = _tail_lines(backup, LOG_TAIL_BYTES) + lines
            if len(lines) >= limit:
                break
    out: list[dict] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def mirror_file() -> Path:
    """The read-only, gitignored posture mirror the box sees (informational; the Mac
    daemons remain the enforcement point). Lives in the dev-VM dir on the shared
    mount so box sessions can read it."""
    return dev_vm_dir() / ".dev-mode.json"
