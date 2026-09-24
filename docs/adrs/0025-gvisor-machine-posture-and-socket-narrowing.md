# ADR-0025 — gVisor as a machine posture (`[machine].runtime = "gvisor"`), with socket narrowing as its enforcement tier

- **Status:** Accepted (2026-09-13). The posture and the **runtime-narrowing** filter are built and
  live-validated on both VM backends, and the strip was **shown live on podman 6.1.1** the same
  day (Consequences); the **broader mount/endpoint allowlist** is deferred (see Decision §5 and
  Consequences). Layer ③ of
  [isolation-layers.md](https://github.com/twistco/foldyard/blob/main/docs/isolation-layers.md) — the living measurement page — is the evidence
  behind every number here.
- **Sources:** the GCP nested-virt rig run-logs and `handover.md` (pre-merge working notes,
  internal); `../isolation-layers.md`. Related:
  [0001](./0001-rootless-podman-vm-isolation-boundary.md) (the VM boundary and the
  escape-test-refused credibility gate this builds on),
  [0010](./0010-podman-everywhere-container-host.md) (engine over `CONTAINER_HOST`, which is why
  the runtime knob had to be a socket, not a flag),
  [0011](./0011-machine-backends-one-socket-contract.md) (the backend ssh contract the posture
  rides), [0023](./0023-no-host-executed-code-from-the-repo-mount.md) (no HOST-executed repo-mount
  code — distinguished below).

## Context

foldyard's isolation boundary is the rootless podman VM ([ADR-0001](./0001-rootless-podman-vm-isolation-boundary.md)):
credentials never enter the VM (they stay host-side behind the egress proxy), the box mounts only
the repo and its worktrees, and `fy verify` proves the box cannot escape to the VM kernel. That
boundary rests on one assumption — that the VM kernel is not itself exploitable from inside a
container. Layer ③ of [isolation-layers.md](https://github.com/twistco/foldyard/blob/main/docs/isolation-layers.md) asks what a second boundary
would cost: run the dev box under **gVisor** (`runsc`), a userspace kernel written in Go behind a
seccomp filter, so a container-to-kernel exploit has to defeat gVisor's Sentry before it reaches
the VM kernel that holds the engine socket, the stack and the mount.

Four findings from the rig and the Mac (all in isolation-layers.md) shaped the decision:

1. **gVisor is affordable for a dev box, unlike a nested microVM.** Measured cost is ~1.2× on a
   Python test suite (Mac), ~1× on git walks, and higher only on fork/exec-heavy work (~11× on the
   rig's 9p mount). The libkrun microVM alternative was 7–30× on the operations a dev box does all
   day (Finding 2) — disqualifying. `--ignore-cgroups` is free (the box sets no per-container
   limits); directfs is load-bearing (off, git walks are ~9×).
2. **The engine socket is the design vector, and gVisor does not close it.** With
   `--host-uds=all` the box holds the *unfiltered* engine API: a sibling created with
   `--privileged -v /:/host` runs under the VM kernel and reads the VM home. So gVisor sandboxes
   the box's *syscalls* but not what the box can ask the *engine* to do. Socket narrowing is a
   **precondition** for any boundary claim, not an alternative to gVisor.
3. **podman's API has no per-container runtime selection at 5.8.** `podman-remote` has no
   `--runtime`; the libpod create endpoint's `oci_runtime` is ignored until 6.0; the compat
   `HostConfig.Runtime` is never read. So the box's runtime cannot be a flag foldyard passes — it
   must be a property of the *socket* the box holds. On podman ≥ 6.0 `oci_runtime` **is** honoured,
   so a client of the socket can pick its runtime — which is exactly what must be filtered out.
4. **gVisor reaches M1/M2 (no KVM needed) and Linux alike.** systrap is a seccomp platform;
   Dain wants the posture on the Mac too, keeping Linux only to shrink the support matrix.

The runtime-selection route was settled after prototyping all three shapes against the rig
socket: a **second podman API service in the VM whose default runtime is runsc**, reached over the
backend's own ssh (no VM restart, no Lima config change). The box is created through it and holds
it. Finding 3 then forces the last piece: because podman ≥ 6 would let the box ask for another
runtime through that same socket, the box must hold a *narrowed* view of it.

## Decision

**1. `[machine].runtime = "gvisor"` is a MACHINE posture, not a per-box knob.** The box must not
be able to opt itself — or a sibling it creates, or an in-box `fy up` stack — out of the sandbox.
So the posture is a property of the machine: `machine ensure` provisions it in the VM, and the
box's own engine socket is the sandboxed one. `native` (no VM) cannot take the posture. The Mac is
included, not just Linux.

**2. The mechanism is a second runsc-default API socket plus a box-facing narrowing filter.**
`machine ensure` provisions, user-level over the backend's ssh (no root, nothing in the boot
script; [ADR-0023](./0023-no-host-executed-code-from-the-repo-mount.md) untouched):

- a pinned, sha512-verified `runsc` release, and a wrapper (`runsc-fy`) with the flags fixed
  (`--ignore-cgroups --host-uds=all`) and **no `--allow-flag-override`** — so a client of the
  socket cannot reach runsc's flags (a widening annotation is refused; a narrowing one is
  honoured, the safe direction);
- the runtime *name* registered engine-wide as a drop-in (so the default crun service can still
  exec/stop/rm a gVisor box), and a `containers.conf` override making a **second** `podman system
  service` (`podman-runsc.service`) default to runsc;
- a **narrowing filter** (`assets/sandbox/socket_filter.py`, a further user unit) serving a third
  socket that forwards to the runsc socket but strips the runtime-selecting fields from every
  container-create — `oci_runtime` and `dev.gvisor.*` annotations (libpod), `HostConfig.Runtime`
  (compat) — and refuses a create body it cannot parse.

The **box mounts the filtered socket** as its `/var/run/docker.sock`; the **host creates the box
through the raw runsc socket** (a trusted create with no runtime field). So the runtime default is
a convenience for the host's own `fy box up`; the filter is the enforcement, and it holds on
podman ≥ 6 where the default alone would not.

**3. Fail closed, both ways.** `machine ensure` aborts if the socket does not answer with the
gVisor runtime (probed *through the filter* — the box's real path). `fy box up` removes a box that
came up under any other runtime before its bootstrap runs, so credentials are never handed to an
unsandboxed box. The filter refuses an unparseable create rather than forward it.

**4. Narrowing scope is the runtime opt-out only.** The filter strips runtime selection, nothing
else. It does **not** restrict what a box-created sibling may bind-mount, nor which engine
endpoints it may call. This is deliberate, with one qualification. foldyard runs one VM per
project, the *host-held* credentials (every minter's `host.env` secret, the operator's keychain,
`~/.foldyard`) never enter the VM, and the VM mounts only the repo and worktrees (verified — §5).
So a sibling that mounts the VM's `/` reads this project's own VM (its repo, worktrees and stack);
there is no *other* project in the VM to leak to. What the VM **does** hold is whatever the box
itself was handed: a non-keyless `[claude]`/`[codex]` box logs in for real, and that token lives
in the `devbox_claude_home` / `devbox_codex_home` named volumes on the VM's disk — the box's own
mounts. A root sibling that bind-mounts the volume store reads them, so "no credentials in the
VM" is only true of the host-held ones; but the box already reads those volumes, so the sibling
holds no principal the box did not, and the host boundary is unchanged. The mount/endpoint
allowlist (the broader "narrowing shape" from isolation-layers Finding 1) would close that reach
too; it is defence in depth against a shared-VM topology foldyard does not use, and is left as
optional future hardening rather than a blocker for this ADR. (The keyless rungs keep even the
agent token host-side — the proxy injects it in flight.)

**5. `verify` gains a kernel row and reclassifies the mount audit under the posture.** In-box,
`fy verify` checks the kernel the box actually runs on reports gVisor. The VM mount audit
(`--pid=host` into the VM's PID-1 mounts) cannot run inside a gVisor box — that reach is exactly
what the `escape refused` check proves is blocked — so it is reported as **not applicable** (`⊘`,
no exit effect), not an advisory: there is nothing to act on in-box, and the audit genuinely runs
at another layer (host-side `fy verify` probes over the default crun socket, which *can* read the
VM mounts; a crun box does the same). The crun path keeps its hard FAIL on an empty mount table.
This never weakens the credibility gate — the escape, credential-absence and egress checks all
still run and assert.

**6. inotify: polling for the host-edit case; no two-way sync.** File watches do not cross the VM
mount boundary *inward* — a watcher inside the box sees writes made inside the box, but not writes
made on the host (a VM-mount property, sharpened by gVisor's gofer). foldyard's model is that the
agent and the attached editor both write from *inside* the box, so in-box watchers fire on the
common path. The only gap is a host-side edit expected to hot-reload a dev server in the box, for
which the answer is polling (`CHOKIDAR_USEPOLLING`, Vite `usePolling`, webpack `watchOptions.poll`).
A Mutagen-style two-way sync into the VM disk would give native watches but replaces the mount and
changes what `verify`'s mount audit proves (there is no host mount to audit; the sync scope
becomes the claim) — rejected as too large a change for the benefit, revisitable if host-side
editing becomes a real workflow.

## Consequences

- **Switching runtime recreates the box, not the VM.** The runtime is fixed when a container is
  created, so `[machine].runtime` changes take effect on `fy box down && fy box up` (~20 s), the
  VM untouched (the filter reaches the socket over ssh; no VM restart). An already-up box from
  before the posture nags to recreate.
- **The strip's observable effect was shown live on podman 6.1.1 (2026-09-13).** Both shipped
  machine VMs run podman 5.8, which ignores `oci_runtime` regardless — so a live run there cannot
  distinguish "the filter stripped it" from "the engine ignored it", and day to day
  `tests/test_socket_filter.py` pins the strip against a recording upstream. The distinction was
  then demonstrated once on the Linux rig with a Fedora 45 nightly guest (Lima/QEMU, podman
  6.1.1): the same libpod create carrying `oci_runtime: crun` came up **crun** through the raw
  runsc socket (6 honours the field — the opt-out is real) and **`runsc-fy`** through the filtered
  socket (the strip fired); a malformed body got the 400. **podman 6 is still not needed for
  safety** — on 5.8 the socket default enforces and the filter is belt-and-braces. Moving the
  shipped guest to podman 6 stays a guest-*image* choice independent of this design, and not one
  to make yet: Fedora 45 is pre-release (nightly images only), and under Lima 2.2.0 that nightly
  refuses sshd the guest-agent socket (SELinux, `Enforcing`) — so Lima reports the VM DEGRADED,
  `limactl start` fails and its port forwards are dead, even though the guest and both podman
  sockets work over plain ssh. Debian 13 ships 5.4. Revisit at Fedora 45 GA. (The run also found
  and fixed a real foldyard bug the fresh image exposed: the boot-time wall left the VM user's
  `~/.config` root-owned — CHANGELOG.)
- **The filter is foldyard's own packaged code run in the GUEST, not host code from the repo
  mount** — so [ADR-0023](./0023-no-host-executed-code-from-the-repo-mount.md) is not in tension.
  It is stdlib-only and runs under the guest's `python3` (present on both backends' guests).
- **The posture is opt-in and off by default** (unset = the VM engine's own crun). The cost
  (§Context 1) and the inotify caveat (§Decision 6) are why it is not the default.
- **Still owed before the boundary is "proven, not only set" end to end:** the host-side mount
  assertion, which arrives with the broader mount/endpoint allowlist if that is ever pursued
  (§Decision 4). Until then the VM mount set is *set* by `machine ensure` and *audited* host-side.

## Rejected alternatives

- **A libkrun microVM for the box (option (c) stacked, the original 2026-09-11 plan).** 7–30× on
  fork/exec, package installs and small-file churn (Finding 2) — the dev box's whole workload.
  gVisor at ~1.2–3× is the affordable second boundary.
- **`[box].runtime` → `podman run --runtime` (prototyped, dropped).** `podman-remote` has no
  `--runtime`, and the libpod/compat runtime fields are ignored on 5.8. No foldyard topology can
  pass it (Finding 3).
- **The libpod REST `oci_runtime` route on a podman-6-only guest.** Works only on ≥ 6.0, forcing a
  guest-image bump for a mechanism the second-socket route delivers on 5.8 today. And it would
  still need the filter, because `oci_runtime` is exactly the field a box could set to opt out.
- **Engine-wide runsc (the whole VM, stack included).** A different product — Postgres and every
  service under gVisor — and `verify`'s probes would need re-reading. The posture is about the
  box, not the stack.
- **The annotation route (`--allow-flag-override` + `--annotation host-uds=all`).** Hands *every*
  runsc flag to whoever holds the socket, i.e. the box — the opposite of narrowing. Flags live in
  the wrapper, override off.
- **The mount/endpoint allowlist now.** Low residual risk on foldyard's one-VM-per-project,
  creds-host-side model (§Decision 4); deferred as optional hardening rather than built as a
  blocker.
