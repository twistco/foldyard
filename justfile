# foldyard — its own dev recipes (install the tool, run the tests).
#
# Imported as a module by the repo-root justfile, so from anywhere in the monorepo:
#   just foldyard install     # uv tool install (per-machine venv; editable)
#   just foldyard test [args] # the test suite (unit + headless Textual pilots)
# Or standalone: `cd foldyard && just <recipe>`.
#
# The USER-facing dev-VM verbs (mode/host/doctor/mode-tui) live in the dev-VM justfile
# (now inlined in the root justfile) — those drive the stack; these build/test foldyard.

# source_directory() = this file's dir (foldyard/) even when imported as a module by the
# root justfile, where justfile_directory() would resolve to the repo root instead.
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
# Extra args pass through to pytest — flags (`just foldyard test -k cli`)
# AND paths, which resolve against foldyard/ from any CWD (`just foldyard test
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
        pytest -k "proxy_e2e or proxy_box_e2e or capture_box_e2e or metadata_box_e2e" {{args}}

# Run the foldyard CLI from source (without installing) — handy while iterating.
# e.g. `just foldyard run mode` / `just foldyard run doctor`.
run *args:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v uv >/dev/null 2>&1 || { echo "✗ uv not found — brew install uv" >&2; exit 1; }
    export UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/foldyard/dev-venv-$(echo "{{_dir}}" | cksum | cut -d\  -f1)"
    exec uv run --project "{{_dir}}" foldyard {{args}}

# Lint + format-check (ruff). Auto-fix with `just foldyard format`.
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

# The NORMAL path is the tag-driven release workflow (.github/workflows/release.yml — trusted
# publishing, no token anywhere); this needs UV_PUBLISH_TOKEN in the environment.
# Publish dist/ to PyPI from a laptop — the manual escape hatch.
publish: build
    #!/usr/bin/env bash
    set -euo pipefail
    : "${UV_PUBLISH_TOKEN:?set UV_PUBLISH_TOKEN (a PyPI API token) — or tag a release instead}"
    uv publish "{{_dir}}"/dist/*

# ------------------------------------------------------------------------------
# Git hooks — thin forwarder to the root hook-runner (config in the root lefthook.yml)
# ------------------------------------------------------------------------------

# lefthook (with `root: foldyard/`) runs us from foldyard/ with the real check as ARGS. Forward
# to the single root `hook-run`, passing our own dir so it cd's there and dispatches the check
# into the dev box (Python 3.12 = CI) — or runs it directly in-box / skips politely when the box
# is down. _dir (= source_directory()) is THIS project's dir even when this file is loaded as a
# module (where justfile_directory() would resolve to the repo root instead).
[group('git hooks')]
hook-run +ARGS:
    @just -f "{{ parent_directory(_dir) / 'justfile' }}" hook-run "{{ _dir }}" {{ quote(ARGS) }}
