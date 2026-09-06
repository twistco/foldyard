# ADR-0003 — The name "foldyard"; rejected alternatives + availability sweep

- **Status:** Accepted (2026-06-13) — in use everywhere; registration formalities still
  outstanding (spinout-plan D10)
- **Sources:** PLAN.md §3 (Naming, decided 2026-06-13), §11 (the `foldyard box` question),
  Appendix A §6 (the Jetify "devbox" collision), docs/spinout-plan.md D10

## Context

The extraction of the Tangible dev-VM machinery into an open-source tool needed a name that could
carry a CLI, a config file, a PyPI package, and a GitHub org. Constraints from the landscape:

- The `*-box` / `*-sandbox` genre is crowded and agent-flavoured — and this tool deliberately is
  not another agent sandbox (ADR-0002).
- "devbox" as a word is **owned**: Jetify Devbox (the Nix-based product) plus Microsoft Dev Box.
  This ruled out the obvious name for the dev-box concept and later even shaped the subcommand
  choice (`foldyard box`, not `foldyard devbox`).
- The name should carry the product's actual metaphor: a bounded *place* where the work happens,
  not a cage around a process.

## Decision

**The name is `foldyard`.** A fold-yard is the enclosed farmyard where livestock are folded
(penned) for the night. The metaphor earns its keep twice:

- **Stack colocation** — "a yard for your stack": everything that runs untrusted code lives in
  the yard; your machine — keys, browser profile, other repos — stays outside the fence.
- **"Bring the agent into the fold"** — suits the multi-session dev box where agents work
  alongside you.

Surfaces: CLI `foldyard` (aliased as **`fy`** — `fy <verb>` == `foldyard <verb>`), config
`foldyard.toml` in the consumer repo, host state under `~/.foldyard/`, distribution
`uv tool install foldyard` from PyPI. We name the *place*, not the cage.

Availability sweep (2026-06-13):

- PyPI ✓ · npm ✓ · crates.io ✓ · Homebrew formula + cask ✓
- GitHub org/user `foldyard` ✓ (zero repos with the name)
- `foldyard.dev` / `.io` / `.sh` unregistered; `.com` registered (likely unrelated/parked)
- Only web namesake: foldyard.co.uk, an art portfolio — different trade class, no software
  footprint

Explanatory metaphors are reserved for docs, not the name: *cofferdam* (work on the real thing,
in the dry) and *glasshouse* (you can see everything inside the boundary).

## Consequences

- The `fy` alias became the daily-driver surface (`fy up | ps | box | mode | verify | tui`), and
  the dev-box subcommand is `foldyard box` — sidestepping the Jetify/Microsoft "devbox" collision
  recorded as an open question during naming.
- The castle/gate words surfaced during naming (gatehouse / postern / drawbridge) were kept as
  candidates for **plugin names** (credential plugins) if a themed system is ever wanted; the
  default remains boring descriptive names — decide at plugin-API time.
- Still-outstanding formalities, deliberately cheap and reversible (carried into the spin-out
  plan as **D10**): claim the `foldyard` GitHub org, publish a placeholder package to
  PyPI, register `foldyard.dev` (+ `.io` if desired), and run a UK IPO / EUIPO trademark search
  before public launch. None had been executed as of the spin-out plan (2026-07-01); they gate
  the public launch, not the extraction.

## Rejected alternatives

- **stackyard** — eliminated: stackyard.cloud / GitHub `StackyardCloud` is an *active OSS local
  cloud-emulation tool* aimed at a directly adjacent audience.
- **cofferdam** — backup only: the npm name is taken (a TypeScript code-quality tool) and SEO is
  swamped by civil engineering; kept as a README metaphor instead.
- **bailey / glasshouse / motte / gatehouse / drawbridge / postern** — viable, but spent better
  as potential plugin names than as the product name.
- **Anything in the `*-box` / `*-sandbox` genre** — avoided on purpose: those names describe a
  cage around a process; this product is a place a whole stack lives.
