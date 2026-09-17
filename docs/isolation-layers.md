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
  │ dev box + the compose stack     ③ optional: gVisor under the box │
  │ agents run here                    ([machine].runtime = "gvisor")│
  └─────────────────────────────────────────────────────────────────┘
```

① is `[machine].backend` + `[machine].vmtype`. ② is ordinary containers. ③ is a second kernel
boundary *underneath* the containers: the box runs on gVisor's userspace kernel, so a container
→ kernel exploit has to beat the Sentry before it reaches the VM kernel that holds the socket,
the stack and the mount. No KVM needed, so it reaches M1/M2 and Linux alike
([ADR-0025](./adrs/0025-gvisor-machine-posture-and-socket-narrowing.md)). The libkrun microVM
this slot was first measured for is the deferred alternative — the measurements below record
why.

The same layers as a hardening ladder — one ring per step, each a line in `foldyard.toml`, with
the egress dial alongside:

![Foldyard isolation layers: four cumulative postures — a rootless Podman VM, Lima with the
in-VM wall, gVisor under the dev box behind a narrowed engine socket, and the host-side wall on
Linux — then the egress dial from open through observe and enforce to fail-closed, and what
never moves: the credentials stay on the host, the socket is the design hole, fy verify proves
the ring you are in.](./assets/foldyard-isolation-layers.svg)

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
actually escaped. That is the headline, and it is already true today. It is also why "move the
Mac to QEMU for uniformity with Linux" is backwards: Lima's QEMU driver on macOS uses the `hvf`
accelerator, so it would keep the same Apple hypervisor underneath and *add* QEMU's device model
on top — strictly more surface, plus a large performance regression. Lima reserves QEMU-on-macOS
for running Intel VMs on ARM, which foldyard does not do. The honest caveat on vz is that Apple's
implementation is closed, so its low public CVE count is partly absence of research; the
comparison does not turn on that.

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
- **The box cannot have the *microVM* layer on the Mac.** The libkrun route for the dev box —
  `--runtime krun` — needs `/dev/kvm` inside the machine VM, which Apple silicon only offers with
  `nestedVirtualization: true` on **M3+ / macOS 15+**. **M1/M2 consumers must be supported**
  (decided 2026-09-11), so krun is a Linux-only option (the deferred *(c)* stack below), and the
  maintainer's M3 nested-virt capability is a *test rig*, never a product assumption. `③` itself
  is gVisor, which needs no KVM (systrap) and so reaches M1/M2 too
  ([ADR-0025](./adrs/0025-gvisor-machine-posture-and-socket-narrowing.md)). The Mac's structure
  is therefore host ← VMM ← guest kernel ← Sentry ← box, against Linux *(c)*'s host ← QEMU ←
  guest kernel ← crun jail ← libkrun ← box kernel ← box: one kernel layer shorter than the
  microVM design, not `③`-less. On the Mac a Sentry escape lands in the machine VM's guest, and
  the machine VM is the whole boundary. What the Mac takes from the Linux work unchanged is the
  socket narrowing, which is platform-independent and closes the design hole on both.
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

**`(b)` is no longer a foldyard option.** It needed the `native` (no-VM) backend, retired on
2026-09-17 ([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)): foldyard always has a
VM, so on Linux the shape is `(a)`, with `(c)` the deferred hardening on top. The column stays as
the record of what `(b)` traded and why `(c)` was measured.

**Lima on Linux does not get you libkrun** — Lima registers only `qemu` there; `krunkit` is a
macOS/arm64 binary. So adopting Lima on Linux is a choice for *(a)*, i.e. for the boundary, mount
and wall properties, with QEMU as the knowingly-accepted weak link. It is not a way to make the
stack uniform with the Mac's, and picking it *for* uniformity would be picking the wrong reason.

Two things to pin if *(a)* is chosen: `vmtype` (so the VMM is a decision) and `mountType` —
Lima's QEMU driver defaults it to **9p**, which is both slower on the build hot path and the more
escape-prone of the two. **But `virtiofs` cannot be pinned on Linux yet** (measured 2026-09-11,
[below](#lima--qemu-on-linux-foldyards-own-lima-backend)): under Lima 2.2.0's rootless
`virtiofsd` 1.14 every inode create returns `EINVAL`, on Fedora and Ubuntu guests alike. So on
Linux the mount stays 9p for now, and the pin is a known gap rather than a setting.

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

### (c) Stack them — measured, and deferred (2026-09-11)

Nested KVM is on by default for `kvm_intel`/`kvm_amd`, and Lima's QEMU driver passes the host CPU
through, so `(a)` and `③` compose on Linux:

```
   host ──① Lima/QEMU VM  (repo-only mount, the wall, concurrency)
            └─② crun container  (namespaces · cgroups · seccomp = the jailer)
               └─③ libkrun microVM  ← the box runs here, on its own kernel
```

An agent in the box must escape libkrun into a jailed container, then the Lima guest kernel, before
it can so much as *see* the QEMU device model. QEMU is still the outer weak link, but it is no
longer reachable from the agent's shell. This was chosen on 2026-09-11 on the principle "stronger
isolation at reasonable performance cost", then measured the same day, and **deferred on the
measurement** ([Measured on GCP](#measured-on-gcp-2026-09-11)): the microVM runs package installs
7× slower and fork/exec 17× (pure CPU 1.3×), on the same rig where a Lima/QEMU guest at the same
depth does not. The second rig session found it is one penalty, not several: **process creation
costs ~8 ms under nested libkrun against ~0.5 ms under crun**, and every virtio-fs metadata
syscall is a 50–80 µs round trip — so `git status` is 27× and a package install forks its way to
7×. That is what builds, test runners, `git` and package managers do all day, and it is the
agent's own working set too, so "agent only in the microVM" does not rescue it.

**The decision (Dain, 2026-09-11).** Containment is the goal; per-session snapshots are not
wanted. `③` is deferred, not rejected: reassess periodically as libkrun, or another library,
improves — the outs were measured the same day ([below](#the-outs-measured)):
none rescues libkrun 1.x, and **gVisor is the live candidate** (package install at 1.1×, rootless
under podman, no KVM needed so it would reach M1/M2 too; its `git`-walk cost, the deciding number,
came in at ~3× against libkrun's ~25× — measured 2026-09-12, [below](#the-outs-measured)). The
product claim on every platform stays what the wall doc already states: **real credentials never
enter the VM** (the moat), plus **the wall as enforcement against everything short of a
guest-kernel exploit**. What `③` would have bought — a wall that survives VM-root — is bounded
by that moat: a VM-root escape can flush the wall but steals no credentials. Two hardening steps
come before any microVM, both at zero runtime cost:

1. **Drop Lima's passwordless sudo grant for the VM user — done 2026-09-11.** The box runs as
   that user's uid, so a container-runtime escape (no kernel exploit needed) was one
   `sudo nft flush ruleset` from open egress. Root in the guest is now boot-time only: a
   provision script recorded in the instance config narrows the grant to `shutdown` and installs
   the wall on every boot, and the host never runs `sudo` in the guest
   ([lima-wall-machine-integration.md](./lima-wall-machine-integration.md#2-enforcement--machinewall--true)).
   Validated live on a Lima/vz VM.
2. **Host-side wall enforcement on Linux — mechanism proven 2026-09-12, module landed.** Match
   the VM's own traffic *on the host*, where the guest has no reach, with nftables, and allow only
   the proxy band; flushing the guest wall then gains nothing, since the packets still have to
   leave through the host. The match is the QEMU process's **cgroup v2 scope**, not its uid:
   Lima's QEMU driver runs the guest's user-mode network inside `qemu-system`, so every guest
   packet leaves the host as that process, and the operator's other work shares their uid but
   only the VM lives in the VM's scope. Proven on the rig against the running `foldyard-example`
   VM: with the table loaded, a direct `https://` from inside the guest was rejected (curl rc 7),
   DNS to the host resolver still resolved, the allowed band port returned 200, an out-of-band
   port and the host's sshd were refused, `limactl shell` and the podman socket kept working, and
   the operator's own egress was untouched. `foldyard.hostwall` renders that ruleset, and as of
   2026-09-12 it is **wired into `fy up`** behind `[machine].host_wall = true`: the VM is started
   inside its own transient scope (`systemd-run --user --scope --unit fy-machine-<vm>.scope`) so
   the match is predictable, the table is re-rendered on every `fy up` for the scope the VM
   actually sits in (Lima allocates the SSH port per boot) and loaded with `sudo nft`, and a VM
   found outside its own scope is refused rather than walled — matching the login session's scope
   would wall the operator's shell. Preflight pairs it with `wall` and with a host that can
   enforce it. Run end to end on the rig the same day (`fy machine ensure` under
   `MACHINE_HOST_WALL=1`, the example VM created from scratch): every probe above held, plus
   `fy verify` ALL PASS under the wall, the hand-started VM refused, and `rm` leaving no table.
   One finding the by-hand probe had missed: the guest's DNS is Lima's *host resolver* — the
   hostagent serves it on a random loopback udp+tcp port and QEMU forwards each query there —
   so a ruleset allowing only `resolv.conf`'s stub cuts DNS; the wall now discovers and opens
   the loopback listeners the VM's own processes hold. Clean on Linux; on macOS Lima's
   user-mode network runs as the operator, so pf cannot single it out without a dedicated uid or
   a different network mode — the module reports itself unavailable there rather than branching
   on the OS.

Socket narrowing (below) stays the fix for the *design* hole, with or without `③`.

The other blocker was the socket: it is `AF_UNIX` over a bind mount, and virtio-fs does not carry
unix sockets across the microVM boundary (crun's manual: "sharing content with processes and
other containers outside of the krun VM is more difficult"). **Answered 2026-09-11.** The
bind-mounted socket fails under krun exactly as predicted — `connection refused`, nothing listens
in the guest kernel — and the shape that works is a **sidecar in the same pod holding the socket
and exposing it over TCP on the pod loopback**. The krun guest reaches the podman API there and
cannot reach the host's sshd. That sidecar is where the filtering proxy goes, and it runs
*outside* the microVM. vsock would be cleaner and libkrun exports the API (`krun_add_vsock_port`),
but crun's krun handler never calls it — an upstream patch, not a knob. The socket finding stands
with or without `③`: narrowing closes the design hole on its own.

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
WSL2 with a VM-less engine is bare Linux with a VM-less engine** — which is why the `native`
backend that offered that shape is retired ([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)).
The Hyper-V VM is real, but it protects Windows.

The good news is that the earlier draft was also wrong about the remedy. It said Lima inside a WSL2
distro needs a custom-built kernel for nested KVM. That was true in 2021 and is stale: the stock
WSL2 kernel (6.x) builds KVM as modules, `nestedVirtualization` defaults on for Windows 11 on x86,
and `/dev/kvm` is present after a `wsl --shutdown`. So **WSL2's menu is the Linux menu** — `(a)`,
and `(c)` above it, are the candidates inside the distro. Two hard limits: Windows 10 silently
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

| claim | macOS | Linux / WSL2 (lima) |
| --- | --- | --- |
| VM boundary exists | ✅ | ✅ |
| repo-only mount | ✅ | ✅ |
| `wall` fail-closed egress | ✅ | ✅ |
| microVM device model | ⚠ krunkit only | ❌ (QEMU) — `(c)` would add one *under* it; deferred |
| VMM jailed | ❌ unconfined operator process | ✅ crun, for `③` only |

A VM-less engine could never have advertised these claims — which is why the `native` backend
is gone ([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)) rather than documented as
weaker. That is not a documentation nicety: it is the difference between a checked property and
a slogan.

**A gap found and closed 2026-09-11, on every platform:** until that day the mount audit ran
`mount` inside a `--privileged` container, which shows the *container's* mount namespace, not the
VM's — a Lima VM that mounted the operator's whole home passed it (measured on the Linux rig with
a deliberately leaky VM). The audit now reads PID 1's table via `--pid=host`, which does see the
leak, and exempts only the repo and worktrees-root mounts by exact path. Run live on a Mac it
passes on exactly those two, and on the rig (2026-09-12) `verify` itself — not the probe by hand —
FAILED against a Lima VM deliberately mounting the whole home (`VM exposes host paths: … /home/dain
9p ro`) and PASSED once the leak was removed. See
[verify-false-pass.md](./verify-false-pass.md#a-second-gap-2026-09-11-the-probe-reads-the-wrong-mount-namespace).

## What can be measured where

The macOS column was measured on 2026-09-07 and the Linux column on 2026-09-11 (both below); the
WSL2 column is still reasoned from source. What the hardware to hand can and cannot settle:

| rig | can verify | cannot |
| --- | --- | --- |
| M3 + Lima/vz **or** krunkit, `nestedVirtualization: true` (a Fedora guest with `/dev/kvm`) — **test rig only**, M1/M2 consumers have no `/dev/kvm` | krun mechanics and the socket transport on arm64 (the Linux `③` code path, cheaply); `(a)` Lima+QEMU inside a Linux-host stand-in, **or** `(b)` krun inside one — two levels, arm64 | `(c)` on Linux: the third level (arm64 nested KVM is experimental). And nothing it shows about `③` is a Mac product claim |
| a household NAS (TrueNAS 25.04, i5-6500T) | nothing until a BIOS visit: VT-x is off since a 2026-09-05 CMOS loss, and it has ~3 GiB free with no swap | — |
| GCP `--enable-nested-virtualization` (Fedora; Windows Server for WSL2) — **used 2026-09-11**, Fedora half only | `(a)`, `(b)` at two levels; `(c)` at three; WSL2 baseline | Windows 11-client-specific claims; and it is one level deeper than a real laptop, so overhead numbers are upper bounds — except that Google's KVM → VM → krun is the *same* two levels as laptop → Lima/QEMU → krun, so for `③` the depth matches the design |
| M3 + UTM, Windows 11 ARM | WSL2 baseline: `/mnt/c`, `wsl.conf`, the supervisor-in-distro finding | `/dev/kvm` in WSL2 — Windows-on-ARM boots the distro at EL1, so it is structurally absent, not just untested |
| M3 + UTM, Windows 11 x86 (emulated) | nothing useful — TCG does not emulate VT-x for Hyper-V | everything |
| any x86 Windows 11 machine | `wsl --shutdown; wsl; ls -l /dev/kvm` — read-only, two minutes | — |
| a Fedora VM on an x86 Linux host with nested virt on (e.g. a NAS hypervisor) | **`(c)` end to end** — x86 nested KVM is mature | — |

So an x86 Linux VM is the only rig that reaches the (now deferred) stacked Linux design, and the
Mac's product path is what M1/M2 consumers already run: the machine VM as the boundary, with no
`③`. What the Mac still owes is the socket narrowing and a decision on krunkit — probed on
2026-09-07, never run at length.

## Measured on macOS (2026-09-07)

What a live M3 Max reports — macOS 26.6.1, podman 6.0.2, Lima 2.1.3 — with both backends in use
side by side: one legacy project on the podman backend (kept unmigrated for colleagues) and four
projects on Lima with the wall.

| | podman backend (×1) | lima backend (×4) |
| --- | --- | --- |
| VMM | **libkrun** (microVM) | **vz** (Virtualization.framework) |
| pinned? | `provider = "libkrun"` in `containers.conf` | was **not** — inherited from Lima's `DefaultDriver()`; `[machine].vmtype` now pins it |
| repo mount | virtiofs, rw, uid maps correctly | virtiofs (vz's default) |
| mount set | repo + worktrees root only ✅ | repo only ✅ |
| `/dev/kvm` in guest | **present** | absent (`nestedVirtualization` defaults false) |
| `[machine].wall` | n/a (podman) | **on, all four**, distinct port bands |
| concurrency | 1 VM | 3 instances coexisting |

The backend kept for legacy compatibility was the one with the microVM device model and nested
virt; the modern default had the wall, concurrency and a conventional VMM — an accident worth not
keeping. Three things this settled, two it corrected:

- **`vmType` was never pinned.** The only `vmType` in any `lima.yaml` was a commented-out note
  about Intel Macs; `limactl list --json` reported `vmType: vz` (resolved) and `mountType: null`
  (never recorded). `[machine].vmtype` closes that; `resolve_vmtype()` returns `vz` on this host,
  correctly declining the registered-but-experimental krunkit.
- **`limactl info` returns `vmTypes` in non-deterministic order** (Go map iteration) — observed
  as both `['qemu','vz','krunkit']` and `['vz','qemu','krunkit']` seconds apart. The explicit
  preference tuple is not fussiness.
- **libkrun's virtio-fs is fine.** The reported macOS bind-mount permission problem does not
  reproduce: both mounts are `virtiofs rw`, owned by `core` = uid 501 = the host user.
- **Correction — the wall was not tied to Lima's `provision:` blocks, and now deliberately is.**
  ADR-0011 says owning VM provisioning "is what makes the fail-closed nftables wall possible".
  On 2026-09-07 that was not literally true: `machine.wall_sync` pushed `machine-wall.sh` into a
  *running* VM over `limactl shell … sudo` and an `fy-wall.service` unit re-asserted it on boot —
  root shell into a mutable guest, not create-time provisioning, so it would survive a `vmType`
  change untouched. Since 2026-09-11 the wall IS a `provision: mode: system` script recorded in
  the instance config, because root in the guest is boot-time only (the VM user's sudo grant is
  dropped by the same script). ADR-0011's sentence is true again, for a different reason; the
  `vmType`-independence still holds (the script is driver-agnostic).
- **Correction — `template://podman` is deprecated.** Lima v2 wants `template:podman`;
  `LimaBackend.create()` emits a deprecation warning on every creation. Works now, won't forever.

### Lima + krunkit: the probe (2026-09-07)

If it works, one backend gets the wall *and* concurrency *and* the microVM device model, and the
podman backend's remaining justification shrinks to "colleagues have not migrated". Before the
probe: the driver was installed and registered (bundled with Lima 2.1.3 at
`/opt/homebrew/Cellar/lima/2.1.3/libexec/lima/lima-driver-krunkit`, listed in `limactl info`),
and foldyard's exact create path — the real podman template plus the real `--set` expression —
passed `limactl validate` under `vz`, `krunkit` and `qemu`, krunkit accepting `mountType: virtiofs`.

The probe: a throwaway `fy-krun-probe` created through that exact path (only the sizing shrunk),
on the same M3 Max, **while the legacy libkrun VM was running**.

| question | result |
| --- | --- |
| Concurrency — does it inherit libkrun's `RequireExclusiveActive`? | **No.** `fy-krun-probe krunkit Running` alongside `/opt/podman/bin/krunkit --cpus 8 --memory 48000`: two libkrun VMs at once, from two managers. |
| Guest boots + provisions? | **Yes** — Fedora 44 under libkrun-efi, `podman 5.8.4` installed by the template's own provision script. |
| Forwarded socket at `<Dir>/sock/podman.sock`? | **Yes**, and a raw `AF_UNIX` connect succeeds. |
| Does it speak libpod — the actual `Backend` contract? | **Yes.** `podman --remote --url unix://…` → `linux arm64 \| rootless=true \| 5.8.4`. |
| virtio-fs mount? | **Yes** — `type virtiofs (rw,relatime,seclabel)`, readable, owned by uid 501. |
| `/dev/kvm` in the guest? | **No** by default; **yes** with `nestedVirtualization: true` (re-created and re-tested). Unlike podman's libkrun, Lima's krunkit leaves it off unless asked. |

So the decider — concurrency — came out in krunkit's favour, and `machine.socket()` /
`guest_socket()` need no change to reach it. **The wall and `fy verify` under krunkit were both
green:** the wall installed unmodified on the first try (`uid 501 +subuid … default-deny; open:
lo, local-DNS, 192.168.5.2 tcp {41800-41889, 41900-41989}`), and the VM-boundary battery passed
wall on and off — as the battery then stood, before the 2026-09-11 mount-namespace fix.

What remains before krunkit could be a recommended posture, and why it stays opt-in: the driver is
upstream-experimental and pinned to a Homebrew Cellar path that moves on every Lima upgrade (a
maintenance commitment, not a footnote); a boot, a socket probe and one battery are not a week of
builds — the four-project Lima/vz mileage is the evidence standard; and it is macOS/arm64 only, so
it changes nothing for Linux. One consequence for the rig: `nestedVirtualization: true` under
krunkit exposes `/dev/kvm`, so [nested-virt.md](./nested-virt.md)'s L1 no longer needs
`podman machine`, and could move to the same backend as the product.

## Measured on GCP (2026-09-11)

Everything in the Linux and WSL2 sections was reasoned from source until this run. This is what an
x86 Linux VM with nested virtualisation actually reports: GCP `n2-standard-4`
(`--enable-nested-virtualization`), Fedora 44, kernel 7.1.10, podman 5.8.4, crun 1.28 +
`crun-krun`, libkrun 1.19.0 with guest kernel (libkrunfw) 6.12.91, Lima 2.2.0, `virtiofsd` 1.14.
Google's KVM → this VM → krun is the same two levels as laptop → Lima/QEMU → krun, so the depth
matches the real design; krun inside the Lima guest (three levels) was run too. The Windows/WSL2
half of the rig has not been started.

### The socket: answered, and the narrowing shape works

| probe | result |
| --- | --- |
| bind-mount the podman `AF_UNIX` socket into a krun container | the file is visible over virtio-fs (`stat` works); `connect` fails **`connection refused`** — nothing listens in the guest kernel. As predicted. (Both crun and krun first failed `permission denied` — SELinux, fixed with `--security-opt label=disable`; the control matters.) |
| TCP to the host's loopback, default network | **unreachable.** The krun guest has only `lo` and a `dummy0` (203.0.113.1/24, no default route); libkrun's TSI proxies guest sockets through the VMM process, so guest `127.0.0.1` is the VMM's network namespace — the pasta netns podman made, which has nothing on it. Internet and the GCE metadata server ARE reachable, as for any container. |
| TCP to the host's loopback, `--network host` | **reachable — and so is everything else on the host's loopback**: sshd `:22`, the metadata server. "Same security context" made concrete: never ship `--network host` for the box. (Oddity, not chased: with `--network host` the guest could NOT reach the internet.) |
| **sidecar in the same pod holding the socket** — `podman pod create`, a crun `socat` container `TCP-LISTEN:2375,bind=127.0.0.1 → UNIX-CONNECT:/sock/podman.sock`, the krun container `--pod` | **works.** The krun guest reaches the podman API at pod-loopback `127.0.0.1:2375`; the host's sshd is unreachable from it. **This is the narrowing shape**: the thing on the pod loopback need not be `socat` — it is where the filtering proxy (endpoint allowlist, mounts restricted to under the repo) goes, and it runs outside the microVM. |
| vsock | `/dev/vsock` exists in the guest and libkrun exports `krun_add_vsock_port` (guest port → host unix socket path — exactly the transport wanted), but **crun's krun handler never calls it**. Confirmed from the binary's imported symbols and upstream `handlers/krun.c`: it calls `krun_add_net_unixstream`, `krun_add_virtiofs2`, `krun_set_vm_config`, `krun_set_nested_virt` and friends, and reads only the `krun.cpus` / `krun.ram_mib` / `krun.gpu_flags` / `krun.nested_virt` / `krun.use_passt` / `krun.variant` annotations plus `/.krun_vm.json` from the image rootfs (a bind-mounted one is not read). vsock is an upstream crun feature request, not a knob. |
| `krun.use_passt=1` | a real `eth0`, but DHCP's default route fails (`ENETUNREACH`, the address arrives as a `/32`), so no egress until `ip route add default dev eth0` inside; slower than TSI on a 100 MB download (1.7 s vs 1.1 s; crun 0.4 s). Not needed. |
| `-p 127.0.0.1:8099:8099` host→guest publish | works. |

`--annotation krun.ram_mib=8192 --annotation krun.cpus=2` is honoured; the guest defaults to
**1 GiB RAM** and the host's vCPU count.

### The overhead of `③`: not "reasonable" for a dev box as-is

Each run twice, stable to ±3%. Same podman, same image; only `--runtime` differs.

| workload | crun | krun (two levels) | ratio |
| --- | --- | --- | --- |
| `podman run … true` (startup) | 0.15–0.30 s | 0.85 s | ~4× |
| `dnf install gcc make` | 14.0 s | 102 s | **7×** |
| `pip install numpy pandas` | 8.6 s | 23.8 s | 2.8× |
| CPU: python `sum(i*i for i in range(30M))` | 2.64 s | 3.45 s | 1.3× |
| 1 GB `dd` to container rootfs (`conv=fsync`) | 0.74 s | 6.7 s | 9× |
| 1 GB `dd` to bind mount | 0.97 s | 7.2 s | 7× |
| 10k small files on bind mount | 0.88 s | 26.6 s | **30×** |
| 10k small files on **tmpfs inside the guest** | 0.19 s | 24 s | **120×** |
| 2000 × fork/exec (`/bin/true`) | 0.91 s | 16.2 s | **18×** |
| 400k syscalls (`dd bs=1`) | 0.073 s | 0.067 s | 1× |
| 100 MB download | 0.43 s | 1.07 s | 2.5× |

What it is and is not:

- **Not virtio-fs**: tmpfs inside the guest is as slow as the bind mount.
- **Not syscalls**: syscall-bound work is at parity, CPU-bound work near it.
- **Not nested virt in general**: the Lima/QEMU guest on the same rig at the same depth does the
  fork/exec loop in 2.8 s (3× crun, not 18×) and the tmpfs churn in 0.05 s. It is libkrun-specific.
- **Not fixed by** more vCPUs (1 vCPU: 11 s; 4: 16 s), host THP `always` (the VMM gets
  hugepages, no change), or unbinding the guest's virtio-balloon — which DOES fix repeated page
  allocation (`dd` 512 MB to tmpfs drops from 3.1 s to 0.7 s on the second run once the balloon's
  free-page reporting stops handing freed pages back) but moves neither fork/exec nor file churn.
- **What `perf` sees from the outer VM**: ~240,000 VM exits for 500 fork/execs (or 3,000 tiny
  files) — ~47% `EPT_VIOLATION`, ~27% `EPT_MISCONFIG` (= MMIO; libkrun's devices are virtio-mmio
  and its APIC is the MMIO xAPIC), ~18% `HLT`. Each inner exit costs outer round-trips. The QEMU
  guest's count for the same loop was not captured cleanly (retry with the loop backgrounded and
  `perf stat -a`).
- **Memory-mapping-heavy work — exec, mmap/munmap, page-table churn — is what hurts.** That is
  builds, test runners, `git` and package managers: the dev box's whole workload.
- Three levels (krun inside the Lima guest) works, guest kernel 6.12.91, and the fork/exec loop
  took **277 s** — 17× the two-level krun number, 130× crun.

The outs are measured in the next subsection: none of them moves libkrun 1.x.

### The outs, measured

*Second rig session, 2026-09-11, except the agent-loop, host-wall and leaky-VM rows, 2026-09-12
(third session).*

Same rig and versions, plus libkrun 1.19.4 built from source, gVisor `runsc`
release-20260817.0, and a direct-libkrun harness (`examples/chroot_vm.c` from v1.19.0 with
`krun_split_irqchip` and the vCPU count from env, rootfs = `podman export` of the tools image).

**Correction to the table above: the "small files 30× / 120×" rows were fork/exec in disguise.**
A `touch` per file costs the same ~8.5 ms as any exec; file creation itself, by shell redirection
with no fork, is 5× (1.26 s vs 0.26 s for 10k files on guest tmpfs). There is one penalty —
**process creation, ~8 ms vs ~0.5 ms** — and package installs, builds and test runners pay it
thousands of times.

**Where the ~400 VM exits per fork/exec go** (`perf stat -a` from the outer VM): 74% are MMIO,
split between the **xAPIC** (~37 IPIs per exec across 4 vCPUs — TLB shootdown and reschedule —
each 3 MMIO writes plus an EOI) and **virtio-fs on the rootfs** (~37 FUSE requests per exec:
notify, interrupt status, ack, EOI); the rest are EPT violations from memory the balloon keeps
un-mapping, and HLT. The Lima/QEMU guest at the same depth does the loop in 2.76 s with ~82
exits per exec, all cheap in-kernel kinds — CPUID and MSR writes, *zero* MMIO, *zero* page
faults — because its APIC is x2APIC (IPIs are MSRs), its rootfs is a block device behind the
guest page cache, and its memory stays mapped. So session 1's "not nested virt in general" was
right: it is libkrun's device model (virtio-mmio + xAPIC) plus a virtio-fs root, under nesting.

| out | result |
| --- | --- |
| split irqchip | **no knob** — crun's krun handler never calls `krun_split_irqchip`, and libkrun 1.19 has no env var for it. Measured anyway via the harness: 16.2 s vs 16.5 s, no effect. (The default is already the in-kernel irqchip; "split" moves the IOAPIC *into* userspace.) 1 vCPU takes ~40% off (9.9 s) — that is the IPI share. |
| x2APIC | the real APIC-side out, and it is closed at the kernel config: libkrunfw 5.5.0 has `CONFIG_X86_X2APIC` unset, so the guest boots xAPIC. Turning it on is a libkrunfw rebuild (not done). Upper bound from the exit histogram: the ICR/EOI exits are about a quarter of all exits — the virtio-fs half is untouched. |
| newer libkrun | 1.19.4 (latest, 2026-07-03; the changelog is virtio-fs and macOS fixes) built and run through the same harness: 17.3 s vs 16.9 s, no change. `main` is the 2.0 API, which crun 1.28 cannot drive. Fedora has nothing newer. |
| agent-shaped workload | **in the expensive band whenever it walks a tree.** `git status` ×50: 27×; `git grep`: 20×; `git log`: 12×; python reading 2000 files: 7–10×; `node` start 2.5×; in-guest HTTP loop 2.8×; CPU 1.3×. In-guest `strace -c` of one `git status` (5 ms crun, 213 ms krun): every metadata syscall that touches virtio-fs — `newfstatat`, `getdents64`, `openat`, even `close` and `fstat` — is a 50–80 µs FUSE round trip against 3–6 µs under crun; cached data reads are fine. A coding agent's working set is `git`, file walks and short-lived tool processes, so "agent only in the microVM, builds outside" does not rescue it. |
| three levels | `dnf install gcc make` under krun inside the Lima guest: **5,537 s** (rc 0; ~20 min overlapped another benchmark, so "an hour and a half") — 54× the two-level number, ~400× crun. |

**gVisor (`runsc`) works rootless under podman**, after three fixes that each failed separately:
`--security-opt label=disable` (SELinux, as for krun); a wrapper so podman passes runsc the
flags it otherwise cannot —

```sh
# /usr/local/bin/runsc-fy
#!/bin/sh
exec /usr/local/bin/runsc --allow-flag-override --ignore-cgroups "$@"
```

(runsc otherwise tries to create a systemd scope over the system bus, and podman refuses
`--cgroups=disabled` for it); and `--annotation dev.gvisor.flag.<name>=…` for per-container flags
such as the platform. The guest reports kernel `4.19.0-gvisor`. Systrap is the default platform
and uses no KVM at all, so it is nesting-agnostic; the kvm platform also runs rootless here.

| workload | crun | runsc systrap (rootless) | runsc kvm platform | krun, for scale |
| --- | --- | --- | --- | --- |
| `podman run … true` | 0.19 s | 0.23 s | 0.25 s | 0.85 s |
| 2000 × fork/exec | 1.13 s | **6.9 s (6×)** | 7.1 s | 17.6 s (17×) |
| 10k files on tmpfs, shell redirect | 0.27 s | **0.83 s (3×)** | 1.8 s | 1.26 s (5×) |
| 3000 × `touch` on a bind mount | — | 11.1 s (3.7 ms each) | 11.9 s | ~25 s (8.4 ms each) |
| CPU: python `sum(i*i)` 30M | 2.83 s | **2.84 s (1.0×)** | 2.95 s | 3.45 s (1.3×) |
| 100 MB download | 0.44 s | 0.28 s | 0.46 s | 1.07 s |
| `dnf install gcc make` | 16.6 s | **18.1 s (1.1×)** | 22.0 s | 102 s (7×) |

Also probed: the bind-mounted podman `AF_UNIX` socket **connects** from inside, but only with the
per-container opt-in `dev.gvisor.flag.host-uds=all` (the default refuses) — so unlike krun the
socket needs no re-plumbing, and it is exactly the unfiltered socket; the host's loopback is
unreachable (netstack has its own); egress works. gVisor is a different boundary — a userspace
kernel in Go behind a small seccomp filter, not a hardware VM — so the Trail of Bits framing
becomes "the sentry's syscall surface" rather than "the VMM's device model".

**The deciding number — the agent's own working set (2026-09-12, third rig session).** gVisor's
gofer/directfs metadata path was the cost centre left to measure; it is the walk a coding agent
does all day, and it is where libkrun collapsed. On the same rig, an agent-shaped image (git +
node + python over a 200-commit checkout), each runtime twice warm:

| workload | crun | runsc systrap (default) | runsc kvm | libkrun, for scale |
| --- | --- | --- | --- | --- |
| `git status` ×50 (rootfs) | 0.48 s | **1.52 s (3.2×)** | 2.04 s | 12.0 s (25×) |
| `git grep` ×20 | 0.37 s | **1.14 s (3.1×)** | 2.24 s | 7.40 s (20×) |
| `git log -20` ×50 | 0.27 s | 0.61 s (2.3×) | 1.08 s | 2.90 s (11×) |
| python read 2000 files | 0.22 s | 0.37 s (1.7×) | 0.54 s | 1.68 s (7.6×) |
| `node -e` start ×50 | 4.85 s | 6.74 s (1.4×) | 15.3 s | 13.5 s (2.8×) |
| in-guest HTTP loop ×300 | 1.45 s | 1.79 s (1.2×) | 2.06 s | 4.08 s (2.8×) |
| python regex (CPU) | 0.77 s | 0.95 s (1.2×) | 0.77 s | 2.39 s (3.1×) |

So gVisor's git-walk cost is **~3×, not libkrun's ~25×** — the number that was missing, and it
lands where it matters most. The default systrap platform (directfs on) is the row that counts:
the kvm platform is 1.3–2× worse on metadata here and buys nothing rootless, and directfs off
roughly doubles the bind-mount walk. Still unsettled before gVisor could be the `③` posture:
whether `--ignore-cgroups` (which the box needs, and which drops podman's per-container resource
limits) is acceptable, the `host-uds=all` socket exposure, and a sustained build rather than
micro-loops. But on cost alone gVisor clears the bar libkrun could not.

**Confirmed on the Mac — arm64 / vz (2026-09-12).** The whole reason gVisor was picked up is that
the Mac has fewer options than Linux (no `③` microVM without nested KVM on M1/M2), so the question
was whether it runs, and at what cost, on a real M3 inside a Lima/vz VM — not just on the x86 rig.
It does. `runsc` release-20260817.0 runs rootless under podman 5.8.1 with the same wrapper
(`--ignore-cgroups`, `--allow-flag-override`) and `--security-opt label=disable`, guest kernel
`4.19.0-gvisor`. Measured in a throwaway Fedora-44 arm64 VM against a real foldyard checkout on a
read-only bind mount:

| workload | crun | runsc systrap (directfs on) | runsc directfs **off** |
| --- | --- | --- | --- |
| `git status` ×200 | 1.72 s | **4.59 s (2.7×)** | 15.1 s (8.8×) |
| `git grep` ×100 | 0.46 s | **1.46 s (3.2×)** | — |
| fork/exec ×2000 (sys) | 0.38 s | 1.83 s (~5×) | — |
| toolchain install + C-ext compile | 4.96 s | 6.95 s (1.4×) | — |
| container startup (`run … true`) | 76 ms | 126 ms | — |

So the arm64/vz git-walk is **~3×**, the same order as the x86 rig — *provided directfs is on*,
which it is by default; forcing it off is 8.8×, so keeping it on is load-bearing (it is the row
that decides, not a tuning knob). Builds and CPU work are ~1.4× or better. Two of the three
"still unsettled" items above are now answered, both on the Mac:

- **`--ignore-cgroups` costs nothing real here.** It drops podman's *per-container* resource
  limits, and the box sets none (`box.py` passes no `--memory`/`--cpus`/`--pids-limit`); the VM's
  own sizing is the ceiling. The flag is needed only because rootless podman otherwise has runsc
  try to make a systemd scope over the system bus and refuses.
- **`host-uds=all` is the unfiltered socket — the door, demonstrated.** With the flag the
  bind-mounted podman socket connects (the default refuses); without it the box can't drive the
  engine at all, so the box needs it. But from inside a gVisor container holding that socket, a
  `podman --remote run --privileged -v /:/host` sibling started, ran under the **VM** kernel (not
  gVisor), and listed the VM's home. So `③` via gVisor does **not** remove the socket hole:
  `host-uds=all` hands over the full API, and an unconfined privileged sibling is one call away.
  **Socket narrowing is a precondition for `③`, not an alternative to it** — the two close
  different holes (kernel-exploit vs. by-design API), and gVisor's netstack (host loopback
  unreachable) does not substitute for the API filter.
- **inotify does not cross the gVisor boundary inward** (same shape as krun): a watcher inside a
  runsc container saw its own writes but neither VM-side nor host-side writes to the shared mount,
  while `ls`/stat (revalidation) saw every file immediately. So file-watching dev servers in the
  box need polling (`CHOKIDAR_USEPOLLING`, `vite --watch` poll, `nodemon -L`, `watchexec --poll`),
  exactly as the shared-mount model already implies — but it is a caveat to carry into any `③`
  decision, and a two-way sync (Mutagen-style) would be the alternative if native inotify ever
  became a hard requirement. ~~Still owed before an ADR: gVisor as the **real box runtime**
  (`fy box up` under runsc, not a hand-run container) and a long build~~ — both done on the rig
  2026-09-13, next section; the Mac's long build is still owed.

### gVisor as the real box runtime, and a sustained build (2026-09-13, fourth rig session)

**The box.** foldyard's own dev box — every argument `fy box up` emits (the echoed `podman run`
line: `--user 0`, `label=disable`, the repo + worktrees mounts, the engine socket, the tool and
shell volumes, the wall-registered network, the proxy env and CA bundle), created under
`runsc` with `--annotation dev.gvisor.flag.host-uds=all` — comes up on the rig's Lima/QEMU VM
(Fedora 44 guest, podman 5.8.4, runsc release-20260817.0 installed **rootless** in the guest
user's `~/.local/bin` with the `--ignore-cgroups` wrapper and a `[engine.runtimes]` entry in the
user's `containers.conf`; no root needed, which matters because root in the guest is boot-time
only). Kernel `4.19.0-gvisor`. From the host `fy box ps`/`exec` see it as the project's box; in
it the foldyard CLI installs from the staged wheel in 7.6 s, `fy ps` reaches the engine over the
socket, and **`fy verify` is ALL PASS** under both walls + the proxy (the probes are siblings the
engine runs under crun, so the boundary battery is unchanged by the box's runtime).

**The sustained workload** — this repository copied onto the worktrees mount (9p), run twice in
each box; the second, warm-cache run is the row (the first differs only in `git status`, which
is cold-cache on both, and in downloads). Same VM, same image, same mounts; only the runtime:

| workload (in-box, on the 9p mount unless noted) | crun | runsc systrap, directfs on | ratio |
| --- | --- | --- | --- |
| `git status` (warm) | 0.37 s | 0.46 s | 1.2× |
| `git grep` · `git log --stat` | 0.28 · 0.23 s | 0.28 · 0.23 s | 1× |
| `uv sync` into a fresh `.venv` (warm uv cache) | 41 s | 104 s | **2.5×** |
| this repo's full suite (1479 tests, serial, `CI=1`) | 142 s | 226 s | **1.6×** |
| `ruff check` | 0.62 s | 0.78 s | 1.3× |
| 10k small files on the mount | 15.8 s | 39.2 s | 2.5× |
| 10k small files on `/tmp` | 0.72 s | 0.70 s | 1× (gVisor's own tmpfs) |
| 2000 × fork/exec (`/bin/true`) | 2.8 s | 30.1 s | **11×** |

(Read those two bold rows with the next section: the venv was ON the 9p mount there, a shape
foldyard's own box never runs — `shadow_volumes` puts it in a named volume — and the fifth session
re-measured both in the shapes that matter.) So the long-run cost lands where the micro-loops said
it would: git walks near parity, the test suite 1.6×, a dependency install 2.5×, and process
spawning ~11× — the one figure that would
hurt a fork-heavy tool (a shell-script-per-file build, `make -j` over many tiny compiles). For
an agent loop that is mostly editing, git, a test run and the odd install, gVisor's cost on this
VM is **under 2× wall-clock** on the realistic items. (All of this is one hypervisor level
deeper than a laptop; the ratios transfer, the absolute seconds do not.)

**The product route is the surprise, and it is a podman fact, not a gVisor one.** foldyard
reaches the VM's engine over its socket everywhere (the host's `podman` goes remote via
`CONTAINER_HOST`; the box's is `podman-remote`), and **podman's API has no per-container runtime
selection at 5.8**: `--runtime` is a *local* podman global option that `podman-remote` does not
have at all (`unknown flag`), the libpod create endpoint's `oci_runtime` field is accepted and
ignored (`pkg/specgen/generate/container_create.go` in 5.8.4 picks a runtime only from the
*image platform*, `platform_to_oci_runtime` — the wasm hook), and the Docker-compat
`HostConfig.Runtime` is never read (unmapped in `compat/containers_create.go`, still on `main`).
All three were tried against the rig's socket and came back under crun. **podman ≥ 6.0 honours
`oci_runtime` on the libpod create endpoint** (`WithCtrOCIRuntime(s.OCIRuntime)` from v6.0.0),
but the CLI still does not set it (`specgenutil` never touches `OCIRuntime`), so on 6.x the
route is the REST API, not a `podman run` flag. Hence the runtime knob prototyped this session
(`[box].runtime` → `podman run --runtime`; kept as a patch, not on the branch) cannot work in
any foldyard topology, and the box above was created by hand *inside the guest* with the local
CLI. The shapes that would work, for the design call:

1. **Create the box over the libpod REST API** with `oci_runtime`, on podman ≥ 6.0 in the VM
   (stdlib `http.client` over the unix socket; the rest of the box lifecycle stays CLI). Needs
   the VM image to carry podman 6 — the rig's Fedora 44 guest has 5.8.4.
2. **A second API socket in the VM whose default runtime is runsc** (`podman system service`
   under a user unit with a `containers.conf` override, forwarded by Lima beside the main one);
   `fy box up` creates the box through that socket and everything else through the default.
   Works on podman 5.8 today; costs a user unit in the guest and a second forward in the
   instance config.
3. **The VM engine's default runtime = runsc for everything**, stack included — a different
   product (Postgres under gVisor), and `verify`'s probes would need re-reading.

Either way the runtime binary and wrapper have to be *in the VM*, rootless, which is the guest
user's `~/.local/bin` + `containers.conf` — installable from the host without root, so it can be
part of `machine ensure` rather than the boot script. And none of it changes the socket door:
`host-uds=all` is still the unfiltered engine API from inside the box, so narrowing stays the
precondition for any boundary claim.

### Where the files live, and what the suite's 1.6× is made of (2026-09-13, fifth rig session)

Two questions from the fourth session's numbers, both answered by measurement on the same rig,
VM and box image (Lima/QEMU Fedora 44 guest, 2 vCPU / 2 GiB, podman 5.8.4, crun vs rootless
runsc release-20260817.0 systrap + directfs, this repository as the workload, `CI=1 uv run
pytest -q -p no:cacheprovider` — the CI hypothesis profile already sets `database=None`, so no
example database is in any row). The crun box is the one `fy box up` creates; the runsc box is
that exact echoed `podman run` line with `--runtime runsc-fy --annotation
dev.gvisor.flag.host-uds=all` inserted (created inside the guest, with the combined CA bundle
foldyard's bootstrap would have written — a hand-created box has none, and uv then fails every
download through the proxy with `UnknownIssuer`). Every row is a warm run; pairs agreed within
2 %.

**1. The placement decides more than the runtime does.** The same `.venv` + uv cache, `uv sync`
into a fresh venv, four placements:

| warm `uv sync` (venv + cache together) | crun | runsc | ratio |
| --- | --- | --- | --- |
| the 9p mount (worktrees dir) | 29.0 s | 51.2 s | 1.8× |
| a named volume (`[box].caches`, VM btrfs) | 0.28 s | 1.57 s | 5.6× |
| the container rootfs (overlay) | 0.35 s | 0.57 s | 1.6× |
| `/tmp` (gVisor's own tmpfs / crun's overlay) | 0.35 s | 0.23 s | 0.7× |
| venv on the mount, cache in the rootfs — the fourth session's row | 42.8 s | 105.8 s | 2.5× |

The mount is a **100× layer under crun** before gVisor enters (29 s vs 0.28 s), and the fourth
session's 2.5× was half the cross-filesystem copy INTO 9p through the gofer (51 s with the cache
beside the venv on the mount, 106 s with it in the rootfs). Off the mount the predicted order
holds under runsc — tmpfs < rootfs < volume < 9p — and the gofer tax on a native volume is real
for a create-heavy install (5.6× relative) but 1.3 s absolute for a 126 MB venv; gVisor's
internal tmpfs beats crun's overlay. Cold installs through the proxy: 2–6 s off the mount, 63 s
(crun) / 94 s (runsc) on it. Copying repo + venv (150 MB) OFF the mount costs 90–110 s under
either runtime — the same 9p read cost seen from the other side; runsc is not slower at reading
through 9p.

**The shape foldyard's own box runs** (`foldyard.toml` `[box]`: `shadow_volumes = [".venv"]`,
`warmup = uv sync --frozen`, `UV_LINK_MODE = "copy"` — source on the mount, ONLY the venv in a
per-box volume, cache in the rootfs; emulated with `UV_PROJECT_ENVIRONMENT`, same filesystems as
the shadow mount):

| the shadow shape | crun | runsc | ratio |
| --- | --- | --- | --- |
| `uv sync --frozen`, warm (a copy across filesystems; hardlink mode falls back to the same) | 1.1 s | 6.1 s | 5.5× |
| the suite, serial | 122 s | 194 s | 1.6× |
| the suite, `-n 2` | 76 s | 165 s | 2.2× |
| `ruff check` · `git status` (warm) | 0.14 · 0.32 s | 0.39 · 0.63 s | 2–3× |

So under gVisor an install into the shadowed venv is 6 s where the mount-shaped row was 106 s,
and the suite's ratio in the realistic shape is the same 1.6× — which brings the second question.

**2. The suite's 1.6× is the syscall path, not the mount.** The candidates were (a) systrap's
per-syscall interception, (b) pytest-side writes landing on 9p through the gofer, (c) source and
site-packages reads through 9p+gofer on import. The suite moved off the mount entirely, and each
pytest-side lever on the mount, one at a time:

| the suite (1479 tests, serial unless noted) | crun | runsc | ratio |
| --- | --- | --- | --- |
| on the 9p mount (baseline, first and last run) | 144 s | 229 s | 1.6× |
| mount + `PYTHONPYCACHEPREFIX=/tmp/pyc` (2nd run) · `PYTHONDONTWRITEBYTECODE=1` | 143 · 143 s | 226 · 229 s | — (no change) |
| mount + `--import-mode=importlib` | 6 collection errors | same | not a candidate |
| mount + `-n 2` (xdist, 2 vCPU) | 94 s | 197 s | 2.1× |
| the whole repo + venv on `/tmp` (gVisor's own tmpfs: NO gofer) | 101 s | 174 s | **1.7×** |
| … in the named volume · in the rootfs | 102 · 101 s | 173 · 172 s | 1.7× |
| off the mount + `-n 2` | 59 s | 145 s | 2.5× |

Off the mount, with no gofer in the path at all, the ratio is 1.7× — *higher* than on the
mount, because 9p costs both runtimes the same ~40 s of import reads and dilutes the ratio. So
(b) is nothing (a warm run's pycs are reads; the hypothesis database was already off), (c) is
the mount's cost under either runtime, not gVisor's, and what remains is **(a): the sentry's
syscall path, which is CPU**. Two corroborations: the three off-mount placements agree to 1 %
(the gofer adds nothing to a read-mostly workload), and xdist scaling — two workers on two vCPUs
take crun from 101 s to 59 s (1.7×) but runsc only from 174 s to 145 s (1.2×), because the
sentry's own CPU work competes with the second worker. No test configuration touches that; the
levers are the platform (kvm rather than systrap — measured next) and the workload's syscall
count. `df` inside a runsc box, for the record, reports every gofer-backed mount as `9p`, the
volume included — the sentry sees them all through one file protocol; the backing store is
whatever the VM has.

**What this means for the advice.** "Caches in a volume" is not a gVisor accommodation, it is
the product's default (`shadow_volumes` + `caches`, which also keep a box-installed package off
the host tree — [configuration.md](./configuration.md)), and it removes the dominant cost for BOTH
runtimes: the example consumer now ships that shape ([example/](../example/)). With it in place,
gVisor's remaining cost on this VM is ~1.6× on a test suite, ~5× on installs (seconds), 2–3× on
sub-second git/lint calls, and the weaker parallel scaling — all of it the syscall path.

**The platform lever, on this rig: no.** The same box with `--annotation
dev.gvisor.flag.platform=kvm` starts rootless (the guest's `/dev/kvm` is world-rw; the sandbox
runs `--platform=kvm`), and the shadow-shape warm-up run took **993 s against 201 s under
systrap** — 5× worse, not better. This is the nested-virt story again, not a gVisor one: on the
rig the sentry is a KVM guest inside a KVM guest inside Google's KVM, so every trap is an L2
exit with L0 round-trips, exactly libkrun's failure above. The number does not transfer to a
Linux laptop (one level less), where the kvm platform is what gVisor recommends for
performance; it is only measurable there. On macOS it does not apply at all (no nested virt on
M1/M2, and `③` is Linux-only). The remaining kvm rows were not run — the warm-up settles the
rig's answer.

### The route: a second, runsc-default API socket — proven on both hosts (2026-09-13, sixth session)

The design call from the fourth session is settled by measurement, not argument. Of the three
shapes, the second — **a second podman API service in the VM whose default runtime is runsc** —
was set up in BOTH machine VMs (the Mac's `foldyard` VM, arm64/vz, Fedora 44, podman 5.8.4; the
rig's `foldyard-example`, x86/QEMU, same guest) and driven by foldyard's real verbs from the host:

```sh
# in the guest, as the VM user — no root, nothing in the boot script
~/.local/bin/runsc                       # the release binary (release-20260817.0)
~/.local/bin/runsc-fy:                   #!/bin/sh
                                         exec ~/.local/bin/runsc --ignore-cgroups --host-uds=all "$@"
~/.config/containers/containers.conf:    [engine.runtimes]  runsc-fy = ["…/runsc-fy"]   # engine-wide NAME
~/.config/containers/runsc.conf:         [engine]  runtime = "runsc-fy"                   # this service's DEFAULT
systemd-run --user --unit podman-runsc --setenv=CONTAINERS_CONF_OVERRIDE=$HOME/.config/containers/runsc.conf \
  podman system service --time=0 unix:///run/user/$UID/podman/podman-runsc.sock
```

Then, on the host, `DOCKER_HOST=CONTAINER_HOST=<the runsc socket> FOLDYARD_ENGINE=podman fy box up`
— the unmodified verb, the unmodified `podman run` line. What that proved, on both hosts:

- **The box is a gVisor box** (`uname -r` = `4.19.0-gvisor`, `OCIRuntime = runsc-fy` in inspect),
  created in 16 s (Mac) / 24 s (rig, walls on), bootstrap and all — every step reports the same
  ✓/⏭/✗ as under crun. **Both sockets share one libpod store**: `fy ps`, `fy box ps`, `exec`,
  `stop`, `rm` through the DEFAULT socket see and drive the runsc box like any container (the
  runtime is recorded on the container; the only requirement is that the runtime NAME is
  registered engine-wide, i.e. in `containers.conf`, not only in the override — with it only in
  the override the default service says `runtime runsc-fy is missing` on exec/rm).
- **In-box `fy verify`: ALL PASS on the rig** (walls + proxy). On the Mac one row fails — `git
  remote UNREACHABLE for non-credential reasons` — because this checkout's origin is an SSH URL
  and the box image has no `ssh` client; the same box under crun reports the same (a property of
  the remote + image, not the runtime; the row is honest: unproven, not passed).
- **The wrapper is the flag boundary.** With `--host-uds=all` in the wrapper and NO
  `--allow-flag-override`, a client of the socket cannot reach runsc's flags: an annotation
  `dev.gvisor.flag.debug-log=/home/<vm-user>/x` (a write primitive on the VM disk) is refused —
  `flag override disabled, use --allow-flag-override`. runsc still honoured
  `dev.gvisor.flag.host-uds=none` from a client (a NARROWING; the socket in that container was
  refused), which is the safe direction. The earlier sessions' annotation route
  (`--allow-flag-override` + `--annotation host-uds=all`) would have handed every flag to
  whoever holds the socket — the box — so the product shape is flags-in-wrapper, override off.
- **A client of the runsc socket cannot opt out on 5.8**: the libpod create endpoint's
  `oci_runtime: "crun"` is ignored (the container came up `runsc-fy`). On podman 6 it would be
  honoured (fourth session) — so the socket the BOX holds is narrowed to strip `oci_runtime`
  (and `dev.gvisor.*` annotations) whatever the version; the runtime default is a convenience for
  the host's own `fy box up`, the filter is the enforcement. **Built (2026-09-13):** the box no
  longer mounts the runsc socket directly — it mounts a third, box-facing socket
  (`podman-runsc-filtered.sock`) served by a stdlib-Python filter
  (`assets/sandbox/socket_filter.py`, a fourth user unit) that forwards to the runsc socket and
  rewrites container-create bodies to drop `oci_runtime` + `dev.gvisor.*` (libpod) /
  `HostConfig.Runtime` (compat). It preserves keep-alive (so a create is filtered even as the
  second request on a reused connection — the bypass a naive proxy leaves), splices hijacked
  streams (attach/exec) raw, and fails closed (an unparseable create body is refused, never
  forwarded). Proven live on the Mac `foldyard` VM (podman 5.8.4): real creates round-trip under
  `runsc-fy` through the filter, a raw create carrying `oci_runtime: crun` is created gVisor and
  a malformed body gets a 400. **The strip itself was then shown live on podman 6.1.1**
  (2026-09-13, the Linux rig, a Fedora 45 nightly guest under Lima/QEMU — the first guest that
  honours the field): the same libpod create carrying `oci_runtime: crun` came up **crun** through
  the raw runsc socket (so on 6 the opt-out is real) and **`runsc-fy`** through the filtered socket
  (the strip fired — not the engine ignoring it), a malformed body still 400. Day to day
  `tests/test_socket_filter.py` pins it against a recording upstream, since the shipped guests are
  podman 5.8.
- **No Lima config change, no VM restart.** The service is a transient user unit started over
  `limactl shell`; the host reaches it either through an `ssh -L` unix-socket forward (what
  Lima's `portForwards` does — adding one there needs a stop/start) **or with no forward at
  all**: podman-remote accepts `CONTAINER_HOST=ssh://<vm-user>@127.0.0.1:<lima ssh port>/run/user/<uid>/podman/podman-runsc.sock`
  with `CONTAINER_SSHKEY=~/.lima/_config/user`, and `podman run` through it came up
  `4.19.0-gvisor`. Switching a box between runtimes is therefore `fy box down && fy box up`
  against the other socket — ~20 s, the VM untouched. (The name in that URI is Lima's ssh
  `User`, which is the host user name, not the guest home's `<user>.guest`.)

**The Mac's sustained workload, finally** (M3, Lima/vz, 4 vCPU / 8 GiB, virtiofs, this
repository, the shadow `.venv` volume, box created by `fy box up` each time, warm rows):

| workload (Mac, arm64/vz) | crun | runsc systrap | ratio |
| --- | --- | --- | --- |
| warm `uv sync --frozen` (venv in the shadow volume) | 6 ms | 14 ms | — |
| the suite, serial (1478 tests) | 55.5 s | 65.6 s | **1.18×** |
| the suite, `-n 4` | 19.4 s | 28.5 s | 1.47× |
| `ruff check .` | 0.021 s | 0.051 s | 2.4× |
| `git status` ×50 | 1.21 s | 2.62 s | 2.2× |
| `git grep` ×20 | 0.32 s | 0.76 s | 2.4× |
| fork/exec ×2000 (`/bin/true`) | 0.46 s | 1.92 s | 4.2× |
| 5000 small files on `/tmp` | 0.13 s | 0.16 s | 1.3× |
| 5000 small files on the virtiofs mount | 1.86 s | 3.56 s | 1.9× |

**The rig, same session** (x86 nested, 2 vCPU, 9p, the fifth session's shadow shape, the box
created by the real `fy box up` through the runsc socket; crun rows are the fifth session's from
the same VM and image):

| workload (rig, x86/QEMU) | crun (5th) | runsc (6th) | ratio | runsc (5th, hand-made box) |
| --- | --- | --- | --- | --- |
| warm `uv sync --frozen` (venv in volume) | 1.1 s | 1.4–1.6 s | 1.4× | 6.1 s |
| the suite, serial | 122 s | 242 s | 2.0× | 194 s |
| the suite, `-n 2` | 76 s | 208 s | 2.7× | 165 s |
| `ruff check .` | 0.14 s | 0.33 s | 2.4× | 0.39 s |
| `git status` ×50 | ~16 s | 31.7 s | 2.0× | (0.63 s each) |
| fork/exec ×2000 | 2.8 s (4th) | 31.3 s | 11× | 30 s (4th) |

Read the two together: the ratios that are about the sentry's syscall path (git walks, ruff,
fork/exec) agree across hosts; the suite's ratio is **1.2× on the Mac against 2.0× on the rig**
(1.6× in the fifth session — the rig's runsc rows moved by a quarter between two sessions on the
same image, which is the noise floor of a nested cloud VM, so the Mac number is the clean one).
On the Mac, gVisor's cost for an agent loop is a fifth on the suite and 2–4× on sub-second git
and lint calls — well inside the "under 2× on the realistic items" verdict, and the Mac was the
host the ③ decision had written off (systrap needs no KVM, so M1/M2 are reachable too).

### Bind mounts under krun

uid mapping is the same as crun (guest root = host uid 1000 files); `rw` works. **inotify does
not cross the boundary inwards**: a watcher in the krun guest sees guest-side writes but never
host-side writes (crun sees both); host-side inotify sees guest writes fine. Dev servers with file
watching inside the box only see edits made inside the box — the foldyard model anyway (the
agent and attached VS Code both write from inside), so a caveat, not a blocker.

### Lima + QEMU on Linux (foldyard's own `lima` backend)

First-ever run of `foldyard machine ensure` with the lima backend on Linux, against `example/`:

- **Works.** `resolve_vmtype()` gave `qemu`; `template://podman`'s deprecation warning appeared as
  expected. It needed `qemu-img` on the host (not pulled in by `qemu-system-x86-core`); the first
  attempt died on that with an error that blames Apple Virtualization — a Mac-flavoured message
  on Linux, one of the legacy strings the sweep owes.
- `/dev/kvm` is present inside the Lima guest (Lima passes `-cpu host`; host `nested=Y`), which
  is what made the three-level run possible.
- `mountType` resolves to **9p**. 9p costs 4.9 s for 3,000 small files vs 0.05 s on tmpfs.
  **Pinning `virtiofs` is not usable on Linux yet**: Lima 2.2.0 warns it is experimental, mounts
  it, reads work, and **every inode create returns `EINVAL`** (`openat(O_CREAT)`, `mkdir`);
  append, chmod and rename of existing files work. Same on a Fedora 44 guest (SELinux enforcing
  AND permissive) and an Ubuntu guest, so not SELinux; the rootless `virtiofsd` 1.14 logs "Failed
  to open file handle for the root node: Operation not permitted … disabling file handles
  altogether". Unexplored: `virtiofsd` as root, or a newer version.
- **The wall works on Linux** (`machine ensure` only, no `fy up`): `fy_wall` nftables installed
  in the QEMU guest, direct `https://example.com` from inside the VM rejected (`curl` rc 7), DNS
  to the local resolver still resolves. Driver-independence holds off macOS.
- **`fy verify` on Linux: ALL PASS**, with `_host_paths()` deriving `/home/dain` as designed —
  and one of those passes is false; see the caveat under
  [What `fy verify` proves](#what-fy-verify-proves-per-platform).

### Not measured yet

- Windows Server + WSL2 on GCP: `/dev/kvm` in the distro, and "WSL2 has no KVM" versus
  "three-deep nesting through Hyper-V failed" — the two need distinguishing.
- A libkrunfw rebuild with `CONFIG_X86_X2APIC` (the one libkrun out worth a compile; ≤ a quarter
  of the exits, so it does not close the gap on its own). gVisor's agent-loop cost is now measured
  (above).
- `fy up` on Linux beyond the host-wall run (only `machine ensure`, `verify` and the host-wall
  probe ran before it); the box e2e on the rig.

## Still open

- **krunkit as a posture**, not a probe: a sustained workload on Lima + krunkit, and a decision
  on carrying an experimental driver at a moving Cellar path. Until then it stays opt-in.
- **Host-side wall enforcement on Linux**: wired and run end to end on the rig 2026-09-12
  (`[machine].host_wall`, above). Still open: the sudoers policy is the operator's (a root prompt
  per `fy up` otherwise), `fy up` proper with the proxy running (the rig run was `machine ensure`
  + `verify`), and WSL2 is unmeasured.
- **Socket narrowing**: BUILT for the runtime opt-out (2026-09-13) — the box-facing filter that
  strips `oci_runtime` / `dev.gvisor.*` from creates (above, and the `③` bullet below). The
  broader "narrowing shape" from Finding 1 (a mount allowlist so a box-created sibling can't
  bind-mount the VM's `/`, and an endpoint allowlist) is a larger, separate guarantee left for
  the ADR's scope, not this pass.
- **`③` reassessment**: periodic, on the same fork/exec, package-install and `git status` loops.
  libkrun needs its 2.0 line driven by a newer crun AND x2APIC in libkrunfw before it is worth
  re-measuring; gVisor's cost is now measured (~3× on git walks, on the x86 rig AND on Mac
  arm64/vz — directfs on) and it is the front-runner. `--ignore-cgroups` is settled (the box sets
  no per-container limits, so it costs nothing); `host-uds=all` is settled the other way (it is
  the unfiltered socket — socket narrowing is a precondition, not an alternative). gVisor as the
  real box runtime and a sustained build are measured on the rig (2026-09-13, above: suite 1.6×,
  fork/exec 11×, git walks ~1×; the install's 2.5× was the venv on the 9p mount — in the shadow
  volume foldyard actually uses it is 6 s vs 1 s, and the suite's 1.6× is the syscall path, not
  the mount: 1.7× on gVisor's own tmpfs — the fifth session, above). The Mac's long build is
  measured (1.2× on the suite, sixth session) and the **runtime-selection route is settled: the
  second, runsc-default API socket**, proven with the unmodified `fy box up` on both hosts, no
  VM restart, reachable over Lima's ssh with no forward. Decided 2026-09-13: it is a MACHINE
  posture (the box must not be able to opt itself or a sibling out — so the box's socket is the
  narrowed runsc one, and the filter strips `oci_runtime` / `dev.gvisor.*`), on the Mac too, and
  wired together with socket narrowing as one opt-in safety option. **Wired (2026-09-13):
  `[machine].runtime = "gvisor"`** — `machine ensure` provisions the second socket in the guest
  (both backends, over their own ssh), `fy box up` creates through it and hands the box that
  socket as its own, fail-closed both ways, `fy verify` checks the kernel (docs/configuration.md).
  **Narrowing filter wired (2026-09-13):** the box mounts a third, box-facing socket served by a
  stdlib filter that strips the runtime opt-out (`oci_runtime` / `dev.gvisor.*` / compat
  `HostConfig.Runtime`) from every create and fails closed — so a sibling cannot escape gVisor
  even on a podman ≥ 6 that honours a client-chosen runtime (above; `tests/test_socket_filter.py`;
  live on the Mac VM, and the strip itself shown live on a podman 6.1.1 guest on the rig).
  **The ③ ADR is written: [ADR-0025](./adrs/0025-gvisor-machine-posture-and-socket-narrowing.md)** —
  the posture and the runtime filter Accepted; the broader mount/endpoint allowlist recorded as
  deferred, with the inotify posture (polling; no two-way sync) decided in it.
- **Linux mounts**: `virtiofs` under Lima on Linux fails every file create (above); 9p until
  upstream moves.
- **WSL2**: still unmeasured — `/dev/kvm` in the distro, and Lima+QEMU inside it.
- **Closed, not open:** per-session pristine state via snapshots (not wanted, 2026-09-11); box-only
  krun on M3 (krun is Linux-only by the M1/M2 decision, and deferred there; `③` is gVisor, which
  needs no KVM).

## Sources

Measured 2026-09-07 on M3 Max / macOS 26.6.1 / podman 6.0.2 / Lima 2.1.3 — the
[Measured on macOS](#measured-on-macos-2026-09-07) section, including the krunkit probe. Why
Firecracker itself is out: [firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md).

Measured 2026-09-11 on GCP `n2-standard-4` with nested virtualisation / Fedora 44 / kernel 7.1.10 /
podman 5.8.4 / crun 1.28 + `crun-krun` / libkrun 1.19.0 (and 1.19.4 from source) / libkrunfw
5.5.0 / Lima 2.2.0 / `virtiofsd` 1.14 / gVisor `runsc` release-20260817.0 — the
[Measured on GCP](#measured-on-gcp-2026-09-11) section. crun's krun handler, for the vsock finding:
<https://github.com/containers/crun/blob/main/src/libcrun/handlers/krun.c>.

Measured 2026-09-12 on the same rig (third session): gVisor's agent-loop cost (git/node/python
walks), the host-side cgroup wall against the running `foldyard-example` VM, and `fy verify`
itself against a deliberately leaky VM — folded into [The outs, measured](#the-outs-measured) and
[verify-false-pass.md](./verify-false-pass.md).

Read 2026-09-11: libkrun README (security model, 2.0 status)
<https://github.com/libkrun/libkrun> (moved out of the `containers` org; old URLs redirect) ·
crun's krun handler <https://github.com/containers/crun/blob/main/krun.1.md> · WSL2 stock-kernel
KVM: <https://www.boxofcables.dev/build-a-custom-wsl-2-kernel-in-2026/>,
<https://www.boxofcables.dev/accelerated-kvm-guests-on-wsl-2/>, Windows 10 override
<https://github.com/microsoft/WSL/issues/40735> · the prompt for the re-read:
<https://rywalker.com/research/libkrun>.
