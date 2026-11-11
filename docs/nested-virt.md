# Nested virtualisation — rigs for what CI can't reach

A contributor recipe. The host-only verbs (`fy machine …`, `fy box up`/`build`, `fy host`, `fy mode`
— the ones that refuse to run inside a dev box) need a real host with a VM. Most of that is now
covered by CI; this page says what isn't, and how to test it on a rig with nested KVM.

> **This is a test rig, not a recommendation.** The hypervisors here are chosen only for whether
> they expose `/dev/kvm` to a guest. Nothing on this page bears on which VMM a consumer's VM should
> run (`[machine] vmtype`); that is weighed in [isolation-layers.md](./isolation-layers.md).

## What CI covers now

The host tier ([DEVELOPMENT.md](../DEVELOPMENT.md#test-tiers)) boots a real Lima/QEMU VM (Fedora 44
guest, rootless podman) on GitHub-hosted x86 runners, on Linux (`lima-host-e2e`) and inside WSL2
(`wsl2-host-e2e`). It drives `fy machine ensure` / `stop` / `recreate`, `fy up` through the
config-adopt gate and the supervisor, `fy box up` with in-box `fy verify`, the VM and host
firewalls, worktrees and `fy reclaim`. The in-box topology (the proxy and box e2es) runs in the
`live-e2e` job inside a container that mirrors the dev box. Results are summarised in
[linux-support.md](./linux-support.md).

## What still needs a rig

| gap | why CI can't | rig |
| -- | -- | -- |
| gVisor as the box runtime (`[machine] runtime = "gvisor"`) end to end, and its overhead | no CI job runs it; the measurements need a stable, sized host | x86 nested KVM (below) |
| krun / libkrun experiments (a microVM per container inside the VM) | needs `/dev/kvm` *inside* the VM guest — a third level on a runner | x86 nested KVM, or an Apple M3+ (untested with the GIC traps below) |
| arm64 Linux hosts | `ubuntu-24.04-arm` runners have no KVM | Apple M3+ (below): the host tier passes there |
| running the host tier without touching the VMs on your computer | CI only runs what's pushed; on your computer the tier drives the one `foldyard-example` VM | Apple M3+ (below) |
| the `podman` backend (`podman machine`) | untried on a runner | any host with podman |
| timings that match a laptop | runners are one level deeper, and shared | a real host |

## Rig 1: an x86 Linux VM with nested KVM

The rig used for the Linux validation rows (2026-09-11 to 13): a GCP `n2-standard-4` created with
`--enable-nested-virtualization`, running Fedora 44, with Lima 2.2.0, podman 5.8.4 and crun 1.28.
Any x86 Linux VM with nested virtualisation enabled works the same way (x86 nested KVM is mature).

1. Check `/dev/kvm` exists in the VM, and that your user can open it.
2. Install the host prerequisites: QEMU (plus `qemu-img`), Lima from its release tarball into
   `/usr/local`, podman, git, and uv. Anything scoped needs a `systemd --user` manager
   (`loginctl enable-linger` for a session without one).
3. Clone foldyard, then `just install` (or `uv tool install --editable .`).
4. Run the host tier: `FOLDYARD_E2E=1 just test tests/test_box_e2e.py` (or any
   `tests/test_*_e2e.py`). The modules drive the example consumer's own VM, never a consumer's.
5. For gVisor, set `[machine] runtime = "gvisor"` in a throwaway copy of `example/`, adopt it,
   `fy up` (`machine ensure` provisions runsc in the VM), then `fy box down` and `fy box up`, and
   run `fy verify` inside the box. The measurements from this rig are in
   [isolation-layers.md](./isolation-layers.md#the-sandbox-layer-gvisor).

The rig is one hypervisor level deeper than a laptop, so its timings are upper bounds; correctness
results transfer as they are.

## Rig 2: Apple Silicon M3+ with nested virtualisation

On an **M3 or newer** running **macOS 15+**, Apple's hypervisor supports one level of nested
virtualisation, and Lima exposes it with `.nestedVirtualization = true` on the `vz` and `krunkit`
drivers. M1 and M2 have no nested virtualisation.

**The Linux host tier runs on this rig, provided the L1 kernel traps the guest's GIC registers.**
Without that setting no L2 gets past early boot. Measured 2026-11-04: M3 Max, macOS 27.0.1, Lima
2.1.3 on your computer and 2.2.0 in the L1, Fedora 44 as L1 and L2 (kernel 6.19.10), QEMU 10.2.2.

### The GIC traps are required

```text
kvm-arm.vgic_v3_group0_trap=1 kvm-arm.vgic_v3_group1_trap=1 kvm-arm.vgic_v3_common_trap=1
```

These go on the **L1** kernel's command line. They make the L1's KVM emulate the L2's GIC register
accesses itself. Without them an L2 hangs about 5 s into boot, at 1, 2 and 4 vCPUs, on `vz` and
`krunkit`, and with L1 kernels 6.19.10 and 7.2.15:

- interrupts aimed at an idle vCPU never land: one CPU soft-locks in `kick_all_cpus_sync` waiting on
  another that shows `0 ticks`, followed by RCU stalls;
- KVM takes about 24,000 WFI exits a second, all of them `halt_successful_poll`, so it sees the
  interrupt pending and re-enters a guest that never takes it.

**`/dev/kvm` existing and `KVM_CREATE_VM` succeeding prove nothing here**: both work in the broken
state. The check is an L2 that reaches a login prompt.

The root cause isn't established, only that the traps work. An untrapped L2 did boot on krunkit in
2026-06, on an older macOS and without any of this, so this may be a regression. These didn't help:
`kvm-arm.wfi_trap_policy=trap`, an L1 kernel upgrade alone, and QEMU's userspace GIC
(`kernel-irqchip=off,gic-version=2`, which stops in the firmware). QEMU's `-machine its=off` did
boot one L2 on krunkit with kernel 7.2.15, but it was not tried on `vz` and Lima can't pass it.

### Running the tier

On your computer:

```bash
just nested-up                     # create or repair the L1, and check it
just nested-test                   # HEAD, all of CI's shards
just nested-test --ref my-branch tests/test_box_e2e.py
just nested-test --with-sudo tests/test_wall_e2e.py   # stops the VM; it must then be replaced
just nested-down                   # stop inside-out; --delete removes the L1 too
```

The tier runs on your computer's committed `--ref`, as `lima-host-e2e` runs it in
[foldyard-e2e.yml](../.github/workflows/foldyard-e2e.yml): the same settings, a base VM baked for
the run, and CI's shards unless you name modules. Each run starts with no VMs and an empty state
directory. A full run takes about 19 minutes, and naming one module about 2 more than that module.
Uncommitted work isn't sent.

`FY_NESTED_VM` names the L1 (`fy-nested-<name>`, one run at a time in each), and `FY_NESTED_CPUS`,
`FY_NESTED_MEMORY_GIB` and `FY_NESTED_DISK_GIB` size a new one (default 8, 16, 60). Each L1 has its
own `~/.lima`, so the tier's fixed `foldyard-example` VM can't collide with one on your computer.
These recipes are foldyard's own, not `fy` verbs: a consumer never gets them. Why they stay outside
the box is in [ADR-0035](./adrs/0035-the-box-asks-for-a-test-run.md).

### What keeps the code under test away from your computer

A Lima guest reaches your computer's `127.0.0.1` through the gateway at `192.168.5.2`: from an
unwalled L1, a listener bound to localhost on your computer answered with a 200. foldyard's own VM
firewall doesn't fit here, since it forces all egress through the proxy, while this tier needs
Fedora's mirrors and the registries directly. So `just nested-up` sets up its own
([provision.sh](../tests/tools/nested_l1/provision.sh)):

- **A wall loaded at boot**, exempting no user: DNS is allowed, then every private address is
  dropped, CGNAT's `100.64.0.0/10` included. DNS has to come first, because the resolver is on the
  gateway. Lima's network dials out from your computer's own sockets, so an address your computer
  holds is reachable too: on a computer with a `100.64` address (Tailscale, some VPNs), a listener
  on it answered the L1 until that range was added. A VPN that gives your computer a public address
  isn't covered.
- **An unprivileged user, `fytest`**, runs the tier, so what it runs can't remove the wall.
  `test_wall_e2e` needs `sudo` on its "host" and skips without it. `--with-sudo` gives it root, and
  **root can't be taken back**: a narrow `sudo` rule or a timer left behind would pass any check
  from outside. So an L1 gets one such run. It's marked from outside before the run and stopped
  straight after, so what root left can't act on your computer between runs, and every run and
  `just nested-up` refuse it until `just nested-down --delete && just nested-up` replaces it.
- **No mounts, forwards or credentials.** A commit arrives as a `git pack-objects` stream. Turning
  forwards off takes an explicit rule: with `.portForwards=[]`, Lima appends a default one, and a
  listener in the guest appeared on your computer's localhost within 4 seconds, where code under
  test could squat a port a local tool or browser trusts. An `ignore` rule on `0.0.0.0` also needs
  `guestIPMustBeZero: false`, or a listener on `127.0.0.1` passes it and still appears.

Every run checks the traps, the mounts, the forwarding rule, any `sudo` rule and the wall's rules
first. It also asks the network: a real connection attempt to a socket on your computer, and a real
listener in the L1 that must not appear there. It refuses if any of them is wrong. The connection
attempt only tries the gateway: the other ranges rest on the rules. If a `--with-sudo` run is killed
outright, the next verb stops the VM; until then it's still up, so run `just nested-down`.

This keeps your computer out of reach. It doesn't limit what leaves the L1 over the public internet,
and a run's verdict is only what the code under test reports. `fytest`'s home also outlives a run,
so one run's code could affect the next's result. `just nested-down --delete` starts clean.

### Results

With an 8 vCPU, 16 GiB L1 on `vz`, and `MACHINE_CPUS=2` as in CI:

| test | result |
| -- | -- |
| raw QEMU boot to a login prompt, 2 vCPUs | 6/6 at about 25 s (TCG, as a control: 84 s) |
| Lima L2 from `template:podman` | 37 s |
| L1 response during a 60 s CPU and disk burn in the L2 | 8–11 ms, no stalls in either kernel |
| `just nested-test`: shard `L1`, including the hypervisor SIGKILL in `test_machine_e2e` | 39 passed, 10:08 |
| `just nested-test`: shard `L2`, including the box | 23 passed, 12 skipped (`test_wall_e2e`, no `sudo`), 6:11 |
| `just nested-test --with-sudo tests/test_wall_e2e.py`: both firewalls, then replacing the VM | 12 passed, 2:06 |
| rootless container, box-style flags plus `--device /dev/kvm`: Lima L2 inside | 46 s, `accel=kvm`, with egress |

The earlier warning that this rig is fragile under load, with an L2 starving the L1's control plane
(2026-06, krunkit), didn't reproduce. Keep `--cpus` at or below the physical core count; oversizing
hung the VM on start then.

### Traps

- **Don't run the tier as Lima's own user in the L1.** Lima names that user's home
  `/home/<user>.guest`, and a bare `template:podman` create there mounts `~` onto the path Lima
  reserves in the L2 (`mountPoint is the reserved internal home directory`). `test_basevm_e2e.py`'s
  stand-in fixture fails on it. `fytest` has a plain home, so the recipes don't hit it. Setting
  `user.home` in `_config/override.yaml` isn't a fix: on VMs that already exist, it left `sudo`
  asking for a password and the next start waiting on its boot check.
- **Lima's user holds the whole default subordinate id range in this image**, so a plain `useradd`
  fails with `Can't get unique subordinate UID range`.
- **`limactl` refuses to run as root**, so a container running it needs a non-root user.
- **`/tmp` in the L1 is empty after a restart**: keep scripts in the home directory.
- **The cloud image has `kernel-core`, not `kernel`**: `dnf upgrade kernel` fails with "available,
  but not installed". Upgrade `kernel-core kernel-modules-core`.
- **Check `/proc/cmdline` after a reboot.** On a krunkit L1 with kernel 7.2.15 the arguments reached
  the boot entries but not the kernel, unexplained, so krunkit with the traps is untested.

Also untested: M4, other macOS versions, the traps' overhead, gVisor on this rig, krun inside it,
and the host tier from inside a real foldyard box.

### Tearing nested guests down

An ungraceful teardown of a nested guest wedged an L1 in 2026-06 (krunkit, untrapped). That didn't
reproduce on 2026-11-04: the L1 survived `test_machine_e2e`'s SIGKILL of the hypervisor, and
`limactl stop` forcing down a hung L2 after its 3-minute timeout. Inside-out costs little, so it's
still the default: stop the nested guest, then its container, then the L1. Treat all of them as
disposable, and recreate an L1 that goes sluggish.

## Network tests don't need nesting

The VM firewall (`[machine] firewall`) is the same nftables rules however the VM is made, so test it
in an ordinary Lima VM (the `test_wall_e2e.py` host tier), not in a nested one: an extra level only
burns CPU. The standalone proof kit is in
[archive/lima-network-forcing-kit/](./archive/lima-network-forcing-kit/).

## `tests/test_proxy_box_e2e.py` — the adapted topology

This e2e drives the real `ProxyPlugin` (its daemon spec, and the CA mount + proxy environment from
`box_args`) to start a real dev-box container over the socket, with a fake token service and a fake
TLS upstream. It checks that requests are rewritten in flight, that the CA mount is needed, and that
a 401 re-mint reaches the box as a 200.

It is *adapted* because in production the box reaches the proxy on the host through the Lima host
gateway (`192.168.5.2`) at the project's proxy port, which needs foldyard to run as the host — and
the `in_box()` guards forbid that inside a dev box. So the test runs the proxy and the upstream
in-process and points `FY_PROXY` at its own container's IP. That address is the only difference from
production. Two constraints for any sibling-container test:

- `-v` mount sources resolve on the VM, not in the test container: mount repo-relative paths, not
  `tmp_path`.
- `box_args` reads the CA path from the ambient `MITMPROXY_CA`, not from its `env` argument.

The faithful path — `fy box up` with a real supervisor — is covered by `test_box_e2e.py` in the host
tier.
