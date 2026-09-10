# Releasing foldyard

Contributor-facing, like [DEVELOPMENT.md](../DEVELOPMENT.md) — deliberately **not** in the
`fy docs` set a consumer install ships (see `docs.py`). Nothing here is a consumer's problem.

## The mechanism, which is already automated

Pushing a `v*` tag runs [`.github/workflows/release.yml`](../.github/workflows/release.yml):

1. **build** — asserts the tag matches `[project].version`, that `CHANGELOG.md` has a section
   for it, and that `uv.lock`'s own `foldyard` entry agrees; then `uv build`, uploading
   `dist/` as an artifact.
2. **publish** — downloads that artifact and runs `uv publish` into the `pypi` environment.

Publishing uses PyPI **trusted publishing**: PyPI is configured to trust this workflow in this
repo, and `uv publish` exchanges the job's OIDC token for a short-lived upload token. **No API
token exists anywhere** — not in the repo, not in Actions secrets, not on anyone's laptop. The
two jobs are split so the publish job holds `id-token: write` and nothing else, and never runs
project code.

`just publish` is the manual escape hatch (needs `UV_PUBLISH_TOKEN`). It exists for the day
GitHub is down. Reach for the tag first.

## Cutting a release

`just release <version>` does steps 1–4 and stops. It deliberately does **not** tag: the tag is
the irreversible act — once a version is on PyPI it can never be reused, even after a delete —
so it stays a deliberate human command.

1. **Bump the version in both places.** `[project].version` in `pyproject.toml`, *and* the
   `foldyard` entry in `uv.lock`. The lock records the workspace package's own version; bump
   pyproject alone and the lock silently disagrees. (CI now catches this — it did not always.)
2. **Roll the changelog.** `## Unreleased` → `## X.Y.Z — YYYY-MM-DD`, and open a fresh empty
   `## Unreleased` above it. Every entry needs a **why**, not just a what: this file is what a
   consumer reads to decide whether to bump, and what they lift into their own
   `[project.foldyard_version_reasons]` ledger. Write the entry so a line of it can be pasted
   there verbatim.
3. **Add a reasons entry to our own `foldyard.toml`.** We dogfood the version window, so
   `[project.foldyard_version_reasons]` gets a one-line summary of the release, and
   `recommended_foldyard_version` moves to it. Raise `min_foldyard_version` only when the
   checkout genuinely stops working below it — a floor is a refusal, not a preference.
4. **`just check`, then `just build`.** Inspect what will ship, especially the docs:
   `unzip -l dist/*.whl | grep assets/docs`. A `fy docs <topic>` that a bundled skill cites but
   `[tool.hatch.build.targets.wheel.force-include]` misses resolves fine from a source checkout
   and 404s for every consumer. `tests/test_agent_guide.py` guards this in `check`; the wheel
   listing is the belt to that braces.
5. **Tag and push, from the host.**
   ```bash
   git tag -a v0.2.1 -m "foldyard 0.2.1" && git push origin v0.2.1
   ```
   Then watch the `release` workflow, and confirm the version on
   [PyPI](https://pypi.org/project/foldyard/).

## Things that have bitten

- **A dev box cannot push.** No credential in a box reaches origin, by design — that is the
  product working. Steps 1–4 are fine in a box; step 5 is a host command.
- **`uv run` inside a box rewrites `uv.lock`.** It re-resolves against the box's interpreter and
  leaves ~90 lines of marker churn that then rides along in the next commit. Our `foldyard.toml`
  sets `UV_FROZEN=1` in `[box].env` to stop it; if you hit it anyway, `git restore uv.lock` and
  redo the version line with `sed`, not `uv`.
- **`pyright` needs a Node runtime in the box.** The PyPI package is a wrapper that prefers a
  global `node` and otherwise fetches one from nodejs.org — which the wall refuses. The
  `nodejs-for-pyright` entry under `[[box.tools]]` installs Debian's nodejs; a box built before
  that entry existed needs `fy box up` to pick it up. `ty` runs regardless. If pyright could not
  run, **say so** rather than reporting a green typecheck — CI is what actually gates it.
- **A version, once published, is burned.** PyPI refuses re-uploads of a version even after you
  delete it. A botched release is fixed by releasing again, never by re-cutting the same number.
