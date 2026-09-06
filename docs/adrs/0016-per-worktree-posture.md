# ADR-0016 — Per-worktree posture: the worktree is the consumer

- **Status:** Accepted (2026-06-30) — implemented (PR #25; per-project port bands 2026-07-05,
  heartbeat/log split 2026-07-06 `6c9b74ea`)
- **Sources:** docs/history/per-consumer-registry-plan.md, docs/history/per-worktree-proxy.md,
  `src/foldyard/config.py`, `src/foldyard/devmode.py`

## Context

Worktrees were already the unit of isolation for the *stack* — their own compose project, ports,
and dev box (ADR-0004) — but credential **posture** was host-wide: one `dev-mode.json`, one proxy
on a fixed port, one mode for every branch. The design note that first examined per-branch modes
(per-worktree-proxy.md, 2026-06-16) concluded "not now"; the trigger it named fired on
2026-06-30: a colleague needed *real* Auth0 + GCP SA on one branch and the *simulator* on another,
simultaneously. Two branches, two postures.

The sharp constraint is leakage. Every box reaches the host as `host.containers.internal`, so on
a shared proxy port the proxy *cannot tell boxes apart* — an offline worktree's box would ride a
credentialed sibling's injection rules. Same for the gcp minter: a single shared minter would
hand a `gcp=sa` worktree's token to a sibling on `gcp=off`. A credentialed worktree must not leak
into an offline one; that is the whole point of declaring posture per branch.

## Decision

**The worktree becomes the consumer.** Config, registry (ADR-0015), and posture state all key on
the worktree, while identity stays project-shared under the one supervisor (ADR-0006).

- **State layout** (`config.py`): `~/.foldyard/<project>/` is the project-shared root —
  `host.env` identity, the supervisor's `flock`, its combined log and liveness heartbeat, the
  egress allow-store, the proxy CA. Posture lives under `posture_dir()`: `<state_dir>/main/` for
  the primary checkout, `<state_dir>/worktrees/<name>/` for worktrees — each holding that
  branch's `dev-mode.json`, `logs/`, and mirror. (Worktrees nest under `worktrees/`, and
  `fy worktree add` reserves the name `main`, so a worktree can never collide with the primary's
  posture dir — that collision once made a mode change to one silently change the other.) A
  legacy project-root `dev-mode.json` is honored for main so upgrades don't reset anyone to
  offline.
- **Per-worktree daemon ports** (`config.proxy_port()` / `gcp_minter_port()`): each daemon
  family's project base + the same 1..89 worktree offset the stack ports use, so a box only ever
  reaches ITS proxy and ITS minter. A `gcp=off` worktree simply has no minter on its port — its
  metadata emulator gets no token, zero secrets, no leak from a `gcp=sa` sibling. Bases come from
  a per-project 200-port band allocated in `~/.foldyard/ports.json` (proxy = band+0..89, minter =
  band+100..189 — disjoint spans, unlike the old 9-apart 8079/8088 bases; per-project because two
  projects' supervisors on one shared base reaped each other's proxies every tick).
- **One supervisor reconciles a mode-map, never N supervisors.** The singleton (ADR-0006) binds
  each worktree's resolved config in turn (`config.using(devmode.worktree_config(wt))`) and
  reconciles the union of every active worktree's desired daemons — N proxy listeners on N ports,
  daemon names suffixed `@<worktree>` (main stays bare). "Active" = the worktree's box is
  actually up (`devmode.up_worktrees()`), with a main-only fallback so a box coming up always
  finds a live proxy + CA: a posture change must never *start* a heavy stack. TTL expiry on
  emergency rungs runs per worktree; mirrors are written only into checkouts whose box is up.
  `set_mode` extends the reconcile to the *stack*: a posture flip re-renders an already-up
  stack when its posture signature (derived env + overlay list) changed, so mode file and
  running containers can't silently disagree.
- **Identity stays shared.** `host.env` (App IDs, PEM secret names) is a machine fact, not a
  branch fact; one CA is mounted into every box; one supervisor process holds the single
  credential footprint. Only the posture *state* and the *listeners* multiplied.
- **Heartbeat/log split** (`6c9b74ea`): the supervisor's liveness heartbeat and combined log are
  project-shared under `state_dir()` — they are facts about the one process. Reading liveness
  from a per-worktree mirror made a healthy supervisor look wedged from any worktree whose box
  was down (its mirror is legitimately stale), so `fy up` there bounced it; the shared
  `host-supervisor.heartbeat`, stamped per tick and at boot, is what launch paths now check. The
  per-daemon JSONLs (`egress.jsonl`, `gcp-minter.jsonl`) stay per-worktree under the posture
  `logs/` — they are facts about that worktree's listeners.

## Consequences

- Parallel branches run genuinely different postures: offline `main` beside a `gcp=sa` +
  real-Auth0 PR-testing worktree, with no cross-contamination — the offline box has no route to
  a credentialed listener.
- Each worktree's mode file is validated against *its* registry (a branch may declare a
  different plugin set); unknown axes in a stored mode degrade gracefully rather than raise.
- A fresh worktree defaults every axis to rung 0 (zero-secret). `fy worktree add` seeds only the
  keyless agent axes (`claude`/`codex`) from main's actual values — never the credentialed cloud
  axes — so an agent box works immediately without ever escalating beyond what main already ran.
- The credential surface did not multiply: still one supervisor, one lock, one TTL loop, one
  terminal (the N-supervisors design was rejected precisely because it spreads the
  credential-holding machinery thin — per-worktree-proxy.md).
- Cost: more moving parts per tick (N listeners, N mode files), and box env is baked at box-up —
  `FY_PROXY`/port env pins mean a moved port needs a box re-bake.

## Rejected alternatives

- **N supervisors (one per worktree)** — multiplies credential-holding processes, TTL loops, and
  terminals; breaks the one-lock ownership story. The singleton reconciling a map keeps the
  safety properties and still gives every branch its own posture.
- **Shared proxy/minter ports with per-box discrimination** — impossible as designed: all boxes
  present the same source (`host.containers.internal`), so ports are the only reliable identity
  channel. Per-branch posture *requires* per-worktree ports.
- **Keying posture on branch instead of worktree dir** — the stack keys on worktree name
  (deterministic offset); posture matching it means a box and its posture always agree, even as
  the branch checked out in a worktree changes.
