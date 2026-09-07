# Changelog

Notable changes, per release. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/) once past `1.0`. Before that, minor versions may
break config or CLI shape, and say so here.

## Unreleased

This repository is a force-pushed snapshot of the foldyard tree in its origin monorepo until
`0.1.0` (see `CONTRIBUTING.md`).

## 0.1.0 — unreleased

- The first release intended for general use.
- **`fy --version`** — foldyard can now name itself. `__version__` resolves lazily from the
  install metadata (PEP 562) instead of being a hand-maintained literal, so it can no longer drift
  from `[project].version`, which is what the box already pins itself to.

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

### Added

- **`[machine].vmtype`** — pin the Lima VM type (`vz`, `qemu`, `krunkit`) rather than taking Lima's
  default. See `docs/isolation-layers.md` for which layer each backend actually gives you, and
  `docs/firecracker-and-microvm-backends.md` for why Firecracker is not one of them.

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
