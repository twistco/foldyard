# Firecracker: why it does not fit

> **What it was:** a 2026-09-07 research note (cut down 2026-09-11) answering "could foldyard use
> Firecracker?", prompted by Trail of Bits' report of an agent escaping QEMU/KVM but not
> Firecracker. **Status:** settled — no; `config.py` cites it for why `[machine] vmtype` is
> pinned. **Superseded by:** [isolation-layers.md](../isolation-layers.md) for which layer carries
> the boundary on each platform.

Prompt: Trail of Bits, ["VMs won't contain cyber-capable agents"](https://blog.trailofbits.com/2026/08/26/vms-wont-contain-cyber-capable-agents/)
(2026-08-26) — an agent escaped a QEMU/KVM guest three times via 0-days in QEMU and KVM, and
*failed* to escape [Firecracker](https://github.com/firecracker-microvm/firecracker). The answer
is no, for one structural reason and two practical ones.

## No filesystem sharing, by design

Firecracker's devices (`docs/device-api.md`, `1.18.0-dev`) are keyboard, serial, virtio-block,
vhost-user-block, virtio-net, virtio-vsock, virtio-rng, virtio-pmem and virtio-mem. No virtio-fs,
no 9p. That is deliberate: [issue #1180](https://github.com/firecracker-microvm/firecracker/issues/1180)
has been open since 2019, a 9p implementation was rejected on security grounds, and the virtio-fs
PR ([#1351](https://github.com/firecracker-microvm/firecracker/pull/1351)) never landed. The
missing devices are *why* the agent couldn't escape — you don't get one without the other.

The repo mount **is** foldyard's isolation claim (`machine._volumes`: the repo and worktrees root
are all the VM sees, editable live from the host). Firecracker's substitutes — a block image of
the checkout, NFS or virtio-fs-over-vsock from a host daemon, a pmem image — either lose
host-side editing or put back, in a guest-reachable host process, exactly the surface Firecracker
removed. Lima's `reverse-sshfs` from a hypothetical Firecracker driver has the same objection,
and is Lima's slowest mount.

## Two practical blockers

- **Wrong layer, and root in the loop.** Firecracker is a VMM (a socket API plus a kernel and
  rootfs you supply), not a VM manager; a machine backend would mean re-implementing Lima. As a
  per-container hypervisor it comes only via Kata, which needs a devmapper block rootfs — the
  mount problem again. Its only network backend is TAP, which needs `sudo` for every VM: root in
  the everyday dev loop, for a tool whose pitch is a rootless boundary.
- **Linux + `/dev/kvm` only.** No macOS port, and none coming.

## What it would have bought

Firecracker replaces QEMU's device model — where nearly every public VM-escape CVE lives — but
not KVM, which is the same under both; the report's Firecracker runs still hit KVM bugs (hard
locks, no escape). A smaller target, not a different kind. Meanwhile a Mac on Lima's `vz` runs
neither QEMU nor KVM already.

The property foldyard did want — a Firecracker-derived device model *with* virtio-fs, on macOS
and Linux — is [libkrun](https://github.com/libkrun/libkrun): Lima's `krunkit` driver for the VM,
`--runtime krun` for a per-container kernel. Both were measured
([archive/isolation-layers-sessions.md](./isolation-layers-sessions.md#measured-on-macos-2026-09-07),
[GCP](./isolation-layers-sessions.md#measured-on-gcp-2026-09-11)): krunkit stays opt-in, the libkrun
box was deferred on cost, and the second kernel under the box became gVisor instead
([ADR-0025](../adrs/0025-gvisor-machine-posture-and-socket-narrowing.md)). Snapshot/restore,
Firecracker's one unique capability, is not wanted.

## Sources

- Trail of Bits, "VMs won't contain cyber-capable agents" (2026-08-26) —
  <https://blog.trailofbits.com/2026/08/26/vms-wont-contain-cyber-capable-agents/>
- Firecracker `1.18.0-dev` (2026-09-07): `docs/design.md`, `docs/device-api.md`,
  `docs/network-setup.md`; filesystem sharing
  <https://github.com/firecracker-microvm/firecracker/issues/1180>,
  <https://github.com/firecracker-microvm/firecracker/pull/1351>
- Kata + Firecracker's block-device requirement <https://blog.cloudkernels.net/posts/kata-fc-k3s-k8s/>
- Lima v2 external drivers <https://lima-vm.io/docs/dev/drivers/>
- libkrun <https://github.com/libkrun/libkrun>
