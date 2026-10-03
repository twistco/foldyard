# ADR-0031 — A root collector in the guest: Tetragon observes, foldyard attributes

- **Status:** Proposed (2026-10-04). Slice 1 (the guest service you can see) is implemented
  behind `[machine] monitor = "observe"`, default off. Export to the host and attribution are the
  next slice; enforcement is not decided here.
- **Sources:** the eBPF investigation and implementation plan (2026-10-03, not in the repo); the
  slice-0 spike [docs/ebpf-monitoring-spike.md](https://github.com/twistco/foldyard/blob/main/docs/ebpf-monitoring-spike.md) (a real Lima
  `template:podman` guest: Fedora 44, kernel 6.19.10, podman 5.8.7, Tetragon v1.7.1). Related:
  [0009](./0009-monitoring-cooperative-enforcement-locked.md) (monitoring is not enforcement),
  [0022](./0022-host-runs-the-adopted-config.md) (the host runs the adopted config),
  [0023](./0023-no-host-executed-code-from-the-repo-mount.md) (no host code from the mount),
  [0025](./0025-gvisor-machine-posture-and-socket-narrowing.md) (the other host-delivered,
  pinned guest binary), [0028](./0028-no-elevation-on-the-host-operator-applies.md) (no
  elevation on the host).

## Context

foldyard's record of what the box did is the proxy's network log: one row per request, for
traffic that reaches the proxy. It can't say what process made a request, what the box ran, which
files it read, or that something tried a way out the VM firewall refused. The VM kernel can, with
eBPF. But collection is guest-wide, so it needs root in the guest, and until now root in the guest
has been boot-time only: one recorded script narrows sudo and installs the firewall, then exits.

The spike settled the facts the design rests on:

1. The guest kernel has what observation needs (kprobes, fentry, BTF; BPF-LSM active).
2. A release delivered by the host over ssh and re-verified as root works.
3. One invalid policy file stops Tetragon starting at all.
4. Tetragon's admin socket, log and unit are closed to the box's uid, but its health server
   listens on every interface, and the box can reach the VM's own address.
5. **Tetragon's container id is not evidence.** Under podman 5 it is empty for every process in a
   container, and a container's creator can set it to another container's id with one annotation.
   The spike showed one container's activity reported as the box's.

## Decision

**Run Tetragon as a root service in the lima guest, provisioned at boot, observing only. foldyard
owns attribution: Tetragon is the sensor, the way the proxy is for HTTP.**

- **A second recorded boot script, under its own marker** (`# fy-monitor <id>`, beside
  `# fy-provision <id>`). Every byte of the wall script is in its id, and a changed id refuses
  every running VM as stale. Adding the monitor to that script would have done that to every VM
  on the day it shipped. With its own entry, a VM that never turns the monitor on records
  nothing. Turning it off records an `off` rendering that removes the install at boot. Stale
  while running is refused, as for the wall: it applies at boot, as root.
- **The release is pinned and the host delivers it.** foldyard pins Tetragon's version and each
  architecture's sha256 in code. The host downloads the release, checks it, caches it and
  streams it over ssh into an inbox only the VM user can write (the same route as gVisor's runsc).
  The guest has no root after boot, so the boot script installs a root `.path` unit. When a
  delivery lands, that unit copies it into root-owned space and checks **that copy** against the
  hash the boot script carried. The transport is untrusted; the recorded hash is the anchor.
  Nothing is read from the checkout. The policy is packaged, fixed and never configurable
  ([0023](./0023-no-host-executed-code-from-the-repo-mount.md)).
- **Config is replaced whole at every boot.** That covers `/etc/tetragon`, the policy and
  foldyard's own unit (not the release's). The health server is bound to `127.0.0.1` (finding 4).
  gops and metrics are off. `enable-process-ns` is on, for attribution.
- **Attribution keys on the mount-namespace inode, resolved against the OUTERMOST
  `libpod-<64 hex>.scope`** in the VM user's delegated cgroup subtree. podman names that scope
  from an id it generated, and an API client can't choose it. A client can choose the sub-cgroup
  below it and the slices above it, so the walk never reads either. Checked live against
  the forged sub-cgroup, a nested `libpod-<box id>.scope` sub-cgroup, `--pid=container:` and
  `--cgroup-parent`. A `--cgroups=disabled` container stays an unmapped namespace: reported,
  never misattributed. Tetragon's `docker` field is never read.
- **The guest reports; the host reads.** `/run/fy-monitor/{applied,artifact,policy}` is
  world-readable and read over ssh as the VM user. `fy up` delivers the release if the guest is
  waiting for one. `fy doctor` shows the row. A monitor that isn't observing is a **warning,
  never an abort**: it observes, so its failure must not take the box away.
- **Observe only.** No selector carries an action, and a test pins that. Enforcement (LSM hooks
  only: this kernel has no `override_return`) needs its own decision and its own failure
  semantics.

## Consequences

- **This extends "root in the guest is boot-time only".** The rule was that the HOST never gets
  root in the guest and the box's uid never does. Both still hold. What changes is that a root
  process now runs for the VM's lifetime (Tetragon), and so does one root unit that acts on a
  file the VM user writes (the installer, which only hashes it before anything else). That is
  more root attack surface in the guest, and it is the price of guest-wide visibility.
- **It is no stronger than the guest kernel.** A guest-kernel or VM-root compromise can blind
  or forge the collector. Events copied to the host survive that; events after it can't be
  trusted. The plan and the docs say so.
- **The relay comes next.** Tetragon's log is root-only (`0600`), and the host's ssh lands as
  the box's uid, so the export can't be a host-side pull. It will be a root relay, started at
  boot, pushing to a host port outside the band the VM firewall opens for the box, with a secret
  the boot script writes root-only.
- The `--cgroups=disabled` gap and the doubled connection events (the container process, then
  pasta) are known, and recorded in the spike doc.
