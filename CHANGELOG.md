# Changelog

Notable changes, per release. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/) once past `1.0`. Before that, minor versions may
break config or CLI shape, and say so here.

## Unreleased

This repository is a force-pushed snapshot of the foldyard tree in its origin monorepo until
`0.1.0` (see `CONTRIBUTING.md`).

## 0.1.0 — unreleased

- The first release intended for general use.

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
