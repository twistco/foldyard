# ADR-0011 — Machine backends behind one libpod-socket contract: podman, lima, native

- **Status:** Accepted (2026-06; native backend + lima spike on main via #25) — implemented;
  lima mock-validated + first real multi-project Mac run 2026-07-05
- **Sources:** docs/lima-backend-scope.md, docs/podman-multi-vm-issue-26281.md,
  src/foldyard/machine_backend.py

## Context

On macOS, `podman machine` with the applehv/libkrun providers allows **one VM at a time** — the
providers gate on `RequireExclusiveActive` (upstream podman#26281). For foldyard that means two
projects are mutually exclusive: the second project's machine can't start while the first runs,
so there is no "open `box shell` in project A while working in project B". Starting a second
machine fails with a cryptic upstream error.

Separately, Linux/WSL2 and CI hosts already have a rootless podman socket and sometimes cannot
(or need not) run a VM at all — but the VM boundary is what makes
"socket = repo + containers only" true (ADR-0001), so a VM-less mode is a *weaker profile*, not a
convenience default.

The risk was forking the codebase per VM technology. Everything above the machine — compose,
worktrees, the box, the proxy — only ever needs one thing from it: a socket to talk to.

## Decision

**Wrap VM lifecycle behind a small backend ABC whose entire downstream contract is a libpod
socket** (`src/foldyard/machine_backend.py`). `machine.socket()` returns a podman `unix://` URI
whichever backend is active; `stack.py`'s `CONTAINER_HOST`/`DOCKER_HOST` export (ADR-0010),
compose, box, and worktrees are backend-blind — no `--connection` juggling anywhere.

The `Backend` ABC carries the lifecycle verbs (`exists`/`state`/`create`/`start`/`stop`/
`remove`/`mounts`/`socket`/`guest_socket`/`list_running`) plus one policy bit,
`supports_concurrent()`. Three implementations:

- **`PodmanBackend`** (the default at decision time — see the 2026-08-29 amendment below) — wraps
  `podman machine`; `supports_concurrent()` → `False`. The one-VM policy lives in `machine._start()`, which checks for another running
  machine and prints guidance (stop it yourself, or switch backends) instead of surfacing
  podman's error. This is the portable floor and unchanged behaviour for existing users. If
  upstream ever lifts #26281, flipping `supports_concurrent()` is the single switch.
- **`LimaBackend`** (`[machine].backend = "lima"` — opt-in at decision time, the default since the
  2026-08-29 amendment below) — wraps `limactl` + Lima's **podman
  template** (a real podman service in each VM, libpod socket forwarded to
  `<instanceDir>/sock/podman.sock`); `supports_concurrent()` → `True`, so per-project machines
  coexist. `create()` uses a `--set` expression that pins sizing and **replaces** the template's
  mounts with only foldyard's isolation set (repo + worktrees root) — the property `fy verify`
  asserts. Memory is emitted in MiB verbatim: a rounded-GiB string like `"3.91GiB"` is
  hard-rejected by macOS Virtualization.framework at every boot, bricking the VM (a real-Mac
  finding, fixed in `_memory_mib`).
- **`NativeBackend` (explicit opt-in, `[machine].backend = "native"`)** — no VM at all; foldyard
  targets the host's rootless podman socket directly. Useful for Linux/WSL2 dev and cheap CI
  coverage, but it **drops the VM boundary** (containers share the host kernel, and the engine
  can see the whole filesystem), hence it is never auto-selected — a consumer opts in knowingly.

Selection is `MACHINE_BACKEND` env → `[machine].backend` toml → a default, which at decision time
was `"podman"` and is `"lima"` since the 2026-08-29 amendment below; an unknown name falls back to
podman with a warning rather than crashing the CLI.

## Consequences

- Concurrent multi-project work is a config line, not an architecture change. Lima status is
  honest: unit-tested against mocked CLIs (`test_machine_backend.py` — selection, sockets, JSON
  parsing, the `--set` override), and the first **real** multi-project Mac run (2026-07-05)
  validated VM download/create/start end-to-end — while exposing a cross-project port collision
  fixed by per-project daemon port bands (`src/foldyard/ports.py`; see
  docs/lima-wall-machine-integration.md). The scope doc's "trickiest bits" (socket-forward
  stability across Lima versions, mount-replacement drift, sustained two-VM concurrency) remain
  the live validation checklist.
- Lima bought more than concurrency: owning VM provisioning (root `provision:` scripts) is what
  makes the fail-closed nftables wall possible (`[machine].wall`, ADR-0009) — a consequence of
  this decision, not a separate backend.
- Backend differences that *do* leak are contained in the backend: podman machine exposes a
  docker-compat guest socket at `/run/docker.sock`, Lima's template forwards a rootless
  `/run/user/<uid>/podman/podman.sock`, native returns the host socket — `guest_socket()`
  absorbs all three, so `box.py` mounts the right thing without knowing why.
- The native backend gives CI a real-engine e2e tier without KVM (ADR-0017), at the documented
  cost of the weaker isolation profile — `verify`'s VM-boundary claims do not hold there and
  must not be advertised as if they do.
- `fy machine stop|rm` verbs (plus a hidden `delete` alias for limactl muscle memory) round out
  the lifecycle so a Lima user never has to drop to `limactl` for routine teardown. They also stop
  the project-scoped host supervisor: without a VM no box can use its proxy, and retaining it could
  leave generated checkout state behind. `fy up` and `fy box up` start one before a box comes up.

## Rejected alternatives

- **Lima (or anything) replacing podman machine as the default:** the podman backend is
  zero-dependency, battle-tested here, and behaviour-preserving; Lima stays opt-in until its
  real-Mac checklist is green. *(Reversed 2026-08-29 — the checklist went green. See below.)*
- **VM-per-worktree instead of concurrent per-project VMs:** worktrees are already isolated as
  namespaced compose projects on one machine (ADR-0004); the scarce resource was *cross-project*
  concurrency, which is exactly what #26281 blocks.
- **Auto-selecting native when no VM tech is available:** silently trading away the isolation
  boundary that is the product's core claim; the weaker profile must be an explicit choice.
- **Abstracting at the CLI-verb level (a "run this podman-machine-ish command" shim):** the
  verbs differ too much (`limactl create --set` vs `podman machine init --volume`); the socket
  is the stable seam, so that is the contract.

## Amendment (2026-08-29) — lima becomes the DEFAULT backend

`config.machine_backend()` now falls back to `lima`, not `podman`. This reverses the rejected
alternative above, on the grounds it named: Lima stayed opt-in "until its real-Mac checklist is
green", and it has been green since the multi-project Mac run — meanwhile `foldyard init` has
scaffolded `backend = "lima"` + `wall = true` since it shipped, so **every consumer created by the
supported path was already on Lima**. The fallback was reaching only hand-written configs, and it
was quietly giving them the weaker of the two backends.

What tipped it beyond parity is that the gap is no longer just convenience. Two properties are
Lima-only, and both are load-bearing:

- **Concurrent per-project VMs.** podman machine on macOS allows one at a time (#26281), so a
  second project can't come up without stopping the first — and stopping it kills any open
  `box shell` / `code` session.
- **The in-VM fail-closed egress wall** (`[machine].wall`). podman machine's CoreOS appliance
  can't be provisioned with it. Without the wall, egress routing is *cooperative* — proxy env
  vars, which well-behaved software honours and malware need not. The wall is what makes the
  proxy the only way out, so defaulting to podman meant defaulting to the cooperative story on
  the product's central claim (ADR-0009).

**This ADDS a dependency rather than swapping one, and that's the honest cost.** The backend's
`cli` is the VM-lifecycle binary only; the engine CLI (`podman`, or `docker` where podman is
absent) is separate and required regardless, because it drives the socket the backend hands out.
So Lima is `limactl` *plus* podman, where the podman backend needed only what you already had.
That is the price of the two properties above, and it is why `backend = "podman"` remains fully
supported rather than deprecated.

Two mechanical consequences, both fixed here rather than left implicit:

- **"Default" is now a property, not a name.** `machine.ensure()` and `preflight` both used to
  special-case the literal `"podman"` to skip quietly when its CLI was missing — the intent being
  "don't abort over a backend the consumer never chose" (a Linux/CI host has no VM tooling and
  needs none). With the default flipped, that literal would have inverted the rule. Both now key
  on `config.machine_backend_explicit()`: silent skip when nobody named a backend, hard error when
  somebody did.
- **`brew install limactl` does not exist.** The install hint was interpolated from `backend.cli`,
  which is right for podman and wrong for Lima (the formula is `lima`). Now a per-backend
  `install_hint`, because the first thing a new default sends people to must not 404.

Existing consumers are unaffected in practice — `fy init` wrote the backend explicitly, and an
explicit value still wins. A repo that *relied* on the old fallback should pin
`backend = "podman"` rather than migrate implicitly: switching backend means a new VM, so the old
machine's box, named volumes and warm caches are orphaned rather than carried over.
