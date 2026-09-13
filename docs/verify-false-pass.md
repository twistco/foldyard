# `fy verify` can report ALL PASS while proving nothing

Found 2026-09-07 while running the isolation battery against a fresh krunkit VM. Filed
separately from the microVM work because it is not about the VMM: it is a defect in the
**credibility check itself**, and it fires on foldyard's *default* posture.

## What happened

First run against a brand-new machine, wall on, no `fy host` running:

```
✓ PASS  engine is rootless
✓ PASS  escape refused (--privileged --pid=host can't read host PID1 ns)
✓ PASS  no /Users inside a --privileged container
✓ PASS  VM mount table free of host home/paths
✓ verify: ALL PASS — isolation intact.
```

The VM had **no images at all** and could not run a container:

```
$ podman --remote --url unix://…/podman.sock images     # (empty)
$ podman --remote --url unix://…/podman.sock run --rm alpine echo hi
Error: … pinging container registry registry-1.docker.io: … proxyconnect tcp:
dial tcp 192.168.5.2:41800: connect: connection refused
```

Every one of those PASSes was vacuous.

## Why — three of the four checks pass *because a command failed*

`verify._vm_boundary` asserts absence, so its checks are negative:

| check | passes when |
| --- | --- |
| escape refused | `_engine_run_succeeds(--privileged --pid=host … cat /proc/1/ns/ipc)` is **False** |
| no `/Users` | `_engine_run_succeeds(--privileged … ls /Users)` is **False** |
| mount table clean | `podman run … mount` stdout has no host paths — **empty stdout qualifies** |

`_engine_run_succeeds` cannot distinguish *"the escape was refused"* from *"the container never
started"*. An unavailable probe image makes all three report success, and the third is worse
still: it greps stdout that is empty precisely because nothing ran.

## Why this is the default, not an edge case

The probe image is `alpine` (override: `VERIFY_IMG`), pulled from Docker Hub. With
`[machine].wall = true` — what `fy init` scaffolds — egress is default-deny except to the
host-side proxy. So on a machine whose image cache is cold and whose supervisor is not running,
the pull *must* fail, and `verify` *must* print ALL PASS. The most locked-down configuration is
the one where the credibility check silently stops checking.

## The same class as the macOS-only mount regex

```python
_HOST_PATHS = re.compile(r"/Users|/private|/var/folders|/Volumes")
```

Every member is macOS-only, so the mount assertion also passes vacuously on any Linux or WSL2
host. Two different roads to the same destination: **`verify` reporting green while asserting
nothing.** For the surface CLAUDE.md calls "the product's credibility check", a false pass is
strictly worse than a false failure — it is the one failure mode that cannot be noticed.

## The fix (APPLIED 2026-09-07 — `fix(verify): a negative check needs a positive control`)

1. **Assert the probe works before trusting any negative check.** Run `<probe> true` first; if it
   cannot run, the boundary checks must report ERROR/FAIL — never PASS. This is a precondition,
   not a new assertion, so it strengthens the battery rather than weakening it.
2. **Do not treat empty stdout as a clean mount table.** Require the `mount` output to be
   non-empty before concluding it lacks host paths.
3. **Give `_HOST_PATHS` host-appropriate members** (`/mnt/c`, `/media`, `/run/host`, host `$HOME`
   outside the repo), chosen from what the host looks like rather than `sys.platform`.
4. **Prefer a probe image that is already local** (or pre-pull through the proxy) so a walled VM
   with a cold cache does not silently disarm the battery.

Item 1 is the load-bearing one: with it, the run that started this note fails loudly instead of
congratulating itself. Verified against the same live VM — cold cache now exits 1 with an
explanation, a genuine boundary still reports ALL PASS.

Two further cases the audit found beyond the three above, fixed in the same pass:

- **`git push refused`** passed whenever `git ls-remote` failed *for any reason*, so a box with no
  egress certified the product's headline claim ("git push is impossible from inside") from a
  network outage. Now classifies stderr: only a credential refusal passes; an unreached origin
  reports UNPROVEN.
- **`_wall_posture`** passed both refusal probes on an offline box — its own docstring already
  admitted this ("an offline Mac also fails these — a false PASS"). The permitted path (the proxy
  the wall funnels into) is now the positive control.

The unifying rule, and the one to apply to any check added later: **a negative check needs a
positive control.** `_stack_health` already worked this way and says so in its own docstring — "a
probe that FAILED is not a probe that found nothing" — so this was foldyard's own principle,
applied in one section and missing from the other three.

## A second gap (2026-09-11): the probe reads the wrong mount namespace

Found on the Linux rig by the deliberate leak the isolation work called for: a probe Lima VM
that mounts **all of `/home/dain`** (read-only, 9p) passes `fy verify` — "VM mount table free of
host home/paths" ✓ — even though `_host_paths()` now matches `/home/dain` correctly.

Cause: the mount-table probe runs `mount` inside a `--privileged` container, which prints the
*container's* mount namespace. VM-level mounts are not in it. The `ls /Users` probe has the same
shape and passes for the same reason on every platform. Measured on the leaky VM:

| probe | shows the 9p mount at `/home/dain`? |
| --- | --- |
| `--privileged … mount` (as written) | no |
| `--privileged --pid=host … cat /proc/1/mounts` | **yes** |
| `--privileged -v /:/host … cat /host/proc/1/mounts` | **yes** |
| `--privileged -v /:/host … ls /host/home/dain` | yes (lists the host home) |

So the two "repo-only mount" ✅s in
[isolation-layers.md](./isolation-layers.md#what-fy-verify-proves-per-platform) are, today,
proven by the mount configuration `machine ensure` writes and not by `verify`. This is the same
class as the two above — green while asserting nothing — reached by a third road: the probe ran,
produced non-empty output, and looked in the wrong place.

**Fixed the same day** (`fix(verify): read the VM's mount table from PID 1, not the probe
container's`). The mount audit now reads `/proc/1/mounts` from `--pid=host` — the escape probe
already uses that flag, and `/proc/1/mounts` is world-readable where `/proc/1/ns/*` is not, so
the two coexist. The container-side `ls /Users` probe was folded into it: it read the same wrong
namespace, and `/Users` is a member of the foreign-mounts list the audit greps for.

Reading the real table has a consequence the old probe never met: it *sees the mounts foldyard
itself makes* — the repo and the worktrees root, at their host paths — and on a Mac those sit
under `/Users/<you>`, so the first live run failed on its own repo mount. The audit therefore
exempts `machine.guest_mounts()` by **exact mountpoint** and nothing else: the home itself, a
sibling under it, or a bind at a sub-path of the repo all still fail (`test_the_isolation_mount_set_is_not_a_leak`).
The positive control above still gates it, the test fixture answers a bare `mount` with a clean
table so a regression to the container view goes red, and the leaky-VM case is pinned by
`test_vm_level_mount_hidden_from_the_container_namespace_is_still_a_fail`. Run live on a Mac
(Lima/vz): the table shows exactly the two mounts and passes.

**Closed on the rig (2026-09-12):** `verify` itself — not the probe by hand — was run against a
Lima/QEMU VM deliberately mounting the whole home (`mounts += /home/dain`, read-only 9p). It
FAILED with `VM exposes host paths: … /home/dain 9p ro`, and PASSED (`VM mount table (PID 1's
namespace) free of host home/paths beyond the repo mounts`) once the leak was removed — the two
directions the fix promised, end to end. One thing the run also confirmed: the probe image must
live in the *VM's* podman store, not the host's — a host-built image the VM can't run trips the
positive control (`probe image … could not run … the boundary battery DID NOT EXECUTE`) rather
than passing vacuously, which is the control doing its job.

## A false FAIL (2026-09-13): the options field

The opposite failure, from the first in-box `fy verify` on a Linux host (Lima/QEMU, a Fedora
guest). The audit reported `VM exposes host paths: /dev/vda3 / btrfs rw,…,subvol=/root 0 0`
— the VM's *root filesystem*. Inside the box the process runs as uid 0, so the home the leak
pattern looks for is `/root`; the btrfs root line carries `subvol=/root` in its **options**
field; and the pattern was searched across the whole line. The mountpoint is `/`. Nothing of the
host is in that line.

A false FAIL is not a security hole, but it is the same credibility problem from the other side:
a battery that cries wolf on a sound boundary teaches people to read past its red. Fixed the same
day (`fix(verify): judge the mount audit by the mountpoint field, not the whole line`): the
pattern is applied to field 2 only — the only field a leak can live in, since a 9p/virtiofs
source is a tag and a bind's source is a device — and the repo-mount exemption is unchanged.
Pinned by `test_a_host_path_string_in_the_mount_options_is_not_a_leak`, which also keeps the
same path *as* a mountpoint a FAIL. A Mac's Ubuntu guest (ext4) never showed it; any btrfs guest
would have.

## Under gVisor (2026-09-13): the mount audit cannot run from inside the box, by design

`[machine].runtime = "gvisor"` (docs/configuration.md) runs the box under gVisor's userspace
kernel. The VM mount audit reads the VM's PID-1 mount table through a `--privileged --pid=host`
sibling — but that reach into the VM kernel is exactly what gVisor blocks, and it is the SAME
property the `escape refused` check proves is blocked. So under the posture the sibling reaches
only the sandbox, `/proc/1/mounts` yields nothing, and the audit cannot execute.

That is not a leak and not a failed probe, so it is reported as an **advisory** (`⚠`, no effect
on the exit code) naming the reason and where the boundary IS checked — host-side, or on a crun
box — never as a silent PASS. The `escape refused`, credential-absence and direct-egress checks
still run and still assert. Pinned by `test_mount_audit_under_gvisor_warns_instead_of_failing`
(and `test_mount_audit_empty_is_still_a_fail_under_crun`, so the crun path keeps its FAIL on an
empty table). The VM's mount set is fixed by `machine ensure` (repo + worktrees only); the audit
re-checks it, and under gVisor that re-check moves outside the sandbox. The socket-narrowing
filter that landed 2026-09-13 narrows only the RUNTIME opt-out (`oci_runtime` / `dev.gvisor.*`
off every create) — it does not restrict what a box-created sibling may mount, so the host-side
mount assertion this note calls for is still owed, now as part of the broader mount/endpoint
allowlist (isolation-layers.md "Socket narrowing"), not the runtime filter.

One more thing the same run taught about the `git push refused` check: it proves the refusal
only against a **private** origin. A public one answers `git ls-remote` without credentials, so
the box reads as "REACHABLE — the box can push"; a repo with no origin at all reads as UNPROVEN.
Neither is a bug in the check — both are the positive control refusing to certify what it could
not test — but a fixture or a fresh `git init` needs a private-looking origin to get a PASS.
