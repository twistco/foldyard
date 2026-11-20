# The guest OS — why Fedora, what a VM costs, and Fedora 45 (2026-11-20)

Each project VM is Fedora because foldyard creates it, and the base VM it clones them from, from
Lima's `template:podman`. Could a smaller distro cut each VM's overhead or its attack surface, and
what changes with Fedora 45 and podman 6, its pasta port forwarder included? This is the record of
one round of measurements and tests on an Apple-silicon Mac with `vz`; foldyard is unchanged.

**In short:** a leaner distro would save a few tens of MiB per VM against gigabytes of page cache,
and could make the VM less safe, not more. Its podman 6 can forward a compose stack's ports with
pasta, saving ~28 MiB per port-publishing container, but the option is experimental and changing it
under running containers jams ports.

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

## foldyard's host tier on Fedora 45

Lima 2.3.0 from Homebrew on the Mac, so `template:podman` and every VM the tier creates are Fedora
45 (podman 6.2.0, kernel 7.2.11); CI's two shards run one after the other, as CI lists them, with
`MACHINE_CPUS=2` and no session base VM (every VM from the template). CI pins Lima 2.2.0, so Fedora
44, and passes.

| shard | rootlessport (the default) | pasta port forwarder |
| -- | -- | -- |
| 1: machine, verify, mount visibility, registry logins, guest compose, forwarder, e2e, probes, monitor | 59 passed, **1 failed, 8 errors**, 2 skipped | the same: 59 passed, 1 failed, 8 errors, 2 skipped |
| 2: base VM, reclaim, host daemons, mounts, box, worktree, wall | 23 passed, 12 skipped | 23 passed, 12 skipped |

The skips are expected on a Mac: `test_wall_e2e` needs a Linux host for the host firewall, and
`test_e2e`'s two need `CONTAINER_HOST`, which CI exports. For the pasta column, a temporary
`provision:` script in Lima's `~/.lima/_config/override.yaml` wrote
`rootless_port_forwarder = "pasta"` at every boot of a VM whose hostname is `lima-foldyard-example`
(and did nothing on any other), so the VMs the tier recreates got it before their first container. A
sampler read the VM every 20 s: in each of the 16 samples with a published port, the setting was
there and pasta held every port, never rootlessport. Both failures reproduce on a rerun of their
module, and both are Fedora 45's, not the forwarder setting's:

- **The guest monitor doesn't start** (`test_monitor_e2e`, all 8 at setup:
  `⚠ the guest monitor is NOT observing: tetragon.service is activating`). Tetragon v1.7.1 exits 255
  at every start: `load vmlinux: parsing .BTF header: header contains non-zero trailer`. Kernel
  7.2's BTF has a header field v1.7.1's loader (cilium/ebpf) doesn't know. Tetragon v1.8.0 (30
  October 2026) is the next release to try; `monitor.TETRAGON_VERSION` pins v1.7.1. `fy up` carries
  on as designed (the monitor only observes) and `fy doctor` keeps its row red.
- **A build step's request is refused under `[machine] attribution = "record"`**
  (`test_forwarder_e2e::test_a_build_step_is_stamped_as_a_build`: no egress row). On podman 5.8.9 a
  build step runs in the VM's own network namespace (`net:[4026531833]`, the VM's), so the forwarder
  finds its socket in the `buildah-buildah<N>` cgroup and stamps it `build`. On podman 6.2.0 each
  step gets a namespace of its own (`net:[4026532499]`) with a pasta of its own, in a
  `podman-<pid>.scope`, and the forwarder refuses a pasta helper's connections by design ("nothing
  here can say whose"). The step sees `Connecting to 192.168.5.2:42400` then
  `wget: error getting response`, and the build itself passes, since the test's step ignores the
  failure. Without attribution the wall passes builds as before (`test_box_e2e`, `fy up`'s builds).
  Ways out, untried: `--network host` on the builds foldyard drives, which brings back 5.x's
  behaviour (a box's own builds aside), or teaching the forwarder to tell a build step's pasta from
  a container's.

## What this suggests

- **Fedora 45 is close, not there.** Lima, the VM provisioning, the wall, the box, worktrees,
  mounts, the base VM and guest compose all pass on it unchanged. Before a consumer's VM is created
  on it (any Lima 2.3.0 on their computer does that), the monitor needs a Tetragon that reads kernel
  7.2's BTF, and `attribution = "record"` needs builds that the forwarder can attribute. CI pins
  Lima 2.2.0, so it stays on Fedora 44 until `LIMA_VERSION` is bumped; bumping it is how these two
  would show there.
- **The pasta port forwarder costs nothing in the tier** and saves ~28 MiB per port-publishing
  container. It would be a provisioning-time switch (the VM user's `containers.conf`, written before
  any container starts, never changed under a running stack), and it wants checking against IPv6: a
  host browser going to `localhost` through Lima's forward may try `::1` first, untried here. Worth
  making the default once podman stops calling it experimental.
- **The distro stays.** Nothing here argues for leaving Fedora; the savings are in the VM's memory
  size and in podman's own processes.

Sources: [Fedora Changes/Podman6](https://fedoraproject.org/wiki/Changes/Podman6),
[podman PR #28478](https://github.com/podman-container-tools/podman/pull/28478) (the option and its
limitations), [the Podman blog on it](https://blog.podman.io/?p=21954) (the upgrade caveat, IPv6),
[podman-rootless(7)](https://www.mankier.com/7/podman-rootless),
[Lima 2.3.0's release notes](https://github.com/lima-vm/lima/releases/tag/v2.3.0) (Fedora 45
templates).
