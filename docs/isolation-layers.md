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
| microVM device model | ⚠ krunkit only | ❌ (QEMU) — `(c)` would add one *under* it; deferred | ⚠ `(b)` only |
| VMM jailed | ❌ unconfined operator process | ✅ crun, for `③` only | ✅ crun, for `(b)` |

`native` must never advertise the VM-boundary claims. That is not a documentation nicety: it is
the difference between a checked property and a slogan.

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
- **Socket narrowing**: the pod-loopback filter shape is proven; the filter itself is not built.
- **`③` reassessment**: periodic, on the same fork/exec, package-install and `git status` loops.
  libkrun needs its 2.0 line driven by a newer crun AND x2APIC in libkrunfw before it is worth
  re-measuring; gVisor's cost is now measured (~3× on git walks) and it is the front-runner —
  what remains is a decision on `host-uds` (the socket) and `--ignore-cgroups` for the box, plus
  a sustained build rather than micro-loops.
- **Linux mounts**: `virtiofs` under Lima on Linux fails every file create (above); 9p until
  upstream moves.
- **WSL2**: still unmeasured — `/dev/kvm` in the distro, and Lima+QEMU inside it.
- **Closed, not open:** per-session pristine state via snapshots (not wanted, 2026-09-11); box-only
  krun on M3 (`③` is Linux-only by the M1/M2 decision, and deferred there).

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
