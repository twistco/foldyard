"""``foldyard init`` — scaffold a ``foldyard.toml`` for a new project (ADR-0013).

One writer (:func:`render`) emits a single, opinionated starter: a LOCKED-DOWN, stack-less
Lima dev box you can safely build and poke, with the agents and the full feature set present
but COMMENTED — so a first run is safe, and growing the config is uncommenting, in the order
the file itself teaches. There is deliberately no interactive wizard: the template is the guide.
Only the project name + VM sizing are parameterised (typer flags); everything else is the same
curated boilerplate every project starts from.

A second writer (:func:`render_local`) emits ``foldyard.local.toml.example``, the committed
template for the gitignored per-developer overlay. It ships WITH the scaffold rather than being
something each project reinvents, because the overlay is where two things belong that the shared
file structurally can't hold: personal posture (whose Claude/Codex subscription, how big this
laptop is), and ``disabled = true`` — the only way to opt OUT of a block the team committed.

Stdlib only — a Mac/host command that stays import-light like the rest of the CLI. The machine is
auto-init'd on the first ``foldyard box up`` (``machine.ensure``), so the flow is
``foldyard init … && foldyard box up``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import compat

# config defaults mirrored here so `init` writes the SAME numbers foldyard would otherwise
# fall back to (config.machine_resources defaults: cpus=4, memory=8192 MiB, disk=60 GiB).
DEFAULT_CPUS = 4
DEFAULT_MEMORY_MIB = 8192
DEFAULT_DISK_GIB = 60

# Box/host-generated files foldyard writes NEXT TO this config — the VM-visible staging dirs
# (proxy CA, Claude transcripts, staged foldyard/gcp assets) regenerated on box-up/`fy up`, and
# the `.dev-mode.json` posture mirror the host drops in `dev_vm_dir()` (default: the repo root).
# None are ever committed. `init` fences them into the consumer's .gitignore under these
# sentinels so a re-run refreshes the block in place instead of appending a duplicate.
# (Plugin/stack-specific artifacts — .stubs/, auth0-sim .certs-local/ — are NOT here: a bare
# scaffolded box never generates them and their dirs are configurable.)
GITIGNORE_BEGIN = "# >>> foldyard (managed) — do not edit inside this block >>>"
GITIGNORE_END = "# <<< foldyard (managed) <<<"
GITIGNORE_ENTRIES = (
    ".devbox-ca/",
    ".devbox-claude/",
    ".devbox-codex/",
    ".devbox-foldyard/",
    ".dev-mode.json",
    # The per-developer overlay itself. It holds each person's keyless credential CHOICE (never a
    # secret — those live in the host's ~/.foldyard) and their `disabled` opt-outs, and it WINS the
    # merge, so committing one silently re-points everyone else's posture. Recursive so a worktree's
    # copy is ignored too; the committed `.example` beside it is what the team shares.
    "**/foldyard.local.toml",
)

# The template `init` writes beside the config — committed, unlike what it's a template FOR.
LOCAL_EXAMPLE = "foldyard.local.toml.example"


def _running_version() -> str:
    """The foldyard doing the scaffolding — what the file it writes is written FOR."""
    from . import __version__

    return __version__


@dataclass
class InitOptions:
    name: str
    cpus: int = DEFAULT_CPUS
    memory_mib: int = DEFAULT_MEMORY_MIB
    disk_gib: int = DEFAULT_DISK_GIB
    #: The version stamped as the scaffold's floor. Defaults to the ``fy`` running `init`;
    #: a field rather than a lookup inside :func:`render` so the writer stays pure + testable.
    version: str = field(default_factory=_running_version)


def _version_lines(version: str) -> list[str]:
    """The declared version window for a freshly scaffolded repo (:mod:`foldyard.compat`).

    The floor is stamped with the ``fy`` that WROTE the file, which is the only version the
    scaffold is known to be right for — and the honest starting point for a key whose whole job is
    to be raised, in the same commit, by whatever later adds a setting an older ``fy`` can't
    honour. It matters more than it looks: ``config`` reads the TOML with ``.get()`` and no schema,
    so an old ``fy`` against a newer ``foldyard.toml`` doesn't fail — it ignores the keys it
    doesn't know and does the old thing, silently. A floor is the only thing that turns that into
    an error, and a floor nobody wrote is a floor of zero.

    The recommendation + the reasons ledger ship COMMENTED, like the rest of the file: they are
    about a project's history with the tool, which a repo on day one has none of.

    When the running version can't be ordered (``0+unknown`` from a bare source-tree import, or a
    local build — :func:`foldyard.compat._parse` returns None for both), the key is written
    COMMENTED. A floor foldyard itself would ignore reads as protection and is none, and guessing
    a number from a build that won't say what it contains is worse than leaving the line to a
    human."""
    known = compat._parse(version) is not None
    lines = [
        "# The foldyard this file was written for, as a FLOOR — `fy` REFUSES to run in this",
        "# checkout below it. Not fussiness: an older fy doesn't fail on config it doesn't",
        "# understand, it ignores those keys silently and does the old thing. Raise this in the",
        "# same commit as any setting that needs a newer fy, and the two can never come apart.",
    ]
    if known:
        lines.append(f'min_foldyard_version = "{version}"')
    else:
        lines += [
            f"# (fy could not tell its own version here — it reported {version!r} — so this is",
            "#  left commented rather than stamped with a number that means nothing.)",
            '# min_foldyard_version = "0.1.0"',
        ]
    # A real number rather than a `"…"` placeholder: uncommented verbatim, an unparseable version
    # is silently inert (compat has no opinion on it), and silently inert config is the thing this
    # file's floor exists to prevent.
    shown = version if known else "0.1.0"
    lines += [
        f'# recommended_foldyard_version = "{shown}"   # a NUDGE, never a block: printed on',
        "#                     # `fy up` / `fy box up` / `fy host` when you're behind it. Raise it",
        "#                     # for a version worth having; leave the floor for one you must have.",
    ]
    return lines


def _slug(name: str) -> str:
    """A safe container/volume/network prefix from a project name (lowercase, alnum + ``-``)."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


def render(opts: InitOptions) -> str:
    """The commented ``foldyard.toml`` body for ``opts``. Pure — easy to unit-test.

    The output is ACTIVE for a box-only lima+wall+proxy dev box, and COMMENTED for everything a
    project grows into (agents, then a stack + the rest). The header spells out the intended
    order so the file bootstraps itself.

    Deliberately ABSENT (documented in docs/configuration.md instead): the posture-wiring tier —
    ``[[overlay]]``, ``[[require]]``, ``[[inject]]``, the credential plugins. A scaffold has no
    axes to wire yet. If a stack/overlay section is ever added here, ``[[require]]`` must ride
    along with it (they're one tier: an overlay creates the coupling a require declares); until
    then the onboarding pathway is the example consumer + compose-overlays.md's worked example."""
    prefix = _slug(opts.name)
    lines = [
        f"# foldyard config — {opts.name}",
        "# Generated by `foldyard init`. This starts as a LOCKED-DOWN, stack-less dev box you can",
        "# safely build and poke — then you GROW it by uncommenting, in this order:",
        "#   1. `fy box up` then `fy box shell` — a rootless Lima VM, egress WALLED to a host-side",
        "#      allowlisting proxy that REFUSES anything you haven't granted, ONLY this repo",
        "#      mounted, no push credential. `fy verify` proves the cage. The first `fy box up`",
        "#      offers you the handful of hosts the box needs to build itself ([proxy] below) —",
        "#      one yes each and it comes up walled and working. Poke around; nothing escapes.",
        "#   2. Uncomment ONE agent ([claude] or [codex]) + `fy box up` again — now an AI agent",
        "#      runs INSIDE the safe sandbox (it offers that agent's install hosts the same way).",
        "#      Use it to work out the rest of this config with you.",
        "#   3. Uncomment a stack + the features at the bottom as the project needs them.",
        "# Resolution everywhere: explicit env var wins → foldyard.local.toml (gitignored,",
        "# per-developer overrides) → this file → built-in default. Keep what's PERSONAL out of",
        "# here: which agent subscription you pay for, how big your VM is, a pinned worktree port.",
        "# Those go in each developer's own copy of foldyard.local.toml.example (written beside this",
        "# file), which can also switch a block below OFF for one person with `disabled = true`.",
        "",
        "[project]",
        # json.dumps escapes quotes/backslashes/control chars — JSON string syntax is valid
        # TOML basic-string syntax, so any name round-trips instead of breaking the parse.
        f"name = {json.dumps(opts.name)}",
        "# Container/volume/network name prefix (a worktree appends `-<name>`).",
        f'prefix = "{prefix}"',
        *_version_lines(opts.version),
        "# A compose stack to drive alongside the box — uncomment when you have one (step 3), add",
        "# its host ports under [ports] below, and extend [claude].system_prompt with the stack",
        "# paragraph parked beside it (an agent that knows the box but not the stack is half-lost).",
        '#   app = "app"                # the app service (fy shell target)',
        '#   app_port = "WEB_PORT"      # [ports] key shown in the TUI + opened by `fy open`',
        '#   compose = ["compose.yml"]  # compose files in -f order, relative to this dir',
        "# Why this repo wanted each foldyard it adopted. Both version messages list the entries",
        "# between the fy you have and the one you're being pointed at, so an upgrade prompt says",
        "# what you'd GAIN. A sub-table, so it must stay last in [project]:",
        "# [project.foldyard_version_reasons]",
        f'# "{opts.version}" = "the version this project started on"',
        "",
        "[machine]",
        "# A rootless VM that mounts ONLY this repo. Default here is the Lima backend + an in-VM",
        "# egress WALL: fail-closed, so the box's only way out is the host's allowlisting proxy",
        "# (below). Sizing applies at FIRST creation; to resize later: `foldyard machine recreate`.",
        '#   backend = "podman"  # the single shared podman machine — no concurrent per-project',
        "#                       # VMs, and no in-VM wall (egress is the host-side proxy + gvproxy)",
        '#   backend = "native"  # host podman directly (no VM) — weakest isolation, shared kernel',
        "#   wall = false        # lima but COOPERATIVE-only: the box uses the proxy, but nothing",
        "#                       # stops a process that ignores it (the wall makes it fail-closed)",
        'backend = "lima"',
        "wall = true",
        f'name = "{prefix}"',
        f"cpus = {opts.cpus}",
        f"memory_mib = {opts.memory_mib}   # MiB — any whole number",
        f"disk_gib = {opts.disk_gib}",
        "",
        "# Egress proxy. Declaring [proxy] routes the box's egress through the host proxy — REQUIRED",
        "# by the wall (which otherwise leaves the box with no way out). ENFORCING from the start:",
        "# a host you haven't granted is refused, and the recommendations below are how the box",
        "# still builds anyway (you're offered them, one yes each, before the first build). To watch",
        "# rather than block while you discover a new dependency's egress:",
        "#     fy allow wall off                     # observe only — everything through, all logged",
        "#     fy allow wall on                      # back to enforcing",
        "# Grants are deliberately NOT config: they live in the host-side allow-store, outside the",
        "# repo mount, so the box can't widen its own wall by editing a file it can write. Blocked",
        "# hosts show live in `fy tui`, where one keypress grants them (and its Network Log's wall",
        "# pane manages grants + these offers). `default_deny` here only SEEDS the first answer —",
        "# `fy allow wall` is the switch from then on. Any keyless host (below) is ALWAYS allowed",
        "# implicitly — never list it.",
        "[proxy]",
        "default_deny = true",
        "# The repo can RECOMMEND hosts (this is how a team SHARES its allowlist): each entry is",
        "# OFFERED per host at `fy up`/`fy box up`/`fy allow sync`, and nothing is granted without",
        "# that yes — repo config asks, the host answers. (`fy allow sync --yes` takes them all at",
        "# once, for an unattended setup with no terminal to answer on.) These cover box bootstrap,",
        "# so the wall above comes up working after one round of consented yeses; the agents below",
        "# add their own install hosts to the same offer when you uncomment one. Add your project's",
        "# own hosts WITH a why (the why is shown at the decision moment).",
        "recommend = [",
        '  { host = "pypi.org", why = "uv/pip — box bootstrap + deps" },',
        '  { host = "files.pythonhosted.org", why = "uv/pip package downloads" },',
        '  { host = "github.com", why = "git fetch/clone + release-asset redirects" },',
        '  { host = "objects.githubusercontent.com", why = "GitHub release assets — uv-managed Python" },',
        '  { host = "release-assets.githubusercontent.com", why = "GitHub release assets (newer host)" },',
        '  { host = "deb.debian.org", why = "apt — [[box.tools]] system packages" },',
        '  { host = "registry-1.docker.io", why = "box image build — base image manifest/blobs" },',
        '  { host = "auth.docker.io", why = "box image build — Docker Hub auth token" },',
        '  { host = "production.cloudfront.docker.com", why = "box image build — Docker Hub blob CDN" },',
        '  { host = "ghcr.io", why = "box image build — the uv binary image layer" },',
        '  { host = "pkg-containers.githubusercontent.com", why = "box image build — ghcr.io blob storage" },',
        "]",
        '# passthrough = ["@all"]   # hosts tunnelled UN-decrypted under capture=on (the toolchain)',
        "",
        "# ── Agents — uncomment ONE, then `fy box up` (step 2) ────────────────────────────────────",
        "# Claude Code (`fy claude`). Declaring [claude] installs it on box-up + mounts its persisted",
        "# login/transcripts. Keyless: the real token is injected at the host proxy, NEVER in the box",
        "# (`fy box up` prompts once, hidden; stored only on the host). Then `fy mode claude=on`.",
        "# system_prompt is appended via --append-system-prompt, so a fresh `fy claude` starts",
        "# oriented instead of re-deriving the box and getting it wrong. The one below WORKS as",
        "# written for a BOX-ONLY project — uncomment and go. It says nothing about a stack, so it",
        "# stays true at step 2; when you add compose at step 3, paste the follow-on paragraph",
        "# below it. Same progressive opt-in as the rest of this file.",
        "# [claude]",
        '# keyless = "oauth"      # Bearer on api.anthropic.com ← `claude setup-token` on the host',
        '# # keyless = "api-key"  # x-api-key on api.anthropic.com ← from console.anthropic.com',
        '# system_prompt = """',
        "# You are root in this project's foldyard dev box, started by `fy claude` with permission",
        "# prompts skipped. Orientation, so you don't have to re-derive it:",
        "#",
        "# The cage is REAL but it is not a locked-down shell. `DOCKER_HOST`/`CONTAINER_HOST` are",
        "# preset and you have FULL engine access through them — `docker`/`podman` run/exec/build,",
        "# compose, the lot. What bounds you is the machine underneath: a ROOTLESS VM that mounts",
        "# ONLY this repo, with an in-VM egress wall whose sole way out is the allowlisting proxy",
        "# on the host. So the blast radius is this repo plus this project's containers — never the",
        "# host. Prove it any time with `fy verify` (expect ALL PASS).",
        "#",
        "# `fy` works in here for the read and box verbs: `fy verify`, `fy box shell|ps|down`, and",
        "# `fy mode` to SEE the credential posture — posture is SET from the host, not from in here,",
        "# and so are egress grants. A second checkout beside this one is `fy worktree add <name>`,",
        "# then `WORKTREE=<name> fy <verb>`.",
        "#",
        "# Git: read and commit locally, but you CANNOT push — no credential in here reaches the",
        "# origin, by design. Push from the host.",
        "#",
        "# Egress that fails is the wall doing its job, not a bug to route around. Report the",
        "# blocked hostname and ask the operator to `fy allow add <hostname>`; the grant store",
        "# lives outside this box, out of your reach.",
        "#",
        "# You may not be alone: several sessions can attach to this one box and checkout. You run",
        "# as root, so files you create land root-owned.",
        "#",
        "# Depth on demand, both version-matched to the foldyard running here — never a web search",
        "# or a clone of the project, which would describe a different version: the `foldyard`",
        "# SKILL in .claude/skills/ (the model — posture, egress, why a change didn't take), and",
        "# `fy docs` for the manual (`fy docs` lists topics; `fy docs modes`, `fy docs adr-0007`).",
        '# """',
        "#",
        "# …and WHEN YOU ADD A COMPOSE STACK (step 3), append this paragraph to that prompt — it is",
        "# the half that only makes sense once there is a stack to drive:",
        "# #",
        "# # This project also drives a compose stack. `fy up | ps | logs [svc] | shell | down`",
        "# # runs it, and the engine you reach IS that running stack — a stray `rm -f` or `down`",
        "# # acts on it for real, including on another session's work. Reach its services by",
        "# # CONTAINER NAME, not localhost: you sit on the project network, and published ports",
        "# # are published to the host, not to you.",
        "#",
        "#",
        "# Session config for the same CLI, merged over the box's own settings — the non-prompt half",
        "# of the same idea. Any key Claude's settings.json takes; `fy claude --settings` still wins.",
        "# [claude.settings]",
        "# showThinkingSummaries = true",
        "#",
        "# OpenAI Codex CLI (`fy codex`). Declaring [codex] installs it on box-up + mounts ~/.codex.",
        "# Native installer (a static binary — no node needed). Keyless like Claude: the proxy",
        "# injects host-side. Leave `keyless` out and you get a plain in-box `codex login` instead.",
        "# [codex]",
        '# keyless = "api-key"    # Authorization: Bearer ← OPENAI_API_KEY (captured to host.env)',
        '# # keyless = "chatgpt"  # your ChatGPT subscription: the minter refreshes ~/.codex/auth.json',
        '# system_prompt = """…"""   # same key + same job as [claude]\'s above (paste that one);',
        "#                          # Codex takes it as a developer instruction, not a system prompt",
        "#",
        "# Codex's config.toml keys, one `-c` per leaf (nested tables flatten to dotted paths):",
        "# [codex.config]",
        '# model_reasoning_summary = "auto"',
        "# tui = { raw_output_mode = false }",
        "",
        "# ── The rest — uncomment as you grow (step 3) ────────────────────────────────────────────",
        "# Host-published ports, keyed by the env var your compose file reads. In-container ports",
        "# never move, so a second worktree's stack gets offset host ports and never collides.",
        "# [ports]",
        "# APP_PORT = 3000",
        "#",
        "# Your OWN toolchain image instead of foldyard's packaged generic box (engine client + git",
        "# + uv). Point it at a Dockerfile; foldyard injects the socket/CLI/proxy-CA at box-up.",
        "#",
        "# STOP THE BOX AND THE HOST CLOBBERING EACH OTHER'S DEPS — the one thing every project",
        "# hits. The box mounts this checkout, so an in-tree build-artifact dir (.venv,",
        "# node_modules, target, .next) is the SAME directory the host builds into: two toolchains,",
        "# two kernels, two uids, one dir. `shadow_volumes` masks each one with a per-box named",
        "# volume — the box gets its own copy at that path, the host keeps its own underneath,",
        "# neither sees the other. It needs the two keys under it, not just itself: a fresh shadow",
        "# volume starts EMPTY (so `warmup` fills it), and it is a DIFFERENT filesystem from the",
        "# package cache (so the default hardlink import fails cross-device — pin `env`). `warmup`",
        "# installs must be FROZEN/locked: the lockfiles live on the mount, and a resolving install",
        "# would rewrite the host tree as root. `caches` is optional — a warm cache that survives",
        "# box recreation; pnpm ALSO needs its store pinned there (npm_config_store_dir), or it",
        "# relocates the store inside the node_modules volume and re-downloads every time.",
        "# [box]",
        f'# image = {{ dockerfile = "box.Dockerfile", tag = "{prefix}-box:latest" }}',
        '# shadow_volumes = [".venv"]   # or ["web/node_modules", "rust/target"] — checkout-relative',
        '# warmup = [{ dir = ".", run = "uv sync --frozen" }]',
        '# env = { UV_LINK_MODE = "copy" }   # pnpm: npm_config_package_import_method = "copy"',
        f'# caches = [{{ volume = "{prefix}-uv-cache", path = ".cache/uv" }}]   # path is under HOME',
        "#",
        "# Consumer tools installed once per fresh box, as MONITORED steps (reported ✓/✗):",
        "# [[box.tools]]",
        '# name = "pulumi"',
        '# install = "curl -fsSL https://get.pulumi.com | sh -s -- --install-root /opt/fy-tools --no-edit-path"',
        "#",
        "# VS Code attach (`fy code`) — persist the vscode-server volume across box recreations.",
        "# [vscode]",
    ]
    return "\n".join(lines) + "\n"


def render_local(opts: InitOptions) -> str:
    """The committed ``foldyard.local.toml.example`` body — the template each developer copies to
    their own gitignored ``foldyard.local.toml``.

    Wholly COMMENTED, like the main scaffold: copying it must change nothing until a line is
    uncommented. It teaches the two things the shared file structurally can't — the personal half
    of posture (your keyless credential, your machine's sizing, your port pins), and ``disabled``,
    the only way to say "not for me" about a block the team committed."""
    lines = [
        f"# foldyard per-developer overrides — {opts.name}   (TEMPLATE — copy, don't edit in place)",
        "#",
        "#     cp foldyard.local.toml.example foldyard.local.toml",
        "#",
        "# The copy is GITIGNORED and yours alone. It is deep-merged OVER the committed",
        "# foldyard.toml — tables merge key-by-key, so a `keyless` here lands beside the shared",
        "# `system_prompt` rather than replacing the table. Full resolution order:",
        "#     explicit env var  →  foldyard.local.toml  →  foldyard.toml  →  built-in default",
        "# (arrays, including arrays-of-tables like [[inject]], are replaced WHOLESALE — redeclare",
        "# every entry you still want, not just the new one).",
        "#",
        "# What belongs here: anything true of YOU rather than of the project — which agent you pay",
        "# for, how big your machine is, which port a worktree must land on. Nothing secret: keyless",
        "# auth stores the real token on the host (~/.foldyard/<project>/host.env), never in the repo",
        "# and never in the box.",
        "#",
        "# This file drives host-side credential injection, so the host runs the copy it ADOPTED:",
        "# after editing, `fy config adopt` (or answer the prompt at the next `fy up` / `fy box up`).",
        "",
        "# ── Turn a shared block OFF: `disabled = true` ───────────────────────────────────────────",
        "# The merge can otherwise only ADD, and several tables are PRESENCE-gated — declaring",
        "# [claude] / [codex] / [vscode] / [plugins.<name>] at all IS the opt-in — so blanking their",
        "# keys doesn't switch them off. `disabled = true` removes the block from the resolved config",
        "# as if it had never been written; the team keeps it, you don't get it. The case it exists",
        "# for: the repo commits [codex], you don't have a Codex subscription, and you'd rather not",
        "# have the CLI installed on box-up, be asked for a credential, or carry a dead codex row in",
        "# `fy mode`. Works at any depth ([plugins.github] too), and `disabled = false` here puts a",
        "# block back that the SHARED file disabled. `fy config widenings` lists what you've removed.",
        "# [codex]",
        "# disabled = true",
        "",
        "# ── Keyless agent auth — the real credential never enters the box ────────────────────────",
        "# The box gets a DUMMY; the egress proxy swaps in the real token host-side, in flight. Which",
        "# one you use is personal, which is why it lives here: a `keyless` in the shared file makes",
        "# every colleague's `fy box up` prompt for a credential they may not have.",
        "#",
        "# Claude Code (`fy claude`) — pick ONE:",
        "#   oauth   — a long-lived OAuth token (sk-ant-oat…) from `claude setup-token` on the host.",
        "#   api-key — an Anthropic API key (sk-ant-api…) from console.anthropic.com.",
        "# A TTY `fy box up` prompts once (hidden) and stores it host-side. Then `fy mode claude=on`.",
        "# Omit `keyless` entirely and you get a plain in-box `claude` login instead.",
        "# [claude]",
        '# keyless = "oauth"',
        "#",
        "# OpenAI Codex CLI (`fy codex`) — pick ONE:",
        "#   chatgpt — your ChatGPT subscription: the access token is minted and refreshed host-side",
        "#             from the host's ~/.codex/auth.json (run `codex login` there once). No key.",
        "#   api-key — an OpenAI API key (sk-…), captured host-side like Claude's.",
        "# Then `fy mode codex=on`. Omit `keyless` for a plain in-box `codex login`.",
        "# `system_prompt` works here exactly as it does for [claude] — Codex takes it as a developer",
        "# instruction rather than a system prompt, but the effect (a session that starts oriented in",
        "# this box) is the same. Keep it in the SHARED file if it's about the project, not about you.",
        "# [codex]",
        '# keyless = "chatgpt"',
        '# system_prompt = """',
        "# …your orientation prompt for `fy codex`.",
        '# """',
        "",
        "# ── Your machine, not the project's ──────────────────────────────────────────────────────",
        "# The committed sizing suits the project; this suits your laptop. Sizing applies when the VM",
        "# is CREATED, so changing it later needs `fy machine recreate`.",
        "# [machine]",
        f"# cpus = {opts.cpus}",
        f"# memory_mib = {opts.memory_mib}   # MiB — any whole number",
        f"# disk_gib = {opts.disk_gib}",
        "",
        "# ── Pin a worktree's host-port offset ────────────────────────────────────────────────────",
        "# A worktree's ports are offset by a deterministic 1..89 hash of its name, so two checkouts",
        "# never collide. Pin a SMALL fixed offset when a worktree needs a KNOWN port — the usual",
        "# reason is a third-party service that only redirects to pre-registered callback URLs (an",
        "# identity provider allowing localhost:3000..3005, say). Every port shifts by the same N, so",
        "# pinned stacks stay collision-free. Read from the MAIN checkout's file wherever `fy` runs;",
        "# WT_OFFSET in the environment still wins.",
        "# [worktree-offsets]",
        "# my-worktree = 1",
        "",
        "# ── A private injector (e.g. your own MCP endpoint) ──────────────────────────────────────",
        "# Same shape as the [[inject]] tables in foldyard.toml, and it becomes its own on/off mode",
        "# axis (`fy mode myservice=on`). The token is read host-side from $FY_INJECT_MYSERVICE in",
        "# ~/.foldyard/<project>/host.env — the box only ever holds a dummy. Add the host to the",
        "# egress allowlist too (`fy allow add my-host.example.com`), or the proxy refuses it.",
        "# [[inject]]",
        '# axis   = "myservice"',
        '# host   = "my-host.example.com"',
        '# header = "Authorization"',
        '# label  = "My private service"',
    ]
    return "\n".join(lines) + "\n"


def _write_local_example(target: Path, opts: InitOptions, *, force: bool) -> str:
    """Write ``foldyard.local.toml.example`` beside the config. Returns a status word ("written" |
    "kept") for the caller to report. Never clobbers without ``--force``: this template is meant to
    grow into a project's own menu of overrides, and a re-run of `init` must not throw that away."""
    path = target / LOCAL_EXAMPLE
    if path.exists() and not force:
        return "kept"
    path.write_text(render_local(opts))
    return "written"


def _gitignore_block() -> str:
    """The managed foldyard section, sentinels included (no trailing newline)."""
    return "\n".join(
        [
            GITIGNORE_BEGIN,
            "# Box/host-generated files foldyard writes beside foldyard.toml; never commit.",
            *GITIGNORE_ENTRIES,
            GITIGNORE_END,
        ]
    )


def _update_gitignore(target: Path) -> str:
    """Ensure ``target/.gitignore`` carries the managed foldyard block. Idempotent: a re-run
    replaces the block in place rather than appending a duplicate. Returns a status word
    ("created" | "added" | "updated" | "unchanged") for the caller to report."""
    path = target / ".gitignore"
    block = _gitignore_block()
    if not path.exists():
        path.write_text(block + "\n")
        return "created"
    existing = path.read_text()
    fenced = re.compile(re.escape(GITIGNORE_BEGIN) + r".*?" + re.escape(GITIGNORE_END), re.DOTALL)
    if fenced.search(existing):
        # A function replacement (not the string form) so re never touches `\`/`\g` in block.
        updated = fenced.sub(lambda _: block, existing)
        if updated == existing:
            return "unchanged"
        path.write_text(updated)
        return "updated"
    # Append after whatever's there, separated by exactly one blank line.
    prefix = existing if existing.endswith("\n") else existing + "\n"
    if not prefix.endswith("\n\n"):
        prefix += "\n"
    path.write_text(prefix + block + "\n")
    return "added"


def _install_agent_skill(target: Path, *, force: bool) -> None:
    """Drop the bundled ``foldyard`` skill into ``<target>/.claude/skills/`` as part of scaffolding.

    An agent in a fresh box needs to know what it CAN'T do (push, grant itself credentials, widen
    its own egress) before it wastes a session discovering it — and the version-matched place for
    that is the packaged skill, not prose copied into each consumer's CLAUDE.md, which is exactly
    what rots. Best-effort and never fatal: a repo with its own skill of that name keeps it (no
    ``--force``), and a failure leaves a scaffolded config that still works.

    ``config.repo_root()`` is what ``skills.install`` writes under, so bind the freshly scaffolded
    directory — ``init`` legitimately runs from outside the target, and the ambient resolution
    would otherwise walk up to some other checkout's ``foldyard.toml``."""
    from . import config, skills

    with config.using(config.resolve(worktree="", repo=target)):
        dest = target / ".claude" / "skills" / "foldyard"
        if dest.exists() and not force:
            return  # already there — a re-run must not clobber a consumer's edits
        skills.install("foldyard", force=True)


def init(
    *,
    path: str = ".",
    name: str = "",
    cpus: int = DEFAULT_CPUS,
    memory: int = DEFAULT_MEMORY_MIB,
    disk: int = DEFAULT_DISK_GIB,
    force: bool = False,
) -> int:
    """Write ``<path>/foldyard.toml``. Returns a process exit code."""
    target = Path(path).expanduser().resolve()
    dest = target / "foldyard.toml"
    if dest.exists() and not force:
        print(f"✗ {dest} already exists — pass --force to overwrite.")
        return 1
    opts = InitOptions(name=name or target.name, cpus=cpus, memory_mib=memory, disk_gib=disk)
    try:
        target.mkdir(parents=True, exist_ok=True)
        dest.write_text(render(opts))
        local_status = _write_local_example(target, opts, force=force)
        gitignore_status = _update_gitignore(target)
    except OSError as e:
        print(f"✗ could not write {dest}: {e}")
        return 1
    floor = f" (floor: foldyard >= {opts.version})" if compat._parse(opts.version) else ""
    print(f"✓ wrote {dest}{floor}")
    if local_status == "written":
        print(f"✓ wrote {target / LOCAL_EXAMPLE} (per-developer overrides — commit this template)")
    if gitignore_status != "unchanged":
        verb = "created" if gitignore_status == "created" else "updated"
        print(f"✓ {verb} {target / '.gitignore'} (box-generated dirs + foldyard.local.toml)")
    _install_agent_skill(target, force=force)
    print("  A locked-down, stack-less Lima box. Next: `foldyard box up` then `foldyard box shell`")
    print("  (needs Lima: `brew install lima`). The file itself guides what to uncomment next.")
    return 0
