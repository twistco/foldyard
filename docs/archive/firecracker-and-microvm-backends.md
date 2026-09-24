# Firecracker: why it does not fit

Research note, 2026-09-07; cut down 2026-09-11 once the question was settled. Prompted by
Trail of Bits, ["VMs won't contain cyber-capable agents"](https://blog.trailofbits.com/2026/08/26/vms-wont-contain-cyber-capable-agents/)
(2026-08-26), which reports an agent escaping a QEMU/KVM guest three times via 0-days in QEMU and
KVM, and *failing* to escape [Firecracker](https://github.com/firecracker-microvm/firecracker).
Could foldyard use Firecracker? **No**, for one structural reason and two practical ones. The
platform-by-platform answer to the post itself — which layer carries the boundary, what was
measured, and what was decided — lives in [isolation-layers.md](../isolation-layers.md).

## No filesystem sharing, by design

Firecracker's device list (`docs/device-api.md`, `1.18.0-dev`) is keyboard, serial, virtio-block,
vhost-user-block, virtio-net, virtio-vsock, virtio-rng, virtio-pmem and virtio-mem. No virtio-fs,
no 9p, and `vhost_user.rs` is block-only. This is not a gap awaiting a patch:
[issue #1180](https://github.com/firecracker-microvm/firecracker/issues/1180) has been open since
2019, the p9 implementation was rejected on security grounds and the virtio-fs PR
([#1351](https://github.com/firecracker-microvm/firecracker/pull/1351)) never landed. Every device
is attack surface, which is *precisely why* the agent could not escape it. You do not get the
escape resistance without the missing device; they are the same fact.

The repo mount **is** foldyard's isolation claim (`machine._volumes`: the repo and the worktrees
root are the only things the VM may see, live-editable from the host). Firecracker's substitutes —
a block image of the checkout, NFS or virtio-fs-over-vsock from a host daemon, a pmem image —
either lose host-side editing or re-add, in a guest-reachable host process, exactly the surface
Firecracker deleted. Lima's `reverse-sshfs` could serve the mount over ssh from a hypothetical
Firecracker driver, at the same objection plus Lima's slowest mount type on the build hot path.

## Two practical blockers

- **Wrong layer, and root in the loop.** Firecracker is a VMM, not a VM manager: a socket API, a
  kernel and a rootfs you supply. Making it a machine backend means re-implementing Lima. As a
  per-container hypervisor it only comes via Kata, which with Firecracker needs a devmapper block
  rootfs — the mount problem one layer down. And its only network backend is TAP, so every VM
  needs `sudo` for the tap, forwarding and NAT: root in the ordinary dev loop, for every project,
  against a tool whose pitch is a rootless boundary.
- **Linux + `/dev/kvm` only.** No macOS port, and none coming — it is a KVM consumer by
  construction. Adopting it would drop every consumer foldyard has.

## What it would and would not have bought

Firecracker replaces the QEMU half of what the post broke — the userspace device model where
nearly every public VM-escape CVE lives — and not the KVM half, which is identical under both. The
post's own Firecracker result is hardlocks through kernel flaws with no escape: the agent still
reached KVM bugs. A microVM is a smaller target, not a different kind of target. Meanwhile a Mac
on Lima's `vz` runs neither QEMU nor KVM, so the headline hardening the post argues for was
already true on foldyard's main platform. On Linux, where the VMM is QEMU, the measured answer is
in isolation-layers.md.

The microVM property foldyard *did* want — a Firecracker-derived device model plus virtio-fs, on
macOS and Linux — is [libkrun](https://github.com/libkrun/libkrun): as Lima's `krunkit` driver
for the machine layer, and as `podman run --runtime krun` for a per-container kernel. Both were
measured ([macOS 2026-09-07](../isolation-layers.md#measured-on-macos-2026-09-07),
[GCP 2026-09-11](../isolation-layers.md#measured-on-gcp-2026-09-11)); the per-container layer is
deferred on its cost, krunkit stays opt-in, and snapshot/restore — Firecracker's one unique
capability — is not wanted.

## Sources

- Trail of Bits, "VMs won't contain cyber-capable agents" —
  <https://blog.trailofbits.com/2026/08/26/vms-wont-contain-cyber-capable-agents/> (2026-08-26)
- Firecracker `1.18.0-dev` (main, 2026-09-07): `docs/design.md`, `docs/device-api.md`,
  `docs/network-setup.md`, `src/vmm/src/devices/virtio/`; filesystem sharing
  <https://github.com/firecracker-microvm/firecracker/issues/1180>,
  <https://github.com/firecracker-microvm/firecracker/pull/1351>
- Kata + Firecracker's block-device requirement <https://blog.cloudkernels.net/posts/kata-fc-k3s-k8s/>
- Lima v2 external drivers <https://lima-vm.io/docs/dev/drivers/> (`pkg/hostagent/mount.go` for
  reverse-sshfs being driver-agnostic)
- libkrun <https://github.com/libkrun/libkrun>
