# The competitor library

Deep-dives on the tools closest to foldyard, one file each. [../prior-art.md](../prior-art.md) is
the *landscape* — the whole neighbourhood, tiered, with one row per tool and the positioning
argument. This directory is the *library*: the handful of tools worth reading in full, read
against foldyard, each producing findings we can act on.

A tool earns a file here when reading its source or specs changes what we would build. A tool that
only needs a row stays in the landscape.

## The roster

| Tool | Unit of isolation | Why it's here | Reviewed | Verdict |
| --- | --- | --- | --- | --- |
| [gondolin.md](./gondolin.md) | one agent *task* (VM per turn) | closest **mechanism** neighbour — host-side egress mediation + placeholder secrets, independently reinvented; programmable VFS | 2026-07-20 @ `29fa74d` (v0.12.0) | not a competitor; 4 gaps found, 1 idea worth adapting |
| [vhrn.md](./vhrn.md) | one agent *run* in one project dir | closest **ergonomics** neighbour — same user, same daily loop, same operator verbs; and the best-specified egress proxy in the field | 2026-09-21 @ `0e3926d` (v0.5.1) | not a competitor on the unit; 6 gaps found, contract + address discipline worth stealing |
| [openshell.md](./openshell.md) | one agent *sandbox* in a gateway-managed fleet | the vendor-scale **reference for agent-side rigour**: no network in the workload, per-program egress, L7 rules, address discipline, a consequence-diff policy review | 2026-10-01 @ `1ad4e428a` (v0.1.2) | not a competitor on the unit; 8 gaps found; composable (agent sandbox inside the VM) but not yet worth it |

## How foldyard compares

The properties that actually separate these tools. Filled from the reviews above — every cell is a
claim some entry file defends. foldyard's column is kept current (2026-10-01); the others are as of
each review.

| Property | foldyard | gondolin | vhrn | OpenShell |
| --- | --- | --- | --- | --- |
| Unit of isolation | the **project** (long-lived) | the **task** (VM per turn) | the **run** (container per invocation) | the **sandbox** (one agent, gateway-managed fleet) |
| Boundary | VM, always (ADR-0001, ADR-0027) | micro-VM (QEMU/libkrun) | container; VM only via Apple `container` | container (`--network none`, Landlock, seccomp); libkrun microVM opt-in |
| Whole compose stack inside | **yes** — the differentiator | no | no | no |
| Human dev loop (IDE, worktrees, TUI) | yes | no | no | no (an approvals TUI; SSH into a sandbox) |
| Egress enforcement without guest cooperation | only with the VM firewall, `[machine] firewall` (opt-in, Lima only; validated in CI since 2026-09-17) | always (host *is* the network) | always (in-container nftables, pre-privilege-drop) | always (no network in the workload; seccomp-notify broker) |
| Address discipline (internal ranges, rebinding) | **none** | blocked by default, connect-time recheck | IANA registry boundary + resolve-once-and-pin | internal ranges denied by default, loopback/link-local always; resolve-once-and-pin; policy DNS |
| Content-level policy | on every decrypted host (all but `passthrough` since ADR-0029) | everything parsed (HTTP/1.x only) | none (no TLS termination) | L7 rules (REST, GraphQL, MCP, WebSocket), audit/enforce; HTTP/1.1 only |
| Credential model | dummy in guest, minted + injected at the proxy (ADR-0007/0008) | placeholder in guest, substituted at the proxy | real token in the container, agent logs in itself | placeholder in workload, substituted by a supervisor *beside* it (host-side only on the VM driver) |
| Credential *minting* (short-lived, scoped) | yes | no (static values) | no | yes (gateway: OAuth2, Google SA, STS; SPIFFE) |
| Policy ownership | host-owned, adopt-gated (ADR-0022) | host-owned (SDK code) | host-owned, repo never read | gateway DB; image-policy fallback; agent proposals |
| Grant model | lifetime: `once`/`session`/`permanent` | SDK-declared per VM | scope: base/harness/global/project/run, with provenance | durable revisions; source + rationale, no approver, no expiry |
| Credential modes (switches, levels, TTL, auto-revert) | yes | no | run-scoped modes only | no (attach/detach) |
| Proxy's own containment | host process; host.env scrubbed from its env, but the addon reads it | host process (Node) | scratch container, no caps, 3 mounts | supervisor container: read-only rootfs, no caps; holds resolved secrets |
| Written proxy contract | none | limitations + security docs | 570-line normative spec + coverage ledger | RFC 0005 + a per-control best-practices page; no clause ledger seen |
| Filesystem policy (hide/shadow/audit) | shadow volumes only | programmable VFS (hide, ro, tmpfs-upper, audit hooks) | project dir only; config copied, not mounted | Landlock allowlist; no host mount by default; gitignore-filtered upload |
| Per-program attribution | none — every box occupant gets every injected credential | n/a (one command per VM) | none (one agent per container) | executable path + SHA-256 (TOFU) + ancestry |
| Workload process controls (uid, caps, nnp, LSM, seccomp) | box root in rootless userns, podman default caps; agents' own sandboxes switched off | not reviewed | non-root `dev`, no sudo, no caps | non-root, zero caps, `no_new_privs`, Landlock + seccomp, probed every launch |

Read down foldyard's column: the stack, the human dev loop and credential modes are ours alone
(minting is now shared with OpenShell); keeping every real credential off the VM is shared only
with gondolin. **Address discipline is now behind all three neighbours**; grant provenance and
workload process controls are behind two; per-program attribution (OpenShell) and a written proxy
contract (vhrn) are behind one each.
Address discipline and provenance are cheap; attribution has a cheap accident-grade first step
([openshell.md](./openshell.md#gaps-in-foldyard-this-review-found)).

## What an entry must contain

The shape every entry follows, in order. It exists because a summary of someone else's repo
is worthless three months later — a set of findings against ours is not.

1. **One-paragraph identification** — what it is, licence, author, and *the exact commit and date
   reviewed*. Claims rot; a pinned commit makes staleness visible.
2. **The verdict**, up front, as bullets: competitor or not, on which dimension, and what reading it
   changed.
3. **What it is** — mechanism and *scale* (lines of code, where the effort went). Scale is the
   honest guard against "we should just build that".
4. **Their model vs ours**, as a property-by-property table. Tables force the uncomfortable rows;
   prose lets them be skipped.
5. **Gaps in foldyard this review found** — the point of the exercise. Ordered by how much they
   matter, each with a fix shape, and each marked **verified against our source** or inferred.
   A review that finds nothing should say so explicitly.
6. **What to steal, in order** — and what deliberately not to, with the ADR that says why.
7. **The daylight** — what they don't have. This is the "why isn't this a switch-to-it question?"
   section, and writing it honestly is what keeps the positioning claims in
   [../prior-art.md](../prior-art.md) true.
8. **Composability: could it run inside foldyard, or foldyard inside it?** Answer it as an
   engineering question, not as a closing remark. Neighbours keep shipping as pluggable
   runtimes, so "we could steal the idea" and "we could run the thing" are different answers
   with different costs. Cover:
   - **Topologies**: where each piece would sit (host, VM, box, beside the box), and which one
     holds up.
   - **What it needs from foldyard's substrate**: engine and privileges, kernel features
     (checked against our guest), rootless podman, and the gVisor posture. Say which of these
     we already provide.
   - **Which layer owns what**: egress, credential injection, policy, grants. Two enforcement
     layers stacked in sequence must not move a real credential into the VM (ADR-0007) or a
     grant out of the host store (ADR-0022).
   - **What it would break**: in foldyard's invariants, in the box's real occupants (the
     coding agent, the VS Code server backend, the build tooling), and in its attribution
     (egress log, per-worktree listeners).
   - **What the composition buys** that neither has alone, the cheapest spike that would
     settle it, and a verdict.

   Then the short reprise: usable as a reference? as a component? as a competitor? (Entries
   before 2026-10 cover only this reprise, under "Could we use it anyway?".)
9. **Sources** — repo at commit, every doc read *in full*, every file of ours cross-checked.

## Maintenance

- **Findings are the deliverable.** A gap found here is not fixed here. Carry it into an issue or
  an ADR; the entry file records what was found and stays a snapshot of that review.
- **Re-review on a version bump that touches the compared properties**, not on a schedule. Gondolin and
  vhrn are fast-moving single-author projects, and OpenShell ships weekly with breaking minors; the roster's `Reviewed` column is the staleness
  signal, and a claim older than its subject's last release should be treated as unverified.
- **Cross-check before claiming a gap.** Every entry's most useful findings came from grepping
  foldyard's own source, not from reading theirs. Gondolin's gaps 1–3 were re-verified as still
  open by the vhrn review two months later, and the OpenShell review found one of them fixed and
  two of vhrn's narrowed — which is the library working.
- **The landscape and the library must agree.** When an entry changes a verdict, update that
  tool's row and the positioning bullets in [../prior-art.md](../prior-art.md) in the same commit.
- **Keep the comparison table above filled.** A new entry that cannot fill a row either found a genuinely
  new property (add it, and backfill the others) or wasn't read closely enough.

## Candidates not yet promoted

From the landscape's 2026-07 re-sweep, in rough order of how much a full read would teach us:
**matchlock** (Firecracker + nftables/gVisor + placeholder injection — the closest thing to
foldyard's enforcement stack in one tool), **Buildkite Cleanroom** (repo-declared deny-by-default
policy; the closest policy-philosophy cousin), **vmoat** (the only stack-colocation shape-match
found anywhere — watch for signs of life), and **container-use** (Dagger's agent workspace, for its
operator surface). None has been read at source.
