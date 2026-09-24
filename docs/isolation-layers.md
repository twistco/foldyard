# Where the boundary lives, per platform

foldyard stacks up to three isolation layers, and **which layer carries the weight differs by
host**. This page says which, per platform, so a uniform-looking config doesn't hide a real
difference. Terms used here are defined in the [glossary](./glossary.md).

The one sentence that explains the rest: **on macOS the VM is a necessity; on Linux it is a
choice.** Containers need a Linux kernel, so a macOS host must run a VM to have containers at all — the
isolation comes with it. A Linux host already has the kernel, so its VM exists *only* to be a
boundary, and has to justify its cost on that alone ([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)
settled that it does: foldyard always runs one).

The dated measurements behind this page — the rig sessions, the benchmark tables, the probes —
are in [archive/isolation-layers-sessions.md](./archive/isolation-layers-sessions.md).

## The three layers

```
  ┌─────────────────────────────────────────────────────────────────┐
  │ HOST            credentials · supervisor · token services       │
  │                 allow-store · the egress proxy · your editor    │
  └───────────────────────────┬─────────────────────────────────────┘
                              │  ① machine layer  (the VM)
                              │     mounts: repo + worktrees root ONLY
                              │     the VM firewall (in-VM nftables)
  ┌───────────────────────────┴─────────────────────────────────────┐
  │ VM              rootless podman  ·  libpod socket ──▶ the host  │
  └───────────────────────────┬─────────────────────────────────────┘
                              │  ② container layer  (the engine)
  ┌───────────────────────────┴─────────────────────────────────────┐
  │ dev box + the compose stack     ③ optional: gVisor under the box│
  │ agents run here                    ([machine] runtime = "gvisor")│
  └─────────────────────────────────────────────────────────────────┘
```

### Legend

Older docs, ADRs and code comments use the numbered and lettered shorthand; this page uses the
names.

| shorthand | name here | what it is | config |
| --- | --- | --- | --- |
| ① | **machine layer** | the per-project VM: its own kernel, mounts only the repo and worktrees root | `[machine] backend`, `vmtype` |
| ② | **container layer** | ordinary rootless containers on the VM's kernel: the box and the stack | — |
| ③ | **sandbox layer** | gVisor's userspace kernel under the box, so a container→kernel exploit must beat gVisor's Sentry before it reaches the VM kernel | `[machine] runtime = "gvisor"` ([ADR-0025](./adrs/0025-gvisor-machine-posture-and-socket-narrowing.md)) |
| (a) | **Lima/QEMU VM** | Linux's shape: the machine layer on QEMU/KVM, containers share its kernel | `backend = "lima"` (vmType `qemu`) |
| (b) | **microVM per container** | no VM, each container in its own libkrun microVM — **retired** with the `native` backend ([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)) | — |
| (c) | **stacked microVM** | (a) with the box in a libkrun microVM inside it — measured and **deferred** on cost | — |

Before ADR-0025 (2026-09-13), ③ meant a libkrun microVM (`--runtime krun`); it now means
gVisor. The libkrun route is the deferred stacked microVM (c).

The same layers as a hardening ladder — one ring per step, each a line in `foldyard.toml`, with
the egress setting alongside:

![Foldyard isolation layers: four cumulative steps — a rootless Podman VM, Lima with the
VM firewall, gVisor under the dev box behind a narrowed engine socket, and the host firewall on
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
        │                  └── Lima  vmType: krunkit          ← opt-in
        │
        └── Virtualization.framework ........ Apple's own VMM + device model
               └── vfkit / Lima vz driver     [com.apple.security.virtualization]
                     └── Lima  vmType: vz                     ← the default
```

**The hypervisor is the same either way.** Choosing krunkit over vz swaps the *userspace* VMM and
device model, not what enforces isolation in hardware. That bounds both the risk and the benefit:
the device-model half shrinks, the hypervisor half is untouched.

**A macOS host runs neither QEMU nor Linux KVM** — the two components the agent in Trail of Bits' report
actually escaped ([archive/firecracker-and-microvm-backends.md](./archive/firecracker-and-microvm-backends.md)).
Moving macOS hosts to QEMU "for uniformity with Linux" would be backwards: Lima's QEMU driver on macOS
uses the same Apple hypervisor (`hvf`) and *adds* QEMU's device model on top — more attack
surface, and much slower. One honest caveat on vz: Apple's implementation is closed, so its low
public CVE count is partly an absence of research.

**Neither path jails the VMM.** libkrun's own README says the guest and the VMM share a security
context, and that isolating them is the host OS's job. Virtualization.framework also runs the VM
inside the calling process, so Lima's vz host agent is in the same position. A VMM escape
therefore lands in an unconfined process running as you, with your home directory, the login
keychain, `~/.foldyard` and the whole network in reach. **The VM firewall does not survive a VMM
escape** — it confines the guest kernel, not the host process running it.

What is open on macOS:

- **No VMM jailer, and no obvious way to add one.** macOS has no namespaces; the only candidate,
  `sandbox-exec`, is deprecated and neither Lima nor krunkit applies it.
- **No host firewall.** Lima's user-mode network runs as you, and pf cannot single that process
  out without a dedicated uid or a different network mode. `hostwall.available()` reports the
  capability absent rather than branching on the OS.
- **No microVM for the box.** A libkrun box needs `/dev/kvm` inside the VM, which Apple silicon
  offers only on M3+ with macOS 15+; M1/M2 must be supported, so the stacked microVM is Linux-only.
  The sandbox layer is gVisor instead, which needs no KVM and runs on M1/M2. On macOS the chain
  is host ← VMM ← guest kernel ← gVisor ← box.
- **krunkit stays opt-in.** A smaller device model, but upstream-experimental, installed at a
  Homebrew path that moves with each upgrade, and never run under a sustained workload. foldyard
  never selects it automatically ([configuration.md](./configuration.md)).

## Linux — the machine layer is QEMU, and QEMU is the price

```
   (a) LIMA/QEMU VM — what ships          (b) MICROVM PER CONTAINER — retired
   backend = "lima", vmType: qemu         (was backend = "native" + --runtime krun)

   host ──① QEMU/KVM VM                   host ── (no VM)
            └─② containers                         └─② containers, each in a
               share the VM kernel                     libkrun microVM with
                                                       its own kernel
   ✅ repo-only mount                     ❌ engine sees the whole host filesystem
   ✅ VM firewall (in-VM nftables)        ❌ no VM to put the firewall in
   ✅ host firewall (on the QEMU process) ❌
   ✅ concurrent per-project VMs          ✅ per-container kernel isolation
   ❌ QEMU device model                   ✅ minimal device model
```

foldyard always has a VM ([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)), so on
Linux the shape is (a). The (b) column stays as the record of what it traded away.

**Lima on Linux does not give you libkrun.** Lima registers only `qemu` there; `krunkit` is a
macOS/arm64 binary. So Lima on Linux buys the boundary, the mount and the firewalls, with QEMU as
the knowingly accepted weak link — not uniformity with macOS.

**The mount is 9p, and can't be pinned to virtiofs yet.** Lima's QEMU driver defaults
`mountType` to 9p, which is slower on the build path and the more escape-prone of the two. But
under Lima 2.2.0's rootless `virtiofsd` 1.14, every file create on a virtiofs mount fails with
`EINVAL` (Fedora and Ubuntu guests alike), so 9p stays until upstream moves. Keep caches and the
venv off the mount (`[box] shadow_volumes`, `caches` — [configuration.md](./configuration.md));
the mount is the dominant cost under any runtime.

### Lima + QEMU on Linux (foldyard's own `lima` backend)

What the `lima` backend does on a Linux host (first measured 2026-09-11; now run on every
`lima-host-e2e` and `wsl2-host-e2e` CI job — the CI section of [DEVELOPMENT.md](../DEVELOPMENT.md)):

- `resolve_vmtype()` picks `qemu` from `limactl info`, not from the OS.
- The host needs `qemu-img` (package `qemu-utils` / `qemu-img`), which `qemu-system-x86-core`
  does not pull in.
- `/dev/kvm` is present inside the guest (Lima passes `-cpu host`, and nested KVM is on by
  default on x86), which is what makes a stacked microVM possible at all.
- The mount is 9p (above).
- The VM firewall works unchanged: direct egress from the guest is refused, DNS to the local
  resolver still resolves. The boot script is driver-independent.
- `fy verify` is ALL PASS, and FAILs against a VM deliberately mounting the whole host home
  (`tests/test_verify_e2e.py`).

### The engine socket is a hole by design

The (b) column lost the repo-only mount and the firewall not because there was no VM, but
because **the box holds the engine socket**, and a libpod socket can bind-mount anything its uid
can read and reach anything its host can reach. The VM never created those properties; it
bounded what the socket could get at. So:

- Whatever the socket allows, an agent in the box can do with no exploit at all. A microVM or
  gVisor removes the *exploit* route (the shared kernel); narrowing the socket removes the
  *by-design* route (the API). Neither substitutes for the other.
- **Built:** under `[machine] runtime = "gvisor"` the box holds a filtered socket that strips
  every runtime opt-out (`oci_runtime`, `dev.gvisor.*`, compat `HostConfig.Runtime`) from
  container creates and refuses what it can't parse, so a sibling can't escape gVisor
  ([ADR-0025](./adrs/0025-gvisor-machine-posture-and-socket-narrowing.md);
  `src/foldyard/assets/sandbox/socket_filter.py`).
- **Deferred:** a broader mount/endpoint allowlist, so a box-created sibling can't bind-mount the
  VM's `/`. On one VM per project that sibling reads only this project's VM, which holds no
  credentials, so it is defence in depth (ADR-0025, Decision §5).

### Hardening that ships, beyond the VM

In order of cost (all zero at runtime):

1. **Root in the guest is boot-time only.** The box runs as the VM user's uid, and Lima's
   cloud-init gives that user passwordless sudo on every boot — one `sudo nft flush ruleset` from
   open egress after a container-runtime escape. foldyard's boot script narrows the grant to
   `shutdown` and installs the VM firewall as root on every boot; the host never runs `sudo` in
   the guest ([lima-wall-machine-integration.md](./lima-wall-machine-integration.md#2-enforcement--machinewall--true)).
   After that, only a guest-*kernel* exploit reaches VM-root.
2. **The host firewall** (`[machine] host_firewall`, below) closes that last gap on Linux.
3. **The sandbox layer** (`[machine] runtime = "gvisor"`, [below](#the-sandbox-layer-gvisor))
   puts a second kernel between the box and the VM kernel.

What all three protect is bounded by the one property that holds everywhere: **real credentials
never enter the VM.** Even a VM-root escape steals none; it can only reach what the VM holds.

### Host firewall on Linux

`[machine] host_firewall = true` enforces the same egress rule *on the host*, where the guest has
no reach, so flushing the VM firewall gains nothing. Design reference:
[lima-wall-machine-integration.md §3](./lima-wall-machine-integration.md#3-host-side-enforcement--machinehost_wall--true-linux);
code: `src/foldyard/hostwall.py`. In short:

- **It matches the VM process by cgroup, not uid.** Lima's QEMU driver runs the guest's user-mode
  network inside `qemu-system`, so every guest packet leaves the host as that process. foldyard
  starts the VM in a per-VM scope under a persistent per-VM slice, and nftables matches the slice
  (`socket cgroupv2`); your other processes share your uid but not the slice.
- **The table is the same every boot.** Loopback flows the VM opens carry a per-project conntrack
  mark and are judged on the INPUT hook by the *listening* socket's cgroup: the VM's own plumbing
  (Lima's host resolver, the SSH forward, on ports Lima picks per boot) or this project's port
  range, nothing else. No port is read from a running VM.
- **You install it once; foldyard probes it on every `fy up`**
  ([ADR-0028](./adrs/0028-no-elevation-on-the-host-operator-applies.md)). `fy machine host-firewall`
  renders the table and a system unit and prints the `sudo` lines; foldyard never elevates. Each
  `fy up` then runs a probe inside the slice and refuses to start if the firewall isn't enforcing.
  A probe, not a read, because the table can't be read without root and a loaded table can hold a
  dead slice's id and match nothing.
- **Fail-closed.** Preflight refuses `host_firewall` without `firewall`, or on a host without
  `nft` and cgroup v2; `ensure` refuses a VM running outside its own scope-under-slice.
- **Not on WSL2:** the stock WSL2 kernel has no `CONFIG_NFT_SOCKET`, so the rule can't load and
  the option fails closed. The VM firewall is unaffected.

### The stacked microVM (c): measured, deferred

Nested KVM is on by default on x86, so a libkrun box inside the Lima/QEMU VM works:

```
   host ──① Lima/QEMU VM  (repo-only mount, the firewalls, concurrency)
            └─② crun container  (namespaces · cgroups · seccomp = the VMM's jailer)
               └─ libkrun microVM  ← the box, on its own kernel
```

It has the one thing no other shape here has: **the VMM is jailed**, because crun builds the
container first and boots libkrun inside it. But it was deferred on cost (2026-09-11): process
creation costs ~8 ms under nested libkrun against ~0.5 ms under crun, and every virtio-fs
metadata syscall is a 50–80 µs round trip, so `git status` runs 27× slower and a package install
7×. That is the box's whole workload. None of the tuning options moved it (split irqchip, newer
libkrun, more vCPUs); x2APIC in libkrunfw would help at most a quarter. Reassess when libkrun 2.0
is drivable by crun. The engine socket also doesn't cross virtio-fs into a microVM; a sidecar in
the same pod exposing it over pod-loopback TCP works. Numbers:
[archive/isolation-layers-sessions.md](./archive/isolation-layers-sessions.md#measured-on-gcp-2026-09-11).

## The sandbox layer: gVisor

`[machine] runtime = "gvisor"` runs the box, and everything the box creates, under gVisor's
`runsc` (systrap platform, directfs on). It is a machine setting, not a per-box one, so the box
can't opt itself or a sibling out. How it's wired: `machine ensure` installs a pinned `runsc`
user-level in the guest and starts a second podman API service whose default runtime is runsc;
`fy box up` creates the box through it and hands the box the filtered view of that socket
([ADR-0025](./adrs/0025-gvisor-machine-posture-and-socket-narrowing.md),
[configuration.md](./configuration.md)). In-box `fy verify` checks the box's kernel is gVisor's.

It needs no KVM, so it works on M1/M2 Macs and Linux alike. Measured cost against crun, warm, with
the venv in a volume as foldyard's box runs it:

| workload | macOS (M3, vz) | Linux rig (x86, nested QEMU) |
| --- | --- | --- |
| this repo's test suite, serial | 1.2× | 1.6–2.0× |
| `git status` / `git grep` | 2.2–2.4× | ~2× (1–1.2× on the warm 9p mount) |
| `ruff check` | 2.4× | 2.4× |
| fork/exec (`/bin/true` ×2000) | 4.2× | 11× |
| `uv sync --frozen`, warm | 6 → 14 ms | 1.1 → 1.4 s |

The cost is gVisor's syscall path (CPU), not the mount: off the mount the suite's ratio is the
same. The rig is one hypervisor level deeper than a laptop, so its ratios are upper bounds.

Things to know when you turn it on:

- **Keep directfs on** (the default): off, git walks go from ~3× to ~9×.
- **`--ignore-cgroups` costs nothing** here: the box sets no per-container limits; the VM's
  sizing is the ceiling.
- **inotify doesn't cross into the sandbox.** A watcher in the box sees the box's own writes but
  not edits from the host. File-watching dev servers need polling (`CHOKIDAR_USEPOLLING`,
  `nodemon -L`, `watchexec --poll`).
- **The `kvm` platform is not a speed-up under nesting**: on the rig it was 5× slower than
  systrap. On a bare-metal Linux host it may be faster; unmeasured.

## WSL2 — a Linux host whose Hyper-V boundary protects the wrong asset

```
   Windows ──[Hyper-V]── WSL2 distro  (the host, for foldyard)
                          │  supervisor · token services · allow-store · credentials
                          └──① Lima/QEMU VM ──② containers
```

The distro is not the boundary. The supervisor runs inside it, so the credentials live in the
distro, exactly as on bare Linux. Hyper-V separates Windows from the distro; it puts nothing
between an agent and those credentials. A VM-less engine in the distro would be bare Linux
without a VM, which is why that shape is gone ([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)).

**WSL2's options are the Linux options.** The stock WSL2 kernel builds KVM as modules, and with
nested virtualisation on, `/dev/kvm` is in the distro; foldyard's `lima` backend boots its
QEMU VM there unchanged. Measured in CI on 2026-09-17 (the `wsl2-host-e2e` job, a hosted Windows
Server 2025 runner, `nestedVirtualization=true` in `.wslconfig`): the full host tier passes.
Details: the CI section of [DEVELOPMENT.md](../DEVELOPMENT.md),
[linux-support.md](./linux-support.md#validated-on-a-linux-host).

Limits:

- **No host firewall.** The stock WSL2 kernel has no `CONFIG_NFT_SOCKET`, so
  `[machine] host_firewall` can't load and fails closed. The VM firewall runs in the guest and
  is unaffected.
- **Windows 10** silently overrides `nestedVirtualization`; **Windows on ARM** boots the distro at
  EL1, so KVM can never work there. Windows 11 enables nested virtualisation by default on x86;
  a real Windows 11 client machine has not been checked (`wsl --shutdown; wsl; ls -l /dev/kvm`).
- **Keep the checkout on the distro's own filesystem.** `/mnt/c` is automounted in the distro, but
  the VM mounts only what foldyard declares, so automount doesn't reach the boundary. A repo
  under `/mnt/c` would be 9p over drvfs with 0777 modes and CRLF endings.
- Lima's *Windows-side* `wsl2` driver is experimental and not the route.

## What `fy verify` proves, per platform

A boundary nothing checks is asserted on trust. `fy verify` derives the host's paths from the
real host (`verify._host_paths`), reads the VM's mount table from PID 1 (not a container's view),
and gates every absence check on a positive control — see
[archive/verify-false-pass.md](./archive/verify-false-pass.md) for how each of those was found.

| claim | checked by | macOS | Linux | WSL2 |
| --- | --- | --- | --- | --- |
| a VM boundary exists; the escape probe is refused | `fy verify` | ✅ | ✅ | ✅ |
| repo-only mount | `fy verify` (PID 1's mount table) | ✅ | ✅ | ✅ |
| VM firewall refuses direct egress (and `:53` to the internet) | `fy verify` in the box; the guest's boot report on every `fy up` | ✅ | ✅ | ✅ |
| host firewall enforcing | a probe on every `fy up`; `fy doctor` | ❌ not available | ✅ | ❌ kernel lacks it |
| box under gVisor | `fy verify` in the box (kernel release) | ✅ opt-in | ✅ opt-in | not tested |
| microVM device model | — | ⚠ krunkit only, opt-in | ❌ QEMU | ❌ QEMU |
| VMM jailed | — | ❌ runs as you | ❌ QEMU runs as you (the host firewall confines its network, not its files) | ❌ same as Linux |

Under gVisor the in-box mount audit reports N/A: the sibling it uses can't reach the VM kernel,
which is the point. `fy verify` on the host still runs it over the default socket.

## Where each claim is tested

| where | covers | can't cover |
| --- | --- | --- |
| CI `lima-host-e2e` (x86 Ubuntu 24.04 runner, Lima/QEMU) | the Linux host tier: `machine ensure`, both firewalls via `fy up`, box, worktrees, `fy verify` including the leaky-home FAIL | arm64 (no KVM on the arm64 runners) |
| CI `wsl2-host-e2e` (Windows Server 2025, WSL2, Lima/QEMU inside) | the same tier on WSL2; the host firewall test skips | a Windows 11 client |
| a macOS host, by hand | the default vz path, krunkit, gVisor on arm64 | anything Linux-only |
| a nested x86 Linux VM ([nested-virt.md](./nested-virt.md)) | the stacked microVM, gVisor's `kvm` platform, podman 6 guests | bare-metal numbers (one level deeper than a laptop) |
| any x86 Windows 11 machine | `/dev/kvm` in the distro — a two-minute read-only check | — |

## Still open

- **krunkit as a supported option**, not a probe: a sustained workload, and a decision on carrying an
  experimental driver at a moving Homebrew path.
- **A VMM jailer on macOS**: no candidate.
- **The broader socket allowlist** (mounts and endpoints), deferred in ADR-0025.
- **virtiofs under Lima on Linux**: fails every file create; 9p until upstream moves.
- **The stacked microVM**: reassess when crun can drive libkrun 2.0 and libkrunfw enables x2APIC.
- **gVisor**: the `kvm` platform on a bare-metal Linux host; WSL2.
- **Windows 11 client**: the `/dev/kvm` check.

Closed: per-session snapshots (not wanted); a krun box on M3 Macs (krun is Linux-only, and
deferred there; the sandbox layer is gVisor).

## Sources

- The measurements: [archive/isolation-layers-sessions.md](./archive/isolation-layers-sessions.md)
  (macOS 2026-09-07; the GCP rig 2026-09-11 to 09-13).
- Why not Firecracker: [archive/firecracker-and-microvm-backends.md](./archive/firecracker-and-microvm-backends.md).
- libkrun README (security model, 2.0 status): <https://github.com/libkrun/libkrun> · crun's krun
  handler: <https://github.com/containers/crun/blob/main/krun.1.md>,
  <https://github.com/containers/crun/blob/main/src/libcrun/handlers/krun.c>.
- WSL2 stock-kernel KVM: <https://www.boxofcables.dev/accelerated-kvm-guests-on-wsl-2/> · the
  Windows 10 override: <https://github.com/microsoft/WSL/issues/40735>.
