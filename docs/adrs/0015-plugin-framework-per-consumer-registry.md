# ADR-0015 — Plugin framework: hooks on a merged Registry, per-consumer and pure

- **Status:** Accepted (2026-06-30) — implemented (per-consumer registry landed with PR #25;
  `compose_overlays`/`mode_issues` hooks added there too)
- **Sources:** PLAN.md §4.2, docs/history/per-consumer-registry-plan.md (Status: IMPLEMENTED),
  `src/foldyard/plugins/__init__.py`

## Context

Foldyard's value is the substrate — mode/posture machinery, the supervisor, the egress proxy
framework, doctor/verify, the TUI — not any one credential mechanism. The original code hardcoded
gcp/github wiring into that substrate; a consumer adding a credential mechanism had to edit core.
The framework inverts this: a plugin *contributes* its mechanism through typed hooks, and the
substrate talks only to the merged view.

Two problems then surfaced. First, the plugin set (and therefore the mode **axes** a consumer
sees) was a process-global, import-time constant: `registry()` was `@lru_cache(maxsize=1)` over a
hardcoded built-in list, and `devmode` snapshotted axes into module globals at import. Every
consumer saw the same four axes regardless of config — a plain `foldyard init` repo got a
simulator-backed `auth0` axis it couldn't use — and a long-lived `fy host`/TUI process could not
hold different registries for different worktrees (which per-worktree posture, ADR-0016,
requires). Second, some built-ins are meaningless without consumer config (a `gcp` axis centred
on a real GCP project), while others are genuinely generic batteries.

## Decision

**Hooks on a merged `Registry`.** `Plugin` (`plugins/__init__.py`) is a base class of no-op
hooks; the full set is: `axes` · `daemons` · `derive_env` · `compose_overlays` · `mode_issues`
· `stage_assets` · `doctor_checks` · `verify_checks` · `box_args` · `box_bootstrap` ·
`tui_panels` · `proxy_rules` · `doctor_fixes`. `Registry` merges them (dicts update, lists
concatenate, in plugin load order — order is meaningful: it sets `fy mode` output order and
compose-overlay `-f` stacking, base→override). The substrate never names a plugin.
Cross-plugin needs are handled
structurally, not by imports: doctor/verify hooks receive context objects (`DoctorContext`,
`VerifyContext`) instead of importing devmode/verify (which would cycle); TUI panels are
data-only (`TuiPanel`/`PanelData` — a plugin never imports Textual, keeping the hot path
stdlib-only); a plugin that aggregates across plugins (the proxy builds its mitmdump from every
plugin's `proxy_rules`) reaches the right registry via a `_registry` back-ref bound in
`Registry.__init__`, not the global.

**Built-ins load directly, never as entry points.** `load_plugins()` imports and instantiates
them: no distribution metadata needed, they work run-from-source, and they cannot be silently
dropped by a broken venv. Entry-point discovery (`foldyard.plugins` group) is for *third-party*
plugins only, and each load is isolated — a broken plugin is skipped rather than taking down the
hot path (`_entry_point_plugins()`).

**Per-consumer registry as a pure function of resolved config.** `registry(config)` builds from
an explicit resolved `Config` (default: the ambient/bound one), cached by config *content*
(`_config_key`: repo_root + worktree + toml, JSON-serialized — content-keyed, not id-keyed, so a
freed dict's reused address can't return a stale registry). Two worktrees with different
`foldyard.toml`s get different registries in one process; identical content is a cache hit.
Axes are snapshotted at construction under the registry's config binding (`config.using`), so
self-gating resolves against the right consumer even in a process holding several registries.

**Core vs declared split, and axes self-gate.** `load_plugins(config)` always loads the CORE —
github, inject, proxy, claude, vscode, codex: the project-agnostic spine plus batteries that are
*inert until their own config table is declared*. The DECLARED set — gcp, auth0_sim, llm — carries
axes meaningless without consumer config, so each loads only when its `[plugins.*]` namespace is
present (`gcp_metadata_declared()` etc.). Independently, every plugin's `axes()` self-gates on
config (the `claude.py` idiom, generalized), so even a loaded-but-unconfigured plugin contributes
no axis: "loaded" and "advertises a posture" are separate questions.

## Consequences

- A generic `foldyard init` repo sees exactly the core surface — no phantom axes; Tangible's toml
  declares gcp/auth0-sim/llm and keeps full batteries. The "small core + plugins" boundary is
  config, not code edits.
- Adding a credential mechanism = shipping a plugin; the substrate is untouched. The `inject`
  plugin proves the floor: a generic header-auth injector is `[[inject]]` config plus one
  `InjectRule`, no code.
- Tests build real registries from synthetic configs instead of monkeypatching a global; the
  registry-plan test strategy adds a generic-name fixture asserting core-set-only as the spinout
  regression guard.
- Registry construction must stay cheap and side-effect-free — it runs on the recipe hot path
  (`foldyard env`/`shellenv`); heavy imports belong inside hook bodies (lazily), never at plugin
  module load.
- Duplicate axis names across plugins are a hard error at registry build, so two plugins can't
  silently fight over one posture dimension.

## Direction of travel

Two moves were planned on top of this (spinout plan D3/D4) and **both were dropped** by
[ADR-0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md), which supersedes ADR-0019.
There is no `[plugins].load`: plugin code runs host-side, next to the minters, and
[ADR-0023](./0023-no-host-executed-code-from-the-repo-mount.md) closed that class by deletion
rather than by gating it. The two Tangible-bound declared plugins do not migrate into Tangible
either — reading them showed `llm.py` implements only `axes()` and `auth0_sim.py` little more, so
they are **deleted** in favour of a declarative `[[axis]]` table plus two small package-side
residues. Entry points remain the only third-party tier: installed = trusted.
