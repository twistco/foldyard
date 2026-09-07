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
