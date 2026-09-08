# Configuration — `foldyard.toml` and the environment

All project config lives in `foldyard.toml` at the repo root — `foldyard init` writes the
starter, and foldyard finds the repo by walking up from your working directory to it. Every
value resolves the same way, everywhere:

1. **an explicit environment variable** (always wins),
2. **`foldyard.local.toml`** — a gitignored, per-developer override file beside
   `foldyard.toml`, deep-merged *over* it,
3. **`foldyard.toml`** — the committed, team-shared file,
4. **the built-in default.**

The deep merge is per-key: a local `[claude] keyless = "oauth"` lands *beside* the committed
`[claude] system_prompt`, it doesn't replace the whole table. Arrays (including
arrays-of-tables like `[[overlay]]`) are replaced wholesale, not merged. Use the local file
for anything personal — your `keyless` choice, a private `[[inject]]` — so it never imposes a
credential prompt on a colleague who doesn't use it. A missing or malformed file simply
contributes nothing.

`foldyard init` writes a commented **`foldyard.local.toml.example`** beside the scaffold and
gitignores `foldyard.local.toml` itself: the template is committed, the copy each developer makes
from it is not.

One config semantic to know up front: several tables are **presence-gated** — declaring the
table, even empty, is the opt-in. `[proxy]`, `[claude]`, `[codex]`, `[vscode]`, and every
`[plugins.<name>]` table work this way. Delete the table to turn the feature off entirely.

### `disabled = true` — the one key that subtracts

A deep merge can only ever *add*, which leaves no way to say "not for me" about a block the team
committed — and because those tables are presence-gated, blanking their keys locally doesn't
switch them off. So any table can carry `disabled = true`, and it is dropped from the resolved
config as if it had never been written:

```toml
# foldyard.local.toml — the repo commits [codex]; you don't have that subscription.
[codex]
disabled = true
```

No Codex CLI installed on box-up, no credential prompt at `fy box up`, no codex row in `fy mode`
or `fy state` — for you, while your colleagues keep it. It works at any depth
(`[plugins.github]`), on entries inside an array-of-tables (an `[[inject]]` rule can be parked
without deleting it), and in either file — so a project can ship a block disabled and a developer
opt *in* with `disabled = false`. The marker itself never reaches the rest of foldyard; pruning
happens after the merge, which is what makes both directions work.

Subtraction is the one config statement whose effect is an *absence*, so `fy config widenings`
lists the blocks you've removed and which file removed them.

And one property that surprises people the first time: **editing these files doesn't change what
the host is doing until you adopt the change** — see [The adopted config](#the-adopted-config-what-the-host-actually-runs)
below. Everything the *box* and the *stack* read is live as always; what's pinned is the half the
Mac acts on.

## `[project]`

Who the project is and what stack it drives.

```toml
[project]
name = "acme"
prefix = "acme"
app = "app"
app_port = "WEB_PORT"
compose = ["compose.yml"]
min_foldyard_version = "0.2.0"
```

- **`name`** — the project key: it names the host state dir `~/.foldyard/<name>/`. Default:
  the repo directory name, lowercased. Env: `FOLDYARD_PROJECT`.
- **`prefix`** — the container/volume/network name prefix (a worktree appends `-<name>`).
  Default: `name`. Env: `FOLDYARD_PROJECT_PREFIX`.
- **`app`** — the compose service `fy shell` targets. Default: `"app"`. Env: `FOLDYARD_APP`.
- **`app_port`** — which `[ports]` key is the browsable app's port; `fy open` opens it and
  the TUI shows it. Port keys are yours, so foldyard never guesses a name here. Absent means
  no browsable-app URL. Env: `FOLDYARD_APP_PORT_KEY`.
- **`compose`** — your compose files, in `-f` order; a single string also works. Paths are
  relative to the active checkout (or absolute). Default: `<dev_vm_dir>/compose.podman.yml`.
  If no configured file exists on disk, the project is treated as stack-less: `fy up` has
  nothing to start and points you at `fy box up`.
- **`ensure_dirs`** — bind-mount source dirs (checkout-relative) your compose file expects to
  pre-exist; foldyard creates them empty on `up`. Default: `[]`.
- **`external_network`** — set `true` to let foldyard own the `{prefix}_default` network's
  lifecycle: pre-created on `up`/`box up`, removed on `nuke`. Your compose file must then
  declare `networks.default` with `external: true` and an explicit `name`. Opt in when the
  dev box's permanent attachment to the network makes compose-owned networks noisy (`down`
  warnings, `box up` before the first `up` failing). Default: `false`.
- **`worktree_init`** — an optional script `fy worktree add` runs in each new worktree, as
  `sh <script> --source <main-repo>` with the new worktree as cwd — use it to copy gitignored
  local config (env files, editor settings) across. Path relative to the repo root, or
  absolute. Default: none. Env: `FOLDYARD_WORKTREE_INIT`.
- **`dev_vm_dir`** — *transitional*: where foldyard's generated assets and the gitignored
  `.dev-mode.json` posture mirror land, relative to the repo root. Default: `"."`. Env:
  `FOLDYARD_DEV_VM_DIR`. Leave it alone unless you want those files tucked into a subdir.
- **`min_foldyard_version`** — a **floor**, not a pin: `fy` refuses to run in this checkout
  below it. Raise it in the same commit that adds a setting an older `fy` cannot honour.
  Default: none — but `fy init` stamps one, set to the `fy` that scaffolded the file (the only
  version that file is known to be right for).
- **`recommended_foldyard_version`** — a nudge, never a block. Printed only on `fy up`,
  `fy box up` and `fy host`. Default: none. Silence with `FOLDYARD_NO_VERSION_NUDGE=1`.
- **`[project.foldyard_version_reasons]`** — an optional ledger of *why this repo wanted* each
  foldyard it adopted, keyed by version. Both messages list the entries between the version you
  have and the bound you are being pointed at. Default: empty.

```toml
[project.foldyard_version_reasons]
"0.2.0" = "the verify false-pass fix; CI runs this"
"0.3.0" = "the Lima backend, for the M-series boxes"
```

```text
▸ foldyard 0.1.0 is behind the 0.3.0 this repo expects. Since yours:
    0.2.0  the verify false-pass fix; CI runs this
    0.3.0  the Lima backend, for the M-series boxes
  Run `uv tool install --upgrade foldyard`.
```

Entries are **appended, never rewritten** — which is the point. A single "why" field next to
the version has to be re-edited on every bump, and the bump where someone forgets is the one
that starts lying. A ledger entry describes a version that is already frozen, so it cannot
drift. It also lets the message say what you would gain across *several* hops rather than only
the newest, which is a much stronger reason to act.

It does not grow without bound, because **the floor is its garbage collector**: once
`min_foldyard_version` is `0.5.0`, every entry below `0.5.0` is unreachable and should be
deleted. What stays live is the versions between your floor and your recommendation — one or
two, if you follow the escalation below.

A version with no entry simply doesn't appear; a malformed key or a non-string reason drops
that line alone rather than blanking the rest.


### What the recommendation is for (and when not to set one)

It is **not** a news feed. foldyard does not tell you a release exists; the consumer repo tells
you which release *it* has adopted. Those are different claims, and only the second is
actionable — a version this repo has never tested is not one you should be upgrading to on its
account.

Its real job is to be **stage one of an escalation that ends in a floor**:

1. You adopt a version — pin CI to it, run the suite, merge.
2. Set `recommended_foldyard_version` to it. Colleagues see one line the next time they start a
   session and upgrade when it suits them.
3. Later, when something actually *needs* that version, raise `min_foldyard_version` to match.

By step 3 almost everyone has already upgraded, so the floor lands as a formality instead of
blocking someone mid-task. That staging is the whole value. Skip step 2 and every floor arrives
as an ambush.

So: **time-box it.** A recommendation that has sat unchanged for months is warning fatigue with
extra steps — either promote it to a floor or delete it. If you find yourself wanting one set
permanently, what you actually want is a floor.

**Why it only speaks on `up` / `box up` / `host`.** A warning printed on every invocation is
filtered out by the reader within a day, and takes the rest of foldyard's stderr with it — and
the people it annoys most would set `FOLDYARD_NO_VERSION_NUDGE` and then never see a nudge
again, including one that mattered. Spending it on the few verbs that start a working session
keeps it worth reading. `fy doctor` reports the window unconditionally for anyone who wants to
ask, including when that variable is set.

This is the bound to leave unset if you are unsure. An absent recommendation costs nothing; a
stale one costs attention every session, and attention does not come back.

### Why the floor is a refusal rather than a warning

foldyard reads `foldyard.toml` with `.get()` and no schema, so unknown keys are tolerated by
construction. An old `fy` against a new config therefore doesn't fail — it silently ignores
the new keys and does the old thing. A warning isn't enough for a failure mode that leaves no
trace, so the floor stops the command.

Both bounds are **declarative, and foldyard never asks PyPI what the latest release is**.
`fy` runs on the host *and* inside the box, where egress is default-deny through the proxy —
a version check would mean punching an allowlist hole in the zero-egress posture to power a
cosmetic message. The consumer's own opinion of "current" is the more useful one anyway: a
repo pins its CI deliberately so it doesn't float with someone else's release.

`fy doctor` shows the window as its own row, and reports the nudge even when
`FOLDYARD_NO_VERSION_NUDGE` is set — that variable silences a per-invocation nag, not an
explicit request to be told everything.

**Inherent limit.** A floor only protects from the release that *implements* it onward; any
older `fy` ignores the key and always will. It can't rescue a migration already in flight —
it earns its keep on the next one.

## `[machine]`

The rootless VM that mounts only this repo. Sizing applies at **first creation** — resize
later with `foldyard machine recreate`.

```toml
[machine]
backend = "lima"
vmtype = "vz"
wall = true
name = "acme"
cpus = 4
memory_mib = 8192
disk_gib = 60
```

- **`backend`** — `"lima"` | `"podman"` | `"native"`. **Default: `lima`** (also what `init`
  writes) — per-project VMs that run concurrently, and the only backend that supports the wall.
  `podman` is one shared podman machine for everything: no concurrent per-project VMs and no
  in-VM wall, but nothing extra to install. `native` is the host's rootless podman socket
  directly — no VM, weakest isolation, an explicit opt-in for Linux/CI. Env: `MACHINE_BACKEND`.

  The backend CLI is **not** the whole prerequisite: it creates the VM, and the container engine
  (`podman`) drives the socket it hands out. So lima needs `limactl` *and* podman; the podman
  backend needs only what you already have. A missing CLI is a hard error when you NAMED the
  backend, and a quiet skip when you didn't — on a host with no VM tooling (Linux, CI) there is
  simply nothing to manage, and foldyard won't abort a verb over a choice you never made.
- **`wall`** — provision the in-VM nftables egress wall, so the box's only way out is the
  Mac-side proxy — fail-closed: traffic that ignores the proxy env is rejected, not silently
  allowed. Lima-only (preflight enforces the pairing); requires `[proxy]` to be declared, or
  the box has no way out at all. Default: `false` (`init` writes `true`). Env: `MACHINE_WALL`
  (`1`/`true`/`on`/`yes`).
- **`vmtype`** — the Lima **driver**, i.e. the hypervisor the VM actually runs on: `"vz"`
  (Apple Virtualization.framework) | `"qemu"` | `"krunkit"` | any external Lima driver plugin.
  Lima-only. **Create-only** — like mounts and sizing, changing it means `fy machine recreate`.
  Env: `MACHINE_VMTYPE`.

  Default: the best of `vz` → `qemu` that *this host registers*, asked of `limactl info` rather
  than inferred from the operating system. That resolves to `vz` on a Mac and `qemu` on Linux,
  and is printed at create so the choice is on the record. The point of pinning it is that the
  VMM is a security decision: QEMU is roughly two million lines emulating decades of hardware
  and is where essentially every published guest→host escape lives, while a Mac on `vz` runs
  neither QEMU nor KVM. Left unpinned, which one you got depended on an unexamined `runtime.GOOS`
  branch inside Lima. See
  [firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md).

  `krunkit` (libkrun — a microVM with a Firecracker-derived device model) is never selected
  automatically: it is upstream-experimental, macOS/arm64 only, and needs `brew install krunkit`.
  Name it explicitly to try it.
- **`name`** — the VM's name. Default: the project name. Env: `PODMAN_MACHINE`.
- **`cpus`** / **`memory_mib`** / **`disk_gib`** — sizing at first creation. Defaults:
  `4` / `8192` / `60`. Env: `MACHINE_CPUS` / `MACHINE_MEMORY` / `MACHINE_DISK`.
- **`worktrees_root`** — the parent dir holding sibling worktree checkouts (mounted into the
  VM alongside the repo). A relative value resolves against the main checkout's path.
  Default: `<repo>-worktrees` beside the repo. Env: `FOLDYARD_WORKTREES_ROOT`.
- **`worktree_base`** — the git ref a brand-new worktree branch forks from. Default: none —
  foldyard auto-detects the default branch (`origin/HEAD`, then `main`/`master`). Set it only
  when that isn't discoverable; the point is that new worktrees fork off the trunk, not off
  whatever branch your main checkout has out. Env: `FOLDYARD_WORKTREE_BASE`.

## `[engine]`

```toml
[engine]
cli = "podman"
```

- **`cli`** — the container-engine CLI that drives the stack. Default: `podman` if installed,
  else `docker`. foldyard exports `CONTAINER_HOST` pointing at the machine's socket, so plain
  podman works everywhere — including inside the box. Env: `FOLDYARD_ENGINE`.

  **The docker fallback is for CI, not a second supported engine.** GitHub-hosted runners ship
  docker and no podman, so foldyard degrades there and `docker compose` runs the same stack.
  On a dev host, a resolved engine of `docker` means podman is missing rather than chosen —
  and podman is needed regardless, since machine lifecycle has no docker equivalent (even the
  lima backend runs a podman service inside the VM). Only names are accepted here, never a
  path: this value is executed host-side, so an arbitrary string would be a code-execution
  channel from repo config (see [ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md)).

## `[ports]`

Host-published port bases. **Keys are the exact env-var names your compose file reads** —
not friendly aliases:

```toml
[ports]
WEB_PORT = 3000
PG_PORT = 5544
```

In-container ports never move; each worktree adds a deterministic per-name offset (1–89) to
every base, so a second stack beside main never collides. No `[ports]` table means worktrees
publish no offset ports.

The offset itself resolves as: `WT_OFFSET` env var → a pin in the main checkout's
`foldyard.local.toml` → a stable hash of the worktree name. Pin an offset when a worktree's
app must land on a known host port (say, one a third-party callback allowlist already
accepts):

```toml
# foldyard.local.toml (main checkout, gitignored)
[worktree-offsets]
my-feature = 2
```

## `[proxy]`

**Presence-gated**: declaring `[proxy]` — even empty — routes the box's egress through the
Mac-side allowlisting proxy (CA mount + `HTTPS_PROXY`/`NO_PROXY` env in the box). Required
whenever `[machine] wall = true`. Absent means a clean box: no proxy env at all.

```toml
[proxy]
default_deny = true
# passthrough = ["@all"]
# no_proxy = ["{project}-postgres", "redis"]
```

- **`default_deny`** — the project's STARTING position for enforcement: when `true`, the proxy
  refuses any host that isn't granted (403 at CONNECT). Built-in default when the key is absent:
  `false` — observe/capture only, never block. `foldyard init` writes `true`, because a scaffold
  ships `recommend` entries covering its own bootstrap, so day-one enforcement costs one round of
  consented yeses rather than a wall of refusals. Blocked hosts show live in `fy tui`, where you
  can grant them (once / until-restart / permanently) without a restart.

  It is a **seed, not the live switch**: once `fy allow wall on|off` has set it, the host-side store
  is authoritative and this key is ignored. Same reason as the grants below — repo config is
  writable from inside the box, and an enforcement switch the yard can flip off for itself is no
  switch at all.

  There is deliberately **no `allow` list here.** Grants of every level live in the host-side
  allow-store (`~/.foldyard/<project>/allow-store.json`, outside the mount) — `fy allow add
  <host> [--level once|session|permanent]`, `fy allow list`, or the TUI's `a` key on a blocked row. That placement is the whole guarantee: config travels with the branch and
  the box can write it, so a `[proxy] allow` list would let the yard widen its own wall by editing a
  file it already owns. A grant is a property of an operator on a host, like the posture itself.
  Any keyless/injector host is allowed implicitly — never list those.
- **`recommend`** — the committed half of the allowlist: hosts this repo ASKS operators to grant,
  each `{ host = "…", why = "…" }` (or a bare host string). Advisory by construction — the proxy
  never reads it. The host OFFERS each entry, per host, at `fy up`/`fy box up`/`fy host`, via
  `fy allow sync`, and in the TUI's Network Log wall pane; the operator answers yes (permanent) /
  session / not now / never, and the answer lands in the host-side store. This is how a team
  shares its allowlist without giving up host-owned grants: the list rides the branch, and every
  machine still consents host by host. Two properties do the security work — the offer reads the
  **adopted** copy (an in-box edit queues nothing until the operator reviews it at the adoption
  gate), and a "never" is remembered (re-offered only if the operator grants it themselves).

  ```toml
  [proxy]
  recommend = [
    { host = "pypi.org", why = "uv/pip — box bootstrap + deps" },
    { host = "unpkg.com", why = "vis-timeline, loaded by the generated pages under test" },
  ]
  ```

  Seeded with the box-bootstrap hosts by `foldyard init`, which is what makes its
  `default_deny = true` survivable: the box comes up walled and working after one round of
  consented yeses. `fy config widenings` reports each entry's standing answer (granted / pending /
  declined).

  **Plugins recommend too, and you don't list theirs.** A declared `[claude]`/`[codex]` (or any
  plugin implementing `Plugin.egress_recommend`) contributes the hosts its OWN install step
  reaches — `claude.ai`/`downloads.claude.ai`, `chatgpt.com`/`releases.openai.com` — into the same per-host
  offer. Foldyard knows where its installers fetch from; a copy of that list in your repo would
  only rot. Injector hosts are still absent from both lists: the proxy exempts those structurally,
  since it has to reach them to mint.

  For an unattended setup (a provisioning script, a fresh CI machine) `fy allow sync --yes` takes
  every pending recommendation at `permanent` in one go. Without it a non-interactive run prints
  the list and grants nothing — "nobody was there to say no" must never read as yes.
- **`passthrough`** — the trusted hosts the proxy TLS-tunnels *without* decrypting when
  capture mode is on; everything else is MITM-decrypted and fully logged. Entries are exact
  hosts, `*.suffix` globs, or `@bundle` refs (`@all` = every built-in toolchain bundle).
  Default: `["@all"]`. An explicit empty list means decrypt everything under capture.

  This one is why the whole file is pinned host-side: a host listed here is exempt from capture,
  so a live-read `passthrough` would let the yard switch off the monitoring it's subject to. Like
  every other key, a change takes effect when you adopt it — see
  [The adopted config](#the-adopted-config-what-the-host-actually-runs). `fy config widenings`
  prints how many hosts your list actually resolves to, and flags a `@bundle` typo (which expands
  to nothing, so it silently trusts *less*).
- **`no_proxy`** — your stack's own hostnames the box must reach **directly**, bypassing the proxy
  entirely. The proxy runs on the Mac and can't resolve a stack-network name, so a proxied call to
  one 502s — after hanging first, which is how it usually presents: a mysteriously slow test
  against an emulator. List the names your containers use for each other:

  ```toml
  [proxy]
  no_proxy = ["{project}-postgres", "redis", "fake-gcs"]
  ```

  `{project}` expands to the compose project name, so one entry covers every worktree's
  container-name prefix. `localhost` and `127.0.0.1` are always included, and foldyard's own
  plugins add their services themselves (the gcp metadata emulator) — you never list those.
  An entry for a service that isn't running is inert, so it's fine to list your whole stack.

  **Entries may not contain a dot**, and `fy up` refuses the config if one does. NO_PROXY is a
  stronger exemption than `passthrough`: a bypassed host doesn't reach the proxy at all, so it is
  neither captured nor subject to `default_deny`. Stack hostnames are single DNS labels, so
  refusing dots admits every real use and structurally excludes every public host — which is what
  keeps a box-writable key from becoming a hole in the wall. To reach an external host, grant it
  on the Mac: `fy allow add <host>`.

## `[[inject]]`

A generic, config-only egress injector: each entry becomes one on/off mode axis
(`fy mode <axis>=on`) plus one proxy rewrite rule. The secret lives only in `host.env` on the
host — it never enters the box or the repo.

```toml
[[inject]]
axis = "tracker"                   # → `fy mode tracker=on`; token = host.env's FY_INJECT_TRACKER
host = "api.tracker.example"       # the host to inject on
header = "Authorization"           # XOR query_param = "userToken"
```

Per entry:

- **`axis`** — the mode-axis name (required for the axis to appear). It also names the token:
  foldyard reads it from `host.env`'s **`FY_INJECT_<AXIS>`** (uppercased, non-alphanumerics folded
  to `_`), and its packaged `static_token` minter passes only that NAME on a command line — the
  value stays host-side. There is deliberately **no `token_env`**: this table is repo config, so a
  consumer-named var would let anything that can write the checkout point a rule at another
  mechanism's secret (`ANTHROPIC_API_KEY`, `GH_PEM_B64`…) and at a `host` of its choosing. Under the
  derived name a rule can only read the var you created for that injector.
- **`host`** — the host to rewrite on (required).
- **`header`** XOR **`query_param`** — inject as a header or a URL query parameter. Neither
  set defaults to the `Authorization` header.
- There is likewise **no `minter = "<command>"`**: a command string in committed config is code the
  host executes, which is the hole the packaged minter kinds closed
  ([ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md)). A mechanism that needs more
  than a static token is a minter KIND in the package (`github-app`, `gh-cli`, the Codex refresh
  flow) or an entry-point plugin.
- **`value_prefix`** — optional prefix for the injected value (e.g. `"Bearer "`).
- **`path_prefix`** — optional: only inject on request paths under this prefix.
- **`replay_on_401`** — optional: re-mint and replay once on a 401. Default: `false`.
- **`ttl`** — optional re-read cadence in seconds for a static token.
- **`label`** — optional; shown in daemon status.

Any number of injectors can be on at once, each with its own host and token.

## `[[secret]]`

Host-side secrets a posture needs **present** before its minter can work. Foldyard's business is
presence, not provenance: it checks `host.env`, surfaces a doctor row, and — on a host TTY —
prompts once for a paste. Where the value comes from is yours to decide.

```toml
[[secret]]
var     = "GH_PEM_B64"                       # the host.env key the minter reads (required)
label   = "GitHub App private key (PEM)"     # shown at the prompt
how     = "gcloud secrets versions access <resource> --impersonate-service-account=<sa> | base64"
pattern = "*-----BEGIN *PRIVATE KEY-----?*-----END *PRIVATE KEY-----*"   # glob the value must match
base64  = true                               # store encoded — host.env is single-line
when    = { github = "app" }                 # posture gate, same semantics as `[[overlay]]`
```

- **`var`** — required; the `host.env` key. Absent ⇒ a loud error at registry build.
- **`label`** — human name at the prompt (defaults to `var`).
- **`how`** — the "where do I get this?" line. **Foldyard PRINTS it; it never runs it.** Executing a
  command string from committed config would re-create host-side execution of repo-controlled code,
  so this stays documentation the operator runs themselves — a gcloud command, an `op read`, a URL,
  "ask Ops".
- **`pattern`** — optional **glob** (`fnmatch`: `*`, `?`, `[seq]`) the value must match, decoded
  first when `base64`. A paste that doesn't match stores **nothing**, so a wrong-shaped secret never
  reaches a minter. A glob rather than a regex on purpose: this is repo config, and repo-controlled
  input to Python's backtracking regex engine makes `fy box up` hangable by a crafted pattern.
- **`base64`** — the value is stored base64-encoded, which is the only way `host.env`
  (single-line `KEY=VALUE`) can carry a multi-line secret like a PEM. It also makes a truncated
  paste fail loudly rather than silently storing half a key.
- **`when`** — posture gate; `axis = value` or `axis = [values]`, AND across keys. When you're
  overriding a plugin's secret, repeat ITS gate — an override must not turn into a prompt for a
  credential the posture never asked for.

Plugins declare their own via the `secrets` hook (e.g. `github=app`'s PEM, whose default `how`
points at the App settings page). A `[[secret]]` row with the same `var` overrides **the fields it
names** and inherits the rest, so retargeting a hint at your own vault — a `gcloud secrets versions
access`, an `op read`, a URL — is one `how = …` line, with no copy of the plugin's `pattern` to rot
the day the plugin tightens it.

## `[[overlay]]`

Posture-conditional compose overlays, pure config: each entry layers an extra compose file
onto the `-f` chain when the current mode matches.

```toml
[[overlay]]
file = "compose.identity.yml"       # required — checkout-relative or absolute
when = { gcp = "sa" }               # optional — AND across keys, OR within a list
env  = "MY_IDENTITY_COMPOSE"        # optional — path-override env var (test/CI hatch)
```

- **`file`** — the compose file to add. A non-existent file is silently skipped.
- **`when`** — `axis = value` or `axis = [values]`; matches iff every named axis holds one of
  its values. Missing/empty `when` always matches (an unconditional base overlay).
- **`env`** — when that variable is set, its value replaces `file` for this entry.

Declaration order is `-f` order — later files override earlier ones. Full semantics,
ordering discipline, and a worked example: [compose-overlays.md](./compose-overlays.md).

## `[[require]]`

Cross-axis coherence requirements, pure config — the consumer tier of the plugins'
`Axis.requires` (same evaluator, same semantics, same synthesized fix message). While `axis`
sits at a rung in `when`, `needs` must sit at a rung in `accepts`; otherwise `fy mode`
refuses the combination (`severity = "error"`, the default) or prints a warning and applies
it (`"warn"`).

```toml
[[require]]
axis = "llm"                 # required — the OWNING axis (whose rungs activate the rule)
when = ["record", "live"]    # required — owning-axis rungs; scalar or list
needs = "gcp"                # required — the required axis, by name
accepts = ["sa", "user"]     # rungs of `needs` that satisfy it; FIRST is the suggested fix
reason = "the runtime-SA identity"   # optional — human name for what's needed
severity = "error"           # optional — "error" (default) | "warn"
message = ""                 # optional — full custom message, overrides synthesis
```

Declare a requirement here when it is a consequence of *your wiring* rather than of the axis
itself — e.g. `llm=record/live` needs the gcp identity only because your `[[overlay]]` routes
LLM traffic through Vertex/ADC; a consumer on another provider would declare a different
`needs` (perhaps an `[[inject]]` credential axis, which no plugin code could name) or none.
Intrinsic couplings ship in the plugin, on its `Axis.requires`.

Semantics match the in-code tier: an absent `needs` axis satisfies nothing (the requirement
still fires); an absent owner *key* in a mode reads as the axis default. Rows merge onto the
owning axis at registry construction, after any in-code rows. Validation is loud: an unknown
owning `axis`, a `when` rung outside its rungs, or a bad `severity` fails every command
rather than becoming a guard that silently never fires.

## `[box]`

The dev box itself. With no `[box].image`, foldyard uses its packaged generic box (engine
client + git + uv) — a stack-less project can `fy box up` with no Dockerfile to author.

```toml
[box]
image = { dockerfile = "box.Dockerfile", tag = "acme-box:latest" }
shadow_volumes = ["web/node_modules"]
caches = [{ volume = "shared-pnpm", path = ".local/share/pnpm" }]
warmup = [{ dir = "web", run = "pnpm install --frozen-lockfile" }]
```

- **`image`** — your own toolchain image: `{ dockerfile, tag?, context?, target?,
  build_args? }`. `dockerfile` is repo-root-relative; `context` defaults to the repo root;
  `tag` defaults to `<prefix>-devbox:latest`; `target` selects a multi-stage stage. The image
  contract is small: an engine client that speaks the mounted socket, plus git. foldyard
  injects its own bits (socket, CLI, proxy CA) at box-up.
- **`sock_in_vm`** — the rootless podman socket path inside the VM, bind-mounted to the box's
  `/var/run/docker.sock`. Default: the active backend's guest socket. Env:
  `PODMAN_SOCK_IN_VM`. You rarely need this.
- **`shadow_volumes`** — in-tree build-artifact dirs (checkout-relative) shadowed with
  per-box named volumes — the workaround for bind-mount uid squashing. Default: `[]`.
- **`caches`** — shared caches mounted into the box: a list of `{ volume, path }`, where
  `path` is relative to the box's `HOME`. Default: `[]`.
- **`warmup`** — background dependency warm-up steps run after box-up: a list of
  `{ dir, run }`, each `run` executed in `<checkout>/<dir>`. Default: `[]`.
- **`env`** — extra static env baked into the box (e.g. tool-cache pinning). `~` in a value
  is expanded against the box's `HOME`. Default: `{}`.
- **`[[box.tools]]`** — one-time tool installs, run as monitored bootstrap steps (reported
  ✓/✗): `{ name, install, check? }`. `check` is a shell guard — skip the install when it
  succeeds; defaults to `command -v <name>`. Keeps project toolchain out of the image when
  you'd rather not rebuild for it.

  ```toml
  [[box.tools]]
  name = "pulumi"
  install = "curl -fsSL https://get.pulumi.com | sh -s -- --install-root /opt/fy-tools --no-edit-path"
  ```

- **`bootstrap`** — free-form shell run once per fresh box, after the structured steps — the
  escape hatch for setup that doesn't fit `[[box.tools]]`. Default: `""`.
- **`clean_docker_config`** — point the box's `DOCKER_CONFIG` at a clean, foldyard-owned
  config dir instead of `~/.docker`. Editor attaches inject a credential helper into
  `~/.docker/config.json` that fails under the box's root user and breaks even anonymous
  pulls; the clean config sidesteps that, and `docker login` still works against it. Default:
  `true`. Set `false` only if your setup genuinely needs its credential helper.
- **`git_index_split`** — install the git index-split shim in the box, so box-side git writes
  its own `.git/index-box` instead of racing host-side git on the shared checkout's index
  (the two-kernel lockfile-atomicity gap on shared mounts). Default: `true`. Turning it off
  just restores plain shared-index behaviour.

## `[claude]` / `[codex]` / `[vscode]`

All three are **presence-gated** — declare the table (even empty) to opt in; delete it for a
plain shell box.

```toml
[claude]
keyless = "oauth"
system_prompt = """
You are root in this project's locked-down foldyard dev box. …
"""

[claude.settings]         # → claude --settings '{"showThinkingSummaries": "…"}'
showThinkingSummaries = true

[codex.config]            # → codex -c 'model_reasoning_summary="auto"' -c tui.raw_output_mode=false
model_reasoning_summary = "auto"
tui = { raw_output_mode = false }
```

- **`[claude]`** — installs Claude Code on box-up and mounts its persisted
  config/transcripts volumes.
  - **`keyless`** — keep the real credential out of the box: a dummy lives inside, and the
    proxy injects the real one (held host-side in `host.env`) in flight. `"oauth"` rewrites
    the bearer token from a `claude setup-token` token; `"api-key"` (or `true`) rewrites
    `x-api-key` on `api.anthropic.com`. Absent/`false` means a normal login — real creds in
    the box's `~/.claude`, persisted across recreation and `fy nuke`. A bare table gets no
    dummy, no onboarding seed and **no mode axis** (nothing host-side to flip), and its login
    + API hosts join the `recommend` offer, since no injector is there to exempt them.
  - **`system_prompt`** — an inline orientation prompt `fy claude` passes via
    `--append-system-prompt`. `init` seeds a generic one; edit it in place. Default: `""`.
  - **`[claude.settings]`** — the non-prompt half of the same idea: a settings table `fy claude`
    passes as JSON to `--settings`, which Claude MERGES into the settings hierarchy — so it
    overrides the box's `~/.claude/settings.json` **per key** rather than replacing it. Write any
    key `settings.json` takes (`model`, `env`, `permissions`, `statusLine`, …). Default: `{}` (no
    flag). An explicit `fy claude --settings …` comes later on the command line and wins.
- **`[codex]`** — installs the OpenAI Codex CLI on box-up (OpenAI's native installer — a static
  binary, so no node needed) and mounts its persisted `~/.codex`.
  - **`keyless`** — `"api-key"` (or `true`) injects the real `OPENAI_API_KEY` as a bearer on
    `api.openai.com`; `"chatgpt"` uses your ChatGPT subscription — the box holds a dummy
    `auth.json` and the host-side minter injects the current access token, refreshed from
    your real one. Absent/`false` means a normal login, with the same bare-table shape as
    `[claude]` above — though in-box that means `codex login --api-key`: the ChatGPT browser
    flow redirects to `127.0.0.1:1455`, a loopback inside the box your browser can't reach,
    which is what `keyless = "chatgpt"` exists to solve.
  - **`system_prompt`** — same key and same job as `[claude]`'s, different delivery: Codex has
    no `--append-system-prompt`, so `fy codex` passes it as `-c developer_instructions="…"`,
    which *adds* one item to the developer message the model already gets. (Not
    `base_instructions`, which would REPLACE Codex's own base prompt.) The value is emitted as a
    quoted TOML string, because `-c` parses its value as TOML and mangles a raw one that happens
    to parse. Default: `""`.
  - **`[codex.config]`** — `[claude.settings]`'s counterpart, in the shape Codex's own
    `~/.codex/config.toml` has: every LEAF becomes one `-c key=value`, with nested tables flattened
    to Codex's dotted paths (`tui = { raw_output_mode = false }` → `-c tui.raw_output_mode=false`).
    Flattened rather than passed whole because `-c tui={ … }` would *replace* the box's entire
    `tui` table instead of overriding one knob. Values are rendered back into TOML (a bool as
    `true`, a string quoted) — `-c` parses them as TOML and silently falls back to a literal string
    when that parse fails. Default: `{}`. Repeated `-c` is last-wins, so `[codex.config]` beats
    `system_prompt` if it sets `developer_instructions`, and `fy codex -c …` beats both.

Both tables also take **`transcript_sync_seconds`** (default `0` = off), which turns the one-shot
`fy transcripts` into a continuous one: while the box is up, the host supervisor promotes that
agent's bound-out transcripts into its durable archive (`~/.claude/projects` / `~/.codex/sessions`)
on that interval, so a crashed box or a `git clean -fdx` costs you nothing instead of everything
since the last `fy box down`.

```toml
[claude]
transcript_sync_seconds = 30    # rounded DOWN to a multiple of the 2s supervisor tick
```

- It runs **host-side**, in `fy host` — the box can't reach the host's home, so it could never
  push. It doesn't have to: the transcripts are already on the host continuously (the box binds
  its `projects/` out to the checkout), so a pass is a host-local rsync between two host paths —
  no engine call. Polling, not a watcher, because host-side inotify doesn't fire for guest writes.
- The interval is expressed in **ticks** (`interval // 2s`, floored, minimum 1), so `30` is 15
  ticks and `15` is 7 ticks = 14s effective. A slow tick stretches the real interval rather than
  firing a catch-up burst.
- **Silent when healthy.** A failed pass logs one line to `host-supervisor.log` when it breaks and
  one when it heals — never one per interval — and escalates to a notification only if it stays
  broken. There is no doctor row on purpose: a failed *archive* sync degrades nothing the box
  does (the bound-out dir still holds every transcript, live), it only means the archive is going
  stale, and `fy transcripts` fixes it by hand.
- Read from the **adopted** config like everything the host acts on, so setting it needs a
  `fy config adopt` before it takes effect.

Both tables deep-merge like everything else, so `foldyard.local.toml` can override a single key
(your model, your reasoning verbosity) without redeclaring the team's. Both also reach a
privileged actor from repo config, so `fy config widenings` lists them under **agent steering** —
by key, since that's where a `hooks` or `permissions` entry would show up.
- **`[vscode]`** — mounts the vscode-server volume so `fy code` (VS Code attach) reuses its
  server across box recreations. No keys.

## `[plugins.<name>]`

Plugin opt-ins — declaring the table (even empty) loads that plugin; a repo that declares
none never sees its mode axes, daemons, or box wiring.

```toml
[plugins.gcp-metadata]
project = "my-gcp-project"
sa_labels = { app = "app-runtime", box = "log-reader" }
```

- **`[plugins.gcp-metadata]`** — the GCP metadata-emulator/minter plugin.
  - **`project`** — the GCP project whose service accounts the minter impersonates. The gcp
    modes need it set. Env: `GCP_PROJECT`.
  - **`sa_labels`** — service-account local-parts by role, e.g.
    `{ app = "app-runtime", box = "log-reader" }`. Default: `{}`.
- **`[plugins.github]`** — the opt-in for the whole `github` axis (`off`/`app`/`user`), plus
  the GitHub App identity the `github=app` rung mints with. Declaring the table — even empty,
  which is all the `github=user` emergency needs — is what makes the axis (and the gh
  CLI/login doctor rows, the box's dummy `GH_TOKEN`, the gh bootstrap) appear at all; a
  consumer without it gets no github surface anywhere. None of the fields are secrets
  (they're identifiers), so they live in committed config rather than `host.env`; the App's
  private KEY is captured separately (see `[[secret]]`).
  - **`app_id`** / **`installation_id`** / **`repo`** — the App's numeric id, its installation
    id, and the bare repo name (not `owner/repo`) the token is scoped to. Env: `GH_APP_ID`,
    `GH_INSTALLATION_ID`, `GH_REPO`.
  - **`permissions`** — optional table narrowing the installation token, e.g.
    `{ issues = "read" }`. Default: `{ pull_requests = "write", issues = "write" }` (the PR-bot
    shape). Env: `GH_APP_PERMISSIONS` (JSON).
- **`[plugins.auth0-sim]`** — an Auth0-simulator harness plugin (consumer-specific; slated to
  move out of the foldyard package into its consumer).
  - **`sim_dir`** — repo-relative dir holding the harness; its `.certs-local/` is where the
    localhost cert lands (the cert doctor checks read it). Absent disables the cert checks.
    Env: `FOLDYARD_AUTH0_SIM_DIR`.
  - **`container`** — the simulator's compose-service suffix (full name =
    `<prefix>-<suffix>`), bounced when a fresh cert is served. Default: `"auth0-sim"`. Env:
    `FOLDYARD_AUTH0_SIM_CONTAINER`.
- **`[plugins.llm]`** — opts into the `llm` mode axis (also consumer-specific and slated to
  move out). No keys.

## The adopted config: what the host actually runs

`foldyard.toml` (and its local overlay) sits **inside the mount**, so anything that can write the
checkout can rewrite it: an in-box agent, an `npm install` lifecycle script, a branch you checked
out to review. Yet parts of it decide host-side credential behaviour — which host an `[[inject]]`
axis hands its token to, which egress `[proxy] passthrough` exempts from decryption. So the host
does **not** read the working tree. It reads a copy you **adopted**, kept in
`~/.foldyard/adopted/<checkout>-<hash>/`, outside the mount and keyed by the checkout PATH — so
nothing the config itself declares can redirect the lookup (see
[ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)).

What this means day to day:

- The first `fy up` / `fy host` in a fresh checkout adopts it once, and says so.
- After that, an edit is **inert on the host** until you adopt it. The supervisor logs the drift
  once, posts a notification, and `fy doctor` shows a warn row — you won't be left wondering why a
  change did nothing.
- `fy up`, `fy box up` and `fy host` ask before starting anything:

  ```
  ⚠ foldyard.toml changed since the host adopted it [main]
    --- adopted/foldyard.toml
    +++ tree/foldyard.toml
    @@ …
    [a]dopt · [r]evert the file · [i]gnore for now (default):
  ```

  **adopt** — run the new config (live within a tick; no supervisor restart).
  **revert** — put the file back to the adopted copy; a file the adopted copy doesn't have (a
  `foldyard.local.toml` that appeared, say) is moved aside into the pin dir rather than deleted.
  **ignore** — keep running the adopted copy and ask again next time. With no TTY (the detached
  launch, CI) nothing is adopted.
- The verbs are the same operations: `fy config status`, `fy config diff`, `fy config adopt`,
  `fy config revert`. All Mac-only — the box must not adopt its own config, exactly like
  `fy allow`.
- **`fy config widenings`** inventories what the adopted config asks the host to allow: how many
  hosts `passthrough` actually exempts from capture (`@all` is one token meaning ~200), where each
  mechanism delivers its credential (including axes that are declared but not yet armed), which
  agent prompts are shared vs personal — and any key that reads as security config but is no
  longer honoured. A `fy doctor` row summarises it and warns on that last group.
- Each worktree has its own adopted copy, since a branch may legitimately declare different
  plugins. Switching branches is drift, and gets asked about.

**Scope:** the pin governs what the HOST does — daemons, injection rules, capture/passthrough, and
the posture surface `fy mode`/the TUI show. Compose files, `[box]` tools and bootstrap, and ports
still read the working tree: their blast radius is the VM the yard already owns.

## Host state: `~/.foldyard/`

Foldyard keeps all host-side state under `~/.foldyard/`, deliberately **outside the repo
mount** — nothing running in the VM or box can read or escalate its own posture.

Cross-project:

- **`ports.json`** — the port-band registry. Each project gets a 200-port band (first-come,
  starting at 41000) for its Mac-side daemons: proxy listeners at `base+0..89` (the worktree
  offset span), minters at `base+100..189`. Bands are stable across restarts; stale entries
  from deleted projects are harmless and can be pruned by editing the file.
  `FY_PROXY_PORT` / `GCP_MINTER_PORT` bypass allocation entirely.

Per project, `~/.foldyard/<project>/`:

- **`host.env`** — the shared identity env (App IDs, captured keyless tokens, injector
  secrets). One per project — the daemons serving every worktree read it.
- **`allow-store.json`** — the authoritative egress grants, at EVERY level (once /
  until-restart / permanent). Outside the mount, so the box can't grant its own egress.
- **`allow-effective.json`** — the resolved allowlist the proxy re-reads per request: the
  store's grants with expired entries dropped.
- **`host-supervisor.log`** — the one supervisor's combined log (its own output plus every
  child daemon's).
- **`host-supervisor.heartbeat`** — the supervisor's liveness stamp, refreshed each
  reconcile tick; launch paths read its age to tell a healthy holder from a wedged one.
- **`main/`** — the primary checkout's per-worktree posture: `dev-mode.json` (the
  authoritative mode file) and `logs/` (the per-daemon JSONL logs — egress proxy, minter).
- **`worktrees/<name>/`** — the same pair for each worktree, so branches hold independent
  postures under the one supervisor.

(An older install may still have a top-level `dev-mode.json`; the main checkout keeps using
it until the per-worktree file exists.)

Inside the repo, foldyard also generates a few gitignored files next to `foldyard.toml`
(`init` fences them into `.gitignore`): `.devbox-ca/`, `.devbox-claude/`,
`.devbox-foldyard/`, and `.dev-mode.json` — the read-only posture mirror the box can see
(informational only; enforcement stays on the host).

## Environment variables

The per-key overrides are listed with their keys above. The globals:

| Variable | What it does |
| --- | --- |
| `FOLDYARD_REPO` | The repo root, skipping CWD-walk resolution. |
| `FOLDYARD_PROJECT` | The project name (state-dir key). |
| `FOLDYARD_ENGINE` | The engine CLI (`podman`/`docker`). |
| `WORKTREE` | The active worktree name; empty = the main checkout. Usually inferred (set inside the box; inferred from CWD on the host). |
| `WT_OFFSET` | Explicit worktree port offset, bypassing pins and the hash. |
| `PODMAN_MACHINE` | The VM name. |
| `MACHINE_BACKEND` / `MACHINE_VMTYPE` / `MACHINE_WALL` / `MACHINE_CPUS` / `MACHINE_MEMORY` / `MACHINE_DISK` | `[machine]` overrides. |
| `FY_PROXY_PORT` | The egress-proxy base port, bypassing the port-band registry. |
| `GCP_MINTER_PORT` | The gcp-minter base port, likewise. |
| `FY_HOST_ALIAS` | The address containers use to reach the host-side daemons — the escape hatch for a customised Lima network whose host gateway differs. |
| `FOLDYARD_COMPOSE_EXTRA` | Extra compose overlay files (path-separator-joined), appended after everything else so an explicit override wins on conflicting keys. |
| `FOLDYARD_NO_VERSION_NUDGE` | Silences the `recommended_foldyard_version` nudge. Never affects the floor, or `fy doctor`'s row. |
| `IN_DEVBOX` | `1` inside the dev box; the signature foldyard's "am I on the host?" guards use. Set by foldyard — don't set it yourself. |

## Internal / advanced

These exist mainly as test/CI hatches or for unusual setups; you should not normally need
them, and none have `foldyard.toml` keys:

- **State-path overrides** — `FOLDYARD_STATE_DIR` (the per-project state root),
  `FOLDYARD_MODE_FILE`, `FOLDYARD_HOST_ENV`, `FOLDYARD_ALLOW_STORE`, `FOLDYARD_ALLOW_FILE`,
  `FOLDYARD_LOG_DIR`, `FOLDYARD_SUPERVISOR_LOG`, `FOLDYARD_HEARTBEAT_FILE` — each repoints
  one of the host-state files/dirs described above.
- **`FY_PORTS_FILE`** — repoints the cross-project port registry.
- **`FOLDYARD_LOG_TAIL_BYTES`** — how much of each JSONL log's tail the TUI panels read
  (default 256 KiB).
- **`FOLDYARD_DEV_VM_DIR`** — overrides `[project] dev_vm_dir`.
- **`FOLDYARD_CHECKOUT`** / **`FOLDYARD_ENV_OVERRIDE`** — set *by* foldyard for the stack:
  the active checkout the stack mounts (main or a worktree dir — distinct from
  `FOLDYARD_REPO`, which is always the main checkout) and the per-checkout env-override file
  path.
