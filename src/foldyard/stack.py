"""Stack-setup env for the `just` dev-VM recipes — the former `_common.sh`, in Python.

`eval "$(foldyard shellenv)"` is a drop-in for the old `source _common.sh`: it emits the
same shell vars (matching their export-ness), the `COMPOSE` bash array, and the
`dev_vm_banner` shim — so recipe bodies are unchanged. Side effects (git core.fileMode,
the rootless-machine ensure) run here in Python; ALL human/progress output goes to STDERR
so the stdout an `eval` consumes is pure shell. On a fatal setup error it prints `exit 1`
to stdout (which the recipe's `eval` then runs) + the reason to stderr. `foldyard banner`
backs the emitted shim; the machine lifecycle lives in foldyard.machine.

Faithful to `_common.sh` incl. worktree namespacing + deterministic host-port offsets
(via the system `cksum`, for parity with the old recipes). Every project-specific value
(the container-name prefix, the app service, the dev-VM subdir, the compose files, the
port bases) is read from `foldyard.config` (i.e. `foldyard.toml`), not hardcoded here —
so this module is project-agnostic (docs/configuration.md).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from shutil import which

from . import config, devmode, machine


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def compose_provider_path() -> str:
    """Resolve foldyard's bundled ``podman-compose`` console script.

    ``uv tool install foldyard`` installs dependency scripts inside foldyard's private venv but
    does not expose them on the user's PATH. Prefer that colocated script, then PATH for source
    checkouts/packagers. The declared runtime dependency means a normal install always hits the
    first branch.
    """
    local = Path(sys.executable).parent / "podman-compose"
    if local.exists():
        return str(local)
    return which("podman-compose") or "podman-compose"


@lru_cache(maxsize=1)
def main_repo() -> Path:
    """The MAIN checkout — git-common-dir's parent resolves to it even from inside a
    worktree (whose --show-toplevel would be the worktree itself).

    MEMOIZED, because this shells out to git and callers are on hot paths: the TUI's 1s
    ``refresh_panels`` timer resolves a worktree config (→ here) on the EVENT LOOP, so any latency
    here stalls the whole UI. In the dev box that latency is not hypothetical — box-side git runs
    through the index-split shim (ADR-0021), and while the index is stale the shim's ancestor probe
    costs ~3s per invocation, i.e. a 1s timer that can never finish a tick. The answer is a
    function of CWD, which no foldyard process ever changes (no ``os.chdir`` in the package), so
    one resolution per process is correct. Tests that relocate the repo clear it via
    ``config.clear_caches()``."""
    out = _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if out.returncode == 0 and out.stdout.strip():
        return Path(out.stdout.strip()).parent
    out = _run(["git", "rev-parse", "--show-toplevel"])
    if out.returncode != 0:
        _err("✗ not a git repository")
        raise SystemExit(1)
    return Path(out.stdout.strip())


def worktrees_root(main: Path) -> Path:
    return config.worktrees_root(main)


def _active_worktree(wt_root: Path) -> str:
    """The active worktree name. An explicit ``WORKTREE`` env var wins; otherwise infer it from
    the current directory when we're standing inside a sibling worktree — so `cd`-ing into a
    worktree makes its stack/box verbs Just Work without the ``WORKTREE=<name>`` prefix. Empty ⇒
    the main checkout (cwd is the main repo, or anywhere outside the worktrees root). The box
    always exports WORKTREE, so this only ever infers on the host."""
    env = os.environ.get("WORKTREE", "")
    if env:
        return env
    try:
        rel = Path.cwd().resolve().relative_to(wt_root.resolve())
    except (ValueError, OSError):
        return ""
    return rel.parts[0] if rel.parts else ""


def _pinned_offset(name: str) -> int | None:
    """A per-worktree port offset pinned in the MAIN checkout's gitignored
    ``foldyard.local.toml`` (``[worktree-offsets]``). Read from ``main_repo()`` (not cwd) so
    it's identical no matter where ``fy`` runs — the same robustness the cksum default has.
    Lets a worktree sit on a fixed SMALL offset so its app lands on a known host port (e.g. one a
    third-party service's redirect/callback-URL allowlist already accepts), not the hashed one."""
    try:
        import tomllib

        data = tomllib.loads((main_repo() / "foldyard.local.toml").read_text())
    except (OSError, ValueError, ModuleNotFoundError):
        return None
    table = data.get("worktree-offsets")
    val = table.get(name) if isinstance(table, dict) else None
    return val if isinstance(val, int) else None


def _offset(name: str) -> int:
    """Host-port offset for a worktree. Precedence (explicit wins, then stable-per-worktree):
    ``WT_OFFSET`` env → a pin in ``foldyard.local.toml`` ``[worktree-offsets]`` → deterministic
    1..89 from the name (cksum; here-string adds a trailing newline for parity with _common.sh)."""
    if os.environ.get("WT_OFFSET"):
        return int(os.environ["WT_OFFSET"])
    pinned = _pinned_offset(name)
    if pinned is not None:
        return pinned
    out = _run(["cksum"], input=f"{name}\n")
    return int(out.stdout.split()[0]) % 89 + 1


def _docker_host(main: Path, wt_root: Path, no_machine: bool) -> str | None:
    """Existing DOCKER_HOST wins (dev box / pre-exported). Else, on a host with podman,
    ensure the machine + derive the socket. --no-machine skips that (recipes that only
    need the repo/path vars, e.g. worktree-add / machine-recreate)."""
    existing = os.environ.get("DOCKER_HOST")
    if existing:
        return existing
    if no_machine:
        return None
    if which("podman"):
        machine.ensure(main, wt_root)
        return machine.socket()
    _err("✗ DOCKER_HOST unset and no 'podman' CLI — set DOCKER_HOST or install podman.")
    raise SystemExit(1)


def pin_filemode(main: Path) -> None:
    """``core.fileMode=false`` in the MAIN checkout's ``.git/config`` — exec-bit churn on the
    shared mount must not read as a modification — written READ-FIRST, so the file is touched
    once per checkout rather than on every ``shellenv``/``resolve`` from both kernels.

    The pin has to live in the file: a host GUI's bundled git (Fork ships its own) reads neither
    shellenv's env nor the box's git shim, and it is the writer that would otherwise see phantom
    mode changes. But ``.git/config`` sits on the virtiofs mount under the same non-atomic
    lock→rename protocol as the index (ADR-0021), and ``git config`` rewrites the file even for an
    unchanged value — so an unconditional write made foldyard the file's most frequent writer and
    a lost update once left a consumer's config as the 25-byte ``[core] fileMode = false``
    (issue #6). ``--local`` on the read: an operator's global ``fileMode=false`` must not skip
    the write the box side (no such global) still needs."""
    got = subprocess.run(
        ["git", "-C", str(main), "config", "--local", "--get", "core.fileMode"],
        capture_output=True,
        text=True,
    )
    if got.returncode == 0 and got.stdout.strip().lower() == "false":
        return
    subprocess.run(
        ["git", "-C", str(main), "config", "core.fileMode", "false"], capture_output=True
    )


def _context(
    no_machine: bool, worktree: str | None = None
) -> tuple[Path, dict[str, str], dict[str, str], dict[str, str]]:
    """Returns (main_repo, plain_vars, exported_vars, ports). Plain vs exported mirrors
    _common.sh exactly so the env it produces is identical."""
    main = main_repo()
    wt_root = worktrees_root(main)
    dev_vm_rel = config.dev_vm_rel()
    prefix = config.project_prefix()
    pin_filemode(main)

    plain = {
        "WORKTREES_ROOT": str(wt_root),
        "HERE": dev_vm_rel,
        "MACHINE": machine.MACHINE,
        "APP": config.app_service(),
        "ENGINE": config.engine(),  # consumer recipes echo `"$ENGINE" …` (podman by default)
    }
    # MAIN_REPO is EXPORTED (not plain): it's the same primary checkout for every worktree, so a
    # consumer compose file / recipe can reference it for resources shared across worktrees (e.g. a
    # dump/cache dir). Exported ⇒ a shell-driven `compose up` subprocess sees it too, not just the
    # Python `resolve()` env.
    exported: dict[str, str] = {"MAIN_REPO": str(main)}
    if config.engine() == "podman":
        exported["PODMAN_COMPOSE_PROVIDER"] = os.environ.get(
            "PODMAN_COMPOSE_PROVIDER", compose_provider_path()
        )

    worktree = _active_worktree(wt_root) if worktree is None else worktree
    # Export the resolved worktree so a shell that `eval`s shellenv — and every `foldyard` it then
    # runs — keys posture on the SAME checkout the stack does (config.active_worktree reads this).
    exported["WORKTREE"] = worktree
    if worktree:
        checkout = wt_root / worktree
        if not checkout.is_dir():
            _err(f"✗ no worktree at {checkout}. Create it first:")
            _err(f"    fy worktree add {worktree} [branch]")
            raise SystemExit(1)
        exported["FOLDYARD_CHECKOUT"] = str(checkout)
        exported["PODMAN_PROJECT"] = f"{prefix}-{worktree.replace('/', '-')}"
        off = _offset(worktree)
    else:
        exported["FOLDYARD_CHECKOUT"] = str(main)
        exported["PODMAN_PROJECT"] = prefix
        off = 0
    # Export the [ports] bases for BOTH the main checkout (offset 0) and worktrees (per-worktree
    # offset). On main these equal the configured bases — the same values the compose files' own
    # `${SIM_PORT:-4400}` fallbacks assume — so nothing changes behaviourally, but SIM_PORT/APP_PORT
    # are now actually SET in the compose env. That silences compose-go's "variable is not set,
    # defaulting to a blank string" warning, which it emits for a referenced-but-unset var even when
    # the reference carries a `:-default`. No [ports] table means an empty dict, i.e. no exports.
    ports = {k: str(base + off) for k, base in config.port_bases().items()}
    exported["COMPOSE_PROJECT_NAME"] = exported["PODMAN_PROJECT"]

    dh = _docker_host(main, wt_root, no_machine)
    if dh:
        # The same docker-compat socket of the rootless machine, under both names:
        # podman reads CONTAINER_HOST, docker reads DOCKER_HOST. Setting both lets podman
        # drive it everywhere (incl. the box, where plain/local podman would be broken)
        # while any remaining docker caller still works.
        exported["DOCKER_HOST"] = dh
        exported["CONTAINER_HOST"] = dh
    exported["FOLDYARD_ENV_OVERRIDE"] = str(
        Path(exported["FOLDYARD_CHECKOUT"]) / dev_vm_rel / "sim.env"
    )
    return main, plain, exported, ports


def network_exists(engine: str, net: str, env: dict) -> bool:
    """True when ``net`` already exists for ``engine`` (its ``network inspect`` succeeds). The
    single source of the "is the network present?" probe, shared by :func:`ensure_network` and
    ``box.up`` so both stay consistent."""
    return (
        subprocess.run(
            [engine, "network", "inspect", net],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def ensure_network(engine: str, net: str, env: dict) -> None:
    """Create the project's podman network when nothing else will. Two callers own this:
    a stack-less project (no ``compose up`` ever creates ``{project}_default`` — the box
    must, see ``box.up``), and an ``[project].external_network`` consumer (compose declares
    the network ``external``, so foldyard creates it before anything attaches — ``up``,
    ``shellenv``-driven raw-compose recipes, ``box up``). Idempotent: ``network inspect``
    short-circuits when the stack (or a prior box-up) already made it. Raises
    ``CalledProcessError`` if creation genuinely fails — a swallowed failure would leave a later
    attach dying on podman's cryptic "network not found"."""
    if network_exists(engine, net, env):
        return
    create = [engine, "network", "create", net]
    _err("+ " + " ".join(create))
    subprocess.run(create, env=env, stdout=subprocess.DEVNULL, check=True)


def _compose_overlays(mode: dict, base: Path) -> list[str]:
    """The posture overlay `-f` files to append, resolved + existence-filtered, in order:
    the plugins' overlays (they STACK — dump + metadata + a data-callback overlay can all be
    present) followed by manual `FOLDYARD_COMPOSE_EXTRA` entries (os.pathsep-joined) LAST so an
    explicit override wins on conflicting keys. Relative paths resolve against `base` (MAIN_REPO
    for shellenv; the active checkout for engine verbs); absolutes pass through. Missing files are
    skipped — a plugin may name an overlay a given branch doesn't ship."""
    from .plugins import registry

    raw = list(registry().compose_overlays(mode))
    env_extra = os.environ.get("FOLDYARD_COMPOSE_EXTRA")
    if env_extra:
        raw += [p for p in env_extra.split(os.pathsep) if p]
    out: list[str] = []
    for entry in raw:
        p = Path(entry)
        p = p if p.is_absolute() else base / p
        if p.is_file():
            out.append(str(p))
    return out


def shellenv(no_machine: bool = False) -> int:
    # The declared compose files must exist in the checkout the stack is bound to — checked
    # FIRST, before _context() can ensure (provision!) the machine for a stack that can't run,
    # and before COMPOSE is emitted: a raw recipe would otherwise hand the provider a file
    # foldyard already knows isn't there. Same gate, same message as the stack verbs.
    if _missing_declared_compose():
        print("exit 1")  # the recipe's `eval` runs this → it aborts (reason already on stderr)
        return 1
    try:
        main, plain, exported, ports = _context(no_machine)
    except SystemExit as e:
        print("exit 1")
        return e.code if isinstance(e.code, int) else 1
    # The checkout _context() bound (a worktree's tree, or main) — the one resolve() joins the
    # `-f` paths onto, and the one the gate above validated against.
    checkout = Path(exported["FOLDYARD_CHECKOUT"])

    # An external_network consumer's raw-compose recipes (`"${COMPOSE[@]}" up …`, e.g. a cold
    # `just e2e`) can't rely on a prior `up` having created the network compose now declares
    # external — so the shared-setup shim ensures it, exactly like it ensures the machine.
    # Skipped with --no-machine (no engine socket resolved to talk to).
    if config.external_network() and not no_machine:
        ensure_network(
            config.engine(),
            f"{exported['PODMAN_PROJECT']}_default",
            {**os.environ, **exported},
        )

    # Mode-derived recipe defaults, emitted as `${K:-v}` so an explicit env var still wins.
    mode = devmode.read()["mode"]
    derived = devmode.derive_env(mode)

    # COMPOSE: configured files + posture overlays, resolved against the CHECKOUT as absolute
    # paths (the recipe `cd`s to MAIN_REPO, which is not a worktree's tree — a relative `-f` there
    # pointed a worktree's stack at main's compose file; resolve() joins the same way). Overlays
    # STACK (a plugin can contribute several; several plugins can each contribute) — e.g. dump +
    # a data-callback overlay — instead of one clobbering the rest. Manual FOLDYARD_COMPOSE_EXTRA
    # entries (os.pathsep-joined) append LAST, so an explicit override wins on conflicting keys.
    # Engine: docker when DOCKER_HOST is preset (the box), else podman.
    compose = config.engine_compose()
    for f in config.compose_files():
        p = Path(f)
        compose += ["-f", str(p if p.is_absolute() else checkout / p)]
    for overlay in _compose_overlays(mode, base=checkout):
        compose += ["-f", overlay]

    # Recipe bodies relied on _common.sh's `set -euo pipefail` (they don't set it
    # themselves), so re-establish it here — the eval runs in the recipe's own shell.
    lines = ["set -euo pipefail", f"cd {shlex.quote(str(main))}"]
    lines += [f"{k}={shlex.quote(v)}" for k, v in plain.items()]
    lines += [f"export {k}={shlex.quote(v)}" for k, v in exported.items()]
    lines += [f"export {k}={shlex.quote(v)}" for k, v in ports.items()]
    lines += [f'export {k}="${{{k}:-{v}}}"' for k, v in derived.items()]
    lines.append("COMPOSE=(" + " ".join(shlex.quote(x) for x in compose) + ")")
    lines.append("dev_vm_banner() { command foldyard banner; }")
    print("\n".join(lines))
    return 0


def _ensure_dirs(checkout: Path) -> None:
    """The bind-mount source dirs the compose file expects to pre-exist, inside the
    (worktree's) checkout — they must live in the SAME tree the stack runs from. All of it is
    project config (`[project].ensure_dirs`), nothing is hardcoded here: a consumer that
    declares nothing gets nothing written."""
    for rel in config.ensure_dirs():
        (checkout / rel).mkdir(parents=True, exist_ok=True)


def _print_banner(env: dict) -> None:
    print(
        f"PODMAN_PROJECT={env.get('PODMAN_PROJECT', '')}  "
        f"(checkout: {env.get('FOLDYARD_CHECKOUT', '')})"
    )
    print(f"DOCKER_HOST={env.get('DOCKER_HOST', '')}")
    print(f"FOLDYARD_ENV_OVERRIDE={env.get('FOLDYARD_ENV_OVERRIDE', '')}")


def banner() -> int:
    """`foldyard banner` — backs the dev_vm_banner shim (reads the eval'd recipe env)."""
    _print_banner(dict(os.environ))
    return 0


# ── engine verbs (compose passthroughs) ───────────────────────────────────────────────


@dataclass
class Context:
    main: Path
    env: dict
    compose: list[str]
    app: str
    project: str
    worktree: str


def resolve(no_machine: bool = False, worktree: str | None = None) -> Context:
    """The fully-resolved stack context for a Python engine verb: the subprocess env (incl.
    mode-derived defaults, with an explicit env var still winning) + the `<engine> compose
    -f …` base command (absolute paths; verbs run with cwd=MAIN_REPO)."""
    main, plain, exported, ports = _context(no_machine, worktree=worktree)
    mode = devmode.read()["mode"]
    derived = devmode.derive_env(mode)
    mode_env = {k: os.environ.get(k, v) for k, v in derived.items()}  # explicit env wins
    # Resolve checkout-relative compose files from the SAME checkout `config.compose_files()`
    # validates against — the worktree checkout when one's bound, else main. Joining relative paths
    # onto `main` instead pointed a worktree's stack at MAIN's compose file (wrong branch, or a file
    # that may not exist there). FOLDYARD_CHECKOUT is main for the primary checkout, so unchanged.
    checkout = Path(exported["FOLDYARD_CHECKOUT"])
    compose = config.engine_compose()
    for f in config.compose_files():
        p = Path(f)
        compose += ["-f", str(p if p.is_absolute() else checkout / p)]
    for overlay in _compose_overlays(mode, base=checkout):
        compose += ["-f", overlay]
    env = {**os.environ, **plain, **exported, **ports, **mode_env}
    return Context(
        main=main,
        env=env,
        compose=compose,
        app=config.app_service(),
        project=exported["PODMAN_PROJECT"],
        worktree=exported["WORKTREE"],
    )


def _compose(ctx: Context, args: list[str], *, extra_profiles: list[str] | None = None) -> int:
    """Run `<engine> compose -f … [args]` with the resolved env, echoing the command (the
    SPIKE 'thin and echoing' rule). Inherits stdio so exec/logs stay interactive.
    ``extra_profiles`` unions extra profiles into the run (see ``_profile_flags``)."""
    cmd = list(ctx.compose) + _profile_flags(ctx, extra_profiles) + args
    _err("+ " + " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, env=ctx.env, cwd=str(ctx.main)).returncode


def _redact_build_args(cmd: list[str]) -> list[str]:
    """The display form of a build command: `--build-arg K=V` values masked as `K=<redacted>`.
    Resolved build args routinely carry secrets (registry tokens, API keys), and both the echoed
    `+ …` line and the build log outlive the build — only the argv handed to the engine keeps
    the real values."""
    shown = list(cmd)
    for i, tok in enumerate(cmd[:-1]):
        if tok == "--build-arg" and "=" in cmd[i + 1]:
            shown[i + 1] = cmd[i + 1].split("=", 1)[0] + "=<redacted>"
    return shown


def _podman_build(
    ctx: Context,
    build_proxy: str | None,
    *,
    extra_profiles: list[str] | None = None,
    services: list[str] | None = None,
    superseded: list[str] | None = None,
) -> int:
    """Build every service with a `build:` section using NATIVE `podman build` (buildah).

    On the podman engine this is the ONLY builder that handles compose `additional_contexts`:
    `<engine> compose --build` shells out to the docker-compose provider, and podman hard-sets
    DOCKER_BUILDKIT=0 on that provider's env, so the build falls back to the classic builder —
    which rejects additional contexts ("the classic builder doesn't support additional
    contexts"). buildah supports them natively (plus `RUN --mount=type=cache`, multi-stage
    `--target`, `COPY --from=<image>`), so there's no BuildKit/buildx, no `moby/buildkit`
    sidecar, and no dependency on Docker/Docker-Desktop being installed at all.

    We resolve the build graph from normalized `compose config` output (context, dockerfile,
    target, args, additional_contexts) and tag each image `<project>_<service>` — the default name
    podman-compose computes at `up`. `up` then runs with `--no-build`, so it REUSES these images
    (podman
    resolves the bare name to its `localhost/…` store entry) and never re-enters the broken
    provider build. The docker engine (CI) never reaches here — compose's BuildKit default
    handles additional contexts there; see `up`.

    ``superseded``, when given, collects the ids of images a successful build untagged — the
    previous `<project>_<service>` images — for `up` to remove once containers have moved off
    them (see ``_remove_superseded``).
    """
    cfg = _run(
        list(ctx.compose) + _profile_flags(ctx, extra_profiles) + ["config"],
        env=ctx.env,
        cwd=str(ctx.main),
    )
    if cfg.returncode != 0:
        _err(cfg.stderr.strip())
        return cfg.returncode
    # podman-compose emits normalized YAML (unlike docker-compose's `--format json`). PyYAML is a
    # podman-compose dependency, imported only on the build path so the regular CLI stays light.
    import yaml

    configured_services = (yaml.safe_load(cfg.stdout) or {}).get("services", {})
    requested = set(services or [])
    missing = requested - configured_services.keys()
    if missing:
        _err("✗ unknown compose service(s): " + ", ".join(sorted(missing)))
        return 2
    # podman-compose's default image name is `<lower(project)>_<service>`; match it exactly so
    # `up --no-build` finds the native Buildah image instead of trying to pull that name.
    project = ctx.env.get("COMPOSE_PROJECT_NAME", ctx.project).lower()
    engine = config.engine()
    to_build = [
        (name, svc)
        for name, svc in configured_services.items()
        if svc.get("build")  # image-only services are pulled at `up` — nothing to build
        and (not requested or name in requested)
    ]
    if not to_build:
        return 0
    # Build output goes to a per-stack log, not the terminal — a warm `fy up` used to scroll
    # hundreds of layer/apt lines past the summaries that matter. The echoed `+ <engine> build …`
    # lines stay (the "thin and echoing" rule), and a failure prints the log tail + path.
    log_path = config.state_dir() / f"build-{project}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _err(f"▶ build output → {log_path} (tail -f to watch; shown here only on failure)")

    def image_for(name: str, svc: dict) -> str:
        return svc.get("image") or f"{project}_{name}"

    def platform_for(svc: dict) -> str:
        # The service's effective build platform: `build.platforms` wins, else the service-level
        # `platform` (compose's own precedence). Part of the group key below so services with
        # identical build mappings but different targets never share one image.
        return ",".join(svc["build"].get("platforms") or []) or svc.get("platform") or ""

    def build_one(group: list[tuple[str, dict]], log) -> tuple[str, int]:
        name, svc = group[0]
        build = svc["build"]
        context = build["context"]
        image = image_for(name, svc)
        cmd = [engine, "build", "-t", image]
        dockerfile = build.get("dockerfile")
        if dockerfile:
            df = Path(dockerfile)
            cmd += ["-f", str(df if df.is_absolute() else Path(context) / df)]
        if build.get("target"):
            cmd += ["--target", build["target"]]
        if platform_for(svc):
            cmd += ["--platform", platform_for(svc)]
        # A walled build reaches the proxy as a TRUSTED BUILD (tunnelled, not decrypted — it has
        # no proxy CA), before the service's own args so a service's proxy arg wins.
        if build_proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                cmd += ["--build-arg", f"{key}={build_proxy}"]
        for k, v in (build.get("args") or {}).items():
            cmd += ["--build-arg", k if v is None else f"{k}={v}"]
        # podman-compose normalizes additional_contexts to either a {name: value} map or a list
        # of `name=value` entries; podman's --build-context accepts both underlying value forms.
        extra_ctx = build.get("additional_contexts") or {}
        if not isinstance(extra_ctx, dict):
            extra_ctx = dict(c.split("=", 1) for c in extra_ctx)
        for cname, cpath in extra_ctx.items():
            cmd += ["--build-context", f"{cname}={cpath}"]
        cmd.append(context)
        echoed = "+ " + " ".join(shlex.quote(c) for c in _redact_build_args(cmd))
        _err(echoed)
        log.write(echoed + "\n")
        log.flush()  # the subprocess writes to the fd directly — keep ordering
        rc = subprocess.run(
            cmd, env=ctx.env, cwd=str(ctx.main), stdout=log, stderr=subprocess.STDOUT
        ).returncode
        if rc == 0:
            # Several services commonly share one base/dev image definition. Build the identical
            # spec once, then give its result every provider-expected service tag instead of making
            # Buildah repeat the same cached traversal and contend on storage locks N times.
            for alias_name, alias_svc in group[1:]:
                alias = image_for(alias_name, alias_svc)
                tag_cmd = [engine, "tag", image, alias]
                tag_echoed = "+ " + " ".join(shlex.quote(c) for c in tag_cmd)
                _err(tag_echoed)
                log.write(tag_echoed + "\n")
                log.flush()
                rc = subprocess.run(
                    tag_cmd,
                    env=ctx.env,
                    cwd=str(ctx.main),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                ).returncode
                if rc != 0:
                    return alias_name, rc
        return name, rc

    # The ids behind every tag about to be (re)written: whatever a successful build replaces
    # is ours to remove, once `up` has recreated the containers onto the new image.
    images = {image_for(name, svc) for name, svc in to_build}
    before = {image: _image_id(engine, image, ctx.env) for image in images}

    with log_path.open("w") as log:
        # Match podman-compose 1.6's build semantics: independent specs build concurrently. Services
        # with an identical normalized build mapping share one build and receive alias tags above.
        groups: dict[str, list[tuple[str, dict]]] = {}
        for name, svc in to_build:
            key = json.dumps([svc["build"], platform_for(svc)], sort_keys=True)
            groups.setdefault(key, []).append((name, svc))
        with ThreadPoolExecutor(max_workers=len(groups)) as pool:
            futures = [pool.submit(build_one, group, log) for group in groups.values()]
            failures = [
                result for future in as_completed(futures) if (result := future.result())[1]
            ]
        if failures:
            log.flush()
            failed = ", ".join(f"{name} (exit {rc})" for name, rc in failures)
            _err(f"✗ build failed for {failed} — last 25 log lines:")
            for line in log_path.read_text().splitlines()[-25:]:
                _err("  " + line)
            _err(f"  full log: cat {log_path}")
            return failures[0][1]
    if superseded is not None:
        after = {image: _image_id(engine, image, ctx.env) for image in images}
        superseded += sorted(
            {old for image, old in before.items() if old and old != after.get(image)}
        )
    return 0


def _image_id(engine: str, image: str, env: dict[str, str]) -> str | None:
    proc = _run([engine, "image", "inspect", "--format", "{{.Id}}", image], env=env)
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _repo_tags(engine: str, image: str, env: dict[str, str]) -> list[str] | None:
    """The tags still naming ``image`` — ``[]`` when untagged, None when unreadable (fail
    closed: an id whose tags can't be read is treated as still referenced)."""
    proc = _run([engine, "image", "inspect", "--format", "{{json .RepoTags}}", image], env=env)
    if proc.returncode != 0:
        return None
    try:
        tags = json.loads(proc.stdout)
    except ValueError:
        return None
    return tags if isinstance(tags, list) else None


def _remove_superseded(ctx: Context, ids: list[str]) -> None:
    """Remove the images `up`'s build untagged. Provably ours and not in flight, so no age
    guard — the dangling sweep's guard exists for OTHER sessions' builds. Never `--force`: an
    id a container still holds (a stopped one) is refused by the engine and left for the
    dangling sweep. An id another tag still names is skipped BEFORE the rmi: `rmi <id>` on an
    image with exactly one remaining tag removes it, and that tag is a service this build
    didn't touch (a profile-gated alias of the same build spec, say) — its image, not a
    leftover. Why it matters: a rebuilt 6 GB image otherwise sits there younger than any
    guard, exactly when the next build needs the room."""
    if not ids:
        return
    eng = config.engine()
    removed = [
        i
        for i in ids
        if _repo_tags(eng, i, ctx.env) == [] and _run([eng, "rmi", i], env=ctx.env).returncode == 0
    ]
    if removed:
        print(f"✓ removed {len(removed)} image(s) this build superseded")


def _build(
    ctx: Context,
    *,
    services: list[str] | None = None,
    extra_profiles: list[str] | None = None,
    superseded: list[str] | None = None,
) -> int:
    """Build compose services with the configured engine.

    Podman uses buildah directly so local development never enters the Docker
    Compose provider's BuildKit path. Docker (including CI) retains Compose's
    regular BuildKit build.
    """
    from . import buildgate

    if config.engine() == "podman":
        return buildgate.run(
            lambda proxy_url: _podman_build(
                ctx,
                proxy_url,
                extra_profiles=extra_profiles,
                services=services,
                superseded=superseded,
            ),
            what="stack build",
        )
    return _compose(ctx, ["build", *(services or [])], extra_profiles=extra_profiles)


def build(services: list[str], *, extra_profiles: list[str] | None = None) -> int:
    """Build selected compose services; an empty list builds every configured service."""
    if (rc := stack_declared("build", "fy box build")) is not None:
        return rc
    return _build(resolve(), services=services or None, extra_profiles=extra_profiles)


def up() -> int:
    # Fail fast on unmet host prerequisites (backend CLI, proxy/keyless posture) BEFORE resolve()
    # creates the machine — turns a cryptic mid-bootstrap egress failure into an early, fixable
    # abort. Host-only; a no-op in the box. See preflight.py.
    from . import preflight

    preflight.check_or_abort("fy up")
    ctx = resolve()  # ensures the machine (so it's up even when there's no stack to start)
    _print_banner(ctx.env)
    # A successful `fy up` always restores the host-side half of the machine lifecycle, including
    # a box-only consumer (which returns below before any compose work). The call is idempotent and
    # `fy box up` repeats it immediately before creating its always-routed box.
    from . import supervisor

    supervisor.ensure_background()
    # Gated AFTER the machine + supervisor, unlike the other stack verbs: a box-only consumer's
    # `fy up` is how both come up (the note below then points at `fy box up`).
    if (rc := stack_declared("up", "fy box up")) is not None:
        return rc
    _ensure_dirs(Path(ctx.env["FOLDYARD_CHECKOUT"]))
    # Stage any VM-visible stack assets a plugin's posture needs (e.g. the gcp metadata emulator's
    # server.py — shipped in the package, off the repo mount) into the checkout BEFORE compose up.
    from .plugins import registry

    registry().stage_assets(
        devmode.read()["mode"],
        ctx.env["FOLDYARD_CHECKOUT"],
        ctx.env.get("HERE") or config.dev_vm_rel(),
    )
    # The supervisor is already coming up in parallel with the slow build, so the proxy + its CA
    # exist by the time a box is created.
    # Span every profile that already has RUNNING services (e.g. the data workers from
    # `just data-up`), so an `up` after a posture change re-renders THEIR config too instead
    # of leaving them on the old identity/env. No extras → plain COMPOSE_PROFILES behaviour.
    extra = _running_extra_profiles(ctx)
    if extra:
        print(f"▶ including running profile(s): {','.join(extra)}")
    # external_network consumer: compose declares `{project}_default` external, so it never
    # creates (or removes) it — foldyard does, before the first thing tries to attach.
    if config.external_network():
        ensure_network(config.engine(), f"{ctx.project}_default", ctx.env)
    # BEFORE the build, not after: the failure this prevents is the build dying mid-layer on
    # "no space left on device" (how a full store first showed up here — a `pnpm install`
    # layer that had already run). A no-op unless the store is genuinely low; see reclaim().
    reclaim(ctx, extra_profiles=extra)
    print(f"▶ building + starting the stack ({ctx.project})…")
    superseded: list[str] = []
    rc = _build(ctx, extra_profiles=extra, superseded=superseded)
    if rc == 0:
        # AFTER the build (a failed build must leave the running stack untouched), BEFORE the
        # provider acts: sweep containers a different compose provider created, which this one
        # can't see — it would die on name conflicts instead of adopting them.
        _reconcile_foreign_containers(ctx)
        # Build has already selected the engine-appropriate implementation; compose only starts
        # the resulting images, never re-enters a provider build path.
        rc = _compose(ctx, ["up", "-d", "--no-build"], extra_profiles=extra)
    if rc != 0:
        return rc
    # Containers are on the new images now; the ones the build replaced can go.
    _remove_superseded(ctx, superseded)
    # Dump-browse only: the Auth0 sim container rsyncs the read-only-mounted harness
    # (tests/auth0-simulator) into /opt/sim and re-seeds the login allow-list ONLY at startup,
    # and `compose up` leaves an already-healthy sim running — so a plain `fy up` after editing
    # the harness (launcher, org chooser, logo) keeps serving the stale copy. Bounce it so every
    # `up` picks up harness edits + a fresh dump seed. A restart (not recreate) is enough: it
    # re-execs the container's startup command, which re-runs the rsync + seed (same idiom the
    # sim plugin's cert doctor-fix uses). Gated on auth0=sim — the sim isn't running otherwise —
    # and best-effort, so a bounce hiccup never fails an otherwise-good `up`.
    if devmode.read()["mode"].get("auth0") == "sim":
        sim = f"{ctx.project}-{config.auth0_sim_container()}"
        print(f"▶ bouncing the Auth0 sim ({sim}) so it re-syncs the harness + re-seeds the dump…")
        subprocess.run([config.engine(), "restart", sim], env=ctx.env, cwd=str(ctx.main))
    _compose(ctx, ["ps"])  # published host ports are shown here (project/consumer-specific)
    print(f"✓ stack '{ctx.project}' is up (app service: {ctx.app}; published ports above).")
    print("  follow logs: foldyard logs    stop: foldyard down")
    # An `up` under a lapsed credential chain must not finish looking healthy: surface the
    # supervisor's probed-and-failing capabilities (the claim `fy mode` renders as DEGRADED)
    # as the LAST thing printed. Best-effort — no published probe results is no claim.
    for axis, detail in devmode.degraded_capabilities():
        print(f"⚠ {axis} capability DEGRADED — {detail}   (recheck: fy mode)")
    return 0


def _provider_signature_filter(ctx: Context) -> str:
    """The ``--filter label=…`` that selects the containers the ACTIVE compose provider can SEE.

    podman-compose finds a project's containers ONLY via ``io.podman.compose.project`` (it also
    writes the ``com.docker.compose.*`` compat labels on everything it creates, but never reads
    them back); docker-compose finds them via ``com.docker.compose.project`` and is the only
    provider that writes ``com.docker.compose.config-hash``. So: podman-compose active → its
    valued project label; docker-compose active (docker engine, or an explicit
    ``PODMAN_COMPOSE_PROVIDER`` override on podman) → the config-hash label's existence."""
    if _podman_compose_active(ctx):
        return f"label=io.podman.compose.project={ctx.project}"
    return "label=com.docker.compose.config-hash"


def _podman_compose_active(ctx: Context) -> bool:
    """Whether podman-compose is the ACTIVE compose provider — the podman engine with its
    provider pinned to podman-compose (foldyard's bundled one, unless an explicit
    ``PODMAN_COMPOSE_PROVIDER`` overrides it). Else docker-compose runs: the docker engine, or
    that override naming docker-compose on podman. The providers label (and hash) differently."""
    provider = Path(ctx.env.get("PODMAN_COMPOSE_PROVIDER", "")).name
    return config.engine() == "podman" and "podman-compose" in provider


def _reconcile_foreign_containers(ctx: Context, emit: Callable[[str], None] | None = None) -> None:
    """Remove this project's containers that the ACTIVE compose provider cannot SEE — the
    migration path across a provider switch (docker-compose ⇄ the bundled podman-compose).

    A provider only recognises containers carrying its own signature label (see
    ``_provider_signature_filter``), so a stack the OTHER provider created is invisible to it:
    ``up`` dies on "container name already in use" + pod-membership errors instead of adopting,
    ``down`` leaves the whole stack running, and a worktree nuke can't free its volumes. Sweep =
    list the project's containers (every provider writes ``com.docker.compose.project``), keep
    the ones the active provider can see, ``rm -f`` the rest; the caller's compose verb then
    proceeds against a stack that is entirely its own. Data survives — named volumes are never
    touched, and the following ``up`` recreates the swept containers from the same images.
    Non-compose neighbours (the dev box) carry no project label and can never match. Best-effort:
    a failed probe skips the sweep (fails safe toward NOT removing; a genuine conflict then
    surfaces loudly in the compose run that follows)."""
    emit = emit or _err
    base = [
        config.engine(),
        "ps",
        "-a",
        "--filter",
        f"label=com.docker.compose.project={ctx.project}",
    ]
    fmt = ["--format", "{{.ID}} {{.Names}}"]
    every = _run(base + fmt, env=ctx.env)
    native = _run([*base, "--filter", _provider_signature_filter(ctx), *fmt], env=ctx.env)
    if every.returncode != 0 or native.returncode != 0:
        return
    seen = {line.split()[0] for line in native.stdout.splitlines() if line.strip()}
    foreign = [
        line.split(None, 1)
        for line in every.stdout.splitlines()
        if line.strip() and line.split()[0] not in seen
    ]
    if not foreign:
        return
    names = ", ".join(name for _, name in foreign)
    emit(
        f"▶ removing {len(foreign)} container(s) created by a different compose provider "
        f"({names}) — recreated on `up`; named volumes (DB data, caches) are kept…"
    )
    cmd = [config.engine(), "rm", "-f"] + [cid for cid, _ in foreign]
    emit("+ " + " ".join(cmd))
    _run(cmd, env=ctx.env)


def _remove_orphan_containers(
    ctx: Context, emit: Callable[[str], None], extra_profiles: list[str] | None = None
) -> None:
    """Remove this project's containers whose compose SERVICE is no longer in the rendered
    config — the targeted replacement for handing ``--remove-orphans`` to the reconcile's
    ``up``. The bundled podman-compose (1.6.0) implements ``up --remove-orphans`` by running
    an internal ``down`` that INHERITS the flag, and that down's orphan branch stops and
    removes EVERY container labelled with the project — service scoping ignored — then the
    up re-creates only the changed subset. One ``fy mode gcp=sa→user`` flip removed a whole
    running dev stack that way (2026-08-05). So foldyard computes orphans itself: render the
    current config's service list, list the project's containers, and remove exactly those
    whose service label left the config (e.g. the metadata emulator after leaving the gcp
    rungs). Fail-safe in every direction — a failed render/probe, an empty service list, or
    a container with no service label all mean "remove nothing" (stale-but-running beats
    swept-by-accident); named volumes are never touched."""
    try:
        rendered = _run(
            [*ctx.compose, *_profile_flags(ctx, extra_profiles), "config", "--services"],
            env=ctx.env,
            cwd=str(ctx.main),
        )
        services = {s.strip() for s in rendered.stdout.splitlines() if s.strip()}
        if rendered.returncode != 0 or not services:
            return
        ps = _run(
            [
                config.engine(),
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={ctx.project}",
                "--format",
                "{{.ID}}",
            ],
            env=ctx.env,
        )
        ids = [i.strip() for i in ps.stdout.splitlines() if i.strip()]
        if ps.returncode != 0 or not ids:
            return
        insp = _run(
            [
                config.engine(),
                "inspect",
                "--format",
                '{{.Id}} {{index .Config.Labels "com.docker.compose.service"}}',
                *ids,
            ],
            env=ctx.env,
        )
        if insp.returncode != 0:
            return
        orphans = []
        for line in insp.stdout.splitlines():
            cid, _, service = line.strip().partition(" ")
            if cid and service and service != "<no value>" and service not in services:
                orphans.append((cid, service))
        if not orphans:
            return
        names = ", ".join(s for _, s in orphans)
        emit(
            f"▶ removing {len(orphans)} container(s) whose service left the rendered config "
            f"({names}); named volumes are kept…"
        )
        cmd = [config.engine(), "rm", "-f"] + [cid for cid, _ in orphans]
        emit("+ " + " ".join(cmd))
        _run(cmd, env=ctx.env)
    except Exception:
        return  # best-effort: an orphan left running is recoverable, a bad sweep is not


def _stack_is_up(ctx: Context, ignore_services: set[str] | frozenset[str] = frozenset()) -> bool:
    """Is this worktree's compose stack already running? (any container with its compose-project
    label). ``ignore_services`` — the plugin-declared posture services the reconcile itself
    materializes — don't count: a lone metadata emulator must not read as "the stack is up", or
    the NEXT posture flip would `up -d` the whole heavy stack the dev never started. EVERY
    ambiguous probe answers False, because the two failure costs are asymmetric: False on an
    actually-up stack only skips one edge-triggered recreate (stale posture env, healed by the
    next flip or `fy up`); True on a down stack starts an unrequested full build+up (a TTL
    expiry did exactly that on 2026-08-07 — 300MB of image builds into the supervisor log).
    So: no/failed ps ⇒ False; a failed/short label probe ⇒ False; and a container WITHOUT a
    compose service label (foreign/manual — compose always stamps one) never counts as stack."""
    try:
        out = subprocess.run(
            [
                config.engine(),
                "ps",
                "--filter",
                f"label=com.docker.compose.project={ctx.project}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=ctx.env,
        )
        if out.returncode != 0:
            return False
        names = [n.strip() for n in out.stdout.splitlines() if n.strip()]
        if not names:
            return False
        if not ignore_services:
            return True
        insp = subprocess.run(
            [
                config.engine(),
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.service"}}',
                *names,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=ctx.env,
        )
        services = [s.strip() for s in insp.stdout.splitlines()]
        if insp.returncode != 0 or len(services) != len(names):
            return False
        return any(s and s not in ignore_services for s in services)
    except Exception:
        return False


def reconcile_posture(
    prev_posture: dict,
    new_posture: dict,
    cfg: config.Config | None = None,
    sink: Callable[[str], None] | None = None,
) -> bool:
    """Bring an ALREADY-RUNNING stack into line after a posture change — the fix for the symptom
    where ``gcp=off→logs`` left the ``metadata-emulator`` absent until a manual ``fy up``
    (per-consumer-registry-plan.md "Reconcile scope"). The arguments are posture SIGNATURES
    (devmode.posture_signature: derived env + overlay list), so overlay/env-only changes
    (``llm=off→record``) reconcile too, not just profile toggles. Two guardrails keep it tame:
    (1) a NO-OP unless the signature actually changed, and (2) only touch a stack that is
    ALREADY up — a posture flip must never START a heavy stack the dev didn't ask for. The one
    carve-out from (2): the tiny plugin-declared POSTURE services (``Plugin.mode_services``,
    e.g. the gcp metadata emulator) are converged even when the stack is down — they are what
    the flip is asking for, and without them a gcp rung declared in a never-upped worktree read
    as granted while the box couldn't mint a token (see ``_reconcile_posture_services``). No
    ``--build`` (a posture toggle needs no rebuild). Now-inactive profile containers (e.g.
    the metadata emulator when leaving the gcp rungs) are dropped by foldyard's own targeted
    sweep AFTER a successful up (``_remove_orphan_containers``) — NEVER by passing
    ``--remove-orphans`` to the up itself, which under the bundled podman-compose tears down
    the entire project (see the sweep's docstring).
    The ``up`` also spans every profile that currently has RUNNING services (see
    ``_running_extra_profiles``), so profile-gated services a developer started outside the mode
    system — e.g. the ``data`` workers from ``just data-up`` — are re-rendered into the new
    posture instead of silently keeping the old identity/env. Host-only and best-effort: any
    environment hiccup is swallowed so it never breaks ``fy mode`` — but a compose run that
    actually FAILED returns False so callers (the TUI) can say so instead of claiming success.

    Output goes through ``sink`` (default: stderr via ``_err``). The TUI passes a sink that routes
    into its log pane — CRUCIAL because the compose subprocess is CAPTURED (not inherited): a raw
    `docker compose` writes to the terminal's real fds, which a Textual app can't redirect, so its
    output bled over the TUI. Capturing + sinking keeps the TUI clean (a spinner shows instead)."""
    emit = sink or _err
    if prev_posture == new_posture or config.in_box():
        return True
    prev_profiles = prev_posture["env"].get("COMPOSE_PROFILES", "")
    new_profiles = new_posture["env"].get("COMPOSE_PROFILES", "")
    overlays = ", ".join(o.rsplit("/", 1)[-1] for o in new_posture["overlays"]) or "none"
    posture = f"profiles {prev_profiles or 'none'} → {new_profiles or 'none'}, overlays {overlays}"
    try:
        ctx_mgr = config.using(cfg) if cfg is not None else nullcontext()
        with ctx_mgr:
            # Cheap, subprocess-free skip for stack-less consumers under the bound config.
            if not config.has_compose_stack():
                return True
            ctx = resolve(no_machine=True, worktree=cfg.worktree if cfg is not None else None)
            from .plugins import registry

            mode = devmode.read()["mode"]
            # Posture-critical services (service → wanted under this mode): the tiny stateless
            # containers a rung is enforced by (the gcp metadata emulator). The reconcile itself
            # materializes them, so they don't count toward "the stack is up".
            managed = registry().mode_services(mode)
            if not _stack_is_up(ctx, ignore_services=set(managed)):
                # guardrail (2): never START the stack on a posture change — but DO converge the
                # declared posture services: they are exactly what the flip is asking for (see
                # _reconcile_posture_services).
                ok = _reconcile_posture_services(ctx, mode, managed, emit)
                _host_log(
                    f"stack {ctx.project} down — the mode applies on next `fy up` ({posture})"
                    + ("" if ok else "; posture services FAILED to converge")
                )
                return ok
            # Stage VM-visible assets a newly-active profile needs (e.g. the gcp metadata emulator's
            # server.py) before compose recreates with the new profile set.
            registry().stage_assets(
                mode,
                ctx.env["FOLDYARD_CHECKOUT"],
                ctx.env.get("HERE") or config.dev_vm_rel(),
            )
            extra = _running_extra_profiles(ctx)
            emit(
                f"▶ mode changed — reconciling stack "
                f"({prev_profiles or 'none'} → {new_profiles or 'none'}"
                + (f", keeping running: {','.join(extra)}" if extra else "")
                + f") for {ctx.project}…"
            )
            # The reconcile `up` is as blind to another provider's containers as `fy up` is —
            # sweep them first or it dies on name conflicts (e.g. a stack predating the
            # bundled-podman-compose switch).
            _reconcile_foreign_containers(ctx, emit)
            rc = _compose_captured(ctx, ["up", "-d"], emit, extra_profiles=extra)
            if rc != 0:
                emit(
                    f"✗ reconcile failed (compose exited {rc}) — the stack may still be "
                    "on the OLD mode; check the output above, then `fy up`"
                )
                _host_log(
                    f"stack {ctx.project} reconcile FAILED (compose exited {rc}) — it may still "
                    f"be on the OLD mode; `fy up` re-renders ({posture})"
                )
                return False
            # Only after a SUCCESSFUL up: reap containers whose service left the new
            # posture's config (dropped profile/overlay). A failed up keeps everything.
            _remove_orphan_containers(ctx, emit, extra_profiles=extra)
            _host_log(
                f"stack {ctx.project} reconciled ({posture}"
                + (f", keeping running: {','.join(extra)}" if extra else "")
                + ")"
            )
    except Exception as e:  # never let a reconcile hiccup break the mode write
        emit(f"⚠ stack mode reconcile skipped: {e}")
        _host_log(f"stack reconcile skipped: {e} ({posture})")
    return True


def _host_log(message: str) -> None:
    """The reconcile's outcome, as one ``[mode]`` line in the host supervisor log — the only
    record that outlives the terminal it ran in (see ``supervisor.append_log``)."""
    from . import supervisor  # deferred like stack.up's: the supervisor imports stack lazily too

    supervisor.append_log(f"[mode] {message}")


def _running_managed_containers(ctx: Context, services: set[str]) -> list[tuple[str, str]]:
    """``(container id, service)`` for RUNNING project containers whose compose service is one of
    ``services``. Fail-safe: ``[]`` on any probe error — the stack-down reap then removes nothing
    (a stale emulator left running is recoverable; a bad sweep is not)."""
    if not services:
        return []
    try:
        ps = _run(
            [
                config.engine(),
                "ps",
                "--filter",
                f"label=com.docker.compose.project={ctx.project}",
                "--format",
                "{{.ID}}",
            ],
            env=ctx.env,
        )
        ids = [i.strip() for i in ps.stdout.splitlines() if i.strip()]
        if ps.returncode != 0 or not ids:
            return []
        insp = _run(
            [
                config.engine(),
                "inspect",
                "--format",
                '{{.Id}} {{index .Config.Labels "com.docker.compose.service"}}',
                *ids,
            ],
            env=ctx.env,
        )
        if insp.returncode != 0:
            return []
        out = []
        for line in insp.stdout.splitlines():
            cid, _, service = line.strip().partition(" ")
            if cid and service in services:
                out.append((cid, service))
        return out
    except Exception:
        return []


def _reconcile_posture_services(
    ctx: Context, mode: dict, managed: dict[str, bool], emit: Callable[[str], None]
) -> bool:
    """Converge ONLY the plugin-declared posture services while the stack is down.

    The stack-down complement of the full reconcile: a posture flip must never start the heavy
    stack (guardrail 2), but the tiny containers a rung is ENFORCED by — the gcp metadata
    emulator — are exactly what the flip is asking for. Without this, a gcp rung declared in a
    checkout that never ran ``fy up`` read as granted (`fy mode` showed it, the host minter ran)
    while the box couldn't mint a single token, and a 2026-08-07 prod investigation burned a
    session chasing the phantom permission errors. Reaps managed services whose rung dropped
    (a zero-secret posture must not keep an identity emulator around), ``up -d``s the wanted
    ones (no build — posture services are image-only by contract), and never touches anything
    else."""
    stale = [
        (cid, svc)
        for cid, svc in _running_managed_containers(ctx, set(managed))
        if not managed[svc]
    ]
    if stale:
        names = ", ".join(svc for _, svc in stale)
        emit(f"▶ removing mode service(s) whose level dropped ({names})…")
        _run([config.engine(), "rm", "-f", *[cid for cid, _ in stale]], env=ctx.env)
    wanted = sorted(s for s, w in managed.items() if w)
    if not wanted:
        return True
    from .plugins import registry

    registry().stage_assets(
        mode, ctx.env["FOLDYARD_CHECKOUT"], ctx.env.get("HERE") or config.dev_vm_rel()
    )
    # The stack never came up, so neither did its network — ensure it exactly like `fy up` does.
    if config.external_network():
        ensure_network(config.engine(), f"{ctx.project}_default", ctx.env)
    emit(
        f"▶ stack is down — starting the service(s) the mode depends on "
        f"({', '.join(wanted)}) for {ctx.project}…"
    )
    rc = _compose_captured(ctx, ["up", "-d", *wanted], emit)
    if rc != 0:
        emit(f"✗ mode service start failed (compose exited {rc}) — check above, then `fy up`")
        return False
    return True


def _profile_flags(ctx: Context, extra_profiles: list[str] | None) -> list[str]:
    """``--profile`` flags for a compose invocation that must ALSO span ``extra_profiles``.
    Compose ignores ``COMPOSE_PROFILES`` the moment any ``--profile`` flag is passed, so when
    extras exist the posture-derived actives are re-passed explicitly alongside them; with no
    extras, no flags — the env var keeps working alone."""
    if not extra_profiles:
        return []
    active = [p for p in (ctx.env.get("COMPOSE_PROFILES") or "").split(",") if p]
    flags: list[str] = []
    for p in dict.fromkeys([*active, *extra_profiles]):  # ordered de-dupe
        flags += ["--profile", p]
    return flags


def _left(deadline: float | None) -> float | None:
    """The seconds a ``deadline`` (a ``time.monotonic()`` value) leaves for the next call; None
    (unbounded) without one. Never zero — subprocess treats that as expired before it starts."""
    return None if deadline is None else max(0.1, deadline - time.monotonic())


def _running_services(ctx: Context, deadline: float | None = None) -> set[str] | None:
    """The project's RUNNING compose services (``compose ps``); None when the engine can't say."""
    try:
        out = _run(
            [*ctx.compose, "ps", "--format", "json"],
            env=ctx.env,
            cwd=str(ctx.main),
            timeout=_left(deadline),
        )
    except Exception:
        return None
    return _compose_ps_services(out.stdout) if out.returncode == 0 else None


def _running_extra_profiles(ctx: Context, deadline: float | None = None) -> list[str]:
    """Compose profiles OUTSIDE the posture-derived active set that currently have RUNNING
    services — e.g. the ``data`` workers ``just data-up`` started (that profile belongs to the
    developer, not the mode system). The posture reconcile and ``up`` union these into their
    ``up -d`` so those services' config is re-rendered too; without this, profile-gated services
    kept their OLD identity/env across a ``gcp=sa⇄off`` flip (they aren't in the active-profile
    config, so compose ignored them) — and the reconcile's orphan sweep, rendering without them,
    took a running one for departed. Each service's profiles come from the ``-f`` files
    themselves (:func:`_service_profiles`), not ``config --profiles``: the bundled podman-compose
    has no such flag, so discovery always came back empty there (#33). Fully generic, no profile
    names baked in. Best-effort: [] on any error (the reconcile then covers just the derived set,
    as before). ``deadline`` bounds the probe (the heal's)."""
    try:
        running = _running_services(ctx, deadline)
        if not running:
            return []
        profiles = _service_profiles(ctx)
        enabled = {p for p in (ctx.env.get("COMPOSE_PROFILES") or "").split(",") if p}
        extra: list[str] = []
        # As FEW profiles as enable every running service: an unscoped reconcile `up` starts every
        # service of each profile it names. Single-profile services go first — they leave no
        # choice — so a multi-profile one reuses theirs before adding its own first profile.
        gated = sorted(
            ((profiles[s], s) for s in running if profiles.get(s)), key=lambda x: (len(x[0]), x[1])
        )
        for service_profiles, _ in gated:
            if not enabled.intersection(service_profiles):
                enabled.add(service_profiles[0])
                extra.append(service_profiles[0])
        return sorted(extra)
    except Exception:
        return []


def _service_profiles(ctx: Context) -> dict[str, list[str]]:
    """Service → its ``profiles``, merged across the ``-f`` files in order the way compose
    merges them: a later file's list APPENDS (podman-compose's list merge, compose-spec's rule for
    sequences), de-duplicated in order. Raises on an unreadable file — callers are best-effort.
    PyYAML rides in with the bundled podman-compose; imported here only, off the hot path."""
    import yaml

    files = [ctx.compose[i + 1] for i, arg in enumerate(ctx.compose[:-1]) if arg == "-f"]
    out: dict[str, list[str]] = {}
    for f in files:
        path = Path(f) if Path(f).is_absolute() else ctx.main / f
        services = (yaml.safe_load(path.read_text()) or {}).get("services") or {}
        for name, spec in services.items():
            if isinstance(spec, dict) and spec.get("profiles"):
                merged = [*out.get(name, []), *(str(p) for p in spec["profiles"])]
                out[name] = list(dict.fromkeys(merged))
    return out


def recreate_services(
    services: list[str], *, worktree: str = "", timeout: float = 180.0
) -> tuple[bool, str]:
    """Recreate the named compose services in ``worktree``'s stack (``""`` = main) — the
    capability-heal "resnapshot" action (``[resnapshot_on_capability]``): a service that
    snapshots credentials once at boot only picks a healed chain up by rebooting. RECREATE, not
    ``compose restart`` (#33): a restart reboots the existing container with its create-time
    env and ignores the ``-f`` list entirely, so a service created under an older posture came
    back on it while looking healed. ``up --force-recreate`` renders it from the CURRENT posture,
    so a heal re-reads credentials AND converges any drift. ``--no-build`` (a heal never builds),
    ``--no-deps`` (only the named services), and the running extra profiles spanned the way the
    posture reconcile spans them, so a profile-gated worker isn't dropped from the render. Only
    services that are RUNNING are named: a heal refreshes what runs and never starts anything —
    ``up`` would create a named service that never ran (under docker compose, enabling its
    inactive profile too) without the dependencies ``--no-deps`` skips. An engine that can't say
    what runs recreates nothing (fail-safe). The summary says what was recreated and what not.
    Headless by design (it runs on a supervisor worker thread): output is captured, and
    ``timeout`` is ONE deadline over every compose call here — the probes as well as the ``up`` —
    so a hung engine can't pin the in-flight guard forever, and it NEVER raises — the
    caller logs (ok, summary) either way. Passing ``worktree`` explicitly (not None) pins the
    resolve to that checkout even when the caller's env carries a different ``WORKTREE``."""
    # Headless and host-side: a heal must never BOOT a stopped VM — nothing runs in it to recreate.
    if (down := machine_down()) is not None:
        return False, f"{down} — nothing to recreate"
    try:
        deadline = time.monotonic() + timeout
        ctx = resolve(worktree=worktree)
        running = _running_services(ctx, deadline)
        if running is None:
            return False, "couldn't list running services — nothing recreated"
        wanted = [s for s in services if s in running]
        idle = [s for s in services if s not in running]
        if not wanted:
            return True, f"none of {', '.join(services)} running — nothing to recreate"
        extra = _running_extra_profiles(ctx, deadline)
        # A named service the render leaves out is silently skipped by podman-compose (exit 0),
        # so only claim what the render has — the rest is reported, never "recreated".
        rendered = _rendered_services(ctx, extra, deadline)
        if not rendered:
            return False, "couldn't render the compose config — nothing recreated"
        unrendered = [s for s in wanted if s not in rendered]
        wanted = [s for s in wanted if s in rendered]
        if not wanted:
            return False, f"{', '.join(unrendered)} not in the rendered config — nothing recreated"
        cmd = [
            *ctx.compose,
            *_profile_flags(ctx, extra),
            *["up", "-d", "--no-build", "--force-recreate", "--no-deps", *wanted],
        ]
        _err("+ " + " ".join(shlex.quote(c) for c in cmd))
        proc = _run(cmd, env=ctx.env, cwd=str(ctx.main), timeout=_left(deadline))
        if proc.returncode != 0:
            lines = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
            return False, (lines[-1] if lines else f"compose exited {proc.returncode}")
        return (not unrendered), f"recreated {', '.join(wanted)}" + (
            f"; not running, left alone: {', '.join(idle)}" if idle else ""
        ) + (
            f"; not in the rendered config, NOT recreated: {', '.join(unrendered)}"
            if unrendered
            else ""
        )
    except (Exception, SystemExit) as e:  # resolve aborts a missing worktree with SystemExit
        return False, f"{type(e).__name__}: {e}"


def _compose_ps_services(stdout: str) -> set[str] | None:
    """RUNNING service names out of ``compose ps --format json`` — one JSON object per line on
    current docker compose (NDJSON), a single JSON array on older releases and under the bundled
    podman-compose, whose ``ps`` is ``podman ps -a``: no ``Service`` key (the service is only the
    ``com.docker.compose.service`` label) and exited containers included. Reading ``Service``
    alone made every running profile invisible there, so a posture reconcile never re-rendered
    the profile-gated workers (#33). Tolerant of all three shapes. None when non-empty output
    yields no row at all: that says nothing about what runs, and an empty set would claim
    nothing does."""
    text = stdout.strip()
    if not text:
        return set()
    try:
        parsed = json.loads(text)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        rows = []
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        if not rows:
            return None
    services: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("State", "running") != "running":
            continue
        labels = row.get("Labels")
        service = row.get("Service") or (
            labels.get("com.docker.compose.service") if isinstance(labels, dict) else None
        )
        if service:
            services.add(service)
    return services


def _compose_captured(
    ctx: Context,
    args: list[str],
    emit: Callable[[str], None],
    extra_profiles: list[str] | None = None,
) -> int:
    """Like ``_compose`` but never inherits the terminal fds: output is STREAMED line-by-line
    through ``emit`` (stderr merged into stdout, via devmode.run_stream) and the exit code is
    returned. Used by the posture reconcile so a TUI-driven `fy mode` toggle doesn't bleed raw
    compose output over the Textual UI (inherited fds bypass any Python-level stdout/stderr
    redirect — capturing is the only fix), and so the log pane fills LIVE during a slow recreate
    instead of all at once at the end."""
    cmd = list(ctx.compose) + _profile_flags(ctx, extra_profiles) + args
    emit("+ " + " ".join(shlex.quote(c) for c in cmd))
    return devmode.run_stream(cmd, emit, env=ctx.env, cwd=str(ctx.main))


def stack_declared(verb: str, box_verb: str | None) -> int | None:
    """The gate every verb that acts on the compose stack runs FIRST — before any engine or
    machine call. Returns an exit code to stop with, or None to carry on.

    ``[project].compose`` unset ⇒ a box-only project: say what ``fy <verb>`` acts on, that this
    project declares no stack, the box counterpart, and where a stack gets set up — exit 0,
    pure config, nothing touched. Nothing is invented: the old default path handed the compose
    provider a file nobody wrote, and its CRITICAL "missing files" read as a real failure to an
    agent following the guide.
    Declared but missing on disk ⇒ foldyard's own error naming the file and the checkout it
    looked in (a branch from before the file, a rename), exit 1. The checkout is the one
    ``resolve()`` joins the ``-f`` paths onto — an explicit ``WORKTREE`` (or a cwd inside one)
    binds the stack to that sibling checkout, which ``config.repo_root()`` (cwd / FOLDYARD_REPO)
    need not be — so the preflight can't pass on main's file and hand the provider the
    worktree's. That costs the memoised ``main_repo()`` git call, never the engine."""
    if not config.has_compose_stack():
        print(
            f"ℹ `fy {verb}` acts on the compose stack, and '{config.project_prefix()}' doesn't "
            "drive one ([project].compose is unset in foldyard.toml)."
        )
        if box_verb:
            print(f"  The dev box: {box_verb}")
        print("  To bring a stack in: fy docs quickstart (step 3) · fy docs configuration")
        return 0
    return 1 if _missing_declared_compose() else None


def _missing_declared_compose() -> bool:
    """Is a declared compose file absent from the checkout the stack is bound to? Reports it
    when so — foldyard's own error naming the file and the checkout it looked in. Shared by the
    stack verbs' gate and ``shellenv`` so the two never disagree on what a missing file looks
    like, and side-effect-free beyond the memoised ``main_repo()`` git call — never the engine
    or the machine — so it can run BEFORE ``_context()`` provisions anything. No declaration ⇒
    nothing to check (the box-only case is the caller's to explain); a worktree that doesn't
    exist is ``_context()``'s own error ("no worktree at …"), not a missing file under it."""
    if not config.has_compose_stack():
        return False
    main = main_repo()
    wt = _active_worktree(worktrees_root(main))
    checkout = worktrees_root(main) / wt if wt else main
    if not checkout.is_dir():
        return False
    missing = config.missing_compose_files(checkout)
    if missing:
        _err(f"✗ [project].compose names {', '.join(missing)} — not found under {checkout}.")
        _err("  Check the branch this checkout is on, or fix the path in foldyard.toml.")
    return bool(missing)


def engine_reachable(nothing_to: str, start: str = "fy up") -> bool:
    """Gate for verbs that only READ or TEAR DOWN engine state (down/nuke/ps/logs), or that
    need a box already running (`fy code`, `fy box shell`): an inherited DOCKER_HOST (box, CI)
    passes, a running machine passes — but an absent or stopped machine short-circuits with a
    note instead of letting ``resolve()`` PROVISION a whole VM just to find nothing inside it
    (``fy down`` after ``fy machine rm`` used to start downloading a VM image; ``fy code`` booted
    the VM only to refuse on the box). Only ``fy up``/``fy box up``/``ensure`` create machines —
    ``start`` names the one the note should point at."""
    reason = machine_down()
    if reason is None:
        return True
    _err(f"({reason} — nothing to {nothing_to}. `{start}` creates/starts it.)")
    return False


def machine_down() -> str | None:
    """Why ``resolve()`` would have to BOOT the machine to reach the engine — None when it
    wouldn't (an inherited DOCKER_HOST, or a running machine). Read-only: the quiet form of
    :func:`engine_reachable`, for callers that report on their own terms."""
    if os.environ.get("DOCKER_HOST"):
        return None
    return machine.not_running_reason()


def down() -> int:
    if (rc := stack_declared("down", "fy box down")) is not None:
        return rc
    if not engine_reachable("stop"):
        return 0
    ctx = resolve()
    # Containers a different compose provider created are invisible to this one — a plain
    # `down` would leave that whole stack running. Sweep them first, then compose reaps its own.
    _reconcile_foreign_containers(ctx)
    # --remove-orphans so switching posture reaps profile-gated leftovers: a container from a
    # now-inactive profile (e.g. the `metadata` emulator from a prior gcp=sa session) isn't in
    # the default-profile config, so a plain `down` would strand it. Scoped to this compose
    # project (COMPOSE_PROJECT_NAME), so it never touches another worktree's stack.
    return _compose(ctx, ["down", "--remove-orphans"])


def ps() -> int:
    if (rc := stack_declared("ps", "fy box ps")) is not None:
        return rc
    if not engine_reachable("show"):
        return 0
    return _compose(resolve(), ["ps"])


def logs(svc: list[str]) -> int:
    if (rc := stack_declared("logs", None)) is not None:
        return rc
    if not engine_reachable("follow"):
        return 0
    return _compose(resolve(), ["logs", "-f", "-n", "50", *svc])


def shell() -> int:
    if (rc := stack_declared("shell", "fy box shell")) is not None:
        return rc
    if not engine_reachable("exec into"):
        return 1
    ctx = resolve()
    return _compose(ctx, ["exec", ctx.app, "bash"])


def _remove_external_network(ctx: Context) -> None:
    """nuke's counterpart to ensure_network: compose never removes an external network, so
    drop it here. Best-effort + quiet — while the dev box (a non-compose container) still
    holds it, the rm fails and the network correctly stays; it goes once the box is gone."""
    if not config.external_network():
        return
    subprocess.run(
        [config.engine(), "network", "rm", f"{ctx.project}_default"],
        env=ctx.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def remove_devbox_volumes(project: str | None = None) -> int:
    """Drop the dev box's PER-BOX named volumes (``{project}-devbox-*``: the shadow
    build-artifact volumes from ``[box].shadow_volumes``). ``box down`` deliberately keeps
    them so a box restart stays warm; worktree REMOVAL calls this so they don't leak forever
    once the box is gone for good. The shared agent/editor/cache volumes (``devbox_tools``,
    ``devbox_claude_home``, …) carry no project prefix and are untouched. Returns the number
    of volumes removed.

    ``project`` targets a worktree whose CHECKOUT is already gone — ``resolve()`` refuses a
    missing worktree dir, so the env comes from the main context and the volume prefix from
    the caller-supplied project name."""
    if not engine_reachable("clean up"):
        return 0
    ctx = resolve(worktree="" if project else None)
    eng = config.engine()
    prefix = f"{project or ctx.project}-devbox-"
    vols = subprocess.run(
        [eng, "volume", "ls", "-q", "--filter", f"name={prefix}"],
        env=ctx.env,
        capture_output=True,
        text=True,
    ).stdout.split()
    # The engine's name filter is a substring match, so a shorter project name would also see
    # a longer sibling's volumes (proj-a-devbox-… matches proj-a-b… queries) — pin the prefix.
    vols = [v for v in vols if v.startswith(prefix)]
    if vols:
        _err(f"+ {eng} volume rm " + " ".join(vols))
        if subprocess.run([eng, "volume", "rm", *vols], env=ctx.env).returncode != 0:
            return 0
    return len(vols)


# ── disk headroom + the pre-build reclaim ─────────────────────────────────────────────
# ONE threshold, read by BOTH consumers — `fy doctor`'s warning row and `fy up`'s reclaim — so
# the doctor can never report a healthy store while `up` is quietly sweeping it (or the reverse).
# The two are OR'd (see DiskHeadroom.low). The fraction is on its own enough to call a store low —
# the VM's disk is sized per consumer ([machine].disk_gib), so a percentage is what travels. The
# floor only WIDENS that, for a small disk where a healthy-looking 20% is still less than a single
# image build (4 GiB free of 20 GiB).
LOW_DISK_FRACTION = 0.20
LOW_DISK_FLOOR = 5 * 1024**3
# Nothing younger than this is ever swept. A build IN FLIGHT commits untagged, childless layers,
# and those ARE `dangling` until its final commit tags them — on a box shared by several agent
# sessions an unguarded prune would delete another session's build out from under it. A day is
# far past any build and still reclaims every superseded image.
RECLAIM_MIN_AGE = "24h"


def _gib(n: int) -> str:
    return f"{n / 1024**3:.1f} GiB"


@dataclass(frozen=True)
class DiskHeadroom:
    """Bytes used/allocated on the engine's image+volume store — i.e. the VM's disk."""

    used: int
    total: int

    @property
    def free(self) -> int:
        return max(0, self.total - self.used)

    @property
    def fraction_free(self) -> float:
        return self.free / self.total if self.total else 0.0

    @property
    def low(self) -> bool:
        return self.free < LOW_DISK_FLOOR or self.fraction_free < LOW_DISK_FRACTION

    def render(self) -> str:
        return f"{_gib(self.free)} free of {_gib(self.total)} ({self.fraction_free:.0%})"


def disk_headroom(env: dict[str, str] | None = None, timeout: float = 15) -> DiskHeadroom | None:
    """Free space on the engine's store, or ``None`` when it cannot be known.

    Read from ``podman info`` (``store.graphRootAllocated``/``graphRootUsed``) rather than a
    ``statvfs``: the store lives inside the VM, so a host-side ``df`` measures the host's disk
    and not the one that runs out mid-build. docker exposes no equivalent field, and guessing
    is worse than declining to answer — every caller treats ``None`` as "no finding, change
    nothing".

    ``timeout`` is generous for the pre-build reclaim (the engine is often busy right then) and
    cut to the module's probe budget by doctor, whose rows a TUI re-runs on a 5s timer."""
    if config.engine() != "podman":
        return None
    try:
        proc = _run(["podman", "info", "--format", "json"], env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        store = json.loads(proc.stdout)["store"]
        total, used = int(store["graphRootAllocated"]), int(store["graphRootUsed"])
    except (ValueError, KeyError, TypeError):
        return None
    return DiskHeadroom(used, total) if total > 0 else None


def _rendered_services(
    ctx: Context, extra_profiles: list[str] | None = None, deadline: float | None = None
) -> set[str]:
    """The compose config's service names (``set()`` on any render failure — callers then
    decompose no image names at all, which keeps everything). ``deadline`` bounds the render."""
    proc = _run(
        [*ctx.compose, *_profile_flags(ctx, extra_profiles), "config", "--services"],
        env=ctx.env,
        cwd=str(ctx.main),
        timeout=_left(deadline),
    )
    if proc.returncode != 0:
        return set()
    return {s.strip() for s in proc.stdout.splitlines() if s.strip()}


def _live_projects(ctx: Context) -> set[str] | None:
    """Compose project names that still have a checkout: the main project plus one per
    REGISTERED git worktree. ``None`` = the list couldn't be read, and callers then sweep
    nothing — an unreadable worktree list must never read as "everything is orphaned".

    The name is derived exactly as ``_context`` derives ``PODMAN_PROJECT``: the checkout's path
    RELATIVE to the worktrees root, separators flattened to ``-``. A nested worktree
    (``<root>/feat/foo`` ⇒ project ``{prefix}-feat-foo``) would otherwise resolve to
    ``{prefix}-foo`` here, miss its own images in the live set, and get a LIVE worktree's
    images swept. A tree parked outside the root can't have a stack at all; its basename is
    added anyway, because an extra live name only ever keeps more."""
    prefix = config.project_prefix()
    try:
        proc = _run(["git", "-C", str(ctx.main), "worktree", "list", "--porcelain"], env=ctx.env)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    root = worktrees_root(ctx.main)
    live = {prefix}
    for line in proc.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line.split(" ", 1)[1].strip())
        if str(path) == str(ctx.main):
            continue  # the main checkout is the bare prefix, already in the set
        try:
            name = path.resolve().relative_to(root.resolve()).as_posix().replace("/", "-")
        except (ValueError, OSError):
            name = path.name
        live.add(f"{prefix}-{name}")
    return live


def _orphan_project_images(ctx: Context, services: set[str], live: set[str]) -> list[str]:
    """Locally built compose images whose project no longer has a checkout — a removed
    worktree's ``{prefix}-{name}_{service}`` tags. ``image prune`` can never reclaim these
    (they are TAGGED, so never dangling) and nothing else deletes them, so they leak forever.

    Parsed from the tag, not from labels: podman-compose writes no ``com.docker.compose.*``
    labels on the images it builds (verified 2026-08), so the name is the only signal. Two
    traps, both fatal to a prefix match:

    * the separator differs per provider — podman-compose builds ``{project}_{service}``,
      docker-compose ``{project}-{service}`` — and one store can hold BOTH spellings;
    * ``{prefix}-{service}`` (the main project, docker spelling) has the same SHAPE as
      ``{prefix}-{worktree}-{service}``, and a worktree named ``fy`` is a prefix of one named
      ``fy-gh-bot``.

    So every name is decomposed against the rendered service list, and anything that ALSO
    decomposes to a live project is kept: ambiguity always resolves toward keeping. An image
    outside the project prefix is never a candidate."""
    if not services or not live:
        return []
    prefix = config.project_prefix()
    proc = _run([config.engine(), "images", "--format", "{{.Repository}}:{{.Tag}}"], env=ctx.env)
    if proc.returncode != 0:
        return []
    orphans: list[str] = []
    for ref in proc.stdout.split():
        name = ref.rpartition(":")[0].split("/")[-1]  # strip the tag and any registry/localhost
        projects = {
            name[: -len(sep + svc)]
            for svc in services
            for sep in ("_", "-")
            if name.endswith(sep + svc) and len(name) > len(sep + svc)
        }
        if not projects or projects & live:
            continue
        if all(p.startswith(f"{prefix}-") for p in projects):
            orphans.append(ref)
    return orphans


def reclaim(ctx: Context, extra_profiles: list[str] | None = None, *, force: bool = False) -> None:
    """Free engine-store space, but ONLY when the store is actually under pressure (or on
    ``force`` — the manual ``fy reclaim``). Three narrow sweeps:

    1. **dangling images older than** :data:`RECLAIM_MIN_AGE` — superseded builds. Podman's
       "dangling" means untagged AND not the parent of another image, so the layer CACHE is
       structurally excluded (every intermediate is a parent): measured on podman 5, a prune
       between two builds still yields ``--> Using cache`` for the unchanged steps. Never
       ``-a`` — that also removes unused TAGGED images (the base images), turning a reclaim
       into a re-pull through the egress wall.
    2. **images of ``{prefix}-{worktree}`` projects whose checkout is gone** — tagged, so
       sweep 1 structurally cannot see them.
    3. **the project's own ``[reclaim] script``**, in the dev box — what else fills the store
       (package-manager caches, test artefacts) lives in volumes only the box mounts.

    (``up`` separately removes the images its own build just superseded — no guard needed
    there; see ``_remove_superseded``.)

    Called from ``up`` BEFORE the build, because the failure this exists to prevent is the
    build itself dying mid-layer on ``no space left on device``. Best-effort throughout: an
    unreadable disk figure, an unrenderable service list or an unreadable worktree list each
    mean "reclaim nothing" and the build proceeds exactly as before. A store still low after
    all three is said so — never ticked — with the sweeps left to a human."""
    before = disk_headroom(ctx.env)
    if not force and (before is None or not before.low):
        return
    eng = config.engine()
    state = before.render() if before is not None else "headroom unknown"
    print(f"▶ reclaiming engine-store space ({state})…")
    cmd = [eng, "image", "prune", "-f", "--filter", f"until={RECLAIM_MIN_AGE}"]
    _err("+ " + " ".join(cmd))
    _run(cmd, env=ctx.env)
    live = _live_projects(ctx)
    if live is not None:
        orphans = _orphan_project_images(ctx, _rendered_services(ctx, extra_profiles), live)
        if orphans:
            print(f"  {len(orphans)} image(s) belong to removed worktrees — dropping those too")
            _err(f"+ {eng} rmi " + " ".join(orphans))
            _run([eng, "rmi", *orphans], env=ctx.env)
    _project_reclaim(ctx)
    # The two probes are independent: a forced reclaim runs with `before` unknown, and the
    # store's state afterwards is still worth saying even when the freed figure isn't known.
    after = disk_headroom(ctx.env)
    if after is None:
        return
    freed = (
        f"reclaimed {_gib(max(0, after.free - before.free))}"
        if before is not None
        else "reclaimed (headroom before unknown)"
    )
    if after.low:
        print(
            f"⚠ {freed}, store still low — {after.render()}. `{eng} system df` shows "
            f"what holds it; `{eng} image prune -a` also drops unused tagged images (base images "
            f"re-pull on the next build), `{eng} container prune` stopped containers."
        )
    else:
        print(f"✓ {freed} — {after.render()}")


def _project_reclaim(ctx: Context) -> None:
    """Run ``[reclaim] script`` where the volumes are: directly when already in the box, else
    exec'd into it (a stopped box is a note, not a failure — the engine sweeps still ran).
    Output streams to the terminal; the exit code is deliberately ignored."""
    script = config.reclaim_script()
    if not script:
        return
    checkout = ctx.env.get("FOLDYARD_CHECKOUT", str(ctx.main))
    if config.in_box():
        print(f"▶ running the project's reclaim script ({script})…")
        subprocess.run(["sh", script], cwd=checkout)
        return
    from . import box

    eng = config.engine()
    name, _ = box._names(ctx)
    if not box._running(eng, name, ctx.env):
        print(f"  (dev box {name} not running — skipping its reclaim script {script})")
        return
    print(f"▶ running the project's reclaim script in {name} ({script})…")
    # Login shell so PATH (pnpm/uv) matches an attached `fy box shell`.
    subprocess.run(
        [eng, "exec", "-w", checkout, name, "bash", "-lc", f"sh {shlex.quote(script)}"],
        env=ctx.env,
    )


def reclaim_now() -> int:
    """``fy reclaim``: the sweeps ``up`` runs under low headroom, unconditionally — for the
    store that is full NOW, without bouncing the box or the machine."""
    if not engine_reachable("reclaim"):
        return 0
    reclaim(resolve(), force=True)
    return 0


def nuke() -> int:
    if (rc := stack_declared("nuke", "fy box down")) is not None:
        return rc
    if not engine_reachable("nuke"):
        return 0
    ctx = resolve()
    # Promote transcripts to the durable host store before tearing volumes down. The bound-out
    # dir survives nuke, but archiving keeps the host's `claude --resume` current. Best-effort —
    # never block. Lazy import: transcripts imports stack, so a top-level import would cycle.
    from . import transcripts

    transcripts.sync_current(ctx.env)
    # Sweep another provider's containers BEFORE the down: they'd otherwise survive it (the
    # active provider can't see them) and pin the project volumes the nuke is about to remove.
    _reconcile_foreign_containers(ctx)
    # --remove-orphans for the same reason as `down`: a container from a now-inactive profile
    # (the metadata emulator after leaving the gcp rungs) isn't in the rendered config, so a
    # plain `down` strands it — and on worktree removal it then outlives the checkout.
    if ctx.worktree:
        # A worktree owns only its project-prefixed volumes; the pnpm/Playwright caches are
        # SHARED, so drop just this project's volumes and keep the shared caches.
        rc = _compose(ctx, ["down", "--remove-orphans"])
        eng = config.engine()
        vols = subprocess.run(
            [eng, "volume", "ls", "-q", "--filter", f"name={ctx.project}_"],
            env=ctx.env,
            capture_output=True,
            text=True,
        ).stdout.split()
        if vols:
            _err(f"+ {eng} volume rm " + " ".join(vols))
            subprocess.run([eng, "volume", "rm", *vols], env=ctx.env)
        _remove_external_network(ctx)
        print(f"✓ worktree {ctx.worktree} nuked (shared pnpm/playwright caches kept)")
        return rc
    rc = _compose(ctx, ["down", "-v", "--remove-orphans"])
    _remove_external_network(ctx)
    return rc
