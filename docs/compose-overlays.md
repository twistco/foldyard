# Posture compose overlays — the `[[overlay]]` table

A foldyard consumer drives its compose stack through **posture axes** (`gcp`, `auth0`, `llm`,
…). Most of what a posture *does* is "layer this extra `docker compose -f` file when the mode
matches." That layering is **pure config**: a `[[overlay]]` array-of-tables in `foldyard.toml`,
matched against the current mode. No plugin code names an overlay path.

This is the same idea as `[[inject]]` (an egress-injector per table entry) and `[[box.tools]]`
(a toolchain install per entry): a generic mechanism the consumer *declares*, not a
Tangible-specific hook baked into the package.

## Schema

```toml
[[overlay]]
file = "dev-stack/compose.identity.yml"     # required — checkout-relative or absolute
when = { gcp = "sa" }                        # optional — see matching below
env  = "FOLDYARD_GCP_IDENTITY_COMPOSE"        # optional — a path-override env var (test/CI hatch)
```

- **`file`** — the compose file to add to the `-f` chain. A checkout-relative path resolves
  against the repo root; a non-existent file is silently skipped (so an overlay for a stack
  component you haven't added yet is inert, not an error).
- **`when`** — a dict of `axis = value`, or `axis = [values]`. The entry matches iff **every**
  named axis holds one of its values: **AND across keys, OR within a list**. A missing or empty
  `when` **always matches** — an unconditional base overlay.
- **`env`** — optional name of an environment variable that, when set, **overrides `file`** for
  that entry. Purely an escape hatch for tests/CI to point one overlay at a scratch file; normal
  runs never set it.

## Ordering — declaration order is `-f` order

Overlays are appended to the compose command **in the order they appear in `foldyard.toml`**, and
`docker compose` lets a later `-f` file override an earlier one's keys. So the table's order *is*
the base→override stack. Put the file others build on first (e.g. an identity overlay before the
storage/auth0/llm overlays that consume that identity), and put an app override before its
data-plane twin.

Only the entries whose `when` matches a given mode appear, but their **relative** order is always
the declaration order — so you reason about the stack once, in one place.

## Worked example

```toml
[[overlay]]                                   # base: app + data ADC at the metadata emulator
file = "compose.identity.yml"
when = { gcp = "sa" }

[[overlay]]                                   # AND: both must hold
file = "compose.auth0-real.yml"
when = { auth0 = "real", gcp = "sa" }

[[overlay]]                                   # OR within one axis
file = "compose.llm.yml"
when = { llm = ["record", "live"] }

[[overlay]]                                   # stacks AFTER compose.llm.yml (later -f wins)
file = "compose.llm-live.yml"
when = { llm = "live" }
```

| mode | `-f` overlays (in order) |
| --- | --- |
| `gcp=sa` | `compose.identity.yml` |
| `gcp=sa auth0=real` | `compose.identity.yml`, `compose.auth0-real.yml` |
| `auth0=real` (no `gcp=sa`) | *(none — the AND isn't satisfied)* |
| `llm=live gcp=sa` | `compose.identity.yml`, `compose.llm.yml`, `compose.llm-live.yml` |

## How it fits the mode system

- **Resolution** lives in `config.matching_overlays(mode)`; `Registry.compose_overlays(mode)`
  lays the table down first, then appends anything a plugin still adds programmatically (see the
  escape hatch below). The `-f` chain and the reconcile's `posture_signature` both consume that.
- **Reconcile for free.** Because an overlay is part of the posture signature (derived env +
  overlay list), adding/removing one across a `fy mode` change flips the signature, so the
  supervisor recreates exactly the affected containers — no special-casing.
- **Gating an optional axis on an overlay.** An axis that would be a no-op without something to
  layer on it can self-gate: `config.overlay_when_axes()` returns every axis name any `when`
  references, and a plugin offers the axis only when it's in that set. (Tangible's `storage` axis
  appears only because an `[[overlay]] when = { storage = "staging" }` is declared.)

## When you still need a plugin

`when` expresses **axis-equality conjunctions and disjunctions** — which covers essentially every
posture overlay. A plugin's `compose_overlays(mode)` hook remains for the rare overlay whose
condition `when` can't state (negation, a value derived across axes, an overlay path computed at
runtime). Plugins keep the parts config can't express anyway: axis declarations + rungs, the
cross-axis `mode_issues` guards, daemons, and `stage_assets`.
