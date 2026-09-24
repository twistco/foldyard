# Compose overlays — the `[[overlay]]` table

Most of what a mode does to your compose stack is "add this extra compose file when the mode
matches". That is plain config: an `[[overlay]]` array of tables in `foldyard.toml`, matched
against the current [mode](./modes.md). No plugin code is needed.

(A **mode** is a set of switches, each at a level — e.g. `gcp=sa llm=live`. See the
[glossary](./glossary.md).)

## Schema

```toml
[[overlay]]
file = "dev-stack/compose.identity.yml"     # required — relative to the checkout, or absolute
when = { gcp = "sa" }                        # optional — see matching below
env  = "FOLDYARD_GCP_IDENTITY_COMPOSE"       # optional — an env var that overrides `file`
```

- **`file`** — the compose file to add as another `-f`. A relative path resolves against the
  checkout's root. A file that doesn't exist is skipped silently, so an overlay for a part of the
  stack a branch doesn't have yet does nothing rather than failing.
- **`when`** — a table of `switch = level` or `switch = [levels]`. The entry matches when
  **every** named switch is at one of its levels: **AND across keys, OR within a list**. A missing
  or empty `when` **always matches** — an unconditional base overlay.
- **`env`** — the name of an environment variable that, when set, **replaces `file`** for that
  entry. An escape hatch for tests and CI to point one overlay at a scratch file; normal runs
  don't set it.

## Order: declaration order is `-f` order

Matching overlays are added after your configured compose files, **in the order they appear in
`foldyard.toml`**. `docker compose` lets a later `-f` file override an earlier one, so the table's
order *is* the base → override order. Put the file others build on first (e.g. an identity
overlay before the overlays that use that identity), and an app override before its data-layer
twin.

Only matching entries are added, but they always keep their declaration order — so you reason
about the stack once, in one place.

## Worked example

```toml
[[overlay]]                                   # base: point the app at the identity emulator
file = "compose.identity.yml"
when = { gcp = "sa" }

[[overlay]]                                   # AND: both must hold
file = "compose.auth0-real.yml"
when = { auth0 = "real", gcp = "sa" }

[[overlay]]                                   # OR within one switch
file = "compose.llm.yml"
when = { llm = ["record", "live"] }

[[overlay]]                                   # added AFTER compose.llm.yml (later -f wins)
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

- **A mode change updates the running stack.** The overlay list is part of what foldyard compares
  across a `fy mode` change (with the mode-derived environment). If it changes, foldyard
  re-creates exactly the affected containers of a running stack — nothing to do by hand.
- **`fy state`** shows the overlays the current mode wants on its `stack` row, and names any
  container whose rendered config no longer matches them (the check `fy up` itself makes).
- **A plugin can offer a switch only when an overlay uses it.** A switch that would change nothing
  without an overlay can hide itself: foldyard collects every switch named in any `when`, and a
  plugin offers the switch only if it's in that set. For example, the built-in `gcp` plugin
  offers its `storage` switch only when some `[[overlay]]` has `when = { storage = "staging" }`.

## When you still need a plugin

`when` expresses "these switches at these levels" — which covers almost every overlay. A plugin's
`compose_overlays(mode)` hook remains for the rare condition `when` can't state (a negation, a
value derived from several switches, a path computed at run time); plugin overlays are added
after the table's. Plugins also keep what config can't express: switch declarations and their
levels, token services, and staged assets. Requirements between switches are data — in the
plugin (`Switch.requires`) or in your config (`[[require]]`, see
[configuration](./configuration.md#require)).
