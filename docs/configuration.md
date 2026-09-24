# Configuration — `foldyard.toml` and the environment

All project config lives in `foldyard.toml` at the repo root. `foldyard init` writes a starter;
foldyard finds the repo by walking up from your working directory to that file. Terms such as
*mode*, *switch* and *level* are defined in the [glossary](./glossary.md).

## How values resolve

Every value resolves the same way, everywhere:

1. **an environment variable** (always wins),
2. **`foldyard.local.toml`** — your gitignored, personal overrides beside `foldyard.toml`,
3. **`foldyard.toml`** — the committed file the team shares,
4. **the built-in default.**

The local file is deep-merged *over* the shared one, key by key: a local `[claude] keyless`
lands beside the committed `[claude] system_prompt` instead of replacing the table. Arrays,
including arrays of tables such as `[[overlay]]`, are replaced whole. Put anything personal
there (your `keyless` choice, a private `[[inject]]`) so it never puts a credential prompt in
front of a colleague. A missing or malformed file contributes nothing.

`foldyard init` writes a commented `foldyard.local.toml.example` and gitignores
`foldyard.local.toml` itself (at any depth, so worktree copies are ignored too).

**Presence-gated tables.** Declaring the table, even empty, is the opt-in: `[proxy]`,
`[claude]`, `[codex]`, `[vscode]` and every `[plugins.<name>]`. Delete the table to turn the
feature off.

**Edits don't reach your computer until you adopt them.** The box and the stack read the files
live; what runs on your computer (the proxy, the token services, credential injection) reads an
adopted copy. See [The adopted config](#the-adopted-config-what-the-host-actually-runs).

### `disabled = true` — the one key that subtracts

A merge can only add, and blanking a presence-gated table's keys doesn't switch it off. So any
table can carry `disabled = true`, and it is dropped from the resolved config as if never
written:

```toml
# foldyard.local.toml — the repo commits [codex]; you don't have that subscription.
[codex]
disabled = true
```

For you, that means no Codex CLI installed at box-up, no credential prompt, and no codex row in
`fy mode` or `fy state`. It works at any depth (`[plugins.github]`), on single entries of an
array of tables (park one `[[inject]]` rule), and in either file: a project can ship a block
disabled and a developer can opt in with `disabled = false`. Only a real TOML boolean counts.
The marker is removed after the merge and never reaches the rest of foldyard.
`fy config widenings` lists the blocks you've removed and which file removed them.

### Renamed keys

| Old name | Current name |
| --- | --- |
| `[machine] wall` | `[machine] firewall` |
| `[machine] host_wall` | `[machine] host_firewall` |
| `[proxy] default_deny` | `[proxy] enforce` |
| `axis` in `[[inject]]` / `[[require]]` | `switch` |
| `MACHINE_WALL` / `MACHINE_HOST_WALL` | `MACHINE_FIREWALL` / `MACHINE_HOST_FIREWALL` |

The old names still work as aliases (if a file has both, the new one wins); `fy doctor` and
`fy config widenings` flag them. When you rename them, raise `min_foldyard_version` in the same
commit: an older `fy` reads the new names as absent and would, for example, silently leave the
VM firewall off.

Some keys are no longer honoured at all — `[proxy] allow`, `[[inject]] minter`, `[[inject]]
token_env`, `[vscode] workspace_file`. `fy config widenings` names each one with its
replacement, and `fy doctor` warns about them.

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

- **`name`** — the project key; names the state dir `~/.foldyard/<name>/`. Default: the repo
  directory name, lowercased. Env: `FOLDYARD_PROJECT`.
- **`prefix`** — container/volume/network name prefix (a worktree appends `-<name>`). Default:
  `name`. Env: `FOLDYARD_PROJECT_PREFIX`.
- **`app`** — the compose service `fy shell` targets. Default: `"app"`. Env: `FOLDYARD_APP`.
- **`app_port`** — which `[ports]` key is the browsable app; `fy open` opens it and the TUI
  shows it. Port keys are yours, so foldyard never guesses. Default: none (no app URL). Env:
  `FOLDYARD_APP_PORT_KEY`.
- **`compose`** — your compose files in `-f` order (a single string works too), relative to the
  active checkout or absolute. Default: none — the project is box-only. Every stack verb (`fy up`,
  `fy ps`, `fy logs`, `fy shell`, `fy build`, `fy down`, `fy nuke`) then says so and points at
  its `fy box …` counterpart where one exists; `fy up` still starts the VM and the supervisor.
  A declared file missing from the checkout is an error naming the file.
- **`ensure_dirs`** — bind-mount source dirs (checkout-relative) your compose file expects;
  created empty on `up`. Default: `[]`.
- **`external_network`** — `true` lets foldyard own the `{prefix}_default` network: created on
  `up`/`box up`, removed on `nuke`. Your compose file must then declare `networks.default` with
  `external: true` and an explicit `name`. Use it when the box's permanent attachment makes a
  compose-owned network noisy (`down` warnings, `box up` before the first `up` failing).
  Default: `false`.
- **`worktree_init`** — a script `fy worktree add` runs for each new worktree, to copy
  gitignored local config (env files, editor settings) across. It runs in a throwaway container
  in the VM from the box image, never on your computer, as `sh <script> --source <main-repo>`
  with the new worktree as the working directory. The path is relative to the repo root and must
  stay inside the repo. If the box image isn't built yet it is skipped with the command to run
  later (`fy worktree init <name>`). Default: none. Env: `FOLDYARD_WORKTREE_INIT`.
- **`dev_vm_dir`** — *transitional*: where generated assets and the `.dev-mode.json` mirror
  land, relative to the repo root. Default: `"."`. Env: `FOLDYARD_DEV_VM_DIR`.
- **`min_foldyard_version`** — a **floor**, not a pin: `fy` refuses to run in this checkout
  below it. Must be a string. Default: none, but `fy init` stamps the version that scaffolded
  the file — unless that version can't be compared (`0+unknown` from a bare source tree, or a
  local build), in which case it writes a commented placeholder instead.
- **`recommended_foldyard_version`** — a nudge, never a block, printed only on `fy up`,
  `fy box up` and `fy host restart`. Default: none. `FOLDYARD_NO_VERSION_NUDGE=1` silences it.
- **`[project.foldyard_version_reasons]`** — a ledger of *why* this repo adopted each version,
  keyed by version. Both messages list the entries between your version and the one you're
  pointed at. Default: empty.

```toml
[project.foldyard_version_reasons]
"0.2.0" = "the verify false-pass fix; CI runs this"
"0.3.0" = "the Lima backend, for the M-series boxes"
```

```text
▸ foldyard 0.1.0 is behind the 0.3.0 this repo expects. Since yours:
    0.2.0  the verify false-pass fix; CI runs this
    0.3.0  the Lima backend, for the M-series boxes
  Run `uv tool upgrade foldyard`.
```

Append entries; never rewrite them — an entry describes a version that's already frozen, so it
can't go stale. Delete entries below the floor: they can no longer be shown. A malformed key or
a non-string reason drops that one line.

### Using the two version bounds

1. Adopt a version: pin CI to it, run the suite, merge.
2. Set `recommended_foldyard_version` to it. Colleagues see one line at their next session start
   and upgrade when it suits them.
3. When something actually *needs* that version, raise `min_foldyard_version` to match. By then
   almost everyone has upgraded, so the floor blocks nobody mid-task.

Time-box a recommendation: one left unchanged for months is noise — promote it to a floor or
delete it. Leave it unset if unsure.

Why it works this way:

- **The floor refuses instead of warning** because foldyard ignores unknown keys, so an old `fy`
  reading a new config silently does the old thing.
- **The nudge speaks only on session-start verbs** so it stays worth reading. `fy doctor` always
  shows the version window, even with `FOLDYARD_NO_VERSION_NUDGE` set.
- **foldyard never asks PyPI for the latest release.** `fy` also runs in the box, where egress
  goes through the allowlist; the repo's own tested version is the useful answer anyway.
- **A floor only protects from the release that implements it onward** — an older `fy` ignores
  the key.

## `[machine]`

The rootless VM that mounts only this repo. Sizing, `vmtype` and mounts apply at **first
creation**; change them with `fy machine recreate`.

```toml
[machine]
backend = "lima"
vmtype = "vz"
firewall = true
host_firewall = true
name = "acme"
cpus = 4
memory_mib = 8192
disk_gib = 60
```

- **`backend`** — `"lima"` | `"podman"`. Default: `"lima"` (also what `init` writes): one VM per
  project, running concurrently, and the only backend with the VM firewall. `"podman"` is one
  shared podman machine for everything — nothing extra to install, but no per-project VMs and no
  VM firewall. There is no VM-less option; a config still naming the retired `native` backend
  gets a warning and the podman backend
  ([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)). Lima needs `limactl` *and*
  `podman` (which drives the VM's socket). A missing CLI is a hard error, whether you named the
  backend or inherited the default. Env: `MACHINE_BACKEND`.
- **`firewall`** — the VM firewall: nftables rules in the VM, installed as root at boot, so the
  box's only way out is the proxy on your computer. Traffic that ignores the proxy settings is
  rejected, not let through. Lima only (preflight enforces it), and needs `[proxy]` declared, or
  the box has no way out at all. Default: `false` (`init` writes `true`). Env:
  `MACHINE_FIREWALL` (`1`/`true`/`on`/`yes`).
- **`host_firewall`** — the host firewall: the same rules again on your computer, matching the
  VM process's own traffic, so a guest-kernel exploit that removes the VM firewall still can't
  get out. It allows only DNS, this project's port range, and the VM's own loopback plumbing
  (the SSH forward, Lima's DNS resolver). Default: `false`. Env: `MACHINE_HOST_FIREWALL`.
  - **Needs** `firewall = true` and a Linux host with `nft`, cgroup v2 and a kernel built with
    `CONFIG_NFT_SOCKET` (the rule matches `socket cgroupv2`). macOS can't provide it and the stock
    WSL2 kernel lacks that option; preflight refuses rather than silently downgrading (on WSL2 the
    VM firewall still applies).
  - **You install it once** — foldyard never loads it
    ([ADR-0028](./adrs/0028-no-elevation-on-the-host-operator-applies.md)). `fy machine
    host-firewall` writes the user slice the VM runs in
    (`~/.local/share/systemd/user/fy-machine-<vm>.slice`) and prints the table, a system unit
    that loads it, and the root commands to install them; `--uninstall` prints the removal
    steps. Launch verbs refuse until it's installed.
  - **Every `fy up` checks it's enforcing**, from inside the VM's slice, and refuses with the
    reason if not (not installed; installed before a reboot changed the slice's id; the port
    range changed). `fy doctor` shows the same row. A VM already running outside its own slice
    is refused with `fy machine stop && fy up`. `fy machine stop` and `fy machine rm` leave the
    install alone.
- **`vmtype`** — the Lima driver, i.e. the hypervisor: `"vz"` (Apple Virtualization.framework) |
  `"qemu"` | `"krunkit"` | any external Lima driver plugin. Lima only, create-only. Default: the
  best of `vz` → `qemu` that `limactl info` reports on this computer (so `vz` on macOS, `qemu` on
  Linux), printed at create. Pin it deliberately: the hypervisor is a security decision, and QEMU
  is where nearly every published VM escape lives (see
  [isolation-layers.md](https://github.com/twistco/foldyard/blob/main/docs/isolation-layers.md#macos-arm64--the-machine-layer-does-the-work)).
  `krunkit` (libkrun microVM) is never picked automatically: experimental upstream,
  macOS/arm64 only, needs `brew install krunkit`. Env: `MACHINE_VMTYPE`.
- **`runtime`** — `"gvisor"` runs the box, and every container the box creates, under gVisor's
  userspace kernel (`runsc`) instead of directly on the VM kernel — layer ③ in
  [isolation-layers.md](https://github.com/twistco/foldyard/blob/main/docs/isolation-layers.md).
  A kernel exploit from the box then has to get through gVisor before it reaches the engine
  socket, the stack and your checkout. Default: unset (the engine's own runtime); any other
  value is an error. Env: `MACHINE_RUNTIME`.
  - `machine ensure` (run by `fy up`/`fy box up`) installs a pinned, checksummed `runsc` user-level
    in the VM plus a second podman API socket that defaults to it, and creates the box through
    that socket. Both backends.
  - The box gets a *filtered* view of that socket as its own engine socket: the filter strips
    runtime-selecting fields from every container create, so the box can't opt a container out
    of gVisor, and refuses a create it can't parse. A box that came up under another runtime is
    removed before bootstrap.
  - Changing it means `fy box down && fy box up` (the box, not the VM).
  - Cost: about 1.2× on a Python test suite, 2–4× on sub-second git/lint calls.
  - File watching: edits made on your computer don't fire `inotify` in the box, so a dev server
    that hot-reloads on them must poll (`CHOKIDAR_USEPOLLING=1`, Vite `server.watch.usePolling`,
    webpack `watchOptions.poll`). Edits made in the box work normally.
  - `fy verify` in the box adds a row checking the kernel it runs on; its VM mount audit reads
    `N/A` there, so run `fy verify` on your computer for that.
- **`name`** — the VM's name. Default: the project name. Env: `PODMAN_MACHINE`.
- **`cpus`** / **`memory_mib`** / **`disk_gib`** — sizing at first creation. Defaults: `4` /
  `8192` / `60`. Env: `MACHINE_CPUS` / `MACHINE_MEMORY` / `MACHINE_DISK`.
- **`worktrees_root`** — the parent dir of sibling worktree checkouts, mounted into the VM
  beside the repo. Relative values resolve against the main checkout's path. Default:
  `<repo>-worktrees` beside the repo. Env: `FOLDYARD_WORKTREES_ROOT`.
- **`worktree_base`** — the git ref a new worktree branch forks from. Default: auto-detected
  (`origin/HEAD`, then `main`/`master`), so new worktrees fork off the trunk rather than whatever
  your main checkout has out. Env: `FOLDYARD_WORKTREE_BASE`.

**Podman Desktop** (no `foldyard.toml` key — it's a personal preference). `fy machine desktop`
registers the VM as a podman connection named `fy-<machine>`, so each project appears as its
own entry once you turn on "Load remote system connections (ssh)" in Podman Desktop's
preferences. Where Podman Desktop is installed, every `fy up`/`fy box up` keeps the entry
current and pins the VM's ssh port to its port range (from the next VM start). `fy machine rm`
removes the connection. Env: `FOLDYARD_PODMAN_DESKTOP=0` opts out, `=1` forces it on where
foldyard doesn't detect the install.

## `[engine]`

```toml
[engine]
cli = "podman"
```

- **`cli`** — the container-engine CLI: `"podman"` or `"docker"`, as a name, never a path (the
  value is run on your computer — [ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md)).
  Default: `podman` if installed, else `docker`. Env: `FOLDYARD_ENGINE`.

The docker fallback exists for CI runners, which ship docker and no podman. On a dev machine you
need podman regardless: VM lifecycle has no docker equivalent. foldyard exports `CONTAINER_HOST`
pointing at the VM's socket, so plain podman works everywhere, including in the box.

## `[ports]`

Ports your stack publishes on your computer. **Keys are the exact env-var names your compose
file reads.**

```toml
[ports]
WEB_PORT = 3000
PG_PORT = 5544
```

In-container ports never move. Each worktree adds a fixed per-name offset (1–89) to every base,
so a second stack beside main never collides. No `[ports]` table means worktrees publish no
offset ports.

The offset resolves as: `WT_OFFSET` env → a pin in the main checkout's `foldyard.local.toml` →
a hash of the worktree name. Pin one when a worktree's app must land on a known port (say, one
a third-party callback allowlist accepts):

```toml
# foldyard.local.toml (main checkout, gitignored)
[worktree-offsets]
my-feature = 2
```

## `[proxy]`

**Presence-gated.** Declaring `[proxy]`, even empty, routes the box's internet traffic through
the proxy on your computer (the box gets its CA and `HTTPS_PROXY`/`NO_PROXY`). Required when
`[machine] firewall = true`. Absent: no proxy settings in the box at all.

```toml
[proxy]
enforce = "learn"
# passthrough = ["@all"]
# no_proxy = ["{project}-postgres", "redis"]
```

- **`enforce`** — where the allowlist *starts*:
  - `true` — refuse any host not granted (403 at CONNECT) from the first run.
  - `"learn"` — the first `fy up`/`fy box up` opens a one-hour learn window: nothing is refused,
    every host that *would* be is recorded with the client's User-Agent, and enforcement turns
    on by itself when the window ends. `fy allow learn` then grants what it recorded in one
    reviewed batch. `init` writes this.
  - `false` — observe only, never block. Default (key absent).

  Any other value counts as `true`, so a typo never loosens the allowlist. It is a *seed*: once
  `fy allow enforce on|off|learn` has run (or a learn window has), the setting stored on your
  computer wins and this key is ignored — the box can write repo config, so it mustn't be able
  to switch enforcement off. Reopen a window with `fy allow enforce learn --for 30m` (8 h max);
  end one early with `fy allow enforce on`. Blocked hosts show live in `fy tui`, where you can
  grant them without a restart.

  There is **no `allow` list here**. Grants of every level live on your computer in
  `~/.foldyard/<project>/allow-store.json`, outside the repo: `fy allow add <host> [--level
  once|session|permanent]`, `fy allow list`, or `a` on a blocked row in the TUI. Hosts the
  proxy injects credentials for are allowed automatically. **A grant means `host:443`** — another
  port is its own grant (`fy allow add github.com:22`), because CONNECT relays whatever the
  client speaks.
- **`recommend`** — hosts this repo *asks* you to grant, each `{ host = "…", why = "…" }` or a
  bare host string. The proxy never reads it. `fy up`, `fy box up`, `fy host restart`,
  `fy allow sync` and the TUI's Network Log offer each pending host, and you answer yes
  (permanent) / session / once (15 minutes) / not now / never; the answer is stored on your
  computer. The offer reads the *adopted* config, and a "never" is remembered.
  `when = "build"` marks a host only an image build needs: it's offered when a build is refused,
  not at launch, and granted for builds only. Default: `[]`.

  ```toml
  [proxy]
  recommend = [
    { host = "pypi.org", why = "uv/pip — box bootstrap + deps" },
    { host = "unpkg.com", why = "vis-timeline, loaded by the generated pages under test" },
  ]
  ```

  `init` seeds it with the box-bootstrap hosts. Declared `[claude]`/`[codex]` tables (and any
  plugin with an `egress_recommend` hook) add the hosts their own installers reach
  (`claude.ai`/`downloads.claude.ai`, `chatgpt.com`/`releases.openai.com`) — don't list those.
  `fy allow sync --yes` grants every pending host permanently for unattended setup; without it,
  a run with no terminal grants nothing. `fy config widenings` shows each entry's answer.
- **`passthrough`** — trusted hosts the proxy tunnels *without* decrypting; everything else is
  decrypted and logged, always ([ADR-0029](./adrs/0029-the-proxy-always-decrypts.md)). Entries
  are exact hosts, `*.suffix` globs, or `@bundle` names (`@all` = every built-in toolchain
  bundle). Default: `["@all"]`; `[]` decrypts everything. Also the escape hatch for a host that
  breaks under decryption (pinned certificate, client certificate, own trust roots). Like every
  host-side key it takes effect when adopted. `fy config widenings` shows how many hosts your
  list resolves to and flags a mistyped `@bundle` (which expands to nothing).
- **`no_proxy`** — your stack's own hostnames the box must reach **directly**. The proxy runs on
  your computer and can't resolve them, so a proxied call hangs, then fails with 502 — usually
  seen as a mysteriously slow test against an emulator. Default: `[]`.

  ```toml
  [proxy]
  no_proxy = ["{project}-postgres", "redis", "fake-gcs"]
  ```

  `{project}` expands to the compose project name, so one entry covers every worktree.
  `localhost`, `127.0.0.1` and foldyard's own services are always included. Listing a service
  that isn't running is harmless. **Entries may not contain a dot or a `*`** (`fy up` refuses
  them): a bypassed host skips the proxy entirely — no logging, no allowlist — and single-label
  names can never be public hosts. The one dotted form allowed is a name under `.localhost`
  (`supabase.localhost`), which resolves to loopback — useful for one URL that must work from
  your browser and from a container (give the gateway service that alias, published on the port
  it listens on). To reach an external host, grant it: `fy allow add <host>`.

## `[[inject]]`

A credential injector from config alone. Each entry becomes one on/off switch
(`fy mode <switch>=on`) and one proxy rule that adds a token to requests for one host. The token
lives only in `host.env` on your computer; turning the switch on prompts for it if missing.

```toml
[[inject]]
switch = "tracker"                 # → `fy mode tracker=on`; token = host.env's FY_INJECT_TRACKER
host = "api.tracker.example"       # the host to inject on
header = "Authorization"           # XOR query_param = "userToken"
```

- **`switch`** — the switch name (required). It also names the token: `host.env`'s
  **`FY_INJECT_<SWITCH>`** (uppercased, non-alphanumerics become `_`). You can't choose another
  variable, so a repo edit can't point a rule at another mechanism's secret.
- **`host`** — the host to inject on (required).
- **`header`** XOR **`query_param`** — where the token goes. Neither: the `Authorization` header.
- **`value_prefix`** — prefix for the injected value, e.g. `"Bearer "`.
- **`path_prefix`** — only inject on paths under this prefix.
- **`replay_on_401`** — re-read the token and replay once on a 401. Default: `false`.
- **`ttl`** — how often (seconds) the token is re-read. Not the switch's lifetime — that's
  `emergency`.
- **`emergency`** — `true` makes `on` an emergency level: it expires (`fy mode <switch>=on
  ttl=30m`; default 1 h, max 8 h) and switches itself off. For tokens you never want left on
  (write access, a shared account).
- **`label`** — shown in daemon status.

There is no `minter` key: a token service that needs more than a static token is a kind built
into foldyard (`github-app`, `gh-cli`, the Codex refresh) or an installed plugin, never a command
from config ([ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md)). Any number of
injectors can be on at once.

## `[[secret]]`

Secrets a mode needs present in `host.env` before its token service can work. foldyard checks
presence, shows a `fy doctor` row, and prompts once for a paste; where the value comes from is
up to you.

The prompt comes when you change mode: `fy mode sanity=on` asks on your terminal *before*
switching, so Ctrl-C leaves the mode unchanged; a mode button in `fy tui` shows the same prompt
(an empty paste arms now, paste later; esc cancels). `fy box up` re-checks as a backstop. With no
terminal foldyard warns and carries on — a missing secret breaks that one host, nothing else.

```toml
[[secret]]
var     = "GH_PEM_B64"                       # the host.env key (required)
label   = "GitHub App private key (PEM)"     # shown at the prompt
how     = "gcloud secrets versions access <resource> --impersonate-service-account=<sa> | base64"
pattern = "*-----BEGIN *PRIVATE KEY-----?*-----END *PRIVATE KEY-----*"   # glob the value must match
base64  = true                               # store encoded — host.env is single-line
when    = { github = "app" }                 # mode gate, same semantics as `[[overlay]]`
```

- **`var`** — the `host.env` key. Required; missing is an error at startup.
- **`label`** — the name at the prompt. Default: `var`.
- **`how`** — "where do I get this?" — **printed, never run** (a gcloud command, an `op read`, a
  URL, "ask Ops").
- **`pattern`** — a glob (`*`, `?`, `[seq]`) the value must match, checked after decoding when
  `base64`. A non-matching paste stores nothing. A glob, not a regex, so a crafted pattern from
  repo config can't hang `fy box up`.
- **`base64`** — store the value base64-encoded, the only way `host.env` (single-line
  `KEY=VALUE`) can hold a multi-line secret such as a PEM; a truncated paste then fails loudly.
- **`when`** — mode gate: `switch = level` or `switch = [levels]`, AND across keys. When
  overriding a plugin's secret, repeat its gate.

Plugins declare their own secrets (e.g. `github=app`'s PEM). A `[[secret]]` with the same `var`
overrides only the fields it names, so retargeting the hint at your own vault is one `how = …`
line.

## `[[overlay]]`

Compose files added to the `-f` chain while the mode matches.

```toml
[[overlay]]
file = "compose.identity.yml"       # required — checkout-relative or absolute
when = { gcp = "sa" }               # optional — AND across keys, OR within a list
env  = "MY_IDENTITY_COMPOSE"        # optional — path-override env var (test/CI hatch)
```

- **`file`** — the compose file to add. Silently skipped if it doesn't exist.
- **`when`** — `switch = level` or `switch = [levels]`; matches when every named switch is at
  one of its levels. Missing or empty always matches.
- **`env`** — if this variable is set, its value replaces `file`.

Declaration order is `-f` order: later files override earlier ones. Details and a worked example:
[compose-overlays.md](./compose-overlays.md).

## `[[require]]`

Rules that one switch needs another, from config. The same check as a plugin's
`Switch.requires`: while `switch` is at a level in `when`, `needs` must be at a level in
`accepts`, otherwise `fy mode` refuses (`severity = "error"`) or warns and applies (`"warn"`).

```toml
[[require]]
switch = "llm"               # required — the owning switch (whose levels activate the rule)
when = ["record", "live"]    # required — owning-switch levels; scalar or list
needs = "gcp"                # required — the required switch, by name
accepts = ["sa", "user"]     # levels of `needs` that satisfy it; FIRST is the suggested fix
reason = "the runtime-SA identity"   # optional — human name for what's needed
severity = "error"           # optional — "error" (default) | "warn"
message = ""                 # optional — full custom message, overrides the generated one
```

Use it when the coupling comes from *your wiring* — `llm=record/live` needs gcp only because
your `[[overlay]]` sends LLM traffic through Vertex; another project would need something else,
perhaps an `[[inject]]` switch no plugin could name. Couplings intrinsic to a switch ship in its
plugin's `Switch.requires`.

An absent `needs` switch satisfies nothing; an owning switch missing from a mode reads as its
default. Config rows are checked after the plugin's own. Mistakes fail every command: an unknown
owning `switch`, a `when` level it doesn't have, or a bad `severity`.

## `[box]`

The dev box. With no `image`, foldyard uses its packaged generic box (engine client + git + uv),
so a stack-less project can `fy box up` with no Dockerfile.

```toml
[box]
image = { dockerfile = "box.Dockerfile", tag = "acme-box:latest" }
shadow_volumes = ["web/node_modules"]
caches = [{ volume = "shared-pnpm", path = ".local/share/pnpm" }]
warmup = [{ dir = "web", run = "pnpm install --frozen-lockfile" }]
```

- **`image`** — your toolchain image: `{ dockerfile, tag?, context?, target?, build_args? }`.
  `dockerfile` is repo-root-relative; `context` defaults to the repo root; `tag` to
  `<prefix>-devbox:latest`; `target` picks a multi-stage stage. The image needs an engine client
  and git; foldyard adds its own pieces (socket, CLI, proxy CA) at box-up.
- **`sock_in_vm`** — the rootless podman socket inside the VM, mounted as the box's
  `/var/run/docker.sock`. Default: the backend's own. Env: `PODMAN_SOCK_IN_VM`. Rarely needed.
- **`shadow_volumes`** — in-tree dirs (checkout-relative) covered by per-box named volumes. Works
  around bind-mount uid squashing, runs installs at VM-disk speed, and keeps what the box
  installs (compromised or merely different) off your checkout, where your own tools would load
  it (see [isolation-layers.md](https://github.com/twistco/foldyard/blob/main/docs/isolation-layers.md)).
  Default: `[]`.
- **`caches`** — shared cache volumes: `[{ volume, path }]`, `path` relative to the box's `HOME`.
  Default: `[]`.
- **`warmup`** — background steps after box-up: `[{ dir, run }]`, each run in `<checkout>/<dir>`.
  Default: `[]`.
- **`env`** — extra static env in the box; `~` expands to the box's `HOME`. Default: `{}`.
- **`[[box.tools]]`** — one-time tool installs, run as bootstrap steps reported ✓/✗:
  `{ name, install, check? }`. `check` is a shell test that skips the install when it succeeds;
  default `command -v <name>`.

  ```toml
  [[box.tools]]
  name = "pulumi"
  install = "curl -fsSL https://get.pulumi.com | sh -s -- --install-root /opt/fy-tools --no-edit-path"
  ```

  The packaged box image ships no apt package lists, so a bare `apt-get install` fails with
  `Unable to locate package`. Add the lists as the first step:

  ```toml
  [[box.tools]]
  name = "apt-lists"
  check = "ls /var/lib/apt/lists/*Packages >/dev/null 2>&1"
  install = "apt-get update -qq"
  ```

- **`bootstrap`** — free-form shell run once per fresh box, after the steps above. Default: `""`.
- **`clean_docker_config`** — point `DOCKER_CONFIG` at a clean foldyard-owned dir instead of
  `~/.docker`, whose editor-injected credential helper fails as root and breaks even anonymous
  pulls. `docker login` still works. Default: `true`.
- **`git_index_split`** — box-side git uses its own `.git/index-box`, so it doesn't race git on
  your computer over the shared index
  ([ADR-0021](./adrs/0021-per-kernel-git-index-split.md)). Default: `true`.

## `[claude]` / `[codex]` / `[vscode]`

All three are **presence-gated**: declare the table (even empty) to opt in.

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

- **`[claude]`** — installs Claude Code at box-up and mounts its config/transcript volumes.
  - **`keyless`** — keep the real credential out of the box: the box holds a dummy, and the
    proxy swaps in the real one from `host.env`. `"oauth"` injects a `claude setup-token` token
    as the bearer; `"api-key"` (or `true`) injects `x-api-key` on `api.anthropic.com`. Default:
    off — a normal login, real credentials in the box's `~/.claude`, kept across recreation and
    `fy nuke`. Without `keyless` there's no switch, and the login and API hosts join the
    `recommend` offer instead.
  - **`system_prompt`** — text `fy claude` passes via `--append-system-prompt`. `init` seeds a
    generic one. Default: `""`.
  - **`[claude.settings]`** — a settings table `fy claude` passes as JSON to `--settings`; Claude
    merges it over the box's `~/.claude/settings.json` per key. Any `settings.json` key works
    (`model`, `env`, `permissions`, `statusLine`, …). Default: `{}`. `fy claude --settings …`
    wins.
- **`[codex]`** — installs the OpenAI Codex CLI at box-up (native installer, no node) and
  mounts its `~/.codex`.
  - **`keyless`** — `"api-key"` (or `true`) injects the real `OPENAI_API_KEY` as a bearer on
    `api.openai.com`; `"chatgpt"` uses your ChatGPT subscription: the box holds a dummy
    `auth.json` and the proxy injects a current access token, refreshed on your computer from
    your real one. Default: off (normal login). In the box that means `codex login --api-key`:
    the ChatGPT browser login redirects to a loopback port in the box your browser can't reach,
    which `"chatgpt"` solves.
  - **`system_prompt`** — same job as `[claude]`'s; `fy codex` passes it as
    `-c developer_instructions="…"`, which adds to (doesn't replace) Codex's own prompt.
    Default: `""`.
  - **`[codex.config]`** — overrides in the shape of Codex's `~/.codex/config.toml`. Each leaf
    becomes one `-c key=value`, nested tables flattened to dotted paths, so one knob is
    overridden without replacing its table. Default: `{}`. Later `-c` wins: `[codex.config]`
    beats `system_prompt` on `developer_instructions`, and `fy codex -c …` beats both.
- **`transcript_sync_seconds`** (both tables) — while the box is up, the supervisor copies that
  agent's transcripts into their archive on your computer (`~/.claude/projects` /
  `~/.codex/sessions`) at this interval, so a crashed box or a `git clean -fdx` loses nothing.
  Default: `0` (off — transcripts are archived at `fy box down`, or by hand with
  `fy transcripts`). Rounded down to a multiple of the 2-second supervisor tick (minimum one
  tick). A failing sync logs once to `host-supervisor.log` when it breaks and once when it
  heals, and notifies only if it stays broken. Read from the adopted config.

  ```toml
  [claude]
  transcript_sync_seconds = 30
  ```

Both tables deep-merge, so `foldyard.local.toml` can set one key (your model) without
redeclaring the team's. `fy config widenings` lists their keys under **agent steering**.

- **`[vscode]`** — mounts the vscode-server volume so `fy code` reuses its server across box
  recreations, and has `fy code` write the attached-container config (extensions + settings)
  from these keys. foldyard writes that file itself; nothing from the repo (such as
  `.vscode/extensions.json`) feeds it. The table is read from the *adopted* config, and `fy code`
  runs the adopt gate first, because extensions can install into your own `~/.vscode/extensions`
  ([ADR-0026](./adrs/0026-vscode-attach-config-is-declarative.md)). `fy code` also starts VS Code
  with an empty SSH agent and the git credential bridge off; what a manual attach still brings is
  removed in the box, and `fy verify` reports what's left
  ([security](./security.md#fy-verify-prove-it-dont-trust-it)).
  - **`extensions`** — marketplace ids (`publisher.name`) installed on attach, without a click.
    The Dev Containers extension and invalid ids are dropped. Default: `[]`.
  - **`settings`** — VS Code settings applied to the box's server (Remote [Machine] scope), e.g.
    `"remote.autoForwardPorts" = false`. Settings can't run code. foldyard adds its own pin (the
    proxy and token-service ports are never auto-forwarded). Values must be JSON-compatible (no
    TOML dates). `fy code` re-applies them on the next attach after a change. Default: `{}`.

  The settings sit above the instance's own user settings and below the checkout's gitignored
  `.vscode/settings.json`, so the table is team policy and that file stays yours. They reach the
  `fy code` window only. Personal tweaks go in `foldyard.local.toml`. Deleting the
  `_generatedBy` key from the written config file stops `fy code` writing it.

  Each worktree gets its own VS Code instance. The first `fy code` for a worktree copies your own
  VS Code's user settings, keybindings and snippets into it, leaving out settings that point at
  another container engine (Docker Desktop's socket or context). After that the instance's
  settings are its own; delete its `User/settings.json` to copy yours again.

  ```toml
  [vscode]
  extensions = ["anthropic.claude-code", "charliermarsh.ruff", "biomejs.biome"]

  [vscode.settings]
  "remote.autoForwardPorts" = false
  "github.gitAuthentication" = false
  ```

## `[reclaim]`

Your project's own disk cleanup. foldyard decides *when* — before a `fy up` build when the engine
store is under 20% free (or under 5 GiB), the same threshold `fy doctor` warns at, or on demand
with `fy reclaim` — and first runs its own sweeps: dangling images over a day old, images of
removed worktrees, and images its build just replaced. Then it runs your script **in the box**,
with the checkout as working directory.

```toml
[reclaim]
script = "dev-stack/reclaim.sh"   # checkout-relative; e.g. pnpm store prune, uv cache prune,
                                  # trimming test artefacts, a stale buildx state volume
```

- **`script`** — checkout-relative path. Default: none.

Best-effort: output streams to the terminal, a non-zero exit isn't a failure, and a stopped box
is a note (run `fy reclaim` in the box to reach its volumes). Per-worktree caches are reachable
only from that worktree's box.

## `[plugins.<name>]`

Plugin opt-ins: declaring the table, even empty, loads the plugin. Without it you never see its
switches, daemons or box wiring.

```toml
[plugins.gcp-metadata]
project = "my-gcp-project"
sa_labels = { app = "app-runtime", box = "log-reader" }
```

- **`[plugins.gcp-metadata]`** — the GCP metadata emulator and its token service.
  - **`project`** — the GCP project whose service accounts it impersonates; the gcp levels need
    it. Env: `GCP_PROJECT`.
  - **`sa_labels`** — service-account names by role, e.g.
    `{ app = "app-runtime", box = "log-reader" }`. Default: `{}`.
- **`[plugins.github]`** — enables the `github` switch (`off`/`app`/`user`) and everything with
  it (gh doctor rows, the box's dummy `GH_TOKEN`, the gh bootstrap). An empty table is enough for
  `github=user`. The fields are identifiers, not secrets; the App's private key is a
  `[[secret]]`.
  - **`app_id`** / **`installation_id`** / **`repo`** — the App id, its installation id, and the
    bare repo name (not `owner/repo`) the `github=app` token is scoped to. Env: `GH_APP_ID`,
    `GH_INSTALLATION_ID`, `GH_REPO`.
  - **`permissions`** — narrows the installation token, e.g. `{ issues = "read" }`. Default:
    `{ pull_requests = "write", issues = "write" }`. Env: `GH_APP_PERMISSIONS` (JSON).
- **`[plugins.fakecred]`** — a zero-secret testing switch pair and fake token service, for
  exercising modes, TTLs and daemons without real credentials; see
  [testing-modes.md](./testing-modes.md). Env: `FAKECRED_PORT` (the fake service's port).
- **`[plugins.auth0-sim]`** — an Auth0-simulator harness (project-specific; slated for removal
  from foldyard, [ADR-0024](./adrs/0024-declarative-consumer-axes-no-repo-path-plugins.md)).
  - **`sim_dir`** — repo-relative harness dir; its `.certs-local/` holds the localhost cert the
    doctor checks read. Absent disables those checks. Env: `FOLDYARD_AUTH0_SIM_DIR`.
  - **`container`** — the simulator's compose-service suffix (full name `<prefix>-<suffix>`),
    restarted when a fresh cert is served. Default: `"auth0-sim"`. Env:
    `FOLDYARD_AUTH0_SIM_CONTAINER`.
- **`[plugins.llm]`** — enables the `llm` switch (project-specific, slated for removal likewise).
  No keys.

## Supervisor settings

Two more top-level tables tune the supervisor, foldyard's background process on your computer:

- **`[host] notifications`** — post desktop notifications when a credential stops or starts
  working again (macOS only today). Default: `true`.
- **`[resnapshot_on_capability]`** — `switch = ["service", …]`: compose services the supervisor
  restarts when that switch's credential check goes from failing to working. For a service that
  fetches credentials once at startup and would otherwise keep the broken state. Default: `{}`.

```toml
[resnapshot_on_capability]
gcp = ["api"]
```

## The adopted config: what the host actually runs

`foldyard.toml` and its local overlay sit **inside the repo**, so anything that can write the
checkout can change them: an agent in the box, an `npm install` script, a branch you checked out
to review. Parts of it decide what your computer does with credentials — which host an
`[[inject]]` switch sends its token to, which traffic `passthrough` leaves undecrypted. So your
computer doesn't read the working tree. It reads a copy you **adopted**, kept in
`~/.foldyard/adopted/<checkout>-<hash>/`, keyed by the checkout's path so the config can't
redirect the lookup ([ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)).

Day to day:

- The first `fy up` / `fy host restart` in a fresh checkout adopts it, and says so.
- After that, an edit does nothing on your computer until adopted. The supervisor logs the change
  once and notifies you, and `fy doctor` shows a warning row.
- `fy up`, `fy box up`, `fy host restart` and `fy code` ask before starting anything:

  ```
  ⚠ foldyard.toml changed since the host adopted it [main]
    --- adopted/foldyard.toml
    +++ tree/foldyard.toml
    @@ …
    [a]dopt · [r]evert the file · [i]gnore for now (default):
  ```

  **adopt** runs the new config (within a tick, no restart). **revert** puts the file back to
  the adopted copy (a file the copy doesn't have is moved aside into the adopted dir, not
  deleted). **ignore** keeps the adopted copy and asks again next time. With no terminal (CI, a
  detached launch) nothing is adopted.
- The same as commands: `fy config status`, `fy config diff`, `fy config adopt`,
  `fy config revert`. They run on your computer only — the box can't adopt its own config.
- `fy config widenings` lists what the adopted config lets through: how many hosts
  `passthrough` leaves undecrypted (`@all` is about 200), where each switch delivers its
  credential (including ones not switched on), which agent prompts are shared vs personal, and
  keys that are renamed or no longer honoured. A `fy doctor` row summarises it.
- Each worktree has its own adopted copy, since a branch may declare different plugins.
  Switching branches counts as a change and gets asked about.

**Scope:** adoption governs what your computer does — daemons, injection, `passthrough`, and the
switches `fy mode`/the TUI show. Compose files, `[box]` tools and bootstrap, and ports still read
the working tree: they only affect the VM, which untrusted code can already reach.

## Host state: `~/.foldyard/`

All state on your computer lives under `~/.foldyard/`, outside the repo, so nothing in the VM or
box can read it or raise its own access.

Shared by all projects:

- **`ports.json`** — the port-range registry. Each project gets 200 ports (first come, from
  41000): proxy listeners at `base+0..89` (one per worktree offset), token services at
  `base+100..189`, the VM's ssh forward at `base+190`. Stable across restarts; entries for
  deleted projects are harmless and can be removed by hand. Env: `FY_PORTS_FILE`;
  `FY_PROXY_PORT` / `GCP_MINTER_PORT` bypass allocation.
- **`adopted/<checkout>-<hash>/`** — the adopted config per checkout (see above).

Per project, `~/.foldyard/<project>/` (env `FOLDYARD_STATE_DIR` relocates it):

- **`host.env`** — secrets and identities the token services read (App ids, captured keyless
  tokens, injector tokens). One per project, shared by every worktree.
- **`allow-store.json`** — the egress grants, at every level. Outside the repo, so the box
  can't grant itself access.
- **`allow-effective.json`** — the resolved allowlist the proxy re-reads per request (the
  store minus expired grants).
- **`build-tokens.json`** — hashed secrets for live image builds, which unlock build-only
  grants at the proxy.
- **`host-supervisor.log`** — the supervisor's combined log, including every daemon it runs.
- **`host-supervisor.heartbeat`** — refreshed each supervisor tick; launch verbs read its age to
  tell a healthy supervisor from a stuck one.
- **`capabilities.json`** — the latest credential-check results `fy mode`/`fy state` show.
- **`blocked-daemons.json`** — daemons the supervisor is holding back, with the reason.
- **`clock-offset`** — test-only clock skew set by `fy clock`; absent normally.
- **`main/`** — the main checkout's `dev-mode.json` (its mode) and `logs/` (per-daemon JSONL
  logs: proxy, token services).
- **`worktrees/<name>/`** — the same for each worktree, so each branch holds its own mode.

(An older install may still have a top-level `dev-mode.json`; the main checkout keeps using it
until `main/dev-mode.json` exists.)

In the repo, foldyard also generates gitignored files next to `foldyard.toml` (`init` adds them
to `.gitignore`): `.devbox-ca/`, `.devbox-claude/`, `.devbox-codex/`, `.devbox-foldyard/`, and
`.dev-mode.json` — a read-only copy of the mode for tools in the box. Nothing grants access based
on it.

## Environment variables

Per-key overrides are listed with their keys above. The globals:

| Variable | What it does |
| --- | --- |
| `FOLDYARD_REPO` | The repo root, skipping the walk up from the working directory. |
| `FOLDYARD_PROJECT` | The project name (state-dir key). |
| `FOLDYARD_ENGINE` | The engine CLI (`podman`/`docker`). |
| `WORKTREE` | The active worktree; empty = the main checkout. Set in the box; inferred from the working directory on your computer. |
| `WT_OFFSET` | Worktree port offset, bypassing pins and the hash. |
| `PODMAN_MACHINE` | The VM name. |
| `MACHINE_BACKEND` / `MACHINE_VMTYPE` / `MACHINE_FIREWALL` / `MACHINE_HOST_FIREWALL` / `MACHINE_RUNTIME` / `MACHINE_CPUS` / `MACHINE_MEMORY` / `MACHINE_DISK` | `[machine]` overrides. |
| `FY_PROXY_PORT` | The proxy's base port, bypassing the port-range registry. |
| `GCP_MINTER_PORT` | The gcp token service's base port, likewise. |
| `FY_HOST_ALIAS` | The address containers use to reach daemons on your computer — for a customised Lima network with a different host gateway. |
| `FOLDYARD_COMPOSE_EXTRA` | Extra compose files (path-separator-joined), appended last so they win. |
| `FOLDYARD_NO_VERSION_NUDGE` | Silences the `recommended_foldyard_version` nudge. Never affects the floor or `fy doctor`. |
| `FOLDYARD_PODMAN_DESKTOP` | `0`/`1`: Podman Desktop integration off/forced on (see `[machine]`). |
| `IN_DEVBOX` | `1` inside the box; what foldyard's "am I on your computer?" checks use. Set by foldyard — don't set it yourself. |

## Internal / advanced

Test and CI hatches with no `foldyard.toml` keys; you shouldn't normally need them.

- **State-path overrides** — `FOLDYARD_STATE_DIR`, `FOLDYARD_MODE_FILE`, `FOLDYARD_HOST_ENV`,
  `FOLDYARD_ALLOW_STORE`, `FOLDYARD_ALLOW_FILE`, `FOLDYARD_BUILD_TOKENS`, `FOLDYARD_LOG_DIR`,
  `FOLDYARD_SUPERVISOR_LOG`, `FOLDYARD_HEARTBEAT_FILE`, `FOLDYARD_CAPABILITIES_FILE`,
  `FOLDYARD_BLOCKED_DAEMONS_FILE` — each repoints one of the state files above.
- **`FOLDYARD_CLOCK_OFFSET`** — clock skew in seconds, overriding `fy clock`'s file.
- **`FOLDYARD_LOG_TAIL_BYTES`** — how much of each JSONL log's end the TUI reads (default
  256 KiB).
- **`FOLDYARD_CHECKOUT`** / **`FOLDYARD_ENV_OVERRIDE`** — set *by* foldyard for the stack: the
  checkout it mounts (main or a worktree) and the per-checkout env-override file.
