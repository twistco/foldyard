# Linux and WSL2 support — status

foldyard has no platform branching: every "can this host do X?" is a capability probe, not a
`sys.platform` test. So a Linux host is not a port; it is a set of code paths that have or have
not been *run there*. This page is the index of which is which. The measurements live where they
were taken (linked per row); keep this page the summary.

Linux and WSL2 hosts are validated in CI on every run of the host tier (`lima-host-e2e` and
`wsl2-host-e2e`, see [DEVELOPMENT.md](../DEVELOPMENT.md#ci-githubworkflowsfoldyardyml--foldyard-e2eyml)).
macOS (`lima` with `vz`) is the platform most day-to-day use has seen
([isolation-layers.md](./isolation-layers.md#macos-arm64--the-machine-layer-does-the-work)).

Where a Linux host stands in the design: the boundary can be **stronger** than on macOS (nftables
can match the VM's own process on the host, which macOS's pf cannot — `[machine] host_firewall`)
and it is never weaker: there is no VM-less backend
([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)). WSL2's Hyper-V boundary protects
Windows, not the credentials, so the VM is required inside the distro too. Both are explained in
[isolation-layers.md](./isolation-layers.md#linux--the-machine-layer-is-qemu-and-qemu-is-the-price).

**Where the results come from:**

- **The CI host tier** — GitHub-hosted `ubuntu-24.04` (x86, `/dev/kvm`, Lima 2.2.0 from the
  release tarball, QEMU 8.2, host podman CLI 4.9.3) booting a Fedora 44 / podman 5.8.4 guest; and
  the same inside WSL2 on `windows-2025`.
- **The nested-KVM rig** — a GCP VM (Fedora 44, Lima 2.2.0, podman 5.8.4, crun 1.28;
  [nested-virt.md](./nested-virt.md)). It is one hypervisor level deeper than a laptop, so its
  timings are upper bounds; correctness results transfer as they are.

## Validated on a Linux host

| area | result | when | where |
| --- | --- | --- | --- |
| `lima` backend on Linux: `machine ensure` boots a QEMU VM | ✅ needs `qemu-img` ([1](#1-lima-on-linux)) | 2026-09-11 | [isolation-layers](./isolation-layers.md#lima--qemu-on-linux-foldyards-own-lima-backend) |
| repo-only mount set | ✅ over 9p ([1](#1-lima-on-linux)) | 2026-09-11 | as above |
| `fy verify` VM-boundary checks, incl. the home-mount negative | ✅ PASS, and FAILS on a leaky VM | 2026-09-11, CI | [archive/verify-false-pass.md](./archive/verify-false-pass.md) |
| `[machine] firewall` (the VM firewall) | ✅ direct guest egress refused, DNS resolves | 2026-09-11, CI | [lima-wall-machine-integration §2](./lima-wall-machine-integration.md#2-enforcement--machinewall--true) |
| root in the guest only at boot; stale provisioning refused | ✅ checked by every `machine ensure` | 2026-09-12, CI | as above |
| `[machine] host_firewall` (the host firewall) via `fy up` | ✅ ([2](#2-the-host-firewall)) | 2026-09-12, CI | [lima-wall-machine-integration §3](./lima-wall-machine-integration.md#3-host-side-enforcement--machinehost_wall--true-linux) |
| `fy machine ensure` / `stop` / `recreate` / `rm`, SIGKILLed QEMU recovered | ✅ | CI (`test_machine_e2e.py`) | [DEVELOPMENT.md](../DEVELOPMENT.md#test-tiers) |
| `fy up` end to end: preflight, config-adopt gate, supervisor, proxy, overlay, compose | ✅ | CI (`test_e2e.py`, `test_host_daemons_e2e.py`) | as above |
| the proxy on the host: CA, box routing, egress log | ✅ | 2026-09-13, CI | as above |
| config-adopt gate: first adoption, drift, revert | ✅ | 2026-09-13 | [ADR-0022](./adrs/0022-host-runs-the-adopted-config.md) |
| `fy box up` / `down` + in-box `fy verify` | ✅ ([3](#3-the-box)) | CI (`test_box_e2e.py`) | [archive/verify-false-pass.md](./archive/verify-false-pass.md#a-false-fail-2026-09-13-the-options-field) |
| `fy worktree add` / `remove` | ✅ | CI (`test_worktree_e2e.py`) | [DEVELOPMENT.md](../DEVELOPMENT.md#test-tiers) |
| `fy reclaim` on a real store | ✅ | CI (`test_reclaim_e2e.py`) | as above |
| engine probes on host podman 4.9 (`fy state`, `fy doctor`) | ✅ after a fix ([4](#4-podman-49-labels)) | CI (`test_probes_e2e.py`) | as above |
| gVisor as the box runtime (`[machine] runtime = "gvisor"`) | ✅ works; overhead measured ([5](#5-gvisor)) | 2026-09-11 – 13, rig | [ADR-0025](./adrs/0025-gvisor-machine-posture-and-socket-narrowing.md) |
| repo mounted under the host's home (`/home/<user>/…`) survives restarts | ✅ | CI (`test_machine_e2e.py`) | [6](#6-open-a-home-mount-added-after-first-boot) |
| the host's whole `/home/<user>` mounted at its own path, added after first boot | ❌ **open** ([6](#6-open-a-home-mount-added-after-first-boot)) | 2026-09-17 | [run 35231657712](https://github.com/twistco/foldyard/actions/runs/35231657712) |
| `ubuntu-24.04-arm` runners | ❌ no KVM, so VM-backed CI is x86-only | 2026-09-17 | — |
| WSL2 as the host (Windows Server 2025 runner) | ✅ 30 passed, 6 skipped ([7](#7-wsl2)) | CI (`wsl2-host-e2e`) | [run 35261179135](https://github.com/twistco/foldyard/actions/runs/35261179135) |
| `[machine] host_firewall` on WSL2 | ❌ kernel lacks `CONFIG_NFT_SOCKET` ([7](#7-wsl2)) | 2026-09-17 | [run 35256429594](https://github.com/twistco/foldyard/actions/runs/35256429594) |

### Details

#### 1. Lima on Linux

`vmtype` resolves to `qemu` from `limactl info`, not from the OS. The host needs `qemu-img`,
which comes from `qemu-utils` and isn't implied by `qemu-system-x86-core`. The mounts use 9p:
`virtiofs` can't be used yet because Lima 2.2.0's rootless `virtiofsd` fails every inode create
(`EINVAL`).

#### 2. The host firewall

Checked on the rig and in `test_wall_e2e.py`: the VM starts in its own cgroup scope; direct guest
egress is refused by name and IP; DNS resolves; the proxy's port is open and other ports and sshd
are refused; your own processes, `limactl shell` and the podman socket are unaffected; `fy verify`
passes; a hand-started VM is refused. The firewall needs `[proxy]` declared (preflight refuses it
otherwise). foldyard never runs `sudo`: `fy machine host-firewall` prints the install commands
and `fy up` checks enforcement ([ADR-0028](./adrs/0028-no-elevation-on-the-host-operator-applies.md)).
It needs a `systemd --user` manager (an SSH login has one; a bare `su` may not —
`loginctl enable-linger` gives one). When probing by hand, use `curl --noproxy '*'`: Lima gives
the VM user the proxy environment, so a bare `curl` from `limactl shell` goes through the proxy.

#### 3. The box

The first rig run found two `fy verify` bugs, both fixed: a false FAIL (a Fedora guest's btrfs
`subvol=/root` mount option matched the in-box home; the audit now reads only the mountpoint),
and the need for a *private* origin (a public one answers `git ls-remote` without credentials).

#### 4. podman 4.9 labels

Ubuntu 24.04's podman 4.9.3 has no `{{.Label "k"}}` in `ps --format` (it is a 5.x template
function), so every probe built on it read "engine unreachable" while `fy ps` worked. Now
`{{json .Labels}}` via `devmode.ps_labels`, pinned in `tests/test_devmode.py`.

#### 5. gVisor

Measured on the rig and on macOS: `fy verify` passes inside a gVisor box; the test suite runs
1.6–2.0× slower on the rig (1.2× on macOS), fork/exec 11×, git walks about 1×. The overhead is
gVisor's syscall path, not file placement. The route is a second, runsc-default podman socket in
the VM, and the box mounts a filtered copy of it that strips runtime overrides. Details:
[isolation-layers.md](./isolation-layers.md#the-sandbox-layer-gvisor) and
[ADR-0025](./adrs/0025-gvisor-machine-posture-and-socket-narrowing.md).

#### 6. Open: a home mount added after first boot

After `machine stop`, adding a read-only 9p mount of the host's `/home/<user>` at the same path
(`limactl edit`), then `machine ensure`, the guest's rootless podman accepts connections but can't
create any container (crun: `open …/merged/etc/resolv.conf: No such file`,
`mkdir /run/secrets: EPERM`). Store ownership and the subuid map are intact; `podman system
migrate` doesn't help; removing the mount doesn't heal it; `fy machine recreate` does. Plain
restarts, a re-provision with the firewall, and SIGKILL → `ensure` are all fine, and a repo mounted
*under* the home at its own path survives restarts, so the trigger is a mount whose location *is*
the home, added after first boot. The rig ran the same recipe healthy on 2026-09-11. Because of
this, `test_verify_e2e.py` exposes the home at `/mnt/c` instead; the home-path branch of the audit
stays unit-tested.

#### 7. WSL2

The distro has `/dev/kvm` once `%USERPROFILE%\.wslconfig` sets `nestedVirtualization=true`
(before the distro first starts). The unmodified `machine ensure` boots the guest to READY in
68 s (47 s on a restart), and the host tier runs as-is, about 3.5× slower than on Linux. Nothing
in the product differs; the job's plumbing (WSLENV, the user switch, the clone onto ext4) is
described in [DEVELOPMENT.md](../DEVELOPMENT.md#ci-githubworkflowsfoldyardyml--foldyard-e2eyml).

The host firewall can't work on stock WSL2: it matches the VM by `socket cgroupv2`, and the WSL2
kernel (6.6 and 6.18 branches) has `# CONFIG_NFT_SOCKET is not set`, so `nft` refuses the rule and
the firewall fails closed. `test_wall_e2e.py` probes for this and skips. A custom WSL2 kernel
(`.wslconfig` `kernel=`) is the only route. The VM firewall runs in the guest and is unaffected.

## Not yet validated on Linux

- **WSL2 on a real Windows 11 machine.** CI runs Windows Server 2025 with
  `nestedVirtualization=true` set explicitly. Windows 11 x86 defaults it on; Windows 10 silently
  overrides it. Still owed: the two-minute check on a real machine
  (`wsl --shutdown; wsl; ls -l /dev/kvm`). Keep the checkout on the distro's ext4, not under
  `/mnt/c`. Windows on ARM has no KVM in the distro. See
  [isolation-layers.md](./isolation-layers.md#wsl2--a-linux-host-whose-hyper-v-boundary-protects-the-wrong-asset).
- **`fy tui`, `fy code` (the VS Code attach), `fy open` (browser)** on Linux — untested.
- **The `podman` backend** (podman machine) on a Linux host — untried.
- **`tests/test_proxy_box_e2e.py` on a real Lima VM** — it needs the test process inside a
  container beside the box, which the `live-e2e` job provides; `test_box_e2e.py` covers the box on
  the VM.

## Outstanding work before "supported"

- **Wording that assumes macOS.** The sweep from "Mac" to "host" is in progress. A few
  user-facing strings remain, e.g. the Lima create failure in `machine.py` that blames Apple
  Virtualization. The rule for new text is in
  [DEVELOPMENT.md](../DEVELOPMENT.md#conventions--gotchas): write *host*, say *macOS* only where
  the claim really is macOS-only.
- **9p is slow on metadata** (4.9 s vs 0.05 s for 3,000 small files). The fix is upstream
  (`virtiofsd` root/file-handle support) or a newer Lima. Re-test when either moves.
- **The gVisor allowlist.** gVisor is wired as `[machine] runtime = "gvisor"`; the broader
  mount/endpoint allowlist is deferred (ADR-0025 §Decision 4). Not a Linux blocker: without gVisor
  the Linux claim is the same as macOS's.
- **What CI can't reach:** arm64 (no KVM) and nested virtualisation inside the guest. For those
  the nested-KVM recipe in [nested-virt.md](./nested-virt.md) remains.

## Recording a new validation

Add a row with the date, versions if they differ from the ones above, and a link to where the
measurement lives — [isolation-layers.md](./isolation-layers.md) for anything about the boundary,
the relevant design page otherwise. Put anything longer than a line under **Details**. Move the
item out of "Not yet validated" in the same commit. A run that *failed* is a row too.
