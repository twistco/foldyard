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
| the `③` question — a per-box userspace kernel (gVisor) under the machine VM | measured, **deferred**: libkrun 25× on `git status`; gVisor ~3× on micro git walks (x86 rig AND Mac arm64/vz). **As the real box runtime** (foldyard's own box args, rootless runsc in the guest): `fy verify` ALL PASS in it; sustained — test suite 1.6×, fork/exec 11×, git walks ~1×; installs 6 s vs 1 s in the shadow volume (the 2.5× row was the venv on the 9p mount — a 100× layer under crun alone). The suite's 1.6× is the sentry's syscall path, not the mount (1.7× on gVisor's own tmpfs; no pytest lever moves it; xdist scales 1.2× vs crun's 1.7× on 2 vCPU). `--ignore-cgroups` free; `host-uds=all` is the unfiltered socket so narrowing is a precondition; inotify-inward needs polling. **The product route is proven:** a second, runsc-default podman API socket in the VM (a user unit + a `containers.conf` override, no root, no boot-script change, no VM restart) — the unmodified `fy box up` against it created a gVisor box on the rig AND the Mac, driven by `fy ps`/`exec`/`verify` through the default socket; in-box `fy verify` ALL PASS on the rig; flags fixed in the wrapper (client annotation overrides refused). Sixth-session suite ratio 2.0× on the rig (noisy: 1.6× a session earlier) vs 1.2× on the Mac | 2026-09-11 / 12 / 13 | [isolation-layers](./isolation-layers.md#gvisor-as-the-real-box-runtime-and-a-sustained-build-2026-09-13-fourth-rig-session), [placement + the 1.6×](./isolation-layers.md#where-the-files-live-and-what-the-suites-16-is-made-of-2026-09-13-fifth-rig-session), [the route](./isolation-layers.md#the-route-a-second-runsc-default-api-socket--proven-on-both-hosts-2026-09-13-sixth-session) |
| **`fy up` end to end** — preflight, the config-adopt gate, `machine ensure`, the supervisor in the background (fake minter, egress proxy), the `fakedep` overlay, the compose build + up | ✅ against the example copied out as its own repo, `fakecred=on fakedep=on`: 58 s incl. the Postgres pull; the api serves `feature: on` and a DB-backed `/db`; `fy state` all ✓ (daemons up, capability probed ok). preflight's nested-project refusal fires on `example/` *inside* the clone — copy it out first | 2026-09-13 | this page; the run notes are in the session handover |
| `[proxy]` on the host — mitmdump + its CA, box routing, the egress log | ✅ first `fy host` generated the CA; the box gets `HTTPS_PROXY=192.168.5.2:41200` + the combined bundle, `curl https://example.com` from the box → 200 through the proxy, logged in `egress.jsonl`; `fy doctor` all ✓ | 2026-09-13 | as above |
| `[machine].wall` + `host_wall` **via `fy up`** (not `machine ensure`) | ✅ needs `[proxy]` declared — preflight refuses the wall on a consumer with nothing routing the box; then: table loaded, VM in its scope, direct guest egress refused by name and IP, DNS resolves, the api still served. Probe with `curl --noproxy '*'`: Lima's `environment.d` gives the VM user the proxy env, so a bare `curl` from `limactl shell` goes through the proxy and answers 200 | 2026-09-13 | [lima-wall-machine-integration.md §3](./lima-wall-machine-integration.md#3-host-side-enforcement--machinehost_wall--true-linux) |
| `fy box up` / `exec` / `down` + **in-box `fy verify`** | ✅ box image built + bootstrapped in 45 s; in-box `fy ps` reaches the engine, `fy mode` reads the mirror; in-box `verify` ALL PASS under walls + proxy — after fixing a false FAIL (a Fedora guest's btrfs `subvol=/root` option matched the in-box home; the audit now judges the mountpoint field) and giving the fixture a *private* origin (a public one answers `ls-remote` without credentials and reads as pushable) | 2026-09-13 | [verify-false-pass.md](./verify-false-pass.md#a-false-fail-2026-09-13-the-options-field) |
| the config-adopt gate ([ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)): first adoption, drift, revert | ✅ a never-adopted checkout with no terminal: `fy up` refuses; `fy config adopt` non-interactive adopts. A drifted tree (`cpus = 2 → 3`): `fy up` prints the diff and keeps running the adopted copy, the supervisor logs the one-per-change line, the doctor row flags it, `fy config revert` restores the file | 2026-09-13 | as above |
| `fy machine recreate`, `fy machine stop` (also stops the supervisor), `fy doctor`, the stale-provisioning refusal (`fy box exec` without the wall env after a walled `up`) | ✅ | 2026-09-13 | as above |
| CI tiers 1–3 (unit/golden/TUI, the example stack up → serve → down, the proxy and box e2es) | ✅ every push, on `ubuntu-latest` — inside a docker-CLI container mirroring the dev box, so `in_box()` is true and the machine + host gates are bypassed | continuous | [DEVELOPMENT.md](../DEVELOPMENT.md#ci-githubworkflowsfoldyardyml--foldyard-e2eyml) |
| **The `lima` backend on a GitHub-hosted runner** (`ubuntu-24.04`, x86, `/dev/kvm` via `modprobe kvm; chown $USER /dev/kvm`, Lima 2.2.0 tarball, QEMU 8.2, host podman 4.9.3 CLI → podman in a Fedora 44 guest) — the phase-0 spike: `machine ensure → up → verify → state → down → machine stop → rm` against the example copied out, as 5 independent attempts × {walls off, walls on} on fresh runners, **no retry wrapper** | ✅ **10/10 green** (+2/2 in the shake-out run). QEMU start → Lima READY 29–41 s; `machine ensure` 39–79 s incl. the 13 s image download; `fy up` 24–30 s (api build + Postgres pull, warm registry); `verify` 2–3 s ALL PASS (rootless, escape refused, PID-1 mount table clean); `fy state` all ✓ — except the stack tier reads "engine unreachable" host-side while `fy ps` reaches it (unexplained; a live-probe test is the place to pin it); walls on: `[machine].wall` + `host_wall` via `fy up` with `[proxy]` declared — host table loaded on the VM's own cgroup scope, direct guest egress refused (`curl --noproxy '*'` connect-refused in 19 ms), DNS resolves, `https://example.com` via the proxy 200; `machine stop` stops the supervisor; `rm` clean. Whole attempt 1 m 35 s – 2 m 22 s | 2026-09-17 | [run 35222981855](https://github.com/twistco/foldyard/actions/runs/35222981855) (spike workflow, since deleted — the job it became is `lima-host-e2e` in [DEVELOPMENT.md](../DEVELOPMENT.md#ci-githubworkflowsfoldyardyml--foldyard-e2eyml)) |
| **The host tier as CI tests** (`lima-host-e2e`: `tests/test_*_e2e.py` over `tests/e2e_host.py`, on the runner's Lima/QEMU VM — Fedora 44 guest, podman 5.8.4; host podman CLI 4.9.3) | ✅ `test_e2e.py` (the example + worktree stacks through the REAL host path: machine ensure → adopt gate → supervisor → compose); `test_box_e2e.py` (`fy box up` on the VM, in-box `fy ps` over `CONTAINER_HOST`, in-box `fy verify` ALL PASS, `box down`); `test_host_daemons_e2e.py` (`fy mode fakecred=on fakedep=on` → the fake minter up, the overlay re-rendered by the supervisor, the api reports `feature: on`, blocked-daemons empty → off); `test_machine_e2e.py` (ensure idempotent; `stop` stops the supervisor — heartbeat stale — and keeps the VM; a SIGKILLed QEMU recovered by `ensure`; `recreate`); `test_probes_e2e.py` (the read-only engine probes in-process — after the podman-4.9 `.Label` fix below; `fy state` clean; `fy doctor` no fail); `test_verify_e2e.py` ALL PASS on the boundary; `test_wall_e2e.py` (`[machine].wall` + `host_wall` via `fy up`: host table on the VM's own scope, direct guest egress refused, DNS resolves, proxy 200, the stale-provisioning refusal of a verb run without the wall config). 28 of 30 green on the first full run, job ≈15 min | 2026-09-17 | [DEVELOPMENT.md](../DEVELOPMENT.md#test-tiers) (tier 4), [run 35231657712](https://github.com/twistco/foldyard/actions/runs/35231657712) |
| **podman 4.9.3's `ps --format` has no `{{.Label "k"}}`** (Ubuntu 24.04's package, i.e. what a stock LTS host has) | ❌→✅ the accessor is a 5.x template function; on a 4.9 CLI it is a template error, a non-zero exit, and every probe built on it — the workspace cards, the reconciler's stack tier, `fy state` — read "engine unreachable" while `fy ps` reached the VM. Now `{{json .Labels}}` via `devmode.ps_labels` (a map from podman, a `k=v` string from docker) | 2026-09-17 | the host tier's first catch; unit-pinned in `tests/test_devmode.py` |
| **A restarted Fedora/QEMU guest with the host's `/home/<user>` mounted at `/home/<user>`** | ❌ **open**: after `machine stop` → `limactl edit` (add the mount, read-only 9p) → `machine ensure`, the guest's rootless podman accepts connections but cannot create ANY container — crun fails inside the merged rootfs (`open …/merged/etc/resolv.conf: No such file`, `mkdir /run/secrets: EPERM`; `--userns=keep-id`'s copy: `lchown bin: EPERM`); native in the guest as well as remote; the store's ownership is intact (12,333 files at uid 1001, postgres dirs at subuid 524357, none root-owned); the subuid map is unchanged; `podman system migrate` does nothing; removing the mount and restarting does not heal it; `fy machine recreate` does. A plain stop→start, a walled re-provision, and a SIGKILL→ensure all restart fine, so it is this mount, not restarts. The rig (Fedora 44 host, same guest) ran this exact recipe healthy on 2026-09-11. The verify negative therefore exposes the home at `/mnt/c` (an audit marker) instead; the home-path branch of the pattern stays unit-tested | 2026-09-17 | [run 35231657712](https://github.com/twistco/foldyard/actions/runs/35231657712) (the guest diagnostics are in the job log) |
| `ubuntu-24.04-arm` runners | ❌ **no KVM**: no `/dev/kvm` before or after `sudo modprobe kvm` (the module loads, no device — no EL2 for the guest). VM-backed CI is x86-only | 2026-09-17 | same run |

## Not yet validated on Linux

- **`fy worktree add` / `remove` on a Linux host** — a second stack in the same VM is validated
  (`test_e2e.py`'s worktree test runs on the lima job, above); the `worktree` verbs themselves,
  incl. `remove`'s teardown, are not.
- **The proxy/box e2e on a real Lima VM** (`tests/test_proxy_box_e2e.py` needs the test process
  INSIDE a container beside the box — the `live-e2e` container mirror covers it; the host tier's
  `test_box_e2e.py` covers the box itself on the VM).
- **`backend = "native"`** on a real Linux host (the host's own rootless podman, no VM) — the
  `native-host-e2e` runner job exercised it (an `ubuntu-latest` runner's rootless podman +
  `systemd --user` healthcheck timers) until 2026-09-17, when the `lima` job replaced it; nothing
  live covers it now. Whether `native` stays a product option at all (it was largely a CI
  stopgap; WSL2 is its remaining rationale) is an open product decision — see the outstanding
  work below.
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
- **`③` on Linux**: gVisor is the candidate; its cost is measured on micro-loops AND as the real
  box under a sustained build (the ③ row above), and decomposed: the file placement is the
  product's `shadow_volumes`/`caches` default (not a gVisor accommodation), and what is left is
  the syscall path. `--ignore-cgroups` and `host-uds` are settled.
  The route is settled and proven (a second runsc-default socket, isolation-layers.md, sixth
  session), the Mac's long build is measured (1.2× on the suite), and it is wired as
  `[machine].runtime = "gvisor"` (both VM backends) with the socket-narrowing filter as its
  enforcement tier (it strips `oci_runtime` / `dev.gvisor.*` / `HostConfig.Runtime` from the
  box's socket; the strip shown live on podman 6.1.1) — accepted as
  [ADR-0025](./adrs/0025-gvisor-machine-posture-and-socket-narrowing.md). What remains deferred
  is the broader mount/endpoint allowlist (ADR-0025 §Decision 4). Not a Linux-support blocker:
  the product claim without `③` is the same as the Mac's.
- **A CI job on a Linux *host* path** — done: `lima-host-e2e` boots the real lima/QEMU VM on an
  `ubuntu-24.04` runner (the row above; [DEVELOPMENT.md](../DEVELOPMENT.md#ci-githubworkflowsfoldyardyml--foldyard-e2eyml)).
  The old sentence here ("podman-machine / Lima VMs are flaky on GitHub-hosted runners") was
  never evidenced for Lima: its one citation was a libvirt permission failure. What the runner
  tier CANNOT reach: the `podman` backend (podman-machine on a runner — untried, no evidence
  either way), arm64 (no KVM), and anything needing nested virtualisation inside the guest — for
  those the nested-KVM-host recipe in [nested-virt.md](./nested-virt.md) remains.
- **Retire `backend = "native"`?** With a VM job in CI the backend's CI rationale is gone; what
  remains is the Linux/WSL2 "weaker boundary" product option. Retiring it means "foldyard always
  has a VM" — an ADR (it changes [isolation-layers.md](./isolation-layers.md) and
  `docs/configuration.md`) and the removal of `machine_backend.NativeBackend`. Decide before
  deleting anything.

## Recording a new validation

Add a row above with the date, the versions if they differ from the rig's, and a link to where
the measurement lives — [isolation-layers.md](./isolation-layers.md) for anything about the
boundary, the relevant design page otherwise. Move the item out of "Not yet validated" in the
same commit. A run that *failed* is a row too: the leaky-VM and the DNS findings both came from
runs that did not go as expected, and both fixed something.
