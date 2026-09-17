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
  backend contract (podman | lima; see `docs/lima-backend-scope.md`).
- `guestlog.py` — the VM's log budget (`machine ensure`, every VM backend): journald cap as root
  (Lima: rendered into the boot script; podman machine: `sudo -n` over ssh) + the rootless API
  service's log level as a user drop-in over ssh. Best-effort — a warning, never an abort.
- `hostwall.py` — the host-side egress wall for the machine VM on Linux: nftables matched by the
  VM's cgroup v2 scope (the VM is started in a per-VM systemd scope so the match is predictable),
  wired via `[machine].host_wall`; fail-closed — a VM outside its own scope is refused, not
  walled. The VM's loopback plumbing (Lima's host resolver, the SSH forward) is discovered from
  its processes' sockets, never guessed.
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
4. **The host tier** — `machine ensure|stop|recreate`, `box up|build`, `host`, `mode set`,
   `verify`'s real VM boundary, the walls. These deliberately **refuse to run inside a dev box**
   (`config.in_box()` guards — the box must not manage its own VM or escalate its posture), and
   creating a machine/box from inside the live box would collide with it. They have a REAL VM
   in CI — the `lima-host-e2e` job (below), and the same VM inside WSL2 on a Windows runner
   (`wsl2-host-e2e`) — and live as `tests/test_*_e2e.py` modules over the
   shared substrate `tests/e2e_host.py` (gated: `FOLDYARD_E2E=1`, not in a box, `limactl` on
   PATH; each module takes a throwaway example copy, leaves the VM running and un-walled):
   - `test_verify_e2e.py` — ALL PASS on the boundary foldyard builds, **FAIL against a VM
     mounting the operator's whole home**, PASS again once the mount is gone (the negative
     [docs/verify-false-pass.md](./docs/verify-false-pass.md) owed). Never weaken this one.
   - `test_probes_e2e.py` — the read-only engine probes in-process (`devmode.workspaces` /
     `up_worktrees` / `_stack_mounts` / `_stack_shadow_check`, `stack.disk_headroom`,
     `machine.state/socket/responsive` with the moved-socket invariant, `reconcile.scopes()`),
     plus `fy state` and `fy doctor` — the output that drifts between podman versions, which the
     hermetic unit suite cannot see.
   - `test_machine_e2e.py` — ensure idempotent; stop stops the supervisor (heartbeat stale) and
     keeps the VM; a SIGKILLed hypervisor recovered by ensure (the flag-is-not-liveness item
     below); recreate. Its copy lives UNDER THE HOST HOME (`~/fy-e2e/machine/`, left in place)
     and the VM is recreated from it first, so every restart runs with a repo mounted at
     `/home/<user>/…` — the realistic Linux layout the open home-mount finding in
     [docs/linux-support.md](./docs/linux-support.md) needed exercised.
   - `test_reclaim_e2e.py` — `fy reclaim` on a real store: a removed worktree's tagged images
     (both provider spellings) swept, the main image + base images kept, the next `up` still
     `Using cache` (the three reclaim properties below, live).
   - `test_worktree_e2e.py` — `fy worktree add` (registered, own branch, clean tree), its stack
     up beside main's, `remove`: containers + volumes gone, the bound-out transcript ARCHIVED
     before the tree is deleted, main untouched, the branch kept, local state dropped.
   - `test_wall_e2e.py` — `[machine].wall` + `host_wall` via `fy up`: the host table on the VM's
     own scope, direct guest egress refused, DNS resolving, the proxy the way out, the api still
     served, and the stale-provisioning refusal.
   - `test_host_daemons_e2e.py` — the supervisor with the zero-secret rig
     ([docs/testing-modes.md](./docs/testing-modes.md)): mode on → fake minter up + the overlay
     re-rendered, blocked-daemons empty; mode off.
   - `test_box_e2e.py` — `fy box up` on the VM (recreated to mount the module's copy), in-box
     `fy ps` over `CONTAINER_HOST`, in-box `fy verify` ALL PASS, `box down`.
   On a Lima host these run against the example's own VM (creating, restarting, recreating it),
   never a consumer's. What the runner cannot reach — nested virtualisation for the gVisor
   posture — stays the recipe in [docs/nested-virt.md](./docs/nested-virt.md).

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

**The suite is hermetic by construction, not by convention** (`tests/conftest.py`, the "hermetic
subprocess" section; pinned by `tests/test_hermetic.py`). Every non-e2e test runs on a PATH
scrubbed to `git`/`cksum`/the shell/the interpreter — a host tool named bare is *not found*, as on
a CI runner, so the code under test takes its "not installed" branch rather than the live
machine's — and `subprocess.run`/`Popen` refuse to execute anything else reached by absolute path
or a caller's own `env["PATH"]`, failing the test by name with an exception no `except Exception`
can swallow. The shell is allowlisted for `bash -n`, not for a program (`bash -c …`/`sh script`
carry one past argv[0] — refused like `shell=True`). A test that means to run a host binary
marks it (`@pytest.mark.spawns("/abs/path")`, or `spawns("bash")` to run a program under that
shell); one that needs a real tool is an e2e test (`tests/*_e2e.py`, exempt). Before this, `set_mode`
round trips were running `limactl list` + `podman ps` against the live machine, and the
supervisor's blocked-daemon push reached a real Notification Center — green in CI only because
those binaries are absent there.

**`just census` is the report over that gate, not a gate itself** (`tests/tools/census.py`, a
`-p` plugin the recipe loads): every process the suite spawns, binary × test, aggregated across
the xdist workers — what the allowlist still lets through (`git` from the git-shim tests, `cksum`
from the port-offset parity test, the shells only from the guard's own tests) and, with
`FOLDYARD_E2E=1 just census tests/test_*_e2e.py` on a Lima host, what the live tiers reach.
Arguments pass to pytest (paths, not a quoted `-k` — `just` splits on whitespace);
`CENSUS_TESTS=1` lists the tests under each binary.

## CI (`.github/workflows/foldyard.yml` + `foldyard-e2e.yml`)

Tiers 1–3 and the host tier all run on GitHub-hosted runners — Linux, and the host tier once more
inside WSL2 on a Windows runner. `foldyard.yml` is the fast gate on every push; `foldyard-e2e.yml`
holds the advisory live tiers, opt-in (`main`, a `[run-e2e]` commit message, or a dispatch). All
four jobs use standard runners, which GitHub bills nothing for on a public repository. Four jobs:

| job | what | engine |
| --- | --- | --- |
| `check` | `just foldyard check` — ruff + pyright + ty + the unit/golden/TUI suite. typecheck installs the `e2e` group so the opt-in proxy/box e2e files (which import `cryptography`/`requests`) resolve | none |
| `live-e2e` | the in-box topology: all the live e2es (`-k e2e`: the example stack up→serve→down, the in-process proxy e2e, AND the box e2e) — run **inside a docker-CLI container** that mirrors the dev box (see below) | runner Docker |
| `lima-host-e2e` | the HOST topology: `ubuntu-24.04` as a real Linux host running foldyard's default `lima` backend — `machine ensure` boots a QEMU/KVM VM on the runner, then `tests/test_e2e.py` drives the real `foldyard up` / worktree lifecycle through the config-adopt gate, the supervisor and compose, exactly as on an operator's machine | Lima VM (podman in the guest, over the forwarded socket) |
| `wsl2-host-e2e` | the same host tier on `windows-2025`: an Ubuntu 24.04 distro under WSL2 ([Vampire/setup-wsl](https://github.com/Vampire/setup-wsl)) is the host, and the `lima` backend boots the QEMU/KVM VM INSIDE it — a second level of nesting the hosted Windows runners expose. Same module list; `test_wall_e2e.py` skips itself there (below) | Lima VM inside the WSL2 distro |

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

**Why a VM job works on a shared runner (2026-09-17).** Until then this page said podman-machine
/ Lima VMs were "flaky" on GitHub-hosted runners; the only citation was a *libvirt* permission
failure, and foldyard's lima backend does not use libvirt. `/dev/kvm` is present on the x86 Linux
runners (under-documented, but Lima's own CI boots QEMU VMs on `ubuntu-24.04` on every PR with the
recipe `modprobe kvm; chown $USER /dev/kvm` — group membership does not take effect there). A
10-attempt spike of the real `lima` backend on `ubuntu-24.04` went 10/10 with no retry wrapper,
with and without the walls, QEMU start → READY in 29–41 s and the whole attempt under 2.5 min;
the record is in [docs/linux-support.md](./docs/linux-support.md#validated-on-a-linux-host). The
job creates the VM once from a throwaway example copy before pytest (the example stack has no bind
mounts, so every test copy can drive the one VM) and exports its socket as `CONTAINER_HOST` — for
the tests' OWN engine calls; a preset `DOCKER_HOST` would make the CLI under test skip
`machine.ensure` (`stack._docker_host`: dev-box semantics) and bypass the very lifecycle the tier
drives. The tests' `foldyard up` then runs the real host path: machine ensure → adopt gate →
supervisor → compose. First catch of the tier: podman 4.9.3's `ps --format` (Ubuntu 24.04's
package) has no `{{.Label "k"}}`, so every probe built on it read "engine unreachable" on a
Linux host — now `{{json .Labels}}` (`devmode.ps_labels`).
Things a Linux runner needs that a Mac does not: `qemu-img` (from `qemu-utils`, not implied by
`qemu-system-x86-core`), a `systemd --user` manager for anything scoped (`loginctl
enable-linger`), and Lima from the release tarball into `/usr/local`. **arm64 runners have no
KVM** (`ubuntu-24.04-arm`: no `/dev/kvm` before or after `modprobe kvm`, probed 2026-09-17), so
the job is x86-only. The `podman` backend (podman-machine) has not been tried on a runner; the
`lima` backend is the product path and the one tested. The Mac / nested-KVM-host recipe in
[docs/nested-virt.md](./docs/nested-virt.md) remains for what a VM job cannot reach (the gVisor
posture under nested virtualisation).

**Why the same tier runs inside WSL2 on a Windows runner (2026-09-17).** WSL2's distro is a
Hyper-V guest, so a Lima/QEMU VM inside it is nested twice (Azure → runner VM → WSL2 utility VM →
QEMU). The hosted `windows-2025` runners allow it: with `[wsl2] nestedVirtualization=true` in
`%USERPROFILE%\.wslconfig` (written BEFORE the distro first starts — the file is read at
utility-VM boot; Windows 11 defaults it on, Windows Server 2025 needs it said) the distro has
`/dev/kvm` (`root:kvm 0660`, WSL2 kernel 6.18), and foldyard's unmodified `machine ensure`
boots the Fedora 44 guest to READY in 68 s (47 s on a restart) — Lullabot/sandbar#149 measured
the same on 2026-08-27. WSL2 is only ever the HOST here: a VM-less engine in the distro would be
bare Linux without the VM ([ADR-0027](./docs/adrs/0027-always-a-vm-native-backend-retired.md)).
The job differs from the Linux one in plumbing, not product: job `env:` is Windows process env,
so the variables the in-distro steps read are forwarded by `WSLENV` (`GITHUB_WORKSPACE/up`
arrives path-translated) and state between steps goes through an in-distro env file, never
`GITHUB_ENV`; the action's default distro user is root, so the root work (packages, Lima, uv,
the `runner` user with `NOPASSWD` sudo + the `kvm` group + linger) runs first and the wsl-bash
wrapper is then regenerated for `runner` (a second `setup-wsl` step with `wsl-shell-user`);
the checkout is CLONED from the Windows drive onto the distro's ext4 (objects carry the
committed modes and LF endings — the 9p automount shows 0777 and the runner's git has
`core.autocrlf`; a local clone needs a global `safe.directory`, the automount is root-owned and
`upload-pack` runs as a child). Automount stays on because the wrapper reads each step's script
through `/mnt/<drive>`. The action caches the distro installer (372 MB): the first run's 5.5 min
install is 40 s after. **What WSL2 cannot do: the host-side wall.** `[machine].host_wall`
matches the VM by `socket cgroupv2`, and the stock WSL2 kernel has `# CONFIG_NFT_SOCKET is not
set` (both the 6.6 and 6.18 branches), so `nft` refuses the rule with ENOENT — the wall fails
closed, as designed. `test_wall_e2e.py` now probes the kernel for the expression in its gate
(one rule into a throwaway table, `sudo -n`) and skips; before that its fixture had already
re-provisioned the VM walled, and the error left the worktree module refusing the stale
provisioning — 9 errors from one kernel option. The in-VM `[machine].wall` is unaffected (it
runs in the guest). Everything else passed unchanged — 30 passed, 6 skipped; the tier is ~3.5×
slower than on Linux (the test step 43 min vs 12; `test_box_e2e` 8 min, `test_machine_e2e` 9;
the job 47 min against a 75-minute budget). Facts about the
runner worth knowing: 16 GB / 4 vCPU, of which WSL2 takes half the memory; the distro's root
is a sparse 1 TB vhdx on `C:` with ~30 GB actually free; the `ubuntu-24.04-arm` finding carries
over — Windows-on-ARM boots the distro at EL1, no KVM, so this is x86-only too.

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
  ~700 legacy mentions is still pending — Linux and WSL2 hosts are now validated in CI, see the
  two host-tier jobs). Say **macOS** only
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
  isn't ready, never falls back). The VS Code attached-container config is AUTHORED by foldyard
  (`vscode._attached_config`) from the ADOPTED `[vscode]` table — `fy code` runs `configpin.gate`
  and reads through `devmode.worktree_config()` like every other host-consequence verb — with
  `remoteUser` and the daemon-port pin as foldyard facts
  ([ADR-0026](./docs/adrs/0026-vscode-attach-config-is-declarative.md)). Don't reintroduce a
  repo-produced document (VS Code's attached-config schema has lifecycle hooks and
  `initializeCommand` runs on the HOST), and don't source the `extensions` list from mount data
  such as `.vscode/extensions.json`: a UI-kind extension installs into the operator's shared
  `~/.vscode/extensions`, so that list is host code execution by another name for anything that
  can write the checkout.
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
- **Root in the Lima guest is boot-time only — never `limactl shell … sudo` from the host.** The
  VM user is the uid the box runs as, and Lima's cloud-init grants it `NOPASSWD:ALL` on EVERY
  boot (the instance id changes each boot). foldyard's one `provision: mode: system` script
  (`assets/machine-wall/guest-boot.sh`, recorded in lima.yaml by `machine._record_provisioning`)
  narrows that grant to `shutdown` and installs the wall as root at each boot; the host then
  reads the guest's report (`/run/fy-wall/state`, no root needed) and fails closed on a
  mismatch. Anything new that needs root in the guest goes INTO that script (a new rendered
  id ⇒ `fy machine stop && fy up`), not into a new sudo call — a sudo path would hand a
  container escape VM-root again. Lima renders the script as a Go template (`{{.User}}`,
  `{{.UID}}`), so no other `{{` may appear in it, and `bash -n` gates it in the tests.
- **Under the gVisor posture the box mounts the NARROWED socket, never the runsc socket
  directly.** `box.py` mounts `sandbox.box_socket()` (`podman-runsc-filtered.sock`), not
  `guest_socket()` — the filter (`assets/sandbox/socket_filter.py`, a guest user unit provisioned
  by `sandbox.ensure`) strips the runtime opt-out (`oci_runtime` / `dev.gvisor.*` / compat
  `HostConfig.Runtime`) from every container-create so the box can't escape gVisor on podman ≥ 6.
  Reverting the box to `guest_socket()` re-opens that door. The filter is the enforcement tier;
  the runsc-default socket is only a convenience for the HOST's own trusted `fy box up` create
  (which still uses the raw socket). Three invariants if you touch the filter: it stays
  **fail-closed** (an unparseable create body is refused, never forwarded — else a runtime slips
  past the strip), **keep-alive-correct** (it frames every response so a create is filtered even
  as the *second* request on a reused connection — the bypass a "peek at the first request then
  splice" proxy leaves), and **stdlib-only** (it runs under the guest's `python3`; both backends
  have `/usr/bin/python3`). It is foldyard's own packaged code run in the GUEST, not host code
  from the repo mount, so ADR-0023 is not in tension. `tests/test_socket_filter.py` pins the
  rewrite, the fail-closed refusal, keep-alive and the hijack splice; the podman-6 strip effect
  (a client-chosen runtime honoured, then stripped) is only observable there day to day — the
  shipped guests are podman 5.8, which ignores the field; it was shown live once on a podman
  6.1.1 guest (2026-09-13, docs/isolation-layers.md).

## The example consumer

[`foldyard/example/`](./example/) is the minimal "bring your own stack" fixture — its own
`foldyard.toml`, a `compose.yml` (Postgres + a FastAPI api), an `api/` image, and a minimal
`box.Dockerfile`. It is both the docs fixture and the workload the nested environment runs.
`tests/test_example.py` guards its config from bitrot. See [example/README.md](./example/README.md).
