# vhrn — a jailed agent harness with a specified egress proxy, read against foldyard

[vhrn](https://github.com/aravind-n/vhrn) ("Virtualized Harness Runtime") runs a coding agent
inside a container jailed to the current project directory, with default-deny egress through a
host-owned allowlist. MIT, Rust, single author (Aravind Nidadavolu), first commit 2026-07-17,
v0.5.1 — reviewed at `0e3926d` (2026-09-21, the day of this review; the project is under daily
development). [prior-art.md](../prior-art.md) carries the one-row positioning summary.

## The verdict

- **Not a competitor on the unit, but the closest neighbour yet on the *operator surface*.**
  vhrn jails one agent run in one project directory. There is no stack, no posture ladder, no
  worktrees, no VM by default. But unlike the per-task sandbox wave, its user is a *developer on
  their own machine with a daily loop* — the same person foldyard serves — and it has grown the
  same operator verbs foldyard has: scoped persistent egress grants (`vhrn net allow --project .`),
  a live status view with provenance, a denial log. Gondolin was the closest *mechanism*
  neighbour; vhrn is the closest *ergonomics* neighbour.
- **It independently reinvents the ADR-0022 rule, and states it more sharply than we do.**
  "Config is host-owned only — vhrn reads nothing from the project directory, so a cloned repo can
  never reconfigure the jail." They went further than foldyard's adopt-gate: they *deleted* the
  `[net]` config table outright and moved every persistent domain into a host state store reached
  only through `vhrn net allow`, with a targeted migration error for stale config. That is
  foldyard's "egress grants live in the host store at EVERY level" don't-break rule, reached
  independently, and enforced by removing the channel rather than gating it.
- **Its proxy contract is the single most valuable artifact in the repo.**
  `docs/proxy/consumer-contract.md` is a 570-line normative specification of the forward proxy —
  RFC 9110/9112/3986/1035 references, MUST/SHOULD language, exact error bodies, a fail-closed
  policy-read rule, and a globally-reachable-unicast boundary defined against dated IANA
  special-purpose registry snapshots. foldyard's proxy has no written contract at all. This is the
  highest-leverage thing to steal, and it is free — it is a document, not code.
- **Reading it re-confirmed the three open gondolin gaps and found three more** (below): the
  proxy is foldyard's most privileged component and is itself unsandboxed; the `~/.foldyard/`
  state-dir key — and therefore the allow-store's location — is derived from a value the mount can
  write; and there is no DNS-answer pinning, so an allowlisted hostname can be rebound.
- **Its credential bet is the opposite of ours, deliberately and coherently.** The proxy never
  terminates TLS, so injection is impossible by construction; credentials therefore live *in* the
  container, in a persistent per-harness store the agent logs into itself. They argue this is
  better than sharing the host's token, which is true, and it is strictly weaker than never having
  the credential in reach, which is also true. Worth understanding as the honest alternative to
  ADR-0007 rather than as a mistake.

## What vhrn is

A Rust CLI (`vhrn <harness>`) that, per run, starts two containers on Apple `container` (preferred)
or Docker: the **agent container** (Debian base + one agent CLI, project bind-mounted at its real
host path, nothing else from `$HOME`) and a **proxy sidecar**. The container's own nftables ruleset
drops all egress except TCP to the sidecar; the sidecar is the only network peer and owns DNS.
Harnesses are `claude`, `codex`, and `pi`, each a spec in `cli/src/harness.rs` plus a thin
`FROM vhrn-base` Dockerfile, pinned and rolled back by agent version (`vhrn install claude@2.1.30`).

Scale, for calibration: ~15.3k lines of Rust in the proxy crate, ~11.9k in the CLI, a 2.2k-line
black-box process test suite that drives the built executable through only its env and TCP
interface, and ~5.6k lines of docs. The proxy was migrated from an earlier implementation through a
13-phase plan (`docs/plans/completed/rust-proxy-migration/`) in which the contract was *frozen
first* and a coverage ledger mapped every normative clause to a named test. Smaller than gondolin's
~40k, and far more narrowly scoped — it owns a forward proxy, not a network stack.

The container is ephemeral (`--rm`); persistence is a property of what is mounted. Each harness gets
`~/.cache/vhrn/state/<harness>/` mounted as its config dir, which is authoritative once populated —
host seed files are copied only while the destination is absent, so an in-container login is never
overwritten.

## The egress architecture

### Their model

Three enforcement points, none of which requires the guest to cooperate:

1. **In-container nftables, installed before the privilege drop.** The entrypoint runs as root,
   installs `table inet vhrn` with `policy drop` (permitting only `lo`, established/related, and
   TCP to the proxy IP:port), then `exec setpriv --reuid dev` — and `dev` has no sudo and no
   capabilities. The rules are enforced from outside the agent's reach *while sharing the
   container*. Direct DNS is dropped with everything else.
2. **The proxy sidecar**, a statically linked Rust binary in a `scratch` image, running as
   `65532:65532`, read-only root filesystem, every capability dropped, no shell. It receives
   exactly three mounts — read-only policy, a writable denial log, and a read-only broker token —
   and the contract states it "MUST NOT require the project, agent configuration, credentials,
   container-engine socket, or any other host path". The agent container never receives any of them.
3. **A host-side broker** for the one thing the proxy cannot do from inside a container: reach a
   host-loopback model server. The proxy holds a per-run token the agent never sees, asks the
   broker to dial an exact `host:port`, and the broker *rechecks policy before dialing*, capped at
   128 connections per run.

Policy is five additive layers evaluated in order — immutable base, selected harness, persistent
global, exact canonical project, run-only `--allow` — re-read and revalidated **for every new
request or CONNECT decision**, with an explicit prohibition on retaining an allow decision across
requests. A missing, unreadable, oversized, non-regular or invalid required file fails closed to
empty-`enforce`, and the process stays alive so a host repair can take effect.

The parts with no foldyard counterpart at all:

- **A globally-reachable-unicast boundary**, defined against the IANA IPv4/IPv6 Special-Purpose
  registries (most-specific entry; `Destination`, `Forwardable` and `Globally Reachable` must all
  be `True`), applied in *every* mode including `open`. A public policy entry of `127.0.0.1`
  therefore cannot grant loopback; private, link-local, multicast, documentation and benchmark
  ranges are unreachable regardless of allowlist. The registry snapshot is fixed at build review
  time and the proxy "MUST NOT fetch policy or registries at runtime" — updating it is a reviewed
  security change.
- **DNS-answer pinning.** Resolve the already-authorized host *exactly once*, require 1–64
  addresses, reject the entire answer if any address fails the unicast test, retain the validated
  set, and attempt only those numeric addresses — no second resolution in the connector or HTTP
  client. A CNAME is not separately authorized but all its addresses face the same test. That
  closes DNS rebinding as a class.
- **Capability separation for loopback.** Host-loopback egress is a *different* capability from
  public egress, with its own grammar (`localhost:<port>`, an exact `127.0.0.0/8` address, or
  `[::1]:<port>`, port mandatory), its own grant verb, and its own always-enforced mode. The
  contract says it plainly: "Public allowlists, `report`, and `open` MUST NOT grant or route a
  local destination."

The price they pay: hostname matching only. The proxy does not terminate TLS, so it cannot see
inside a CONNECT tunnel, cannot inject credentials, cannot apply path/method policy, and cannot
stop exfiltration to an allowed domain or domain-fronting behind an allowed CDN. Their threat model
says so in those words.

### Theirs vs foldyard's

| Property | vhrn | foldyard today |
| --- | --- | --- |
| Enforcement without guest cooperation | always (in-container nftables installed pre-privilege-drop; no sudo) | only with `[machine] firewall` (opt-in, Lima only; validated in CI since 2026-09-17) — the box's own netns is unfenced |
| Proxy's own privilege | scratch container, uid 65532, read-only rootfs, no caps, three mounts | `mitmdump` as the operator on the host, full host network, holds every axis's secret — **see gap 5** |
| Internal-range upstreams | denied in every mode, by IANA registry classification | **not checked at all** — gondolin gap 1, still open |
| Loopback access | separate capability, exact authority, brokered, always enforced | not expressible; reachable only as an unchecked side effect of gap 1 |
| DNS rebinding | closed (resolve once, validate, pin the answer set) | open — policy never looks at IPs |
| Policy re-read | every request, decision never cached, fail-closed to empty-enforce | `allow-effective.json` per request; allow-store unreadable ⇒ enforce ✓ (converges) |
| Grant scoping | base / harness / global / **exact project** / run, with provenance in `net status` | `once` / `session` / `permanent` — *lifetime*, not scope; one store per project dir |
| Content-level policy, credential injection | impossible by construction (no TLS termination) | every non-`passthrough` host decrypted (ADR-0029); `[[inject]]` mints and substitutes |
| Written proxy contract | 570-line normative spec + coverage ledger + black-box process suite | none |
| Denial observability | `vhrn net denied` since last idle, append-only log the agent cannot read | `egress.jsonl` + TUI network log |
| Protocol reach | HTTP/1.0–1.1 ingress; CONNECT is an opaque relay, so h2/h3 tunnel fine | full; CONNECT is the policy point under `[proxy] enforce` |

The two systems are near-mirror-images: **vhrn is rigorous about *where* a connection may go and
blind to *what* travels; foldyard is rigorous about *what* travels on decrypted hosts and blind to
where the connection actually lands.** Each one's strength is precisely the other's gap. Nothing in
vhrn's address discipline conflicts with foldyard's injection model — they compose, and gap 1 is
exactly the missing half.

## The policy model — scope beats lifetime

The sharpest single contrast is not a mechanism, it's a data model.

foldyard's allow-store keys a host to a **lifetime**: `once` (120 s TTL), `session` (until the
supervisor restarts), `permanent`. One store lives at `~/.foldyard/<project>/allow-store.json`, and
a grant is a host in that store.

vhrn keys a domain to a **scope**: which of five layers put it there. `vhrn net status --domains`
reports provenance, `vhrn net deny` removes it from one named scope and *reports which other layers
still supply it* rather than writing a negative override, and a denial batch is atomic across the
selected scope. Run-only grants (`--allow`, `--open-net`) are a layer that cannot persist by
construction, not a TTL that must be swept.

Both models answer "is this host allowed?". Only the scoped one answers "**why** is this host
allowed, and what happens if I remove it here?" — which is the question an operator actually asks
when auditing a widened wall six weeks later. foldyard's `exposure.py` inventory exists because
that question matters; the allow-store is the one widening surface that cannot answer it.

They also made the harder call: `--allow` and `--open-net` are run-only *and* `net open|guard|
report` affect active runs only, with idle state reporting "future runs default to enforce". There
is no way for a loosened posture to survive into a run that didn't ask for it. foldyard's TTL +
settle cascade reaches the same place by a different route, and the TTL route buys the emergency
ladder vhrn has no equivalent of — but it is worth noticing that "run-scoped by construction" needs
no clock, no sweep, and no supervisor.

## Credentials — the opposite bet, coherently argued

vhrn cannot inject, so it does the next best thing and is careful about it:

- The agent logs in **inside** the container (`codex login --device-auth`, Pi's `/login`), and that
  login lands in the container-owned persistent store. Device-auth is used because the browser
  callback port isn't reachable from the jail *and* because "it mints the container its own token
  instead of sharing your host one" — a scoping argument, not just a plumbing one. Host Pi
  credentials are never imported.
- Where a host credential must be forwarded (`gh` token as `$GH_TOKEN`, `CODEX_API_KEY` and
  friends), it is forwarded as env and **hidden from commands the agent spawns** by default, with
  the host's `[shell_environment_policy]` winning if set.
- Host config comes in as a **disposable copy**, not a mount, so the container cannot write back to
  the real config. Codex's `config.toml` is mounted a layer *below* at `/etc/codex/config.toml`,
  read-only, with its `[projects.*]` trust entries stripped on the way in: "trusting a folder on
  the host does not trust it in the jail."

Read against ADR-0007/0008, the comparison is clean: foldyard's guest holds a dummy and the real
token exists only on the host for the duration of one substituted request; vhrn's guest holds a
real, long-lived, agent-scoped token. foldyard's posture is strictly stronger *and* costs a TLS
MITM, a minter per mechanism, a supervisor, and the decrypt/passthrough distinction. vhrn's costs a
login prompt. Their honesty about what remains exposed — trusted repo content executing with the
forwarded GitHub token in reach of an allowed domain — is the part to match, not the mechanism.

## Gaps in foldyard this review found

Gaps 1–3 are the gondolin review's, **re-verified as still open** against
`src/foldyard/assets/proxy/egress_proxy.py` at this review:

1. **No internal-range check on proxy upstreams.** Still true: the addon contains no loopback,
   RFC1918, link-local or CGNAT test of any kind (`grep -i 'loopback|127\.0\.0|rfc1918|169\.254'`
   over the addon returns nothing). vhrn's contract is the better fix shape than gondolin's, and it
   is specified in enough detail to implement directly: classify the target *before any network
   operation*, apply the unicast boundary in every mode, and make loopback a separate capability
   with its own grammar rather than an entry on the same allowlist.
2. **No tripwire when a dummy credential heads to the wrong host.** Still open.
3. **`query_param` inject rules still leak the real token into the egress log.** Confirmed
   mechanically: `_log_flow` records `flow.request.path[:200]` and is called after `request()` has
   applied injection, so a `[[inject]]` rule using `query_param` writes the minted token into
   `egress.jsonl`. Header rules are unaffected.

New in this review:

4. **No DNS-answer pinning — an allowlisted hostname can be rebound.** foldyard's policy is a
   hostname match; the actual connect-time address is never examined, and nothing pins the answer
   set between the policy decision and the dial. An allowlisted host that resolves to a
   host-loopback or LAN address walks straight through, and so does one whose second resolution
   differs from its first. **Fix shape:** vhrn's four-step rule — resolve once, validate every
   returned address against the unicast boundary, retain the validated set, attempt only those
   numeric addresses with no second resolution in the connector. This and gap 1 are one change,
   and gap 1 is not really fixed without it.
5. **The proxy is foldyard's most privileged component and the only one that is unsandboxed.**
   `mitmdump` runs on the host as the operator, with the operator's full network reach and an
   environment holding every axis's `host.env` secret (deliberately — that is how injection works;
   the addon scrubs it for *minters*, not for itself). vhrn's equivalent is a no-shell scratch
   container with every capability dropped and three mounts. This is inherent to ADR-0007 and is
   not a bug, but it is unstated: the threat model in [security.md](../security.md) should say
   plainly that compromising the proxy process compromises every credential the posture has
   enabled, and gap 1 should be read in that light — the unchecked upstream dial is performed by
   the one process that holds everything.
6. **The `~/.foldyard/<project>/` key — and so the allow-store's location — is derived from a
   value the mount can write.** `config.project()` resolves `FOLDYARD_PROJECT` → `[project].name`
   from the tree's `foldyard.toml` → the repo dir name, and nothing in the package sets
   `FOLDYARD_PROJECT` host-side. `state_dir()` is built from it, and `allow-store.json` lives
   there. configpin already learned exactly this lesson and keys adopted snapshots by *checkout
   path* — "keyed by the CHECKOUT PATH so nothing the config declares" can move it. The same
   argument applies to the state dir, which holds the allow-store and the posture state. vhrn's
   rule is the clean statement of it: canonicalize the cwd once, match the project key
   byte-for-byte, and forbid `~`, `.` and `..` in keys. **Worth verifying before fixing** — the
   supervisor's `_toml` is `lru_cache`d, so a long-running host process may never re-read an
   edited name, and the practical effect of a rename may be an empty store (fail-safe, enforcing)
   rather than adoption of another project's permanent grants. Either way the channel is open by
   the ADR-0022 argument, and the fix is the one configpin already shipped.

## What to steal, in order

1. **Write the proxy's consumer contract.** A normative document specifying observable behaviour
   and security outcomes of `egress_proxy.py` — accepted request forms, exact refusal responses,
   the policy re-read rule and its fail-closed behaviour, what decryption does and does not see, what
   injection guarantees, and the address boundary once gaps 1/4 land. vhrn's is the template, down
   to the "where this contract is stricter than the protocol permits, this contract wins" line.
   foldyard's proxy is the component where a subtle behavioural drift is most expensive and least
   visible, and it is currently specified only by its own source.
2. **The address discipline (gaps 1 + 4 together)**, implemented to vhrn's specification rather
   than invented: unicast boundary in every mode, resolve-once-and-pin, loopback as a separate
   narrow capability. Their `shared/testdata/loopback-authorities.tsv` fixture is the shape of the
   test table to write.
3. **Provenance in the allow-store.** Record which scope granted a host and show it in
   `fy allow`/the TUI, and make removal report what still supplies it. This is a data-model change
   to a small JSON file and it answers the audit question the current store cannot.
4. **Per-box nftables installed before the privilege drop.** vhrn gets non-cooperative egress
   enforcement on *every* engine and platform from ~10 lines in an entrypoint, because the rules
   are installed by root and the agent user has no sudo. foldyard's equivalent rung today is
   `[machine] firewall` — opt-in and Lima-only (validated in CI). A box-local `table inet` with `policy drop`,
   permitting the compose network (in-stack traffic must stay direct under `NO_PROXY`) and the
   proxy, would close the "cooperative capture is bypassable" hole for the box on backends the VM
   wall doesn't cover. This needs a spike against the compose network and the socket mount before
   believing it, but it is much cheaper than the wall.
5. **The revocation-latency note.** vhrn documents that policy visibility through a shared mount
   has "no guaranteed immediate visibility or bound" and that a pooled plain-HTTP request can pass
   after revocation. foldyard's `once` grants auto-revert on a supervisor sweep and the proxy
   re-reads per request; the honest equivalent sentence belongs in
   [networking.md](../networking.md).
6. **Their harness rule, checked against ours:** *"If an agent writes its own config file, vhrn
   does not write it. Inject through a layer the agent only reads instead."* Worth auditing
   `skills.py` and `transcripts.py` against — foldyard installs bundled skills into a consumer's
   `.claude/skills/` and syncs transcripts, and the question "does the agent also write here, and
   do we destroy a decision it recorded?" has not been asked in those words. Their second
   observation is the same class: Codex keeps all transcripts in one flat tree, so mounting it
   "would hand a jailed agent every transcript on the machine" — a per-project session store was
   the fix.

Deliberately **not** stealing: the container-owned credential store (ADR-0007 is strictly stronger),
hostname-only policy (gives up capture and injection), and the harness-registry/image-per-agent
model (foldyard's box image is consumer-supplied by ADR-0014, and baking agent CLIs into foldyard's
own images would invert that).

## What vhrn doesn't have (the daylight)

The mirror check. No stack colocation of any kind — the unit is one agent in one directory, and
nothing runs a compose stack beside it. No posture system: no axes or rungs, no TTLs, no emergency
ladder, no auto-revert, no host supervisor, no capability probes. No credential minting or
injection, and no way to add them without terminating TLS. No worktrees, no multi-project view, no
TUI, no `verify` battery, no IDE integration, no plugin framework, no consumer-supplied image, no
`fy doctor`/`fy state` reconciler. Its default engine (Apple `container`) is macOS-only and its
Docker path shares the host kernel — the VM boundary foldyard treats as non-negotiable (ADR-0001,
ADR-0027) is an engine choice there, and their threat model lists container escape under Docker as
out of scope. Brokered loopback is verified only on Apple `container` and Docker-through-Colima;
native Linux Docker is explicitly unsupported for that route.

And the framing difference that explains most of the rest: vhrn's project mount is the *current
directory*, chosen at launch, with host config copied in per run. foldyard's project is a declared,
adopted, long-lived thing with its own state dir, posture, stack and worktrees. vhrn is a better
`cd && run the agent safely`; it is not trying to be an environment.

## Could we use it anyway?

- **As a specification source, immediately.** The consumer contract and the loopback-authority
  fixture are directly usable; the contract-frozen-first / coverage-ledger / black-box-process-suite
  method is worth copying for foldyard's own proxy regardless of whether any vhrn behaviour is
  adopted.
- **As a component, no.** It is a whole-run wrapper with its own engine, image set and login model;
  there is no seam to embed. The overlap would be total and the composition incoherent.
- **As a competitor, partially — watch it.** It does not compete for foldyard's project-environment
  user today. It does compete for the *first hour* of that user's attention: a developer who wants
  "run Claude Code safely against this repo" is well served by `vhrn claude`, and will meet
  foldyard only if they later need the stack, the posture ladder, or a second worktree. Single
  author, two months old, shipping daily, with better-specified egress than foldyard has written
  down. The realistic risk is not that it grows a compose stack — it is that it defines what
  developers expect "jailed agent" to mean, on axes (address discipline, grant provenance,
  proxy containment) where foldyard currently has no answer.

## Sources

- Repo: <https://github.com/aravind-n/vhrn> — reviewed at `0e3926d` (2026-09-21), v0.5.1, MIT,
  144 commits from 2026-07-17, all by Aravind Nidadavolu.
- Docs read in full: `README.md`, `docs/sandbox-design.md`, `docs/proxy/consumer-contract.md`,
  `docs/adding-a-harness.md`, `CHANGELOG.md`, and
  `docs/plans/completed/rust-proxy-migration/phase-12-qualification.md`.
- Code read: `image/base/entrypoint.sh` (the pre-privilege-drop firewall), `image/base/Dockerfile`,
  and the `proxy/src/` + `cli/src/` layout for scale.
- foldyard cross-checks: `src/foldyard/assets/proxy/egress_proxy.py` (gaps 1–5),
  `src/foldyard/allowlist.py` and `src/foldyard/config.py` (`project()`, `state_dir()` — gap 6),
  `src/foldyard/configpin.py` (the checkout-path keying that gap 6 should follow),
  [ADR-0007](../adrs/0007-credential-injection-at-egress-proxy.md),
  [ADR-0009](../adrs/0009-monitoring-cooperative-enforcement-locked.md),
  [ADR-0022](../adrs/0022-host-runs-the-adopted-config.md).
