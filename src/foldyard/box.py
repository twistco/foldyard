"""The dev box — `foldyard box build|up|shell|down|ps` (the `[box]` table — docs/configuration.md).

A long-lived, per-worktree container ON the rootless machine, with the (worktree's) repo +
the machine's podman socket, so an agent inside drives the WHOLE stack exactly like the
engine verbs do. SAFE because the machine is rootless + mounts only the repo (the escape
test is refused — `foldyard verify`). Long-lived so MULTIPLE sessions attach as separate
`exec` shells into the ONE per-worktree box.

DELIBERATELY no ~/.ssh mount, no SSH-agent forwarding, no gh/GITHUB_TOKEN: the box gets the
full local git toolkit (via the mounted .git) but NO credential to reach the origin — prompt
injection / malicious deps can't push or exfiltrate via push.

Faithful port of the `devbox` recipe. Project-specific values come from `[box]`
in foldyard.toml (image · shadow_volumes · caches · warmup · env · sock_in_vm · tools ·
bootstrap); the credential box env/mounts (the github proxy CA + env, the gcp metadata host +
SA label) AND the agent/editor volumes + installs (Claude, VS Code) come from the plugins
(`registry().box_args` + `registry().box_bootstrap`), each gated on its config table — so a
table-less consumer gets a plain shell box. Core here keeps only the socket, the shared tools
prefix, caches, the foldyard self-install, and `fy claude` (the launcher).

VALIDATION CAVEAT: box CREATION can't be exercised from inside the box (it would recreate the
running box). The command assembly is golden-tested (engine mocked) + faithful to the recipe;
`up`/`build` want a Mac / fresh-box run. `shell`/`down`/`ps` are safe everywhere.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from . import config, keyless, stack
from .plugins import registry

_BOX_FINGERPRINT_LABEL = "io.foldyard.box-fingerprint"

# TOML's bare-key alphabet, and its two non-finite floats — both for `_toml_value`/`_toml_key`,
# which render `[codex.config]` back into the TOML syntax `codex -c` parses.
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_INF = float("inf")


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _echo(cmd: list[str]) -> None:
    _err("+ " + " ".join(shlex.quote(c) for c in cmd))


def _ctx():
    """The resolved stack context (env carries PODMAN_PROJECT / FOLDYARD_CHECKOUT / the
    derived mode env that the plugin box_args read: FY_PROXY, GCP_METADATA_HOST, DEVBOX_SA)."""
    return stack.resolve()


def _names(ctx) -> tuple[str, str]:
    return f"{ctx.project}-devbox", f"{ctx.project}_default"


def engine_env() -> tuple[str, dict]:
    """``(engine, env)`` for a one-off container run — no machine provisioning (``no_machine``), so a
    caller that only wants to run something in the yard doesn't create a VM to find out it can't.
    Used by ``worktree.init_config``, which runs the consumer's init script in a container instead of
    on the host."""
    ctx = stack.resolve(no_machine=True)
    return ctx.env.get("ENGINE") or config.engine(), ctx.env


def image_exists(engine: str, image: str, env: dict) -> bool:
    out = subprocess.run(
        [engine, "image", "inspect", image], env=env, capture_output=True, text=True
    )
    return out.returncode == 0


def _running(engine: str, box: str, env: dict) -> bool:
    out = subprocess.run(
        [engine, "ps", "-q", "-f", f"name=^{box}$", "-f", "status=running"],
        env=env,
        capture_output=True,
        text=True,
    )
    return bool(out.stdout.strip())


def _exists(engine: str, box: str, env: dict) -> bool:
    out = subprocess.run(
        [engine, "ps", "-aq", "-f", f"name=^{box}$"], env=env, capture_output=True, text=True
    )
    return bool(out.stdout.strip())


def _baked_env(engine: str, box: str, env: dict, key: str) -> str | None:
    """The value of env var ``key`` baked into the running box at create time, or None. Used to
    detect config that a running box can't pick up live (it's frozen in the container's env)."""
    out = subprocess.run(
        [engine, "inspect", box, "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
        env=env,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        name, sep, val = line.partition("=")
        if sep and name == key:
            return val
    return None


def _warn_stale_proxy_port(engine: str, box: str, env: dict) -> None:
    """A box created before its project's port band moved (foldyard upgrade that introduced bands,
    or a registry re-allocation) has ``FY_PROXY_PORT`` baked at the OLD base, while the host
    supervisor now serves the proxy at the NEW base — so the box's egress (incl. keyless agent
    auth) connection-refuses with nothing linking it to the move. Env can't change in a running
    box, so nag to recreate. Best-effort: silent if we can't read the baked value."""
    baked = _baked_env(engine, box, env, "FY_PROXY_PORT")
    if baked is None:
        return
    try:
        current = config.proxy_port_base()
    except (OSError, RuntimeError, ValueError):  # a nag must never crash `fy box up`
        return
    if baked.strip() and baked.strip() != str(current):
        print(
            f"⚠ dev box {box} was built for proxy port {baked.strip()}, but this project's proxy "
            f"now serves :{current} (its port band moved). The box's egress — including keyless "
            f"agent auth — will connection-refuse until you recreate it: `fy box down && fy box up`."
        )


def _box_image_fingerprint(main: Path) -> str:
    """Fingerprint the inputs Foldyard owns for a dev-box image.

    A tag only says an image exists; it does not say it was built from the current Dockerfile.
    Track the Dockerfile plus configured build shape so ``box up`` can reject an obsolete cached
    image. Consumer files copied from the wider build context remain the consumer's responsibility
    (``fy box build`` explicitly rebuilds those).
    """
    img = config.box_image()
    configured_context = img.get("context")
    context = Path(configured_context) if configured_context else main
    if not context.is_absolute():
        context = main / context
    dockerfile = Path(img["dockerfile"])
    if not dockerfile.is_absolute():
        dockerfile = context / dockerfile
    payload = {
        "build_args": img.get("build_args") or {},
        "context": str(configured_context or "."),
        "dockerfile_sha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest(),
        "target": img.get("target"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fingerprint(engine: str, kind: str, target: str, env: dict) -> str | None:
    """Read Foldyard's build fingerprint from an image or a running container."""
    cmd = [engine]
    if kind == "image":
        cmd.append("image")
    cmd += [
        "inspect",
        target,
        "--format",
        f'{{{{ index .Config.Labels "{_BOX_FINGERPRINT_LABEL}" }}}}',
    ]
    out = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value if value and value != "<no value>" else None


def _attach_hint(worktree: str) -> str:
    return f"WORKTREE={worktree} fy box shell" if worktree else "fy box shell"


def _has_compose(checkout: str) -> bool:
    """Does this project ship a compose stack? When it doesn't, `compose up` owns no
    `{project}_default` network, so `box up` must create it itself — see
    `stack.ensure_network`. Shared with `stack.up` via `config.has_compose_stack`."""
    return config.has_compose_stack(Path(checkout))


def _build_img(engine: str, main: Path, env: dict) -> int:
    img = config.box_image()
    fingerprint = _box_image_fingerprint(main)
    dockerfile = img["dockerfile"]
    # Build context: a consumer dockerfile builds against the repo root; the packaged generic
    # box carries its own `context` (the packaged assets dir) so it doesn't pull in the repo.
    context = img.get("context") or str(main)
    print(f"▶ building dev box image {img['tag']} (from {dockerfile})…")
    cmd = [engine, "build"]
    # podman needs --format docker so the Dockerfile's SHELL [...] directive is honoured.
    if engine == "podman":
        cmd += ["--format", "docker"]
    cmd += ["--label", f"{_BOX_FINGERPRINT_LABEL}={fingerprint}"]
    if img.get("target"):
        cmd += ["--target", str(img["target"])]
    for key, value in (img.get("build_args") or {}).items():
        cmd += ["--build-arg", f"{key}={value}"]
    cmd += ["-f", dockerfile, "-t", img["tag"], context]
    _echo(cmd)
    return subprocess.run(cmd, env=env, cwd=context).returncode


def _box_home(engine: str, img: str, env: dict) -> tuple[str, str]:
    """Where the image puts HOME + where claude resolves its config (CLAUDE_CONFIG_DIR or
    ~/.claude). The image bakes HOME=/home/vscode (its `vscode` user) and `--user 0` KEEPS
    that ENV, so even as root ~/.claude is /home/vscode/.claude — mount THERE, not /root."""
    out = subprocess.run(
        [
            engine,
            "run",
            "--rm",
            img,
            "sh",
            "-c",
            'printf "%s %s" "$HOME" "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"',
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    parts = out.stdout.split()
    home = parts[0] if len(parts) >= 1 and parts[0] else "/home/vscode"
    claude = parts[1] if len(parts) >= 2 and parts[1] else f"{home}/.claude"
    return home, claude


# The base bootstrap scaffolding (runs once per fresh box). The native Claude installer (now the
# [claude] plugin's box_bootstrap) hardcodes ~/.local/bin/claude, so that dir must beat
# /opt/fy-tools/bin on PATH; an npm install would pin the stale prefix.
# Trust the egress-proxy MITM CA SYSTEM-WIDE when a proxy/capture mode mounted it (the box's
# box_args `-v …:/etc/dev-proxy-ca.pem`) — so curl, wget, apt, etc. verify proxied HTTPS, not just
# the runtimes that read NODE_EXTRA_CA_CERTS/REQUESTS_CA_BUNDLE/GIT_SSL_CAINFO. Additive (adds to
# the system roots), and handles both Debian/Ubuntu (update-ca-certificates) and Fedora/RHEL
# (update-ca-trust) bases. The box is recreated per `up`, so a box with no CA mounted simply leaves
# a clean store. A standalone snippet (prepended to _BASE_SCRIPT) so the capture e2e can run it.
#
# It ALSO builds the COMBINED bundle (/etc/dev-proxy-ca-combined.pem = system roots + mitm CA) that
# box_args points REQUESTS_CA_BUNDLE/GIT_SSL_CAINFO at. Phase A′ always-routes, and capture=off
# TLS-passthrough leaves un-decrypted hosts presenting their REAL certs end-to-end — a mitm-only
# bundle would reject those. Built AFTER the system-trust install so the source bundle is fresh.
_CA_TRUST_SNIPPET = r"""
if [ -r /etc/dev-proxy-ca.pem ]; then
  if command -v update-ca-certificates >/dev/null; then          # Debian / Ubuntu
    cp /etc/dev-proxy-ca.pem /usr/local/share/ca-certificates/dev-proxy-ca.crt 2>/dev/null \
      && update-ca-certificates >/dev/null 2>&1 || echo "(dev-proxy CA system-trust install failed)"
  elif command -v update-ca-trust >/dev/null; then               # Fedora / RHEL
    cp /etc/dev-proxy-ca.pem /etc/pki/ca-trust/source/anchors/dev-proxy-ca.crt 2>/dev/null \
      && update-ca-trust >/dev/null 2>&1 || echo "(dev-proxy CA system-trust install failed)"
  fi
  combined=/etc/dev-proxy-ca-combined.pem
  for sys in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
    [ -r "$sys" ] && cat "$sys" /etc/dev-proxy-ca.pem > "$combined" 2>/dev/null && break
  done
  [ -r "$combined" ] || cp /etc/dev-proxy-ca.pem "$combined" 2>/dev/null || true   # no system bundle → mitm-only
fi
"""

# The PATH prepend, made idempotent. Four sites prepend the same two dirs — this snippet, the line
# it writes into ~/.bashrc, the `box shell` wrapper, and (outside foldyard) the image's own
# ~/.bashrc — and every one of them runs AGAIN in a nested shell: `box shell` execs `bash -l`, which
# re-reads ~/.profile → ~/.bashrc on top of what the wrapper just exported. None of them looked at
# what $PATH already held, so a box shell accumulated the same directories over and over (measured
# in a live box: 19 entries, 10 distinct). Resolution was never wrong — the first hit wins — but
# `which -a claude` then prints ONE install six times, which is precisely the signature of a
# leftover npm/`~/.claude/local` install, in the situation where you're hunting for exactly that.
#
# REMOVE-then-prepend, not skip-if-present: the ordering is load-bearing (see below — ~/.local/bin
# must beat /opt/fy-tools/bin), and skip-if-present would silently KEEP a parent shell's wrong
# order, quietly giving up the property that comment exists to protect. This is idempotent AND
# order-restoring. The substitution loops to fixpoint because one pass can't remove adjacent
# duplicates (`:a:a:` → `:a:`, non-overlapping matches). bash-only (`${p//…}`, `local`) — every
# site runs the script under `bash -lc`.
#
# Pre-existing boxes keep the old un-guarded line until they're recreated (~/.bashrc is on the
# container layer, so `fy up` regenerates it): harmless, because the block appended here runs
# after it and dedupes what it added.
_PATH_PREPEND_SNIPPET = r"""
fy_path_prepend() {   # drop existing occurrences, then prepend — idempotent, order-restoring
  local d p
  for d; do
    p=":$PATH:"
    while [ "${p//:$d:/:}" != "$p" ]; do p="${p//:$d:/:}"; done
    p="${p#:}"; p="${p%:}"
    PATH="$d${p:+:$p}"
  done
  export PATH
}
fy_path_prepend "/opt/fy-tools/bin" "$HOME/.local/bin"
"""

# Base scaffolding the bootstrap runs FIRST (idempotent shell config, not an install): CA
# system-trust, the PATH + persisted-history ~/.bashrc edits, and the `run_step` helper that the
# MONITORED install steps (foldyard + plugins + consumer [[box.tools]]) call — so a failed step is
# REPORTED, not silently swallowed (the old `|| echo "(… failed)"`). run_step args: label · check
# (a shell guard; skip when it succeeds — empty = always run) · run (the install command).
_BASE_SCRIPT = (
    _CA_TRUST_SNIPPET
    + r"""
grep -q "fy_path_prepend" ~/.bashrc 2>/dev/null || cat >> ~/.bashrc <<'BASHPATH'"""
    + _PATH_PREPEND_SNIPPET
    + r"""BASHPATH
"""
    + _PATH_PREPEND_SNIPPET
    + r"""
# Persist bash history: the box is recreated per `up`, so $HOME (incl. ~/.bash_history) is
# ephemeral — point HISTFILE into ~/.devbox, an UNCONDITIONAL named volume (devbox_shell) mounted
# for every box. (It used to live in ~/.claude because that happened to be the only persisted
# volume — which silently lost history on any box without [claude], and put shell state in an
# agent's config dir.) Stays in-box: the volume is never bound out to the Mac. `history -a`
# flushes after each command so the multiple sessions sharing this box don't clobber each
# other's history on exit (default histappend only flushes whole-session on logout).
grep -q "devbox: persisted bash history" ~/.bashrc 2>/dev/null || cat >> ~/.bashrc <<'BASHHIST'
# devbox: persisted bash history (survives box recreation via the devbox_shell volume)
# One-time migration from the pre-devbox_shell location (only if the claude volume is mounted).
[ -f "$HOME/.claude/.bash_history" ] && [ ! -f "$HOME/.devbox/.bash_history" ] && cp "$HOME/.claude/.bash_history" "$HOME/.devbox/.bash_history" 2>/dev/null
export HISTFILE="$HOME/.devbox/.bash_history"
export HISTSIZE=100000
export HISTFILESIZE=200000
shopt -s histappend
case "$PROMPT_COMMAND" in
  *'history -a'*) ;;
  *) PROMPT_COMMAND="history -a${PROMPT_COMMAND:+; $PROMPT_COMMAND}" ;;
esac
BASHHIST
# A Python for box-side scripting on ANY image. The box contract (ADR-0014) promises git + uv + an
# engine client — NEVER python: the packaged image is uv-first by design (debian:trixie-slim has no
# python3 at all), so a bare `python3` in a bootstrap step is the same defect class as a bare `npm`.
# uv's managed interpreter is already on disk by the time steps run — the `foldyard CLI` step
# provisions one — and `uv run` reaches it offline in ~15ms. `--no-project` so the checkout's own
# pyproject (the bootstrap runs with cwd=checkout) can never pull a project sync into a box step.
fy_python() {
  if command -v python3 >/dev/null 2>&1; then python3 "$@"; else uv run --no-project --quiet python "$@"; fi
}
_FY_BOOTSTRAP_FAILS=""
run_step() {  # label · check · run
  if [ -n "$2" ] && eval "$2" >/dev/null 2>&1; then printf '⏭ %s (present)\n' "$1"; return 0; fi
  printf '▶ %s…\n' "$1"
  if eval "$3"; then printf '✓ %s\n' "$1"; else
    printf '✗ %s (see output above)\n' "$1"; _FY_BOOTSTRAP_FAILS="$_FY_BOOTSTRAP_FAILS $1"
  fi
}
"""
)


def _warmup_script(checkout: str) -> str:
    """Background dep warm-up from `[box].warmup` — FROZEN/LOCKED installs into the shadow
    volumes. MANDATORY frozen: the lockfiles live on the host bind mount, so a plain install
    that rewrote them would write the host tree as root (the uid clash these volumes avoid)."""
    lines = [
        "exec >>/tmp/devbox-deps.log 2>&1",
        'echo "=== deps warm started $(date -u +%FT%TZ) ==="',
    ]
    for step in config.box_warmup():
        d, run = step["dir"], step["run"]
        lines.append(f'echo "--- {run} ({checkout}/{d}) ---"')
        lines.append(
            f'(cd "{checkout}/{d}" && {run}) || echo "({run} failed in {d} — check egress/lockfile)"'
        )
    lines.append('echo "=== deps warm done $(date -u +%FT%TZ) ==="')
    return "\n".join(lines)


def stage_foldyard_for_box(checkout: str, here: str, src: Path) -> Path | None:
    """Build a foldyard wheel from the host source into a VM-visible dir under the checkout.

    For a consumer repo that does NOT vendor foldyard (e.g. homelab), the box can't
    ``uv tool install --editable {checkout}/foldyard`` — there's no such dir on the mount, and
    the machine virtiofs-mounts ONLY the repo + worktrees, so the host's foldyard source under
    ``~/.local/...`` is invisible to podman inside the VM. So when the Mac's foldyard is an
    EDITABLE install, build a wheel from its source into a gitignored ``.devbox-foldyard/`` under
    the checkout (which IS mounted) and hand the box that wheel path — the proxy-CA staging trick.
    The wheel pins the box to the exact host (WIP) version. Returns the staged wheel, or ``None``
    if the build fails — there is no fallback: the caller flags ``fy_stage_failed`` and the
    bootstrap's foldyard install FAILS CLOSED rather than drifting off an unpinned version."""
    dest = Path(checkout) / here / ".devbox-foldyard"
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.whl"):  # one wheel only — a stale one would make the install ambiguous
        old.unlink()
    r = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dest), str(src)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        # The TAIL, whole lines: uv reports resolution failures as a `×/├─▶/╰─▶` pyramid whose
        # LAST line is the root cause ("network was disabled", "tunnel error: unsuccessful") —
        # a char-count truncation kept the symptom and cut exactly the line that explains it.
        _err("  ✗ foldyard wheel build failed — the box's `fy` install will fail closed:")
        for line in r.stderr.strip().splitlines()[-12:]:
            _err(f"      {line}")
        proxies = [
            k
            for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY")
            if os.environ.get(k)
        ]
        if proxies:
            # The one real failure seen so far: a debugging shell exporting the egress proxy,
            # which under default_deny refuses pypi.org — so `uv build` can't fetch hatchling.
            _err(
                f"      (this shell exports {', '.join(proxies)} — the build fetches its build "
                "backend from PyPI through it; `unset` them or allow pypi.org)"
            )
        return None
    wheels = sorted(dest.glob("*.whl"))
    return wheels[-1] if wheels else None


def _foldyard_install_subst(checkout: str, here: str) -> dict[str, str]:
    """How the box should install foldyard, resolved host-side by how the Mac's foldyard is
    installed (the box image already ships ``uv``). Returns ``{fy_wheel, fy_version}`` placeholders
    the bootstrap's fallback chain reads. NEVER assumes an editable install:

    * repo VENDORS foldyard (``{checkout}/foldyard``) → both empty; the bootstrap installs editable
      from the mount (Tangible's live dogfood — picks up WIP source, no wheel rebuild needed);
    * else host is an EDITABLE dev install → build + stage a wheel (homelab: a Mac dev checkout
      driving a repo that doesn't vendor foldyard);
    * else host is a PUBLISHED install → pin the box to the host's version (``foldyard==X``)."""
    if (Path(checkout) / "foldyard" / "pyproject.toml").exists():
        return {"fy_wheel": "", "fy_version": ""}
    from .plugins import proxy  # lazy: keep the registry-load hot path import-light

    src = proxy._foldyard_src()
    if src is not None:
        wheel = stage_foldyard_for_box(checkout, here, src)
        if wheel is None:
            # There WAS source to install from and the build broke — flag it, so the box-side
            # fail-closed message points at the host build instead of claiming nothing existed.
            return {"fy_wheel": "", "fy_version": "", "fy_stage_failed": "1"}
        return {"fy_wheel": str(wheel), "fy_version": ""}
    try:
        return {"fy_wheel": "", "fy_version": metadata.version("foldyard")}
    except (
        Exception
    ):  # pragma: no cover — defensive; bootstrap falls back to `uv tool install foldyard`
        return {"fy_wheel": "", "fy_version": ""}


def _in_box() -> bool:
    """True when running INSIDE the dev box. The marker is explicit so a native Linux/WSL2 host
    with ``DOCKER_HOST`` set to its local podman socket is not mistaken for the box."""
    return os.environ.get("IN_DEVBOX") == "1"


def _claude_argv(prompt: str, settings: dict, args: list[str]) -> list[str]:
    """The `claude` argv: skip permission prompts (the box's rootless isolation IS the safety
    boundary) + the inline orientation prompt (when set) + the `[claude.settings]` overrides (when
    any), then the caller's extra args.

    `--settings` takes a file path OR a literal JSON string, and Claude MERGES what it finds into
    the settings hierarchy — so the table overrides per key rather than replacing the box's
    `~/.claude/settings.json`. It goes in BEFORE the caller's args so an explicit
    `fy claude --settings …` still wins."""
    argv = ["claude", "--verbose", "--dangerously-skip-permissions"]
    if prompt.strip():
        argv += ["--append-system-prompt", prompt]
    if settings:
        # default=str: TOML has date/time types JSON doesn't, and a settings value that can't be
        # encoded must not take down the launcher.
        argv += ["--settings", json.dumps(settings, default=str)]
    return argv + args


def claude(args: list[str] | None = None) -> int:
    """`fy claude` — run Claude Code in the dev box, pre-oriented + skipping permission prompts
    . Refuses outside the box; needs `[claude]` in
     foldyard.toml (its plugin installs claude + mounts its volumes)."""
    if not _in_box():
        _err("✗ fy claude runs INSIDE the dev box ('fy box shell' first).")
        _err("  It skips permission prompts and trusts the rootless-machine boundary.")
        return 1
    if not shutil.which("claude"):
        _err("✗ claude not on PATH — add a [claude] table to foldyard.toml, then `fy box up`.")
        return 1
    argv = _claude_argv(config.claude_system_prompt(), config.claude_settings(), args or [])
    os.execvpe(argv[0], argv, os.environ.copy())


def _toml_value(value: object) -> str:
    """One `[codex.config]` value as a TOML literal, because `codex -c key=value` parses the value
    AS TOML and silently falls back to a raw literal STRING when that parse fails — the same trap
    `_codex_argv` closes for the prompt, reached through every other key.

    JSON syntax is TOML syntax for strings, integers, floats and arrays, so `json.dumps` carries
    those. What it can't: booleans (`True`), inline tables (`:` vs `=`), non-finite floats
    (`Infinity`) and TOML's date/time types, which have no JSON form at all but whose `str()` IS
    their TOML spelling."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and (value != value or value in (_INF, -_INF)):
        return "nan" if value != value else ("inf" if value > 0 else "-inf")
    if isinstance(value, (int, float, str)):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):  # only reachable INSIDE an array — a table at any key flattens
        return (
            "{ "
            + ", ".join(f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items())
            + " }"
        )
    return str(value)  # date / time / datetime


def _toml_key(key: str) -> str:
    """A dotted-path segment, quoted only when it isn't a TOML bare key — so the common case stays
    `tui.raw_output_mode` and an exotic one still reaches codex as the key it was written as."""
    return key if _BARE_KEY.fullmatch(key) else json.dumps(key)


def _codex_overrides(table: dict, prefix: str = "") -> list[str]:
    """`[codex.config]` as `-c key=value` payloads, one per LEAF.

    Nested tables FLATTEN to Codex's dotted paths (`tui.raw_output_mode=false`) rather than being
    emitted as one inline table, because `-c tui={ … }` would REPLACE the whole `tui` table in the
    box's `~/.codex/config.toml` while the dotted path overrides only the leaf — the difference
    between "set one knob" and "drop every other knob under it"."""
    out: list[str] = []
    for key, value in table.items():
        path = f"{prefix}{_toml_key(str(key))}"
        if isinstance(value, dict):
            out += _codex_overrides(value, f"{path}.")
        else:
            out.append(f"{path}={_toml_value(value)}")
    return out


def _codex_argv(prompt: str, overrides: dict, args: list[str]) -> list[str]:
    """The `codex` argv: bypass approvals + its own sandbox (the box's rootless isolation IS the
    safety boundary) + the inline orientation prompt (when set) + the `[codex.config]` overrides
    (when any), then the caller's extra args.

    Codex has no `--append-system-prompt`. Its equivalent is the `developer_instructions` config
    key, delivered per-invocation with `-c`: it adds one more item to the developer message the
    model already receives (measured against `codex debug prompt-input` — one item added, none
    replaced), which is what makes it the analogue of Claude's flag. Deliberately NOT
    `base_instructions`, which REPLACES Codex's own base prompt rather than adding to it.

    The value is emitted as a QUOTED TOML string (json.dumps — JSON string syntax is valid TOML
    basic-string syntax, newlines included as `\\n` escapes), because `-c` parses the value as TOML
    and only falls back to a literal when that fails. A raw prompt therefore gets silently mangled
    whenever it happens to parse: `Be terse. "Ship it"` loses its final quote, and a prompt of
    `true` is a hard error ("invalid type: boolean"). Both verified against codex-cli 0.150.1.

    The overrides land AFTER the prompt and BEFORE the caller's args, because a repeated `-c` on
    the same key is LAST-WINS (verified with `codex debug prompt-input`, 0.150.1): so an explicit
    `[codex.config] developer_instructions` deliberately beats `system_prompt`, and a
    `fy codex -c …` on the command line beats both."""
    argv = ["codex", "--dangerously-bypass-approvals-and-sandbox"]
    if prompt.strip():
        argv += ["-c", f"developer_instructions={json.dumps(prompt)}"]
    for override in _codex_overrides(overrides):
        argv += ["-c", override]
    return argv + args


def codex(args: list[str] | None = None) -> int:
    """`fy codex` — run the OpenAI Codex CLI in the dev box, pre-oriented, bypassing approvals + its
    own sandbox (the rootless box IS the boundary; verified flag
    `--dangerously-bypass-approvals-and-sandbox`). Refuses outside the box; needs `[codex]` in
    foldyard.toml (its plugin installs codex)."""
    if not _in_box():
        _err("✗ fy codex runs INSIDE the dev box ('fy box shell' first).")
        _err("  It bypasses approvals and trusts the rootless-machine boundary.")
        return 1
    if not shutil.which("codex"):
        _err("✗ codex not on PATH — add a [codex] table to foldyard.toml, then `fy box up`.")
        return 1
    argv = _codex_argv(config.codex_system_prompt(), config.codex_config(), args or [])
    os.execvpe(argv[0], argv, os.environ.copy())


def _step_line(label: str, check: str, run: str) -> str:
    """One monitored step as a ``run_step`` call, shell-quoted so an arbitrary install command
    (pipes, quotes, newlines) survives intact into the box's bash, where ``run_step`` ``eval``s it."""
    return f"run_step {shlex.quote(label)} {shlex.quote(check)} {shlex.quote(run)}"


def _foldyard_run(checkout: str, subst: dict[str, str]) -> str:
    """The foldyard self-install command (Item-1 fallback chain) with the host-resolved wheel /
    version baked in; the in-box ``[ -d …/foldyard ]`` branch covers the vendored-repo case.

    Every branch installs foldyard BARE — never the ``[host]`` extra — because the box only ROUTES
    egress through the Mac's mitmdump proxy, it never runs mitmproxy itself. That keeps the box light
    and, crucially, shrinks its bootstrap egress: no cryptography/mitmproxy wheels to pull through the
    proxy just to get `fy` on PATH (mitmproxy is the host's `just foldyard install` `[host]` extra)."""
    wheel, version = subst.get("fy_wheel", ""), subst.get("fy_version", "")
    repo = shlex.quote(f"{checkout}/foldyard")
    # Fail closed rather than `uv tool install foldyard` unpinned: an unpinned install would
    # silently drift from the tested version. But say WHY there was nothing to pick — "no wheel,
    # pinned version, or vendored source" sent an operator hunting for missing source when the
    # actual failure was the host-side wheel build, reported minutes earlier and scrolled away.
    if subst.get("fy_stage_failed"):
        why = (
            "the host-side foldyard wheel build FAILED (see the `fy box up` output on the Mac) "
            "— fix that and re-run `fy box up`"
        )
    else:
        why = (
            "no wheel, pinned version, or vendored source — cannot pick a tested version to install"
        )
    return (
        f"if [ -n {shlex.quote(wheel)} ] && [ -f {shlex.quote(wheel)} ]; then "
        f"uv tool install --force {shlex.quote(wheel)}; "
        f'elif [ -n {shlex.quote(version)} ]; then uv tool install "foldyard=={version}"; '
        f"elif [ -d {repo} ]; then uv tool install --editable {repo}; "
        f"else echo {shlex.quote(f'✗ foldyard: {why}')} >&2; exit 1; fi"
    )


def _git_shim_step() -> tuple[str, str, str] | None:
    """The git index-split shim as a monitored bootstrap step (``[box].git_index_split``,
    default on): install the packaged ``assets/box/git-index-shim.sh`` to ``/usr/local/bin/git``
    so every PATH-resolved git in the box (agents, recipes, the VS Code server) writes
    ``<gitdir>/index-box`` instead of racing host-side git on the shared checkout's index
    (docs/adrs/0021-per-kernel-git-index-split.md). Runtime install — works on ANY consumer image, no
    Dockerfile required — and failure-tolerant by construction: a ✗ step (read-only bin dir,
    exotic image) just leaves the box on today's shared-index behaviour."""
    if not config.box_git_index_split():
        return None
    shim = (Path(__file__).resolve().parent / "assets" / "box" / "git-index-shim.sh").read_text()
    run = (
        "cat > /usr/local/bin/git <<'FY_GIT_SHIM_EOF'\n"
        f"{shim}"
        "FY_GIT_SHIM_EOF\n"
        "chmod 755 /usr/local/bin/git"
    )
    return ("git index shim (shared-checkout split)", "", run)


def _bootstrap_script(checkout: str, here: str, env: dict) -> str:
    """The full one-time box bootstrap: base scaffolding + the monitored install steps (core
    foldyard + Claude, then each enabled plugin's, then the consumer's ``[[box.tools]]`` and
    free-form ``[box].bootstrap``), then a failure summary. Each step is reported ✓/⏭/✗."""
    subst = _foldyard_install_subst(checkout, here)
    steps: list[tuple[str, str, str]] = []
    if shim := _git_shim_step():  # first, so the rest of the bootstrap's git is already split
        steps.append(shim)
    steps += [
        # Core: foldyard self-install. Claude/editor installs come from their gated plugins below.
        ("foldyard CLI", "command -v foldyard", _foldyard_run(checkout, subst)),
    ]
    steps += [(s["label"], s.get("check", ""), s["run"]) for s in registry().box_bootstrap(env)]
    for tool in config.box_tools():  # consumer toolchain (e.g. pulumi) — out of the package
        steps.append(
            (tool["name"], tool.get("check") or f"command -v {tool['name']}", tool["install"])
        )

    lines = [_BASE_SCRIPT]
    if config.box_clean_docker_config():
        # Seed the clean DOCKER_CONFIG dir the run env points at (see _up's env_args) — kept
        # out of ~/.docker so an editor attach can't re-inject its root-broken credsStore.
        lines.append(
            'mkdir -p "$HOME/.docker-fy" && '
            '{ [ -f "$HOME/.docker-fy/config.json" ] || printf "{}" > "$HOME/.docker-fy/config.json"; }'
        )
    lines += [_step_line(*s) for s in steps]
    custom = config.box_bootstrap()
    if custom.strip():
        lines.append(_step_line("custom bootstrap", "", custom))
    lines.append(
        '[ -n "$_FY_BOOTSTRAP_FAILS" ] '
        '&& printf "⚠ bootstrap: step(s) failed:%s\\n" "$_FY_BOOTSTRAP_FAILS" || true'
    )
    return "\n".join(lines)


def _motd(box: str) -> str:
    return f"""
      ┌─ dev box ({box}) ─────────────────────────────────────────────────────
      │  Root in the dev image, on the rootless machine.
      │  DOCKER_HOST → the machine engine (bounded: repo + this machine only).
      │  This is one of possibly several sessions sharing this box + checkout.
      │
      │    fy claude                   # start the agent, pre-oriented (skips prompts)
      │    fy ps | logs | e2e | verify
      │
      │  Orientation: foldyard/README.md + DEVELOPMENT.md + docs/
      └──────────────────────────────────────────────────────────────────────
"""


def _capture_keyless() -> None:
    """Mac-side, before box-up: for each agent whose ``keyless`` is on, make sure the real key/token
    is in host.env — prompting once on a TTY (classified by prefix), warning (not blocking) without
    one. No-op when no keyless is configured. The box never sees the secret; only the host.env var
    the proxy's minter reads. See :mod:`foldyard.keyless`."""
    # Codex ChatGPT mode's credential is the Mac's ~/.codex/auth.json (refreshed host-side), NOT a
    # host.env var — so it's not prompted for; just confirm it's there (warn, don't block).
    if config.codex_keyless() == "chatgpt":
        path = keyless.codex_auth_json_path()
        if keyless.codex_account_id(path):
            _err(
                f"✓ keyless Codex (ChatGPT): using {path} on the Mac (refreshed host-side, never in the box)."
            )
        else:
            _err(
                f"⚠ keyless Codex (ChatGPT) is on but no usable {path} — run `codex login` (ChatGPT) on the Mac first."
            )

    agents = (
        ("Claude", config.claude_keyless(), keyless.CLAUDE_KEYLESS),
        ("Codex", config.codex_keyless(), keyless.CODEX_KEYLESS),
    )
    for provider, mode, taxonomy in agents:
        if not mode or mode == "chatgpt":  # chatgpt handled above (auth.json, not host.env)
            continue
        spec = taxonomy.get(mode)
        if not spec:  # a mode with no host.env var (shouldn't happen for the api-key shapes)
            _err(f"⚠ {provider} keyless = {mode!r} isn't supported yet — no credential captured.")
            continue
        keyless.ensure_cred(
            config.host_env_file(),
            spec["env"],
            f"{provider} {mode} credential",
            interactive=sys.stdin.isatty(),
            # getpass hides the paste (no terminal echo / scrollback) — a real secret.
            prompt=getpass.getpass,
            echo=_err,
            how=spec.get("how", ""),
        )


def _warn_keyless_axis_at_rest() -> None:
    """Host-side, before box-up: a keyless agent whose posture axis is still at rest.

    This is the last step of the scaffold's step 2 ("uncomment ONE agent, then `fy box up`") and the
    easiest to skip, because nothing else looks wrong: declaring ``keyless`` is what CREATES the
    axis (``ClaudePlugin.axes`` returns nothing without it) but it does not ARM it, so the box comes
    up with its install, its volumes and its DUMMY credential — and then the agent 401s against an
    otherwise perfect box. The same failure the comment above ``_capture_keyless`` describes,
    reached from the other side: there the host had no token, here the posture won't let the proxy
    inject the one it has.

    Said HERE and not from ``fy mode``, deliberately. An axis at its default rung is the PRODUCT —
    secretless-by-default, armed for a session and TTL'd back down — so warning on every posture
    read would be nagging at a resting posture, and would be tuned out by the time it mattered. A
    session-starting verb is where "you are about to use this agent" is actually true.
    """
    from . import devmode

    declared = [
        (axis, table, kind)
        for axis, table, kind in (
            ("claude", "[claude]", config.claude_keyless()),
            ("codex", "[codex]", config.codex_keyless()),
        )
        if kind
    ]
    if not declared:  # no keyless ⇒ no axis to be off (a bare [claude] logs in inside the box)
        return
    try:
        mode = devmode.read(apply_expiry=True)["mode"]
    except Exception as e:  # pragma: no cover — a corrupt/absent mode file must not block box-up
        _err(f"⚠ couldn't read the posture to check the agent axes ({e}) — skipping.")
        return
    rungs, defaults = devmode.axes(), devmode.axis_defaults()
    for axis, table, kind in declared:
        rest = defaults.get(axis)
        if rest is None or mode.get(axis, rest) != rest:
            continue
        # The rung to suggest comes from the registry, not a literal "on": the axis belongs to the
        # plugin, and this message must not be the thing that goes stale if its rungs change.
        arm = next((r for r in rungs.get(axis, ()) if r != rest), "on")
        _err(
            f"⚠ {table}.keyless = {kind!r} is declared but the posture has {axis}={rest} — the box "
            f"gets only a dummy credential, so the agent cannot reach its API.\n"
            f"  Arm it: `fy mode {axis}={arm}`  (or `fy tui`, where one keypress flips it)."
        )


def _capture_secrets() -> None:
    """Mac-side, before box-up: for every secret the CURRENT posture declares it needs (plugin
    ``secrets`` hooks + the consumer's ``[[secret]]`` rows), make sure it's in host.env — prompting
    once on a TTY, warning (never blocking) without one. This is the declarative replacement for a
    consumer script that fetched the secret from a vault: foldyard checks presence and echoes the
    declared ``how`` hint for the human to run, and never executes it. See
    :class:`foldyard.plugins.Secret`."""
    from . import devmode

    try:
        mode = devmode.read(apply_expiry=True)["mode"]
    except Exception as e:  # pragma: no cover — a corrupt/absent mode file must not block box-up
        _err(f"⚠ couldn't read the posture to check declared secrets ({e}) — skipping.")
        return
    for secret in registry().secrets(mode):
        keyless.ensure_secret(
            config.host_env_file(),
            secret,
            interactive=sys.stdin.isatty(),
            # getpass hides the paste (no terminal echo / scrollback) — a real secret.
            prompt=getpass.getpass,
            echo=_err,
        )


def _up(ctx, engine: str, box: str, net: str) -> int:
    env = ctx.env
    img = config.box_image()["tag"]
    main = ctx.main
    checkout = env["FOLDYARD_CHECKOUT"]
    here = env.get("HERE") or config.dev_vm_rel()
    worktree = ctx.worktree

    # The box ALWAYS routes its egress through the host supervisor's always-on proxy (it's where
    # keyless agent-auth injection happens), so make sure that supervisor is up — launch it detached
    # if nothing's serving. `fy up` does this too, but a box-only flow (`fy box up` / `fy claude`
    # without a prior `fy up`) never would, leaving the box pointed at a dead :8088 even with axes
    # like penpot/capture marked "on" (a stored posture is NOT a running daemon). Idempotent +
    # Mac-only (a no-op in the box, or when a supervisor already serves its daemons). Done before the
    # early-return so even an already-up box re-checks, and before the slow build so the proxy + CA
    # come up in parallel.
    from . import supervisor

    supervisor.ensure_background()

    # Declared `[[secret]]` capture runs BEFORE the already-up early-return: a secret is read by
    # the HOST minter at mint time, so it matters whether the posture needs it, not whether the box
    # was just created. Turning `github=app` on with a box already running must still prompt for the
    # PEM. Keyless agent creds are captured here too, for the same reason — the REAL token feeds the
    # host proxy's minter, not the container (only the DUMMY is baked at create time — nagged in the
    # already-up branch below). Keeping them below the return "because the dummy is create-time" is
    # how declaring [claude] on a running box got no prompt until `fy box down`, while the proxy
    # injected nothing and the box 401'd for days.
    _capture_secrets()
    _capture_keyless()
    # Above the early return for the same reason: arming an axis is host-side and needs no recreate,
    # so an already-up box is exactly where this is most likely to be the one thing still missing.
    _warn_keyless_axis_at_rest()

    expected_fingerprint = _box_image_fingerprint(main)

    if _running(engine, box, env):
        _warn_stale_proxy_port(engine, box, env)  # a pre-band box strands on a dead proxy port
        if _fingerprint(engine, "container", box, env) != expected_fingerprint:
            print(
                f"⚠ dev box {box} was built from an older image definition. Recreate it to pick "
                "up the current tools: `fy box down && fy box up`."
            )
        # Config drift a running box CANNOT pick up live: the Claude install, volumes and dummy
        # credential are all create-time (claude.py box_args/box_bootstrap), so a box created
        # before [claude] was declared stays shell-only however often `fy box up` reruns. A box
        # created WITH [claude] bakes CLAUDE_CONFIG_DIR — its absence is the drift signal.
        if config.claude_enabled() and _baked_env(engine, box, env, "CLAUDE_CONFIG_DIR") is None:
            print(
                "⚠ [claude] is declared but this box was created without it (no Claude install, "
                "volumes or dummy credential). Recreate to add them: `fy box down && fy box up`."
            )
        # Same drift, same signal, for codex: a box created WITH [codex] bakes CODEX_HOME. Without
        # this row the failure is SILENT and reads as an auth bug — the box has no codex, `fy codex`
        # says "add a [codex] table" (which you already did), and nothing points at the recreate.
        if config.codex_enabled() and _baked_env(engine, box, env, "CODEX_HOME") is None:
            print(
                "⚠ [codex] is declared but this box was created without it (no Codex install, "
                "~/.codex volume or keyless seed). Recreate to add them: `fy box down && fy box up`."
            )
        print(f"✓ dev box {box} already up. Attach: {_attach_hint(worktree)}")
        return 0
    if _exists(engine, box, env):
        subprocess.run(
            [engine, "rm", "-f", box], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    if _fingerprint(engine, "image", img, env) != expected_fingerprint:
        rc = _build_img(engine, main, env)
        if rc != 0:
            return rc

    transcripts_dir = (
        os.environ.get("DEVBOX_TRANSCRIPTS") or f"{checkout}/{here}/.devbox-claude/projects"
    )
    box_home, claude_in_box = _box_home(engine, img, env)
    # Resolved HOME paths the agent/editor plugins' box_args need (they only get `env`): the box
    # HOME, where Claude resolves ~/.claude, and the Mac-side transcripts dir to bind in.
    env["FY_BOX_HOME"] = box_home
    env["FY_CLAUDE_HOME"] = claude_in_box
    env["FY_TRANSCRIPTS"] = transcripts_dir
    env["FY_CODEX_TRANSCRIPTS"] = f"{checkout}/{here}/.devbox-codex/sessions"
    print(f"▶ starting long-lived dev box {box} (network {net})…")
    if config.claude_enabled():
        print(
            f"  claude home → {claude_in_box} (in box); transcripts → {transcripts_dir} (on the Mac; survives nuke)"
        )
    if config.codex_enabled():
        print(f"  codex sessions → {env['FY_CODEX_TRANSCRIPTS']} (on the Mac; survives nuke)")

    # Mount the MAIN repo root (a worktree's `.git` points into it) + the WORKTREES ROOT, so every
    # box sees every sibling checkout: an agent can read/edit a worktree copy (the machine already
    # mounts this root — it was only the container that couldn't see it), and `worktree init_config`
    # can run the consumer's init script in here instead of on the host. Cross-worktree reach is
    # within the existing trust tier (ADR-0004: worktrees are your own code at equal trust); posture
    # state stays host-side, so no box gains another worktree's credentials.
    mounts = ["-v", f"{main}:{main}"]
    wt_root = config.worktrees_root(main)
    if wt_root.is_dir() and not str(wt_root).startswith(f"{main}/"):
        mounts += ["-v", f"{wt_root}:{wt_root}"]
    if checkout != str(main) and not str(checkout).startswith(f"{wt_root}/"):
        mounts += ["-v", f"{checkout}:{checkout}"]

    # Shadow each in-tree build-artifact dir with a per-box named volume (uid-squash
    # workaround): the volume masks the host bind mount at that subpath.
    shadow_args = []
    for d in config.box_shadow_volumes():
        vn = f"{box}-shadow-" + d.replace("/", "-").replace(".", "-")
        shadow_args += ["-v", f"{vn}:{checkout}/{d}"]

    # Stage the proxy CA under the checkout (VM-visible) before box_args mounts it — the machine
    # mounts ONLY repo + worktrees, so a CA under ~/.mitmproxy can't be bind-mounted. AMBIENT and
    # UNCONDITIONAL: the proxy plugin's box_args mounts the CA whenever it EXISTS (the ambient-CA
    # rule — trust pre-positioned regardless of routing), so staging MUST be just as ambient. Gating
    # staging on proxy_enabled/GH_INJECT (as this once did) leaves MITMPROXY_CA at its Mac default
    # ~/.mitmproxy/… for a proxy-less box; box_args then ambient-mounts that NON-VM-visible host
    # path and `podman run` dies with `statfs …: no such file or directory`. stage_ca_for_box
    # no-ops (returns None) when no CA exists, so calling it always is safe for a truly CA-less box.
    from .plugins import proxy  # lazy: keep the registry-load hot path import-light

    proxy.stage_ca_for_box(checkout, here)

    # Credential box env/mounts from the plugins (github proxy CA+env; gcp metadata+SA label).
    try:
        plugin_args = registry().box_args(env)
    except SystemExit as e:
        _err(str(e))
        return 1

    # Core box volumes: the engine socket, the shared on-PATH tools prefix (foldyard + consumer
    # [[box.tools]]), and the configured caches. The agent/editor volumes (Claude config/native +
    # transcripts, vscode-server) come from the gated claude/vscode plugins' box_args above.
    agent_vols = [
        "-v",
        f"{config.box_sock_in_vm()}:/var/run/docker.sock",
        "-v",
        "devbox_tools:/opt/fy-tools",
        # Agent-neutral persisted shell state (bash history — see the _BASE_SCRIPT HISTFILE
        # block). Unconditional like devbox_tools, so a shell-only box keeps history too.
        "-v",
        f"devbox_shell:{box_home}/.devbox",
    ]
    for cache in config.box_caches():
        agent_vols += ["-v", f"{cache['volume']}:{box_home}/{cache['path'].lstrip('/')}"]

    env_args = [
        "-e",
        "DOCKER_HOST=unix:///var/run/docker.sock",
        "-e",
        "CONTAINER_HOST=unix:///var/run/docker.sock",
    ]
    if config.box_clean_docker_config():
        # Sidestep the editor-attach credsStore helper (255s under root, breaks even anonymous
        # compose pulls) — docker/compose read this foldyard-owned config instead of ~/.docker.
        # Seeded `{}` by the bootstrap; `docker login` writes here and still works.
        env_args += ["-e", f"DOCKER_CONFIG={box_home}/.docker-fy"]
    for key, value in config.box_env().items():
        env_args += ["-e", f"{key}={value.replace('~', box_home)}"]
    env_args += [
        "-e",
        f"FOLDYARD_CHECKOUT={checkout}",
        "-e",
        f"FOLDYARD_ENV_OVERRIDE={env.get('FOLDYARD_ENV_OVERRIDE', '')}",
        "-e",
        f"WORKTREE={worktree}",
        "-e",
        f"PODMAN_PROJECT={ctx.project}",
        "-e",
        f"COMPOSE_PROJECT_NAME={ctx.project}",
        # PIN the project's Mac daemon port bases (allocated band, or the env override) into the
        # box: in-box port derivations (`fy mode` daemon probes, FY_PROXY re-derivation) can't
        # read the Mac's ~/.foldyard/ports.json registry, and both env vars already win over
        # allocation (config._daemon_port_base), so the box always agrees with the host that
        # created it.
        "-e",
        f"FY_PROXY_PORT={config.proxy_port_base()}",
        "-e",
        f"GCP_MINTER_PORT={config.gcp_minter_port_base()}",
    ]
    for key in config.port_bases():  # passthrough -e KEY (value flows from env below)
        env_args += ["-e", key]
    env_args += [
        "-e",
        "IN_DEVBOX=1",
        "-e",
        "IS_SANDBOX=1",
        "-e",
        "DO_NOT_TRACK=1",
        "-e",
        "NEXT_TELEMETRY_DISABLED=1",
    ]

    # Who creates `{project}_default` before the box attaches? Stack-less project → nobody
    # else ever will; external_network consumer → foldyard owns it (compose declares it
    # external), which is what lets `box up` run before any `up`. Create it (no-op once it
    # exists). A compose-OWNED network is compose's to create — pre-creating it would lack
    # compose's labels and break the stack's first `up` — so a missing one means the stack
    # has never been up: say so instead of dying on podman's cryptic "network not found".
    if not _has_compose(checkout) or config.external_network():
        stack.ensure_network(engine, net, env)
    elif not stack.network_exists(engine, net, env):
        _err(f"✗ network {net} doesn't exist yet — it's created by the stack's first up:")
        _err("      foldyard up")
        _err("  (or set [project].external_network in foldyard.toml + declare the network")
        _err("  external in compose to let foldyard own it — then box up works standalone).")
        return 1

    run = [
        engine,
        "run",
        "-d",
        "--user",
        "0",
        "--security-opt",
        "label=disable",
        "--name",
        box,
        "--network",
        net,
        *mounts,
        *shadow_args,
        *plugin_args,
        "-w",
        checkout,
        *agent_vols,
        *env_args,
        img,
        "sleep",
        "infinity",
    ]
    _echo(run)
    if subprocess.run(run, env=env, stdout=subprocess.DEVNULL).returncode != 0:
        _err("✗ dev box create failed")
        return 1

    # One-time MONITORED install steps (core foldyard + Claude, plugins, consumer [[box.tools]]),
    # then warm deps in the background. Output streams through so each step's ✓/⏭/✗ is visible.
    # foldyard self-install resolves its source host-side (vendored repo → editable; else a staged
    # wheel from an editable Mac install; else a PyPI version pin) so `fy` works even in a repo
    # that doesn't vendor foldyard (e.g. homelab) — never assumes an editable checkout on the mount.
    subprocess.run(
        [engine, "exec", box, "bash", "-lc", _bootstrap_script(checkout, here, env)], env=env
    )
    if not os.environ.get("DEVBOX_SKIP_DEPS") and config.box_warmup():
        print("▶ warming deps in background (log: /tmp/devbox-deps.log inside the box)…")
        subprocess.run(
            [engine, "exec", "-d", box, "bash", "-lc", _warmup_script(checkout)], env=env
        )

    print("✓ up. Attach a session (repeatable for multiple sessions):")
    print(f"    {_attach_hint(worktree)}")
    return 0


def main(cmd: str = "shell", args: list[str] | None = None) -> int:
    if cmd == "up":  # fail fast on unmet host prereqs before _ctx() creates the machine (host-only)
        from . import preflight

        preflight.check_or_abort("fy box up")
    if cmd in ("down", "ps") and not stack.engine_reachable("stop" if cmd == "down" else "show"):
        # Read/teardown verbs must never provision the VM _ctx() would ensure. A `box down`
        # with no machine still clears the posture mirror below on the up path; with the VM
        # gone the box is gone too, so drop the mirror here as well.
        if cmd == "down":
            config.mirror_file().unlink(missing_ok=True)
        return 0
    ctx = _ctx()
    engine = config.engine()
    box, net = _names(ctx)
    env = ctx.env

    if cmd == "exec":
        return _exec(engine, box, ctx, env, args or [])
    if cmd == "build":
        rc = _build_img(engine, ctx.main, env)
        if rc == 0:
            print(f"✓ built {config.box_image()['tag']}. Start the box: fy box up")
        return rc
    if cmd == "up":
        return _up(ctx, engine, box, net)
    if cmd == "shell":
        if not _running(engine, box, env):
            hint = _attach_hint(ctx.worktree).replace("shell", "up")
            _err(f"dev box {box} not running — start it: {hint}")
            return 1
        # Same idempotent prepend the bootstrap uses, not a raw `PATH=…:$PATH` — this exec's
        # `bash -l` re-reads the rc files on top of whatever we set here, which is how the box's
        # PATH used to grow a fresh copy of both dirs on every attach.
        script = f'{_PATH_PREPEND_SNIPPET}\ncat <<"MOTD"\n{_motd(box)}\nMOTD\nexec bash -l'
        return subprocess.run(
            [engine, "exec", "-it", box, "bash", "-lc", script], env=env
        ).returncode
    if cmd == "down":
        # Belt-and-suspenders: promote this box's transcripts to the durable Mac store before
        # we drop the container. The bound-out dir survives `box down`, but archiving here keeps
        # the Mac's `claude --resume` in sync. Best-effort — never block a teardown. Lazy import
        # avoids a stack↔transcripts cycle.
        from . import transcripts

        transcripts.sync_current(env)
        if _exists(engine, box, env):
            subprocess.run([engine, "rm", "-f", box], env=env, stdout=subprocess.DEVNULL)
            print(f"✓ dev box {box} stopped (login/CLI volumes kept).")
        else:
            print(f"(no dev box {box})")
        # The posture mirror exists FOR box sessions; with the box gone it's just an untracked
        # file dirtying the checkout (release flows choke on it). `box up` + the supervisor
        # reseed it while a box is up.
        config.mirror_file().unlink(missing_ok=True)
        return 0
    if cmd == "ps":
        return subprocess.run(
            [
                engine,
                "ps",
                "-a",
                "-f",
                f"name=^{box}$",
                "--format",
                "table {{.Names}}\t{{.Status}}\t{{.Networks}}",
            ],
            env=env,
        ).returncode
    _err("usage: foldyard box {build|up|shell|exec|down|ps}")
    return 2


def _exec(engine: str, box: str, ctx, env: dict, args: list[str]) -> int:
    """Run a command NON-interactively in the running dev box, at the caller's cwd (the repo is
    bind-mounted at the same path in the box, so platform/ on the Mac is platform/ in the box).
    The primitive behind git hooks dispatching into the box: `foldyard box exec '<shell-cmd>'`.

    `--skip-if-down` makes a stopped box a clean no-op (exit 0 + note) instead of an error — for
    hooks that shouldn't block a Mac-side commit when the box is down (CI stays the backstop)."""
    skip_if_down = "--skip-if-down" in args
    rest = [a for a in args if a != "--skip-if-down"]
    if not rest:
        _err("usage: foldyard box exec [--skip-if-down] <shell-command> [args…]")
        return 2
    # One arg → a ready shell string (the hooks path; preserves the command's own quoting);
    # multiple → join them shell-safely (the `fy box exec ls -la` ergonomic path).
    script = rest[0] if len(rest) == 1 else shlex.join(rest)
    if not _running(engine, box, env):
        hint = _attach_hint(ctx.worktree).replace("shell", "up")
        if skip_if_down:
            _err(f"(dev box {box} not running — skipping; start it with `{hint}` or run inside it)")
            return 0
        _err(f"dev box {box} not running — start it: {hint}")
        return 1
    # Login shell so PATH (pnpm/node/uv) matches an attached `fy box shell`.
    return subprocess.run(
        [engine, "exec", "-w", os.getcwd(), box, "bash", "-lc", script], env=env
    ).returncode
