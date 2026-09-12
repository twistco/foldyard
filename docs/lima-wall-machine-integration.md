# Lima × the Mac proxy × `[machine].wall` — routing + enforcement for the real machine

**Status: BUILT (Mac-unvalidated).** Supersedes the earlier draft of this doc, which proposed
relocating the egress proxy INTO the lima VM. That design was dropped before implementation — see
"The rejected alternative" at the bottom for why. What shipped instead is two decoupled pieces:

1. **Routing** (always on for `backend = "lima"`): the box reaches the **Mac-side** proxy/minters
   via Lima's guest→host gateway — `config.host_alias()`.
2. **Enforcement** (opt-in, `[machine].wall = true`): an nftables default-deny wall provisioned
   into the real lima machine VM makes that Mac proxy the *only* way out — fail-closed.

Companions: [lima-backend-scope.md](./lima-backend-scope.md) (the backend contract) and the runnable
[lima-network-forcing-kit/](./lima-network-forcing-kit/) (the standalone proof + security
argument). NB the packaged `fy wall` verb and `assets/wall/` were RETIRED once this real-machine
integration landed — the kit under `docs/` remains as the standalone red-team rig, but foldyard
no longer ships a separate proof VM (it couldn't be exercised through an example project anyway).

## 1. Routing — the problem was the alias, not reachability

`host.containers.internal` is how the box addresses Mac-side daemons (`FY_PROXY`,
`GCP_MINTER_URL`). That alias reaches the Mac **only under podman-machine's gvproxy**
(`192.168.127.254`); under lima, podman resolves it to the VM's own gateway — nothing listens
there, so keyless/proxy egress connection-refused. The earlier draft read that as "lima can't
reach the Mac"; it can: Lima's user-mode network **always** forwards `192.168.5.2` (aka
`host.lima.internal` in the *guest's* /etc/hosts — invisible inside containers) to the host.

So the fix is one address helper, `config.host_alias()`:

- podman / native → `host.containers.internal` (unchanged, byte-identical env)
- lima → `192.168.5.2` (`config.LIMA_HOST_GATEWAY`); `FY_HOST_ALIAS` env overrides (escape hatch)

Consumers: `proxy.derive_env` (`FY_PROXY`), `gcp.derive_env` (`GCP_MINTER_URL`),
`devmode.probe` (in-box daemon probes). Emitting the literal IP deliberately sidesteps podman's
alias machinery (`containers.conf host_containers_internal_ip`, pasta `--map-guest-addr` quirks)
and works for already-created VMs. Everything else — supervisor, per-worktree ports, CA staging
under the checkout (which lima mounts), allowlist, host.env, the Network Log TUI — is untouched:
the proxy never moved. The old preflight hard-block on lima+keyless is gone.

```
┌─ Mac ─────────────────────────────────────────────────────────────────────────┐
│  host.env (real creds) · allow-effective.json · egress.jsonl · minters        │
│  mitmdump egress proxy :<band>+offset  (allowlist + keyless injection + log)  │
│      band = the project's allocated base (ports.py; 41000, 41200, …)          │
│        ▲ 192.168.5.2 (Lima usernet host-forward)                              │
│  ┌─────┴────── lima machine VM (per-project, concurrent) ────────────────────┐│
│  │  [machine].wall = true →  nft `fy_wall`: default-deny the VM user's uid;  ││
│  │      open: lo · DNS · 192.168.5.2 tcp {proxy+minter port ranges}          ││
│  │      + environment.d proxy env (pulls/builds egress via the Mac proxy)    ││
│  │  ┌────────── the dev box (rootless podman container) ──────────────────┐  ││
│  │  │  HTTPS_PROXY=http://192.168.5.2:<port>  + combined CA bundle        │  ││
│  │  │  (egress NATs out as the VM user's uid → caught by the wall)        │  ││
│  │  └──────────────────────────────────────────────────────────────────────┘  ││
│  └────────────────────────────────────────────────────────────────────────────┘│
└────────────────────────────────────────────────────────────────────────────────┘
```

### Per-project port bands (found on the first real multi-project Mac run, 2026-07-05)

"Per-project, concurrent" in the diagram was aspirational until the Mac-side ports grew a
project dimension: the proxy/minter bases were global (8088/8079 + worktree offset only), while
the supervisor singleton lock is per-project — so TWO projects' supervisors each legitimately
held their own lock, both bound :8088, and each reaped the other's live proxy as "orphaned from
a dead supervisor" every ~10s tick, forever. Symptoms: mirror-image supervisor boot-loop logs in
both projects, keyless API errors (traffic landing on the proxy with the WRONG project's creds),
and registry pulls dying mid-blob (`unexpected EOF`) or failing TLS (`x509: unknown authority` —
the other project's MITM CA). Fixed by **ports.py**: each project gets a 200-port band
(41000, 41200, …) from a flock-guarded `~/.foldyard/ports.json`, first-come and sticky; proxy =
band+0..89, minter = band+100..189 (disjoint spans — the old 9-apart bases let worktree offsets
collide across daemons). `box_up` PINS the resolved bases into the box env (in-box derivations
can't read the Mac registry); the wall marker records the port set so a moved band re-provisions;
and the supervisor's orphan-reaper is scoped to THIS project's staged addon **file path** (not a
bare `state_dir` substring — `~/.foldyard/app` prefixes `~/.foldyard/app2`, which would re-open the
reap war), so any residual collision nags loudly instead of fighting. `FY_PROXY_PORT`/
`GCP_MINTER_PORT` env still override everything.

**Migration (upgrading an existing project to the band world):** a box built before bands has its
old proxy port (`:8088`) baked into its container env, which can't change in a running box — so
after the host supervisor moves to the allocated band, `fy box up` on an already-running box
prints a **recreate nag** (`_warn_stale_proxy_port`) rather than letting egress silently
connection-refuse. Recreate the box (`fy box down && fy box up`) to pick up the new port. A lone
upgrading project can also keep `:8088` by seeding `~/.foldyard/ports.json` with its base (what
Tangible does) — only *concurrent* projects strictly need distinct bands. Worktree offsets are
validated ≤89 at preflight (an explicit `WT_OFFSET`/`foldyard.local.toml` pin ≥90 would land the
proxy outside the wall's opened range or collide with the minter span).

## 2. Enforcement — `[machine].wall = true`

Routing alone is *cooperative* (exactly like the podman backend today): a box process that
ignores `HTTPS_PROXY` egresses direct. The wall makes lima **fail-closed**:

- `config.machine_wall()` — `MACHINE_WALL` env → `[machine].wall` → default **false** (opt-in
  until Mac-validated). Preflight enforces coherence: wall needs `backend = "lima"`, and wall
  without any `[proxy]`/keyless/injector is an airgapped box → both abort `fy up` early.
- `assets/machine-wall/machine-wall.sh` (`install|uninstall|status`, idempotent) — this has no
  in-VM proxy, no agent user, and must NOT `flush ruleset` (netavark/pasta nft state survives):
  - nft `fy_wall`/`fy_wall6` tables: default-deny the **VM user's uid AND its rootless-podman
    subuid range** (`{ $WALL_UID, subuid_start-subuid_end }` from `/etc/subuid`). Normal
    container egress NATs out as the user's own uid via pasta/slirp4netns, but a
    `--network=host` container as a non-root *container* user skips the NAT and egresses with a
    **subuid** — covering the subuid range closes that bypass (a hardening found in the 2026-07
    review; the review battery's C4 in `test_network.sh` exercises it). Open: lo, established,
    **DNS to LOCAL resolvers only** (`127/8,10/8,172.16/12,192.168/16` — a public-IP `:53` stream
    is an exfil tunnel, not resolution, so it's denied; that's tighter than the kit's
    accept-all-`:53`), and `192.168.5.2 tcp {<band>-<band>+89, <band>+100-<band>+189}` (THIS
    project's allocated daemon bases — ports.py — + the 0..89 worktree-offset span; NOT all
    ports, so a walled agent can't probe arbitrary Mac-loopback services, nor a SIBLING project's
    daemons). REJECT+log, never silent-drop; `fy-wall-denied` in the guest shows the denies. A
    `forward`-chain reject covers bridged (rootful) container egress; the rootful `podman.socket`
    is masked (container-root over it would be VM-root = `nft flush` = no wall).
  - proxy env via `environment.d` (user manager → the rootless podman service) + `profile.d`:
    this is what lets image PULLS and build RUN steps — which also egress as the walled uid —
    out through the Mac proxy (podman propagates proxy env into containers/builds by default).
    `NO_PROXY` includes the **Lima gateway `$GW`** so a container's DIRECT calls to the Mac-side
    daemons (e.g. the gcp emulator → `GCP_MINTER_URL=http://$GW:<minter>`) go straight there
    instead of being tunnelled through the mitmdump proxy, which runs ON the Mac and can't reach
    `$GW` from there. VM-level operations use the MAIN proxy port; each box gets its per-worktree
    `FY_PROXY`.
  - systemd `fy-wall.service` (oneshot nft load) → survives VM restarts.
- **Provisioned at BOOT, as root, from the host's config — the host never runs `sudo` in the
  guest (2026-09-11).** `machine.ensure`/`recreate` record ONE `provision: mode: system` script
  (`assets/machine-wall/guest-boot.sh`, with `machine-wall.sh` embedded) in the instance's
  lima.yaml via `limactl edit --set`, which refuses a running instance; Lima runs it as root on
  every boot, after cloud-init. The script narrows Lima's passwordless sudo grant to `shutdown`
  only (cloud-init re-creates `NOPASSWD:ALL` on every boot — the instance id changes each boot —
  so this too runs every boot), installs the wall root-owned from its own embedded copy (never
  from the repo mount), applies `install …`/`uninstall`, and writes what it applied to
  `/run/fy-wall/state`, world-readable. The **guest's own report is the source of truth**: after
  every start, and once per steady-state `fy up`, `_guest_state()` reads that file plus
  `systemctl is-active fy-wall.service` and `is-enabled podman.socket` — none needs root — and a
  mismatch (a reset VM, a failed boot script, a re-enabled rootful socket) fails `ensure` closed
  with `limactl shell <name> cat /run/fy-wall/boot.log` as the recourse. There is no host-side
  marker any more. A change — the wall flipped, a moved band, a VM created before the grant was
  dropped — is a different script id, and a RUNNING VM whose recording is stale is refused:
  `fy machine stop && fy up`. That restart is the price of "root only at boot"; bands are sticky,
  so it is rare. There is deliberately NO separate CLI verb — toggling the wall IS editing
  `foldyard.toml` + that restart; in-VM diagnosis is the boot log above and
  `example-lima-wall/test_network.sh`.
- `fy verify` (in-box, when lima+wall): raw proxy-ignoring connects to `1.1.1.1:443` **and
  `1.1.1.1:53`** must both be REFUSED — the fail-closed probe plus the port-53-tunnel probe. (The
  box is an unprivileged container, so it can't inspect VM-root invariants — those live in the
  host-side `_guest_state` check + `test_network.sh`'s C battery.)

**Security posture (matches the spike's conclusion, HANDOVER: "the backstop is a chokepoint
outside the VM"):** real credentials stay on the Mac, period. A VM-root escalation can flush the
wall (defense-in-depth, not the moat) but steals no creds; the box container itself has no sudo
/ rootful socket, so for the *agent* the wall is enforcement. **The VM user has no path to
VM-root either (closed 2026-09-11):** Lima's passwordless sudo grant — which made a
container-runtime escape landing as the VM user one `sudo nft flush ruleset` from open egress —
is narrowed to `shutdown` at every boot by the same script that installs the wall. Only a
guest-KERNEL exploit reaches VM-root now, and even that steals no credentials. Validated live on
macOS (Lima/vz): `sudo -n true` → "a password is required", `sudo -l` lists shutdown only, the
wall active, direct egress rejected. **Known residual:** the fuller red-team battery (walled user
can't `nft flush`, the subuid bypass) runs only in `test_network.sh` / the standalone kit —
`fy verify` and `_guest_state` cover the highest-value invariants (direct + `:53` egress refused,
the guest's applied state, rootful socket masked) but not the whole battery, because an
unprivileged box can't and a per-`fy up` host check shouldn't run the destructive checks.

## 3. Host-side enforcement — `[machine].host_wall = true` (Linux)

Section 2 is enforcement the guest applies to itself, so the one thing that beats it is a
guest-KERNEL exploit reaching VM-root (`nft flush`). `host_wall` closes that on a Linux host by
matching the VM's OWN traffic *on the host*, where the guest has no reach: Lima's QEMU driver runs
the guest's user-mode network inside `qemu-system`, so every guest packet leaves the host as that
process, and host nftables can single it out. Flushing the guest wall then gains nothing.

- **The match is a cgroup v2 scope, not a uid** — the operator's other work shares their uid;
  only the VM lives in the VM's scope. To make that scope predictable, `machine._start` launches
  the backend's start under `systemd-run --user --scope --unit fy-machine-<vm>.scope`, so
  limactl, the hostagent and QEMU all land in one transient cgroup and nothing else does.
- **Rendered for where the VM actually sits, on every `fy up`.** `foldyard.hostwall.render`
  emits a per-VM table (`fy_host_wall_<vm>`): established/related, loopback to this project's
  daemon bands (the same `base..base+89` spans section 2 opens) + the VM's own loopback
  plumbing, the host's resolvers (from `resolv.conf`) on `:53`, else REJECT. The plumbing is
  the loopback listeners the VM's host processes hold — `hostwall.listener_ports`, socket
  inodes from `/proc/<pid>/fd` against `/proc/net/*` — because the guest's DNS is Lima's host
  resolver: the hostagent serves it on a random loopback udp+tcp port and QEMU forwards each
  query there (the first rig run walled DNS by allowing only `resolv.conf`'s stub). QEMU's SSH
  `hostfwd` is the other one. The scope is read from the VM pid's `/proc/<pid>/cgroup`, the SSH
  port from `limactl list` (Lima allocates all of these per boot), and the
  table is loaded with `sudo nft -f -` as a declare-then-delete-then-declare idempotent replace —
  after every start, a revive, and each steady-state `fy up`, since it can't be read back
  without root. That root prompt is the price; a passwordless sudoers rule for `nft` is the
  operator's call and makes it silent.
- **Fail-closed, never a silent downgrade to the guest wall alone.** Preflight refuses
  `host_wall` without `wall`, and on a host without `nft` + cgroup v2 (macOS reports itself
  unavailable rather than branching on the OS). `ensure` refuses a VM found OUTSIDE its own scope
  — started by hand, or before the option was turned on — because walling the login session's
  scope it landed in would wall the operator's whole shell: `fy machine stop && fy up`. An
  unreadable SSH port or a failed load also stop `fy up`. `fy machine rm` removes the table;
  `stop` leaves it (inert once the scope is empty, re-rendered on the next up).

Run end to end on the Linux rig (2026-09-12, `fy machine ensure` with `MACHINE_HOST_WALL=1`
against the example): the VM created and started inside its scope, direct guest egress refused by
name and by IP (curl rc 7), DNS resolved, the band port answered 200 while an out-of-band port and
the host's sshd were refused, the operator's egress and `limactl shell` + the podman socket
untouched, `fy verify` ALL PASS under the wall, a hand-started VM refused, `fy machine rm` left no
table. Details in [isolation-layers.md](./isolation-layers.md) under "Host-side wall enforcement
on Linux".

## Validated where?

Unit/golden coverage: `test_config` (host_alias/machine_wall), `test_plugins` (lima derive_env),
`test_preflight` (block matrix + the nested-project trap + inject-only proxy routing + the
worktree-offset ≤89 guard), `test_machine` (boot provisioning: the rendered script drops the
sudo grant + installs/uninstalls the wall, `bash -n` clean, only Lima's template fields; recorded
before the first boot, updated on a stopped VM, a running stale VM refused, the guest's report
checked after every start and on the steady state, fails closed on a missing/mismatched report,
an inactive unit or an unmasked rootful socket + asset shape), `test_machine_backend`
(memory-MiB sizing, the `limactl edit --set` recording + marker read), `test_proxy_inject` (keyless always decrypts,
even vs an explicit passthrough listing), `test_verify` (wall posture: 443 + the `:53` exfil
probe), `test_ports` (band allocation + config derivations), `test_supervisor` (the project-scoped
orphan-reaper, incl. the prefix-collision guard), `test_box` (the stale-proxy-port recreate nag),
`test_example_lima_wall` (the locked-down fixture parses). The runnable **`example-lima-wall/` +
`test_network.sh`** (C battery now covers the subuid/host-network bypass + the port-53 tunnel + local
DNS still resolving) is the on-Mac harness for everything below.

**A 2026-07 high-effort review hardened the wall + band migration.** Fixes landed for: the
subuid/`--network=host` egress bypass, the unrestricted-`:53` exfil tunnel, the `NO_PROXY`-missing-
gateway misroute of container→Mac-daemon calls, marker-trust (VM recreated/reset ran unwalled) and
the mirror stale-off case, the lost verify coverage (in-box `:53` probe + host-side rootful-socket
check), the pre-band box stranding on a dead port (recreate nag), the unclamped worktree offset,
the supervisor's prefix-collision reap, and preflight missing `[[inject]]`-only routing. All are
code-complete + unit-covered; the wall-rule changes still want on-Mac validation via
`test_network.sh` (they can't be exercised without a real Lima VM).

**A real-Mac sizing bug was found + fixed here:** Lima's `--set .memory` was emitted as a rounded
2-decimal GiB string (e.g. `"3.91GiB"` for 4000 MiB), which macOS Virtualization.framework rejects
at *every* boot (`“memorySize” is not a multiple of 1 megabyte`), bricking the VM until deleted.
Fixed by emitting MiB verbatim (`_memory_mib`). A VM created before the fix must be
`limactl delete`d and recreated.

**Validated on a real Mac (2026-07-05, a second project's `fy up`):** lima VM download + create +
start end-to-end, and the wall provisioning (`✓ fy-wall installed: uid 501 default-deny; open:
lo, DNS, 192.168.5.2 tcp {…}`). That same run is what exposed the cross-project port collision
fixed by the bands above (and reached quay.io through the *fighting* proxies — pull errors that
should now be honest). **Still unvalidated** — the scope doc's SPIKE items, plus, new here
(`bash example-lima-wall/test_network.sh` drives all of these):

- container → `192.168.5.2` reachability under pasta (the routing hot path, `test_network.sh` §B2),
  and that Lima's usernet forwards it to Mac-loopback-bound daemons;
- `SUDO_UID` resolves to the podman user under `limactl shell … sudo`;
- pulls/builds actually pick the environment.d proxy env up (user-manager restart timing);
- stack containers inheriting proxy env (podman's default propagation) don't break in-stack
  service→service HTTP — compose services override with per-service `no_proxy` (the example's
  `worker` demonstrates the fix; `test_network.sh` §E asserts it).

## The rejected alternative (the earlier draft): run the proxy inside the VM

Superficially neat ("host.containers.internal becomes correct"), but it dragged the proxy's whole
ecosystem into the VM: mitmproxy installs + CA lifecycle in-VM, `allow-effective.json` and
**host.env creds synced into the very VM the agent lives in** (breaking the creds-never-leave-
the-Mac invariant and contradicting the spike's own backstop conclusion), the `INJECT_COMMAND`
minters (github App key material!) running in-VM, a second daemon-supervision path (systemd vs
the Mac supervisor), a broken Network Log / `fy allow` UX (egress.jsonl would land in the VM),
an unsolved per-worktree port story — and it still fixed only `FY_PROXY`, leaving
`GCP_MINTER_URL` pointing at the wrong place. Routing to the Mac fixes every
`host.containers.internal` consumer at once and keeps one proxy code path on one machine.
