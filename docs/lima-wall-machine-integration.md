# Lima: routing to the host proxy, the VM firewall and the host firewall

**Status: built.** The VM firewall is validated live on macOS (Lima/vz) and runs in CI on Linux
and WSL2 hosts (`tests/test_wall_e2e.py`, the `lima-host-e2e` and `wsl2-host-e2e` jobs); the
host firewall runs in the Linux job. This is the design reference for three pieces that work
together on the `lima` backend:

1. **Routing** (always on for `backend = "lima"`): the box reaches the host-side proxy and token
   services through Lima's guest→host gateway (`config.host_alias()`).
2. **The VM firewall** (`[machine] firewall = true`): nftables default-deny inside the VM, set up
   as root at boot, so the host-side proxy is the *only* way out — fail-closed.
3. **The host firewall** (`[machine] host_firewall = true`, Linux): the same rule enforced on
   the host around the VM process, so even VM-root can't get out.

Terms: [glossary](./glossary.md). Companions: [lima-backend-scope.md](./lima-backend-scope.md)
(the backend contract), [isolation-layers.md](./isolation-layers.md) (which layer does what per
platform), and the standalone proof kit in
[archive/lima-network-forcing-kit/](./archive/lima-network-forcing-kit/). The design where the
proxy ran *inside* the VM was rejected — [last section](#the-rejected-alternative-run-the-proxy-inside-the-vm).

## 1. Routing — the problem was the alias, not reachability

The box addresses host-side daemons (`FY_PROXY`, `GCP_MINTER_URL`) by a host alias.
`host.containers.internal` reaches the host only under podman machine's gvproxy
(`192.168.127.254`). Under Lima, podman resolves it to the VM's own gateway, where nothing
listens. Lima's user-mode network, though, always forwards `192.168.5.2` to the host (the guest
calls it `host.lima.internal`, a name containers can't see).

So one helper, `config.host_alias()`, picks the address:

- `podman` backend → `host.containers.internal`
- `lima` backend → `192.168.5.2` (`config.LIMA_HOST_GATEWAY`); `FY_HOST_ALIAS` overrides it

Its consumers are `proxy.derive_env` (`FY_PROXY`), `gcp.derive_env` (`GCP_MINTER_URL`) and the
in-box daemon probes. A literal IP sidesteps podman's alias machinery and works for VMs that
already exist. Nothing else moved: the proxy, the supervisor, the per-worktree ports, CA staging,
the allowlist and `host.env` all stay on the host.

```
┌─ host ────────────────────────────────────────────────────────────────────────┐
│  host.env (real credentials) · allow-effective.json · egress.jsonl            │
│  mitmdump egress proxy :<range base>+offset (allowlist + injection + log)     │
│        ▲ 192.168.5.2 (Lima user-mode network → host)                          │
│  ┌─────┴────── Lima VM (one per project, several at once) ───────────────────┐│
│  │  [machine] firewall = true → nft `fy_wall`: default-deny the VM user's uid ││
│  │      and its subuids; open: lo · local DNS · 192.168.5.2 tcp {port range} ││
│  │      + environment.d proxy env (image pulls and builds go via the proxy)  ││
│  │  ┌────────── the dev box (rootless podman container) ──────────────────┐  ││
│  │  │  HTTPS_PROXY=http://192.168.5.2:<port>  + combined CA bundle        │  ││
│  │  │  (egress leaves as the VM user's uid → caught by the firewall)     │  ││
│  │  └──────────────────────────────────────────────────────────────────────┘  ││
│  └────────────────────────────────────────────────────────────────────────────┘│
└────────────────────────────────────────────────────────────────────────────────┘
```

### Per-project port ranges

Each project gets its own 200-port range on the host (the code calls it a band), allocated
first-come and kept forever in a flock-guarded `~/.foldyard/ports.json`, starting at 41000
(`src/foldyard/ports.py`):

- base + 0..89 — proxy listeners (main checkout +0, worktrees +1..89)
- base + 100..189 — token service (gcp) listeners, same offsets
- base + 190 — the Lima VM's ssh forward, when Podman Desktop follows the VM

Why: with one global port, two projects' supervisors each bound it and each reaped the other's
proxy as orphaned, every tick — pulls died mid-blob and requests carried the wrong project's
credentials. So:

- `box_up` pins the resolved bases into the box's env (the box can't read the host registry).
- The VM firewall opens only *this* project's ranges, so a box can't reach another project's
  daemons. The range is rendered into the boot script, so a moved range changes the script's id
  and needs a VM restart (section 2).
- The supervisor's orphan reaper matches this project's staged addon *file path*, not a
  `state_dir` prefix (`~/.foldyard/app` is a prefix of `~/.foldyard/app2`).
- Preflight refuses a worktree offset outside 0..89 (a pin ≥90 would land outside the opened
  range or collide with the token-service span).
- `FY_PROXY_PORT` / `GCP_MINTER_PORT` still override everything.

A box created before a project's range moved has the old proxy port baked into its env; `fy box
up` on such a running box prints a recreate warning (`_warn_stale_proxy_port`). Recreate it with
`fy box down && fy box up`.

<a id="2-enforcement--machinewall--true"></a>

## 2. The VM firewall — `[machine] firewall = true`

Routing alone is *cooperative*: a process that ignores `HTTPS_PROXY` goes direct. The VM firewall
makes Lima **fail-closed**.

**Config.** `config.machine_wall()`: `MACHINE_FIREWALL` env → `[machine] firewall` → default
`false`. `fy init` scaffolds `firewall = true`. Preflight aborts `fy up` if the firewall is on
without `backend = "lima"`, or with nothing routing the box (no `[proxy]`, keyless agent or
`[[inject]]` — that would be an airgapped box).

**The rules** (`assets/machine-wall/machine-wall.sh`, `install|uninstall|status`, idempotent;
never `flush ruleset`, because netavark/pasta keep their own nft state):

- `fy_wall` / `fy_wall6` tables default-deny the **VM user's uid and its rootless-podman subuid
  range** (from `/etc/subuid`). Normal container egress leaves as the user's uid through pasta,
  but a `--network=host` container running as a non-root container user leaves with a *subuid*;
  covering the range closes that bypass.
- Open: loopback, established flows, **DNS to local resolvers only** (`127/8`, `10/8`,
  `172.16/12`, `192.168/16` — `:53` to a public IP is a tunnel, not resolution), and
  `192.168.5.2` tcp on this project's port ranges only.
- Refusals are REJECT + log, never a silent drop; `fy-wall-denied` in the guest lists them.
- A `forward`-chain reject covers bridged (rootful) container egress, and the rootful
  `podman.socket` is masked — container-root over it would be VM-root.
- Proxy env via `environment.d` (the user manager, so the rootless podman service) and
  `profile.d`, so image pulls and build `RUN` steps — which also leave as the firewalled uid — go
  through the host proxy. `NO_PROXY` includes the gateway, so a container's direct calls to
  host-side daemons (e.g. an emulator → `GCP_MINTER_URL`) don't get tunnelled through the proxy.
- A systemd `fy-wall.service` (oneshot nft load) keeps it across restarts.

**Set up at boot, as root; the host never runs `sudo` in the guest.** `machine.ensure` /
`recreate` render one `provision: mode: system` script (`assets/machine-wall/guest-boot.sh`, with
`machine-wall.sh` embedded) and record it in the instance's `lima.yaml` via `limactl edit --set`,
which only works on a stopped VM. Lima runs it as root on every boot. It:

1. narrows Lima's passwordless sudo grant to `shutdown` only (cloud-init re-creates
   `NOPASSWD:ALL` every boot, so this runs every boot too);
2. installs or removes the firewall from its own embedded copy, never from the repo mount;
3. writes what it applied to `/run/fy-wall/state`, world-readable.

**What the host checks.** The host keeps no state file of its own. It compares two things:

- *What will apply at next boot:* the script's id — a hash of the rendered script, which
  includes the firewall setting and the port ranges — read back from the `# fy-provision <id>`
  line in `lima.yaml`. If the config wants a different id and the VM is running, `fy up` refuses:
  `fy machine stop && fy up`. That restart is the price of root-only-at-boot; ranges are sticky,
  so it is rare.
- *What did apply:* after every start and on each steady-state `fy up`, `_guest_state()` reads
  `/run/fy-wall/state`, `systemctl is-active fy-wall.service` and `is-enabled podman.socket` —
  none needs root. A mismatch (a reset VM, a failed boot script, a re-enabled rootful socket)
  fails closed and points at `limactl shell <name> cat /run/fy-wall/boot.log`.

There is no separate CLI verb: turning the firewall on or off is editing `foldyard.toml`, then
`fy machine stop && fy up`.

**What `fy verify` checks** (in the box, with Lima and the firewall on): direct connections to
`1.1.1.1:443` and `1.1.1.1:53` must both be refused, with the proxy path as the positive control.
The box is unprivileged, so it can't inspect VM-root state; that is the host's `_guest_state`
check above.

**What this protects.** Real credentials stay on the host regardless. The box has no sudo and no
rootful socket, so for an agent the firewall is enforcement. The VM user has no route to VM-root
either (the sudo grant is gone), so only a guest-*kernel* exploit reaches VM-root — which could
flush the firewall but still finds no credentials. Section 3 closes that gap on Linux.

The full red-team battery (the firewalled user can't `nft flush`, the subuid bypass, the `:53`
tunnel, in-stack service→service calls) runs in `example-lima-wall/test_network.sh`, not in
`fy verify` — an unprivileged box can't run it, and a per-`fy up` check shouldn't run
destructive probes.

<a id="3-host-side-enforcement--machinehost_wall--true-linux"></a>

## 3. The host firewall — `[machine] host_firewall = true` (Linux)

Section 2 is enforcement the guest applies to itself, so a guest-kernel exploit that reaches
VM-root can remove it. The host firewall matches the VM's own traffic *on the host*, where the
guest has no reach. Lima's QEMU driver runs the guest's user-mode network inside `qemu-system`,
so every guest packet leaves the host as that process. Code: `src/foldyard/hostwall.py`.

- **Matched by a cgroup v2 slice, not a uid.** Your other processes share your uid; only the VM
  lives under its slice. `machine._start` launches the backend under `systemd-run --user --scope
  --slice fy-machine-<vm>.slice --unit fy-machine-<vm>.scope`, so limactl, the host agent and QEMU
  all land in one scope under one per-VM slice. The table matches the *slice*: `socket cgroupv2`
  compiles the path to a cgroup id at load, a scope dies with its last process, but a slice
  survives being emptied and keeps its id across VM restarts. A slice that is stopped and
  recreated gets a new id and the loaded rule silently matches nothing — which is why foldyard
  probes rather than assumes.
- **The table is the same every boot.** `hostwall.render` emits one table per VM
  (`fy_host_wall_<vm>`):
  - OUTPUT, for the slice: established/related, `:53` to any resolver (the host agent resolves for
    the guest), loopback allowed out under a per-project conntrack mark, else REJECT.
  - INPUT, for loopback flows carrying that mark: allowed only when the *listening* socket is in
    the VM's own slice (the host agent's resolver, QEMU's SSH forward — ports Lima picks per boot,
    never named) or on this project's port ranges, else REJECT.

  The mark is foldyard's byte over the project's range base, so two projects' tables never judge
  each other's flows. Nothing in the table comes from a running VM.
- **You install it once; foldyard probes it on every `fy up`
  ([ADR-0028](./adrs/0028-no-elevation-on-the-host-operator-applies.md)).** foldyard never runs
  `sudo`. `fy machine host-firewall` writes and enables the slice (a persistent user unit,
  `fy-machine-<vm>.slice`, under `$XDG_DATA_HOME/systemd/user/`), renders the table and a system
  unit `fy-host-wall-<vm>.service` (bound to `user@<uid>.service`, so it loads once your user
  manager is up and drops when it stops) into `~/.foldyard/<project>/host-wall/`, and prints them
  with the four `sudo` lines that install them. Then each `fy up` probes from a child in the
  slice: it must be refused a loopback listener outside the slice and a connection to
  `192.0.2.1:9` (TEST-NET-1, never routable), and must reach a listener on the project's range.
  If not, `fy up` refuses and says which half failed. The same probe is the verb's status line
  and `fy doctor`'s row. `fy machine stop` and `rm` leave the install in place;
  `fy machine host-firewall --uninstall` prints the removal steps.
- **Fail-closed, never a silent downgrade.** Preflight refuses `host_firewall` without
  `firewall`, and on a host without `nft` and cgroup v2 (`hostwall.available()`; on macOS it
  reports itself unavailable). `ensure` refuses a VM found outside its own scope-under-slice —
  started by hand, or before the option was on — because firewalling the login session's scope
  would cut off your whole shell: `fy machine stop && fy up`.
- **Not on WSL2.** The stock WSL2 kernel lacks `CONFIG_NFT_SOCKET`, so the rule can't load and
  the option fails closed.

## Where it is validated

- **Unit and golden tests:** `test_config` (`host_alias`, the firewall keys), `test_plugins`
  (Lima `derive_env`), `test_preflight` (the refusal matrix, `[[inject]]`-only routing, the
  offset ≤89 guard), `test_machine` (the boot script: sudo narrowed, firewall installed/removed,
  `bash -n` clean, only Lima's template fields; recorded on a stopped VM, a running stale VM
  refused, the guest's report checked after every start), `test_machine_backend` (the `limactl
  edit --set` recording and reading back its id), `test_hostwall` (the rendered table and units,
  the probe), `test_verify` (the `:443` and `:53` probes), `test_ports`, `test_supervisor` (the
  project-scoped orphan reaper), `test_box` (the stale-port recreate warning),
  `test_example_lima_wall`.
- **Live, in CI:** `tests/test_wall_e2e.py` — both firewalls via `fy up`: refused until the
  host firewall is installed, then the table on the VM's own slice, direct guest egress refused,
  DNS resolving, the proxy the way out, the api still served, and a verb run without the
  firewall config refused on the firewalled VM. It skips on WSL2 (no `CONFIG_NFT_SOCKET`).
- **Live, by hand:** macOS (Lima/vz) — `sudo -n true` refused in the guest, `sudo -l` lists only
  `shutdown`, the firewall active, direct egress refused; `example-lima-wall/test_network.sh` for
  the destructive battery.

**One caveat stays with you, not foldyard:** stack containers inherit the VM's proxy env, so
service→service HTTP inside the stack needs a per-service `no_proxy` (the `example-lima-wall`
`worker` shows the fix; `test_network.sh` §E asserts it).

## The rejected alternative: run the proxy inside the VM

It looked neat (`host.containers.internal` would just work), but it would have pulled the proxy's
whole world into the VM: mitmproxy and its CA lifecycle, `allow-effective.json`, and **the
`host.env` credentials, synced into the VM the agent lives in** — breaking the rule that
credentials never leave the host. It would also have run the token services (GitHub App key
material) in the VM, added a second daemon supervisor, moved the network log out of reach of
`fy allow` and the TUI, left the per-worktree ports unsolved, and still fixed only `FY_PROXY`,
not `GCP_MINTER_URL`. Routing to the host fixes every consumer of the alias at once and keeps one
proxy on one machine.
