# Releasing foldyard

Contributor-facing, like [DEVELOPMENT.md](../DEVELOPMENT.md) — deliberately **not** in the
`fy docs` set a consumer install ships (see `docs.py`). Nothing here is a consumer's problem.

## The mechanism, which is already automated

Pushing a `v*` tag runs [`.github/workflows/release.yml`](../.github/workflows/release.yml):

1. **build** — asserts the tag matches `[project].version`, that `CHANGELOG.md` has a section
   for it, and that `uv.lock`'s own `foldyard` entry agrees; then `uv build`, uploading
   `dist/` as an artifact, and the version's CHANGELOG section as a second one.
2. **publish** — downloads that artifact and runs `uv publish` into the `pypi` environment.
3. **github-release** — once PyPI has it, creates the GitHub Release from the notes, with the
   same wheel and sdist attached. It holds `contents: write` and runs no project code.

Publishing uses PyPI **trusted publishing**: PyPI is configured to trust this workflow in this
repo, and `uv publish` exchanges the job's OIDC token for a short-lived upload token. **No API
token exists anywhere** — not in the repo, not in Actions secrets, not on anyone's laptop. The
two jobs are split so the publish job holds `id-token: write` and nothing else, and never runs
project code.

`just publish` is the manual escape hatch (needs `UV_PUBLISH_TOKEN`). It exists for the day
GitHub is down. Reach for the tag first.

## Cutting a release

Between releases, the unreleased changelog lives in [`.changes/`](../.changes/README.md): every
PR a consumer would notice adds one file there. `just release <version>` turns them into a
release, where `<version>` is `X.Y.Z` or `patch` / `minor` / `major` (the next one from
`pyproject.toml`). It runs on `main` and:

1. **Refuses before writing anything** unless the tree is clean (bar `foldyard.toml`, below),
   there is at least one fragment, the version is a step up that isn't tagged yet, and our own
   `foldyard.toml` already names it (step 0). It warns when you ask for a smaller step than a
   fragment's `bump:` says — `patch` with a `minor` change pending.
2. **Bumps the version in both places**: `[project].version` in `pyproject.toml`, *and* the
   `foldyard` entry in `uv.lock`. The lock records the workspace package's own version; bump
   pyproject alone and the lock silently disagrees. (CI catches this too — it did not always.)
3. **Folds `.changes/` into `CHANGELOG.md`** as `## X.Y.Z — YYYY-MM-DD`: any `Summary` prose
   first, then sections in the order Security, Removed, Deprecated, Changed, Added, Fixed, and
   each section's entries in the order they were merged. The fragments are deleted.
4. **Runs `just check`, then `just build`.**
5. **Commits** (`release: X.Y.Z`) and **tags** `vX.Y.Z` — locally. Nothing has left your machine.

Then it stops and prints the push. Before running it:

0. **Add a reasons entry to our own `foldyard.toml`.** We dogfood the version window, so
   `[project.foldyard_version_reasons]` gets a one-line summary of the release, and
   `recommended_foldyard_version` moves to it — `just release` refuses until both name the new
   version, and carries the edit in the release commit (it is the one sentence only you can
   write). Raise `min_foldyard_version` only when the checkout genuinely stops working below it
   — a floor is a refusal, not a preference.

And before pushing, **inspect what will ship**, especially the docs:
`unzip -l dist/*.whl | grep assets/docs`. A `fy docs <topic>` that a bundled skill cites but
`[tool.hatch.build.targets.wheel.force-include]` misses resolves fine from a source checkout and
404s for every consumer. `tests/test_agent_guide.py` guards this in `check`; the wheel listing is
the belt to that braces. A paragraph for the release as a whole (0.3.2's "A security release:
upgrade now") is a `section: Summary` fragment, added before running it.

**Push the commit and the tag together, from the host** — this is the irreversible step:

```bash
git push --atomic origin main vX.Y.Z
```

`--atomic` means both or neither: a tag pushed without its commit would name one `main` doesn't
have. The tag runs the `release` workflow: PyPI, then a **GitHub Release** whose notes are the
version's CHANGELOG section, with the wheel and sdist attached. Watch it, and confirm the version
on [PyPI](https://pypi.org/project/foldyard/).

Until the push everything is local, so a mistake is undone with
`git tag -d vX.Y.Z && git reset --hard HEAD~1`. If `check` or `build` fails partway, fix it and
run `just release X.Y.Z` again **by number**: it recognises the prepared, untagged release and
resumes (a keyword would step past it, so it refuses and says so).

## Things that have bitten

- **A dev box cannot push.** No credential in a box reaches origin, by design — that is the
  product working. `just release` is fine in a box; the push is a host command.
- **`uv run` inside a box rewrites `uv.lock`.** It re-resolves against the box's interpreter and
  leaves ~90 lines of marker churn that then rides along in the next commit. Our `foldyard.toml`
  sets `UV_FROZEN=1` in `[box].env` to stop it; if you hit it anyway, `git restore uv.lock` and
  redo the version line with `sed`, not `uv`.
- **`pyright` needs a Node runtime in the box.** The PyPI package is a wrapper that prefers a
  global `node` and otherwise fetches one from nodejs.org — which the allowlist refuses. The
  `nodejs-for-pyright` entry under `[[box.tools]]` installs Debian's nodejs; a box built before
  that entry existed needs `fy box up` to pick it up. `ty` runs regardless. If pyright could not
  run, **say so** rather than reporting a green typecheck — CI is what actually gates it.
- **A version, once published, is burned.** PyPI refuses re-uploads of a version even after you
  delete it. A botched release is fixed by releasing again, never by re-cutting the same number.
