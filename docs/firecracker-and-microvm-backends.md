# Firecracker & the microVM question

Research note, 2026-09-07. Prompted by Trail of Bits,
["VMs won't contain cyber-capable agents"](https://blog.trailofbits.com/2026/08/26/vms-wont-contain-cyber-capable-agents/)
(2026-08-26), which reports an agent escaping a QEMU/KVM guest three times via 0-days in QEMU and
KVM, and *failing* to escape [Firecracker](https://github.com/firecracker-microvm/firecracker) —
"substantially harder targets, though not impenetrable". The question this note answers: could
foldyard use Firecracker, and where would it go?

**Nothing is decided here.** No ADR, no code. Sources are the upstream repos read at the commits
below, plus dated web checks.

| read | version |
| --- | --- |
| `firecracker-microvm/firecracker` | `1.18.0-dev` (main, 2026-09-07) |
| `lima-vm/lima` | v2.x main (2026-09-07) |

## Verdict, in one table

| question | answer |
| --- | --- |
| Would it work with podman? | Not as a peer of `podman machine`/Lima. Only via **Kata** or **crun's krun handler** as a per-*container* hypervisor — a different layer. |
| Lima replacement, or a different part of the stack? | It *could* be machine backend #4, and the `Backend` ABC would fit. The mount blocker is soluble at the Lima layer (reverse-sshfs) but only by re-adding the surface Firecracker deleted; **TAP-only networking needing root per VM** is the harder one. See [Could Lima drive Firecracker?](#could-lima-drive-firecracker). |
| Linux/WSL2, not just Mac? | **Inverted.** Firecracker is Linux + `/dev/kvm` only, x86_64/aarch64. There is no macOS port and there won't be one — it is a KVM consumer by construction. Adopting it would drop every consumer we actually have. |
| So is there nothing here? | There is. The microVM property foldyard wants is available today from **libkrun/krunkit**, which is Firecracker's code and philosophy plus the one device Firecracker refuses — and it runs on macOS *and* Linux. See [The thing that does fit](#the-thing-that-does-fit-libkrun--krunkit). |

## What Firecracker is, and which layer that puts it on

Firecracker is a **VMM** — the userspace half of a hypervisor. Its peers are QEMU,
Apple's Virtualization.framework, and libkrun. Its peers are *not* `podman machine` or `limactl`,
which are VM **managers**: image fetch, provisioning, mounts, port forwarding, DNS, an in-guest
agent, lifecycle state. Firecracker gives you a JSON-over-unix-socket API, a kernel, a rootfs
image you supply yourself, and nothing else. `LimaBackend` is ~150 lines because `limactl` does
all of that work; a `FirecrackerBackend` would have to *be* that work.

That distinction matters for foldyard because our backend contract
([ADR-0011](./adrs/0011-machine-backends-one-socket-contract.md)) is a **libpod socket**. Getting
one out of a Firecracker microVM means building the guest that serves it: kernel, rootfs with
podman + systemd, socket forwarding over vsock, provisioning for `[machine].wall`. That is
Lima's job description, re-implemented.

## The blocker: Firecracker has no filesystem sharing, deliberately

The full device list at `1.18.0-dev`, from `docs/device-api.md` and confirmed against
`src/vmm/src/devices/virtio/`:

> keyboard · serial console · virtio-block · vhost-user-**block** · virtio-net · virtio-vsock ·
> virtio-rng · virtio-pmem · virtio-mem

No virtio-fs. No 9p. `vhost_user.rs` is block-only, so it is not a side door. From `docs/design.md`:

> "Firecracker emulated block devices are backed by files on the host. To be able to mount block
> devices in the guest, the backing files need to be pre-formatted with a filesystem that the
> guest kernel supports."

This is not a gap awaiting a patch.
[Issue #1180, "Host Filesystem Sharing"](https://github.com/firecracker-microvm/firecracker/issues/1180)
has been open since 2019-07-15; the p9 implementation was rejected on security grounds and the
virtio-fs PR ([#1351](https://github.com/firecracker-microvm/firecracker/pull/1351)) never landed.
The charter is that every device is attack surface — which is *precisely why* the Trail of Bits
agent could not escape it. You do not get the escape resistance without the missing device; they
are the same fact.

Now put that against `machine._volumes()`:

```python
def _volumes(main: Path, wt_root: Path) -> list[tuple[str, str]]:
    """The isolation mount set as ``(host, guest)`` pairs (host==guest path) — the ONLY
    things the VM may see. REPLACES the backend's default mounts."""
```

The repo mount **is** foldyard's isolation claim. `verify` asserts on the guest mount table; the
whole product proposition is "edit on the host with your own editor, the yard sees the repo and
nothing else". Firecracker's substitutes are all bad here:

- **A block image of the repo** — you would be copying the checkout into an ext4 file, losing live
  host-side editing. That is not the dev loop.
- **NFS or virtio-fs-over-vsock from the host** — re-adds, in a host-side daemon reachable from the
  guest, exactly the complexity Firecracker deleted. You would pay Firecracker's cost and forfeit
  its benefit.
- **virtio-pmem with a repo image** — file-backed and fast, but still an image, and read-mostly.
  Same objection as the block route.

## Where it *could* plug in, honestly assessed

**(a) Machine backend #4 — replacing Lima.** The `Backend` ABC fits on paper: `exists`/`state`/
`create`/`start`/`stop`/`remove`/`mounts`/`socket`/`guest_socket`/`list_running` +
`supports_concurrent`. Concurrency would be free (`True` — one process per microVM, no
`RequireExclusiveActive` analogue). But you inherit: writing the VM manager Lima already is,
losing macOS entirely, and `mounts()` having no implementation. Not viable.

**(b) Per-container microVM *inside* the machine VM.** This is the shape our
[prior-art table](./prior-art.md) already attributes to Docker Sandboxes ("microVM per *agent*"),
and it is *additive* rather than adversarial: the repo mount stays a Lima virtio-fs mount into the
machine VM, and only the box / stack containers gain a second VM boundary underneath it. Two
routes:

- **Kata Containers with the FC hypervisor** (`podman --runtime kata`). Real, but heavy: Kata+FC
  requires containerd's **devmapper** snapshotter because — again — [virtio-fs is not available on
  Firecracker, so the container rootfs must be a hot-plugged block
  device](https://blog.cloudkernels.net/posts/kata-fc-k3s-k8s/). Bind-mounting the repo into the
  box hits the same wall one layer down.
- **crun's krun handler** (`podman run --runtime krun`) — same idea, far less machinery, and it
  uses libkrun rather than Firecracker, so virtio-fs *is* available. See below.

Either way this needs **nested KVM**, which [nested-virt.md](./nested-virt.md) tells us is present
inside the machine VM on Apple Silicon M3+ / macOS 15+ under libkrun, and absent on M1/M2. So it
would be a capability some consumers have and others don't — an awkward thing to hang a security
claim on.

**(c) Nothing.** The honest default, unless (b) via krun turns out cheap.

## Platform reality inverts the ask

`docs/getting-started.md`: "Firecracker supports **x86_64** and **aarch64 Linux**", and "requires
read/write access to `/dev/kvm`". There is no Hypervisor.framework backend and no plan for one.

So Firecracker would not extend us to Linux/WSL2 *in addition to* macOS — it would move us to
Linux/WSL2 *instead of* macOS. WSL2 does expose KVM with nested virt, so it is not absurd there;
but every consumer created by the supported path today is on a Mac with Lima.

Worth separating two things the question ran together. The real Linux/WSL2 gap today is not the
VMM — it is that those hosts fall to `backend = "native"`, which **has no VM boundary at all**
(ADR-0011). A microVM backend would genuinely close that gap. It just doesn't have to be
Firecracker, and it shouldn't be.

## The thing that does fit: libkrun / krunkit

[libkrun](https://github.com/containers/libkrun) is a VMM shipped as a *library*: any process
links it and gets a hardware-isolated VM from a C call. Three facts make it the interesting answer
to this question rather than a tangent:

1. It "**incorporates code from Firecracker, rust-vmm and Cloud-Hypervisor**" and shares the
   minimal-device-model philosophy — the property the Trail of Bits result is actually about.
2. It runs on **KVM (Linux) *and* HVF (macOS/ARM64)** — so it covers the platform Firecracker
   never will, which is the platform we're on.
3. **It has virtio-fs** (plus console, block, gpu, net, vsock, balloon, rng) — the one device
   Firecracker declines, and the one foldyard cannot do without.

It also arrives pre-integrated at both layers we care about:

- **Machine layer:** `krunkit` is already a `podman machine` provider on macOS (the nested-virt rig
  assumes it), and Lima ships a `krunkit` driver. Caveats, read from the Lima tree: the driver is
  `darwin_arm64`-only, shipped as an **external** gRPC driver plugin, and documented as
  *experimental*.
- **Container layer:** `podman run --runtime krun` gives a microVM per container with no Kata /
  containerd / devmapper apparatus at all.

### We may already be running it — on the wrong VM

A `krunkit` process on a developer's Mac while a **podman machine** is up means that machine is
a libkrun microVM. That is not exotic here: `nested-virt.md` *instructs* setting
`provider = "libkrun"` persistently for the nested-KVM rig, and a machine's provider is fixed at
init, so any machine created that way stays on libkrun no matter what the current default is.
(Upstream's macOS/arm64 default has been moving toward libkrun too — version-dependent, worth
checking rather than assuming: `podman machine info --format '{{.Host.VMType}}'`.)

The implication is worth stating plainly, because it changes the cost of everything above:

- The microVM posture this note recommends is **not hypothetical for us** — libkrun already runs
  on the maintainer's Mac, with a working virtio-fs mount and nested virt, and `krunkit` is
  already installed.
- But it is running under the **podman** backend, i.e. in the validation rig — while real project
  work happens on the **lima** backend, which resolves to `vz`. So today the microVM is in the
  test harness and the ordinary VMM is in the product. That is exactly backwards from a
  hardening point of view.
- Which makes open question 1 (does Lima's `krunkit` driver satisfy the backend contract?) much
  cheaper to answer than it looked: the hypervisor is installed and proven on the machine, so the
  experiment is `limactl start --vm-type=krunkit` against the podman template, not an
  installation project.

Do not read this as "we already have it". Two different VM stacks are involved, and the property
only counts where the work happens.

The trade is visible and worth stating: libkrun is *less* minimal than Firecracker — virtio-fs and
virtio-gpu are attack surface Firecracker won't carry, and virtio-fs specifically is where a
shared-filesystem VMM has historically been attacked. We would be buying most of the microVM
posture, not all of it, in exchange for a product that still works.

## One thing to check about what we run today

`LimaBackend.create()` uses `template://podman` and a `--set` expression that pins cpus / memory /
disk / mounts. It does **not** pin `vmType`. Lima's `DefaultDriver()` (`pkg/limatype/lima_yaml.go`)
is:

```go
func DefaultDriver() VMType {
	switch runtime.GOOS {
	case "darwin":
		return VZ
	default:
		return QEMU
	}
}
```

So a foldyard consumer is on **Virtualization.framework** on a Mac, and on **QEMU/KVM** on a Linux
host — the exact VMM the Trail of Bits agent escaped three times. Nobody chose that; it is a
default we never expressed an opinion about. Whatever we conclude about microVMs, `[machine].vmtype`
threaded into the `--set` expression looks like the cheapest real hardening available, and it makes
the VMM a decision with an owner rather than an accident of `runtime.GOOS`.

## Could Lima drive Firecracker?

Yes, in principle — and this refines the "no filesystem sharing, therefore no" reasoning above,
which was too absolute *at the Lima layer*.

Lima v2 has an **external driver plugin API**: a `lima-driver-<name>` executable speaking gRPC,
discovered via `LIMA_DRIVERS_PATH` or `<prefix>/libexec/lima/`. `krunkit` already ships this way,
so the path is real rather than theoretical. A driver implements `Lifecycle` + `GUI` +
`SnapshotManager` + `GuestAgent` plus `Configure`/`Validate`/`SSHAddress`
(`pkg/driver/driver.go`). Upstream calls the API **experimental**.

**The mount problem has a Lima-layer answer.** Lima's mount types are `virtiofs`, `9p`,
`reverse-sshfs` and `wsl2` — and `reverse-sshfs` is implemented in `pkg/hostagent/mount.go`, i.e.
in the **host agent over the SSH connection**, not in any driver. It is driver-agnostic by
construction; the vz driver accepts exactly `virtiofs` or `reverse-sshfs`. So a Firecracker driver
could serve foldyard's repo mount over sshfs despite having no filesystem-sharing device.

That is a real answer, and it is also the objection restated concretely: you would be running an
sshfs daemon with access to the checkout, reachable from the guest, to replace a device the VMM
omitted for attack-surface reasons. Plus the performance floor — reverse-sshfs is Lima's slowest
mount type, and the repo is the hot path for every build in the yard.

**The harder blocker is networking.** From `docs/network-setup.md`: "Firecracker supports only a
TUN/TAP network backend". There is no user-mode networking — no slirp, no gvproxy, no vmnet
equivalent. The documented setup is:

```bash
sudo ip tuntap add tap0 mode tap
sudo ip addr add 172.16.0.1/30 dev tap0
sudo ip link set tap0 up
echo 1 | sudo tee /proc/sys/net/ipv4/ip_forward
sudo nft add rule firecracker postrouting ip saddr 172.16.0.2 oifname eth0 counter masquerade
```

Every one of those needs root. foldyard creates a VM **per project, on demand**, from `fy up` —
so this is `sudo` in the middle of the ordinary dev loop, for every project, forever. Against a
tool whose entire pitch is a rootless boundary, that is worse than the mount problem.

(Note the irony in that snippet: Firecracker's own network doc has you writing nftables NAT on the
**host**, which is architecturally the same trick as `[machine].wall` — one layer out.)

So: buildable, by someone, at the cost of an unmaintained experimental gRPC driver, a slow mount,
and root in the hot path. Not a thing to build for a security property that libkrun already offers
without any of the three.

## Firecracker only removes half of what the post broke

Worth stating plainly, because the post's framing can obscure it: **Firecracker runs on KVM.**

Trail of Bits report escapes via "three 0-day exploits in QEMU **and Linux KVM**". Firecracker
replaces the QEMU half — the ~2M-line userspace device model where nearly every public VM-escape
CVE lives (an emulated device parses an attacker-controlled descriptor and turns it into host
memory corruption: CVE-2015-5165, CVE-2015-7504, CVE-2020-14364, and so on). It does **not**
replace the KVM half, which is identical under both. Consistent with this, the post's Firecracker
result is *hardlocks through kernel flaws* but no escape — the agent still reached kernel bugs; it
just could not get from there to the host.

That surface is live. 2026 alone: ITScape (CVE-2026-46316, KVM/arm64), Januscape (CVE-2026-53359,
KVM/x86 — undiscovered for 16 years), Zapscape (CVE-2026-64561). A microVM is a smaller target,
not a different kind of target.

## Should the Mac move to QEMU? No — that is backwards

The Linux-defaults finding above is easy to misread as "QEMU is what we're stuck with, so pin it".
The opposite: **QEMU is the thing to get away from, and on macOS we already have.**

| | host VMM | kernel-side | device model |
| --- | --- | --- | --- |
| macOS + `vz` (today) | Virtualization.framework | Hypervisor.framework | Apple's, minimal virtio set |
| macOS + `qemu` | QEMU + `hvf` accel | Hypervisor.framework | **QEMU's, in full** |
| Linux + `qemu` (today) | QEMU | **KVM** | **QEMU's, in full** |

Lima's QEMU driver on macOS uses the `hvf` accelerator (`pkg/driver/qemu/qemu.go`), falling back
to `tcg` software emulation for non-native arch. So switching a Mac from vz to QEMU would keep the
same Apple hypervisor underneath and **add** QEMU's device model on top of it — strictly more
attack surface, for nothing, plus a large performance regression. Lima's own vmType flowchart
reserves QEMU-on-macOS for one case: running Intel VMs on ARM, which foldyard does not do.

The honest caveat on vz: Apple's implementation is closed, so its low public CVE count is partly
*absence of research*, not demonstrated absence of bugs — a much less-studied target rather than a
proven-safer one. But the comparison does not turn on that. A Mac on vz runs **neither QEMU nor
KVM** — neither component the post broke. That is a stronger position than the post's own subject
was in, and stronger than Firecracker-on-Linux, which still carries KVM.

**Where the finding actually bites is Linux**, and there Lima offers no escape: its driver set is
qemu · vz (darwin) · krunkit (darwin/arm64) · wsl2 · hcs (both Windows). On a Linux host, Lima
means QEMU or nothing. Getting a Linux host off QEMU means an external driver, or a different
backend — which is one more argument for the libkrun direction, since libkrun is the one option
here that runs on KVM *and* HVF.

So the `[machine].vmtype` idea is worth having for a different reason than "choose something
better on macOS": it would **pin** vz rather than inherit it from `runtime.GOOS`, making the
better-of-the-two a decision with an owner, and it gives krunkit somewhere to be configured if
that experiment happens.

## Settling Linux and WSL2

The question "which VMM?" turned out to be downstream of a bigger one: **on a non-Mac host,
what is the boundary, and does anything check it?** Recommendations first, then the blocker.

### Linux host: `lima` + `qemu` + `wall`, and say plainly that QEMU is the weak link

Lima registers exactly one driver on Linux, so there is no VMM choice to make — `vmtype` will
resolve to `qemu` and that is the whole menu. The choice that *is* live is boundary-vs-none:

| | per-project VM | repo-only mount | `[machine].wall` | VMM |
| --- | --- | --- | --- | --- |
| `lima` + qemu | ✅ concurrent | ✅ | ✅ | QEMU/KVM |
| `native` | ❌ none | ❌ engine sees the host FS | ❌ | — |

So: **`lima` on Linux**, and treat QEMU as a known, named weak link rather than a secret. It is
the price of having a boundary at all; `native` is not a cheaper boundary, it is no boundary.

Two Linux-specific settings worth pinning alongside it, neither of which macOS surfaces:

- **Mount type.** Lima's QEMU driver defaults `mountType` to **9p** (`pkg/driver/qemu/qemu_driver.go`),
  falling back to reverse-sshfs. QEMU's 9p has its own escape-CVE history and is slow on the
  build hot path. `virtiofs` (needs `virtiofsd`) is the better answer, and probably wants the
  same treatment `vmtype` just got — pinned, not inherited.
- **The `--runtime krun` hardening is *more* available here than on a Mac.** A libkrun microVM
  per container needs nested KVM, which on Apple Silicon means M3+/macOS 15+, but on x86 Linux
  is routine. So the defence-in-depth option this note has been circling is cheapest exactly
  where the VMM is weakest. That inverts what you'd guess.

### WSL2: the distro *is* the boundary — the honest framing, not a workaround

Two things that look like WSL2 support are not:

- **Lima inside a WSL2 distro** needs nested KVM in WSL2, which needs a **custom-built WSL2
  kernel** (`CONFIG_MODULES=y`, lockdown/LoadPin off). Not a supportable default.
- **Lima's `wsl2` driver** runs on *Windows* (`limactl.exe`), is documented experimental,
  "doesn't support many of Lima's options", and wants a tar rootfs rather than a VM image. Not
  a foundation.

But WSL2 doesn't need either, because **a WSL2 distro already is a Hyper-V VM**. `native` inside
it is therefore not the same weak profile as `native` on bare Linux — there is a hypervisor
between the containers and the Windows host. Two caveats have to be said in the same breath:

1. It is **one boundary shared by every project**, not per-project. No concurrent isolation, and
   `[machine].wall` has no VM of its own to be provisioned into.
2. **WSL2 automounts the Windows drives at `/mnt/c` by default** — which straightforwardly
   breaks the repo-only mount property (ADR-0001). `/etc/wsl.conf` with `[automount] enabled =
   false` should be a documented prerequisite, not an optimisation.

That is a coherent, honest profile — weaker than the Mac's, stronger than bare-Linux `native`
— and it is deliverable without waiting for anything upstream.

### The blocker: `verify` does not currently check any of this off a Mac

```python
# Host-home / macOS paths that must NEVER appear in the VM's mount table.
_HOST_PATHS = re.compile(r"/Users|/private|/var/folders|/Volumes")
```

Every path in that regex is macOS-only. On a Linux or WSL2 host the mount-table assertion — one
of the load-bearing checks in the product's credibility battery — **passes vacuously**, and
`ls /Users` a few lines above it does too. `verify` would print green while asserting nothing,
and `/mnt/c` mounted straight through would sail past it.

This is the same "write host, not Mac" debt CLAUDE.md already flags, except here it is not
cosmetic: it is the check that is supposed to *prove* the isolation claim. **Supporting Linux and
WSL2 starts here, not with the VMM** — a boundary nothing verifies is a boundary you are
asserting on trust, which is precisely what foldyard exists not to do.

Concretely: `_HOST_PATHS` needs host-appropriate members (`/mnt/c`, `/home` outside the repo,
`/media`, `/run/host`), chosen by what the host actually looks like rather than by
`sys.platform`, plus a `native`-profile row that states which claims do *not* hold there. That
is a small change with a large honesty payoff, and it is a prerequisite for advertising either
platform.

## Measured on a real host (2026-09-07)

Everything above this section was reasoned from source. This section is what a live M3 Max
actually reports — macOS 26.6.1, podman 6.0.2, Lima 2.1.3 — with both backends in use side by
side: `Tangible` on the legacy podman backend (kept unmigrated for colleagues), and four
projects on Lima with the wall.

| | `Tangible` (podman backend) | `claude-code-log` + 3 (lima backend) |
| --- | --- | --- |
| VMM | **libkrun** (microVM) | **vz** (Virtualization.framework) |
| pinned? | `provider = "libkrun"` in `containers.conf` | **no** — inherited from Lima's `DefaultDriver()` |
| repo mount | virtiofs, rw, uid maps correctly | virtiofs (vz's default) |
| mount set | repo + worktrees root only ✅ | repo only ✅ |
| `/dev/kvm` in guest | **present** | absent (`nestedVirtualization` defaults false) |
| `[machine].wall` | n/a (podman) | **on, all four**, distinct port bands |
| concurrency | 1 VM | 3 instances coexisting |

**The irony is now measured, not inferred: the backend kept for legacy compatibility is the one
with the microVM device model and nested virt.** The modern default has the wall and concurrency
and a conventional VMM. Whichever way this goes, that pairing is an accident worth not keeping.

Three claims this settles, and two it corrects:

- **`vmType` was never pinned.** The only `vmType` in any `lima.yaml` is a *commented-out* note
  about Intel Macs; `limactl list --json` reports `vmType: vz` (resolved) and `mountType: null`
  (never recorded). Exactly the accident `[machine].vmtype` now closes — and `resolve_vmtype()`
  returns `"vz"` on this host, correctly declining the registered-but-experimental krunkit.
- **`limactl info` returns `vmTypes` in non-deterministic order** (Go map iteration) — observed
  as both `['qemu','vz','krunkit']` and `['vz','qemu','krunkit']` seconds apart. An explicit
  preference tuple is not fussiness; anything order-dependent here would be flaky.
- **libkrun's virtio-fs is fine.** The reported macOS bind-mount permission problem does **not**
  reproduce: both mounts are `virtiofs rw`, and the repo is owned by `core` = uid 501 = the host
  user. This was the checkable held to decide whether krunkit is viable at all. It passes.
- **Correction — the wall is NOT tied to Lima's `provision:` blocks.** ADR-0011 says owning VM
  provisioning "is what makes the fail-closed nftables wall possible". The implementation
  (`machine.wall_sync` → `assets/machine-wall/machine-wall.sh` + an `fy-wall.service` unit) pushes
  the wall into a *running* VM over shell and re-asserts it on boot. So it needs root shell into a
  mutable Linux guest — not Lima's create-time provisioning — and it should therefore survive a
  `vmType` change untouched. That widens the options considerably.
- **Correction — `template://podman` is deprecated.** Lima v2 wants `template:podman`;
  `LimaBackend.create()` emits a deprecation warning on every VM creation today. Works now,
  won't forever.

### Lima + krunkit: the consolidation candidate, and what is actually unknown

If it works, one backend gets the wall *and* concurrency *and* the microVM device model, and the
podman backend's remaining justification shrinks to "colleagues have not migrated" — a migration
question, not an architectural one. What is already evidenced:

- The krunkit driver is **installed and registered** — bundled with Lima 2.1.3 at
  `/opt/homebrew/Cellar/lima/2.1.3/libexec/lima/lima-driver-krunkit`, listed in `limactl info`.
- **foldyard's exact create path validates under it.** The real podman template plus the real
  `--set` expression passes `limactl validate` under `vz`, `krunkit` *and* `qemu`; krunkit also
  accepts `mountType: virtiofs`.
- libkrun's virtio-fs works on this machine (above), and the wall is driver-independent (above).

### The probe: it works (2026-09-07)

Run for real — a throwaway `fy-krun-probe` created through **foldyard's exact create path**
(`limactl create --tty=false --name … template://podman --set '<the real expression>'`, only the
sizing shrunk), on the same M3 Max, **while Tangible's libkrun VM was running**.

| question | result |
| --- | --- |
| Concurrency — does it inherit libkrun's `RequireExclusiveActive`? | **No.** `fy-krun-probe krunkit Running` alongside `/opt/podman/bin/krunkit --cpus 8 --memory 48000`. **Two libkrun VMs at once**, from two different managers. |
| Guest boots + provisions? | **Yes** — Fedora 44 under libkrun-efi, `podman 5.8.4` installed by the template's own provision script. |
| Forwarded socket at `<Dir>/sock/podman.sock`? | **Yes**, and it *accepts*: a raw `AF_UNIX` connect succeeds. |
| Does it speak libpod — the actual `Backend` contract? | **Yes.** `podman --remote --url unix://…` → `linux arm64 \| rootless=true \| 5.8.4`. |
| virtio-fs mount? | **Yes** — `type virtiofs (rw,relatime,seclabel)`, content readable, owned by uid 501. |
| `/dev/kvm` in the guest? | **No** by default — but **yes** with `nestedVirtualization: true` (re-created and re-tested: `crw-rw-rw-. 1 root kvm 10, 232 /dev/kvm`). Unlike podman's libkrun, Lima's krunkit leaves it off unless asked. |

So the three unknowns are answered, and the decider — concurrency — came out in krunkit's
favour. **Lima + krunkit delivers the wall, concurrency, and a microVM device model at once**,
and `machine.socket()` / `guest_socket()` need no change to reach it.

### The wall and `fy verify` under krunkit — both green

- **The wall provisioned first try, unmodified.** `wall_sync` installed into the krunkit VM with
  no code change: `✓ fy-wall installed: uid 501 +subuid, 524288-1074266111 default-deny; open:
  lo, local-DNS, 192.168.5.2 tcp {41800-41889, 41900-41989}`. The argument that it is
  driver-independent (post-hoc shell + a systemd unit, not create-time provisioning) is now a
  measurement.
- **`fy verify`'s VM-boundary battery passes**, wall on and wall off: engine rootless, escape
  refused (`--privileged --pid=host` cannot read host PID1 ns), no `/Users` in a privileged
  container, VM mount table free of host paths.

What genuinely remains before it could be a recommended posture:

1. **The driver is upstream-experimental**, and pinned to a Homebrew Cellar path
   (`/opt/homebrew/Cellar/lima/2.1.3/libexec/lima/lima-driver-krunkit`) that moves on every Lima
   upgrade. That is a maintenance commitment, not a footnote.
2. **No sustained workload.** A boot, a socket probe and one battery are not a week of builds —
   the four-project Lima/vz mileage is the actual evidence standard here.
5. **macOS/arm64 only**, so it changes nothing for the Linux/WSL2 question.

**The rig can follow.** Because `nestedVirtualization: true` does expose `/dev/kvm` under Lima's
krunkit, the nested-virt rig no longer needs `podman machine` for the one thing it needed it for.
That matters more than it sounds: [nested-virt.md](./nested-virt.md) currently validates
**host-only verbs on a backend most consumers no longer run**, which is a poor foundation for a
fidelity rig. Moving its L1 to Lima would put the rig on the same backend as the product and
retire the last hard dependency on `provider = "libkrun"` in the operator's global
`containers.conf`.

## Reading the post against our own threat model

Two things are worth holding in tension before treating VMM hardening as the next spend.

**The post's own remedies are largely already our thesis.** Its recommendations — least privilege
on network and credentials, short operational windows, pristine environments, monitoring — read as
a description of secretless-by-default posture
([ADR-0005](./adrs/0005-secretless-by-default-posture-axes.md)), TTL-bound modes, and the egress
wall ([ADR-0009](./adrs/0009-monitoring-cooperative-enforcement-locked.md)). Hardening the VMM
raises the *cost* of an escape; denying the loot changes what an escape is *worth*. We have spent
heavily on the second and nothing on the first, which is an argument in both directions.

**The asymmetry that argues against complacency:** foldyard's host is not an empty host. It
deliberately holds the supervisor, the minters, and the allow-store — that is the whole point of
[ADR-0006](./adrs/0006-host-side-enforcement-single-supervisor.md). An agent that escapes the VM on
our model lands somewhere with credentials in reach. So "the boundary is only cooperative anyway"
is not available as a shrug here; VMM attack surface is genuinely load-bearing for us in a way it
isn't for a tool whose host holds nothing.

**And the one capability Firecracker has that nothing else here offers:** snapshot/restore. The
post's "short operational windows with pristine environments" is a real recommendation we have no
answer to — foldyard has no per-session reset. Restore-from-snapshot is Firecracker's signature
trick. Whether that is reachable via libkrun, or worth building crudely at the box layer instead,
is a separate question this note does not answer.

## Open questions, if this is picked up

1. Does the Lima `krunkit` driver satisfy the backend contract — specifically, does the podman
   template's socket forward survive under it, and do virtio-fs mounts behave as `verify` expects?
   (This is the same checklist as [lima-backend-scope.md](./lima-backend-scope.md)'s "trickiest
   bits", re-run under a different `vmType`.)
2. Can `[machine].wall` be provisioned under krunkit, or does the guest image change break it?
   The wall is Lima-only today for provisioning reasons, not VMM reasons — that should hold, but
   it is untested.
3. Is `podman run --runtime krun` for the **dev box only** (leaving the stack on ordinary
   containers) a cheap, additive second boundary? It needs nested virt, so: M3+ Macs only.
4. What would per-session pristine state cost at the box layer, without VM snapshots?
5. ~~Does pinning `vmType` warrant its own small ADR?~~ **Done** — `[machine].vmtype` exists,
   resolved from `limactl info` rather than the OS, and printed at create.
6. Does `verify`'s mount assertion get fixed before, or alongside, any Linux/WSL2 claim? This
   note argues before.
7. Should `mountType` get the same pinning treatment as `vmtype` (9p is the QEMU default and is
   the wrong answer)?

## Sources

- Trail of Bits, "VMs won't contain cyber-capable agents" —
  <https://blog.trailofbits.com/2026/08/26/vms-wont-contain-cyber-capable-agents/> (2026-08-26)
- Firecracker: `docs/design.md`, `docs/device-api.md`, `docs/getting-started.md`,
  `docs/kernel-policy.md`, `src/vmm/src/devices/virtio/` @ `1.18.0-dev` ·
  filesystem sharing <https://github.com/firecracker-microvm/firecracker/issues/1180> ·
  <https://github.com/firecracker-microvm/firecracker/pull/1351>
- Lima: `pkg/driver/` (qemu · vz · wsl2 · krunkit · hcs · external), `pkg/limatype/lima_yaml.go`
  `DefaultDriver()`, `website/content/en/docs/config/vmtype/krunkit.md` · external drivers
  <https://lima-vm.io/docs/dev/drivers/> · v2.0 <https://www.cncf.io/blog/2025/12/11/lima-v2-0-new-features-for-secure-ai-workflows/>
- libkrun <https://github.com/containers/libkrun> · krun handler
  <https://github.com/containers/crun/blob/main/krun.1.md>
- Kata + Firecracker, block-device requirement <https://blog.cloudkernels.net/posts/kata-fc-k3s-k8s/>
- podman machine providers (applehv default, libkrun opt-in) — see also [nested-virt.md](./nested-virt.md) ·
  WSL2 nested KVM needs a custom kernel <https://github.com/microsoft/WSL/issues/4193>
- QEMU escape history: CVE-2015-5165 · CVE-2015-7504 (Phrack, "VM escape — QEMU case study") ·
  CVE-2020-14364 · 2026 KVM escapes: CVE-2026-46316 (ITScape, arm64) · CVE-2026-53359 (Januscape,
  x86) · CVE-2026-64561 (Zapscape)
