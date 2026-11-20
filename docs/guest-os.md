# The guest OS — why Fedora, what a VM costs, and moving to Fedora 45 (2026-11-20)

Each project VM is Fedora because foldyard creates it, and the base VM it clones them from, from
Lima's `template:podman`. Could a smaller distro cut each VM's overhead or its attack surface? What
changes with Fedora 45 and podman 6, its pasta port forwarder included, and can a project's VM move
to it without losing what it holds? This is the record of one round of measurements, fixes and tests
on an Apple-silicon Mac with `vz`.

**In short:** a leaner distro would save a few tens of MiB per VM against gigabytes of page cache,
and could make the VM less safe, not more. Fedora 45 broke two things in foldyard, the guest monitor
and build attribution; both are fixed here, and the whole host tier passes on Fedora 45 and still on
44\. Podman 6 can forward a compose stack's ports with pasta, saving ~28 MiB per port-publishing
container; the tier passes with it too, but the option is experimental and changing it under running
containers jams ports. A clone of a real project VM upgraded from 44 to 45 in place in two and a
half minutes, hands-off, with every image, volume and container kept.

## Why Fedora

Nobody chose it as such: it is what Lima's podman template runs, and no ADR weighs one distro
against another. It suits podman well, though. Fedora gets each podman release first, since podman's
main developers work at Red Hat; rootless podman under a systemd user service works out of the box;
and SELinux runs in enforcing mode, giving every container a label of its own, which confines a
container breakout further. foldyard leans on the rest of the platform too: its VM firewall and
wall-denies logger are systemd units with nftables rules, its socket filter (ADR-0025) runs under
the guest's `python3`, and its base VM ([base-vm.md](./base-vm.md)) counts as stale when Lima's
template moves to a new Fedora release.

ADR-0025 tried podman 6 on a Fedora 45 nightly (QEMU, Lima 2.2.0) in September 2026: SELinux kept
sshd from the guest agent's socket, Lima reported the VM degraded, and the ADR left the move until
Fedora 45's release. Fedora 45 came out at the start of November 2026 with podman 6, and Lima
2.3.0's `template:podman` is Fedora 45 (2.2.x's is still 44), so a new VM follows when Lima is
upgraded.

## What a VM costs, and what a distro could save

A project VM (Fedora 44, 7.7 GiB, its stack running) at rest:

| where | memory |
| -- | -- |
| every process's own memory (`AnonPages`) | 198 MiB |
| of which Lima's guest agent (any distro) | 36 MiB |
| of which Fedora's own services: NetworkManager, logind, udevd, resolved, journald, dbus… | ~55 MiB |
| of which podman and the user's services | ~80 MiB |
| file cache (`Cached`) | 5.6 GiB |

A distro with fewer services (Alpine with OpenRC, say) might save a few tens of MiB of the ~55,
unmeasured here. The file cache is about a hundred times that, and no distro changes it: on `vz`,
memory the guest has touched stays allocated on the host until the VM restarts, so it grows to
whatever the VM is given. The levers that matter are `[machine] memory_mib`, stopping idle VMs and
restarting busy ones; then podman's own processes (below).

## Would a leaner guest be safer?

Less than it seems. In foldyard's model the attacker starts in a rootless container, and the way out
runs through the VM's kernel, the container runtime (crun, podman) and the hypervisor, the same
whatever the distro. The guest's services matter only after an escape, for raising privileges inside
the VM, and foldyard already plans for that: the to-box share is read-only outside the guest
([ADR-0033](./adrs/0033-a-to-box-and-a-from-box-beside-the-checkout.md)), and the host firewall
holds even if the guest's is removed ([networking.md](./networking.md)). A smaller distro could lose
what does help:

- **SELinux:** Alpine has none by default; Debian's AppArmor confines containers less tightly than
  SELinux's per-container labels.
- **Patching:** fewer packages matters less than keeping them current, and these VMs don't update
  themselves (the base VM warns past 30 days).
- **foldyard's own needs:** systemd, nftables and `python3`. A guest without systemd means rewriting
  the VM provisioning.

## Fedora 45 beside 44

Two throwaway VMs from `template:podman` on Lima 2.1.3, `vz`, 4 CPUs, 4 GiB, nothing mounted: one on
Fedora 44 as shipped, one with the image set to `Fedora-Cloud-Base-Generic-45-1.4.aarch64.qcow2`.

|  | Fedora 44 | Fedora 45 |
| -- | -- | -- |
| kernel | 6.19.10 | 7.2.11 |
| podman, netavark, crun | 5.8.9, 1.17.2, 1.27 | 6.2.0, 2.1.0, 1.29.1 |
| passt (pasta) | 0^20260120 | 0^20260728, with `pesto` |
| boot | not degraded | not degraded: the nightly's SELinux denial is gone |
| SELinux | enforcing, 0 denials | enforcing, 0 denials |
| at rest, one container publishing a port (`AnonPages`) | 185 MiB | 181 MiB |
| services running, packages, root filesystem | 18, 421, 815 MiB | 18 (no auditd), 443, 900 MiB |

The libpod API answers podman 5's paths with the same fields on 6.2, apart from `PerCPU` in
container stats; the forwarded socket and Lima's port forwarding work as before.

## The pasta port forwarder

A plain rootless `podman run` gets a pasta network of its own (~12 MiB), and that pasta holds its
published ports: no rootlessport. Rootlessport comes in on bridge networks, which is what compose
makes for each project: one rootlessport pair per container that publishes ports, around one shared
pasta for the network. Podman 6 adds `rootless_port_forwarder = "pasta"` (`[network]` in
`containers.conf`), which lets that shared pasta hold the ports, set up by `pesto`, instead. It is
not the default in any podman 6 release, and the man page calls it "experimental and its behavior is
subject to change"; nothing published gives a reason beyond that.

Measured on Fedora 45 with a compose-style network (`podman network create`):

- **Memory:** rootlessport costs 27.8 MiB PSS per port-publishing container (16.0 MiB for
  `rootlessport`, 11.8 MiB for `rootlessport-child`; 128 + 94 MiB for 8). With pasta, nothing per
  container: the one pasta, ~11.5 MiB, holds every port.
- **What works:** a port from the guest and from the host through Lima's forward; UDP; a port bound
  to `127.0.0.1` stays on loopback, its `HostIp` kept in inspect; the same port on `127.0.0.1` and
  `127.0.0.2` from two containers, each reaching its own (the limitation the PR describes is gone
  with this passt); stop, start, restart, a freed port reused, `podman network reload`, network
  disconnect and connect, containers by name, the internet; SELinux, 0 denials.
- **IPv6:** a port is bound on `0.0.0.0` only, where rootlessport binds `*`, so `[::1]` in the guest
  gets no answer. The blog says the container network needs to be dual-stack and the service to bind
  IPv6.
- **The client's address** is the forwarder's selling point, but under Lima there's none to keep:
  Lima's forward hands every connection in from inside the guest. A request from the host reached
  nginx as `192.168.5.2` (pasta's stand-in for the guest's loopback); with rootlessport it's
  `10.89.0.19`.
- **Changing it under running containers jams ports.** Switched back to rootlessport with containers
  running that pasta forwards: stopping or removing one leaves pasta holding its port, and starting
  it again, or a new container on the port, fails with
  `rootlessport listen tcp :8046: bind: address already in use`. `podman stop -a`, then starting
  them again, clears it, since the network's pasta exits with the last container. Going to pasta
  wants the same: stop every container, change it, start them.

## What Fedora 45 broke, and the fixes

Run first on Fedora 45 as foldyard stood (Lima 2.3.0 from Homebrew, so `template:podman` and every
VM the tier creates are Fedora 45), the host tier failed in two places, both reproducing on a rerun
of their module:

- **The guest monitor never started** (`test_monitor_e2e`, all 8 at setup). Tetragon v1.7.1 exited
  255 at every start: `load vmlinux: parsing .BTF header: header contains non-zero trailer`, a
  header field of kernel 7.2's BTF its loader (cilium/ebpf) doesn't know. **Fix:** Tetragon v1.8.0
  (`monitor.TETRAGON_VERSION`, its sha256s from the release's own files), which loads and attaches
  the packaged policy on 7.2 (the policy carries no action, so v1.8.0's removal of the deprecated
  ones doesn't touch it). That exposed a second break: v1.8.0's `tetra tracingpolicy list` puts a
  `DOMAIN` column between `NAME` and `STATE`, so the guest's report never saw the policy as loaded
  and `fy up` waited its full ten minutes before warning. **Fix:** `fy-monitor.sh`'s
  `policy_enabled`, pure bash, reads either layout; the unit suite runs it against both tables, as
  each release's source prints them.
- **A build step's proxy request was refused under `[machine] attribution = "record"`**
  (`test_forwarder_e2e::test_a_build_step_is_stamped_as_a_build`). On podman 5.8.9 a build step runs
  in the VM's own network namespace (`net:[4026531833]`, the VM's), so the forwarder finds its
  socket in the `buildah-buildah<N>` cgroup and stamps it `build`. On podman 6.2.0 each step gets a
  namespace of its own (`net:[4026532499]`) served by a pasta of its own, in a `podman-<pid>.scope`
  (`[containers] netns = "host"` would bring 5.x's behaviour back, but it is `podman run`'s default
  too, which attribution needs on the bridge). The forwarder refuses a pasta helper's connections by
  design, so the step saw `Connecting to 192.168.5.2:42400` then `wget: error getting response`.
  **Fix:** a helper is a build's when every walled process in the network namespace it holds open
  (its fd links, not its argv, which `--network pasta:<opts>` can add to) is in a build step's
  cgroup (`fwd.Cgroups.served`); any other helper is still refused, including a container's own
  pasta and a container joined to a build step's namespace. The `build` stamp grants nothing:
  build-scoped grants come from the build's secret in its proxy URL (`buildgate`), not the stamp.
  [ADR-0034](./adrs/0034-the-proxy-knows-which-container-asked.md) carries the amendment.

## The host tier, after the fixes

CI's two shards one after the other, as CI lists them, `MACHINE_CPUS=2`, no session base VM (every
VM from the template). For the pasta column, a temporary `provision:` script in Lima's
`~/.lima/_config/override.yaml` wrote `rootless_port_forwarder = "pasta"` at every boot of a VM
whose hostname is `lima-foldyard-example` (and did nothing on any other), so the VMs the tier
recreates got it before their first container. For Fedora 44, CI's guest, the same file set
`images:` to Fedora 44's cloud image, which only a VM being created reads. A sampler read the VM
every 20 s: each phase ran on the guest it names, and in the pasta phase every published port was
held by pasta, never rootlessport.

| shard | Fedora 45, before the fixes | Fedora 45 | Fedora 45, pasta forwarder | Fedora 44 |
| -- | -- | -- | -- | -- |
| 1: machine, verify, mount visibility, registry logins, guest compose, forwarder, e2e, probes, monitor | 59 passed, 1 failed, 8 errors | 68 passed | 68 passed | 68 passed |
| 2: base VM, reclaim, host daemons, mounts, box, worktree, wall | 23 passed | 23 passed | 23 passed | 23 passed |

Every column also skips 14, as on any Mac: `test_wall_e2e`'s 12 need a Linux host for the host
firewall, and `test_e2e`'s 2 need `CONTAINER_HOST`, which CI exports. The template's own
`dnf install podman` lifts the cloud image's podman (6.1.3 on Fedora 45) to the current one (6.2.0)
before any test. CI moves to Lima 2.3.0 with these fixes, so it runs Fedora 45 from here on.

## Moving a project's VM to Fedora 45

A VM keeps the Fedora it was created with: a newer Lima changes what NEW VMs get, and
`fy machine recreate` would throw away the VM's podman store (images, the stack's volumes, agent
logins). Two ways keep it, each tried on a copy-on-write `limactl clone` of a real project VM
(Fedora 44, podman 5.8.9: 48 volumes holding ~40 GB, 98 images, 17 containers), with no mounts and
every TCP port forward ignored, so nothing in it could touch the project's checkout or its ports.
Lima 2.3.0's clone keeps the source's pinned `ssh.localPort`, which the running original holds; the
clones needed it set back to `0`.

**In place: 153 s, hands-off.** Root in the guest exists only at boot, so the upgrade is a one-off
recorded `provision: mode: system` script under its own marker, which the host removes again
([guest-os-kit/upgrade-inplace.sh](./guest-os-kit/upgrade-inplace.sh), the prototype that ran it):

1. snapshot what must survive (every volume, image and container, and a 64 MiB random file in a
   sentinel volume, by sha256); stop the VM; record the boot script (3 s);
2. boot: the script stops the VM user's containers cleanly (`podman stop -a -t 30`, so a database
   gets its shutdown, not a power cut), masks `user@<uid>.service` for that boot (a login, Lima's
   readiness checks or a client of the podman socket would otherwise start podman mid-transaction),
   runs `dnf -y --releasever=45 distro-sync --allowerasing`, and unmasks it (~2 min, the download
   included);
3. stop, drop the script, boot the new kernel (23 s);
4. `podman system migrate`, and compare (2 s).

Fedora 44 to 45, kernel 6.19 to 7.3.4, podman 5.8.9 to 6.2.0: every volume, image and container
still there and the sentinel's sha256 unchanged. foldyard's own boot provisioning came back on the
new release by itself (the wall with the project's port bands and its `firewall_allow` opening; sudo
narrowed again). The project's Postgres image, started on its data volume, came up without recovery
and read every database, its tables and rows. Both podman 6 removals that could strand a store were
absent, as on every Lima VM foldyard makes: its database is SQLite, not BoltDB, and its containers
are on netavark bridges, not slirp4netns.

**A new VM, then carry the data: 211 s, and then the first `fy up`.** The way the project's own
Podman-machine-to-Lima migration script works, made as cheap as it gets: by that script's rules (the
agent homes, the stack's data volumes but not its installs and caches, the box image and the pulled
base images), streamed VM to VM over two `limactl shell` pipes, nothing written on the host.
Choosing what to carry took 5 s (11 volumes, 2.0 GB, and 27 images); a new VM from Lima's template,
its image cached, 24 s; the images 163 s; the volumes 19 s; the sentinel's sha256 matched. That is
before the first `fy up`, untimed here: it has to rebuild what wasn't carried, ~70 of the stack's
own images without their build cache and the other 36 volumes (~38 GB of node_modules, venvs,
`.next` and caches), through the proxy. While it runs, the old VM and the new one are both on the
host's disk: the 18 GB carried took a Mac with 42 GiB free down to 24 GiB.

### `fy machine upgrade`

The prototype became a verb (`src/foldyard/upgrade.py`, its boot script
`assets/upgrade/guest-upgrade.sh`). Two things changed on the way:

- **The target is the Fedora the installed Lima's own podman template uses, and never past it.**
  That is the check that Lima suits the release, with no table to keep: Lima's guest agent and boot
  setup are made for what its template ships (ADR-0025 saw a newer guest boot degraded under an
  older Lima), and the limit moves with Lima. Fedora upgrades at most two releases at a time, so a
  guest further behind is refused with the step to take first.
- **The baseline is taken once the containers have stopped.** The first live run took it while the
  stack ran and reported a changed volume: Postgres's last checkpoint, written as it stopped, had
  grown its anonymous volume from 45.8 to 45.9 MiB. Compared at rest, the store has to match
  exactly: every volume at its byte size, every image, every container.

It refuses rather than guesses, and never leaves a VM worse than it found it:

- **Before anything changes**: a snapshot that didn't arrive whole (a failed ssh reads as empty
  output, which would match anything; every volume's size must be read), containers that didn't all
  stop, too little room, a target past Lima's template, or `[machine] host_firewall` (the upgrade
  boot needs the Fedora mirrors, which the host wall refuses, and booting outside the wall's slice
  would leave the VM unwalled for the whole transaction; not supported yet).
- **Never stopped mid-transaction**: when `limactl start` gives up on the upgrade boot, the guest's
  own record is read until it says done or failed; one still running after another hour is left to
  finish, with its log's path. The record says done only once dnf succeeded, never from the release
  number, which a failed transaction can already have moved.
- **The script never runs twice by accident**: a VM that won't stop after the upgrade boot, or a
  script that can't be removed, is left stopped with what to do, not booted again.
- **Done means checked**: the store at rest matches, the guest reports the new release, and the
  containers that ran before run again; otherwise it says which and exits non-zero.

Live, on foldyard's example project (Postgres and an API, both running) in a Fedora 44 VM foldyard
made: `fy machine upgrade --yes` took it to Fedora 45 (kernel 7.3.4, podman 6.2.0) in 257 s,
reported every volume, image and container kept, and started both containers again. A 64 MiB
sentinel's sha256 was unchanged, the boot script was gone from `lima.yaml`, and sudo was narrowed
again. The time is mostly the mirror's: the clone above took 153 s with the same steps.

## What this suggests

- **Fedora 45 is ready for foldyard with these fixes.** Lima, the provisioning, the wall, the box,
  worktrees, mounts, the base VM, guest compose, the forwarder and the monitor all pass on it, and
  still on 44.
- **Move existing VMs in place.** It was the quicker of the two before the other had even started
  its rebuild, it keeps everything (the build cache and installs included), and it needs no room on
  the host. Carrying into a new VM is for when the disk itself is the problem, or the distro
  changes. The pattern is the one foldyard already uses for root: a recorded boot script under its
  own marker, then a restart: `fy machine upgrade`. CI moves to Lima 2.3.0 with these fixes, so a
  consumer upgrading Lima gets Fedora 45 in new VMs and the verb for the ones they have.
- **The pasta port forwarder costs nothing in the tier** and saves ~28 MiB per port-publishing
  container. It wants setting when a VM is provisioned (or in that same upgrade, when the containers
  are stopped anyway), never changed under a running stack, and checking against IPv6: a host
  browser going to `localhost` through Lima's forward may try `::1` first, untried here. Worth
  making the default once podman stops calling it experimental.
- **The distro stays.** Nothing here argues for leaving Fedora; the savings are in a VM's memory
  size and in podman's own processes.

Sources: [Fedora Changes/Podman6](https://fedoraproject.org/wiki/Changes/Podman6),
[podman PR #28478](https://github.com/podman-container-tools/podman/pull/28478) (the option and its
limitations), [the Podman blog on it](https://blog.podman.io/?p=21954) (the upgrade caveat, IPv6),
[podman-rootless(7)](https://www.mankier.com/7/podman-rootless),
[Lima 2.3.0's release notes](https://github.com/lima-vm/lima/releases/tag/v2.3.0) (Fedora 45
templates).
