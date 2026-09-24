# Isolation layers — the measurement sessions (2026-09-07 to 09-13)

> **What it was:** the dated rig and Mac sessions behind [isolation-layers.md](../isolation-layers.md),
> moved out of that page. **Status:** finished — the libkrun box was deferred, gVisor shipped as
> `[machine] runtime = "gvisor"` ([ADR-0025](../adrs/0025-gvisor-machine-posture-and-socket-narrowing.md)),
> the host firewall shipped ([ADR-0028](../adrs/0028-no-elevation-on-the-host-operator-applies.md)).
> **Superseded by:** isolation-layers.md for the conclusions; this page keeps the numbers.

The ①②③ / (a)(b)(c) shorthand is defined in isolation-layers.md's legend. In these sessions ③
first meant a libkrun microVM, later gVisor; each section says which. "The wall" is the VM
firewall (`[machine] firewall`); "host wall" is the host firewall (`[machine] host_firewall`).

## Measured on macOS (2026-09-07)

M3 Max, macOS 26.6.1, podman 6.0.2, Lima 2.1.3; one project on the podman backend, four on Lima
with the VM firewall.

| | podman backend (×1) | lima backend (×4) |
| --- | --- | --- |
| VMM | libkrun (microVM) | vz (Virtualization.framework) |
| pinned? | `provider = "libkrun"` in `containers.conf` | no — inherited from Lima's `DefaultDriver()`; `[machine] vmtype` now pins it |
| repo mount | virtiofs, rw, uid maps correctly | virtiofs |
| mount set | repo + worktrees root only | repo only |
| `/dev/kvm` in guest | present | absent (`nestedVirtualization` defaults false) |
| VM firewall | n/a | on, all four, distinct port ranges |
| concurrency | 1 VM | 3 instances at once |

Findings:

- **`vmType` was never pinned** (`mountType: null` in `limactl list --json`). Hence
  `[machine] vmtype` and `resolve_vmtype()`, which returns `vz` here and never auto-selects krunkit.
- **`limactl info` lists `vmTypes` in random order** (Go map iteration), so the preference order
  must be explicit.
- **libkrun's virtio-fs is fine:** the reported macOS permission problem did not reproduce.
- **`template://podman` is deprecated** in Lima v2 (`template:podman`); it still works and still
  warns on every create.
- *Superseded:* the VM firewall was then pushed into a running VM over `limactl shell … sudo`.
  Since 2026-09-11 it is a boot-time `provision: mode: system` script and the host never runs
  `sudo` in the guest.

### Lima + krunkit: the probe (2026-09-07)

A throwaway VM through foldyard's own create path with `vmType: krunkit`, while the podman
backend's libkrun VM was running.

| question | result |
| --- | --- |
| Concurrency — does it inherit libkrun's exclusive-active limit? | **No** — two libkrun VMs at once, from two managers |
| Guest boots + provisions? | Yes — Fedora 44, podman 5.8.4 |
| Forwarded socket at `<Dir>/sock/podman.sock`, speaks libpod? | Yes — `linux arm64 \| rootless=true \| 5.8.4` |
| virtio-fs mount? | Yes, rw, owned by uid 501 |
| `/dev/kvm` in the guest? | Only with `nestedVirtualization: true` |

The VM firewall installed unchanged and the boundary battery passed (as it stood before the
2026-09-11 mount-table fix). krunkit stayed opt-in: upstream-experimental, a Homebrew path that
moves per upgrade, one probe rather than a week of builds.

## Measured on GCP (2026-09-11)

GCP `n2-standard-4` with nested virtualisation, Fedora 44, kernel 7.1.10, podman 5.8.4, crun 1.28
+ `crun-krun`, libkrun 1.19.0 (libkrunfw 6.12.91 guest kernel), Lima 2.2.0, `virtiofsd` 1.14.
Google's KVM → this VM → krun is the same two levels as laptop → Lima/QEMU → krun.

### The socket: answered, and the narrowing shape works

| probe | result |
| --- | --- |
| bind-mount the podman `AF_UNIX` socket into a krun container | file visible, `connect` **refused** — nothing listens in the guest kernel. (Both runtimes first needed `--security-opt label=disable` for SELinux.) |
| TCP to host loopback, default network | unreachable: libkrun's TSI proxies guest sockets through the VMM's netns, which has nothing on it. Internet and the metadata server reachable. |
| TCP to host loopback, `--network host` | reachable — and so is sshd and the metadata server. Never ship `--network host` for the box. |
| **sidecar in the same pod** (a crun `socat` container, TCP on pod loopback → the unix socket) | **works**; host sshd unreachable. This is where a filtering proxy would go, outside the microVM. |
| vsock | libkrun exports `krun_add_vsock_port`, but crun's krun handler never calls it — an upstream feature, not a knob |
| `krun.use_passt=1` | real `eth0`, broken default route, slower than TSI. Not needed. |

The guest defaults to 1 GiB RAM; `krun.ram_mib` / `krun.cpus` annotations are honoured.

### The overhead of the libkrun box

Same podman and image; only `--runtime` differs. Stable to ±3%.

| workload | crun | krun (two levels) | ratio |
| --- | --- | --- | --- |
| `podman run … true` | 0.15–0.30 s | 0.85 s | ~4× |
| `dnf install gcc make` | 14.0 s | 102 s | **7×** |
| `pip install numpy pandas` | 8.6 s | 23.8 s | 2.8× |
| CPU (python sum of squares, 30M) | 2.64 s | 3.45 s | 1.3× |
| 1 GB `dd` to rootfs / bind mount | 0.74 / 0.97 s | 6.7 / 7.2 s | 7–9× |
| 2000 × fork/exec | 0.91 s | 16.2 s | **18×** |
| 400k syscalls | 0.073 s | 0.067 s | 1× |
| 100 MB download | 0.43 s | 1.07 s | 2.5× |

Three levels (krun inside the Lima guest) works, but the fork/exec loop took 277 s and
`dnf install gcc make` 5,537 s (~400× crun).

### The outs, measured

*Second session 2026-09-11; the agent-loop table 2026-09-12.* Also libkrun 1.19.4 from source,
gVisor `runsc` release-20260817.0, and a direct-libkrun harness.

**It is one penalty: process creation, ~8 ms vs ~0.5 ms.** The earlier "small files 30× / 120×"
rows were fork/exec in disguise; file creation without a fork is 5×. `perf stat -a` from the
outer VM: ~400 VM exits per fork/exec, 74% MMIO — the xAPIC (IPIs for TLB shootdown) and
virtio-fs on the rootfs. The Lima/QEMU guest at the same depth takes ~82 cheap exits per exec
(x2APIC, block-device rootfs), 2.76 s for the loop. It is libkrun's device model under nesting,
not nesting in general.

| out | result |
| --- | --- |
| split irqchip | no knob in crun; via the harness, no effect. 1 vCPU takes ~40% off (the IPI share). |
| x2APIC | disabled in libkrunfw 5.5.0's kernel config; a rebuild would fix at most a quarter of the exits |
| newer libkrun | 1.19.4: no change. `main` is the 2.0 API, which crun 1.28 can't drive. |
| agent-shaped work | `git status` 27×, `git grep` 20×, `git log` 12×, reading 2000 files 7–10×. Every virtio-fs metadata syscall is a 50–80 µs round trip (3–6 µs under crun). |

**gVisor works rootless under podman** with `--security-opt label=disable`, a wrapper passing
`--ignore-cgroups` (runsc otherwise wants a systemd scope over the system bus), and flags via
`dev.gvisor.flag.*` annotations. Systrap needs no KVM.

| workload | crun | runsc systrap | runsc kvm | krun |
| --- | --- | --- | --- | --- |
| `podman run … true` | 0.19 s | 0.23 s | 0.25 s | 0.85 s |
| 2000 × fork/exec | 1.13 s | 6.9 s (6×) | 7.1 s | 17.6 s |
| 10k files on tmpfs | 0.27 s | 0.83 s (3×) | 1.8 s | 1.26 s |
| CPU | 2.83 s | 2.84 s (1.0×) | 2.95 s | 3.45 s |
| `dnf install gcc make` | 16.6 s | **18.1 s (1.1×)** | 22.0 s | 102 s |

The bind-mounted socket connects only with `dev.gvisor.flag.host-uds=all`, and then it is the
unfiltered socket. Host loopback is unreachable (gVisor's own netstack).

**The agent's working set** (git + node + python over a 200-commit checkout, 2026-09-12):

| workload | crun | runsc systrap | runsc kvm | libkrun |
| --- | --- | --- | --- | --- |
| `git status` ×50 | 0.48 s | **1.52 s (3.2×)** | 2.04 s | 12.0 s (25×) |
| `git grep` ×20 | 0.37 s | 1.14 s (3.1×) | 2.24 s | 7.40 s (20×) |
| `git log -20` ×50 | 0.27 s | 0.61 s (2.3×) | 1.08 s | 2.90 s (11×) |
| python read 2000 files | 0.22 s | 0.37 s (1.7×) | 0.54 s | 1.68 s (7.6×) |
| `node -e` start ×50 | 4.85 s | 6.74 s (1.4×) | 15.3 s | 13.5 s (2.8×) |

gVisor's git walk is ~3× against libkrun's ~25×: the deciding number.

**On the Mac (arm64/vz, 2026-09-12)**, runsc under podman 5.8.1, a real checkout on a read-only
bind mount:

| workload | crun | runsc (directfs on) | runsc directfs off |
| --- | --- | --- | --- |
| `git status` ×200 | 1.72 s | 4.59 s (2.7×) | 15.1 s (8.8×) |
| `git grep` ×100 | 0.46 s | 1.46 s (3.2×) | — |
| fork/exec ×2000 | 0.38 s | 1.83 s (~5×) | — |
| toolchain install + C compile | 4.96 s | 6.95 s (1.4×) | — |

Findings from the Mac run:

- **directfs must stay on** (8.8× off).
- **`--ignore-cgroups` is free**: the box sets no per-container limits.
- **`host-uds=all` hands over the full engine API**: from a gVisor container, a
  `podman --remote run --privileged -v /:/host` sibling ran under the VM kernel and listed the VM
  home. Socket narrowing is a precondition for the sandbox layer, not an alternative.
- **inotify doesn't cross into the sandbox**: host-side writes are invisible to a watcher inside;
  stat sees them. Dev servers need polling.

### Bind mounts under krun

uid mapping as crun; `rw` works. inotify doesn't cross inward (same as gVisor above).

### Lima + QEMU on Linux (first run, 2026-09-11)

`machine ensure` on the lima backend worked (`vmtype` → `qemu`); it needed `qemu-img` on the
host, and the error when it was missing blamed Apple Virtualization. Guest has `/dev/kvm`. The
mount is 9p (4.9 s for 3,000 small files vs 0.05 s on tmpfs). Pinning virtiofs: reads work,
**every inode create returns `EINVAL`** on Fedora and Ubuntu guests, SELinux or not; rootless
`virtiofsd` 1.14 logs "Failed to open file handle for the root node: Operation not permitted".
The VM firewall worked; `fy verify` passed — including, falsely, the mount audit
([verify-false-pass.md](./verify-false-pass.md#a-second-gap-2026-09-11-the-probe-reads-the-wrong-mount-namespace)).

## The host firewall on the rig (2026-09-12)

`fy machine ensure` with `MACHINE_HOST_WALL=1` against the example, VM created from scratch
inside its own scope: direct guest egress refused by name and IP (curl rc 7); DNS resolved; the
project's port range answered 200; an out-of-range port and the host's sshd refused; your own
egress, `limactl shell` and the podman socket untouched; `fy verify` ALL PASS under it; a
hand-started VM refused.

One finding: the guest's DNS is Lima's host resolver, on a random loopback port per boot.
*Superseded 2026-09-18:* the first fix discovered and opened those ports per boot (so the table
was re-rendered each boot) and `fy machine rm` removed the table. The shipped design judges
loopback on the INPUT hook by the listening socket's cgroup under a per-project ct mark, so the
table is boot-stable, and you install it once — `rm` leaves it (ADR-0028,
[lima-wall-machine-integration.md §3](../lima-wall-machine-integration.md#3-host-side-enforcement--machinehost_wall--true-linux)).

## gVisor as the real box runtime, and a sustained build (2026-09-13, fourth rig session)

foldyard's own box — every argument `fy box up` emits — created under rootless `runsc` inside the
rig's Lima/QEMU guest: it comes up (kernel `4.19.0-gvisor`), `fy ps` reaches the engine, and
in-box `fy verify` is ALL PASS under both firewalls and the proxy. This repository on the 9p
mount, warm runs:

| workload | crun | runsc | ratio |
| --- | --- | --- | --- |
| `git status` · `git grep` · `git log --stat` | 0.37 · 0.28 · 0.23 s | 0.46 · 0.28 · 0.23 s | 1–1.2× |
| `uv sync` into a fresh `.venv` on the mount | 41 s | 104 s | 2.5× (see next section) |
| the suite (1479 tests, serial) | 142 s | 226 s | 1.6× |
| 10k small files on the mount / on `/tmp` | 15.8 / 0.72 s | 39.2 / 0.70 s | 2.5× / 1× |
| 2000 × fork/exec | 2.8 s | 30.1 s | 11× |

**podman's API has no per-container runtime choice at 5.8:** `podman-remote` has no `--runtime`,
the libpod create endpoint ignores `oci_runtime` (honoured from 6.0), and compat
`HostConfig.Runtime` is never read. So a `[box] runtime` flag cannot work in any foldyard
topology. Of the three routes considered — create over the REST API on podman 6; a second,
runsc-default API socket; runsc as the VM engine's default for everything — the second won
(below).

## Where the files live, and what the suite's 1.6× is made of (2026-09-13, fifth rig session)

Same rig (2 vCPU / 2 GiB), warm runs, pairs within 2%.

**The placement matters more than the runtime.** `uv sync` with venv and cache together:

| placement | crun | runsc | ratio |
| --- | --- | --- | --- |
| the 9p mount | 29.0 s | 51.2 s | 1.8× |
| a named volume | 0.28 s | 1.57 s | 5.6× |
| the container rootfs | 0.35 s | 0.57 s | 1.6× |
| `/tmp` | 0.35 s | 0.23 s | 0.7× |

The mount is a 100× cost under crun alone. The fourth session's 2.5× was mostly copying into 9p.
In foldyard's own box shape (`shadow_volumes = [".venv"]`, source on the mount): `uv sync
--frozen` 1.1 s vs 6.1 s; the suite 122 s vs 194 s (1.6×), `-n 2` 76 s vs 165 s.

**The suite's ratio is gVisor's syscall path, not the mount.** With the whole repo + venv on
`/tmp`, in a volume or in the rootfs, the ratio is 1.7× (all three within 1%); pytest levers
(`PYTHONDONTWRITEBYTECODE`, a pyc prefix) change nothing; xdist scales crun 1.7× but runsc only
1.2× on 2 vCPU, because the Sentry's own CPU competes. So "caches in a volume" is the product
default for both runtimes, not a gVisor workaround; the example consumer ships it.

**The `kvm` platform under nesting: no.** The warm-up took 993 s against 201 s under systrap —
a KVM guest in a KVM guest in a KVM guest. Unmeasured on a bare-metal Linux host.

## The route: a second, runsc-default API socket — proven on both hosts (2026-09-13, sixth session)

A second podman API service in the VM whose default runtime is runsc — a user unit with a
`containers.conf` override, runsc and its wrapper in `~/.local/bin`, no root, no Lima config
change, no VM restart — set up on the Mac's VM (arm64/vz) and the rig's (x86/QEMU). Then the
unmodified `fy box up`, pointed at that socket:

- **The box is a gVisor box**, bootstrapped as under crun. Both sockets share one libpod store, so
  `fy ps`, `fy box ps`, exec, stop and rm through the default socket drive it — provided the
  runtime *name* is registered engine-wide in `containers.conf`.
- **In-box `fy verify`: ALL PASS on the rig.** On the Mac one row was UNPROVEN (an SSH origin and
  no `ssh` in the image) — the same under crun.
- **Flags belong in the wrapper, with overrides off.** A client annotation like
  `dev.gvisor.flag.debug-log=…` (a write primitive) is then refused; a narrowing like
  `host-uds=none` is still honoured, which is the safe direction.
- **The filter.** On 5.8 a client's `oci_runtime: crun` is ignored; on 6.x it is honoured. So the
  box gets a third, filtered socket (`src/foldyard/assets/sandbox/socket_filter.py`) that strips
  `oci_runtime`, `dev.gvisor.*` and compat `HostConfig.Runtime`, keeps keep-alive, splices hijacked
  streams, and refuses unparseable bodies. Shown live on podman 6.1.1 (a Fedora 45 guest on the
  rig): `oci_runtime: crun` came up crun through the raw runsc socket and runsc through the
  filter.
- **No forward needed:** podman-remote reaches the socket over Lima's ssh
  (`CONTAINER_HOST=ssh://<user>@127.0.0.1:<port>/run/user/<uid>/podman/podman-runsc.sock`).
  Switching runtimes is `fy box down && fy box up`, ~20 s.

**Sustained workload, the Mac** (M3, 4 vCPU / 8 GiB, virtiofs, shadow venv volume):

| workload | crun | runsc | ratio |
| --- | --- | --- | --- |
| the suite, serial (1478 tests) | 55.5 s | 65.6 s | **1.18×** |
| the suite, `-n 4` | 19.4 s | 28.5 s | 1.47× |
| `ruff check .` | 0.021 s | 0.051 s | 2.4× |
| `git status` ×50 / `git grep` ×20 | 1.21 / 0.32 s | 2.62 / 0.76 s | 2.2–2.4× |
| fork/exec ×2000 | 0.46 s | 1.92 s | 4.2× |
| 5000 small files, `/tmp` / virtiofs mount | 0.13 / 1.86 s | 0.16 / 3.56 s | 1.3× / 1.9× |

**The rig, same session:** the suite 2.0× serial, 2.7× `-n 2`; ruff 2.4×; `git status` 2.0×;
fork/exec 11×. The rig's runsc suite moved by a quarter between two sessions on the same image —
the noise floor of a nested cloud VM — so the Mac number is the clean one.

This became `[machine] runtime = "gvisor"` the same day (ADR-0025).

## Sources

GCP rig: Fedora 44, kernel 7.1.10, podman 5.8.4, crun 1.28, libkrun 1.19.0 / 1.19.4, libkrunfw
5.5.0, Lima 2.2.0, `virtiofsd` 1.14, gVisor `runsc` release-20260817.0. Mac: M3 Max, macOS 26.6.1,
Lima 2.1.3 (then 2.2.x), Fedora 44 guest, podman 5.8.x. crun's krun handler:
<https://github.com/containers/crun/blob/main/src/libcrun/handlers/krun.c>.
