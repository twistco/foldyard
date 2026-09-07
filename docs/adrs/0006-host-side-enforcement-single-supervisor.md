# ADR-0006 — Host-side enforcement: ONE supervisor, reconciling a per-worktree mode-map

- **Status:** Accepted (singleton lock 2026-06-16; per-worktree mode-map 2026-06-30; stale-holder
  bounce 2026-07-03; per-project port bands 2026-07-05) — implemented
- **Sources:** docs/history/per-worktree-proxy.md, docs/history/per-consumer-registry-plan.md,
  src/foldyard/supervisor.py, src/foldyard/ports.py

## Context

The posture model (ADR-0005) only means something if enforcement lives where the yard can't
touch it. The enforcement point is a set of host-side daemons — the credential minters and the
injecting egress proxy (ADR-0007) — and something has to own their lifecycle: start what the
current mode demands, stop what it doesn't, apply TTL expiry, and survive races between the
several paths that can launch it (`fy host` foreground, the TUI toggle, the detached
`ensure_background` from `fy up`).

Two pressures shaped the design. First, per-branch posture became a real need (2026-06-30: real
Auth0 + GCP SA on one branch, the simulator on another, simultaneously) — so the daemons must
serve N worktrees with N postures. Second, daemons are long-lived while the code is editable:
a supervisor computes daemon specs and minter allowlists from the code loaded at *its* start,
and a forgotten restart left a pre-update allowlist silently in force (the concrete 2026-07-03
incident).

## Decision

**Enforcement lives host-side, and there is exactly ONE supervisor per project.**

- `fy host` (`src/foldyard/supervisor.py`) is a deliberately-foreground process: daemons
  holding credentials stay visible in one terminal, logs interleave there, and Ctrl-C reliably
  stops everything — no orphan minter still serving tokens after you forgot about it.
- **Singleton by `flock`:** `acquire_singleton()` takes a non-blocking exclusive lock on
  `~/.foldyard/<project>/host-supervisor.lock` and holds the fd for the process lifetime. Every
  launch path converges on one owner; losers exit 0. The lock releases with the process on
  crash, so there is no stale-pidfile semantics to reason about.
- **Per-worktree posture, singleton process:** the one supervisor reconciles a **mode-map** —
  each tick it binds each up worktree's config in turn (`config.using`), expires *its* TTLs,
  collects *its* proxy/minter daemons, and refreshes *its* mirror. Daemon names carry a
  `@<worktree>` suffix; each worktree gets its own proxy *and minter* listener port (a shared
  gcp minter would hand a `gcp=sa` worktree's token to a sibling on `gcp=off` — per-worktree
  minter ports close that leak). Identity stays shared: `host.env`, the CA, the lock.
- **Per-project port bands** (`src/foldyard/ports.py`): each project gets a 200-port band from
  a flock-guarded `~/.foldyard/ports.json` registry (bands from 41000; proxy = base+0..89
  worktree offsets, minter = base+100..189). Two projects on one Mac previously bound the same
  fixed port and — each correctly holding its *own* singleton lock — reaped each other's proxy
  every tick, forever. The orphan reaper is likewise scoped to this project's staged addon path
  (`_is_our_proxy`), never "any foldyard proxy", and never a foreign process.
- **Stale-holder detection + launch-time bounce:** on acquire, the supervisor stamps
  `pid + code fingerprint` (a hash over the package tree's relpath/size/mtime + the interpreter
  path) into the lock file. The launch paths (`fy host`, `fy up` via `ensure_background`)
  compare the stamp against the code installed *now*, plus a project-shared heartbeat age check
  (stamped every ~2 s tick; > 30 s while the lock is held ⇒ the reconcile loop is wedged), and
  gracefully **bounce** a stale holder: SIGTERM (its shutdown stops each child cleanly), wait
  for the flock to release, SIGKILL fallback; orphaned listeners are reaped by the successor.
  `fy host --restart` forces a bounce. A *running* supervisor is never interrupted between
  launches — an editable-install save must not blip the proxy under a live session.
- **Heartbeat is project-shared, logs per-worktree:** the liveness stamp lives at project level
  because the per-worktree mirror only refreshes while that worktree's box is up — a down
  worktree's stale mirror used to read as "wedged" and bounce a healthy supervisor. Egress logs
  and posture files stay under each worktree's posture dir.

## Consequences

- One credential-holding footprint, one TTL loop, one terminal — however many worktrees run.
- `fy up` is idempotent and race-free: two near-simultaneous launches converge on the lock
  instead of fighting over a port and restart-looping on EADDRINUSE.
- After updating foldyard, a plain `fy up` hands the daemons over to the new code instead of
  deferring to last week's supervisor.
- The supervisor is a single point of failure per project — accepted: it self-heals on crash
  (lock dies with it), and its children are re-reaped by the next launch.

## Rejected alternatives

- **N supervisors (one per worktree):** multiplies the credential-holding processes, TTL loops,
  and terminals; invites orphan minters and port fights; and the singleton lock would have to
  become per-worktree, re-opening ownership questions. The cost was never the proxy listener —
  it was spreading the credential machinery thin (per-worktree-proxy.md "Why NOT N supervisors").
- **In-box enforcement:** anything inside the yard is inside the blast radius (ADR-0005).
- **Mid-flight auto-restart on code change** (spinout plan D7): the supervisor owns live
  children (a mid-flight mint, open proxy connections); a clean stop→re-exec→respawn handoff is
  real machinery for a dev-only condition. Replace-on-*launch* gets the safety without it.
- **Warn-only staleness:** shipped first, proved insufficient in practice — the warning was
  never seen while a stale allowlist kept serving. Detection had to act, at launch time.
