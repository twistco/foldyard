# Linux and WSL2 support — status

foldyard's product path today is a Mac host (`lima` + `vz`, the wall, four projects side by
side — [isolation-layers.md](./isolation-layers.md#macos-arm64--the-machine-layer-does-the-work)).
The package carries no platform branching — every "can this host do X?" is a capability probe,
not a `sys.platform` test — so a Linux host is not a port, it is a set of paths that have or have
not been *run there*. This page is the index of which is which. The measurements themselves live
where they were taken (linked per row); keep this page the summary, not a second copy.

Where a Linux host stands in the design: the boundary can be **stronger** than the Mac's
(nftables can match the VM's own process on the host, which pf cannot — `[machine].host_wall`)
and it can be **weaker** (`backend = "native"` drops the VM entirely; WSL2's Hyper-V boundary
protects Windows, not the credentials). Both are explained in
[isolation-layers.md](./isolation-layers.md#linux--the-machine-layer-is-optional-and-qemu-is-the-price).

**Validation host:** the GCP nested-KVM rig (Fedora 44, Lima 2.2.0, podman 5.8.4, crun 1.28 —
[nested-virt.md](./nested-virt.md)). It is one hypervisor level deeper than a laptop, so timings
there are upper bounds; correctness results transfer as they are.

## Validated on a Linux host

| area | result | when | recorded in |
| --- | --- | --- | --- |
| `lima` backend on Linux — `machine ensure` creates and boots a QEMU VM; `vmtype` resolves to `qemu` from `limactl info`, not from the OS | ✅ works; needs `qemu-img` on the host (not implied by `qemu-system-x86-core`) | 2026-09-11 | [isolation-layers](./isolation-layers.md#lima--qemu-on-linux-foldyards-own-lima-backend) |
| repo-only mount set (repo + worktrees root, nothing else) | ✅ over **9p** — `virtiofs` cannot be pinned yet: Lima 2.2.0's rootless `virtiofsd` fails every inode create (`EINVAL`), so 9p until upstream moves | 2026-09-11 | [isolation-layers](./isolation-layers.md#lima--qemu-on-linux-foldyards-own-lima-backend) |
| `fy verify` — the VM-boundary battery, PID 1's mount table | ✅ ALL PASS; and `verify` itself FAILS against a VM deliberately mounting the whole home, PASSES once removed | 2026-09-11 / 12 | [verify-false-pass.md](./verify-false-pass.md) |
| `[machine].wall` — the in-VM nftables wall, provisioned at boot as root | ✅ direct egress from the guest rejected, DNS resolves; driver-independence holds off macOS | 2026-09-11 | [isolation-layers](./isolation-layers.md#lima--qemu-on-linux-foldyards-own-lima-backend) |
| root in the guest is boot-time only (the VM user's sudo grant dropped; `fy up` refuses a running VM with stale provisioning) | ✅ recorded and checked by every `machine ensure` on the rig | 2026-09-12 | [lima-wall-machine-integration.md](./lima-wall-machine-integration.md#2-enforcement--machinewall--true) |
| `[machine].host_wall` — the host-side cgroup-matched wall, end to end (`machine ensure` under `MACHINE_HOST_WALL=1`) | ✅ VM created + started in its scope; guest egress refused by name and IP; DNS resolves; band port 200; out-of-band port + sshd refused; operator, `limactl shell`, podman socket untouched; `fy verify` ALL PASS under it; hand-started VM refused; `rm` leaves no table | 2026-09-12 | [lima-wall-machine-integration.md §3](./lima-wall-machine-integration.md#3-host-side-enforcement--machinehost_wall--true-linux) |
| `fy machine ensure` / `stop` / `rm` lifecycle | ✅ exercised repeatedly by the host-wall run (create → start → stop → hand start → stop → rm) | 2026-09-12 | as above |
| the `③` question — a per-box userspace kernel (gVisor) under the machine VM | measured, **deferred**: libkrun 25× on `git status`; gVisor ~3× (x86 rig AND Mac arm64/vz, directfs on), rootless, no KVM — the front-runner. `--ignore-cgroups` costs nothing (box sets no limits); `host-uds=all` is the unfiltered socket so narrowing is a precondition; inotify-inward needs polling | 2026-09-11 / 12 | [isolation-layers](./isolation-layers.md#the-outs-measured) |
| **`fy up` end to end** — preflight, the config-adopt gate, `machine ensure`, the supervisor in the background (fake minter, egress proxy), the `fakedep` overlay, the compose build + up | ✅ against the example copied out as its own repo, `fakecred=on fakedep=on`: 58 s incl. the Postgres pull; the api serves `feature: on` and a DB-backed `/db`; `fy state` all ✓ (daemons up, capability probed ok). preflight's nested-project refusal fires on `example/` *inside* the clone — copy it out first | 2026-09-13 | this page; the run notes are in the session handover |
| `[proxy]` on the host — mitmdump + its CA, box routing, the egress log | ✅ first `fy host` generated the CA; the box gets `HTTPS_PROXY=192.168.5.2:41200` + the combined bundle, `curl https://example.com` from the box → 200 through the proxy, logged in `egress.jsonl`; `fy doctor` all ✓ | 2026-09-13 | as above |
| `[machine].wall` + `host_wall` **via `fy up`** (not `machine ensure`) | ✅ needs `[proxy]` declared — preflight refuses the wall on a consumer with nothing routing the box; then: table loaded, VM in its scope, direct guest egress refused by name and IP, DNS resolves, the api still served. Probe with `curl --noproxy '*'`: Lima's `environment.d` gives the VM user the proxy env, so a bare `curl` from `limactl shell` goes through the proxy and answers 200 | 2026-09-13 | [lima-wall-machine-integration.md §3](./lima-wall-machine-integration.md#3-host-side-enforcement--machinehost_wall--true-linux) |
| `fy box up` / `exec` / `down` + **in-box `fy verify`** | ✅ box image built + bootstrapped in 45 s; in-box `fy ps` reaches the engine, `fy mode` reads the mirror; in-box `verify` ALL PASS under walls + proxy — after fixing a false FAIL (a Fedora guest's btrfs `subvol=/root` option matched the in-box home; the audit now judges the mountpoint field) and giving the fixture a *private* origin (a public one answers `ls-remote` without credentials and reads as pushable) | 2026-09-13 | [verify-false-pass.md](./verify-false-pass.md#a-false-fail-2026-09-13-the-options-field) |
| the config-adopt gate ([ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)): first adoption, drift, revert | ✅ a never-adopted checkout with no terminal: `fy up` refuses; `fy config adopt` non-interactive adopts. A drifted tree (`cpus = 2 → 3`): `fy up` prints the diff and keeps running the adopted copy, the supervisor logs the one-per-change line, the doctor row flags it, `fy config revert` restores the file | 2026-09-13 | as above |
| `fy machine recreate`, `fy machine stop` (also stops the supervisor), `fy doctor`, the stale-provisioning refusal (`fy box exec` without the wall env after a walled `up`) | ✅ | 2026-09-13 | as above |
| CI tiers 1–3 (unit/golden/TUI, the example stack up → serve → down, the proxy and box e2es) | ✅ every push, on `ubuntu-latest` — inside a docker-CLI container mirroring the dev box, so `in_box()` is true and the machine + host gates are bypassed | continuous | [DEVELOPMENT.md](../DEVELOPMENT.md#ci-githubworkflowsfoldyardyml) |

## Not yet validated on Linux

- **Worktrees on a Linux host** — `fy worktree add` + a second stack in the same VM; `fy up`
  itself is validated above, worktrees are not.
- **The box e2e on the rig** (`tests/test_proxy_box_e2e.py` against the rig's engine — the
  recipe is in [nested-virt.md](./nested-virt.md)); CI covers it on a runner, the rig would cover
  it on a real Lima VM.
- **`backend = "native"`** on a real Linux host (the host's own rootless podman, no VM) — only
  the CI container mirror has exercised it.
- **WSL2 — nothing measured.** `/dev/kvm` in a stock Windows 11 x86 distro (two minutes on any
  such machine: `wsl --shutdown; wsl; ls -l /dev/kvm`), Lima + QEMU inside it, `[automount]
  enabled = false` for the repo-only mount. Windows-on-ARM boots the distro at EL1, so KVM is
  structurally absent there. See
  [isolation-layers.md](./isolation-layers.md#wsl2--a-linux-host-whose-hyper-v-boundary-protects-the-wrong-asset).
- **`fy tui`, `fy code` (the VS Code attach), `fy open` (browser)** on Linux — untouched.
- **The host wall's operator side**: the `sudo nft` prompt on every `fy up` (a passwordless
  sudoers rule for `nft` is the documented answer, not yet written up as a recipe), and the
  requirement for a `systemd --user` manager (an SSH login has one; a bare `su` may not) — the
  scope wrapper would fail loudly, but the message is systemd's, not ours.

## Outstanding work before "supported"

- **The Mac-flavoured strings sweep.** ~280 `Mac` mentions in `src/`, ~150 in `docs/` + README.
  The ones a Linux user hits first: `run on the host (Mac)` (the in-box refusals in
  `machine.py`), the lima create failure that blames Apple Virtualization, `allocated Mac daemon
  ports` / `Mac proxy only` / `Mac-side foldyard daemons` from `machine ensure`, and every
  `install_hint`/message that says `brew install …` (lima, podman, uv — a Fedora host wants
  `dnf`, and Lima there is a release tarball into `/usr/local`). The rule for new text is in
  [DEVELOPMENT.md](../DEVELOPMENT.md#conventions--gotchas): write *host*, say *macOS* only where
  the claim really is macOS-only.
- **Lima mounts on Linux**: 9p is slow on metadata (4.9 s vs 0.05 s for 3,000 small files); a
  fix is upstream (`virtiofsd` root/file-handle support), or a newer Lima. Re-test when either
  moves.
- **`③` on Linux**: gVisor is the candidate and its cost is measured (~3× on git walks, both
  hosts). `--ignore-cgroups` and `host-uds` are settled (see the ③ row above); what remains is
  gVisor as the real box runtime (`fy box up` under runsc), a sustained build, and socket
  narrowing as its precondition — then an ADR. Not a Linux-support blocker: the product claim
  without `③` is the same as the Mac's.
- **A CI job on a Linux *host* path** is not possible on GitHub-hosted runners (podman-machine /
  Lima VMs are flaky there — [DEVELOPMENT.md](../DEVELOPMENT.md#ci-githubworkflowsfoldyardyml)),
  so the rig stays the validation host for tier 4. Its recipe and its cost are in
  [nested-virt.md](./nested-virt.md).

## Recording a new validation

Add a row above with the date, the versions if they differ from the rig's, and a link to where
the measurement lives — [isolation-layers.md](./isolation-layers.md) for anything about the
boundary, the relevant design page otherwise. Move the item out of "Not yet validated" in the
same commit. A run that *failed* is a row too: the leaky-VM and the DNS findings both came from
runs that did not go as expected, and both fixed something.
