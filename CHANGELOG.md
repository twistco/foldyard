# Changelog

Notable changes, per release. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/) once past `1.0`. Before that, minor versions may
break config or CLI shape, and say so here. How a release is cut:
[docs/releasing.md](./docs/releasing.md).

## Unreleased

### Changed

- **Clearer names for the firewall and allowlist settings.** "Wall" meant three different things:
  the firewall inside the VM, the one on your computer, and the proxy's allowlist switch. Each now
  has its own name:

  | was | now |
  | --- | --- |
  | `[machine] wall` | `[machine] firewall` |
  | `[machine] host_wall` | `[machine] host_firewall` |
  | `fy machine host-wall` | `fy machine host-firewall` |
  | `[proxy] default_deny` | `[proxy] enforce` |
  | `fy allow wall on\|off\|learn` | `fy allow enforce on\|off\|learn` |
  | `axis =` in `[[inject]]` / `[[require]]` | `switch =` |
  | `MACHINE_WALL` / `MACHINE_HOST_WALL` | `MACHINE_FIREWALL` / `MACHINE_HOST_FIREWALL` |

  **Migrating:** nothing breaks. The old names keep working as aliases, and `fy doctor` /
  `fy config widenings` list any still in use. Rename them in the same commit that raises
  `[project] min_foldyard_version` to this release: an older `fy` doesn't know the new names, so it
  would read `firewall = true` as no firewall at all.

- **`fy host` is the supervisor's status, not a foreground run.** The supervisor was already
  started detached by `fy up` / `fy box up` almost every time, so the foreground terminal it was
  documented as living in was one nobody had open — and nothing said whether it was running.
  Now: `fy host` (or `fy host status`) shows this project's supervisor — running or not, pid,
  uptime, whether it runs the installed code, its heartbeat, the current worktree's daemons and
  the last index heal — and exits non-zero when it needs attention. `fy host restart` replaces it
  (or starts it) and waits until the new one is ticking. `fy host logs [-n N] [-f]` tails its log.
  There is deliberately no `stop`: a VM without its supervisor is a box whose egress is refused,
  so it stops with the VM (`fy machine stop`). **Migrating:** `fy host` no longer starts
  anything — use `fy host restart`; `fy host --restart` is gone the same way. Every "run `fy
  host`" message now names `fy host restart` or `fy host`.

### Fixed

- **`git worktree add` in the box no longer corrupts both checkouts' indexes.** The box's git
  shim let the new worktree's checkout write into the calling checkout's `index-box`: phantom
  `MM` pairs there (a commit would revert the difference; a later `git switch` carried the files
  over as local edits, silently), no index at all in the new worktree. `git worktree` now runs
  unredirected, like `clone`/`init`.

## 0.3.1 — 2026-09-18

### Changed

- **`[machine].host_wall` is the operator's install — foldyard never runs `sudo` on the host
  ([ADR-0028](./docs/adrs/0028-no-elevation-on-the-host-operator-applies.md)).** 0.3.0 loaded the
  host table with `sudo nft -f -` on every `fy up`: a password prompt with nothing to read, the
  ruleset over stdin where the operator could not see it, and the habit it teaches — approving
  whatever asks. Now `fy machine host-wall` renders the nftables table and a small system unit
  that loads it with your user manager into `~/.foldyard/<project>/host-wall/`, prints both in
  full, and prints the four `sudo` lines that install them (two `install`, a `daemon-reload`, an
  `enable --now`; `--uninstall` prints the reverse). You run those, once per host, with the
  content in front of you; the installed copies are root-owned. Every `fy up` — and the verb, and
  doctor's new `host wall` row — then *probes* enforcement from inside the VM's cgroup (a child
  under it must be refused a loopback listener foldyard opened outside, must be refused an
  off-host address, must reach the project's band) and refuses with the failing half named when
  it is not. A launch verb on a host where nothing is set up refuses before booting anything and
  names the verb. The install is not part of the VM's lifecycle: `fy machine stop` and `rm`
  leave it (`rm` says so). **Migrating from 0.3.0:** run `fy machine host-wall` once and the
  four lines it prints; a passwordless sudoers rule for `nft` is no longer needed and can go.
- **The host wall's table is the same text on every boot, bound to a persistent slice.** It
  used to name Lima's forwarded SSH port and the hostagent's loopback ports (allocated per boot)
  and the host's resolvers, which is why it had to be re-rendered and re-loaded every `fy up`.
  Loopback flows are now allowed out under a per-project conntrack mark and judged on the input
  hook by the *listening* socket's cgroup — the VM's own plumbing passes whatever ports Lima
  picked, the project's band passes, the operator's other local services are refused — and DNS
  is port 53 to any resolver. The VM runs in its scope under a per-VM slice
  (`fy-machine-<vm>.slice`, a unit under `~/.local/share/systemd/user/` that only the verb
  writes, saying so once) and the table matches the slice: nftables binds a cgroup *id* at load,
  a scope dies with its last process, a slice survives being emptied. A host reboot is a new id,
  which is what the probe above exists to catch — a loaded table that matches nothing is
  fail-open, and shown to be so on a runner (2026-09-18). A VM already running in its scope but
  not under its slice (an older foldyard started it) is refused until `fy machine stop && fy up`.

### Fixed

- **`fy machine ensure` no longer fails on a Lima hostagent still leaving after its driver died.**
  Lima's hostagent notices a dead QEMU, flips the instance to Stopped and exits — but not
  atomically, and `limactl start` inside that window refuses with "host agent is running but
  driver is not". The Linux runner never fell into it; the WSL2 runner (3.5× slower) did. The
  lima backend now waits for a hostagent whose driver is dead to leave on its own (announced, up
  to 20 s) and signals it only if it lingers — identified by Lima's own `ha.pid`, never by name;
  a VM whose driver is alive is never touched. Both of `ensure`'s paths are covered.
- **The upgrade the version floor asked for no longer breaks the install it runs on.** It said
  `uv tool install --upgrade foldyard`, which re-specifies the requirement as bare `foldyard`:
  the `[host]` extra is dropped — mitmproxy uninstalled, so the next `fy box up` fails preflight
  and the box's egress would connection-refuse — and an editable checkout is swapped for PyPI's.
  Seen on the first 0.3.0 upgrade. It now says `uv tool upgrade foldyard`, which re-resolves the
  install as it was made (extras and editable path kept, an editable install's baked version
  refreshed). And the mitmproxy-missing preflight / doctor row / PyJWT error named `just foldyard
  install` — foldyard's own dev recipe, in a spelling that no longer exists even here; they now
  print the `uv tool install --force … [host]` reinstall shaped to your install, the same
  command the TUI's fix button runs. The safe sequence is written down:
  [quickstart → Upgrading](./docs/quickstart.md#upgrading) (`fy docs quickstart`).

## 0.3.0 — 2026-09-18

### Removed

- **`[machine].backend = "native"` — the VM-less backend.** foldyard always has a VM
  ([ADR-0027](./docs/adrs/0027-always-a-vm-native-backend-retired.md)): its CI rationale is
  gone (the host tier runs on a real Lima/QEMU VM on runners), WSL2 does not rescue it (Hyper-V
  protects Windows, not the credentials), and "weaker profile" meant every product claim at once —
  no repo-only socket, no wall, no gVisor posture, `verify` unable to assert the boundary. A config
  still naming it gets a loud warning naming the ADR and the `podman` backend (a VM, so the
  fall-back adds the boundary rather than dropping it); `machine ensure|stop|rm|recreate` and
  preflight lose their VM-less branches, and the fail-closed "no `limactl`" message offers two ways
  out, both VMs.
- **The hardcoded `<dev_vm_dir>/.stubs/{adc.json,access-token}` every `fy up` wrote.** A leftover
  of the origin monorepo's compose file (which mounted the empty `adc.json` as a stand-in for a
  host ADC file the VM can't reach), undocumented and ungated, so every consumer — GCP or not —
  grew an unexplained `.stubs/` in its repo root (the `dev_vm_dir` default) that the managed
  .gitignore block deliberately did not cover. `fy up` now writes only what `[project].ensure_dirs`
  declares. A compose file that still mounts the old path gets a directory, not an empty file —
  drop the mount: an app that wants "no GCP identity" says so with
  `METADATA_SERVER_DETECTION=none` (Node google-auth) / an empty `GCE_METADATA_HOST` (Python),
  not with an empty credential file. The hidden `foldyard stubs` verb and the `ensure_stubs`
  shell shim `shellenv` emitted for it go with it (`dev_vm_banner` / `foldyard banner` stay).

### Fixed

- **Every engine probe read "engine unreachable" on a stock Ubuntu 24.04 host.** podman 4.9.3's
  `ps --format` (the LTS package) has no `{{.Label "k"}}` — a 5.x template function — so the
  workspace cards, the reconciler's stack tier and `fy state` all failed the template while
  `fy ps` reached the VM. `devmode.ps_labels` now reads `{{json .Labels}}` (a map from podman, a
  `k=v` string from docker). Caught by the first run of the VM-backed host tier in CI.
- **`[vscode.settings]` changes now reach a box whose server has already been attached to.**
  `fy code` reset Dev Containers' `.writeMachineSettingsMarker` on every settings change, and it
  wasn't enough: the extension (0.469, its own source) writes the box's Machine `settings.json`
  only when that file does NOT exist — marker or no marker — so on a consumer the rendered file
  sat two months stale while the marker was dutifully reset on every `fy code`. The rendered
  file goes with the marker now; the extension regenerates it from the config (plus its own
  Copilot/ports additions). And a reset marker is only READ when the box's server goes through
  set-up — an attach that finds the server still running from the last session reconnects and
  skips it (seven extensions "will install on attach", three attaches, zero installed) — so
  `fy code` restarts the box's server whenever it resets a marker; a window still attached
  reconnects to the new one. Hand edits in the "Remote [Attached Container]" settings tab are the
  one thing that resets — they belong in `[vscode.settings]`.
- **A `[vscode]` table offers the marketplace hosts at the launch verbs.** The server IN the box
  installs `[vscode] extensions` itself, and under an enforcing wall the gallery query
  (`marketplace.visualstudio.com`) was refused — the install failed silently, every extension
  "not found", the extensions dir empty. The `vscode` plugin now recommends the gallery, the
  VSIX hosts (`*.gallery.vsassets.io`, `*.gallerycdn.vsassets.io`) and the server-build hosts, one consented yes each,
  like the agents' installer hosts. Telemetry and experiment hosts are deliberately not offered.
- **A leftover `[vscode] workspace_file` is reported as IGNORED** (`fy config widenings`, the
  doctor row, the adopt gate's footnote) with the migration spelled out — it was the one
  generator-era key #18 removed without adding to `IGNORED_KEYS`, so a consumer that had
  migrated everything else got silence while the extensions its generator used to collect
  quietly stopped installing.
- **A daemon's own launch no longer reads as a lapse.** The supervisor tick probes capabilities
  before it spawns daemons, so the tick that activated a rung (and the first tick of every
  restarted supervisor — each `fy up`) probed the minter's port before anything had bound it:
  a failure cached for the probe's whole interval, rendered by `fy mode` as `⚠ DEGRADED —
  minter port not answering` beside the same daemon's `● up`, plus a spurious DEGRADED →
  recovered notification pair a minute apart on every launch. An axis whose daemon is not yet
  answerable (about to be spawned, or launched under `DAEMON_WARMUP_SECONDS` ago) now makes no
  claim, and a verdict cached from before the launch is dropped so the axis is probed fresh
  once warm. Neither a child that already died (a crash-loop still reads as one) nor a daemon
  a spawn gate holds back (missing host.env, a foreign listener on its port) is warming —
  the latter keeps the port probe that names a forwarder shadowing the minter.
- **The gcp metadata emulator logs a client hang-up as one line, not a traceback.** A token
  client giving up mid-response (google-auth's own timeout against a slow mint, a container
  stopping) hit socketserver's default `handle_error` — a full traceback per request, which
  read as the emulator being broken when the caller had merely left. Only the hang-up class
  (`BrokenPipeError` / `ConnectionResetError`) is quietened; anything else still traces. The
  file is restaged on the next `fy up`; the running emulator container picks it up on its
  next (re)create.

### Added

- **The host tier runs on a real Lima/QEMU VM in CI** (`lima-host-e2e` in `foldyard-e2e.yml`,
  opt-in like the other live tiers): foldyard's default `lima` backend boots a VM on `ubuntu-24.04`
  runners (x86 only — arm64 runners have no KVM), and eight `tests/test_*_e2e.py` modules over
  `tests/e2e_host.py` drive the real product path — `machine ensure|stop|recreate` (including a
  SIGKILLed hypervisor recovered by `ensure`, and restarts with the repo mounted under the host
  home), the adopt gate, the supervisor with the zero-secret rig, both walls, the box (in-box
  `fy verify` ALL PASS), `verify`'s negative against a VM exposing the host home, the read-only
  engine probes, `fy reclaim` on a real store, and `fy worktree add|remove`. 36/36, ~15 minutes.
- **The same host tier inside WSL2 on a Windows runner** (`wsl2-host-e2e`: `windows-2025` +
  Vampire/setup-wsl, Ubuntu 24.04 under WSL2, foldyard's `lima` backend booting the QEMU/KVM
  VM INSIDE the distro — nested twice, which the hosted runners allow once `.wslconfig` asks for
  `nestedVirtualization`). The product needed no change; the job's plumbing (WSLENV, a root→user
  wrapper switch, the checkout cloned onto ext4) is documented in DEVELOPMENT.md. One finding:
  the stock WSL2 kernel has no `CONFIG_NFT_SOCKET`, so `[machine].host_wall` cannot load there
  (fails closed; the in-VM wall is unaffected) — `test_wall_e2e.py` now probes the kernel for
  nft's `socket` expression in its gate and skips, instead of erroring after re-provisioning the
  VM walled and taking the next module down with it.
- **`host_wall` on a kernel without `CONFIG_NFT_SOCKET` is refused up front, with the reason.**
  preflight reads the kernel config (`/proc/config.gz`, then `/boot/config-<release>`;
  `hostwall.nft_socket_in_kernel`) and refuses `fy up` before it re-provisions the VM walled;
  where no config is readable, the load-time `nft` error is now captured and, when it is ENOENT
  at the `socket cgroupv2` rule, explained (`hostwall.explain_load_failure`) instead of left as
  a bare "No such file or directory". Either way the message says what still applies (the in-VM
  wall) and what would change it (a kernel built with nft_socket).
- **`just census`** — the subprocess report over the hermetic guard (`tests/tools/census.py`):
  every process the suite spawns, binary × test, aggregated across xdist workers; a report of what
  the allowlist still lets through and what the live tiers reach, not a gate.
- **The editor attach's host bridges are neutralised in-box, the egress wall fences CONNECT to
  `:443`, and `fy verify` reports both.** Seen live: a `fy code` attach put a LIVE SSH agent
  socket (one key) and VS Code's git-credential bridge (`GIT_ASKPASS` back to the host's store)
  into a box whose posture read "never push" — Dev Containers forwards them into every terminal
  it opens, no setting stops it, and launching VS Code with `SSH_AUTH_SOCK` stripped still
  forwards — but what the process HOLDS it forwards verbatim; only an unset var makes it hunt for
  the host's agent. So `fy code` now launches VS Code with foldyard's own EMPTY ssh-agent (per
  instance, reused while it answers, refused if it ever holds a key): the SSH side carries no
  credential by construction, since no setting chooses what is forwarded. The git side gets
  `git.terminalAuthentication` / `git.useIntegratedAskPass` pinned off at user and machine scope
  — a default only, because workspace settings win and `.vscode/settings.json` is mount data the
  box can write; `fy verify` FAILS a checkout that flips either back on. So for the git bridge,
  and for a manual attach from the operator's own VS Code, the enforcement is in-box. The one
  defeat that existed was an rc-file unset in a single consumer's box image, which no other
  consumer had, and which leaves the socket reachable by path anyway; now foldyard's bootstrap
  installs `~/.config/foldyard/harden.sh` in every box (image-agnostic),
  sourced at `~/.bashrc` line 1 so every bash inherits the vars' absence, plus a reaper that
  unlinks `/tmp/vscode-ssh-auth-*.sock` and `/tmp/vscode-git-*.sock` as they appear — restarted
  from every shell and by `fy code` before the attach (which refuses to attach if it can't);
  `fy box up` re-applies it to a running box, so no recreate. Independently, a host grant now
  means `host:443` (and `:80` for cleartext): CONNECT is a raw tunnel and a bare `github.com`
  grant reached `github.com:22`, so an agent had somewhere to go; another port is its own grant
  (`fy allow add github.com:22`, offered by the TUI from the blocked row, which carries the
  port), and an injector host's exemption never covers cleartext. `fy verify` gains rows for the
  git-credential bridge vars and for the sockets on disk (a socket is the boundary; an unset var
  is hygiene), and its `SSH_AUTH_SOCK` row names the attach. `fy config widenings` lists
  `[vscode]` as the attach.
- **`fy reclaim`, `[reclaim] script`, and `fy up` removes the images its own build superseded.**
  Measured on a 90 GB store that died mid-build on `no space left on device`: the automatic
  sweep freed `0.0 GiB` under a `✓`, because every dangling image was younger than its 24h
  guard — one rebuild round leaves ~10 GB of freshly untagged images (a 6 GB app image, three
  1.3 GB scraper builds in an hour), all younger than any age guard exactly when the next
  build needs the room. Three changes. `up` now records the id behind every tag it is about to
  rebuild and, once the containers have been recreated onto the new images, removes the old ids
  — provably its own superseded builds, so no guard is needed (never `--force`: an id a
  container still holds is refused and left to the dangling sweep). `[reclaim] script` runs the
  project's own sweep in the dev box after foldyard's engine-level ones — the rest of the store
  is package-manager caches and test artefacts in volumes only the box mounts (that store's
  other 56 GB were volumes). And `fy reclaim` runs all of it on demand, unconditionally, so a
  full store can be dealt with without bouncing the box or the machine; the doctor's low-disk
  row now points at it. A store still low afterwards is said so, with the sweeps deliberately
  left to a human (`system df`, `image prune -a`, `container prune`), instead of ticked.
- **A daemon the supervisor refused to start says why, everywhere.** The three spawn gates
  (a `requires` key missing from `host.env`; a foreign process on the daemon's port; the exec
  failing) used to put their reason in the supervisor log's 30-second nag and nowhere else —
  every posture surface computed daemon status by probing the port itself, so `fy mode` said
  `○ DOWN — run fy host` while `fy host` was running, and a forwarder squatting on the minter's
  port read as `● up`. The supervisor now publishes the gate's own sentence each tick
  (`blocked-daemons.json`, honoured only while its heartbeat is fresh), `devmode.daemon_status`
  carries it as `blocked`, and `fy mode` / `fy state` / the TUI render `○ BLOCKED — <fix>` —
  outranking `● up`, which is the lie a foreign listener tells. One notification per
  newly-blocked daemon. The capability warm-up reads the same verdicts, so a gated daemon's
  axis is still probed (a forwarder there first is the probe's to name) while a launching one
  is not. The gcp port probe's JSON parse moves out of its connect `try`, so a squatter that
  speaks HTTP but not the minter's JSON (or answers a 404) reads as "held by another process" rather than "not
  answering — is `fy host` up?", contradicting the BLOCKED row beside it.
- **`fy verify` points at its own manual.** A failing row names *what* failed and stops there;
  the battery now prints the exact `fy docs security` call up front and again beside a FAIL
  verdict, and that page gains a "When a check fails" section — per row, what it usually
  means and which verb to reach for next.
- **An isolation-layers diagram, in the README and at the top of docs/isolation-layers.md.**
  `docs/assets/foldyard-isolation-layers.svg` draws the hardening ladder as four cumulative
  postures — a rootless Podman VM, Lima with the in-VM wall, gVisor under the dev box behind the
  narrowed engine socket, the host-side wall on Linux — each labelled with the `[machine]` line
  that turns it on, plus the egress dial (open → observe → enforce → fail-closed) and what never
  moves across postures. The page's "three layers" sketch now names ③ as gVisor
  (`[machine].runtime`), not the libkrun microVM it was first measured for.
- **`[machine].runtime = "gvisor"` — the dev box under gVisor's userspace kernel, as a machine
  posture.** `machine ensure` provisions a second podman API service in the VM whose default
  runtime is `runsc` (a pinned, sha512-verified release installed user-level over the backend's
  ssh, a wrapper with the flags fixed and no per-container override, a drop-in registering the
  runtime name engine-wide, an enabled user unit); `fy box up` creates the box through that
  socket (`CONTAINER_HOST=ssh://…`, so no VM config change and no restart) and hands the box that
  socket as its own, so a sibling or an in-box `fy up` cannot come up unsandboxed. Fail-closed on
  a socket that does not answer with the gVisor runtime and on a box that came up under another
  runtime (removed before bootstrap); an already-up box from before the posture nags to recreate.
  In-box `fy verify` gains a row checking the kernel the box actually runs on. Both VM backends
  (`lima`, `podman`). Measured 2026-09-13 on the Mac and the Linux rig: the route, the cost
  (suite 1.2× on the Mac) and the flag boundary are in docs/isolation-layers.md. The box mounts a
  narrowed view of that socket, not the runsc socket directly: a small in-VM stdlib filter (a
  further user unit) forwards to it but strips the runtime opt-out (`oci_runtime`, `dev.gvisor.*`,
  the compat `Runtime`) from every container-create and refuses an unparseable create, so the box
  cannot escape gVisor even on a podman ≥ 6 that honours a client-chosen runtime (shown live on
  podman 6.1.1: the same create came up crun through the raw socket, runsc through the filter).
- **`[machine].host_wall` — enforce the egress wall on the host too (Linux).** With
  `wall = true` and a host that has `nft` + cgroup v2, foldyard starts the Lima VM inside its
  own systemd scope and loads a host nftables table matching that scope: only this project's
  daemon band, the VM's own loopback plumbing (SSH forward, Lima's host resolver) and the
  host's resolvers get out, so even a guest-kernel
  exploit that flushes the in-VM wall leaves through a host that rejects it. Loading the table
  is `sudo nft` on every `fy up` (a passwordless sudoers rule for `nft` makes it silent). A VM
  already running outside its scope — started before the option was on — is refused until
  `fy machine stop && fy up`; asking for it on a host that can't enforce it (macOS) is a
  preflight error, never a silent downgrade. Default off. Env: `MACHINE_HOST_WALL`.
- **`fy box up` warns when a running box's foldyard isn't the one the host runs.** The box's
  foldyard is installed by the bootstrap, which only runs on a freshly *created* box — so a host
  upgrade leaves the two sides on different versions indefinitely, with every `fy box up` in
  between reporting "already up". `up` now asks the running box what `foldyard --version`
  answers (over the same login-shell route as `fy box exec`) and compares it with the host's, so
  the mismatch is announced with the recreate that fixes it rather than being discovered later
  as a refusal. A box where nothing answers — no install, a broken one, or one too old to know
  the flag — is treated as drift, which is correct: it is the most stale case there is. And the
  recreate now actually moves it: the bootstrap's foldyard step used to skip whenever *a*
  foldyard was on PATH, so one baked into the image or kept on a retained uv tool dir
  (`[box].caches`, a `UV_TOOL_DIR` pin in `[box].env`) outlived every recreate; the guard is now
  "present *and* the host's version".

### Changed

- **`machine ensure` caps the VM's journal at 1G and quietens the API service's access log.**
  Containers log to journald, so the guest journal IS the container logs — and Fedora's default
  cap is min(10% of the fs, 4G): a 90G VM disk sat at 4.1G of journal, the largest producer
  being the rootless `podman.service` at its stock `LOGGING=--log-level=info` (the TUI/doctor
  polling `_ping`/list-containers, ~48k lines per 2h), then conmon relaying container stdout.
  The cap is a root-owned `/etc` drop-in: on Lima it is rendered into the root boot script (so
  the provisioning id changes and an existing VM re-provisions on `fy machine stop && fy up`);
  on podman machine — no boot script, but the appliance user keeps passwordless sudo — it goes
  over ssh with `sudo -n`. The log level is a user-level `podman.service` drop-in
  (`Environment=LOGGING=--log-level=warn`) over ssh on both, restarting the service only when
  the file changed. Both are housekeeping, not a posture: a guest that refuses is a warning,
  never an abort. Measured after applying by hand: 4.1G → 983M.
- **`fy code` authors the attached-container config itself from a declarative `[vscode]` table,
  read from the ADOPTED copy; the in-box generator script and `[vscode] workspace_file` are
  removed.** `extensions` (marketplace ids installed on attach) and `[vscode.settings]` (applied
  to the box's server) are config, behind the same adopt gate as every host-consequence verb —
  the extensions list decides what the host installs, so a box edit to it is inert until adopted.
  `remoteUser` and the daemon-port pin are foldyard facts. Nothing under the mount is read any
  more: not a consumer script's output, not `.vscode/extensions.json`, and not a
  `.code-workspace` — the attach is always the checkout folder, one shape for a worktree's
  lifetime, so window-scoped editor state saved in one session (Peacock colours, …) is read by the
  next. **Migration:** drop `workspace_file` and any generator script from `[vscode]`; move the
  extensions it collected into `[vscode] extensions` and its machine settings into
  `[vscode.settings]`; commit per-folder `.vscode/settings.json` files if you relied on a
  generated multi-root workspace ([ADR-0026](./docs/adrs/0026-vscode-attach-config-is-declarative.md)).
- **The example consumer's api is a uv project, and the example's box shadows its venv.**
  `example/api` gains `pyproject.toml` + `uv.lock` (the same three pins, now locked) and an image
  built from that lockfile with uv; `example/foldyard.toml`'s `[box]` wires the in-tree-artefact
  pattern every real consumer needs — `shadow_volumes = ["api/.venv"]`, a frozen `uv sync`
  warmup, `UV_LINK_MODE=copy` + `UV_FROZEN` — so the box's dependencies live in a per-box volume
  on the VM disk (never on the host tree, and never on the shared mount: the fifth rig session
  measured that placement as a 100× layer for installs under crun alone). `tests/test_example.py`
  pins the shape.
- **Root in the Lima machine VM is now boot-time only — the VM user's passwordless sudo is
  dropped.** The dev box runs as that user's uid, so a container-runtime escape used to be one
  `sudo nft flush ruleset` from open egress. foldyard now records a root boot script in the
  instance config that narrows the sudo grant to `shutdown` and (re)installs the egress wall on
  every boot; the host no longer runs `sudo` in the guest, and reads the guest's own report of
  what it applied. **Migration:** every existing Lima VM keeps the old grant until it is
  restarted once — `fy up` refuses a running VM whose recording is stale and tells you to
  `fy machine stop && fy up`. Only `[machine].backend = "lima"` is affected.

- **`fy verify`'s mount audit reads the VM's real mount table** (PID 1's, via `--pid=host`)
  instead of a `--privileged` container's own namespace, which never showed VM-level mounts — a
  Lima VM mounting the operator's whole home previously passed. The repo and worktrees-root
  mounts are exempt by exact path; a home mount, a sibling, or a nested bind still fail.

### Fixed

- **`fy code` now pins every `[ports]` base against VS Code auto-forwarding, as the range a
  worktree offset can land on (`"4400-4489": onAutoForward: ignore`).** The mirror of the
  minter/proxy pin: under podman machine a publish of `127.0.0.1:P` is a gvproxy bind on the
  host's loopback, so once VS Code auto-forwards P — it read `127.0.0.1:4400->4400/tcp` off a
  `ps` line while the stack was down after a reboot, then `remote.restoreForwardedPorts`
  re-bound it on every reopen — the container can never start (`bind: address already in use`
  on every `up`), and nothing in the box can see the holder. Seen live on a consumer's Auth0
  simulator port; the same instance had a worktree's `APP_PORT+1` forwarded, hence the range.
- **The gVisor posture's socket probe could report podman's own client version block as "the
  runtime".** podman-remote prints its client info to stdout (exit 125) when it cannot reach the
  server, and the probe trusted stdout — so the first `fy box up` after a fresh provisioning,
  racing a service that was 'active' but not yet listening, failed with `answers with runtime
  'OS: linux/amd64…'`. The exit code now decides (a failure reports podman's stderr), and a
  refused connection is retried for a few seconds before the fail-closed abort.
- **The in-VM wall left the VM user's `~/.config` ROOT-owned on a fresh guest image.** The
  boot-time wall script (root) wrote its proxy `environment.d` drop-in with a bare `install -d`,
  which creates a missing `~/.config` as root and only chowned the leaf. Fedora 44 images
  happened to pre-create the directory; on a Fedora 45 guest every later user-level step then
  failed — Lima's `systemctl --user enable podman.socket` (so the API socket never came up and
  `limactl start` timed out) and the gVisor posture's own drop-ins. Every directory the wall
  creates under the user's home is now created owned by the user (pinned by a test over the
  script). A new rendered provisioning id, so an existing VM re-provisions on
  `fy machine stop && fy up`.
- **`fy verify` in a box with an SSH origin and no ssh client reported the push refusal
  UNPROVEN.** `git ls-remote` fails there with `cannot run ssh`, which the check read as a
  network failure — so every consumer with a `git@…` origin (this repo included) was short of
  ALL PASS by construction. With no keys and no agent (asserted beside it), a transport that
  does not exist IS the refusal: the row now passes and says why. A real network failure
  still reads as unproven.
- **`fy verify` under `[machine].runtime = "gvisor"`**: the VM mount audit needs a
  `--pid=host` reach into the VM that gVisor blocks (the same property `escape refused` proves),
  so from inside a gVisor box it cannot run — now an advisory naming the reason and where to
  audit the boundary instead, not a FAIL (docs/verify-false-pass.md). The crun path keeps its
  FAIL on an empty mount table.
- **foldyard's own `[[box.tools]]` apt steps failed on every fresh box** (`Unable to locate
  package nodejs/just/unzip`): the packaged image ships no apt lists. An `apt-lists` step
  fetches them once, first; docs/configuration.md documents the shape for consumers.
- **`fy doctor` no longer fails forever on a consumer the proxy never serves.** The proxy
  plugin's `mitmproxy` / `mitm CA` / `egress proxy` rows were unconditional, while its daemon
  and the box's routing are gated on a declared `[proxy]` or an active injector — so a consumer
  with neither saw a red "NOT running … the box always routes through it" on every run, which
  was false for it. The rows now follow the daemon's gate: none for such a consumer; the two
  prerequisites (not the listener row) for a declared-but-off injector, so a missing
  mitmproxy/CA shows BEFORE `fy mode github=app` needs it; all three once opted in or armed.
  `DoctorContext` gains the current `mode` for this. Found by the first `fy doctor` on the
  Linux rig's no-`[proxy]` example.
- **`fy verify`'s mount audit judged the whole `/proc/1/mounts` line, not the mountpoint.** Run
  inside the box (uid 0) the home it looks for is `/root`, and a Fedora guest's btrfs root line
  carries `subvol=/root` in its *options* — a false FAIL on a table that exposes nothing. The
  audit now matches the mountpoint field only; the same path *as* a mountpoint, and the
  repo-mount exemption, are unchanged. Found by the first in-box `verify` on a Linux host.
- **A version-window refusal inside the box pointed at a command that does nothing.** `_fix`
  told an in-box reader to run `fy box up` from the Mac, and its docstring claimed the bootstrap
  reinstalls foldyard "at every `fy box up`". It doesn't: `box.up` early-returns on a running box
  before any bootstrap step and prints `✓ dev box … already up`. So the one reader this message
  exists for — someone whose in-box `fy` is too old to honour the repo's config — followed the
  advice, saw a tick, and was no better off. It now says `fy box down && fy box up`, matching the
  three drift warnings that already sit in that same early-return branch.


## 0.2.1 — 2026-09-10

### Fixed

- **`fy tui` shows every workspace from inside a worktree, and opens on the one you're standing
  in.** The workspace list anchored on `config.repo_root()`, which stops at the *current*
  checkout's `foldyard.toml` — so run from a worktree it looked for siblings under a
  `<worktree>-worktrees` directory that has never existed. The list collapsed to a single card
  labelled "main" whose path was actually the worktree, and no worktree could be seen, switched to
  or acted on from any worktree; the workaround was to go back to the main checkout.

  The set of workspaces is a property of the repo, so it now anchors on the primary checkout
  (`stack.main_repo()`, git's common dir — the same anchor `worktree_keys()` already used) and is
  identical wherever `fy` runs. Where you stand picks the initial SELECTION instead: the TUI opens
  with your own checkout's card highlighted, so the Mode tab, the plugin panels and the workspace
  actions target the checkout a bare `fy up` here would act on. It sets the opening row only —
  later refreshes never yank the highlight back from wherever you moved it.

### Added

- **`fy init` now stamps the version floor it scaffolds for.** A fresh `foldyard.toml` carries
  `[project].min_foldyard_version` set to the `fy` that wrote it — the only version that file is
  known to be right for — with the recommendation and the reasons ledger parked as commented
  lines beside it, and the floor reported on the way out (`✓ wrote … (floor: foldyard >= X)`).

  The window landed in 0.2.0 as something a consumer had to know to write, which meant a repo
  that never heard of it kept the default: no floor at all. Since an older `fy` doesn't fail on
  config it doesn't understand — it ignores those keys and does the old thing, silently — a repo
  with no floor is the case the mechanism exists for, and the scaffold is the one place foldyard
  can put a floor there without asking anyone. A `fy` that can't name its own version
  (`0+unknown` from a source tree, or a local build) leaves the key commented rather than
  stamping a number that means nothing.

- **An architecture diagram, above the README's "Why".**
  `docs/assets/foldyard-architecture.svg` draws the host/yard split in one picture: what runs
  outside the fence (the CLI, the supervisor, the credential minters, the egress proxy, the
  adopted config, the access modes), what runs inside it (the compose stack, the dev box, the
  coding agent, per-worktree boxes), and what `fy verify` checks across the seam.

  The previous drawing had drifted from the code. It credited `verify` with a
  gitleaks/TruffleHog secret scan it has never run — that stays the operator's clean-repo
  PREREQUISITE (`docs/security.md`), and the diagram now says so; it claimed `verify` re-runs
  on every mount when it is on-demand only; it called the yard a podman machine after Lima
  became the default backend; and it led with "no daemon running ⇒ no credential flow", true
  when written but misleading now that `fy up` / `fy box up` start the supervisor themselves so
  the always-on proxy is up. The guarantees that carry that weight today are drawn instead: the
  mode file is host-side and the yard only ever gets a read-only copy, and repo config is inert
  until adopted (ADR-0022). Redrawn as hand-editable SVG (14 KB, one `<style>` block) rather
  than a 350 KB design-tool export, so the next correction is a text edit.

## 0.2.0 — 2026-09-08

### Added

- **A declared version window: `[project].min_foldyard_version` and
  `[project].recommended_foldyard_version`.** A consumer repo can now say which foldyard its
  checkout needs. The floor **refuses to run** below it; the recommendation prints one line and
  continues (`FOLDYARD_NO_VERSION_NUDGE=1` silences it). `fy doctor` shows the window as its own
  row, and reports the nudge even when that variable is set.

  The nudge speaks only on `fy up`, `fy box up` and `fy host`. A warning on every invocation is
  filtered out by the reader within a day and takes the rest of foldyard's stderr with it, so it
  is spent on the verbs that start a session. `docs/configuration.md` covers what a
  recommendation is for — briefly, staging a floor so it lands as a formality rather than an
  ambush — and when to leave it unset.

- **`[project.foldyard_version_reasons]`** — an optional ledger, keyed by version, of why the
  consumer wanted each foldyard it adopted. Both the nudge and the refusal list the entries
  between the version you have and the bound you are pointed at, so the message says what you
  would *gain* rather than only which number to type. Entries are appended and never rewritten,
  so a reason cannot drift out of date with the version it describes, and the floor prunes them
  (anything below it is unreachable).

  The floor refuses rather than warns because `foldyard.toml` is read with `.get()` and no schema:
  unknown keys are tolerated by construction, so an old `fy` against a new config doesn't fail —
  it silently ignores the new keys and does the old thing. A warning is not enough for a failure
  mode that leaves no trace. Raise the floor in the same commit that adds the setting it needs.

  Both bounds are declarative; **foldyard never asks PyPI what the latest release is.** `fy` runs
  inside the box too, where egress is default-deny, so a lookup would mean punching an allowlist
  hole in the zero-egress posture to power a cosmetic message — and a consumer pins its CI
  deliberately so it doesn't float with someone else's release, which makes the repo's own opinion
  of "current" the more useful one.

  Inert until a consumer declares a bound, so existing repos see no change. Note the inherent
  limit: a floor only protects from *this* release onward — any older `fy` ignores the key and
  always will. It can't rescue a migration already in flight; it earns its keep on the next one.
  See `docs/configuration.md` and the 2026-09-08 amendment to ADR-0020.

## 0.1.0 — 2026-09-07

The first release intended for general use, and the end of the extraction: this repository is now
foldyard's home. Up to `0.0.1` it was a squash-start snapshot force-pushed from the monorepo it was
carved out of; from here history is real and pull requests are merged rather than ported.

### Added

- **`fy --version`** — foldyard can now name itself. `__version__` resolves lazily from the
  install metadata (PEP 562) instead of being a hand-maintained literal, so it can no longer drift
  from `[project].version`, which is what the box already pins itself to.
- **`[machine].vmtype`** — pin the Lima VM type (`vz`, `qemu`, `krunkit`) rather than taking Lima's
  default. See `docs/isolation-layers.md` for which layer each backend actually gives you, and
  `docs/firecracker-and-microvm-backends.md` for why Firecracker is not one of them.

### Fixed

- **`fy tui` could crash with `NoMatches` on the Network Log's wall pane.** The 1s panel-refresh
  timer checked that `#network-manage` was mounted and then queried a *different* widget
  (`#network-manage-summary`) without a guard, so a tick landing while the pane was half-mounted
  (startup) or half-detached (teardown) took the whole TUI down.
- **`fy verify` could report `ALL PASS — isolation intact` without having tested anything.**
  Every check in the battery asserts an ABSENCE, so each one passed when its probe merely failed to
  run — and a VM with no images behind a wall with no proxy (foldyard's own default posture, cold
  cache) certified itself clean. Each negative check now carries a positive control, and the
  `git push` refusal reports UNPROVEN rather than PASS when origin was never reached. `_HOST_PATHS`
  was macOS-only (`/Users|/private|/var/folders|/Volumes`), so the host-filesystem leak check passed
  vacuously on Linux and WSL2; it is now derived from the real host home plus the fixed points other
  platforms expose a host filesystem at.

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
