# What must always hold

The short list is in [AGENTS.md](../../AGENTS.md). This page has the reasoning, and the rules for one
area of the code.

## Repo config is never live input to the host

`foldyard.toml` is inside the mount, so anything that can write the checkout can write it — and the
host's reading of it decides where credentials get injected and what egress goes undecrypted. The
supervisor therefore reconciles from the copy an operator ADOPTED (`configpin`, ADR-0022), and a
tree edit is inert until adopted. Two rules follow when you touch this area:

- **Host-side config reads go through `devmode.worktree_config()`** (a named worktree's adopted
  toml) **or the ambient `config.X()` readers**, whose parse on the host IS the adopted snapshot
  (`config._host_toml`; `{}` when nothing is adopted, and `machine.ensure` refuses then — an empty
  `[machine]` is an unwalled VM). Never `config.resolve(repo=…)` / `config._tree_toml` / a direct
  `tomllib` read of a checkout file for anything with a host consequence: that re-opens the
  channel for every field at once — which is exactly how `[proxy] passthrough` and
  `[[inject]].host` stayed live after four rounds of per-field fixes
  ([ADR-0022](../adrs/0022-host-runs-the-adopted-config.md) — the channel, not the field).
  The launch gate runs first (`preflight.check_or_abort`) and re-runs the command after an
  adoption, since import-time constants (`machine.MACHINE`…) read the config before it. In the
  suite, conftest's `ambient_reads_the_tree` points the seam at the tree (what the tests were
  written against); `tests/test_ambient_adopted.py` runs the real host read.
- **Reporting is part of the fix.** Pinning makes an edit silent by construction, so anything that
  ignores repo config must SAY so — the tick's one-per-change log line + edge notification and the
  `adopted config` doctor row exist so "my config change did nothing" is never a mystery.

## Don't-break list

- The hot path stays venv-free / Textual-free. **`verify` stays the credibility check — never
  weaken its assertions.** Keep echoing the underlying `"$ENGINE"` commands.
- **Don't stop emitting `CONTAINER_HOST`, and don't make `engine()` "prefer docker" again** —
  in-box `up`/`ps`/`verify` all depend on podman reaching the socket via it.
- **No host-executed code from the repo mount, and no command strings from config.** Minters are
  packaged KINDS (`Plugin.secrets` declares what host.env must hold; a `[[secret]]` `how` hint is
  PRINTED, never run). Don't reintroduce a `minter = "<command>"` config key or a minter path under
  `dev_vm_dir()` — that is host code execution for anything that can write the checkout, which is
  what [ADR-0023](../adrs/0023-no-host-executed-code-from-the-repo-mount.md) closed. A new
  mechanism = a new kind, or an
  entry-point plugin. foldyard's OWN children are part of this: run them through
  `selfspawn.python_m` (`python -P -m`), never `[sys.executable, "-m", …]` — their working directory
  is usually a checkout, and plain `-m` imports a `foldyard/` the box planted there first; one run
  in ANOTHER directory takes `selfspawn.env()`, which pins relative `PYTHONPATH` entries.
  Consumer scripts run in a CONTAINER, never on the host —
  `[project].worktree_init` via `worktree._init_in_yard` (skips with a retry hint when the yard
  isn't ready, never falls back). So does `fy worktree add`'s CHECKOUT (`worktree._checkout_in_yard`:
  a host checkout runs the filter drivers the shared `.git/config` names): the host runs only
  `git worktree add --no-checkout`, and with no box image it refuses before any git runs —
  never a host checkout with the filters "neutralised by name". The VS Code attached-container config is AUTHORED by foldyard
  (`vscode._attached_config`) from the ADOPTED `[vscode]` table — `fy code` runs `configpin.gate`
  and reads through `devmode.worktree_config()` like every other host-consequence verb — with
  `remoteUser` and the daemon-port pin as foldyard facts
  ([ADR-0026](../adrs/0026-vscode-attach-config-is-declarative.md)). Don't reintroduce a
  repo-produced document (VS Code's attached-config schema has lifecycle hooks and
  `initializeCommand` runs on the HOST), and don't source the `extensions` list from mount data
  such as `.vscode/extensions.json`: a UI-kind extension installs into the operator's shared
  `~/.vscode/extensions`, so that list is host code execution by another name for anything that
  can write the checkout.
- **Two different env sets on an `InjectRule`.** `requires` gates the whole proxy daemon's spawn
  (so it must never carry one mechanism's credential — a proxy that won't launch
  connection-refuses every box request under always-route); `env` is what that rule's minter may
  READ, since the addon runs minters with a base env plus those names instead of inheriting the
  supervisor's environment, which holds every axis's host.env secret. The addon resolves those
  names itself (the operator's exports, then `host.env`) and the proxy runs with host.env's keys
  stripped from its environment ([ADR-0030](../adrs/0030-the-proxy-reloads-instead-of-restarting.md)).
- **Nothing that varies with posture may reach the proxy's launch env.** The supervisor restarts a
  daemon whose cmd/env changed, and a restart cuts every connection in flight (a mode switch once
  killed an apt download mid-package). Rules, the allowlist's enforce switch and `passthrough` go in
  the spec's `live` data, which the supervisor writes and the addon re-reads; a narrowing closes
  only the connections it no longer allows, through mitmproxy internals pinned by
  `tests/test_proxy_reload_e2e.py`
  ([ADR-0030](../adrs/0030-the-proxy-reloads-instead-of-restarting.md)).
- **A failing minter must never log at ERROR during proxy STARTUP** — mitmproxy's `ErrorCheck`
  addon exits the process ("Error logged during startup, exiting…") on any ERROR logged while
  starting, and the supervisor respawns it, so one rule whose host.env secret is missing
  crash-loops the proxy and cuts ALL egress for that checkout (box sessions see API errors for
  Claude/Codex/etc., whose credentials were fine). This is the same failure `requires` is kept
  credential-free to avoid, reached by a different door. `running()`'s warm-up therefore calls
  `rule.token(warm=True)`, which reports at WARN; the request path — past the startup window —
  keeps ERROR. Pinned by `test_warm_up_mint_failure_stays_below_error`. Any new startup-time
  work in the addon inherits this constraint.
- **Egress grants live in the host store at EVERY level** (`allow-store.json`) — never in
  `foldyard.toml`. Repo config travels with the branch and the box can write it, so a committed
  allowlist lets the yard widen its own allowlist.
- **Plugins stay stdlib-only + import-light** (`plugins.registry()` loads on the hot path).
  Don't register the built-ins as entry points (double-load → duplicate-axis error). When
  adding a plugin hook, thread it through the `Registry` — never let `devmode`/`verify`/`tui`
  name a specific credential mechanism again.
- **Box creation (`fy box up`/`build`) can't run in-box** — it would recreate the running box.
  `box.py` is golden-tested for command shape; exercise real `up`/`build` on a host or the
  nested rig.
- **Root in the Lima guest is boot-time only — never `limactl shell … sudo` from the host.** The
  VM user is the uid the box runs as, and Lima's cloud-init grants it `NOPASSWD:ALL` on EVERY
  boot (the instance id changes each boot). foldyard's one `provision: mode: system` script
  (`assets/machine-wall/guest-boot.sh`, recorded in lima.yaml by `machine._record_provisioning`)
  narrows that grant to `shutdown` and installs the wall as root at each boot; the host then
  reads the guest's report (`/run/fy-wall/state`, no root needed) and fails closed on a
  mismatch. Anything new that needs root in the guest goes INTO that script (a new rendered
  id ⇒ `fy machine stop && fy up`), not into a new sudo call — a sudo path would hand a
  container escape VM-root again. Lima renders the script as a Go template (`{{.User}}`,
  `{{.UID}}`), so no other `{{` may appear in it, and `bash -n` gates it in the tests.
  **Every byte of `guest-boot.sh` and `machine-wall.sh` is in that id** — a comment edit too —
  so any change refuses every existing VM as stale until its user runs `fy machine stop && fy up`.
  Don't reword them in a sweep; batch text changes with a change that needs the re-provision
  anyway. (Their `fy-wall` paths, units and tables keep the old name: renaming those is a
  migration, not wording.)
  **The one exception is an opt-in feature's own root work**, which gets its own recorded script
  under its own marker so it never touches that id: the guest monitor (`assets/monitor/`,
  `# fy-monitor <id>`, ADR-0032) is the one instance. Its rules: the two scripts never mention
  each other's marker (`set_provision` drops entries by a regex on the marker, so a mention would
  delete the other script), it ALWAYS exits 0 (Lima marks the whole boot failed otherwise —
  report problems in `/run/fy-monitor/` instead), a VM that never turned it on records nothing,
  and no recursive `rm` reaches `/run` or `/sys` (Tetragon mounts cgroup2 under
  `/var/run/tetragon`; an `rm -rf` there rmdirs empty system cgroups — seen live).
- **The to-box is read-only OUTSIDE the guest, or it isn't mounted** (ADR-0033). Never mount it
  on a vmType/mountType pair whose share server doesn't enforce `writable: false` (only vz +
  virtiofs and qemu + 9p do), never mount the state dir itself (it holds `host.env` and the keys —
  `verify` exempts exactly the two subdirectories), and never put a secret in it: every box of the
  project reads it. `verify` proves it by a real write from a `--privileged` container, never by
  the `ro` flag (root in the guest can drop the flag and the host still refuses). The from-box is
  box-written: the host makes its slots with `mountwrite.makedirs`, reads it only through the
  transcript sync's no-follow walk, and never mounts it on the podman backend.
- **Under the gVisor posture the box mounts the NARROWED socket, never the runsc socket
  directly.** `box.py` mounts `sandbox.box_socket()` (`podman-runsc-filtered.sock`), not
  `guest_socket()` — the filter (`assets/sandbox/socket_filter.py`, a guest user unit provisioned
  by `sandbox.ensure`) strips the runtime opt-out (`oci_runtime` / `dev.gvisor.*` / compat
  `HostConfig.Runtime`) from every container-create so the box can't escape gVisor on podman ≥ 6.
  Reverting the box to `guest_socket()` re-opens that door. The filter is the enforcement tier;
  the runsc-default socket is only a convenience for the HOST's own trusted `fy box up` create
  (which still uses the raw socket). Three invariants if you touch the filter: it stays
  **fail-closed** (an unparseable create body is refused, never forwarded — else a runtime slips
  past the strip), **keep-alive-correct** (it frames every response so a create is filtered even
  as the *second* request on a reused connection — the bypass a "peek at the first request then
  splice" proxy leaves), and **stdlib-only** (it runs under the guest's `python3`; both backends
  have `/usr/bin/python3`). It is foldyard's own packaged code run in the GUEST, not host code
  from the repo mount, so ADR-0023 is not in tension. `tests/test_socket_filter.py` pins the
  rewrite, the fail-closed refusal, keep-alive and the hijack splice; the podman-6 strip effect
  (a client-chosen runtime honoured, then stripped) is only observable there day to day — the
  shipped guests are podman 5.8, which ignores the field; it was shown live once on a podman
  6.1.1 guest (2026-09-13, docs/isolation-layers.md).
