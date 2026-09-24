# Gondolin — earendil's micro-VM agent sandbox, read against foldyard

[Gondolin](https://github.com/earendil-works/gondolin) is Armin Ronacher's (Earendil Labs)
open-source agent sandbox: "local Linux micro-VMs with programmable network and filesystem
control." Public since 2026-02, Apache-2.0, v0.12.0 and very active as of this review
(2026-07-20, commit `29fa74d`). It was missed by the 2026-06 prior-art sweep — it predates it
by four months — and it is the closest *mechanism* neighbour foldyard has, so it gets its own
deep-dive. [prior-art.md](../prior-art.md) carries the one-row positioning summary.

## The verdict

- **Not a competitor — a different unit of isolation.** Gondolin sandboxes a *task*: a
  disposable QEMU/libkrun micro-VM (Alpine, sub-second boot, one command at a time) that an
  agent harness creates from a TypeScript SDK, uses, and throws away. foldyard isolates a
  *project*: a long-lived VM holding the whole compose stack, dev box, and IDE backend for a
  human-plus-agent dev loop. Neither can do the other's job.
- **It independently reinvents foldyard's two central bets**, which is strong validation:
  the host (not the guest) is the enforcement point for network and filesystem, and secrets
  never enter the untrusted environment — the guest holds a placeholder and the real value is
  substituted in-flight at the mediation point, per-destination-host. That is ADR-0007/0008,
  arrived at from the opposite end of the design space. (The 2026-07 re-sweep in
  [prior-art.md](../prior-art.md) found the same convergence across the wider per-task wave —
  vindicated, and therefore no longer differentiating; gondolin is simply the most
  substantial implementation of it.)
- **Reading it exposed three real foldyard gaps** (detailed below, fix-shapes included):
  the egress proxy will dial loopback/RFC1918 upstreams on the box's behalf — from *outside*
  the wall; `[[inject]]` rules using `query_param` leak the real minted token into
  `egress.jsonl`; and the DNS-tunneling residual that survives the wall is undocumented.
- **The programmable VFS is the genuinely new idea** — and the wrong thing to import
  wholesale. What transfers is its *policy vocabulary* (hide, read-only, tmpfs-upper,
  audit), which foldyard can get from kernel primitives it already uses. The FUSE-over-RPC
  mechanism itself would be fatal to a monorepo dev loop and requires owning a guest agent
  and hypervisor wiring that foldyard deliberately doesn't own.

## What gondolin is

A host-side TypeScript library + CLI (`npx @earendil-works/gondolin bash`) that boots a
minimal Alpine guest under QEMU (default; HVF on macOS, KVM on Linux) or experimental
libkrun. The guest runs four small Zig daemons wired to the host over virtio-serial:
`sandboxd` (exec), `sandboxfs` (FUSE → host RPC), and two loopback-only TCP forwarders for
SSH and ingress. vsock is deliberately avoided (portability, and a generic socket-to-host is
one accident away from a tunnel). Everything interesting — network policy, filesystem
semantics, secret substitution — is JavaScript running on the host.

Scale, for cost calibration: ~33k lines of host TypeScript (userspace network stack ~7.8k,
VFS ~4.5k including a vendored *patched fork of Node's unreleased VFS layer*
([node#61478](https://github.com/nodejs/node/pull/61478)), image build ~4.3k, HTTP mediation
~2.1k) plus ~6.4k lines of guest Zig. This is a serious, focused implementation investment by
a very capable team — worth remembering when an idea below looks temptingly importable.

Lifecycle philosophy (their docs, near-verbatim): VMs are disposable per task/turn; boot is
under a second so re-creation beats resumption; persistence goes through explicit VFS mounts
or disk-only qcow2 checkpoints; "managing persistence is hard, particularly with agents that
might touch completely random places — ideally you avoid this problem as much as you can."

## The programmable VFS

### How it works

The guest mounts one FUSE filesystem (`sandboxfs`, default at `/data`); every file operation
becomes an RPC over virtio-serial to the host, where a stack of JavaScript *providers*
implements the filesystem. Mounts are a map of absolute paths → providers, routed by longest
prefix, bind-mounted into place. Built-ins:

- `MemoryProvider` — in-RAM scratch space, gone on close.
- `RealFSProvider(hostPath)` — a real host directory; symlink escapes are blocked
  fail-closed (dangling symlinks too) for every follow-style operation.
- `ReadonlyProvider(inner)` — rejects mutations.
- `ShadowProvider(inner, {shouldShadow, writeMode})` — selectively *hides* paths: reads
  behave as ENOENT, entries vanish from `readdir`, and writes are either denied or diverted
  to a tmpfs upper layer. A `realpath()` check blocks trivial symlink bypasses.
- Custom providers, plus `before`/`after` hooks on every operation for auditing.

Their two canonical recipes are instantly recognisable from foldyard's world:

1. **Mount the repo but hide `/.env` and `/.npmrc`** (ShadowProvider, deny-writes).
2. **Shadow `/node_modules` with a tmpfs upper layer** so the guest `npm install`s its own
   copy without touching (or seeing) the host's — also recommended for `.git`, `.venv`,
   `dist`, and wrong-architecture build dirs.

Recipe 2 is exactly foldyard's **shadow volumes** (`box.py`), generalised from a
uid-squash workaround into a first-class, policy-driven layer. Recipe 1 is something
foldyard *cannot do at all* today.

Operational limits worth noting: RPC payloads are capped at 60 KiB per read/write, VFS data
is excluded from disk checkpoints, and the guest's CA trust bundle is itself injected
through this mount system (hide it by accident and TLS dies).

### What it would buy foldyard

- **The ADR-0021 problem class disappears structurally.** The git index corruption
  ([per-kernel index split](../adrs/0021-per-kernel-git-index-split.md)) exists because two
  kernels write one checkout over virtiofs. In gondolin's model there is only ever *one*
  kernel touching real files — the host's; the guest sees an RPC facade. They looked at the
  same wall foldyard hit and concluded a raw shared mount is untenable for an adversarial
  (or merely concurrent) guest. That's independent confirmation of ADR-0021's diagnosis,
  reached by paying the full price of the alternative.
- **Dirty-repo adoption.** foldyard's stated precondition is "it can mount a clean repo; it
  can't clean a dirty one" — secrets already in the checkout are visible to the box. A
  shadow layer over `.env`-class files would soften that into "mount it, hide the loot,"
  which matters for onboarding real-world repos.
- **A read-audit trail.** VFS hooks answer "what did the agent actually read?" — nothing in
  foldyard sees file access today; our observability stops at the egress log.
- **Per-worktree/per-session views** without copying: CoW branches of one checkout.

### Why not to import it

- **Performance is disqualifying for the dev-stack workload.** Every stat/read/write is a
  host round-trip with a 60 KiB data cap. Gondolin's target is a small per-task workspace;
  foldyard's is a monorepo where `pnpm install` does hundreds of thousands of operations and
  the daily loop *is* watchers (HMR, test watch) whose inotify semantics through FUSE-RPC
  range from degraded to absent. virtiofs is already the slow-but-bearable floor.
- **The implementation cost is real and permanent.** They vendor-patch an unreleased Node
  subsystem and own both ends: hypervisor device wiring and a guest daemon. foldyard owns
  neither — it drives stock podman/Lima and ships no guest agent beyond an nftables script,
  and that stock-components posture is a deliberate bet (ADR-0011), not an accident.
- **It solves an adversarial-guest problem foldyard scoped differently.** foldyard's
  boundary is the VM; inside it, the box is trusted-ish tooling on purpose ("isolation
  without hiding"). Gondolin needs the VFS because its guest is fully adversarial *and*
  gets host files mounted in.

### The middle path — steal the vocabulary, not the mechanism

The provider stack's policy vocabulary maps onto kernel primitives foldyard already
provisions at box-up:

| Gondolin provider | Native equivalent for foldyard |
| --- | --- |
| `ShadowProvider` + tmpfs upper | shadow volumes (shipped) — extend to arbitrary config'd paths |
| `ShadowProvider` deny-reads (`.env`) | tmpfs/empty-file overmounts on the mount list at box-create; crude but effective for a fixed path list |
| `ReadonlyProvider` | `:ro` bind mounts (podman supports per-volume ro) |
| per-session CoW views | overlayfs per worktree (lower = shared checkout ro, upper = named volume) — future direction; conflicts with the ADR-0001 "same path both sides" invariant and git's view of the world, so it's an ADR-sized decision, not a tweak |
| VFS `before`/`after` audit hooks | no native equivalent — the honest gap; fanotify in the VM is the closest sketch |

A `[box] hide = [".env", ".npmrc"]` config that overmounts those paths at box-create would
capture the highest-value recipe for a few dozen lines. The overlayfs worktree idea is worth
keeping on the shelf for when per-agent-session isolation inside one project becomes a need.

## Network egress confinement

### Their model

Gondolin gives the guest a normal `eth0` and **no NAT whatsoever**. The host *is* the
network peer: a userspace Ethernet/ARP/DHCP/ICMP/IPv4/TCP stack in TypeScript receives every
frame. Each outbound TCP flow is classified from its first bytes — explicit host-mapped TCP,
HTTP/1.x, TLS ClientHello, or (opt-in, allowlisted) SSH banner — and *everything else is
dropped before a real host socket exists*. Then:

- **HTTP/TLS flows are terminated and replayed.** TLS is MITM'd via SNI (local CA injected
  into the guest's trust store); the inner HTTP request is parsed, checked against a
  hostname allowlist, transformed by hooks, and re-issued by host `fetch`. `CONNECT` is
  denied outright. Redirects are followed *on the host*, each hop re-validated; the guest
  only ever sees the final response.
- **Internal ranges are blocked by default** (`blockInternalRanges: true`): loopback,
  RFC1918, link-local, CGNAT, metadata-style targets, and the IPv6 equivalents — with
  `allowedInternalHosts` as the scoped exception list.
- **DNS-rebinding is closed at connect time.** Policy resolves hostnames host-side, then a
  guarded `lookup()` in the HTTP client re-checks the *actual* connect-time IP. Guest DNS
  answers are ignored for policy entirely.
- **DNS itself is mode-switched.** Default `synthetic`: no upstream DNS at all — the host
  answers every A/AAAA with a fixed fake IP (policy doesn't need real answers, since the
  Host header + host-side resolution drive everything). This *eliminates DNS as an egress
  channel*. `trusted` mode forwards validated queries to host resolvers only — and their
  docs say plainly that this still permits classic DNS tunneling. `open` is open.
- **Secrets ride the same pipeline** — placeholder env vars in the guest, substitution into
  outbound headers only when the destination matches that secret's host allowlist, and a
  request that presents a placeholder to a *disallowed* host is **blocked**, not passed
  through. Basic-auth tokens are decoded/substituted/re-encoded; query-param substitution is
  opt-in "because it increases reflection risk"; bodies never.

The price of owning the whole data path: HTTP/1.x only (no h2/h3/QUIC/gRPC), no UDP beyond
DNS, WebSockets opaque after the 101, size caps on bodies, and ~10k lines of network code to
maintain. Their security doc is admirably blunt that mediation ≠ safety: "if you allow
`api.example.com`, malicious guest code can still send any data it can read to that host,"
and header-echoing servers can bounce secrets back out.

### Theirs vs foldyard's three layers

foldyard's discipline (ADR-0009) is *cooperative capture → proxy default-deny → nftables
wall*, built from stock components. Gondolin has no cooperative layer at all — there is
nothing to bypass because there is no NAT to fall back to. The honest comparison, per path:

| Property | gondolin | foldyard today |
| --- | --- | --- |
| Enforcement without guest cooperation | always (host is the network) | only with `[machine].wall` — now `[machine] firewall` (opt-in, Lima only; validated in CI since 2026-09-17) |
| Content-level policy (paths, methods, bodies) | always (everything parsed) | only decrypted hosts — `capture=on` at review time; every non-`passthrough` host since ADR-0029 |
| Protocol compatibility | HTTP/1.x + explicit exceptions; h2/QUIC/UDP break | full (passthrough tunnels anything TLS; wall denies UDP so QUIC falls back to TCP) |
| CONNECT | denied (it's a generic tunnel) | *is* the transport; the CONNECT target is the policy point under `default_deny` |
| Internal-range upstreams | blocked by default, connect-time re-checked | **not checked at all — see gap 1** |
| DNS as covert channel | eliminated (`synthetic` default) | survives the wall via local-resolver recursion — see gap 4 |
| Secret misuse toward wrong host | request blocked | header silently *not* rewritten (dummy goes out) — see gap 2 |
| Non-HTTP dev protocols (Postgres etc.) | explicit `tcp.hosts` raw mappings, no hooks/secrets | in-stack traffic never leaves the VM (`NO_PROXY`); nothing needed |

Their **egress capability matrix** doc pattern — one table stating, per egress path, how
destination selection works, the mediation level, and whether secrets/hooks apply — is worth
copying into [networking.md](../networking.md) for foldyard's own three paths (proxied,
NO_PROXY-direct in-VM, wall-denied).

## Gaps in foldyard this review found

Ordered by how much they matter. 1 and 2 were verified against
`src/foldyard/assets/proxy/egress_proxy.py` at review time, not just inferred from docs.

1. **The proxy will dial internal upstreams on the box's behalf — from outside the wall.**
   The addon's only gate is the hostname allowlist, and only when `default_deny = true`
   (now `[proxy] enforce`; `init` seeds `enforce = "learn"`, which records instead of refusing
   until the learn window ends). `mitmdump` runs on the host, so a box-side
   `curl -x $FY_PROXY https://127.0.0.1:8443/` (or `http://192.168.1.x/`,
   `http://169.254.169.254/`) makes the *host* connect to host loopback / the LAN — precisely
   the "probe arbitrary host-loopback services" the wall's port-band design exists to
   prevent, resurrected through the front door. Even with `default_deny` on, an allowlisted
   hostname that later resolves to an internal IP (rebinding) walks through, because policy
   never looks at IPs. **Fix shape:** in the addon, resolve the upstream and refuse
   loopback/RFC1918/link-local/CGNAT destinations by default (both at CONNECT and at
   connect-time), with a scoped `allow_internal` exception list à la gondolin's
   `allowedInternalHosts` — the gcp metadata emulator and friends are compose-internal and
   never transit the proxy, so the exception list should start empty.
2. **No tripwire when a dummy credential heads to the wrong host.** Gondolin *blocks* any
   request that presents a secret placeholder to a host outside that secret's allowlist.
   foldyard's equivalent event — a known dummy value in a decrypted flow whose host matches
   no inject rule — is currently passed through untouched. That event is near-certainly an
   agent being steered (prompt injection, or exfiltrating the dummy to probe). The addon
   already knows every dummy value and already decrypts injector hosts; scanning decrypted
   flows for dummies and blocking + logging a red TUI row is cheap and high-signal.
3. **`query_param` inject rules leak the real token into the egress log.** `_log_flow`
   records `flow.request.path[:200]` from the flow *after* `request()` has applied inject
   rules — for a `[[inject]]` rule using `query_param` (header rules are fine; headers
   aren't logged), the minted real token lands in `egress.jsonl`, which the TUI renders and
   rotation keeps on disk. Fix: capture the path before injection, or redact the rule's
   query param at log time. And copy gondolin's honest doc note — anything downstream of
   substitution (hooks, logs) may see real values.
4. **The DNS-tunneling residual is real and undocumented.** The wall allows UDP/53 to
   *local* resolvers only — but those resolvers recurse to the attacker's authoritative NS,
   so low-bandwidth exfil via DNS labels survives `wall + default_deny`. Gondolin documents
   this exact residual for their `trusted` mode; foldyard's docs currently imply the
   port-53 story is closed (the wall denies *direct* public `:53`, which is the different,
   easier half). Minimum: document it in [networking.md](../networking.md)/
   [security.md](../security.md). Available tightening, sketch only: since all legitimate
   egress is proxied by hostname (CONNECT carries the name, in-stack DNS is
   netavark-internal), a host-side filtering resolver — or gondolin-style synthetic answers
   on the wall backend — would close the channel without breaking the loop; needs a spike
   against image pulls and stray tooling before believing it.

None of these weaken the three-layer story; 1–3 are addon-sized changes on the existing
proxy path.

## What gondolin doesn't have (the daylight)

The mirror check — foldyard capabilities with no gondolin counterpart, i.e. why this isn't
a "switch to it" question: stack colocation (nothing runs a compose stack in or beside a
gondolin VM; the guest executes one command at a time), any posture system (no axes/rungs,
no TTL'd emergency modes, no auto-revert, no host-side supervisor — secrets are whatever
static values the embedding app passes at `VM.create`), credential *minting* (foldyard's
short-lived scoped tokens + 401 re-mint vs their substitute-a-given-string), a human
operator surface (no TUI, no live grants; the SDK's user is an agent harness author),
worktrees, IDE integration, a `verify` battery, non-Alpine guests, and HTTP/2+ egress
compatibility. Gondolin also cheerfully requires Node + QEMU on the host and hands the
whole policy surface to the embedding application as code — the right call for their user
(a platform engineer building an agent product), the wrong interface for foldyard's (a
developer who wants `fy up`).

## Could we use it anyway?

- **As a reference implementation, immediately.** It is the best public, working answer to
  "what does content-aware egress mediation with in-flight secret injection look like when
  you own the whole data path," and their security/limitations docs set the honesty bar
  ours should match (explicit non-goals, safe-envelope rules, capability matrix).
- **As a component, plausibly later.** A "run this untrusted one-off in a throwaway
  gondolin VM" verb (suspicious postinstall scripts, worm-sample repro) would complement
  the yard rather than compete with it — the yard isolates the project; gondolin would
  isolate a single blast radius *within* it. Host-side only (QEMU/HVF on the Mac; there's
  no `/dev/kvm` in the box), and it's a Node+QEMU dependency, so this is a someday-verb,
  not a plan.
- **As a competitor, no** — unless they pivot from task-sandbox to dev-environment, which
  their workload doc explicitly disclaims (VMs aligned to agent turns, persistence
  discouraged). Watch the repo; Ronacher ships.

## Sources

- Repo: <https://github.com/earendil-works/gondolin> — reviewed at `29fa74d` (2026-07-06,
  v0.12.0, Apache-2.0); first commit 2026-02-02; ~347/396 commits by Armin Ronacher.
- Docs read in full: `docs/{index,architecture,security,network,sdk-network,secrets,vfs,
  sdk-storage,snapshots,limitations,backends,qemu,ingress,ssh,workloads}.md`.
- Vendored Node VFS context: <https://github.com/nodejs/node/pull/61478>.
- foldyard cross-checks: `src/foldyard/assets/proxy/egress_proxy.py` (gaps 1–3),
  [lima-wall-machine-integration.md](../lima-wall-machine-integration.md) (wall port-band
  guarantees), [ADR-0021](../adrs/0021-per-kernel-git-index-split.md).
