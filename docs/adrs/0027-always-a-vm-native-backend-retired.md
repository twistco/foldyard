# ADR-0027 — foldyard always has a VM: the `native` backend is retired

- **Status:** Accepted (2026-09-17) — **amends [ADR-0011](./0011-machine-backends-one-socket-contract.md)**
  (its third implementation is removed; the socket contract and the two VM backends stand) and
  **supersedes the "CI uses containers, not KVM" clause of
  [ADR-0017](./0017-nested-virt-validation-strategy.md)** (a real Lima/QEMU VM now runs in CI).
  Implemented, on `main` since #17 (released in 0.3.0): `machine_backend.NativeBackend` and its
  branches in `machine`, `preflight`, `init`, `sandbox` are gone; a config still naming it is
  told so.
- **Sources:** [linux-support.md](https://github.com/twistco/foldyard/blob/main/docs/linux-support.md) (the 2026-09-17 runner rows),
  [isolation-layers.md](https://github.com/twistco/foldyard/blob/main/docs/isolation-layers.md) (the Linux and WSL2 sections), the
  `lima-host-e2e` job in `.github/workflows/foldyard-e2e.yml`. Related:
  [0001](./0001-rootless-podman-vm-isolation-boundary.md) (the VM is required everywhere,
  including Linux), [0009](./0009-monitoring-cooperative-enforcement-locked.md) (the wall),
  [0025](./0025-gvisor-machine-posture-and-socket-narrowing.md) (the gVisor posture).

## Context

`backend = "native"` targeted the host's own rootless podman socket with no VM. ADR-0011 admitted
it as an explicit opt-in "with a stated weaker profile", never a default, for two reasons: Linux/
WSL2 convenience, and "a real-engine e2e tier in CI without KVM" (ADR-0017's consequence). Both
reasons are gone, and the profile turned out to be weaker than "weaker" says.

**1. The CI reason is gone.** On 2026-09-17 foldyard's default `lima` backend booted a QEMU/KVM
VM on GitHub-hosted `ubuntu-24.04` runners, 10/10 with no retry wrapper, and the whole host tier —
`machine ensure|stop|recreate`, the config-adopt gate, the supervisor, both walls, the box, verify's
negative, `reclaim`, the worktree verbs — runs there, 36/36 ([linux-support.md](https://github.com/twistco/foldyard/blob/main/docs/linux-support.md)).
The "VMs are flaky on runners" sentence that justified a VM-less CI backend was never evidenced
for Lima. `native-host-e2e` was deleted the same day; nothing live has covered `native` since.

**2. WSL2 does not rescue it.** Inside the distro the supervisor, the minters, the allow-store and
the engine share one kernel and one uid — Hyper-V separates Windows from the distro and puts
nothing between the agent and the credentials. `WSL2 + native` is bare-Linux `native`
([isolation-layers.md](https://github.com/twistco/foldyard/blob/main/docs/isolation-layers.md#wsl2--a-linux-host-whose-hyper-v-boundary-protects-the-wrong-asset)).
The consistent design there is the same as on Linux: a Lima/QEMU VM inside the distro, which the
stock WSL2 kernel supports on Windows 11 x86 (`/dev/kvm` after `wsl --shutdown`). Windows-on-ARM
boots the distro at EL1 and can never have KVM; that is a platform foldyard does not reach, not a
reason to ship a shape that reaches it without the boundary.

**3. "Weaker profile" was every product claim at once.** ADR-0001 says the VM is what makes
"socket = repo + containers only" true at all: with the engine on the host, a socket-holding box
can mount `~/.ssh` into a new container — no exploit needed. From there the rest follows
structurally, not as a list of caveats: no in-VM wall (ADR-0009: enforcement is not cooperative —
with `native` it was), no host-side wall (it matches the VM's own cgroup scope), no gVisor
posture (ADR-0025: it provisions the VM), and `verify` — "the product's credibility check" —
could not assert its VM-boundary claims and had to be told never to advertise them. What was
left was a compose runner with foldyard's posture UI on top, carrying the product's name.

**4. It cost more than a class.** Every VM-lifecycle verb carried a `BACKEND.name == "native"`
branch (`ensure` a silent skip, `not_running_reason`, `stop`/`rm`, `recreate` each a bespoke
refusal); `preflight`'s and `ensure`'s fail-closed message for a missing `limactl` offered
`backend = "native"` as one of three ways out — the exact "auto-select native" alternative
ADR-0011 rejected, one edit away; `init`'s scaffold, the example config and six doc pages
described it; and a per-platform column in the isolation page existed only to say ❌.

## Decision

**Remove the `native` backend. foldyard always has a VM.** `[machine].backend` is `"lima"`
(default) or `"podman"`; both are VM backends, so every claim in
[security.md](../security.md) holds for every backend.

- `machine_backend.NativeBackend` is deleted, with the `name == "native"` branches in
  `machine.ensure`, `not_running_reason`, the `stop`/`rm` guard and `recreate`, the `init`
  scaffold line, the example config's comment, and `sandbox`'s aside.
- **A config that still names it is told, loudly, on every command** and treated as the
  unknown-name case ADR-0011 already defined: a warning naming this ADR, then the `podman`
  backend — a VM backend, so the fall-back is in the safe direction (the boundary appears rather
  than disappears), and it is a warning rather than an abort for the reason ADR-0011 gave (a
  config value must not make `fy docs` unusable). Switching backend means a new VM; the
  message says to pick `lima` or `podman` and adopt.
- The fail-closed message for a missing `limactl` on the inherited default offers two ways out
  (install it, or name `podman`), never a VM-less one.
- The WSL2 route is Lima/QEMU inside the distro (Windows 11 x86, nested virtualisation on,
  automount off); it stays "not yet validated" in [linux-support.md](https://github.com/twistco/foldyard/blob/main/docs/linux-support.md) until
  someone runs the two-minute `/dev/kvm` check on a real machine. Windows-on-ARM is out of scope.
- ADR-0017's "keep CI on containers, no KVM job" clause is superseded by the `lima-host-e2e`
  job; its nested-virt rig remains the answer for what a runner VM cannot reach (the gVisor
  posture under nested virtualisation, arm64).

## Consequences

- The isolation story has one shape per platform: a VM whose mount table is the repo and the
  worktrees root, an engine inside it, the wall around it. `verify`'s per-platform table loses
  the column that only ever said ❌; `isolation-layers.md`'s Linux option *(b)* (microVM per
  container on a VM-less engine) stays on the page as the record of what it traded, marked as
  no longer offered.
- Linux/WSL2 hosts need `limactl` + QEMU + `/dev/kvm` (or `podman machine`), exactly as the
  runner recipe in [linux-support.md](https://github.com/twistco/foldyard/blob/main/docs/linux-support.md) installs them. A machine without any
  VM technology cannot run foldyard — before, it could run something that looked like foldyard.
- `machine.py` and `preflight` lose their VM-less branches, and the retired-name message is the
  one place `native` is still spelled in code.
- Consumers that had `backend = "native"` in a hand-written config see the warning on their
  next command and choose a VM backend; nothing is migrated implicitly (a backend switch is a
  new VM: box, volumes and caches are not carried over, as ADR-0011's amendment already says).

## Rejected alternatives

- **Keep it as "unsupported, at your own risk".** A warning label on the absence of the
  product's core claim, paid for with a branch in every lifecycle verb and a way out of the
  fail-closed default that leads straight to the rejected auto-select. Nothing live would cover
  it, so it would rot silently.
- **Keep it for WSL2.** Rested on the idea that Hyper-V is "already the boundary"; it protects
  Windows, not the credentials, which live inside the distro with the engine.
- **Keep it for CI without KVM.** `/dev/kvm` is present on x86 GitHub-hosted runners and a Lima
  VM boots there in under a minute; the job exists and is green. arm64 runners have no KVM, and a
  VM-less backend would not make the arm64 job test the product either.
- **Fail hard on `backend = "native"`.** Tempting for a retired security-relevant option, but it
  would take `fy docs`, `fy config` and `fy doctor` — the tools that explain the change — down
  with it. The unknown-name rule (loud warning, VM fallback) already exists and errs toward the
  boundary.
