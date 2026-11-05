# Working on foldyard

foldyard runs AI agents in dev boxes on a rootless VM, with credentials and egress held on the host.
This file holds what applies to every task, and points at the rest: read the page for what you're
about to change, because the reasons and the traps are there. The "why" behind design calls is in
[docs/adrs/](docs/adrs/README.md).

## Commands

foldyard is a uv project (`src/foldyard/`, `tests/`).

```bash
just test [args]   # the suite (add -n auto to parallelise)
just run <verb>    # the CLI from source
just lint          # ruff check + format --check (auto-fix: just format); line length 100
just typecheck     # pyright + ty; keep both green
just check         # lint + typecheck + suite: what CI gates on, and zero before committing
just install       # `fy` on your PATH, editable from this checkout
just prune         # list merged .claude/worktrees, branches, orphaned dev venvs (--apply removes)
just nested-up     # Apple M3+: a VM of its own for the host tier
just nested-test [--ref REF] [modules]   # send a commit in, run the tier
just nested-down [--delete]
```

There's no bare `python` or `python3` in a dev box: use `uv run python`.

## Before you change something, read

| If you're changing | Read |
|---|---|
| Tests, or anything that spawns an engine or a host tool | [docs/dev/testing.md](docs/dev/testing.md) |
| A workflow, a shard, or the host-tier substrate (`tests/e2e_host.py`) | [docs/dev/ci.md](docs/dev/ci.md) |
| The wall, guest provisioning, the proxy, minters, box mounts, the gVisor socket, anything that runs on the host | [docs/dev/invariants.md](docs/dev/invariants.md) |
| VM lifecycle, stack verbs, disk reclaim, port offsets, config caches, Lima calls | [docs/dev/gotchas.md](docs/dev/gotchas.md) |
| The host-tier rig on an Apple M3+ | [docs/nested-virt.md](docs/nested-virt.md) |
| The host tier from a Claude Code cloud session | [.claude/skills/cloud-host/SKILL.md](.claude/skills/cloud-host/SKILL.md) |

A module's docstring explains that module, so read it before editing the file.

## Host or box

The split that decides what you can do is host (your computer) vs box. Test it with
`config.in_box()` (`IN_DEVBOX=1`), never `which("podman")`: the box has podman too.

- **You can run in a box:** `fy verify`, `fy ps/down/up/logs`, the suite, the e2e tiers that aren't
  the host tier.
- **You can't:** `fy host restart`, `fy mode <set>`, `fy tui`, real credentialed modes, `fy box
  up`/`build` (they'd recreate the box you're in), and the host tier.

## A host-tier change isn't finished until the tier has run

The host tier is `machine`, `box up`, `fy host`, `mode set`, `verify`'s real VM boundary and the
walls. You can't run it from a box, so asking for the run is a normal step of the work, not an
escalation.

Commit, then finish by giving the operator the exact command, with the hash and the modules that
cover your change:

```bash
just nested-test --ref <hash> tests/test_<x>_e2e.py   # after `just nested-up`, on an Apple M3+
```

- Say what you ran in the box and what's still unverified until that run comes back.
- `test_wall_e2e.py` also needs `--with-sudo`, which uses up that VM: it has to be replaced after.
- Without an Apple M3+: the operator pushes a commit with `[run-e2e]` in its message and CI runs it.

A new host-tier module must be added to a shard in both jobs in `foldyard-e2e.yml`, or `check` goes
red.

## How to change things

- **Test first.** A new property test must be seen red: mutate the code under test, watch it fail,
  undo with the exact inverse edit (not `git checkout`, which also wipes uncommitted work). Set
  `PYTHONDONTWRITEBYTECODE=1`, or a same-size mutation can read as a false green.
- **A consumer would notice it?** Add `.changes/<slug>.md` (format in
  [.changes/README.md](.changes/README.md)). Never edit `CHANGELOG.md` or add an `## Unreleased`
  section.
- **Not ready to stand behind a setting?** It goes in `experimental.REGISTER`, gets the
  `*(experimental)*` tag in docs/configuration.md, and its fragment says `experimental: true`.
- **Skills, the `fy init` scaffold and shipped docs teach, and don't fail when they're wrong.** Write
  commands in backticks so `tests/test_agent_guide.py` can check them, and don't restate `--help`.
- **Write "host" in code, "your computer" to users, never "Mac".** Say macOS only where the claim is
  macOS-only (`brew`, the keychain, `vz`). Near the allowlist and proxy, `host` means a hostname.
- **Reporting is part of the fix.** Anything that ignores repo config must say so: a log line, a
  notification or a doctor row.

## What must always hold

The reasoning, and the rules for one area of the code, are in
[docs/dev/invariants.md](docs/dev/invariants.md).

- **Repo config is never live input to the host.** The box can write `foldyard.toml`. Host-side reads
  go through `devmode.worktree_config()` or the ambient `config.X()` readers, which read the adopted
  copy. Never `config.resolve(repo=…)`, `config._tree_toml` or a direct `tomllib` read of a checkout
  file for anything with a host consequence (ADR-0022).
- **No host-executed code from the repo mount, and no command strings from config** (ADR-0023). A new
  mechanism is a new credential kind or an entry-point plugin. foldyard's own children go through
  `selfspawn.python_m`, never `[sys.executable, "-m", …]`. Consumer scripts run in a container.
- **Root in the Lima guest is boot-time only.** Never `limactl shell … sudo` from the host. Every byte
  of `guest-boot.sh` and `machine-wall.sh` is in the provisioning id, so any edit refuses every
  existing VM as stale.
- **`verify` stays the credibility check.** Never weaken its assertions, including to suit CI.
- **Egress grants live in the host store at every level,** never in `foldyard.toml`.
- **The hot path stays venv-free and Textual-free,** and plugins stay stdlib-only and import-light.
  Don't register the built-ins as entry points. New plugin hooks go through the `Registry`: `devmode`,
  `verify` and `tui` never name a credential mechanism.
- **Keep emitting `CONTAINER_HOST`, and don't make `engine()` prefer docker.** In-box `up`, `ps` and
  `verify` depend on it.
- **Golden tests never see a live engine socket.** Fixture `DOCKER_HOST` values are dead paths, and a
  fixture that lets a verb execute stubs `stack.subprocess.run` itself. An escaped probe once removed
  a box's whole dev stack with every test green.
- **The suite is hermetic.** A test that means to run a host binary marks it
  `@pytest.mark.spawns("/abs/path")`; one that needs a real tool is an e2e test (`tests/*_e2e.py`).
- **`machine.state()` is a flag, not liveness.** Before deciding the engine is reachable, pair it with
  `machine.responsive()`.
- **Never `limactl list --json`.** It fails on long configs and every lookup reads as "no such VM".
  Use `--format '{{json .}}'`.

## Where things are

`src/foldyard/`, one docstring per module. Tests are in `tests/`, and `example/` is the minimal
consumer they run against.

| Area | Modules |
|---|---|
| CLI, config | `cli` · `config` · `configpin` (the adopted config) · `exposure` · `experimental` |
| Stack | `stack` · `composeguard` · `registrylogins` · `confighash` · `reconcile` · `state_view` |
| Posture | `devmode` · `verify` · `preflight` · `allowlist` · `keyless` · `credscope` · `plugins/` |
| VM | `machine` · `machine_backend` · `basevm` · `boxmounts` · `hostwall` · `sandbox` · `guestlog` · `prereq` |
| Box | `box` · `boximage` · `crossmask` · `crossinject` · `agents` · `boxpackages` · `hostclock` · `hostgit` |
| Host daemons | `supervisor` · `selfspawn` · `forwarder` · `attribution` |
| Guest monitor | `monitor` · `monitorlog` · `monitorstore` · `monitorhooks` · `monitorsink` |
| Worktrees, git | `worktree` · `worktree_registry` · `githeal` · `mountwrite` · `transcripts` · `ports` |
| Other | `tui` · `init` · `skills` · `docs` · `changelog` · `compat` · `buildgate` · `browser` · `vscode` · `podman_desktop` · `term` |
