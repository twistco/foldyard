# ADR-0013 — Packaging: in-repo carve-out until the interface stops moving; squash-start at extraction

- **Status:** Accepted (PLAN §8 2026-06; squash-start history settled as spinout-plan D8, 2026-07-01)
- **Sources:** PLAN.md §8, spinout-plan D8 + Phase 4

## Context

foldyard was built inside the Tangible monorepo, with Tangible as its first (and initially only)
consumer. The goal was always an open-source extraction, which raised two housing questions long
before extraction day: where does the code live *while* the interface is still churning, and what
history does the public repo start with?

The churn was real, not hypothetical: the plugin hook set gained `compose_overlays` and
`mode_issues` in a single PR (#25), the mode model was reworked from two conflated axes to four
orthogonal ones, and state paths, env-var names, and the registry model all changed shape after
first contact with the consumer. Nearly every one of those changes was **atomic across the
boundary** — a package-side hook change plus the consumer-side `foldyard.toml` / compose overlay
that exercises it, landing in one commit, reviewed and bisected as one change.

The history question has a security dimension. The monorepo's foldyard directory carries two
months of run-logs (HANDOVER, PROGRESS, SPIKE, plan documents) that freely name Tangible staging
project IDs, org references, and colleague activity — exactly the material a public history must
not contain.

## Decision

1. **No git submodule.** Submodules reintroduce the multi-repo friction the monorepo exists to
   kill: detached heads, forgotten pointer bumps, clone flags, and the loss of atomic
   cross-cutting commits.

2. **In-repo carve-out until the interface stops moving.** `foldyard/` lives as a root-level
   sibling of `platform/`/`data/`/`infra/`, a self-contained uv project (`pyproject.toml`,
   `src/foldyard/`, `tests/`). Tangible is consumer #1 of the real interface — the carve-out pays
   for itself even if OSS never ships.

3. **Dual-home by construction.** foldyard ships its own `foldyard.toml` at the package root.
   Because `config.repo_root()` walks up to the *nearest* `foldyard.toml`, that file is
   simultaneously foldyard's dev-box config as a monorepo subdir and the repo-root config of the
   future standalone repo. The invariant that makes it travel verbatim: it references **nothing
   outside `foldyard/`** — no `../`, no absolute paths — guarded by `tests/test_dual_home.py`.
   The package root *is* the future repo root; extraction is a copy, not a restructure.

4. **Truth flips exactly once, at extraction.** Until that day the monorepo is canonical and
   there is no second copy to reconcile. After it, the new repo is canonical and Tangible becomes
   a consumer (ADR-0020).

5. **Squash-start history (D8).** The new repo begins from a **single clean initial commit** of
   the then-current `foldyard/` contents, with the commit message recording "extracted from the
   Tangible monorepo at `<sha>`" for provenance. No `git filter-repo`, no secret-scan of two
   months of history — a gitleaks pass over the one snapshot tree is the whole belt-and-braces
   check (one tree, not a history: cheap). Nothing Tangible-entangled can reach the public
   history **by construction**, because no monorepo commit ever becomes a public commit.

## Consequences

- Cross-cutting changes stay atomic and bisectable for the entire churn period; there is never a
  version-skew window between package and consumer before extraction.
- The public repo's "why" record cannot come from commit archaeology — there is none. So the
  ADRs (this set) must be written **before** extraction, from the historical docs, while both
  live side by side. The private monorepo keeps the full archaeology forever; the provenance sha
  in the initial commit lets maintainers with monorepo access dig when needed.
- External contributors joining post-extraction see a repo that starts at `0.1.0`-era shape with
  no incremental context — a real cost, mitigated by the ADRs and the docs spine (spinout-plan
  Phase 3), which exist precisely because of this decision.
- Extraction mechanics stay small (spinout-plan Phase 4): copy the tree, one commit, port the two
  CI workflows (already Tangible-free), add LICENSE/NOTICE/SECURITY.md.

## Rejected alternatives

- **Git submodule** — detached-head workflow, forgotten bumps, `--recurse-submodules` clone
  ceremony, and no atomic cross-boundary commits; the exact friction the monorepo consolidation
  had just paid to remove.
- **Early split into its own repo** — every interface change during the churn period becomes a
  two-repo dance (publish, bump, pray), and the consumer feedback loop that shaped the plugin API
  would have been an order of magnitude slower.
- **History-preserving extraction (`git filter-repo`)** — technically possible, but it drags two
  months of run-logs through a filter whose correctness must then be *verified* by scanning the
  full rewritten history for staging project names, org references, and colleague activity. The
  squash-start makes that entire risk class unrepresentable instead of merely checked.
