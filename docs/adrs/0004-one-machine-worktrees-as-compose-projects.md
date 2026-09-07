# ADR-0004 — One machine + worktrees as namespaced compose projects (not VM-per-worktree)

- **Status:** Accepted (2026-06-08, with the spike) — implemented; default-branch forking fixed
  2026-07-06 (`de962868`), pinned offsets added with PR #21
- **Sources:** SPIKE.md ("Worktrees — one machine, namespaced projects"), PLAN.md §4.1,
  `src/foldyard/worktree.py`, `src/foldyard/stack.py`, `src/foldyard/machine.py`

## Context

Developers run several parallel checkouts of one repo — git worktrees — and each needs its own
full stack (DB, emulators, app) plus its own dev box, simultaneously. Cloud CDEs answer this with
one VM per workspace. Podman can mirror that locally: `podman machine init <name>` gives a VM per
worktree.

But on a laptop that model is wrong by default. Each machine VM costs multi-GB RAM, a duplicated
image store and build/package caches on disk, and a slower boot; and — decisive for podman on
macOS — only one podman machine can run at a time (upstream #26281; `machine.py start()` refuses
to start beside another running VM). More fundamentally, worktree-vs-worktree kernel isolation is
not in the threat model: the asset being protected is the *Mac*, and the one rootless, restricted
machine already walls that off (ADR-0001). Two worktrees are your own code at equal trust —
putting a VM between them buys nothing the threat model asks for.

## Decision

One machine; worktrees are **namespaced compose projects** on it.

- **The worktrees root is a machine mount.** The VM's isolation mount set is exactly two entries —
  the main checkout and the worktrees root (`machine.py _volumes()`), host path == guest path —
  so a new worktree needs no machine change: `fy worktree add <name>` creates a git worktree under
  the root and it is already visible to the engine.
- **Each worktree is its own compose project.** `stack.py` sets
  `COMPOSE_PROJECT_NAME`/`PODMAN_PROJECT` to `<prefix>-<worktree>` (bare `<prefix>` for main), so
  containers, networks, and named volumes are namespaced per worktree. Teardown honors the
  boundary: `fy nuke` in a worktree drops only that project's volumes; the shared pnpm/Playwright
  caches and the one image store are kept (`stack.py` nuke path).
- **Deterministic host-port offsets.** Every `[ports]` base gets `+offset(worktree)`, a stable
  1..89 derived from the worktree name via `cksum` (`stack.py _offset()`), so a given worktree
  always lands on the same host ports and simultaneous stacks usually avoid colliding on Mac
  localhost. The offset is a hash into 89 slots, so two worktree names *can* collide; the pin
  below is the escape hatch when they do. Precedence: an explicit `WT_OFFSET` env → a pin in the main
  checkout's gitignored `foldyard.local.toml` `[worktree-offsets]` table (PR #21;
  `_pinned_offset()`, read from `main_repo()` so it resolves identically wherever `fy` runs) →
  the cksum hash. Pins exist for worktrees that must sit on a *known* port, e.g. one a third-party
  service's OAuth-callback allowlist already accepts.
- **New worktree branches fork off the project's default branch** (`de962868`).
  `git worktree add -b` with no base ref forks from the primary checkout's *current HEAD* —
  usually some other in-flight branch, whose commits then silently leak into every new worktree.
  `worktree.py _default_base_ref()` now resolves an explicit `--from`, then
  `[machine].worktree_base` / `FOLDYARD_WORKTREE_BASE`, then auto-detects (`origin/HEAD`, then
  `main`/`master`), falling back to current HEAD only when nothing resolves.
- **Worktree lifecycle verbs**: `fy worktree add|list|remove`. `remove` tears down the worktree's
  box and stack, archives its Claude transcripts to the durable Mac store *before* git deletes the
  checkout, and refuses (without `--force`) if archiving fails.

**Escape hatch:** for a genuinely untrusted worktree — reviewing unknown code — spin a *throwaway
dedicated machine* for just that one. Isolation is a spectrum, light by default; the heavy shape
is one command away when the trust assumption ("two worktrees are your own code") doesn't hold.

## Consequences

- N stacks + N boxes share one VM's RAM, one image store, and the shared caches — bringing up a
  second worktree costs seconds, not a VM boot, and everything stays visible in one Podman
  Desktop view.
- The namespacing carries beyond the stack: per-worktree posture state and per-worktree
  proxy/minter ports (ADR-0016) reuse exactly this worktree key and port offset, so a worktree's
  daemons, containers, and posture always agree on identity.
- The hashed offset can collide between two worktree names (89 slots); `[worktree-offsets]` pins
  are also the manual fix for that.
- Cloud parity is preserved as a deployment knob, not a fork: the same compose files run
  one-VM-per-workspace in the cloud (where VMs are cheap at rest) and one-shared-machine locally.
- Mount scope is the blast-radius trade-off: the VM sees the narrowest directory containing all
  worktrees, so a box can read sibling worktrees. Accepted — they are equal-trust code by the same
  argument that rejected VM-per-worktree; the escape hatch covers the exception.

## Rejected alternatives

- **VM-per-worktree** — multi-GB per worktree, duplicated images/caches, slow boots, and
  impossible under podman-on-macOS's one-machine-at-a-time constraint (ADR-0011); defends against
  a threat (worktree-vs-worktree escape) that isn't in the model.
- **One compose project with prefixed service names** — loses compose-level lifecycle isolation
  (`down`/`nuke` scoping, default networks) that project namespacing gives for free.
- **Dynamic (first-free) port allocation** — ports would change across restarts; the
  deterministic hash keeps a worktree's URLs stable, and pins cover the cases that need exact
  values.
