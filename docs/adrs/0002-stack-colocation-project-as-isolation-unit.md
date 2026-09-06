# ADR-0002 — Stack colocation: the project (whole compose stack), not the agent, is the unit of isolation

- **Status:** Accepted (2026-06-13) — implemented; positioning confirmed by the prior-art sweep
- **Sources:** README.md theses 1–2, PLAN.md §1 (vision & category), §2 (positioning) +
  Appendix A (prior-art research, 2026-06-13)

## Context

Agent sandboxes are plentiful — a crowded genre: Docker Sandboxes (microVM per *agent*, its own
engine, proprietary), nono (Landlock/Seatbelt process sandbox), srt / ags / yolobox /
agent-sandbox, Coder Boundary (process-level egress isolator). Isolated dev environments also
exist: VS Code devcontainers, Codespaces, Gitpod, Coder, Daytona. But each half misses the other:

- Devcontainers on Docker Desktop are a reproducible-toolchain container, **not a security
  boundary** (rootful daemon, `/Users` mounted, secrets passed via env — see ADR-0001).
- Cloud CDEs (Citrix Secure Developer Spaces, Gitpod/Ona, Coder) sell the exact value-prop
  *language* ("the laptop holds nothing") but are remote-first, enterprise, closed.
- Per-agent sandboxes constrain one process; the dev *stack* — databases, emulators, dev servers,
  the e2e loop — stays outside their boundary, on the host.

The deep prior-art sweep (2026-06-13, ~20 searches; PLAN Appendix A) confirmed the exact
combination — laptop-local, VM-bounded, secretless-by-default, whole-compose-stack + dev box +
IDE backend, host-side TTL credential posture — is **unoccupied as a tool**. Notably, *nobody*
does stack colocation, and nobody does host-enforced credential posture for humans locally.

The motivating threat is supply-chain, not the agent: npm/PyPI infostealer worms (Shai-Hulud and
its successors) execute on developer laptops via install scripts and poisoned transitive deps —
and the worms now also ride VS Code tasks, git hooks, and dev-server runtime, so install-only
sandboxing (Socket safe-npm, npq, ignore-scripts) demonstrably under-covers. `npm install` is
RCE; the question is what it can reach when it runs.

## Decision

**The unit of isolation is the project's whole dev stack, not an agent process.** Everything that
executes untrusted code — dependency installs, dev servers, compose services, the IDE backend,
and optionally a coding agent holding the engine socket — runs together inside the same
rootless-VM boundary (ADR-0001) that holds **zero credentials**.

What the colocation buys, concretely: the **real compose stack**, the **real e2e loop**, and
**services addressed by container name** all live inside the fence. The gap in the landscape is
precisely an environment that gives the agent — *or just you* — those three things inside a
boundary with nothing to steal.

Two personas, one tool:

1. **Agent autonomy** (the original motivation): hand the agent the machine's Podman socket —
   full `compose`/build/run — bounded to repo + containers because the VM is rootless and mounts
   only the repo.
2. **Supply-chain defense** (the wider audience): in the yard a malicious install script finds no
   `~/.ssh`, no tokens, no browser profile, no other repos, no push capability. This persona
   needs **no agent at all** — "do your normal dev work, but inside the fence." Deny-the-loot,
   where everyone else does detect-the-malware.

**Clean-repo precondition:** secretless is a precondition foldyard *relies on*, not a
transformation it performs. The yard mounts only the repo, so the guarantee holds precisely when
the repo carries config, not credentials — adoption starts with a gitleaks/TruffleHog history
scan, and anything surfaced moves into a host-side posture (ADR-0005).

## Consequences

- foldyard deliberately does **not** enter the agent-sandbox genre. The agent is a *plugin* on
  principle — it keeps the core honest about not being an agent sandbox, and codex/opencode
  siblings strengthen that story. Process-level sandboxes (nono) are complementary: they
  constrain the process, foldyard constrains the environment; a nono-wrapped agent can run inside
  the yard.
- Because the whole stack is inside, nothing about the dev loop degrades: the spike's gate was
  the full stack + Playwright e2e at 29/29 with zero secrets, inside the boundary (PROGRESS.md,
  2026-06-08). Isolation that breaks the e2e loop would just get turned off.
- The compose files, project recipes, and toolchain image remain the **consumer's** payload
  ("bring your own stack"); foldyard is the fence and the runner, not the stack (ADR-0014).
- The overlap window is named honestly: declaring a credential posture while running an untrusted
  install step is the one moment install-time code and a live token coincide (README).
- Positioning and launch language follow directly: "your whole compose stack inside the
  boundary", "git push is impossible from inside", "the local, open-source answer to the
  secure-CDE pitch".

## Rejected alternatives

- **Per-agent sandbox** (the crowded genre) — leaves the stack, the installs, and the IDE backend
  on the host; the worm doesn't care which process was sandboxed.
- **Isolation by hiding** (nested daemons, separate engines the GUI can't see) — costs
  visibility for no security gain once the machine is rootless (ADR-0001).
- **Cloud CDE** — the right value prop, wrong locus: remote-first and closed; the laptop-local,
  individual, open version of that pitch is exactly the unoccupied niche.
- **Install-time screening only** (safe-npm, npq, lifecycle-script defaults) — sandboxes the
  install decision, not the dev loop; complementary, not sufficient.
