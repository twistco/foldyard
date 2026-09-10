# Changelog

Notable changes, per release. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/) once past `1.0`. Before that, minor versions may
break config or CLI shape, and say so here. How a release is cut:
[docs/releasing.md](./docs/releasing.md).

## Unreleased

## 0.2.1 — 2026-09-10

### Fixed

- **`fy tui` shows every workspace from inside a worktree, and opens on the one you're standing
  in.** The workspace list anchored on `config.repo_root()`, which stops at the *current*
  checkout's `foldyard.toml` — so run from a worktree it looked for siblings under a
  `<worktree>-worktrees` directory that has never existed. The list collapsed to a single card
  labelled "main" whose path was actually the worktree, and no worktree could be seen, switched to
  or acted on from any worktree; the workaround was to go back to the main checkout.

  The set of workspaces is a property of the repo, so it now anchors on the primary checkout
  (`stack.main_repo()`, git's common dir — the same anchor `worktree_keys()` already used) and is
  identical wherever `fy` runs. Where you stand picks the initial SELECTION instead: the TUI opens
  with your own checkout's card highlighted, so the Mode tab, the plugin panels and the workspace
  actions target the checkout a bare `fy up` here would act on. It sets the opening row only —
  later refreshes never yank the highlight back from wherever you moved it.

### Added

- **`fy init` now stamps the version floor it scaffolds for.** A fresh `foldyard.toml` carries
  `[project].min_foldyard_version` set to the `fy` that wrote it — the only version that file is
  known to be right for — with the recommendation and the reasons ledger parked as commented
  lines beside it, and the floor reported on the way out (`✓ wrote … (floor: foldyard >= X)`).

  The window landed in 0.2.0 as something a consumer had to know to write, which meant a repo
  that never heard of it kept the default: no floor at all. Since an older `fy` doesn't fail on
  config it doesn't understand — it ignores those keys and does the old thing, silently — a repo
  with no floor is the case the mechanism exists for, and the scaffold is the one place foldyard
  can put a floor there without asking anyone. A `fy` that can't name its own version
  (`0+unknown` from a source tree, or a local build) leaves the key commented rather than
  stamping a number that means nothing.

- **An architecture diagram, above the README's "Why".**
  `docs/assets/foldyard-architecture.svg` draws the host/yard split in one picture: what runs
  outside the fence (the CLI, the supervisor, the credential minters, the egress proxy, the
  adopted config, the access modes), what runs inside it (the compose stack, the dev box, the
  coding agent, per-worktree boxes), and what `fy verify` checks across the seam.

  The previous drawing had drifted from the code. It credited `verify` with a
  gitleaks/TruffleHog secret scan it has never run — that stays the operator's clean-repo
  PREREQUISITE (`docs/security.md`), and the diagram now says so; it claimed `verify` re-runs
  on every mount when it is on-demand only; it called the yard a podman machine after Lima
  became the default backend; and it led with "no daemon running ⇒ no credential flow", true
  when written but misleading now that `fy up` / `fy box up` start the supervisor themselves so
  the always-on proxy is up. The guarantees that carry that weight today are drawn instead: the
  mode file is host-side and the yard only ever gets a read-only copy, and repo config is inert
  until adopted (ADR-0022). Redrawn as hand-editable SVG (14 KB, one `<style>` block) rather
  than a 350 KB design-tool export, so the next correction is a text edit.

## 0.2.0 — 2026-09-08

### Added

- **A declared version window: `[project].min_foldyard_version` and
  `[project].recommended_foldyard_version`.** A consumer repo can now say which foldyard its
  checkout needs. The floor **refuses to run** below it; the recommendation prints one line and
  continues (`FOLDYARD_NO_VERSION_NUDGE=1` silences it). `fy doctor` shows the window as its own
  row, and reports the nudge even when that variable is set.

  The nudge speaks only on `fy up`, `fy box up` and `fy host`. A warning on every invocation is
  filtered out by the reader within a day and takes the rest of foldyard's stderr with it, so it
  is spent on the verbs that start a session. `docs/configuration.md` covers what a
  recommendation is for — briefly, staging a floor so it lands as a formality rather than an
  ambush — and when to leave it unset.

- **`[project.foldyard_version_reasons]`** — an optional ledger, keyed by version, of why the
  consumer wanted each foldyard it adopted. Both the nudge and the refusal list the entries
  between the version you have and the bound you are pointed at, so the message says what you
  would *gain* rather than only which number to type. Entries are appended and never rewritten,
  so a reason cannot drift out of date with the version it describes, and the floor prunes them
  (anything below it is unreachable).

  The floor refuses rather than warns because `foldyard.toml` is read with `.get()` and no schema:
  unknown keys are tolerated by construction, so an old `fy` against a new config doesn't fail —
  it silently ignores the new keys and does the old thing. A warning is not enough for a failure
  mode that leaves no trace. Raise the floor in the same commit that adds the setting it needs.

  Both bounds are declarative; **foldyard never asks PyPI what the latest release is.** `fy` runs
  inside the box too, where egress is default-deny, so a lookup would mean punching an allowlist
  hole in the zero-egress posture to power a cosmetic message — and a consumer pins its CI
  deliberately so it doesn't float with someone else's release, which makes the repo's own opinion
  of "current" the more useful one.

  Inert until a consumer declares a bound, so existing repos see no change. Note the inherent
  limit: a floor only protects from *this* release onward — any older `fy` ignores the key and
  always will. It can't rescue a migration already in flight; it earns its keep on the next one.
  See `docs/configuration.md` and the 2026-09-08 amendment to ADR-0020.

## 0.1.0 — 2026-09-07

The first release intended for general use, and the end of the extraction: this repository is now
foldyard's home. Up to `0.0.1` it was a squash-start snapshot force-pushed from the monorepo it was
carved out of; from here history is real and pull requests are merged rather than ported.

### Added

- **`fy --version`** — foldyard can now name itself. `__version__` resolves lazily from the
  install metadata (PEP 562) instead of being a hand-maintained literal, so it can no longer drift
  from `[project].version`, which is what the box already pins itself to.
- **`[machine].vmtype`** — pin the Lima VM type (`vz`, `qemu`, `krunkit`) rather than taking Lima's
  default. See `docs/isolation-layers.md` for which layer each backend actually gives you, and
  `docs/firecracker-and-microvm-backends.md` for why Firecracker is not one of them.

### Fixed

- **`fy tui` could crash with `NoMatches` on the Network Log's wall pane.** The 1s panel-refresh
  timer checked that `#network-manage` was mounted and then queried a *different* widget
  (`#network-manage-summary`) without a guard, so a tick landing while the pane was half-mounted
  (startup) or half-detached (teardown) took the whole TUI down.
- **`fy verify` could report `ALL PASS — isolation intact` without having tested anything.**
  Every check in the battery asserts an ABSENCE, so each one passed when its probe merely failed to
  run — and a VM with no images behind a wall with no proxy (foldyard's own default posture, cold
  cache) certified itself clean. Each negative check now carries a positive control, and the
  `git push` refusal reports UNPROVEN rather than PASS when origin was never reached. `_HOST_PATHS`
  was macOS-only (`/Users|/private|/var/folders|/Volumes`), so the host-filesystem leak check passed
  vacuously on Linux and WSL2; it is now derived from the real host home plus the fixed points other
  platforms expose a host filesystem at.

## 0.0.1 — 2026-09-06

First published release, and an **alpha**. The engine has run daily inside Twist's monorepo for
months, but this is its first life as a standalone package — cut early to establish the name on
PyPI and to exercise the release path before it matters. Expect the config and CLI shape to move
before `0.1.0`; see the Status section of `README.md` for what is and isn't validated (the host
side is exercised on macOS only).

- Extracted from Twist's monorepo as a squash-start ([ADR-0013](./docs/adrs/0013-in-repo-carve-out-until-extraction.md)).
- Consumption is PyPI-first ([ADR-0020](./docs/adrs/0020-post-extraction-consumption-model.md),
  amended): `uv tool install foldyard`. Published by tag from the release workflow via PyPI
  trusted publishing — no API token exists anywhere.
