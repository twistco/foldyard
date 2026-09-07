# ADR-0005 — Secretless by default: posture axes/rungs as data, TTLs, host-side state

- **Status:** Accepted (2026-06; four-axis form landed 2026-07-02, PR #25) — implemented
- **Sources:** PLAN.md §4.1 + §6, README.md thesis 2, docs/history/per-consumer-registry-plan.md

## Context

The yard's promise is that untrusted code — an agent, an `npm install`, a lifecycle script —
wakes up with **zero ambient credentials**. But real development sometimes needs real access:
reading staging logs, pushing a PR as a bot, hitting a live LLM. If those grants are ad-hoc
(an env var someone exports, a token file someone mounts), the zero-credential default erodes
one convenience at a time, and there is no way to *see* what the current exposure is, let alone
assert it in `verify`.

We needed a posture model where (a) the resting state is provably secretless, (b) every grant
is an explicit, visible, reversible declaration, (c) high-privilege grants cannot linger by
accident, and (d) nothing running *inside* the boundary can widen it.

## Decision

**Posture is data, not code paths.** Each credential dimension is an `Axis`
(`src/foldyard/plugins/__init__.py`): a name, an ordered tuple of `rungs` (least → most
privileged), a per-rung blurb, the daemon its status maps to, and which rungs are `emergency`.
Plugins *define* axes; the substrate (`src/foldyard/devmode.py`) validates, persists, and
enforces them uniformly. A consumer's active axis set is a pure function of its resolved config
(the per-consumer registry), so `fy mode` never advertises a posture the repo can't back.

- **`rungs[0]` is the axis's zero-secret default** — the rung every axis rests at and reverts
  to. It is *not* forced to be `"off"`: `auth0` defaults to `sim` (an offline simulator is the
  secretless resting state), `storage` to `local`, `llm` to `off`. The default-rung invariant is
  structural: `Axis.__post_init__` refuses a default that is also an emergency rung.
- **Emergency rungs carry a mandatory TTL.** Rungs declared `emergency` (e.g. `gcp=user`,
  `github=user` — "act as me" grants) get an `expires` stamp on every `set_mode`
  (`DEFAULT_TTL` = 1 h). The supervisor's reconcile loop applies expiry to the *authoritative*
  file (`expire_user_modes`), flipping the axis back to its default durably and stopping the
  daemon — an expiry is a de-escalation and is never refused, so emergencies structurally
  cannot linger.
- **Authoritative state lives host-side, outside the mount:** per-worktree posture under
  `~/.foldyard/<project>/<worktree-or-main>/dev-mode.json` (`config.posture_dir()`), with shared
  identity (`host.env`, the supervisor lock, the CA) at the project root. The box sees only a
  **read-only, gitignored mirror** (`.dev-mode.json` in the repo, `config.mirror_file()`) that
  the supervisor refreshes with daemon health while that worktree's box is up — informational
  only, never read for enforcement.
- **The box cannot escalate its own posture.** `devmode.set_mode` hard-refuses under
  `in_box()`: mode changes are Mac-only, because the file that matters is in the Mac home where
  no in-yard process can reach it. Editing the mirror changes nothing.
- **No daemon running ⇒ no credential flows.** Daemons (minters, the injecting proxy) are the
  only path a real credential takes, and the supervisor runs exactly the daemons the current
  mode demands (ADR-0006). Whatever a file inside the yard claims, a stopped daemon mints
  nothing.
- **Coherence is guarded, not assumed:** `Plugin.mode_issues(mode)` lets plugins veto
  nonsensical combinations (`set_mode` refuses `"error"` issues with the full atomic fix in the
  message; `"warn"`s print), and `Plugin.compose_overlays(mode)` stacks each posture's compose
  overlays so several axes can shape the stack at once. Stale mode-file keys (an axis the
  current config no longer activates) are dropped on read by construction.

## Consequences

- The secretless claim is checkable: `fy mode` shows the exact posture, `verify` asserts
  mode-aware invariants (dummy-token-OK / real-token-FAIL), and the TUI renders the same data.
- Adding a credential mechanism means declaring an axis, not threading a new flag — the TTL,
  mirror, refusal-in-box, and reconcile behaviour come for free.
- A worktree is the posture unit (ADR-0016): parallel branches hold different postures without
  either seeing the other's tokens.
- The cost: posture is one more state file to reason about, and the supervisor must be running
  for TTL expiry to fire (mitigated: `read(apply_expiry=True)` also applies expiry on read, so
  a lapsed emergency is never *reported* as live even with no supervisor).

## Rejected alternatives

- **Credentials baked into box env/config at creation** — invisible, unbounded lifetime, and
  revocation would need a re-bake; the exact ambient-credential model the product exists to end.
- **A single online/offline switch** — real needs are per-mechanism (staging logs without LLM
  spend, GitHub App without GCP); one boolean forces all-or-nothing escalation.
- **Forcing `rungs[0] == "off"`** — pre-#25 shape; it conflated "zero-secret" with "disabled"
  and made offline simulators (`auth0=sim`) look like exceptions instead of defaults.
- **Enforcing posture from inside the box** (a guard the box consults) — anything in-yard is
  inside the blast radius and can be patched out by the code it is supposed to constrain.
