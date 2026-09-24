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

## How foldyard compares

The properties that actually separate these tools. Filled from the reviews above — every cell is a
claim some entry file defends. foldyard's column is kept current (2026-09-24); the others are as of
each review.

| Property | foldyard | gondolin | vhrn |
| --- | --- | --- | --- |
| Unit of isolation | the **project** (long-lived) | the **task** (VM per turn) | the **run** (container per invocation) |
| Boundary | VM, always (ADR-0001, ADR-0027) | micro-VM (QEMU/libkrun) | container; VM only via Apple `container` |
| Whole compose stack inside | **yes** — the differentiator | no | no |
| Human dev loop (IDE, worktrees, TUI) | yes | no | no |
| Egress enforcement without guest cooperation | only with the VM firewall, `[machine] firewall` (opt-in, Lima only; validated in CI since 2026-09-17) | always (host *is* the network) | always (in-container nftables, pre-privilege-drop) |
| Address discipline (internal ranges, rebinding) | **none** | blocked by default, connect-time recheck | IANA registry boundary + resolve-once-and-pin |
| Content-level policy | on every decrypted host (all but `passthrough` since ADR-0029) | everything parsed (HTTP/1.x only) | none (no TLS termination) |
| Credential model | dummy in guest, minted + injected at the proxy (ADR-0007/0008) | placeholder in guest, substituted at the proxy | real token in the container, agent logs in itself |
| Credential *minting* (short-lived, scoped) | yes | no (static values) | no |
| Policy ownership | host-owned, adopt-gated (ADR-0022) | host-owned (SDK code) | host-owned, repo never read |
| Grant model | lifetime: `once`/`session`/`permanent` | SDK-declared per VM | scope: base/harness/global/project/run, with provenance |
| Credential modes (switches, levels, TTL, auto-revert) | yes | no | run-scoped modes only |
| Proxy's own containment | host process, holds every secret | host process (Node) | scratch container, no caps, 3 mounts |
| Written proxy contract | none | limitations + security docs | 570-line normative spec + coverage ledger |
| Filesystem policy (hide/shadow/audit) | shadow volumes only | programmable VFS (hide, ro, tmpfs-upper, audit hooks) | project dir only; config copied, not mounted |

Read down foldyard's column: the stack, credential modes and credential minting are ours alone;
**address discipline, proxy containment, grant provenance and a written proxy contract are the four
places where both neighbours are ahead of us**, and all four are cheap.

## What an entry must contain

The shape both current entries follow, in order. It exists because a summary of someone else's repo
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
8. **Could we use it anyway** — as a reference, as a component, as a competitor.
9. **Sources** — repo at commit, every doc read *in full*, every file of ours cross-checked.

## Maintenance

- **Findings are the deliverable.** A gap found here is not fixed here. Carry it into an issue or
  an ADR; the entry file records what was found and stays a snapshot of that review.
- **Re-review on a version bump that touches the compared properties**, not on a schedule. Both current
  entries are fast-moving single-author projects; the roster's `Reviewed` column is the staleness
  signal, and a claim older than its subject's last release should be treated as unverified.
- **Cross-check before claiming a gap.** Both entries' most useful findings came from grepping
  foldyard's own source, not from reading theirs. Gondolin's gaps 1–3 were re-verified as still
  open by the vhrn review two months later — which is the library working.
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
