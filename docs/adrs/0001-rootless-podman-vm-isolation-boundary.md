# ADR-0001 — Rootless Podman VM as the isolation boundary; repo-only mounts; escape-test-refused as the credibility gate

- **Status:** Accepted (2026-06-08, spike validated GREEN) — implemented (`src/foldyard/machine.py`,
  `src/foldyard/verify.py`)
- **Sources:** SPIKE.md (the original spike plan + rationale), PROGRESS.md (validation run-log),
  PLAN.md §6 (security model & honesty rules), README.md thesis 3

## Context

We wanted four things at once: **isolation from the laptop**, **GUI visibility**, **open source**,
and **agent autonomy** (hand the agent an engine socket and let it drive the whole stack). On
Docker Desktop these fight each other, for two confirmed reasons:

1. Its VM virtiofs-mounts **all of `/Users`, `/Volumes`, `/private`, `/var/folders`** (`rw`,
   `fakeowner`) — a container that escapes to VM-root reads/writes the entire home: SSH keys,
   browser profiles, every other project's volumes under `/var/lib/docker`.
2. Its engine is **rootful**, so a `--privileged` container can `nsenter -t 1` to VM-root and do
   exactly that.

The only isolation available there was to **hide the stack** on a daemon the GUI doesn't manage
(nest it, or move to Colima) — which costs the GUI. The threat model is not the agent itself: it
is prompt injection plus malicious npm/PyPI dependencies executing inside the dev environment.

## Decision

Run everything that executes untrusted code inside a **dedicated rootless Podman machine** (a
throwaway Linux VM) that mounts **only the repo plus the worktrees root** — nothing else. Two
properties combine into the boundary:

- **Rootless engine**: a container's "root" maps to an unprivileged uid *inside the VM*, so the
  `--privileged --pid=host nsenter -t 1` breakout is **refused** at the source, not filtered.
- **Restricted mounts**: `podman machine init --volume` **replaces** the default mounts rather
  than adding to them (proven empirically 2026-06-08: the VM's mount table shows a single
  virtiofs mount, the repo). There is no `/Users` to reach even at VM level.
  `machine._volumes()` codifies the mount set as exactly `{repo, worktrees-root}`.

**The VM is required everywhere, including Linux.** On *native* rootless Linux podman
the repo-only blast radius does not exist: the engine sees the user's whole filesystem, so a
socket-holding box could mount `~/.ssh` into a new container. The VM is not just kernel-exploit
defense — it is what makes "socket = repo + containers only" true at all, and it keeps the mount
scoping and escape test identical across OSes. A `native` backend exists only as an explicit
opt-in with a stated weaker profile.

**Escape-test-refused is the credibility gate.** `foldyard verify` (`src/foldyard/verify.py`)
automates the assertions that were first verified by hand:

- engine reports **rootless**;
- the escape probe — `--privileged --pid=host` reading `/proc/1/ns/ipc` — is **refused**
  (observed 2026-06-08: `cannot open /proc/1/ns/ipc: Permission denied`);
- no `/Users` inside a `--privileged` container;
- **mount audit**: the VM mount table is free of host paths (`/Users|/private|/var/folders|/Volumes`);
- in-box posture: no SSH agent, no `~/.ssh` key material, no `~/.netrc`, and `git ls-remote origin`
  fails — push is impossible by construction — plus mode-aware plugin posture checks.

Each check prints PASS/FAIL and the command exits non-zero on any FAIL (CI-usable). The honesty
rule this serves: never extract or ship code whose verify story is unvalidated. And the honest framing: the
battery tests the escapes we know about — **it raises assurance, it doesn't prove a negative.**

The boundary also **relies on a clean repo**: the yard mounts the repo, so the guarantee holds
when the repo carries config, not credentials. Adoption starts with a history scan
(gitleaks/TruffleHog), and the README commits `verify` to re-running a secret scan so the
precondition can't silently rot back in — secretless is a precondition foldyard depends on, not a
transformation it performs.

**Isolation without hiding.** Because the boundary does not depend on concealment, nothing needs
to be nested: the stack runs *directly* on the machine's engine, and Podman Desktop (Apache-2.0,
multi-engine) shows every container. On Docker Desktop, hiding was the only isolation; here,
visibility and isolation coexist because the isolation comes from the VM moat + rootless mapping,
not from where the daemon lives.

## Consequences

- Handing an agent the machine's Podman socket is safe *because of* this ADR: the socket's power
  is bounded to the repo and the machine's containers, never the host. A container escape lands
  the attacker inside a disposable VM with nothing mounted worth taking. "Safer autonomy, not
  less of it."
- The spike's empirical gate held: the full compose stack + Playwright e2e reached 29/29 with
  zero secrets on the rootless machine (2026-06-08), so no capability was lost to the boundary.
- Machine mounts are init-only: adding a mount (e.g. the co-dev checkout) requires
  `machine recreate`, and any new *declared* mount must be blessed by a positive mount-set check
  rather than silently widening the boundary.
- The dev box deliberately gets no push path (no `~/.ssh` mount, no agent forwarding, no tokens):
  malicious code can scribble on the local checkout (recoverable) but cannot push, destroy the
  remote, or exfiltrate via push.
- Network *monitoring* is cooperative (env-var proxying can be unset by malicious code);
  *enforcement* is a separate milestone — don't market filtering until it's the locked kind.

## Rejected alternatives

- **Docker Desktop + nesting/Colima** — isolation only by hiding the stack from the GUI; rootful
  engine + `/Users` mount make the VM boundary porous anyway.
- **Native rootless Linux podman as the default boundary** — no repo-only blast radius (see
  above); kept only as an explicit, degraded opt-in backend.
- **Process-level sandboxes (nono, bwrap) as the boundary** — they constrain a *process*, not the
  environment; complementary layers that can run inside the yard, not substitutes for the VM
  (see ADR-0002).
