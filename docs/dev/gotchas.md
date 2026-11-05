# Conventions and gotchas

- **Experimental means a register entry, not a word in prose.** A setting you're not ready to
  stand behind goes in `experimental.REGISTER` with what's unproven and what would graduate it,
  gets the `*(experimental)*` tag in docs/configuration.md, and its changelog fragment says
  `experimental: true`. Graduating one is deleting the entry and the tag, plus a `.changes/` entry
  under Changed — review the `graduates_when` lines when cutting a release.
- **Changelog entries are files, not edits.** A change a consumer would notice adds
  `.changes/<slug>.md` (section + bump frontmatter, then the bullet; format in
  [.changes/README.md](../../.changes/README.md)); `just release` folds them into `CHANGELOG.md`.
  Never add an `## Unreleased` section back — it made every pair of open PRs conflict.
  `tests/test_changes.py` validates the pending files in `check`.
- **The docs site renders `docs/` as it is** (`mkdocs.yml`, `.github/workflows/docs.yml`: a
  `--strict` build on every PR that touches docs, deployed from main to
  [twistco.github.io/foldyard](https://twistco.github.io/foldyard/); `just docs-site` locally).
  Write pages for GitHub and `fy docs` as before — `docs-site/hooks.py` adapts them at build time:
  a link out of `docs/` goes to GitHub, a GitHub URL for something the site holds comes back to
  the site, and heading ids are GitHub's, so `#anchor` links work in both. A talk's Marp source
  isn't a page (`exclude_docs`): the site serves the HTML and PDF committed beside it, so after
  editing a deck, render both again (`docs/talks/README.md`) and commit them with the change.
- **Three surfaces TEACH, and none of them fails when it lies:** the bundled skills
  (`assets/skills/`, installed into a consumer's `.claude/skills/`), the `fy init` scaffold, and
  the docs `fy docs` ships. `tests/test_agent_guide.py` is the mechanical guard — every ``fy …``
  command they name must resolve in the live typer app, the scaffold must not offer a key from
  `exposure.IGNORED_KEYS`, and a `fy docs <topic>` they cite must be in the wheel's force-include
  list (running from source hides that: the topic resolves for you and 404s for consumers). Write
  commands in backticks or the guard can't see them. **The skills must not restate `--help`** —
  duplicated verb lists are the part that rots; teach the model, then point at the CLI.
- **The box HAS podman** — so "am I on the host (can manage the VM)?" must be tested with
  `config.in_box()` (`IN_DEVBOX=1` — a preset docker-compat DOCKER_HOST is deliberately NOT enough,
  Linux/WSL2 hosts export one too), NEVER `which("podman")`.
- **Write "host", not "Mac" — in new code, docstrings, messages and docs.** foldyard's split is
  host vs box, and the host being a Mac is a fact about today's users, not about the design (there
  is no platform branching in the package: no `sys.platform`, no `Darwin` test; Linux and WSL2
  hosts are validated in CI, see the two host-tier jobs). The one exception is
  `vscode._write_local_terminal_cwd`: which `terminal.integrated.env.<os>` key VS Code reads IS
  the OS name, so no capability probe can stand in for it. User-facing text says *your computer*
  (see [docs/glossary.md](../glossary.md)); `tests/test_docs_shipped.py` refuses "Mac" in the
  shipped docs, the skills and `--help`. Say **macOS** only
  where the claim really is macOS-only — `brew`, the login keychain, `security add-trusted-cert`,
  Virtualization.framework/`vz`, desktop notifications. One trap: **`host` already means an
  egress HOSTNAME** across the allowlist/proxy surface (`fy allow add <host>`, `[proxy] recommend`,
  `_allowed(host)`), so near that code prefer *host-side*, *the operator*, or *the host machine*
  rather than writing "the host offers the host".
- **`CONTAINER_HOST` is load-bearing** — plain podman's local (nested) mode is broken in the
  box; it only works because foldyard emits `CONTAINER_HOST` (and passes `-e CONTAINER_HOST`
  at box-create).
- **Nested Podman in the box is only partial** — `crun` can't set up a container in-box
  (`/proc/sys` read-only, no devices). foldyard doesn't need it: test with the mocked engine
  (golden tests) or the real socket.
- **Two "repo roots":** `stack.main_repo()` = `git --git-common-dir` (MAIN checkout, even from
  a worktree); `config.repo_root()` = the `foldyard.toml` marker (per-checkout). They diverge
  for worktrees, deliberately — engine setup uses main_repo, posture/state uses config.
- **shellenv error propagation:** `eval "$(foldyard shellenv)"` can't fail the recipe via exit
  code, so on a fatal error shellenv prints `exit 1` to **stdout** (eval runs it) + the reason
  to stderr. Recipes have no `set -euo pipefail` of their own — shellenv emits it.
- **`just` modules:** inside a module file `justfile_directory()` is the **root** justfile's
  dir; use `source_directory()` for the module's own dir.
- **`shlex.quote` leaves safe strings unquoted** (`APP_PORT=3007`, not `'3007'`) — golden
  tests must not assume quotes.
- **`machine.state()` is a lifecycle FLAG, not liveness** — it survives the VM dying underneath
  it. A host crash (or a guest that never signals ready) leaves `running` set while podman has
  torn down the api socket and the network forwarder, and left the hypervisor process orphaned:
  every engine call then dies on a raw `dial unix …: no such file or directory`. Anything
  deciding "can I reach the engine?" must pair it with `machine.responsive()` (a real connect to
  the socket — `exists()` would pass on the stale socket file a killed process leaves). `ensure`
  recovers this itself (stop → `reap_orphans` → start); `reap_orphans` matches processes by
  podman's OWN recorded paths (disk image / EFI store / api socket), never by machine name —
  name matching would let `acme`'s `fy up` kill `acme-two`'s VM. Lima's version is narrower
  and runs inside `start` on both of `ensure`'s paths: a hostagent (Lima's own `ha.pid`) still
  alive after its driver (`qemu.pid`) died is waited for — it exits by itself once it notices,
  but not atomically with flipping the instance to Stopped, and `limactl start` in that window
  refuses with "host agent is running but driver is not" (the WSL2 runner, 2026-09-18) — and
  signalled only if it lingers. A VM whose driver is alive is never touched. On `vz` (macOS) the
  VM runs INSIDE the hostagent — `vz.pid` is `ha.pid` — so a live hostagent is a live VM there;
  reading "no `qemu.pid`" as "driver dead" once made every live vz VM an orphan to signal.
  A HUNG vz VM is Lima's `Broken` status (its hostagent stopped answering) — not Running, so
  `ensure` takes the start path, not the revive: `start` ends a Broken instance with Lima's own
  `limactl stop --force` first (a SIGSTOPped hostagent + removed socket reproduces it; recovered
  in 34 s live, 2026-09-28).
- **Never `limactl list --json`** — that path re-reads its own output through a 64 KiB-a-line
  `bufio.Scanner`, and each line carries the instance's whole config. With the wall's and the
  guest monitor's boot scripts recorded (~68 KB of `lima.yaml`), `list` fails "token too long"
  and prints nothing, so every lookup reads "no such VM" (Lima 2.2.0; the host tier's first
  monitor run). `LimaBackend._instances` and `e2e_host.lima_list` use `--format '{{json .}}'`,
  which writes straight out; split its rows on `\n` only (Go leaves U+0085 unescaped).
- **Port offsets:** `stack._offset` takes `WT_OFFSET`, then a pin, then the offset the host recorded
  for that worktree. Only its fallback, `_hashed_offset`, shells to system `cksum`, for exact parity
  with the original shell implementation.
- **Module-level constants** (`machine.MACHINE`/`BACKEND`, `devmode.PODMAN_MACHINE`/`_BACKEND`) bind
  at import from `config`, and `config.clear_caches()` doesn't rebind them. Tests **monkeypatch the module attrs**. The axis tables aren't constants: `devmode.axes()`,
  `mode_blurb()`, `axis_daemon()` and `emergency()` read the live registry. `config`'s ambient readers
  (`_repo_root_ambient`, `_toml_ambient`, `_identity_ambient`) are `lru_cache`d → tests call
  `config.clear_caches()` (see `conftest.fresh_config`). `plugins.registry()` is also cached — tests build a
  `Registry([...])` directly or `load_plugins(extra=…)` rather than mutate the live one.
  `conftest` pins `FOLDYARD_ENGINE=podman` (autouse) for determinism.
- **`fy down` ends with "Network … still in use"** — expected (the dev box is on that
  network); containers are still removed. Not a regression.
- **`fy up` reclaims disk before building, and ONLY under pressure** (`stack.reclaim`, gated on
  `DiskHeadroom.low` — the same threshold the `engine disk` doctor row warns at, deliberately
  shared so the two can never disagree). Nothing accumulates a reclaim otherwise: a healthy
  store sees no destructive verb at all. Three properties are load-bearing if you touch it.
  *Plain `prune`, never `-a`*: podman's "dangling" means untagged AND not the parent of another
  image, so the layer cache (every intermediate is a parent) is structurally out of reach — a
  prune between two builds still yields `--> Using cache`; `-a` drops every image no container
  uses — the untagged ones a plain prune spares AND the tagged base images — turning a reclaim
  into a re-pull through the egress wall.
  *`until=` is not optional*: a build in flight commits untagged, childless (= dangling) layers,
  so on a box shared by several agent sessions an unguarded prune deletes another session's
  build out from under it. *Removed worktrees' images are a separate sweep* — they're TAGGED
  (`{prefix}-{wt}_{svc}`), so prune can never see them, and `worktree remove` doesn't drop them
  either (it can't: reaping images is exactly the thing that must not race a live build). The
  parse is by rendered service name, both separators, keeping anything ambiguous — see
  `_orphan_project_images`, and note `{prefix}-{service}` (main, docker spelling) has the same
  shape as `{prefix}-{worktree}-{service}`.
