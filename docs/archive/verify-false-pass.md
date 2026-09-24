# `fy verify` can report ALL PASS while proving nothing

> **What it was:** the record of four ways `fy verify`'s boundary battery went green (or red)
> without checking what it claimed, 2026-09-07 to 09-13. **Status:** all fixed — see
> `src/foldyard/verify.py` (`_vm_boundary`, `_host_paths`, `_mountpoint`) and the tests named
> below; the leaky-home negative runs in CI (`tests/test_verify_e2e.py`). **Superseded by:** the
> verify table in [isolation-layers.md](../isolation-layers.md#what-fy-verify-proves-per-platform).

The rule all four fixes share, for any check added later: **a negative check needs a positive
control.** A probe that failed to run is not a probe that found nothing.

## The first gap (2026-09-07): three checks passed because a command failed

A fresh krunkit VM, VM firewall on, no supervisor running, no images: every boundary check
printed PASS, yet `podman run … alpine` could not even pull (the proxy wasn't up).

| check | passed when |
| --- | --- |
| escape refused | `_engine_run_succeeds(--privileged --pid=host … cat /proc/1/ns/ipc)` is False |
| no `/Users` | `_engine_run_succeeds(--privileged … ls /Users)` is False |
| mount table clean | `podman run … mount` output has no host paths — **empty output qualified** |

`_engine_run_succeeds` couldn't tell "the escape was refused" from "the container never started".
And this was the *default*: the probe image (`alpine`, override `VERIFY_IMG`) comes from Docker
Hub, and with the VM firewall on — what `fy init` scaffolds — a cold image cache plus a stopped
supervisor guarantees the pull fails.

A second road to the same place: the host-path pattern was macOS-only
(`/Users|/private|/var/folders|/Volumes`), so the mount assertion passed vacuously on Linux and
WSL2.

**Fixed** (`fix(verify): a negative check needs a positive control`):

1. Run the probe image with `true` first; if it can't run, the boundary checks report that the
   battery **did not execute** — never PASS.
2. Empty mount output is not a clean table.
3. The host-path pattern is derived from the real host (`_host_paths`: your home matched exactly,
   plus foreign mount roots like `/mnt/c`, `/media`, `/run/host`), not from the OS name.
4. Prefer a probe image already in the VM.

Two more checks had the same shape and were fixed in the same pass:

- **`git push refused`** passed whenever `git ls-remote` failed for any reason — so a network
  outage certified "git push is impossible". Now only a credential refusal passes; an unreached
  origin is UNPROVEN.
- **`_wall_posture`** passed both refusal probes on an offline box. The permitted path (the proxy)
  is now the positive control.

## A second gap (2026-09-11): the probe reads the wrong mount namespace

On the Linux rig, a Lima VM deliberately mounting all of `/home/dain` (read-only 9p) passed "VM
mount table free of host home/paths" — the pattern was right, the probe looked in the wrong
place. `mount` inside a `--privileged` container prints the *container's* mount namespace:

| probe | shows the 9p mount at `/home/dain`? |
| --- | --- |
| `--privileged … mount` (as written) | no |
| `--privileged --pid=host … cat /proc/1/mounts` | **yes** |
| `--privileged -v /:/host … cat /host/proc/1/mounts` | yes |

**Fixed** (`fix(verify): read the VM's mount table from PID 1, not the probe container's`): the
audit reads `/proc/1/mounts` via `--pid=host` (world-readable, unlike `/proc/1/ns/*`), and the
`ls /Users` probe, which read the same wrong namespace, was folded in. Reading the real table
shows foldyard's own mounts — the repo and worktrees root, at their host paths — so the audit
exempts `machine.guest_mounts()` by **exact mountpoint** and nothing else: the home itself, a
sibling, or a sub-path of the repo still fail. Pinned by
`test_the_isolation_mount_set_is_not_a_leak` and
`test_vm_level_mount_hidden_from_the_container_namespace_is_still_a_fail`.

Closed end to end on the rig (2026-09-12): `fy verify` itself FAILED against the leaky VM
(`VM exposes host paths: … /home/dain 9p ro`) and PASSED once the mount was removed. The probe
image must be in the *VM's* store; a host-built image trips the positive control, as intended.
The same negative now runs in CI (`tests/test_verify_e2e.py`).

## A false FAIL (2026-09-13): the options field

The first in-box `fy verify` on a Linux host (Fedora guest, btrfs) reported
`VM exposes host paths: /dev/vda3 / btrfs rw,…,subvol=/root 0 0` — the VM's root filesystem. In
the box the process is uid 0, so the home pattern is `/root`, and btrfs puts `subvol=/root` in the
**options** field; the pattern was matched across the whole line.

A false FAIL is the same credibility problem from the other side: a battery that cries wolf
teaches people to ignore red. **Fixed** (`fix(verify): judge the mount audit by the mountpoint
field, not the whole line`): the pattern applies to field 2 only (`_mountpoint`). Pinned by
`test_a_host_path_string_in_the_mount_options_is_not_a_leak`.

## Under gVisor: the mount audit can't run from inside the box, by design

With `[machine] runtime = "gvisor"` the audit's `--privileged --pid=host` sibling reaches only the
sandbox, not the VM kernel — the same property the `escape refused` check proves. So in the box
the audit reports **N/A** (no effect on the exit code), not a warning and not a failure. `fy
verify` on the host runs it over the default (crun) socket, which can read the VM's PID 1 mounts.
Pinned by `test_mount_audit_under_gvisor_is_not_applicable_not_a_failure` and
`test_mount_audit_empty_is_still_a_fail_under_crun`.

The runtime filter narrows only the runtime opt-out, not what a box-created sibling may
bind-mount. On one VM per project such a sibling reads only this project's VM, which holds no
credentials; the broader mount/endpoint allowlist is deferred in
[ADR-0025](../adrs/0025-gvisor-machine-posture-and-socket-narrowing.md).

One lesson for fixtures: `git push refused` proves the refusal only against a **private** origin.
A public one answers `git ls-remote` without credentials and reads as "the box can push"; no
origin reads as UNPROVEN. Give a test repo a private-looking origin.
