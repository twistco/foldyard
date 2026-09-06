# ADR-0019 — Third-party plugins from the consumer repo: path-loading gated by a hash-acknowledged trust store

- **Status:** **Superseded by
  [ADR-0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md)** (2026-08-29) — was
  Accepted (2026-07-01), and **never implemented**: there is no `[plugins].load`, no trust store
  and no `fy plugins trust`. The two built-ins whose migration was to be the API-sufficiency proof
  turned out to implement almost nothing but `axes()` — data wearing a class — so ADR-0024 makes
  consumer axes a declarative table instead, and the gate has no customer left to gate.
  Along the way (2026-08-08) it was **narrowed by
  [ADR-0023](./0023-no-host-executed-code-from-the-repo-mount.md)** for host-executed *credential*
  code specifically; ADR-0024 closes the remainder.
  Kept below in full, unedited, as the public record of a design reasoned through and then found
  unnecessary. If a genuinely bespoke host-side mechanism ever appears, start from ADR-0023's
  rejected alternatives rather than from here — they carry the corrected shape (ack by copying the
  bytes *out* of the mount, trust the snapshot, never re-read the working tree).
- **Sources:** spinout-plan D3 (+ Phase 2)

## Context

The plugin hook set is complete (axes, daemons, derive_env, compose_overlays, mode_issues,
stage_assets, doctor, verify, box_args, box_bootstrap, tui_panels, proxy_rules, doctor_fixes) and
the per-consumer registry makes membership config-driven. But third-party discovery exists only
via the `foldyard.plugins` **entry-point group** — the plugin must be an installed distribution,
and since foldyard is a uv tool, installed *into foldyard's tool venv* (`uv tool install foldyard
--with their-plugin`). There is no way to load a plugin from a file in the consumer's own repo.
That is the gap between today and "people write their own plugins in their own repo without
contributing back".

The obvious fix — point config at a repo file and import it — collides head-on with foldyard's
threat model. Plugin code runs **host-side**: in the supervisor process, next to the credential
minters and `host.env`. Meanwhile the repo contents are exactly what foldyard treats as
**untrusted** — the repo is what gets mounted into the box, where agents edit it freely. A
malicious branch (a compromised dependency's postinstall, a poisoned PR an agent was asked to
review, an agent gone wrong) could ship a "plugin" that the supervisor then imports with full
host privileges: a credential exfiltrator sitting beside the minters. That is the precise
supply-chain attack the product exists to stop, pointed at itself. So the trust gate below is
load-bearing, not paranoia.

## Decision

Add repo-relative path loading, gated by a hash-acknowledged trust store (the direnv model):

- **Declaration:** `[plugins].load = ["tools/my_plugin.py:MyPlugin", …]` in `foldyard.toml`,
  repo-relative paths, resolved by `load_plugins()` (`src/foldyard/plugins/__init__.py`)
  alongside the entry-point tier.
- **Trust gate:** a plugin file that is *new* or whose *content changed* is **refused** with a
  clear message — foldyard never imports it, not even to inspect it (an import executes module
  top-level code; "look before trusting" would be the vulnerability). Trust is granted only by
  an explicit `fy plugins trust`, which records `path + sha256` under
  `~/.foldyard/<project>/trusted-plugins.json` — host-side state, deliberately outside the repo
  mount.
- **Host-only granting:** `fy plugins trust` requires a host-side TTY; the box mirror can never
  grant trust. Otherwise an agent in the box could trust its own exfiltrator — the gate must sit
  on the side of the boundary the attacker can't reach.
- **Any change re-refuses:** editing a trusted plugin (including by branch switch) drops it back
  to refused until re-acked, exactly like `direnv allow` after an `.envrc` edit.
- **Isolated loads:** a plugin that throws at load is reported and skipped; it cannot take the
  hot path down — the same guarantee entry-point plugins already have.
- **Two tiers, both documented in the plugin guide:** entry-point plugins remain the
  "installed = trusted" tier (installing a distribution into the tool venv is already an
  explicit, host-side act); path-loaded plugins are the "acked-by-hash" tier.

Phase 2 makes the model prove itself: the two Tangible-shaped built-ins (`auth0_sim`, `llm`)
migrate out of the package into Tangible as path-loaded, trust-acked plugins. If they can't live
outside the package, the plugin API has a hole to fix pre-launch — the migration is the
API-sufficiency proof, not an optional fast-follow.

## Consequences

- Consumers can keep private plugins private — written, versioned, and reviewed in their own
  repo — without publishing a distribution or forking foldyard.
- One deliberate friction point: after every plugin edit (or a branch switch that changes one),
  the next `fy` invocation refuses until `fy plugins trust` is re-run on the host. This is the
  direnv trade and it is accepted; a doctor check surfaces untrusted/changed plugin files so the
  refusal is diagnosable at a glance.
- The trust store is per-project and host-side, so a repo clone on a new machine starts with
  zero trusted plugins — correct: trust is a property of an operator on a host, not of the repo.
- The supervisor's launch paths must resolve trust *before* import, and the refusal must be loud
  in daemons too (a silently skipped plugin whose daemon should be running is a posture bug).

## Rejected alternatives

- **Per-hook capability tiering** (e.g. untrusted plugins may contribute axes but not daemons).
  Nearly every hook is host-powerful: `daemons` is host-side command execution by definition,
  `box_args` can mount arbitrary host paths into the box, `derive_env`/`proxy_rules` shape what
  the credential proxy injects where. A capability matrix would be a fiction of granularity over
  a surface that is almost uniformly privileged — whole-plugin trust is the honest boundary.
- **Ungated path loading** ("the user declared it in their own config, that's consent") — the
  config file travels with the repo; the *branch* is the attacker. Consent recorded in the
  attacker-writable surface is not consent.
- **Import-then-inspect** (load the module, examine it, decide) — importing *is* executing;
  there is no safe inspection of untrusted Python by the process holding credentials.
