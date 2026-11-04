# Nested virtualisation — rigs for what CI can't reach

A contributor recipe. The host-only verbs (`fy machine …`, `fy box up`/`build`, `fy host`,
`fy mode` — the ones that refuse to run inside a dev box) need a real host with a VM. Most of that
is now covered by CI; this page says what isn't, and how to test it on a rig with nested KVM.

> **This is a test rig, not a recommendation.** The hypervisors here are chosen only for whether
> they expose `/dev/kvm` to a guest. Nothing on this page bears on which VMM a consumer's VM
> should run (`[machine] vmtype`); that is weighed in [isolation-layers.md](./isolation-layers.md).

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
| --- | --- | --- |
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
   `fy up` (`machine ensure` provisions runsc in the VM), then `fy box down` and `fy box up`,
   and run `fy verify` inside the box. The measurements from
   this rig are in [isolation-layers.md](./isolation-layers.md#the-sandbox-layer-gvisor).

The rig is one hypervisor level deeper than a laptop, so its timings are upper bounds;
correctness results transfer as they are.

## Rig 2: Apple Silicon M3+ with nested virtualisation

On an **M3 or newer** running **macOS 15+**, Apple's hypervisor supports one level of nested
virtualisation, and Lima exposes it with `.nestedVirtualization = true` on the `vz` and `krunkit`
drivers. M1 and M2 have no nested virtualisation.

**The Linux host tier runs on this rig, provided the L1 kernel traps the guest's GIC registers.**
Without that setting no L2 gets past early boot. Measured 2026-11-04: M3 Max, macOS 27.0.1,
Lima 2.1.3, Fedora 44 as L1 and L2 (kernel 6.19.10), QEMU 10.2.2.

### The GIC traps are required

```text
kvm-arm.vgic_v3_group0_trap=1 kvm-arm.vgic_v3_group1_trap=1 kvm-arm.vgic_v3_common_trap=1
```

These go on the **L1** kernel's command line. They make the L1's KVM emulate the L2's GIC register
accesses itself. Without them an L2 hangs about 5 s into boot, at 1, 2 and 4 vCPUs, on `vz` and
`krunkit`, and with L1 kernels 6.19.10 and 7.2.15:

- interrupts aimed at an idle vCPU never land: one CPU soft-locks in `kick_all_cpus_sync` waiting
  on another that shows `0 ticks`, followed by RCU stalls;
- KVM takes about 24,000 WFI exits a second, all of them `halt_successful_poll`, so it sees the
  interrupt pending and re-enters a guest that never takes it.

**`/dev/kvm` existing and `KVM_CREATE_VM` succeeding prove nothing here**: both work in the broken
state. The check is an L2 that reaches a login prompt.

The root cause isn't established, only that the traps work. An untrapped L2 did boot on krunkit in
2026-06, on an older macOS and without any of this, so this may be a regression. These didn't help:
`kvm-arm.wfi_trap_policy=trap`, an L1 kernel upgrade alone, and QEMU's userspace GIC
(`kernel-irqchip=off,gic-version=2`, which stops in the firmware). QEMU's `-machine its=off` did
boot one L2 on krunkit with kernel 7.2.15, but it was not tried on `vz` and Lima can't pass it.

### Setting it up

From your computer. No mounts, and nothing forwarded: code reaches the L1 only by being sent there.

```bash
limactl start --tty=false --name=fy-nested --vm-type=vz --cpus=8 --memory=16 --disk=60 \
  --set '.nestedVirtualization=true | .mounts=[] | .portForwards=[]' template:podman
limactl shell --workdir / fy-nested sudo grubby --update-kernel=ALL --args="\
kvm-arm.vgic_v3_group0_trap=1 kvm-arm.vgic_v3_group1_trap=1 kvm-arm.vgic_v3_common_trap=1"
limactl stop fy-nested && limactl start --tty=false fy-nested
limactl shell --workdir / fy-nested sudo dmesg | grep 'sysreg trapping'  # must print a line
```

Then, inside the L1:

1. `sudo dnf install qemu-system-aarch64-core qemu-img edk2-aarch64 git just uv jq nftables`
2. Install Lima from its `Linux-aarch64` release tarball into `/usr/local`, then run
   `sudo loginctl enable-linger "$USER"` (`sudo` is required here, unlike on a runner).
3. Send the code over: `git bundle create x.bundle <ref>` on your computer, `limactl copy` it in,
   and `git clone` it.
4. Run the modules as `lima-host-e2e` in
   [foldyard-e2e.yml](../.github/workflows/foldyard-e2e.yml) does: its `env`, the session base bake,
   `fy machine ensure` from a copy of `example/`, then `uv run --group e2e pytest` per shard.

Each L1 has its own `~/.lima`, so the host tier's fixed `foldyard-example` VM can't collide with
one on your computer.

### Keep the code under test away from your computer

A Lima guest reaches your computer's `127.0.0.1` through the gateway at `192.168.5.2`: from an
unwalled L1, a listener bound to localhost on your computer answered with a 200. So the L1 holds no
credentials and mounts nothing, and if it runs code you wouldn't run on your computer, wall it
first. foldyard's own VM firewall doesn't fit, since it forces all egress through the proxy, while
this tier needs Fedora's mirrors and the registries directly.

```bash
sudo nft -f - <<'NFT'
table inet fy_nested_wall {
  chain output { type filter hook output priority 0; policy accept;
    oifname "lo" accept
    ct state established,related accept
    ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16 } udp dport 53 accept
    ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16 } tcp dport 53 accept
    ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16 } drop
  }
}
NFT
```

The DNS lines must come first: the resolver is on the gateway. Behind this rule the listener timed
out, while DNS, public egress, `limactl shell` from your computer and a full L2 boot with an image
pull all worked. It doesn't survive a reboot, so apply it after each start. It leaves the L1 free
to reach the public internet, so it protects your computer, not your data's route out. **The full
host tier hasn't been run behind it**: the results below are from an unwalled L1 running `main`.

### Results

With an 8 vCPU, 16 GiB L1 on `vz`, and `MACHINE_CPUS=2` as in CI:

| test | result |
| --- | --- |
| raw QEMU boot to a login prompt, 2 vCPUs | 6/6 at about 25 s (TCG, as a control: 84 s) |
| Lima L2 from `template:podman` | 37 s |
| L1 response during a 60 s CPU and disk burn in the L2 | 8–11 ms, no stalls in either kernel |
| shard `L1`, including the hypervisor SIGKILL in `test_machine_e2e` | 39 passed, 11:36 |
| shard `L2`, including the box and both firewalls | 34 passed, 1 setup error (below), 8:35 |
| `test_basevm_e2e.py` with no session base: bake, clone, `rm` | 3 passed, 2:05 |
| rootless container, box-style flags plus `--device /dev/kvm`: Lima L2 inside | 46 s, `accel=kvm`, with egress |

The earlier warning that this rig is fragile under load, with an L2 starving the L1's control plane
(2026-06, krunkit), didn't reproduce. Keep `--cpus` at or below the physical core count; oversizing
hung the VM on start then.

### Traps

- **`test_basevm_e2e.py`'s stand-in fixture fails with a session base.** Lima names its guest home
  `/home/<user>.guest`, so that is `$HOME` in the L1, and a bare `template:podman` create there
  mounts `~` onto the path Lima reserves in the L2 (`mountPoint is the reserved internal home
  directory`). It fails at validation, before any VM starts. Run that module without
  `FY_BASE_VM_NAME`/`FY_BASE_VM_STAMP`. Don't set `user.home` in `_config/override.yaml` to avoid
  it: on VMs that already exist, it left `sudo` asking for a password and the next start waiting on
  its boot check.
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

The VM firewall (`[machine] firewall`) is the same nftables rules however the VM is made, so test
it in an ordinary Lima VM (the `test_wall_e2e.py` host tier), not in a nested one: an extra level
only burns CPU. The standalone proof kit is in
[archive/lima-network-forcing-kit/](./archive/lima-network-forcing-kit/).

## `tests/test_proxy_box_e2e.py` — the adapted topology

This e2e drives the real `ProxyPlugin` (its daemon spec, and the CA mount + proxy environment from
`box_args`) to start a real dev-box container over the socket, with a fake token service and a
fake TLS upstream. It checks that requests are rewritten in flight, that the CA mount is needed,
and that a 401 re-mint reaches the box as a 200.

It is *adapted* because in production the box reaches the proxy on the host through the Lima host
gateway (`192.168.5.2`) at the project's proxy port, which needs foldyard to run as the host —
and the `in_box()` guards forbid that inside a dev box. So the test runs the proxy and the upstream
in-process and points `FY_PROXY` at its own container's IP. That address is the only difference
from production. Two constraints for any sibling-container test:

- `-v` mount sources resolve on the VM, not in the test container: mount repo-relative paths,
  not `tmp_path`.
- `box_args` reads the CA path from the ambient `MITMPROXY_CA`, not from its `env` argument.

The faithful path — `fy box up` with a real supervisor — is covered by `test_box_e2e.py` in the
host tier.
