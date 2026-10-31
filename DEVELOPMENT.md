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
just install     # uv tool install --force --editable (per-machine venv)
just test [args] # the suite (add -n auto to parallelise)
just run <verb>  # run the CLI from source without installing
just lint        # ruff check + format --check   (auto-fix: just format)
just typecheck   # pyright + ty (both, run together)
just check       # lint + typecheck + suite — what CI gates on
just prune       # list merged .claude/worktrees, local branches, orphaned dev venvs (--apply removes)
```

**Tooling (dev deps, in `pyproject.toml`):** ruff (lint+format, line-length 100), pyright +
ty (both type-checkers — keep green under each), pytest-xdist (`-n auto`). ruff config
carries the deliberate ignores (✓/✗/▶ glyphs = RUF001-003; typer `Argument`/`Option`
defaults = B008; Textual mutable class-attrs = RUF012 in tui; box.py embeds long shell =
E501). Keep all three at zero before committing (`just check`).

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
- `composeguard.py` — refuses compose paths that would make the HOST-side compose client read
  outside the checkout (`env_file`, build contexts/Dockerfiles/additional contexts/ssh,
  `include`/`extends` targets, secrets/configs files): the rendered config (compose's own
  interpolation) plus a raw scan of what rendering consumes. Asked by every compose call that
  creates or builds (`stack._escapes`), host-only. Interim — a key list, and racy against a box
  that swaps a checked file for a symlink; the structural fix runs compose in the VM. Its sibling
  clamp: `stack._host_passthrough`, the compose client's allowlisted env.
- `confighash.py` — the stack scope's verdict: the compose provider's own per-service config
  hash vs a fresh render (what `up` compares). podman: a `python -m foldyard.confighash`
  subprocess drives the bundled podman-compose's parser (its internals — degrades to
  "unavailable", never a guess; `tests/test_confighash.py` pins the contract); docker:
  `config --hash '*'`. The live oracles are `test_probes_e2e.py` / `test_e2e.py`.
- `verify.py` — the isolation battery, the product's credibility check; plugin-agnostic core +
  the registry's `verify_checks`.
- `machine.py` / `machine_backend.py` — rootless dev-VM lifecycle behind the pluggable
  backend contract (podman | lima; see `docs/lima-backend-scope.md`).
- `boxmounts.py` — ADR-0033's two foldyard-owned directories beside the checkout, lima only:
  `<state dir>/to-box` (written by the host, shared READ-ONLY with the VM and bound `:ro` at
  `/run/foldyard/to-box` in every box — only on vz+virtiofs / qemu+9p, the `mountType` pinned
  with the vmType, since read-only must hold outside the guest) and `<state dir>/from-box`
  (box-writable, a slot per checkout: `main/`, `worktrees/<n>/`; the agents' transcripts bind out
  there). `machine._record_mounts` records the two on a stopped VM and refuses a running one that
  lacks them, from the instance's EFFECTIVE config (`limactl list`'s merged `config`); the
  checkout mounts are left as created. The podman backend's mounts are untouched. Host writes
  into the from-box go through `mountwrite.makedirs` (no `mkdir(parents=True)`), reads through
  the transcript sync's no-follow walk, deletes through `boxmounts.remove_slot`.
- `basevm.py` — the project-less BASE VM (`fy machine base`, `fy prereq --fetch`): Lima's podman
  template, nothing mounted, booted once, the packaged box image built in it, stopped, stamped in
  `~/.foldyard/base-vm.json` by its INPUTS (resolved template · vmType · packaged-box
  fingerprint) + age. `LimaBackend.create` clones a new VM from it when it fits (else the
  template, as before) and `box up` re-tags its box image (`box._from_base_image`). A clone
  happens only at creation, the base is never made from a project VM, and an unstamped
  `foldyard-base` instance is never touched. Lima 2.1.3's clone leaves `sock/` mode 0000 —
  `clone` resets it. See docs/base-vm.md.
- `prereq.py` — `fy prereq`: project-free host checks (pure `rows(Host)` over gathered facts,
  capability-detected, install hints per package manager present); `--fetch` bakes the base.
- `monitor.py` — the guest monitor (`[machine] monitor = "observe"`, ADR-0032): a root Tetragon
  in the lima VM, provisioned by a SECOND recorded boot script under its own marker
  (`# fy-monitor <id>`, beside `# fy-provision <id>`, so it never makes the wall script's id
  stale); the pinned release is host-downloaded, streamed into a VM-user inbox and installed by a
  root `.path` unit that re-checks the root-owned copy; the guest reports in `/run/fy-monitor/`.
  Observe only — a monitor that isn't observing warns, never aborts. Attribution is NOT
  Tetragon's container id (forgeable — docs/ebpf-monitoring-spike.md): mount-namespace inode vs
  the outermost `libpod-<id>.scope`.
- `monitorlog.py` — the guest monitor's events on the host: the PULL (a supervisor thread; one
  ssh as the VM user fetches the root relay's spool — `assets/monitor/relay.py` — in BYTES after a
  cursor, verifies each line's HMAC and per-boot sequence, stores what verifies, records gaps and
  forgeries, asks the engine once per new container for its name/compose project, recorded as
  claims) and the JOIN (pure, at read time: an event takes the snapshot before it if that still
  holds its mount namespace, else the one after; never carried past a snapshot that dropped it —
  namespace numbers are reused, the lowest free one handed to the next namespace made. Where the
  two disagree, the containers' own init marks between them — the old init leaving, the new one
  entering (`monitorstore.init_mark`) — choose between the two containers the snapshots named,
  never a third (`_handover`; cabash, 2026-10-23). What no container holds is named from kernel
  facts, never a binary's name (`_Facts`, `monitorstore.ns_fact`): `podman` (the namespace a
  process enters straight from the VM's), `build` (an init podman itself starts there), `vm`
  (also the VM's own user namespace); a container's name holds only within its init's life
  (`_lifetime`, paired by the init's pid namespace), only inits from podman's namespace count (a
  container can make its own), and a process crossing into the VM's or podman's namespace from a
  container's is `crossed`. Any stretch is joined with the catalogue's snapshots and facts around
  it (`join`), never only those inside the bytes read: under load two snapshots are tens of MB
  apart, and a byte window once silently dropped every event between them (2026-10-21).
  `fy monitor` prints it (`--since/--until` reads the whole store); doctor's `monitor events` row
  reads the pull cursor. The relay only signs and snapshots — attribution lives here so it changes
  with a foldyard upgrade, not a VM restart.
- `monitorstore.py` — that store on the host: segments named by their first line (a position is
  `[segment id, uncompressed offset]`, surviving the seal and the gzip — never an inode), sealed
  at 32 MB and compressed (~30×), kept within a HOST-ONLY disk budget
  (`<state dir>/monitor-store.toml`, never `foldyard.toml`: the box mustn't shorten the evidence
  about itself), and a SQLite catalogue (derived; rebuilt from the segments) of every snapshot,
  container-init mark, namespace fact (`ns_fact`: an init's mark with where it came from and its
  pid namespace, podman entering a namespace from the VM's, and anything entering one from
  elsewhere — each classified when indexed and stored with its kind, so a read and retention need
  only the newest few of some kinds per namespace), engine claim and segment span. The marks and
  the facts sit in tables BESIDE schema 2, never in it, each with its own progress: a supervisor
  still on 0.6 (or on the marks alone) after an editable upgrade would drop a catalogue whose
  schema it doesn't know, and fail every write to a table with a column it doesn't know. The pull
  indexes a record before writing it; every reader catches the catalogue up first.
- `monitorhooks.py` — the operator's hooks over those events: host-only config
  (`<state dir>/monitor-hooks.toml`, NEVER `foldyard.toml` — a command there is host code
  execution for the box, a url an exfiltration channel), settled events only (a later snapshot
  ANYWHERE in the store, else the settle window), at-least-once with a per-hook cursor, backoff and
  a bounded give-up, every batch's `gaps` saying what it can't vouch for (rotated, transport,
  relay, unsettled, ambiguous, skipped) and `passed` counting by `who` what its filters left out
  (a hook without `who` skips `vm` and `podman` — `skip_unless_asked`; a `crossed` event reaches
  every hook), commands run with a minimal environment (never
  the supervisor's: it holds every host.env secret) on their own supervisor thread. The design
  they plug into — storage tiers, source filtering, score → decide → review — is
  [docs/monitor-pipeline.md](./docs/monitor-pipeline.md).
- `monitorsink.py` — the hooks' built-in `file` sink: an append that walks the path from `/` with
  `O_NOFOLLOW` per component (a link is followed only when root owns it in a directory the operator
  can't write — the box writes the checkout AS the operator), each batch one line HMAC-signed with a
  host-only key (`monitor-sink.key`, never the relay's), numbered and chained; `verify` is
  `fy monitor check`. `monitorhooks` also edits `monitor-hooks.toml` for `fy monitor hook add|remove`
  — textual, proven by re-parsing, refusing what it can't change exactly.
- `guestlog.py` — the VM's log budget (`machine ensure`, every VM backend): journald cap as root
  (Lima: rendered into the boot script; podman machine: `sudo -n` over ssh) + the rootless API
  service's log level as a user drop-in over ssh. Best-effort — a warning, never an abort.
- `hostwall.py` — the host firewall for the machine VM on Linux: nftables matched by the VM's cgroup
  v2 SLICE (the VM is started in a per-VM scope under a persistent per-VM slice, so the match is
  predictable and its cgroup id survives restarts), wired via `[machine] host_firewall`; fail-closed
  — a VM outside its own scope-under-slice is refused, not walled. Boot-stable: the VM's loopback
  plumbing (Lima's host resolver, the SSH forward) is judged on the INPUT hook by the listener's
  cgroup, never named. **foldyard never loads it** (ADR-0028): `fy machine host-firewall` renders
  the table + a system unit and prints the `sudo` lines, the operator runs them once, and every
  `fy up` PROBES enforcement from inside the slice — the table can't be read without root, and one
  that exists may hold a dead slice's id (fail-open otherwise).
- `box.py` — the dev-box lifecycle (`fy box build|up|shell|down|ps`) + the monitored bootstrap.
- `crossmask.py` — what a box mounts over OTHER checkouts' `[box].shadow_volumes` dirs (#91): one
  read-only note volume per project (`<prefix>-elsewhere`, holding `RUN-IT-IN-ITS-OWN-BOX`,
  rewritten by `box up`) over each, so an install there fails instead of writing onto your computer;
  the box's own dirs keep their shadow volumes. Worktrees from the host's registry at create, never
  a listing of the mount. Mounts are create-time, so for a box already running `crossinject.py`
  ADDS them (`fy worktree add`, the already-up `fy box up`): foldyard's own helper
  (`assets/crossmask/inject.py`, sent on stdin to the guest's `python3 -I -` over the backend's
  ssh, as the VM user — no root, no sudo) joins the BOX's user namespace and its conmon's mount
  namespace (never `podman unshare`'s: after a `system migrate` the API service's and the CLI's
  differ), `open_tree`-clones the note volume read-only and private, and `move_mount`s it on a
  target resolved with `openat2(RESOLVE_NO_SYMLINKS)`; each attach is re-resolved and compared by
  mount id, and detached again if the box renamed the dir in between. Mount propagation can't do
  this (crun's binds come up private — the spike, 2026-10-27). The target dirs are made host-side
  first with `mountwrite.makedirs`. It refuses, falling back to the nag + recreate, in a box, under
  gVisor (the recorded runtime), for a rootful engine (the box's userns not the VM user's) and on
  ENOSYS. `inspect`'s `Mounts` never shows an added mask, so `_mask_gaps` (the nag, doctor's
  `box shadowing` row, `fy worktree add`'s `_late_boxes`) asks the kernel (`crossinject.check`:
  the helper's `check` mode; in a box, its own `/proc/self/mountinfo`), falling back to `inspect`.
  An added mask dies with a box or VM restart; the next create carries every mask. None of it
  recreates a box.
- `boximage.py` — foldyard's packaged box image: its fingerprint and the build shape a project's
  box and the base VM's share. Imports only `config`, so the project-less bake never imports
  `box` → `machine`, whose `MACHINE` binds at import from the project name (outside a project,
  the working directory's name, which the plain-name check can refuse).
- `supervisor.py` — the host supervisor (`fy host` status · `restart` · `logs`; always detached,
  started with the VM, no `stop`): the ONE host-side process per project running the credential
  daemons (singleton lock, per-worktree listeners, replace-on-launch staleness handling, the
  capability-probe loop, TTL expiry + settle). Each tick publishes a readiness record (every
  job's `up`/`starting`/`blocked`/`failing`); the launch verbs mark the worktree they bring up
  (served before its box exists, with main for builds) and `await_ready` on it, refusing on a
  blocked or failing job.
- `hostclock.py` — your computer's timezone (`TZ`: the `/etc/localtime` link's name after
  `zoneinfo/`, else `/etc/timezone`) and time locale (`LC_TIME`), each pattern-checked, which
  `fy box up` passes to the BOX only (`box._clock_env`; the stack stays on UTC). Host facts, not
  repo config; `[box].env` overrides either.
- `hostgit.py` — your computer's git `user.name` / `user.email`, from the GLOBAL config only
  (run from `/`, never the box-writable checkout's), which the box bootstrap writes into the box's
  own global config where it has none (`box._git_identity_step`) — the layer the box's fresh HOME
  lacks, so the env and a repo's own config still win. Those two keys only.
- `worktree_registry.py` — the host's own record of a project's worktrees (name → real path,
  keyed by the main checkout's path). Written only by `fy worktree add`/`remove`; everything
  host-side that enumerates worktrees (supervisor, git heal, `fy mode`, the TUI) reads it, never a
  listing of the box-writable worktrees root. Beside it (never in it — an older supervisor drops
  an entry it can't read), each registered worktree's PORT OFFSET (`offsets`, via
  `stack.worktree_offsets`): the name's hashed slot while it's free, else a free one, so no two
  registered worktrees share ports — the hash alone put two Tangible worktrees on one proxy port
  (2026-10-26). Pins win and are never handed out; the box gets every offset baked in
  (`FY_WORKTREE_OFFSETS`). Should two daemons still want one port (a pin, `WT_OFFSET`), the
  supervisor runs one and blocks the other (`supervisor._port_sharers`) — never reaps its own.
- `agents.py` — `fy agent start|ls|attach|stop` (docs/agents.md): `fy claude`/`fy codex` in a
  detached tmux session in the box, on foldyard's own server (`tmux -L fy -f /dev/null`, its
  options set by argv on every start — `allow-passthrough off` and `set-clipboard external` keep an
  agent's ordinary output off your terminal and clipboard, so tmux < 3.3 is refused; a DEFAULT,
  not a boundary: the server and the binary are the box's, which can turn them back on). One UUID per session, passed to Claude as `--session-id`, so
  a transcript viewer's id is the attach handle. The host reaches boxes over `podman exec` only (no
  listener); every field a box's tmux reports is box-written — sanitised, never acted on beyond the
  session named. `ls` with no project fans out to each ADOPTED main checkout
  (`configpin.adopted_checkouts`) as `fy agent ls --here --json` in its own process (spawned through
  `selfspawn`: `python -P -m`, relative `PYTHONPATH` pinned, since it runs in another checkout), so each
  project resolves its own VM/prefix/worktrees; a stopped VM is reported, never booted.
  `fy box down` (and the TUI's stop confirm) names live sessions first. Next phase:
  docs/fleet-scope.md.
- `worktree.py` · `transcripts.py` · `tui.py` · `init.py` · `skills.py` · `browser.py` ·
  `vscode.py` · `podman_desktop.py` — worktrees, agent-transcript sync, the Textual TUI,
  `foldyard init`, bundled skills, `fy open`, `fy code`, `fy machine desktop` (an `fy-<machine>`
  podman connection per VM, which Podman Desktop lists side by side; where Podman Desktop is
  installed, `machine.ensure` keeps it current and pins the ssh port — `FOLDYARD_PODMAN_DESKTOP`
  `0`/`1` overrides the detection).
- `selfspawn.py` — how foldyard runs its own modules as a child: `python -P -m <module>`
  (`python_m`), never plain `-m`, which puts the WORKING DIRECTORY first on `sys.path` — and those
  children run in a box-writable checkout (the supervisor keeps `fy up`'s directory, the proxy's
  minters keep the supervisor's). `-P`, not `-I`: a `PYTHONPATH=src` source-tree run must keep
  working. A child run in ANOTHER directory than its parent (`confighash`, in the main checkout)
  takes `selfspawn.env()`: a relative `PYTHONPATH` entry is resolved against the child's directory,
  so it is pinned where the parent resolved it. `tests/test_selfspawn.py` plants a `foldyard/` and
  runs the real child, and fails on any new `sys.executable, "-m"` argv in the package.
- `docs.py` — `fy docs [topic]`: the manual served from THIS install (the consumer subset is
  force-included into the wheel; see pyproject). Version-matched by construction, works with no
  egress — the reason an agent should never clone the project to read about it.
- `configpin.py` — the ADOPTED `foldyard.toml` the host reconciles from (the file lives in the
  mount, so the supervisor reads a snapshot under `~/.foldyard/adopted/<checkout>-<hash>/`
  instead — keyed by checkout PATH, never by anything the config declares): drift detection + diff, the `adopt`/`revert`/`ignore` gate every launch verb runs,
  and `fy config …`. See [ADR-0022](./docs/adrs/0022-host-runs-the-adopted-config.md).
- `exposure.py` — the widenings inventory (`fy config widenings` + its doctor row): what the adopted
  config asks the HOST to allow — `passthrough` (undecrypted) hosts with `@bundle` refs RESOLVED to
  a count, injection targets (armed and latent, asked of the registry so an odd shape like Codex's
  ChatGPT rung reports its real host), agent prompts attributed to the shared vs the gitignored
  file, and `IGNORED_KEYS` — config that reads as a control and is no longer honoured.
- `credscope.py` — what each credential was last OBSERVED to grant (ADR-0031: reported, never
  capped): a kind's probe reads it (`CapabilityProbe.scope`), the supervisor records it per switch
  and credential identity and logs + notifies a change; `fy config widenings`, `fy mode` and the
  TUI read the record offline.
- `experimental.py` — the register of experimental settings: per entry, the config value that
  turns it on, its kind (*may change* — graduates by a decision to freeze it; *unproven in use* —
  graduates on evidence, which without telemetry means a maintainer's own day-to-day use), what is
  unproven, and what would graduate it. `fy doctor`'s `experimental` row and the `*(experimental)*`
  tags in docs/configuration.md derive from it (`tests/test_experimental.py` holds the tags and the
  register together, both ways); a changelog fragment introducing one says `experimental: true`.
- `allowlist.py` · `ports.py` · `preflight.py` · `keyless.py` — the egress allow-store behind
  `fy allow` + the TUI's host grants (all levels host-side), cross-project port-band allocation,
  hard-prerequisite checks before `up`, and the keyless-agent credential taxonomy + host-side
  secret capture (`ensure_cred` for agent creds, `ensure_secret` for declared `[[secret]]` rows).

`plugins/` — the framework (`__init__.py`: `Switch`/`Requires`/`Plugin`/`Registry`, per-consumer
loading; cross-axis coherence is declared as `Requires` DATA on two tiers — in-code
`Switch.requires` for intrinsic couplings, the consumer's `[[require]]` table for
wiring-dependent ones (docs/configuration.md), merged at Registry construction; the
`mode_issues` hook is the escape hatch for logic the data can't express, e.g. combination
warnings)
plus the built-ins: `gcp.py` + `gcp_metadata/`, `proxy.py` + `_passthrough_bundles.py` (the
egress proxy), `inject.py` (each `[[inject]]` row → one switch + one rule; `box_env` dummies and
their verify rows) + `kinds.py` (the credential KINDS a row names — a token protocol with a fixed
field shape, [ADR-0031](./docs/adrs/0031-credentials-are-protocol-kinds-the-credential-owns-its-scope.md)):
`static` → `static_token.py`, `github-app` → `github_app_token.py` (the App installation token,
asking for no `permissions` — the installation's are the scope — + the kind's capability probe),
`gh-cli` → `gh_cli_token.py` (`gh auth token`, emergency-only); minters are packaged because the
mint path runs host-side beside the credentials —
[ADR-0023](./docs/adrs/0023-no-host-executed-code-from-the-repo-mount.md). There is no github
plugin: a `[plugins.github]` table is refused at launch with its replacement rows. `claude.py`,
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
   registries — synthetic axes/constraints from `tests/pbt.py`),
   `tests/test_monitor_properties.py` (a hook's deliveries == the whole-store join, or a `gaps`
   entry says why, over generated streams, rotation and filters) and `tests/test_reconcile_model.py`
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
   flakes; local runs stay randomized; `explore` (`FOLDYARD_HYPOTHESIS_PROFILE=explore`, which
   wins over `CI`) is the weekly randomised hunt in CI, below. A test that sets its own
   `max_examples` wraps it in `pbt.scaled_examples(n)`, or `explore` leaves it at its everyday
   budget. Keep strategies as plain data (hypothesis shrinks data, not closures).
2. **Engine verbs (golden tests)** — mock the engine; assert the exact `docker`/`podman`
   command *sequences* foldyard emits. No real engine.
3. **Live smoke (e2e)** — drive the REAL machinery on a real engine, opt-in (`FOLDYARD_E2E=1`):
   - `tests/test_e2e.py`: the real CLI (`foldyard up`/`ps`/`down`) against the
     [example consumer](./example/), asserting the api serves a DB-backed request
     (`just test -k e2e`). Isolated by the example's own `fyex` project.
   - `tests/test_proxy_e2e.py`: a real `mitmdump` + the `egress_proxy` addon + a fake minter + a
     CA-trusting `requests` client — host-side header rewrite, network log, 401 re-issue.
   - `tests/test_proxy_reload_e2e.py`: the same real `mitmdump` driven by its live file — a
     download survives a settings change on the same process, a tunnel whose host leaves
     `passthrough` is closed, an untouched one stays open.
   - `tests/test_proxy_box_e2e.py`: the **proxy plugin driving a REAL dev-box container** over
     the socket — runs in an adapted topology so it works inside a dev box; see
     [docs/nested-virt.md](./docs/nested-virt.md). The proxy e2es:
     `just test-proxy-e2e` (pulls the `e2e` group).
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
     [docs/archive/verify-false-pass.md](./docs/archive/verify-false-pass.md) owed). Never weaken this one.
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
   - `test_mount_visibility_e2e.py` — the git heal's premise on THIS host's mount (virtiofs on
     macOS, 9p on the Linux/WSL2 runners): a file the VM renames into place reaches the host
     whole, never briefly missing, never older than one already seen (ADR-0021, 2026-09-27).
   - `test_mounts_e2e.py` — ADR-0033's to-box against ROOT in the guest: a test-only boot script
     (own marker, always exits 0) remounts it rw and tries every kind of write; all refused and the
     host's seeded file unchanged. The red-first control records it `writable: true` (started with
     `limactl start`, as `fy up` would re-record it read-only): the writes land and `fy verify`
     FAILs its to-box row. First, its FRESHNESS: after the guest has read a to-box file, an append
     and a rename-over from your computer each reach it within 5 s, and on 9p the to-box carries
     foldyard's pin (`9p.cache = mmap`; Lima's default `fscache` never revalidates, and left every
     round stale on CI's 9p hosts before the pin).
   - `test_reclaim_e2e.py` — `fy reclaim` on a real store: a removed worktree's tagged images
     (both provider spellings) swept, the main image + base images kept, the next `up` still
     `Using cache` (the three reclaim properties below, live).
   - `test_worktree_e2e.py` — `fy worktree add` (registered, own branch, clean tree), its stack
     up beside main's, `remove`: containers + volumes gone, the bound-out transcript ARCHIVED
     before the tree is deleted, main untouched, the branch kept, local state dropped.
   - `test_monitor_e2e.py` — `[machine] monitor = "observe"` via `fy up`: the release delivered
     and installed, the packaged policy LOADED on the shipped kernel (an invalid one stops
     Tetragon outright), and the box's uid unable to stop it, reach its socket, read its log,
     write its policy dir or reach its health port; a junk inbox delivery rejected beside the
     running install; a container's file read reaching `fy monitor` attributed to that
     container's id through the real relay → pull → join; `off` removes it at the next boot (the
     teardown, unasserted — and skipped in CI, where the module runs last in its shard).
   - `test_wall_e2e.py` — `[machine] firewall` + `host_firewall` via `fy up`: the fixture IS the
     operator — the first `fy up` refused (nothing installed), then the `sudo` lines `fy machine
     host-firewall` printed run verbatim, then `fy up` passes; the host table on the VM's own slice,
     the unit active, direct guest egress refused, DNS resolving, the proxy the way out, the api
     still served, and the stale-provisioning refusal.
   - `test_host_daemons_e2e.py` — the supervisor with the zero-secret rig
     ([docs/testing-modes.md](./docs/testing-modes.md)): mode on → fake minter up + the overlay
     re-rendered, blocked-daemons empty; mode off.
   - `test_box_e2e.py` — `fy box up` on the VM (recreated to mount the module's copy), in-box
     `fy ps` over `CONTAINER_HOST`, in-box `fy verify` ALL PASS, `box down`.
   On a Lima host these run against the example's own VM (creating, restarting, recreating it),
   never a consumer's. What the runner cannot reach — nested virtualisation for the gVisor
   posture — stays the recipe in [docs/nested-virt.md](./docs/nested-virt.md).
   From a Claude Code cloud session (root, no `/dev/kvm`, an egress proxy that re-signs TLS) the
   tier runs under QEMU's TCG, slowly: [`.claude/skills/cloud-host/`](./.claude/skills/cloud-host/SKILL.md)
   sets that host up in one script and lists what breaks there and why.

**In-box validation you CAN do:** `fy verify`, `fy ps/down/up/logs`, the e2e tiers above.
**CANNOT from inside the box:** `fy host restart` (real daemons), `fy mode <set>` (authoritative
state), `fy tui`, real credentialed modes, and box creation (`fy box up`/`build` would
recreate the running box; `fy box ps`/`shell`/`down` are safe).

### Repo config is never live input to the host

`foldyard.toml` is inside the mount, so anything that can write the checkout can write it — and the
host's reading of it decides where credentials get injected and what egress goes undecrypted. The
supervisor therefore reconciles from the copy an operator ADOPTED (`configpin`, ADR-0022), and a
tree edit is inert until adopted. Two rules follow when you touch this area:

- **Host-side config reads go through `devmode.worktree_config()`** (a named worktree's adopted
  toml) **or the ambient `config.X()` readers**, whose parse on the host IS the adopted snapshot
  (`config._host_toml`; `{}` when nothing is adopted, and `machine.ensure` refuses then — an empty
  `[machine]` is an unwalled VM). Never `config.resolve(repo=…)` / `config._tree_toml` / a direct
  `tomllib` read of a checkout file for anything with a host consequence: that re-opens the
  channel for every field at once — which is exactly how `[proxy] passthrough` and
  `[[inject]].host` stayed live after four rounds of per-field fixes
  ([ADR-0022](./docs/adrs/0022-host-runs-the-adopted-config.md) — the channel, not the field).
  The launch gate runs first (`preflight.check_or_abort`) and re-runs the command after an
  adoption, since import-time constants (`machine.MACHINE`…) read the config before it. In the
  suite, conftest's `ambient_reads_the_tree` points the seam at the tree (what the tests were
  written against); `tests/test_ambient_adopted.py` runs the real host read.
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
holds the advisory live tiers, opt-in (`main`, a `[run-e2e]` commit message, or a dispatch). Every
job uses standard runners, which GitHub bills nothing for on a public repository. (Beside
them, `claude-pr-review.yml` posts an advisory Claude review on each same-repo PR, from the
base branch's `.claude/commands/pr-review.md`, the prompt `/pr-review` runs locally. A flaw it
finds already on main is filed as a private vulnerability report by
`.github/scripts/report-private-findings.sh`, never posted.) Five jobs, the two host tiers
sharded (below):

| job | what | engine |
| --- | --- | --- |
| `check` | `just check` — ruff + pyright + ty + the unit/golden/TUI suite. typecheck installs the `e2e` group so the opt-in proxy/box e2e files (which import `cryptography`/`requests`) resolve | none |
| `live-e2e` | the in-box topology: all the live e2es (`-k e2e`: the example stack up→serve→down, the in-process proxy e2e, AND the box e2e) — run **inside a docker-CLI container** that mirrors the dev box (see below) | runner Docker |
| `lima-host-e2e` | the HOST topology: `ubuntu-24.04` as a real Linux host running foldyard's default `lima` backend — `machine ensure` boots a QEMU/KVM VM on the runner, then `tests/test_e2e.py` drives the real `foldyard up` / worktree lifecycle through the config-adopt gate, the supervisor and compose, exactly as on an operator's machine. Two shards, `L1`/`L2` | Lima VM (podman in the guest, over the forwarded socket) |
| `wsl2-host-e2e` | the same host tier on `windows-2025`: an Ubuntu 24.04 distro under WSL2 ([Vampire/setup-wsl](https://github.com/Vampire/setup-wsl)) is the host, and the `lima` backend boots the QEMU/KVM VM INSIDE it — a second level of nesting the hosted Windows runners expose. Same modules, four shards `W1`–`W4`; `test_wall_e2e.py` skips itself there (below) | Lima VM inside the WSL2 distro |
| `e2e` | the one status for the workflow: `needs:` every job above and fails unless each succeeded (a matrix job's result is its worst shard's) | none |

**The weekly randomised property run (`property-explore.yml`).** Every job above loads the `ci`
hypothesis profile, which replays the same examples every run and so never finds a new
counterexample; local randomised runs at about 3,000 examples found two real bugs that CI's replayed
examples had passed (PR #138, 2026-10). So once a week (Tuesdays 03:17 UTC, kept weekly to go easy
on GitHub's runners) one job runs the property tier — every test module that imports hypothesis,
which `tests/test_property_explore.py` holds the workflow's `PROPERTY_MODULES` list to — under the
`explore` profile: randomised, `max_examples` 5000 (50× the default; each test's own budget scales
with it), no deadline, `-n auto`. The tier took 7.7 min on a hosted runner (5.4 on 4 local workers),
under the step's 22-minute cap; the hook-delivery property is most of it. The example database lives
at `.hypothesis/examples`, which `actions/cache` carries from run to run (each run saves under
`hypothesis-explore-<run_id>-<attempt>`, the next restores the newest by the prefix), so a
counterexample found one week is replayed first every week until it is fixed. GitHub evicts an entry
nobody has read for 7 days, which a weekly run only just beats; a late start may find it gone and
begin a fresh database, which loses nothing the issue below doesn't keep. A manual run (Actions →
property explore → Run workflow) takes a `max_examples` and a `dry_run`.

**A failure opens a public issue, because GitHub's own failure email does not reach the
operator.** A failed scheduled run emails whoever last edited the workflow's cron line, so a
reformatting PR would silently move the alert to its author. Instead, on any failure,
`tests/tools/property_failures.py` turns the JUnit report (or, without one, the log's tail) into a
summary — the run's URL, the failing tests, each falsifying example and its `@reproduce_failure`
line — and `.github/scripts/notify-property-failure.sh` comments it on the open issue labelled
`property-tests`, or opens one assigned to and @-mentioning `NOTIFY_LOGIN` (the workflow's
top-level `env`; change it there to send it elsewhere). Assignment and mention both email under
GitHub's default notification settings. Public is deliberate: the run's log already shows the same
examples. The summary is untrusted text (whatever a strategy generated): it reaches `gh` only as
`--body-file`, inside a code fence it cannot close, and no `run:` in the workflow expands an
expression. A `dry_run` prints the issue instead of posting it.

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
mounts, so every test copy can drive the one VM). It bakes a session base VM first (`fy-ci-base`,
named by `FY_BASE_VM_NAME`/`FY_BASE_VM_STAMP` job-wide, retried once), so that VM and every one a
module recreates are clones: a template create runs the template's `dnf install podman` over the
Fedora mirrors, and one stalled mirror once left a WSL2 run's every module after it on a VM whose
boot never finished (2026-10-16). The cost: a template-created PROJECT VM (foldyard's provisioning
on a fresh template boot) is no longer exercised by the host tier. `test_basevm_e2e` checks that
session base instead of baking a second one (sharding, below). It exports the VM's socket as
`CONTAINER_HOST` — for
the tests' OWN engine calls; a preset `DOCKER_HOST` would make the CLI under test skip
`machine.ensure` (`stack._docker_host`: dev-box semantics) and bypass the very lifecycle the tier
drives. The tests' `foldyard up` then runs the real host path: machine ensure → adopt gate →
supervisor → compose. First catch of the tier: podman 4.9.3's `ps --format` (Ubuntu 24.04's
package) has no `{{.Label "k"}}`, so every probe built on it read "engine unreachable" on a
Linux host — now `{{json .Labels}}` (`devmode.ps_labels`).
Things a Linux runner needs that a macOS host does not: `qemu-img` (from `qemu-utils`, not implied
by `qemu-system-x86-core`), a `systemd --user` manager for anything scoped (`loginctl
enable-linger`), and Lima from the release tarball into `/usr/local`. **arm64 runners have no
KVM** (`ubuntu-24.04-arm`: no `/dev/kvm` before or after `modprobe kvm`, probed 2026-09-17), so
the job is x86-only. The `podman` backend (podman-machine) has not been tried on a runner; the
`lima` backend is the product path and the one tested. The nested-KVM rig recipe in
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
install is 40 s after. **What WSL2 cannot do: the host firewall.** `[machine] host_firewall`
matches the VM by `socket cgroupv2`, and the stock WSL2 kernel has `# CONFIG_NFT_SOCKET is not
set` (both the 6.6 and 6.18 branches), so `nft` refuses the rule with ENOENT — the wall fails
closed, as designed. `test_wall_e2e.py` now probes the kernel for the expression in its gate
(one rule into a throwaway table, `sudo -n`) and skips; before that its fixture had already
re-provisioned the VM walled, and the error left the worktree module refusing the stale
provisioning — 9 errors from one kernel option. The VM firewall (`[machine] firewall`) is
unaffected (it runs in the guest). Everything else passed unchanged — 30 passed, 6 skipped; the
tier is ~3.5× slower than on Linux (unsharded, the test step 43 min vs 12; `test_box_e2e` 8 min,
`test_machine_e2e` 9; the job 47–62 min — what the sharding below fixed). Facts about the
runner worth knowing: 16 GB / 4 vCPU, of which WSL2 takes half the memory; the distro's root
is a sparse 1 TB vhdx on `C:` with ~30 GB actually free; the `ubuntu-24.04-arm` finding carries
over — Windows-on-ARM boots the distro at EL1, no KVM, so this is x86-only too.

**Why the host tiers are sharded (2026-10-22).** Unsharded, `wsl2-host-e2e` took 47–62 min and
`lima-host-e2e` 16–18. Timing the WSL2 tier found no hotspot to fix: three levels of virtualisation
make everything 3–6× slower than on Linux (a process start ~4×, `podman run` ~6×, small files on
the shared folder ~12×), and the minutes go on the cold `fy up` after each VM recreate (65–154 s,
~24 s on Linux), with runners varying ±50%. Setup is only ~6 min of a job and the VM boots are
fine. Every module drives the one VM, so tests can't run in parallel INSIDE a job — the modules
are split across jobs instead: each host job is a matrix whose shards (`matrix.shard.modules`)
each pay the full setup on their own runner (distro, Lima, the session base bake, the first VM),
then run their modules in order — four on WSL2 (`W1`–`W4`), two on Linux (`L1`/`L2`, each the union
of two WSL2 shards, so a module's neighbours are the same on both hosts). The split is by module
times averaged over several runs, and each shard starts with a module that recreates the VM (`W3`
starts from the VM the setup just created). A module's time depends on its place: the first
`fy up` after a recreate is the cold one (image builds and pulls), so whichever module follows a
recreate carries it. Judge a rebalance on several runs' tables, not one: the apt step alone has
ranged from under a minute to 13 min on the same install (once 12 on Linux). (Pointing the
distro's apt at `azure.archive.ubuntu.com`, the Linux runners' mirror, made it slower, 2–16 min,
not faster. What the WSL2 job does instead is cache the debs together with the package lists they
were resolved against, keyed by month (`actions/cache`, restored to `RUNNER_TEMP`), and install
offline from them. On a miss it installs online as before and saves the result; if the offline
install fails it installs online too, but a cache key can't be overwritten, so that cache stays
until the month's key changes — bump the key's `v1` to replace it sooner, and when the package
list changes.)

**4 vCPUs for the CI VM was tried and reverted (2026-10-22).** `MACHINE_CPUS=4` (over the example's
`cpus = 2`) made no Linux module faster, and on WSL2 it made the VM-heavy ones slower and erratic.
Six unsharded 2-vCPU runs plus the 2-vCPU sharded spike had `test_machine_e2e` at 6.4–9.2 min
and `test_verify_e2e` at 3.6–4.2. Six sharded 4-vCPU runs had them at 7.5–14.7 and 4.2–10, the
box module at 6–11 against 4.3–6.5, and in-box `fy verify`'s 12 s dry-run push to the public
origin timing out in three of eight `test_box_e2e` attempts ("push refusal UNPROVEN (timed
out)"), which never happened at 2. The likely mechanism is a nested guest given every vCPU WSL2's
utility VM has, which leaves QEMU's own I/O thread (Lima's user-mode network included) and the
host processes to fight it. The jobs now set `MACHINE_CPUS=2` explicitly. The create step asserts
`limactl list`'s CPU count and prints the guest's `nproc`, and on WSL2 the variable is forwarded by
`WSLENV` like every job variable (the guard test holds `WSLENV` to the job's `env:` keys). Trying
another size is a one-line change; measure it over several runs. The verify bound stays where it
is: verify is never weakened to suit CI. The setup's base bake caps its first attempt at 6 min
(good bakes take 1–4.5): a stalled Fedora mirror otherwise holds Lima's boot wait for its full 10
min before the retry.

`tests/test_e2e_workflow.py`, in the fast suite, holds the matrix to the modules: every host-tier
module (test_e2e.py and each `test_*_e2e.py` that imports `e2e_host`) in exactly one shard per
job, so **a new host-tier module must be added to a shard in both jobs** or `check` goes red. Two
smaller savings ride along:
- the guest monitor's teardown skips its monitor-off reboot when nothing runs after it: the shard
  hands its module list to the substrate as `FY_E2E_MODULES` (the same string pytest gets), and
  `e2e_host.runs_last` is true only for its final entry. Unset (a local run), the VM is restored as
  before;
- `test_basevm_e2e` checks the job's session base (`fy-ci-base`) and clones from it instead of
  baking a second base. **The trade-off:** in CI the module no longer proves a bake from scratch on
  its own — the job's setup step, the same `fy machine base bake`, is that proof — and `rm` is
  proven on a never-booted stamped stand-in so the session's base survives for the modules after
  it. Without `FY_BASE_VM_NAME`/`FY_BASE_VM_STAMP` (a local run) it bakes, clones and removes its
  own, as before.

Each shard writes a per-module timing table to its run page (`--junitxml` →
`tests/tools/e2e_timings.py` → `$GITHUB_STEP_SUMMARY`; on WSL2 the distro appends to the runner's
file through `WSLENV`'s `GITHUB_STEP_SUMMARY/p`), the shards time out at 45 (WSL2) / 30 (Linux)
minutes, and the `e2e` job is the single status to check.

## Conventions & gotchas

- **Experimental means a register entry, not a word in prose.** A setting you're not ready to
  stand behind goes in `experimental.REGISTER` with what's unproven and what would graduate it,
  gets the `*(experimental)*` tag in docs/configuration.md, and its changelog fragment says
  `experimental: true`. Graduating one is deleting the entry and the tag, plus a `.changes/` entry
  under Changed — review the `graduates_when` lines when cutting a release.
- **Changelog entries are files, not edits.** A change a consumer would notice adds
  `.changes/<slug>.md` (section + bump frontmatter, then the bullet; format in
  [.changes/README.md](./.changes/README.md)); `just release` folds them into `CHANGELOG.md`.
  Never add an `## Unreleased` section back — it made every pair of open PRs conflict.
  `tests/test_changes.py` validates the pending files in `check`.
- **The docs site renders `docs/` as it is** (`mkdocs.yml`, `.github/workflows/docs.yml`: a
  `--strict` build on every PR that touches docs, deployed from main to
  [twistco.github.io/foldyard](https://twistco.github.io/foldyard/); `just docs-site` locally).
  Write pages for GitHub and `fy docs` as before — `docs-site/hooks.py` adapts them at build time:
  a link out of `docs/` goes to GitHub, a GitHub URL for something the site holds comes back to
  the site, and heading ids are GitHub's, so `#anchor` links work in both. `talks/` is in the repo
  but not on the site yet (the hook's `UNPUBLISHED`): a link into it fails the build.
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
  is no platform branching in the package: no `sys.platform`, no `Darwin` test; Linux and WSL2
  hosts are validated in CI, see the two host-tier jobs). User-facing text says *your computer*
  (see [docs/glossary.md](./docs/glossary.md)); `tests/test_docs_shipped.py` refuses "Mac" in the
  shipped docs, the skills and `--help`. Say **macOS** only
  where the claim really is macOS-only — `brew`, the login keychain, `security add-trusted-cert`,
  Virtualization.framework/`vz`, desktop notifications. One trap: **`host` already means an
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
  name matching would let `acme`'s `fy up` kill `acme-two`'s VM. Lima's version is narrower
  and runs inside `start` on both of `ensure`'s paths: a hostagent (Lima's own `ha.pid`) still
  alive after its driver (`qemu.pid`) died is waited for — it exits by itself once it notices,
  but not atomically with flipping the instance to Stopped, and `limactl start` in that window
  refuses with "host agent is running but driver is not" (the WSL2 runner, 2026-09-18) — and
  signalled only if it lingers. A VM whose driver is alive is never touched. On `vz` (macOS) the
  VM runs INSIDE the hostagent — `vz.pid` is `ha.pid` — so a live hostagent is a live VM there;
  reading "no `qemu.pid`" as "driver dead" once made every live vz VM an orphan to signal.
  A HUNG vz VM is Lima's `Broken` status (its hostagent stopped answering) — not Running, so
  `ensure` takes the start path, not the revive: `start` ends a Broken instance with Lima's own
  `limactl stop --force` first (a SIGSTOPped hostagent + removed socket reproduces it; recovered
  in 34 s live, 2026-09-28).
- **Never `limactl list --json`** — that path re-reads its own output through a 64 KiB-a-line
  `bufio.Scanner`, and each line carries the instance's whole config. With the wall's and the
  guest monitor's boot scripts recorded (~68 KB of `lima.yaml`), `list` fails "token too long"
  and prints nothing, so every lookup reads "no such VM" (Lima 2.2.0; the host tier's first
  monitor run). `LimaBackend._instances` and `e2e_host.lima_list` use `--format '{{json .}}'`,
  which writes straight out; split its rows on `\n` only (Go leaves U+0085 unescaped).
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
  entry-point plugin. foldyard's OWN children are part of this: run them through
  `selfspawn.python_m` (`python -P -m`), never `[sys.executable, "-m", …]` — their working directory
  is usually a checkout, and plain `-m` imports a `foldyard/` the box planted there first; one run
  in ANOTHER directory takes `selfspawn.env()`, which pins relative `PYTHONPATH` entries.
  Consumer scripts run in a CONTAINER, never on the host —
  `[project].worktree_init` via `worktree._init_in_yard` (skips with a retry hint when the yard
  isn't ready, never falls back). So does `fy worktree add`'s CHECKOUT (`worktree._checkout_in_yard`:
  a host checkout runs the filter drivers the shared `.git/config` names): the host runs only
  `git worktree add --no-checkout`, and with no box image it refuses before any git runs —
  never a host checkout with the filters "neutralised by name". The VS Code attached-container config is AUTHORED by foldyard
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
  supervisor's environment, which holds every axis's host.env secret. The addon resolves those
  names itself (the operator's exports, then `host.env`) and the proxy runs with host.env's keys
  stripped from its environment ([ADR-0030](./docs/adrs/0030-the-proxy-reloads-instead-of-restarting.md)).
- **Nothing that varies with posture may reach the proxy's launch env.** The supervisor restarts a
  daemon whose cmd/env changed, and a restart cuts every connection in flight (a mode switch once
  killed an apt download mid-package). Rules, the allowlist's enforce switch and `passthrough` go in
  the spec's `live` data, which the supervisor writes and the addon re-reads; a narrowing closes
  only the connections it no longer allows, through mitmproxy internals pinned by
  `tests/test_proxy_reload_e2e.py`
  ([ADR-0030](./docs/adrs/0030-the-proxy-reloads-instead-of-restarting.md)).
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
  allowlist lets the yard widen its own allowlist.
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
  **Every byte of `guest-boot.sh` and `machine-wall.sh` is in that id** — a comment edit too —
  so any change refuses every existing VM as stale until its user runs `fy machine stop && fy up`.
  Don't reword them in a sweep; batch text changes with a change that needs the re-provision
  anyway. (Their `fy-wall` paths, units and tables keep the old name: renaming those is a
  migration, not wording.)
  **The one exception is an opt-in feature's own root work**, which gets its own recorded script
  under its own marker so it never touches that id: the guest monitor (`assets/monitor/`,
  `# fy-monitor <id>`, ADR-0032) is the one instance. Its rules: the two scripts never mention
  each other's marker (`set_provision` drops entries by a regex on the marker, so a mention would
  delete the other script), it ALWAYS exits 0 (Lima marks the whole boot failed otherwise —
  report problems in `/run/fy-monitor/` instead), a VM that never turned it on records nothing,
  and no recursive `rm` reaches `/run` or `/sys` (Tetragon mounts cgroup2 under
  `/var/run/tetragon`; an `rm -rf` there rmdirs empty system cgroups — seen live).
- **The to-box is read-only OUTSIDE the guest, or it isn't mounted** (ADR-0033). Never mount it
  on a vmType/mountType pair whose share server doesn't enforce `writable: false` (only vz +
  virtiofs and qemu + 9p do), never mount the state dir itself (it holds `host.env` and the keys —
  `verify` exempts exactly the two subdirectories), and never put a secret in it: every box of the
  project reads it. `verify` proves it by a real write from a `--privileged` container, never by
  the `ro` flag (root in the guest can drop the flag and the host still refuses). The from-box is
  box-written: the host makes its slots with `mountwrite.makedirs`, reads it only through the
  transcript sync's no-follow walk, and never mounts it on the podman backend.
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
