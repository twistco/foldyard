# Mode/state handling — review and consolidation proposal

> **What it was:** a 2026-07-16 audit of every state-handling bug fix in foldyard's history,
> asking "would a state machine help?", with five proposals. **Status:** finished 2026-07; all
> five landed — see `src/foldyard/reconcile.py` (the scope inventory) and
> `tests/test_reconcile_scenarios.py` / `tests/test_reconcile_model.py`. **Superseded by:** the
> code; this page keeps the failure classes the tests are built around.

(Written before the vocabulary change: *posture* is now **mode**, *axis* **switch**, *rung*
**level** — see the [glossary](../glossary.md).)

## The shape: already a control loop

foldyard's mode handling is declarative reconciliation: `fy mode` writes the desired mode, and
enforcement converges on it — the supervisor tick (~2 s) for host daemons,
`stack.reconcile_posture` for the running stack on any `posture_signature` change, and TTL
expiry applied on read and written back. A global transition-table state machine would fit
*worse*: the state is a product of independent switches × worktrees × lifecycle stages ×
external capability, and none of the historical bugs was "transition A→B handled wrongly".

## Where the bugs came from: four failure classes

**1. Several state tiers with different refresh lifetimes.** The mode lives in the authoritative
host-side `dev-mode.json`; the per-worktree `.dev-mode.json` mirror (refreshed only while that box
is up); env baked into containers at create time (fixed until recreate); and snapshots taken by
app processes at boot. Bug after bug was a decision reading the wrong tier: a liveness check read
a mirror and bounced healthy supervisors; an idle supervisor dirtied clean checkouts with mirror
writes; a box stranded on an old proxy port needed a recreate warning.

**2. Incomplete change-detection signatures.** Reconcile first compared `COMPOSE_PROFILES` only
and missed overlay/env-only changes; daemon restarts keyed on a hand-rolled `repr((cmd, env))`.
Each signature grew a dimension only after a bug proved it missing.

**3. External capability wasn't modelled.** A mode is desired state, not capability — a correct
security line — but nothing observed capability continuously. A lapsed PAM grant, expired ADC or
a token service that can no longer impersonate its service accounts all left the dashboard green
while requests failed.

**4. Boot-time snapshots never reconcile.** A consumer's services fetched secrets once at boot; a
fetch during a PAM lapse left the process running with blank credentials until restarted. No
mode signature changes when capability returns, so reconcile was blind to it by construction.

The reported "elevation lapse" incident was classes 3 and 4 together: re-elevating restored
minting within ~60 s, but services that booted during the lapse kept their blank snapshot, so it
*looked* like `fy down && fy up` was needed.

## The five proposals, and what landed

| | proposal | landed as |
| --- | --- | --- |
| **A** | one reconciler contract: every scope declares desired vs observed and one engine diffs and acts | `reconcile.py`: every tier a `Scope` carrying its desired-vs-observed rows (what `fy state` renders) and its action at the tier's existing trigger. Daemon and capability actions stay the supervisor tick's delegates, named in the inventory. `tests/test_reconcile_scenarios.py` pins what fires on which trigger. |
| **B** | a `capability_probes` plugin hook, run continuously, surfacing DEGRADED | `CapabilityProbe` + the supervisor's probe loop, published to `capabilities.json` and each box's mirror; `fy mode` / `fy state` show DEGRADED with the fix. gcp ships a dry-run impersonation probe. |
| **C** | close the boot-snapshot gap | foldyard-side: the supervisor diffs each tick's capability map against the last published one (seeded from the file, so a lapse spanning a restart still fires); both edges notify, and a heal restarts the consumer's `[resnapshot_on_capability]` services on a worker thread. (An app-side retry was tried and removed: the consumers bind secrets at *import*.) |
| **D** | small local state machines where lifecycle really is sequential | the supervisor's per-daemon branching is an explicit, unit-tested `ChildStep` (`_child_step`). The box lifecycle is still `if` chains. |
| **E** | settle expiry to a coherent mode | `devmode.settle_incoherent` cascades stranded dependents to their defaults in the expiry's atomic write (greedy, downgrade-only). |

All of it is testable with zero secrets: the `[plugins.fakecred]` rig and `fy clock`
([testing-modes.md](../testing-modes.md)).

**The design line worth keeping:** a capability lapse renders DEGRADED but never *writes* the
mode. The desired mode is your intent and heals in place; only TTL expiry writes, and E makes
that write land somewhere coherent. "Degraded" is a status, not a level.
