# foldyard — its own dev recipes (install the tool, run the tests).
#
#   just install     # uv tool install (per-machine venv; editable, with the [host] extra)
#   just test [args] # the test suite (unit + headless Textual pilots)
#
# These build/test foldyard. The USER-facing verbs (`fy up`/`mode`/`host`/`doctor`/`tui`) are
# the CLI's, run from a consumer checkout — a consumer's justfile has none of these recipes.
# (`just foldyard <recipe>` was the origin monorepo's spelling, where this file was imported
# as a module; the extraction — ADR-0013 — made it the root justfile.)

# source_directory() = this file's dir, and still the right call should a host justfile ever
# import this one as a module (justfile_directory() would resolve to the importer's dir).
_dir := source_directory()

default:
    @just --list

# Install/upgrade foldyard as a uv tool: an on-PATH `foldyard`/`fy` with its own per-machine venv
# (off the shared mount). --editable ⇒ src edits are live; re-run after dep changes. This is the
# HOST install, so it pulls the `[host]` extra (mitmproxy) — the host runs the :8088 mitmdump proxy
# the box routes egress through (Phase A′). The box installs foldyard BARE (no mitmproxy) — see box.py.
install:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v uv >/dev/null 2>&1 || { echo "✗ uv not found — brew install uv" >&2; exit 1; }
    uv tool install --force --editable "{{_dir}}[host]"
    echo "✓ foldyard installed — try: fy mode | fy doctor"

# Run the test suite (devmode/config/cli unit + headless Textual run_test pilots). The
# uv dev env is kept OFF the shared mount (UV_PROJECT_ENVIRONMENT) so Mac/box runs don't
# thrash one .venv — and keyed PER CHECKOUT (the cksum of _dir): with one shared dev-venv,
# every `uv run` re-pointed the editable foldyard install at whichever checkout ran last, so
# concurrent sessions on main + a worktree silently swapped each other's source mid-test-run
# (import errors for symbols only one branch has, tests exercising the other checkout's code).
# Extra args pass through to pytest — flags (`just test -k cli`)
# AND paths, which resolve against foldyard/ from any CWD (`just test
# tests/test_cli.py`); no args ⇒ the whole suite (pyproject's testpaths).
test *args:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v uv >/dev/null 2>&1 || { echo "✗ uv not found — brew install uv" >&2; exit 1; }
    export UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/foldyard/dev-venv-$(echo "{{_dir}}" | cksum | cut -d\  -f1)"
    cd "{{_dir}}"
    exec uv run --project . pytest {{args}}

# The opt-in egress-proxy e2e(s): a REAL mitmdump + the egress_proxy addon + a fake minter + a
# CA-trusting client (test_proxy_e2e.py), a REAL dev-box container driven by the proxy plugin's
# box wiring over the socket (test_proxy_box_e2e.py), AND the `capture` axis end to end through a
# real box with the MITM CA in its system trust store (test_capture_box_e2e.py) — no real token
# needed. Pulls the `e2e` group (mitmproxy + requests) on demand; plain `test`/`check` skip them.
test-proxy-e2e *args:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v uv >/dev/null 2>&1 || { echo "✗ uv not found — brew install uv" >&2; exit 1; }
    export UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/foldyard/dev-venv-$(echo "{{_dir}}" | cksum | cut -d\  -f1)"
    cd "{{_dir}}"
    exec env FOLDYARD_E2E=1 uv run --project . --group e2e \
        pytest -k "proxy_e2e or proxy_reload_e2e or proxy_box_e2e or capture_box_e2e or metadata_box_e2e" {{args}}

# The subprocess census — a REPORT, not a gate: every process the suite spawns, binary × test,
# aggregated across xdist workers (tests/tools/census.py). Says what the hermetic guard's allowlist
# still lets through (git, cksum, the shell) and, with `FOLDYARD_E2E=1 just census tests/test_*_e2e.py`
# on a Lima host, what the live tiers reach. Extra args pass to pytest; `--tests` is not one of
# them — set CENSUS_TESTS=1 to list the tests under each binary.
census *args:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v uv >/dev/null 2>&1 || { echo "✗ uv not found — brew install uv" >&2; exit 1; }
    export UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/foldyard/dev-venv-$(echo "{{_dir}}" | cksum | cut -d\  -f1)"
    cd "{{_dir}}"
    out="$(mktemp -d "${TMPDIR:-/tmp}/fy-census.XXXXXX")"
    trap 'rm -rf -- "$out"' EXIT
    PYTHONPATH=tests uv run --project . pytest -q -n auto -p tools.census --census="$out" {{args}} || true
    uv run --project . python tests/tools/census.py "$out" ${CENSUS_TESTS:+--tests}

# Run the foldyard CLI from source (without installing) — handy while iterating.
# e.g. `just run mode` / `just run doctor`.
run *args:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v uv >/dev/null 2>&1 || { echo "✗ uv not found — brew install uv" >&2; exit 1; }
    export UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/foldyard/dev-venv-$(echo "{{_dir}}" | cksum | cut -d\  -f1)"
    exec uv run --project "{{_dir}}" foldyard {{args}}

# Lint + format-check (ruff). Auto-fix with `just format`.
lint:
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/foldyard/dev-venv-$(echo "{{_dir}}" | cksum | cut -d\  -f1)"
    uv run --project "{{_dir}}" ruff check "{{_dir}}"
    uv run --project "{{_dir}}" ruff format --check "{{_dir}}"

# Auto-format + apply safe lint fixes (ruff).
format:
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/foldyard/dev-venv-$(echo "{{_dir}}" | cksum | cut -d\  -f1)"
    uv run --project "{{_dir}}" ruff format "{{_dir}}"
    uv run --project "{{_dir}}" ruff check "{{_dir}}" --fix

# Type-check with both pyright (Microsoft) and ty (Astral). The `e2e` group is installed so the
# opt-in proxy/box e2e tests typecheck too (they import cryptography/requests — e2e-only deps; a
# fresh env without the group can't resolve them, which a warm dev venv silently masks).
typecheck:
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/foldyard/dev-venv-$(echo "{{_dir}}" | cksum | cut -d\  -f1)"
    cd "{{_dir}}"
    # Pin the package config explicitly: pyright otherwise walks up and prefers the monorepo's
    # IDE/LSP-only pyrightconfig.json over this project's [tool.pyright] section. ty walks up
    # the same way — safe only while the repo root has no pyproject.toml/ty.toml, so pin it too.
    uv run --group e2e pyright --project pyproject.toml
    uv run --group e2e ty check --project .

# Everything CI gates on: lint + types + the suite (parallel via pytest-xdist).
check: lint typecheck
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/foldyard/dev-venv-$(echo "{{_dir}}" | cksum | cut -d\  -f1)"
    exec uv run --project "{{_dir}}" pytest "{{_dir}}/tests" -n auto

# Run before tagging to see what will ship (`unzip -l dist/*.whl` lists the force-included docs).
# Build the sdist + wheel into dist/ — the artefacts the release workflow publishes.
build:
    #!/usr/bin/env bash
    set -euo pipefail
    rm -rf "{{_dir}}/dist"
    uv build --project "{{_dir}}" --out-dir "{{_dir}}/dist"

# The push is the irreversible half (it runs the PyPI workflow, and PyPI never lets a version
# number be reused, even after a delete), so it stays a command you type, on the host — a dev box
# has no credential that reaches origin. Until then everything is local: `git tag -d vX.Y.Z &&
# git reset --hard HEAD~1` undoes it.
#
# The edits are made in stdlib python (tests/tools/changes.py), not `uv version` or sed: `uv`
# would re-resolve and rewrite uv.lock wholesale (inside a box that is ~90 lines of
# interpreter-marker churn riding along in the release commit), and `sed -i` differs between BSD
# and GNU. `--no-project` keeps this off the project env entirely, so preparing a release can
# never itself touch the lock. Re-running after a failed check resumes by the version's NUMBER.
#
# Cut a release (X.Y.Z or patch/minor/major): fold .changes/, bump, check, build, commit, tag.
release version:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v uv >/dev/null 2>&1 || { echo "✗ uv not found — brew install uv" >&2; exit 1; }
    cd "{{_dir}}"
    # The tag has to name a commit on main: one cut on a branch and then squash-merged would
    # point at a commit main never has.
    [ "$(git branch --show-current)" = main ] \
        || { echo "✗ release from main (on '$(git branch --show-current)')" >&2; exit 1; }
    v="$(uv run --no-project python tests/tools/changes.py prepare "{{version}}" "$(date +%F)")"
    just check
    just build
    paths=(pyproject.toml uv.lock CHANGELOG.md .changes foldyard.toml)
    if [ -n "$(git status --porcelain -- "${paths[@]}")" ]; then
        git add -A -- "${paths[@]}"
        git commit -q -m "release: $v" -- "${paths[@]}"
    fi
    git rev-parse -q --verify "refs/tags/v$v" >/dev/null || git tag -a "v$v" -m "foldyard $v"
    echo
    echo "✓ foldyard $v committed and tagged here. Before pushing, look at what ships:"
    echo "  · unzip -l dist/*.whl | grep assets/docs"
    echo "  · git show --stat HEAD"
    echo "then, ON THE HOST (the PyPI release and the GitHub Release follow from the tag):"
    echo "  git push --atomic origin main v$v"

# The NORMAL path is the tag-driven release workflow (.github/workflows/release.yml — trusted
# publishing, no token anywhere); this needs UV_PUBLISH_TOKEN in the environment.
# Publish dist/ to PyPI from a laptop — the manual escape hatch.
publish: build
    #!/usr/bin/env bash
    set -euo pipefail
    : "${UV_PUBLISH_TOKEN:?set UV_PUBLISH_TOKEN (a PyPI API token) — or tag a release instead}"
    uv publish "{{_dir}}"/dist/*
