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
| krun / libkrun experiments (a microVM per container inside the VM) | needs `/dev/kvm` *inside* the VM guest — a third level on a runner | x86 nested KVM, or an Apple M3+ |
| arm64 hosts | `ubuntu-24.04-arm` runners have no KVM | Apple M3+ (below) |
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
virtualisation, and Lima exposes it. Measured 2026-09-07 (M3 Max, macOS 26.6.1, Lima 2.1.3): an
instance created from `template:podman` with `.vmType = "krunkit"` and
`.nestedVirtualization = true` has `/dev/kvm` in the guest; without the flag it doesn't. The `vz`
driver supports the same flag. M1 and M2 have no nested virtualisation.

Nested virtualisation on Apple hardware is **fragile under load**: a 2-vCPU L2 guest starved the
L1 VM's control plane (`exec` timeouts, the L1 needed a restart). Use it for `KVM_CREATE_VM`
smoke tests and short krun experiments, not a full guest under a sustained workload. Keep `--cpus`
at or below the physical core count; oversizing hangs the VM on start.

### Tear nested guests down gracefully

An ungraceful teardown of a nested guest can wedge the L1 VM, which then needs recreating.

- Never `kill -9` the inner VMM, and never `docker rm -f` / `podman rm -f` a container that runs
  a nested guest.
- Tear down inside-out: power the nested guest off from inside it, stop its container, then stop
  or remove the nested VM.
- Treat the nested VM as disposable: if it wedges, remove and recreate it. If the L1 VM itself
  goes sluggish, recreate it from the host — never force-kill from inside.

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
