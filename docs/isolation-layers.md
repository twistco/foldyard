# Where the boundary lives, per platform

foldyard stacks three boundaries, and **which layer carries the weight differs by host**. This
page pins that down, because the differences are load-bearing and easy to paper over with a
uniform-looking config.

The one sentence that explains the rest: **on macOS the machine layer is a necessity; on Linux it
is a choice.** Containers need a Linux kernel, so a Mac must run a VM to have containers at all —
the isolation is a bonus the VM was going to give you anyway. A Linux host already has the kernel,
so its VM exists *only* to be a boundary, and has to justify its cost on that alone.

## The three layers

```
  ┌─────────────────────────────────────────────────────────────────┐
  │ HOST            credentials · supervisor · minters · allow-store│
  │                 the egress proxy · the operator's editor        │
  └───────────────────────────┬─────────────────────────────────────┘
                              │  ① MACHINE layer  (a VM)
                              │     mounts: repo + worktrees root ONLY
                              │     the in-VM nftables wall
  ┌───────────────────────────┴─────────────────────────────────────┐
  │ VM              rootless podman  ·  libpod socket ──▶ the host   │
  └───────────────────────────┬─────────────────────────────────────┘
                              │  ② CONTAINER layer  (the engine)
  ┌───────────────────────────┴─────────────────────────────────────┐
  │ dev box + the compose stack     ③ optional: microVM per container│
  │ agents run here                    (podman run --runtime krun)   │
  └─────────────────────────────────────────────────────────────────┘
```

① is `[machine].backend` + `[machine].vmtype`. ② is ordinary containers. ③ is a second VM
boundary *underneath* the containers — available only where nested KVM is.

## macOS (arm64) — the machine layer does the work

```
    Apple EL2  (hardware virtualisation, in silicon + xnu)
        │                                   ← IDENTICAL on both paths
        ├── Hypervisor.framework ............ low-level: vCPU + memory only
        │      └── libkrun.dylib  (Rust; VMM + a small virtio set)
        │            └── krunkit            [com.apple.security.hypervisor]
        │                  ├── podman machine --provider libkrun
        │                  └── Lima  vmType: krunkit          ← candidate
        │
        └── Virtualization.framework ........ Apple's own VMM + device model
               └── vfkit / Lima vz driver     [com.apple.security.virtualization]
                     └── Lima  vmType: vz                     ← today's default
```

**The hypervisor is the same either way.** Choosing krunkit over vz swaps the *userspace* VMM and
device model, not the thing enforcing isolation in hardware. That makes the change lower-risk than
"trust libkrun over Apple" suggests — and it also bounds the benefit: the device-model half
shrinks, the hypervisor half is untouched.

Either way, a Mac runs **neither QEMU nor Linux KVM** — the two components Trail of Bits' agent
actually escaped. That is the headline, and it is already true today.

## Linux — the machine layer is optional, and QEMU is the price

```
   (a) BOUNDARY-PRESERVING            (b) MICROVM-PER-CONTAINER
   backend = "lima", vmType: qemu     backend = "native" + --runtime krun

   host ──① QEMU/KVM VM               host ── (no VM)
            └─② containers                     └─② containers, each ③ a
               share the VM kernel                 libkrun microVM with
                                                   ITS OWN kernel
   ✅ repo-only mount                 ❌ engine sees the whole host FS
   ✅ [machine].wall (in-VM nft)      ❌ no VM to put the wall in
   ✅ concurrent per-project VMs      ✅ per-container kernel isolation
   ❌ QEMU device model               ✅ minimal device model
   ⚠  mountType defaults to 9p        n/a
```

**Lima on Linux does not get you libkrun** — Lima registers only `qemu` there; `krunkit` is a
macOS/arm64 binary. So adopting Lima on Linux is a choice for *(a)*, i.e. for the boundary, mount
and wall properties, with QEMU as the knowingly-accepted weak link. It is not a way to make the
stack uniform with the Mac's, and picking it *for* uniformity would be picking the wrong reason.

Two things to pin if *(a)* is chosen: `vmtype` (so the VMM is a decision) and **`mountType:
virtiofs`** — Lima's QEMU driver defaults `mountType` to **9p**, which is both slower on the build
hot path and the more escape-prone of the two.

`(b)` is the shape that actually carries libkrun on Linux, and nested KVM is routine on x86, so it
is *cheaper* there than on a Mac. It trades away the two properties foldyard is most opinionated
about. **Neither option dominates**; they trade different things, which is why this is a documented
choice and not a default.

## WSL2 — the distro is already the boundary

```
   Windows ──[Hyper-V]── WSL2 distro ──②── containers
                             ▲
                             └─ this IS ①, but ONE boundary shared by every
                                project, and not foldyard's to provision
```

Two things that look like WSL2 support are not: Lima *inside* a WSL2 distro needs a custom-built
WSL2 kernel for nested KVM, and Lima's `wsl2` driver runs on Windows, is experimental, and
"doesn't support many of Lima's options". Neither is a foundation.

But neither is needed, because a WSL2 distro *is* a Hyper-V VM. `backend = "native"` there is
therefore not the bare-Linux weak profile. Two caveats belong in the same breath:

- It is **one boundary for all projects**, not per-project, and `[machine].wall` has no VM of its
  own to be provisioned into.
- **`/mnt/c` is automounted by default**, which breaks the repo-only mount outright.
  `/etc/wsl.conf` with `[automount] enabled = false` is a prerequisite, not a tuning knob.

## What `fy verify` proves, per platform

A boundary nothing checks is a boundary asserted on trust. As of 2026-09 the mount-leak assertion
is derived from the real host (`verify._host_paths`) rather than a macOS-only regex, and every
absence-check is gated on a positive control — see
[verify-false-pass.md](./verify-false-pass.md). What still differs:

| claim | macOS | Linux (lima) | WSL2 (native) |
| --- | --- | --- | --- |
| VM boundary exists | ✅ | ✅ | ⚠ shared, not per-project |
| repo-only mount | ✅ | ✅ | ⚠ needs automount off |
| `wall` fail-closed egress | ✅ | ✅ | ❌ no VM to wall |
| microVM device model | ⚠ krunkit only | ❌ (QEMU) | ❌ |

`native` must never advertise the VM-boundary claims. That is not a documentation nicety: it is
the difference between a checked property and a slogan.

## Sources

Measured 2026-09-07 on M3 Max / macOS 26.6.1 / podman 6.0.2 / Lima 2.1.3 — see
[firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md) for the runs behind
the macOS rows, including the krunkit probe.
