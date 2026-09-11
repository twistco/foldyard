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

**Neither path jails the VMM.** libkrun's README says the guest and the VMM "pertain to the same
security context" and that isolating them is the host OS's job (namespaces on Linux). That is not a
libkrun-specific weakness: Virtualization.framework runs the VM inside the calling process too, so
Lima's vz host agent is in exactly the same position. What Firecracker adds — and neither of these
has — is a jailer around the VMM process. The consequence on macOS is worth stating without
softening: a VMM escape lands in an *unconfined process running as the operator*, with the home
directory, the login keychain, `~/.foldyard` and the whole network in reach. **The in-VM wall does
not survive a VMM escape** — it bounds the guest kernel, not the host process that hosts it.

### macOS is not settled either

The headline above is true, but "already true today" should not read as "done". Three things are
open on the Mac, as of 2026-09-11:

- **There is no jailer, and no obvious way to add one.** Lima's vz host agent is an ordinary
  process; krunkit is an ordinary process. macOS has no namespaces; the only candidate is App
  Sandbox / `sandbox-exec`, which is deprecated and which neither Lima nor krunkit applies. So the
  machine-layer VMM escape lands on the operator's credentials under either vmType. The Linux
  stack *(c)* below has an answer to this (crun); the Mac does not yet.
- **The box cannot have the microVM layer on the Mac.** The `③` layer — `--runtime krun` for the
  dev box — needs `/dev/kvm` inside the machine VM, which Apple silicon only offers with
  `nestedVirtualization: true` on **M3+ / macOS 15+**. **M1/M2 consumers must be supported**
  (decided 2026-09-11), so `③` is a Linux-only layer, and the maintainer's M3 nested-virt capability
  is a *test rig*, never a product assumption. The Mac's structure is therefore one kernel layer
  shorter than Linux *(c)*: host ← VMM ← guest kernel ← container, versus host ← QEMU ← guest
  kernel ← crun jail ← libkrun ← box kernel ← box. On the Mac a box kernel escape lands in the
  machine VM's guest, and the machine VM is the whole boundary. What the Mac *can* still take from
  the Linux work is the socket narrowing, which is platform-independent and closes the design hole
  on both.
- **krunkit stays opt-in.** It swaps the device model for a smaller one but is upstream-experimental
  on a Cellar path that moves per upgrade; libkrun 2.0 is an API break for krunkit's maintainers,
  not for Lima, but it is churn under a security layer. The probe passed; a week of builds has not
  been run.

So the Mac's honest state is: better VMM than the post's subject, unjailed like everything else,
and one layer short of the Linux design.

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

**On Linux the VMM *is* jailed.** crun's krun handler builds the container first — namespaces,
cgroups, seccomp, the rootfs — and only then boots libkrun inside it. So the "guest and VMM share a
security context" caution above collapses to "a guest escape lands in a container", which is the
jailer Firecracker ships and macOS lacks. `(b)` is a stronger boundary than the generic libkrun
caution suggests.

### The two ❌s are one fact: the engine socket

Native does not lose the repo-only mount and the wall because there is no VM. It loses them because
**the dev box holds the engine socket**, and a libpod socket can bind-mount anything its uid can
read and reach anything its host can reach. The VM never *created* those properties; it bounded
what the socket could get at. Two consequences:

- The socket is a hole punched through *every* boundary by design — whatever it can do, the agent
  can do with no exploit at all. Narrowing it (a filtering API proxy, or an engine running as a
  dedicated uid that host-side nftables can `skuid`-wall) is therefore not an alternative to a VM
  layer; it is the other half. A microVM removes the *exploit* vector (the shared kernel); socket
  narrowing removes the *design* vector (the API). Neither substitutes for the other.
- **Nothing about the socket is fixed.** The box uses it to drive the compose stack from inside,
  which is a convenience the design can revisit, not a contract.

### (c) Stack them — the direction chosen 2026-09-11

Nested KVM is on by default for `kvm_intel`/`kvm_amd`, and Lima's QEMU driver passes the host CPU
through, so `(a)` and `③` compose on Linux:

```
   host ──① Lima/QEMU VM  (repo-only mount, the wall, concurrency)
            └─② crun container  (namespaces · cgroups · seccomp = the jailer)
               └─③ libkrun microVM  ← the box runs here, on its own kernel
```

An agent in the box must escape libkrun into a jailed container, then the Lima guest kernel, before
it can so much as *see* the QEMU device model. QEMU is still the outer weak link, but it is no
longer reachable from the agent's shell. The cost is nested-VM overhead on every box syscall,
accepted deliberately: stronger isolation at reasonable performance cost is the principle.

The blocker to solve first is the socket again: it is `AF_UNIX` over a bind mount, and virtio-fs
does not carry unix sockets across the microVM boundary (crun's manual: "sharing content with
processes and other containers outside of the krun VM is more difficult"). The box under `③`
loses the engine unless the socket is re-plumbed — over TCP inside the Lima guest's own network,
or vsock — and re-plumbing it is the natural moment to narrow it. Unverified as of 2026-09-11; see
[What can be measured where](#what-can-be-measured-where).

## WSL2 — a Linux host whose Hyper-V boundary protects the wrong asset

```
   Windows ──[Hyper-V]── WSL2 distro ────────②── containers
                          ▲  supervisor · minters · allow-store
                          │  ALL live in here, in the same home as the engine
                          └─ Hyper-V separates Windows from the distro.
                             It puts NOTHING between the agent and the credentials.
```

An earlier draft of this page called the distro "already the boundary". That is comfort for the
wrong asset. The supervisor runs inside the distro — it is Linux Python — so the credentials and the
engine share one kernel and one uid there, exactly as on bare Linux. **For foldyard's threat model,
WSL2 + `native` is bare-Linux `native`.** The Hyper-V VM is real, but it protects Windows.

The good news is that the earlier draft was also wrong about the remedy. It said Lima inside a WSL2
distro needs a custom-built kernel for nested KVM. That was true in 2021 and is stale: the stock
WSL2 kernel (6.x) builds KVM as modules, `nestedVirtualization` defaults on for Windows 11 on x86,
and `/dev/kvm` is present after a `wsl --shutdown`. So **WSL2's menu is the Linux menu** — `(a)`,
`(b)` and `(c)` above are all candidates inside the distro. Two hard limits: Windows 10 silently
overrides the setting, and Windows-on-ARM boots the distro at EL1, so KVM can never work there.
Lima's *Windows-side* `wsl2` driver remains what it was — experimental, "doesn't support many of
Lima's options" — and is not the route.

Still true, and still prerequisites rather than tuning:

- **`/mnt/c` is automounted by default**, which breaks the repo-only mount outright.
  `/etc/wsl.conf` with `[automount] enabled = false` comes first.
- Nothing above is measured. `/dev/kvm` on a stock Windows 11 x86 machine is a two-minute,
  read-only check; Lima+QEMU inside the distro is not yet a probe anyone has run.

## What `fy verify` proves, per platform

A boundary nothing checks is a boundary asserted on trust. As of 2026-09 the mount-leak assertion
is derived from the real host (`verify._host_paths`) rather than a macOS-only regex, and every
absence-check is gated on a positive control — see
[verify-false-pass.md](./verify-false-pass.md). What still differs:

| claim | macOS | Linux / WSL2 (lima) | Linux / WSL2 (native) |
| --- | --- | --- | --- |
| VM boundary exists | ✅ | ✅ | ❌ (WSL2's Hyper-V does not count: the credentials are inside it) |
| repo-only mount | ✅ | ✅ | ❌ engine sees the host FS; WSL2 also needs automount off |
| `wall` fail-closed egress | ✅ | ✅ | ❌ no VM to wall |
| microVM device model | ⚠ krunkit only | ❌ (QEMU) — `(c)` adds one *under* it | ⚠ `(b)` only |
| VMM jailed | ❌ unconfined operator process | ✅ crun, for `③` only | ✅ crun, for `(b)` |

`native` must never advertise the VM-boundary claims. That is not a documentation nicety: it is
the difference between a checked property and a slogan.

## What can be measured where

Everything off the macOS column is reasoned from source as of 2026-09-11. What the hardware to
hand can and cannot settle:

| rig | can verify | cannot |
| --- | --- | --- |
| M3 + Lima/vz **or** krunkit, `nestedVirtualization: true` (a Fedora guest with `/dev/kvm`) — **test rig only**, M1/M2 consumers have no `/dev/kvm` | krun mechanics and the socket transport on arm64 (the Linux `③` code path, cheaply); `(a)` Lima+QEMU inside a Linux-host stand-in, **or** `(b)` krun inside one — two levels, arm64 | `(c)` on Linux: the third level (arm64 nested KVM is experimental). And nothing it shows about `③` is a Mac product claim |
| a household NAS (TrueNAS 25.04, i5-6500T) | nothing until a BIOS visit: VT-x is off since a 2026-09-05 CMOS loss, and it has ~3 GiB free with no swap | — |
| GCP `--enable-nested-virtualization` (Fedora; Windows Server for WSL2) | `(a)`, `(b)` at two levels; `(c)` at three; WSL2 baseline | Windows 11-client-specific claims; and it is one level deeper than a real laptop, so overhead numbers are upper bounds |
| M3 + UTM, Windows 11 ARM | WSL2 baseline: `/mnt/c`, `wsl.conf`, the supervisor-in-distro finding | `/dev/kvm` in WSL2 — Windows-on-ARM boots the distro at EL1, so it is structurally absent, not just untested |
| M3 + UTM, Windows 11 x86 (emulated) | nothing useful — TCG does not emulate VT-x for Hyper-V | everything |
| any x86 Windows 11 machine | `wsl --shutdown; wsl; ls -l /dev/kvm` — read-only, two minutes | — |
| a Fedora VM on an x86 Linux host with nested virt on (e.g. a NAS hypervisor) | **`(c)` end to end** — x86 nested KVM is mature | — |

So an x86 Linux VM is the only rig that reaches the chosen Linux design, and the Mac's product
path is what M1/M2 consumers already run: the machine VM as the boundary, with no `③`. What the
Mac still owes is the socket narrowing and a decision on krunkit; neither has been measured.

## Sources

Measured 2026-09-07 on M3 Max / macOS 26.6.1 / podman 6.0.2 / Lima 2.1.3 — see
[firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md) for the runs behind
the macOS rows, including the krunkit probe.

Read 2026-09-11: libkrun README (security model, 2.0 status)
<https://github.com/libkrun/libkrun> (moved out of the `containers` org; old URLs redirect) ·
crun's krun handler <https://github.com/containers/crun/blob/main/krun.1.md> · WSL2 stock-kernel
KVM: <https://www.boxofcables.dev/build-a-custom-wsl-2-kernel-in-2026/>,
<https://www.boxofcables.dev/accelerated-kvm-guests-on-wsl-2/>, Windows 10 override
<https://github.com/microsoft/WSL/issues/40735> · the prompt for the re-read:
<https://rywalker.com/research/libkrun>.
