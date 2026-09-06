# Mode/state handling — review and consolidation proposal

*Research note, 2026-07-16. Prompted by the pattern of ad-hoc fixes for untested state
combinations (an observability SDK repeatedly fighting the llm mode, a report of cloud
elevation lapsing) and the question "would a state machine help?". Sources: the full
foldyard commit history in this repo (19 commits, ~17 of them state-handling-adjacent),
`devmode.py`, `supervisor.py`, `stack.py`, the gcp/llm plugins, and a consumer service's
boot-time secret loader. The commit-by-commit evidence stays in the origin monorepo's audit
run-log; every finding that matters is restated below.*

## What we actually have (it's already a control loop, not an event pile)

The architecture is declarative reconciliation, Kubernetes-style: `fy mode` writes a
**desired posture**, and enforcement converges on it continuously — the supervisor tick
(`supervisor.reconcile_once`, ~2s) for host daemons, `stack.reconcile_posture` for the
running compose stack on any `posture_signature` change, TTL expiry applied on read and
written back durably. That is the right shape, and a classic transition-table state
machine would fit *worse*: the state space is a product of independent axes × N worktrees
× lifecycle stages × external capabilities, so enumerating transitions explodes
combinatorially, and none of the historical bugs were "transition A→B handled wrongly".

## Where the bugs actually came from

Auditing every state-handling fix in the history, they cluster into four failure classes,
none of which an FSM addresses:

**1. Multiple state tiers with different refresh lifetimes.** Mode state lives in (at
least) four tiers: the authoritative Mac `dev-mode.json`; the per-worktree `.dev-mode.json`
mirror (refreshed only while that box is up); env **baked into containers at create time**
(immutable until recreate — the box, and every compose service); and **process-boot-time
snapshots** inside app processes (`load_secrets()` runs once at service start). Bug after
bug was a decision reading the wrong tier: the supervisor-liveness check read a
per-worktree mirror and false-bounced healthy supervisors (#43); the idle supervisor
re-dirtied clean checkouts with mirror writes (#44); the box env hint mis-fired "out of
date" forever (`_box_env_hint`'s NB comment); a box stranded on a pre-band proxy port
needed a recreate-nag (#36).

**2. Incomplete change-detection signatures.** Reconcile originally compared
`COMPOSE_PROFILES` only and missed overlay/env-only posture changes (`llm=off→record`) —
fixed by growing `posture_signature` (#35). Daemon restarts key on a hand-rolled
`repr((cmd, env))` signature. Each signature grew a dimension only after a bug proved it
was missing.

**3. External capability is not modeled at all.** "A mode is a desired posture, not a
capability" is a deliberate and correct security line — but there is no *continuous*
capability observation, only the on-demand `fy doctor`. The PAM grant behind
`just gcp-elevate`, the validity of the operator's ADC, and "can the minter actually
impersonate the allowed SAs right now" are all links in the chain that `gcp=sa` promises,
and all can lapse while the dashboard shows everything green. The minter then fails
per-request, silently from the posture system's point of view.

**4. Downstream boot-snapshots never reconcile.** A consumer's backend services fetch their
third-party secrets from a cloud secret manager **once at boot**; a fetch that fails during a PAM
lapse is swallowed with a warning and the process runs with blank credentials for its
lifetime → intermittent 401s until someone restarts the container. Nothing re-runs the
fetch when capability returns, because "PAM lapsed and came back" changes no posture
signature — the reconcile machinery is blind to it by construction.

The elevation-lapse report is classes 3+4 compounded: re-elevating does restore minting
within ~60s (the minter is stateless per request; the emulator caches ~60s), but any
service that *booted* during the lapse holds its blank snapshot — so it *looks* like
"permissions don't pass through until `fy down && fy up`", when actually only the
snapshot-holding containers need a restart, and nothing tells you which. The
observability-SDK saga (#25, #37) was class 1: the SDK's *host* was pinned in one tier (a
static env file), its *keys* arrived via another (the boot-time secret fetch), and ADC via
a third (the posture overlay) — so each fix just moved one variable between tiers.

## Proposal: consolidate the reconcilers, model capability, close the snapshot gap

**A. One reconciler contract instead of six hand-rolled ones.** Today there are ~6
independent desired-vs-actual mechanisms (supervisor daemons; stack profiles/overlays;
mirror writes; box-env staleness nag; auth0-sim restart-on-up; staged assets), each with
its own signature scheme and its own staleness-bug history. Generalize what
`posture_signature` did for the stack: every reconciled **scope** declares
`desired(mode) → signature` and `observed() → signature`, and one engine diffs and acts
(or, where it can't act — the box's baked env — surfaces the diff as the nag). New axes
then get reconciliation for free, and "which tier is authoritative for this decision"
becomes explicit in the scope definition rather than folklore. This is the same move the
plugin `Registry` made for credential mechanisms, applied to state.

**B. A `capability_probes(mode)` plugin hook.** The supervisor (already ticking) runs
cheap, cached probes for the active rungs — gcp: a dry-run impersonation of one allowed SA
every ~60s while a gcp rung is on. On failure, the axis renders **degraded** everywhere the
posture shows: `fy mode` / TUI `gcp sa ⚠ DEGRADED — PAM grant lapsed (just gcp-elevate)`,
stamped into the mirror so box sessions see it too, plus a supervisor log nag (the
missing-env nag pattern). This keeps the security line intact — capability is *observed*,
never granted, by the probe — and turns the silent intermittent 401s into a named state.
It's also exactly what `fy doctor` already knows how to check; the change is running the
check continuously and wiring the result into the posture surfaces.

**C. Close the boot-snapshot gap from both sides.**
- *App side (the consumer's):* a boot-time secret loader should not leave a service healthy-but-credless
  under a posture that promises credentials. Either re-attempt still-missing names
  periodically (it already knows which fetches failed), or expose "required-but-missing"
  through the service healthcheck so compose restarts it into a working boot. The
  never-raise-off-Cloud-Run doctrine survives: the zero-secret loop has no
  `GCE_METADATA_HOST` and skips fetching entirely — only the "ADC plausible but fetch
  failed" path retries.
- *Foldyard side:* capability transitions (probe failing→working) are exactly the edge on
  which snapshot-holding services became stale. An overlay/`[[overlay]]` flag like
  `resnapshot_on_capability = ["gcp"]` would let the supervisor bounce just those services
  on the recovery edge — same machinery as `reconcile_posture`, different trigger. (B
  alone may be enough in practice if the app-side healthcheck lands; ship B first.)

**D. Small, local FSMs where lifecycle really is sequential.** `reconcile_once`'s
per-child branching (missing-env → port-conflict → orphan-reap → backoff → spawn) and the
box lifecycle (absent/created/running/stale-env) are genuine little state machines hiding
in `if` chains; making them explicit `Enum`-state objects would simplify those two spots.
That is the honest scope of "use a state machine" — local, not global.

**E. Settle expiry to a coherent posture.** TTL expiry force-writes a de-escalation that
can strand a dependent axis (`gcp=sa` expires under `llm=live` → llm stranded on an
error combination until the next interactive `fy mode`). With dependencies already
declared in `mode_issues`, expiry could cascade dependents down to their defaults in the
same atomic write — an emergency lapse then lands on a posture that *works* offline
instead of one that errors.

## Status (2026-07-16): all five landed

- **A** — landed as the scope contract: `reconcile.py` is the one inventory — every tier
  is a `Scope` class carrying its desired-vs-observed rows (what `fy state` renders — the
  view and the action can no longer drift apart) and its action, wired at the tier's
  existing trigger. The stack edge routes through `StackScope.reconcile`; the daemon and
  capability actions remain the supervisor tick's delegates (deliberately — the tick is the
  security-critical loop and the logic stays next to its tests), named and documented in
  the inventory table. `tests/test_reconcile_scenarios.py` is the conformance suite that
  pins what fires on which trigger, so further consolidation is a keep-green refactor.
- **B** — landed: `CapabilityProbe` plugin hook + the supervisor probe loop
  (`run_capability_probes`, per-probe intervals, transition logging), published to
  `capabilities.json` + each up box's mirror; `fy mode`/`fy state` render DEGRADED with
  the fix. gcp ships a real probe (dry-run impersonation of the active rung's identity).
- **C** — landed foldyard-side (the app-side retry was tried first and removed in review:
  the headline consumers snapshot secrets at **import** — one module binds its auth config
  into module constants, another constructs a module-level SDK client — so a late
  `os.environ` fill-in never reached them and the incident still needed a restart).
  The supervisor now diffs each tick's **per-axis-merged** capability map against the last
  published `capabilities.json` (`capability_edges` — seeded from the file, so a lapse+heal
  spanning a supervisor restart still fires) and reacts to the edges: both edges post a
  macOS notification (`_notify`, `[host] notifications` opt-out), and a heal restarts the
  consumer's `[resnapshot_on_capability]` services on a daemon worker thread
  (`_resnapshot_worker` → `stack.restart_services`, in-flight-guarded, off the
  heartbeat-owning tick). The tick also re-stamps the heartbeat before EACH due probe, so
  back-to-back slow probes (gcp=user's two 20s chains) can't read as a wedged supervisor.
- **D** — landed for the supervisor: the per-daemon branching is now an explicit
  `ChildStep` decision (`_child_step`, pure + unit-tested) with the spawn gates in
  `_spawn_child`. The box lifecycle FSM remains future work.
- **E** — landed: `devmode.settle_incoherent` cascades stranded dependents to their
  defaults in the expiry's atomic write (greedy, downgrade-only, plugin-agnostic via
  `mode_issues`).

All of it is testable with zero secrets: the `[plugins.fakecred]` rig + `fy clock`
fast-forward ([testing-modes.md](./testing-modes.md)) exercise expiry, the cascade, probe
degrade/recover, and daemon lifecycle live; the unit suite covers the same paths.

Design line worth keeping (from review discussion): a capability lapse renders as DEGRADED
but never *writes* the mode — desired posture is user intent and heals in place; only the
structural TTL expiry writes, and E makes that write land coherent. "Degraded" is a status
dimension, not a rung.

## Ordering (as proposed)

B is the highest-leverage/lowest-risk (observe + display only), and directly addresses the
reported incident class; the app-side half of C is its natural companion and lives in
`data/`. A is the real consolidation but is refactor-shaped — do it when the next scope
gets added rather than speculatively. D and E are opportunistic cleanups.
