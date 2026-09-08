# Developing foldyard

How to work on foldyard itself and validate changes. The "why" behind design calls lives in
[docs/adrs/](./docs/adrs/); the pre-extraction run-logs stay in the origin monorepo (ADR-0013).
This file is project-agnostic: foldyard is consumed by a host project's `foldyard.toml`, but
nothing here is specific to any one consumer. (`CLAUDE.md` symlinks here, so agents and
humans read the same guide.)

## The package

foldyard is a uv project (`pyproject.toml`, `src/foldyard/`, `tests/`), distributed via
`uv tool install --editable` so the `foldyard` entry point is on PATH with a per-machine venv
(off any shared mount). Stdlib-only on the recipe hot path; typer drives the CLI and Textual
the TUI (imported lazily).

```bash
just foldyard install     # uv tool install --force --editable (per-machine venv)
just foldyard test [args] # the suite (add -n auto to parallelise)
just foldyard run <verb>  # run the CLI from source without installing
just foldyard lint        # ruff check + format --check   (auto-fix: just foldyard format)
just foldyard typecheck   # pyright + ty (both, run together)
just foldyard check       # lint + typecheck + suite — what CI gates on
```

**Tooling (dev deps, in `pyproject.toml`):** ruff (lint+format, line-length 100), pyright +
ty (both type-checkers — keep green under each), pytest-xdist (`-n auto`). ruff config
carries the deliberate ignores (✓/✗/▶ glyphs = RUF001-003; typer `Argument`/`Option`
defaults = B008; Textual mutable class-attrs = RUF012 in tui; box.py embeds long shell =
E501). Keep all three at zero before committing (`just foldyard check`).

## Module map — `src/foldyard/`

Core (stdlib-only on the hot path; heavy imports lazy):

- `cli.py` — the typer app; every verb, heavy imports deferred.
- `config.py` — repo/project/path resolution + ALL consumer config (env var wins →
  `foldyard.local.toml` → `foldyard.toml` → default) + the `~/.foldyard/<project>/` state paths.
- `stack.py` — stack-setup env + the compose verbs (`up`/`down`/`ps`/`logs`/`shell`…); emits
  `ENGINE`, `DOCKER_HOST` **and** `CONTAINER_HOST` (same socket, both names), and pins Podman's
  provider to the `podman-compose` bundled in Foldyard's tool venv.
- `devmode.py` — posture-mode substrate (state I/O, TTLs, expiry + the settle cascade,
  dashboard incl. DEGRADED capability, the `fy clock` test skew); axes/daemons/env/probes
  delegate to the plugin registry.
- `reconcile.py` — the reconciler SCOPE inventory (consolidation proposal A): every state
  tier as a class carrying its desired-vs-observed rows AND its action (delegating to the
  tested implementation at its existing trigger); tests/test_reconcile_scenarios.py pins the
  orchestration.
- `state_view.py` — `fy state`: thin renderer over `reconcile.scopes()`; non-zero exit on
  drift.
- `verify.py` — the isolation battery, the product's credibility check; plugin-agnostic core +
  the registry's `verify_checks`.
- `machine.py` / `machine_backend.py` — rootless dev-VM lifecycle behind the pluggable
  backend contract (podman | lima | native; see `docs/lima-backend-scope.md`).
- `box.py` — the dev-box lifecycle (`fy box build|up|shell|down|ps`) + the monitored bootstrap.
- `supervisor.py` — `fy host`: the ONE Mac-side process running the credential daemons
  (singleton lock, per-worktree listeners, replace-on-launch staleness handling, the
  capability-probe loop, TTL expiry + settle).
- `worktree.py` · `transcripts.py` · `tui.py` · `init.py` · `skills.py` · `browser.py` ·
  `vscode.py` — worktrees, agent-transcript sync, the Textual TUI, `foldyard init`, bundled
  skills, `fy open`, `fy code`.
- `docs.py` — `fy docs [topic]`: the manual served from THIS install (the consumer subset is
  force-included into the wheel; see pyproject). Version-matched by construction, works with no
  egress — the reason an agent should never clone the project to read about it.
- `configpin.py` — the ADOPTED `foldyard.toml` the host reconciles from (the file lives in the
  mount, so the supervisor reads a snapshot under `~/.foldyard/adopted/<checkout>-<hash>/`
  instead — keyed by checkout PATH, never by anything the config declares): drift detection + diff, the `adopt`/`revert`/`ignore` gate every launch verb runs,
  and `fy config …`. See [ADR-0022](./docs/adrs/0022-host-runs-the-adopted-config.md).
- `exposure.py` — the widenings inventory (`fy config widenings` + its doctor row): what the
  adopted config asks the HOST to allow — capture exemptions with `@bundle` refs RESOLVED to a
  count, injection targets (armed and latent, asked of the registry so an odd shape like Codex's
  ChatGPT rung reports its real host), agent prompts attributed to the shared vs the gitignored
  file, and `IGNORED_KEYS` — config that reads as a control and is no longer honoured.
- `allowlist.py` · `ports.py` · `preflight.py` · `keyless.py` — the egress allow-store behind
  `fy allow` + the TUI's host grants (all levels host-side), cross-project port-band allocation,
  hard-prerequisite checks before `up`, and the keyless-agent credential taxonomy + host-side
  secret capture (`ensure_cred` for agent creds, `ensure_secret` for declared `[[secret]]` rows).

`plugins/` — the framework (`__init__.py`: `Axis`/`Requires`/`Plugin`/`Registry`, per-consumer
loading; cross-axis coherence is declared as `Requires` DATA on two tiers — in-code
`Axis.requires` for intrinsic couplings, the consumer's `[[require]]` table for
wiring-dependent ones (docs/configuration.md), merged at Registry construction; the
`mode_issues` hook is the escape hatch for logic the data can't express, e.g. combination
warnings)
plus the built-ins: `gcp.py` + `gcp_metadata/`, `github.py` + its two MINTER KINDS
(`github_app_token.py` = the App installation token + the `github=app` capability probe,
`gh_cli_token.py` = `gh auth token` for the `user` emergency; packaged because the mint path runs
host-side beside the credentials — [ADR-0023](./docs/adrs/0023-no-host-executed-code-from-the-repo-mount.md)), `proxy.py` +
`inject.py` +
`static_token.py` + `_passthrough_bundles.py` (the egress proxy + injector rules), `claude.py`,
`codex.py` + `codex_chatgpt_token.py`, `vscode.py`, and the Tangible-bound declared pair
`auth0_sim.py` + `llm.py` (both slated for DELETION, not migration — see
[ADR-0024](./docs/adrs/0024-declarative-consumer-axes-no-repo-path-plugins.md): they turned out to
be data, so a declarative `[[axis]]` table replaces them), and the declared
zero-secret testing rig `fakecred.py` + `fakecred_minter.py` (docs/testing-modes.md). Built-ins
load DIRECTLY (never as entry points); third parties via the `foldyard.plugins` entry-point
group.

## Test tiers

foldyard's surface splits by *where it can be validated*:

1. **Substrate + TUI** — `devmode`/`config`/`plugins` unit tests + headless Textual pilot
   tests. Run anywhere, no engine. Includes the **property-based tier** (hypothesis):
   `tests/test_properties.py` (pure-function properties + `settle_incoherent` over GENERATED
   registries — synthetic axes/constraints from `tests/pbt.py`) and `tests/test_reconcile_model.py`
   (a stateful model driving the real set_mode/expiry/probe/publish pipeline — including the box
   mirror tier via box_up/box_down rules — through generated operation sequences). Convention: a new property must be validated **red first** — mutate the
   code under test, watch hypothesis shrink a counterexample, revert (each test section notes its
   mutations). **Commit everything BEFORE mutation-testing**: the natural revert is
   `git checkout -- <file>`, which wipes any uncommitted work in that file along with the
   mutation — a mutation round on a file carrying uncommitted changes has destroyed them once
   already. Git cannot help here: it cannot tell the mutation from your work (both are just
   unstaged changes to that file), so `stash` is no safer than `checkout` — stashing first
   mutation-tests code without the change you are validating, stashing after bundles the two
   back together. Revert with the exact INVERSE edit instead, leaving git out of it;
   committing first is then the fallback, not the mechanism. (Nor does `&&`-chaining the
   revert save you — it only guards the round where the mutation step itself failed.) There
   is no bare `python`/`python3` in a dev box — `uv run python`. Profiles live in
   `conftest.py`: CI (`CI` env) is derandomized so `check` never
   flakes; local runs stay randomized. Keep strategies as plain data (hypothesis shrinks data,
   not closures).
2. **Engine verbs (golden tests)** — mock the engine; assert the exact `docker`/`podman`
   command *sequences* foldyard emits. No real engine.
3. **Live smoke (e2e)** — drive the REAL machinery on a real engine, opt-in (`FOLDYARD_E2E=1`):
   - `tests/test_e2e.py`: the real CLI (`foldyard up`/`ps`/`down`) against the
     [example consumer](./example/), asserting the api serves a DB-backed request
     (`just foldyard test -k e2e`). Isolated by the example's own `fyex` project.
   - `tests/test_proxy_e2e.py`: a real `mitmdump` + the `egress_proxy` addon + a fake minter + a
     CA-trusting `requests` client — host-side header rewrite, network log, 401 re-issue.
   - `tests/test_proxy_box_e2e.py`: the **proxy plugin driving a REAL dev-box container** over
     the socket — runs in an adapted topology so it works inside a dev box; see
     [docs/nested-virt.md](./docs/nested-virt.md). Both proxy e2es:
     `just foldyard test-proxy-e2e` (pulls the `e2e` group).
4. **Host-only / "Mac-only" paths** — `machine ensure|recreate`, `box up|build`, `host`,
   `mode set`. These deliberately **refuse to run inside a dev box** (`config.in_box()`
   guards — the box must not manage its own VM or escalate its posture), and creating a
   machine/box from inside the live box would collide with it. Validate them on a real host,
   or headlessly via the nested-virt rig: [docs/nested-virt.md](./docs/nested-virt.md).

**In-box validation you CAN do:** `fy verify`, `fy ps/down/up/logs`, the e2e tiers above.
**CANNOT from inside the box:** `fy host` (real daemons), `fy mode <set>` (authoritative
state), `fy tui`, real credentialed modes, and box creation (`fy box up`/`build` would
recreate the running box; `fy box ps`/`shell`/`down` are safe).

### Repo config is never live input to the host

`foldyard.toml` is inside the mount, so anything that can write the checkout can write it — and the
host's reading of it decides where credentials get injected and what egress goes undecrypted. The
supervisor therefore reconciles from the copy an operator ADOPTED (`configpin`, ADR-0022), and a
tree edit is inert until adopted. Two rules follow when you touch this area:

- **Host-side config reads go through `devmode.worktree_config()`** (which returns the adopted
  toml), not `config.resolve(repo=…)` / the ambient parse. Adding a new host-side reader that
  resolves the tree directly re-opens the channel for every field at once — which is exactly how
  `[proxy] passthrough` and `[[inject]].host` stayed live after four rounds of per-field fixes
  ([ADR-0022](./docs/adrs/0022-host-runs-the-adopted-config.md) — the channel, not the field).
- **Reporting is part of the fix.** Pinning makes an edit silent by construction, so anything that
  ignores repo config must SAY so — the tick's one-per-change log line + edge notification and the
  `adopted config` doctor row exist so "my config change did nothing" is never a mystery.

### Golden tests must never see a live engine socket

Tier-2 fixtures use a REAL consumer's project prefix, and inside the dev box
`unix:///var/run/docker.sock` is the machine's LIVE engine — reachable by the box's installed
podman-remote via the stack-exported `CONTAINER_HOST`. In 2026-08 a single engine probe that
escaped the reconcile tests' mocks `podman rm -f`'d the box's entire running dev stack while
every test passed green. Invariants: fixture `DOCKER_HOST` values must be dead paths
(`unix:///nonexistent/…`, never a plausible socket), and a fixture that lets a verb execute
(`up`/`down`/`reconcile_posture`) must stub `stack.subprocess.run` itself — stubbing only a
higher layer (e.g. `devmode.run_stream`) leaves the direct-engine helpers live. When touching
stack verbs, canary-check from the box with the active provider's signature. For Docker Compose:
`docker run -d --name fy-canary --label com.docker.compose.project=<project> --label
com.docker.compose.config-hash=fy-canary <any-image> …`. For podman-compose: `podman run -d
--name fy-canary --label com.docker.compose.project=<project> --label
io.podman.compose.project=<project> <any-image> …`. Run the suite once in each provider direction,
confirm the matching canary survives each run, then remove it.

## CI (`.github/workflows/foldyard.yml`)

Tiers 1–3 run on GitHub-hosted `ubuntu-latest` runners — **all of it on containers, none on
KVM**, which is the key point: the egress-proxy box e2e needs a container engine and a test
running *inside* a container, not a VM, and Docker is preinstalled on Linux runners. Two jobs:

| job | what | engine |
| --- | --- | --- |
| `check` | `just foldyard check` — ruff + pyright + ty + the unit/golden/TUI suite. typecheck installs the `e2e` group so the opt-in proxy/box e2e files (which import `cryptography`/`requests`) resolve | none |
| `live-e2e` | all the live e2es (`-k e2e`: the example stack up→serve→down, the in-process proxy e2e, AND the box e2e) — run **inside a docker-CLI container** that mirrors the dev box (see below) | runner Docker |

**Why `live-e2e` runs inside a container.** The box e2e (`test_proxy_box_e2e.py`) spawns a
*sibling* box and must discover its own network — so the test process itself has to be in a
container, exactly as in the dev box. The job's `docker run` reproduces the dev-box environment
precisely: **docker CLI only** (no podman ⇒ foldyard's `_engine()` picks docker ⇒ `docker compose`,
not the `podman machine` VM path that a bare runner would take); the **host socket mounted** +
`IN_DEVBOX=1` + `DOCKER_HOST` set (⇒ `in_box()` true ⇒ the launch gates — preflight's backend
check, the config-adopt gate — and `machine.ensure` all short-circuit; no `/dev/kvm` needed); and
the **repo mounted at its same host path** (so the box's `-v <CA>` mount, which the daemon resolves
on the host, points at a path that exists there — the test copies the CA under the repo for this).
A `docker compose` v2 plugin binary is dropped in for the example stack.

**Why no KVM job.** The only foldyard surface that needs `/dev/kvm` is the `foldyard machine`
(podman-machine VM) lifecycle — tier 4. Raw `/dev/kvm` *is* present on GitHub's Linux runners
(the android-emulator action relies on it, via a `udev` rule), but full podman-machine / libvirt
VMs are flaky there (nested-virt limits — see
[josecelano/github-actions-virtualization-support](https://github.com/josecelano/github-actions-virtualization-support)),
so that path stays the Mac / nested-KVM-host recipe in
[docs/nested-virt.md](./docs/nested-virt.md) rather than a CI job.

## Conventions & gotchas

- **Three surfaces TEACH, and none of them fails when it lies:** the bundled skills
  (`assets/skills/`, installed into a consumer's `.claude/skills/`), the `fy init` scaffold, and
  the docs `fy docs` ships. `tests/test_agent_guide.py` is the mechanical guard — every ``fy …``
  command they name must resolve in the live typer app, the scaffold must not offer a key from
  `exposure.IGNORED_KEYS`, and a `fy docs <topic>` they cite must be in the wheel's force-include
  list (running from source hides that: the topic resolves for you and 404s for consumers). Write
  commands in backticks or the guard can't see them. **The skills must not restate `--help`** —
  duplicated verb lists are the part that rots; teach the model, then point at the CLI.
- **The box HAS podman** — so "am I on the host (can manage the VM)?" must be tested with
  `config.in_box()` (`IN_DEVBOX=1` — a preset docker-compat DOCKER_HOST is deliberately NOT enough,
  Linux/WSL2 hosts export one too), NEVER `which("podman")`.
- **Write "host", not "Mac" — in new code, docstrings, messages and docs.** foldyard's split is
  host vs box, and the host being a Mac is a fact about today's users, not about the design (there
  is no platform branching in the package: no `sys.platform`, no `Darwin` test; the sweep of the
  ~700 legacy mentions and Linux/WSL2 host validation are still pending). Say **macOS** only
  where the claim really is macOS-only — `brew`, the login keychain, `security add-trusted-cert`,
  Virtualization.framework/`vz`. One trap: **`host` already means an
  egress HOSTNAME** across the allowlist/proxy surface (`fy allow add <host>`, `[proxy] recommend`,
  `_allowed(host)`), so near that code prefer *host-side*, *the operator*, or *the host machine*
  rather than writing "the host offers the host".
- **`CONTAINER_HOST` is load-bearing** — plain podman's local (nested) mode is broken in the
  box; it only works because foldyard emits `CONTAINER_HOST` (and passes `-e CONTAINER_HOST`
  at box-create).
- **Nested Podman in the box is only partial** — `crun` can't set up a container in-box
  (`/proc/sys` read-only, no devices). foldyard doesn't need it: test with the mocked engine
  (golden tests) or the real socket.
- **Two "repo roots":** `stack.main_repo()` = `git --git-common-dir` (MAIN checkout, even from
  a worktree); `config.repo_root()` = the `foldyard.toml` marker (per-checkout). They diverge
  for worktrees, deliberately — engine setup uses main_repo, posture/state uses config.
- **shellenv error propagation:** `eval "$(foldyard shellenv)"` can't fail the recipe via exit
  code, so on a fatal error shellenv prints `exit 1` to **stdout** (eval runs it) + the reason
  to stderr. Recipes have no `set -euo pipefail` of their own — shellenv emits it.
- **`just` modules:** inside a module file `justfile_directory()` is the **root** justfile's
  dir; use `source_directory()` for the module's own dir.
- **`shlex.quote` leaves safe strings unquoted** (`APP_PORT=3007`, not `'3007'`) — golden
  tests must not assume quotes.
- **`machine.state()` is a lifecycle FLAG, not liveness** — it survives the VM dying underneath
  it. A host crash (or a guest that never signals ready) leaves `running` set while podman has
  torn down the api socket and the network forwarder, and left the hypervisor process orphaned:
  every engine call then dies on a raw `dial unix …: no such file or directory`. Anything
  deciding "can I reach the engine?" must pair it with `machine.responsive()` (a real connect to
  the socket — `exists()` would pass on the stale socket file a killed process leaves). `ensure`
  recovers this itself (stop → `reap_orphans` → start); `reap_orphans` matches processes by
  podman's OWN recorded paths (disk image / EFI store / api socket), never by machine name —
  name matching would let `acme`'s `fy up` kill `acme-two`'s VM.
- **Port offsets:** `stack._offset` shells to system `cksum` for exact parity with the
  original shell implementation.
- **Module-level constants** (`devmode.AXES`/`MODE_BLURB`/`AXIS_DAEMON`/`EMERGENCY`,
  `machine.MACHINE`) bind at import from `config`/the registry. Tests **monkeypatch the module
  attrs**; `config.repo_root`/`_toml` are `lru_cache`d → tests `cache_clear()` (see
  `conftest.fresh_config`). `plugins.registry()` is also cached — tests build a
  `Registry([...])` directly or `load_plugins(extra=…)` rather than mutate the live one.
  `conftest` pins `FOLDYARD_ENGINE=podman` (autouse) for determinism.
- **`fy down` ends with "Network … still in use"** — expected (the dev box is on that
  network); containers are still removed. Not a regression.
- **`fy up` reclaims disk before building, and ONLY under pressure** (`stack.reclaim`, gated on
  `DiskHeadroom.low` — the same threshold the `engine disk` doctor row warns at, deliberately
  shared so the two can never disagree). Nothing accumulates a reclaim otherwise: a healthy
  store sees no destructive verb at all. Three properties are load-bearing if you touch it.
  *Plain `prune`, never `-a`*: podman's "dangling" means untagged AND not the parent of another
  image, so the layer cache (every intermediate is a parent) is structurally out of reach — a
  prune between two builds still yields `--> Using cache`; `-a` drops every image no container
  uses — the untagged ones a plain prune spares AND the tagged base images — turning a reclaim
  into a re-pull through the egress wall.
  *`until=` is not optional*: a build in flight commits untagged, childless (= dangling) layers,
  so on a box shared by several agent sessions an unguarded prune deletes another session's
  build out from under it. *Removed worktrees' images are a separate sweep* — they're TAGGED
  (`{prefix}-{wt}_{svc}`), so prune can never see them, and `worktree remove` doesn't drop them
  either (it can't: reaping images is exactly the thing that must not race a live build). The
  parse is by rendered service name, both separators, keeping anything ambiguous — see
  `_orphan_project_images`, and note `{prefix}-{service}` (main, docker spelling) has the same
  shape as `{prefix}-{worktree}-{service}`.

## Don't-break list

- The hot path stays venv-free / Textual-free. **`verify` stays the credibility check — never
  weaken its assertions.** Keep echoing the underlying `"$ENGINE"` commands.
- **Don't stop emitting `CONTAINER_HOST`, and don't make `engine()` "prefer docker" again** —
  in-box `up`/`ps`/`verify` all depend on podman reaching the socket via it.
- **No host-executed code from the repo mount, and no command strings from config.** Minters are
  packaged KINDS (`Plugin.secrets` declares what host.env must hold; a `[[secret]]` `how` hint is
  PRINTED, never run). Don't reintroduce a `minter = "<command>"` config key or a minter path under
  `dev_vm_dir()` — that is host code execution for anything that can write the checkout, which is
  what [ADR-0023](./docs/adrs/0023-no-host-executed-code-from-the-repo-mount.md) closed. A new
  mechanism = a new kind, or an
  entry-point plugin. Consumer scripts run in a CONTAINER, never on the host —
  `[project].worktree_init` via `worktree._init_in_yard` (skips with a retry hint when the yard
  isn't ready, never falls back), and the vscode attached-config generator via
  `vscode._generate_attached_config`, which prints a JSON document that foldyard sanitizes with an
  ALLOWLIST (`_ALLOWED_CONFIG_KEYS`) before doing the one host-side write. Keep it an allowlist:
  VS Code's `initializeCommand` runs on the HOST, and a denylist fails open when the schema grows.
- **Two different env sets on an `InjectRule`.** `requires` gates the whole proxy daemon's spawn
  (so it must never carry one mechanism's credential — a proxy that won't launch
  connection-refuses every box request under always-route); `env` is what that rule's minter may
  READ, since the addon runs minters with a base env plus those names instead of inheriting the
  supervisor's environment, which holds every axis's host.env secret.
- **A failing minter must never log at ERROR during proxy STARTUP** — mitmproxy's `ErrorCheck`
  addon exits the process ("Error logged during startup, exiting…") on any ERROR logged while
  starting, and the supervisor respawns it, so one rule whose host.env secret is missing
  crash-loops the proxy and cuts ALL egress for that checkout (box sessions see API errors for
  Claude/Codex/etc., whose credentials were fine). This is the same failure `requires` is kept
  credential-free to avoid, reached by a different door. `running()`'s warm-up therefore calls
  `rule.token(warm=True)`, which reports at WARN; the request path — past the startup window —
  keeps ERROR. Pinned by `test_warm_up_mint_failure_stays_below_error`. Any new startup-time
  work in the addon inherits this constraint.
- **Egress grants live in the host store at EVERY level** (`allow-store.json`) — never in
  `foldyard.toml`. Repo config travels with the branch and the box can write it, so a committed
  allowlist lets the yard widen its own wall.
- **Plugins stay stdlib-only + import-light** (`plugins.registry()` loads on the hot path).
  Don't register the built-ins as entry points (double-load → duplicate-axis error). When
  adding a plugin hook, thread it through the `Registry` — never let `devmode`/`verify`/`tui`
  name a specific credential mechanism again.
- **Box creation (`fy box up`/`build`) can't run in-box** — it would recreate the running box.
  `box.py` is golden-tested for command shape; exercise real `up`/`build` on a host or the
  nested rig.

## The example consumer

[`foldyard/example/`](./example/) is the minimal "bring your own stack" fixture — its own
`foldyard.toml`, a `compose.yml` (Postgres + a FastAPI api), an `api/` image, and a minimal
`box.Dockerfile`. It is both the docs fixture and the workload the nested environment runs.
`tests/test_example.py` guards its config from bitrot. See [example/README.md](./example/README.md).
