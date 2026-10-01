# OpenShell — NVIDIA's agent-sandbox platform, read against foldyard

[OpenShell](https://github.com/NVIDIA/OpenShell) is "the safe, private runtime for fleets of
autonomous AI agents": a gateway control plane that creates per-agent sandboxes on Docker, Podman,
Kubernetes or a libkrun microVM, with declarative YAML policy over files, processes, network and
credentials. Apache-2.0, Rust, by NVIDIA. First commit 2026-01-29; stable `v0.1.2` (2026-09-28).
Reviewed at `1ad4e428a` (2026-10-01, the day of this review).
[prior-art.md](../prior-art.md) carries the one-row positioning summary. Claims are marked
**[V]** where they were checked in source, and **[I]** where they are inferred or come from docs
only.

## The verdict

- **Not a competitor on the unit, and the first neighbour that is a platform rather than a tool.**
  - OpenShell's unit is an agent sandbox inside a fleet: multi-tenant, with a gateway, OIDC/RBAC,
    Helm charts and SDKs in four languages.
  - There is no compose stack, no posture ladder, no worktrees and no human dev loop.
    "Workspaces" in its docs means tenants, not checkouts.
  - It competes with foldyard for nothing a single developer on a laptop does. What it does do is
    set the industry's reference for **how rigorous agent-side enforcement can be**, with a
    vendor's weight behind it: 128 contributors from NVIDIA, Red Hat, Canonical and Docker, and
    340 commits in September alone.
- **It closes, by construction, the egress gaps foldyard still has open.** The workload has no
  network at all: `--network none` on Docker and Podman, and no NIC in the VM. Every `connect()`
  and DNS query is trapped by seccomp user-notification and brokered to a trusted supervisor. That
  supervisor does all of the following before dialling:
  - classifies the target address;
  - resolves the name once and dials only the validated addresses;
  - answers DNS itself;
  - attributes the connection to a program by executable path, SHA-256 and ancestry.

  So gondolin gaps 1 and 4 (no internal-range check, no answer pinning) are now open in foldyard
  against all three library neighbours. That is no longer a frontier, just debt.
- **It validates ADR-0007 a third time, and diverges where it matters.** Agents hold an opaque
  placeholder, and real values are substituted only at profile-authorised endpoints, with a
  wrong-host tripwire. But the resolved secrets live in the supervisor's memory, which runs
  in a container beside the workload; only their VM driver keeps them off the guest. foldyard's
  "nothing real in the VM" is stricter. They also *removed* their managed inference router in
  0.1.0 in favour of native endpoints plus injected credentials, which is foldyard's
  claude/codex design.
- **The "formal verification" headline is narrower than it reads.**
  - The gateway's approval path asserts every policy fact to Z3 as a constant true or false, then
    diffs finding sets: a solver evaluating a formula whose answer is already fixed
    (`openshell-prover/src/model.rs:134-160`) **[V]**.
  - The genuine symbolic containment check (`containment.rs`) runs only in the standalone CLI. It
    returns `unsupported` for `tls:` fields and credential rewrites, which are exactly the fields
    foldyard's ADR-0022 calls material.
  - The idea underneath is excellent and needs no solver: show a reviewer the **new consequences**
    of a policy change, not its text.
- **Reading it found eight new foldyard gaps** (below). The most consequential is that **every
  program in the box receives every injected credential**. A VS Code extension that sends the
  dummy `x-api-key` to `api.anthropic.com` gets the real key, just as the agent does. Two of the
  older gaps have moved: the `query_param` log leak is **fixed**, and the state-dir key and the
  proxy's containment are **narrowed**.
- **Composability is real, and not worth taking yet.** An OpenShell sandbox for the agent can run
  on foldyard's rootless VM podman, chained into foldyard's host proxy. It would buy what no
  native foldyard change buys cheaply: an agent that **does not hold the engine socket**.
  - It costs the agent its reach into the compose stack (ADR-0002's whole point), and it adds a
    second policy surface and a double TLS interception.
  - It is not safe to chain before foldyard closes gaps 1 and 4, because the chain hands the final
    DNS resolution to foldyard.
  - Details are in the [composability section](#composability-openshell-inside-foldyard).

## What OpenShell is

Four components (`docs/about/architecture.mdx`):

| Component | Role |
| --- | --- |
| Gateway | Control plane. Holds sandbox records, policy revisions and provider credentials (an encrypted DB, Vault or Kubernetes Secrets). Issues per-sandbox JWTs, runs the prover, serves the TUI and the approval queue. |
| Compute driver | Builds the boundary per runtime and reports readiness. It never makes policy decisions. |
| Supervisor | The trusted side. Evaluates policy (OPA/Rego through the embedded `regorus` engine), terminates TLS with a per-sandbox CA, applies L7 rules, injects credentials, answers DNS, dials upstream. It talks to the sandbox over mTLS HTTP/2 on a Unix socket (Docker, Podman) or vsock (VM). |
| Sandbox | A helper inside the workload, with no capabilities. Launches the agent as an owned child under Landlock and seccomp, traps network syscalls, identifies the calling program from `/proc`, and freezes the agent if the supervisor drops. |

How each runtime places the supervisor and fences the workload:

| Runtime | Supervisor | Workload fence | Kernel |
| --- | --- | --- | --- |
| Docker | Own container: host network, read-only rootfs, `cap_drop ALL` | `--network none`, `cap_drop ALL`, `no-new-privileges`, non-root **[V]** `driver-docker/src/lib.rs:5770-5845` | Host's (Docker Desktop's VM on macOS) |
| Podman | Own container: `netns=host`, `userns=host`, rootless | same **[V]** `driver-podman/src/container.rs:1460-1606` | Host's |
| Kubernetes | Own pod | NetworkPolicy (the CNI must enforce it) | Node's |
| VM (experimental) | Native host process | libkrun guest with **no NIC**; vsock only; **no host directory sharing at all** **[V]** `driver-vm/src/runtime.rs:332-333` | Custom libkrunfw |

**Scale**, for calibration: about 540k lines of Rust across 38 crates. The largest:

| Crate | Lines | What it holds |
| --- | --- | --- |
| `openshell-server` | 141k | the gateway |
| `openshell-supervisor-network` | 86k | proxy, OPA, L7, policy DNS, credentials |
| `openshell-cli` | 46k | |
| `openshell-policy` | 14k | `merge.rs` alone is 6.9k |
| `openshell-tui` | 14k | |
| `openshell-ocsf` | 9k | an OCSF event crate |
| `openshell-prover` | 7.6k | |

The rest: SDKs in Python, TypeScript, Go and Rust; 43 workflow files; 14 RFCs; 24k lines of docs.

Calibration in the other direction: foldyard's whole package is about 31k lines. A vendor
platform at this cadence is not a codebase to track. It is a source of specifications and
patterns, and occasionally of a component.

## Enforcement — their model vs ours

### Inside the workload: ordered, probed, fail-closed

The sandbox applies controls in a fixed order (`docs/security/best-practices.mdx`, "Enforcement
Application Order"):

1. privileged bootstrap;
2. a startup seccomp prelude with `no_new_privs`;
3. namespace entry;
4. privilege drop with post-condition checks;
5. core-dump hardening;
6. a **mandatory Landlock baseline** (ABI 3, Linux ≥ 6.2) plus the policy's allowlist;
7. a runtime seccomp denylist.

The denylist covers `ptrace`, `memfd_create`, `bpf`, `io_uring_setup`, the whole mount API,
`userfaultfd`, `perf_event_open`, `CLONE_NEWUSER`, `seccomp(SET_MODE_FILTER)`, and the
`AF_PACKET`/`VSOCK`/`BLUETOOTH` socket families **[V]** `openshell-sandbox/src/sandbox/linux/seccomp.rs:184-292`,
`openshell-isolation-interface/src/linux/child_seccomp.rs:120-170`.

The ordering is a typestate (`attach → Bound → confirm → Ready → start_agent → Running`, RFC 0012).
Its invariant is foldyard-relevant: **no untrusted instruction runs before the controls are
confirmed in force.** Every launch runs an active qualification and refuses on failure **[V]**
`openshell-sandbox/src/main.rs:72-110`. It checks:

- uid ≠ 0, capabilities zero, `no_new_privs`;
- Landlock allow and deny in a child;
- the seccomp-notify round trip;
- TCP allow and deny round trips.

Exec and SSH sessions, including VS Code Remote-SSH, go through the same launcher, so they are
confined too.

### On the wire: address discipline, policy DNS, program identity

- **Internal ranges** (`openshell-core/src/net.rs:62-80, 212-290`) **[V]**:
  - RFC 1918, CGNAT, TEST-NETs, benchmarking and ULA are denied by default.
  - An exact hostname declared in user policy may resolve to RFC 1918, and so may an explicit
    `allowed_ips` CIDR.
  - **Loopback, link-local and unspecified are always denied**, and a policy whose `allowed_ips`
    overlaps them fails to load.
  - Endpoints proposed by the advisor never get the private-address exception.
  - Their list is hand-written and misses 240/4, 224/4 and the NAT64/6to4 embeddings.
    vhrn's IANA-registry boundary is the better *list*; OpenShell's is the better *mode
    semantics*.
- **Resolve once, pin the answer** (`proxy/destination.rs:300-385`) **[V]**. The whole answer is
  rejected if any address is internal, and only the validated set is dialled.
- **Policy DNS** (`policy_dns/mod.rs:107-226`) **[V]**:
  - A name no rule could match gets `REFUSED` without any upstream lookup.
  - An eligible name gets a synthetic `198.18.0.0/15` answer (TTL ≤ 30 s), which the supervisor
    maps to the validated real set.
  - That closes DNS tunnelling and DNS rebinding as classes. foldyard has neither closed, nor
    the tunnelling residual written down.
- **Program identity** (`identity.rs`, `openshell-binary-identity`) **[V]**:
  - Each connection is attributed to the executable that owns the socket, by path, SHA-256 and
    ancestry, read from `/proc` by the trusted side.
  - The digest is pinned trust-on-first-use, and a later mismatch denies immediately.
  - A socket shared by two different identities is denied.
  - Rules may match an ancestor, so a rule for `claude` covers the `bash`, `curl` and `npm` it
    spawns. In practice their split is **process tree against process tree**, not agent against
    its own tools.
- **L7 rules** **[V]**:
  - per endpoint: REST method, path and query; GraphQL operation and fields; JSON-RPC and MCP
    methods; WebSocket text frames;
  - `audit` or `enforce` per endpoint;
  - paths are canonicalised before matching (`l7/path.rs:1-33`).
  - ALPN is pinned to `http/1.1` on both legs (`l7/tls.rs:229,285`), so h2 is never parsed and
    gRPC through an inspected endpoint fails.

### Theirs vs foldyard's

| Property | OpenShell | foldyard today |
| --- | --- | --- |
| Egress without guest cooperation | always: no network in the workload; seccomp-notify broker | only with `[machine] firewall` (opt-in, default `False`, `config.py:1043`) **[V]** |
| Internal-range upstreams | denied by default; loopback/link-local always | **not checked** **[V]**, gondolin gap 1 |
| DNS | answered by the supervisor; ineligible names refused, no upstream lookup | the VM's resolver recurses; mitmproxy resolves at dial time; `_reissue_401` resolves a second time through `requests` (`egress_proxy.py:1383-1396`) **[V]** |
| Attribution | program path + SHA-256 + ancestry | per worktree (listener port); User-Agent in the log |
| Content-level policy | L7 rules per endpoint, audit/enforce, canonical paths, HTTP/1.1 only | allowlist of `host[:port]` on decrypted hosts; full protocol reach |
| Workload privileges | non-root, zero caps, `no_new_privs`, Landlock, seccomp, probed every launch | the box runs as `--user 0` in rootless userns with podman's default caps (`box.py:1303-1308`) **[V]**; `fy claude` / `fy codex` turn the agents' own sandboxes **off** (`box.py:625, 784`) **[V]** |
| Engine socket in the workload | never | always, by design (security.md: "bounded to the VM") |
| Supervisor / proxy containment | Docker: read-only rootfs, no caps, host network; holds resolved secrets | `mitmdump` as the operator on the host; host.env scrubbed from its environment, but the addon reads `HOST_ENV_FILE` itself **[V]** |
| Self-test | per-launch qualification, refuses to start | on-demand `fy verify` with positive controls; gVisor runtime check at box-up |

The previous library entries framed foldyard and vhrn as mirror images: rigorous about *what*
against rigorous about *where*. OpenShell is rigorous about both, *and* about *who* (which program).
It pays for that with a helper inside every workload, an 86k-line network crate, and HTTP/1.1
only. foldyard's answer cannot be "build that". It has to be a choice of which of the three axes
to close natively and which to accept.

## Credentials — the same bet, different custody

**Placeholder and substitution**:

- Agents hold `openshell:resolve:env:KEY` (`openshell-core/src/secrets.rs:13-14`) **[V]**.
- The supervisor substitutes the real value into headers, Basic auth (decoded and re-encoded),
  query, path segments, opt-in REST bodies and opt-in WebSocket text, and re-signs SigV4 for AWS.
- A placeholder headed for a host its profile does not authorise gets 403
  `credential_endpoint_mismatch` plus a detection event (`proxy.rs:168-200`) **[V]**.
- The target is rewritten to `[CREDENTIAL]` *before* OPA and logging, so redaction is
  structural, not per field (`secrets.rs:1325-1372`).

**Minting**: the gateway mints OAuth2 refresh, client-credentials, Google service-account JWTs and
AWS STS, and the supervisor handles SPIFFE token grants. The token endpoint is owned by the
profile and "material cannot override" it.

**Provider profiles** (`providers/*.yaml`, 18 shipped) are data. Each declares:

- the credential's environment names;
- the endpoints it may reach (host, port, path globs);
- the binaries allowed to use it;
- a refresh strategy from a closed enum.

That is ADR-0023's "kinds, not commands" expressed as a file format. It is a strong template for
the *credential-mechanism* half of ADR-0024's declarative `[[axis]]` direction. It is no template
for the *posture* half: OpenShell has no rungs, TTLs, emergency ladder or auto-revert, only
attach and detach. Even their data model did not reach 100%: `google_cloud.rs` and `vertex.rs`
are still code, the same residue as foldyard's `auth0_sim`.

**Custody**: real values sit in supervisor memory, the current generation plus 8 retained
(`provider_credentials.rs:16-30`) **[V]**. On Docker, Podman and Kubernetes that supervisor runs
beside the workload, inside the same host or VM. foldyard's guest never holds a real value
(ADR-0007/0008). That is strictly stronger, and it costs a host-side proxy, a minter per mechanism
and a supervisor.

**Where foldyard is ahead:**

- Subscription auth for agents: OpenShell's Claude Code and Codex profiles are API-key only, and
  their docs say subscription tokens are unsupported. foldyard has the Claude OAuth and Codex
  ChatGPT refresh minters.
- Held-credential 401s written in the *provider's own* error shape, so the agent's client prints
  the fix. OpenShell has no equivalent.

## The policy lifecycle — consequences, not text

**Ownership.** The gateway owns policy, in this order of precedence (`policies/overview.mdx:55-64`):

1. a global policy;
2. the sandbox's saved revision;
3. a policy shipped in the image at `/etc/openshell/policy.yaml`;
4. a restrictive default.

Only the network sections are hot-reloadable. A network change closes every connection opened
under the old rules.

**The agent-driven loop** (`policy_local.rs`, `grpc/policy.rs`) **[V]**:

1. A denied request gets a structured JSON 403 that points the agent at `http://policy.local`.
2. The agent reads its denials and the current policy, then posts a proposed merge with an
   `intent_summary`.
3. Separately, a mechanistic mapper drafts a one-binary, one-host rule from **every** blocked
   connection.
4. Each proposal stores:
   - the current and candidate effective policies and their hashes;
   - the prover's finding delta;
   - heuristic security notes;
   - a `review_token`.

   Approval must quote the token, and fails with "refetch and review again" if anything changed
   since review.

Agent proposals may not request `protocol: tcp` or `tls: skip`. `proposal_approval_mode=auto`
approves a proposal whose finding delta and heuristic flags are both empty.

**The delta is the idea worth having.** Findings are tuples
`(binary, host:port, category, method)` in four categories:

- `credential_reach_expansion`;
- `capability_expansion`;
- `l7_bypass_credentialed`;
- `link_local_reach`.

The delta is a set difference between baseline and candidate (`policy.rs:1026-1083`). Two caveats
**[V]**:

- A new *read-only* credentialed reach produces no finding (`model.rs:364-372`), although the
  docs say it does.
- Approvals record no approver: the chunk proto has no approver field, and the OCSF approval event
  carries only the sandbox.

**Theirs vs foldyard's:**

| Property | OpenShell | foldyard today |
| --- | --- | --- |
| Policy authority | gateway DB; image fallback | host: adopted snapshot + allow-store (ADR-0022) |
| Workload's influence on its own policy | proposals, inert until approved; optional auto-approve | repo edits inert until adopted; `[proxy] recommend` asks at adoption |
| Review artefact | consequence delta + hashes | a line diff of the toml (`configpin.py:241-401`); `fy config widenings` describes the *adopted* config, not the candidate **[V]** |
| Stale-review guard | `review_token` | `adopt(reviewed=digest)` → `ReviewStale`: **converged independently** |
| Grant provenance | source, rationale, denial ids; no approver | `{level, expires, added, scope}` (`allowlist.py:769`) **[V]** |
| History | revisions with Loaded/Failed status | current + one previous generation |
| Expiry | **none**; approvals are permanent until removed | `once`/`session`, rung TTLs with auto-revert and settle cascade |
| Applying a change | closes every old-rule connection | narrowing closes only what it no longer allows (ADR-0030) |

## Gaps in foldyard this review found

**Re-verification of earlier gaps** (gondolin 1–3, vhrn 4–6), against `egress_proxy.py`,
`config.py` and `proxy.py` at this review:

| Earlier gap | Status | Detail |
| --- | --- | --- |
| 1. No internal-range check | **Still open** **[V]** | `http_connect` / `_on_request` match hostnames only |
| 2. No dummy-to-wrong-host tripwire | **Still open** **[V]** | New gap 4 below explains why it can't be built yet |
| 3. `query_param` token in `egress.jsonl` | **Fixed** **[V]** | `_logged_path` → `_redact_param` (`egress_proxy.py:315-331, 939-947`). Residual **[I]**: `error_body` logs up to 1 KiB of an upstream error, which could echo a URL |
| 4. No DNS-answer pinning | **Still open, and wider than recorded** **[V]** | `_reissue_401` dials through `requests`, a second independent resolution |
| 5. Proxy unsandboxed | **Narrowed** **[V]** | host.env values are scrubbed from the proxy's environment, but the addon reads `HOST_ENV_FILE` itself and runs as the operator with full host networking |
| 6. State-dir key from mount-writable config | **Narrowed** **[V]** | on the host, `project()` now reads the *adopted* snapshot (`config.py:462-475`), so the live-tree channel is closed. But adopting a `[project].name` change silently moves `state_dir()`, and with it the allow-store, host.env and the lock; no rename-specific refusal was found |

New in this review, ordered by how much they matter:

1. **Every program in the box receives every injected credential** **[V]**.
   - The box's occupants are the coding agent and its tool calls, the VS Code server with its
     extensions, and package managers. All run as uid 0 in one container.
   - `_Rule.matches` keys on host and path only (`egress_proxy.py:535-539`). A VS Code extension,
     or an `npm` postinstall, that sends the dummy `x-api-key` to `api.anthropic.com` gets the
     real key.
   - This is security.md's "one overlap window", widened: a switch that is on reaches *every*
     occupant, not only the one it was switched on for.
   - **Fix shape, accident-grade:** a per-occupant label in the proxy userinfo, reusing the
     build-marker machinery (`_BUILD_TUNNEL_USER`, `_trusted_build`). The agent launcher gets one
     labelled proxy URL. The VS Code server gets another through the `http.proxy` key in the
     Machine settings foldyard already authors (ADR-0026). Inject rules and grants are then keyed
     on the label.
   - Honest limit: children inherit the label, so this separates the agent's tree from the IDE's
     tree, not the agent from its own `npm`. Same uid means `/proc/<pid>/environ` is readable
     across occupants.
   - **Fix shape, enforcement-grade:** the VS Code server in its own container (or a distinct
     uid), each occupant on its own listener port (the per-worktree listener pattern), with the
     VM firewall pinning which source may reach which port.
2. **The adopt gate shows text, not consequences** **[V]**.
   - An edit to `host =` inside the third `[[inject]]` block renders as a bare line change with
     an `@@ [[inject]] @@` locator and no switch name.
   - Adding `@bundle` to `passthrough` shows the token, not the hosts it resolves to.
   - The gate then points at `fy config widenings`, which reports what is *already* adopted.
   - **Fix shape:** run `exposure.collect` over both the pinned and the candidate config. Diff
     structured tuples:
     - `(switch, host+path_prefix)` for injection targets;
     - the resolved passthrough host set;
     - agent prompts and config keys;
     - `compose_env`.

     Print consequences ("credential for switch X now reaches `evil.example`", "+14 hosts now
     tunnelled undecrypted"). Require an explicit `a` when the delta is non-empty. About 150
     lines of Python, no solver.
3. **`path_prefix` matching is non-canonical** **[V]**.
   - `path.startswith(prefix)` on the raw path, query included (`egress_proxy.py:539`, and the
     `held` check).
   - A prefix of `/mcp` matches `/mcp-admin`, `/mcp/../app` and `/mcp%2F..%2Fx`. The credential is
     then injected on a path the upstream routes outside the intended scope. It stays on the same
     host: a scope leak, not a host leak.
   - **Fix shape:** canonicalise the way `l7/path.rs` does (decode unreserved bytes, resolve dot
     segments, refuse `%2F` and `;params`), then match on a segment boundary against the path
     without its query.
4. **The dummies are too generic for a tripwire** **[V]**.
   - `GH_TOKEN=x` and `sk-ant-dummy` (`keyless.py:53`) are literals that anything might send.
     Scanning for them would false-positive, so gondolin gap 2 can't be built on them.
   - **Fix shape:** a per-project random dummy that keeps the provider's prefix
     (`sk-ant-api03-fyd-<random>`), stored host-side. A decrypted flow carrying it to a host with
     no matching rule gets 403 and a red TUI row: OpenShell's `credential_endpoint_mismatch`.
5. **Refusals are not machine-readable, and one is mislabelled** **[V]**.
   - `_REFUSED_BODY` is plain text with a literal `<host>` (`egress_proxy.py:190`).
   - A host-mismatch refusal returns its own body, but logs through the same `_log_blocked` as an
     allowlist refusal (`:1081`). So the TUI offers an "allow" action that cannot fix it.
   - **Fix shape:**
     - a `reason` on every refusal row (`allowlist`, `port`, `host_mismatch`, `held:<axis>`);
     - the allow action offered only for the first two;
     - a JSON body with the fix command.
   - Caveat **[I]**: most clients show only the CONNECT status line, so the fix may also belong
     in the reason phrase.
6. **Third-party plugins that fail to load disappear silently** **[V]**.
   - `_entry_point_plugins` swallows every load exception (`plugins/__init__.py:850-856`).
   - A plugin that supplies a `verify` check or a wall therefore fails open, with no doctor row and
     no list of loaded plugins.
   - **Fix shape:** record load failures and show them in `fy doctor`, add `Plugin.api_version`,
     and fail closed when the adopted config names a switch only a failed plugin provides.
7. **The agent is launched with its own sandbox off and nothing in its place** **[V]**.
   - `fy claude` passes `--dangerously-skip-permissions` (`box.py:625`), and `fy codex` passes
     `--dangerously-bypass-approvals-and-sandbox` (`box.py:784`).
   - The threat model accepts this deliberately: the agent holds the engine socket and is
     "bounded to the VM" (security.md).
   - The gap is narrower than it looks. Landlock or seccomp at the agent's `execvpe` would cut
     kernel exploit surface (`io_uring`, `userfaultfd`, `bpf`, user namespaces) and keep the agent
     out of `~/.vscode-server`. But **any in-box confinement is cosmetic while the agent holds
     the socket**: it can start an unconfined sibling container. Whether Landlock even mediates
     `connect()` on the pathname socket is unprobed **[I]**.
   - The one structural fix is the socket-free agent, below.
8. **The CI and release tiers don't hold their own skips or artefacts to account** **[V]**.
   - Nothing asserts the WSL2 job's six expected skips, so a new skip passes silently.
   - The release workflow publishes without a post-publish install check. `docs/releasing.md`
     still asks for a manual `unzip -l` of the wheel.
   - The podman machine backend has never run live in CI.
   - **Fix shape**, from RFC 0014 at foldyard's size:
     - an expected-skips ledger that fails on any undeclared skip;
     - a canary that does `uv tool install foldyard==X` from PyPI and runs every `fy docs <topic>`
       the skills cite;
     - and either a backend conformance module or retiring the podman backend (the ADR-0027
       precedent).

## What to steal, in order

1. **Address discipline, now three reviews overdue** (gaps 1 + 4):
   - OpenShell's *mode semantics*: public-only by default; an exact declared host may reach
     RFC 1918; loopback and link-local never, whatever is declared.
   - vhrn's IANA-registry *range list*.
   - Resolve once and dial the validated set, **including inside `_reissue_401`**.
   - Policy DNS is the full version. It is worth a spike only if the VM firewall becomes default,
     because without the wall the box can use any resolver it likes.
2. **The consequence diff at the adopt gate** (gap 2). The delta-of-findings idea without the
   solver. Highest leverage per line.
3. **Per-occupant attribution** (gap 1), accident-grade first: labelled proxy URLs, with inject
   rules keyed on the label (the Anthropic credential for the agent's tree only, the marketplace
   hosts for the IDE's tree only).
4. **Unique dummies and the credential-endpoint tripwire** (gap 4, closing gondolin gap 2).
5. **Canonical path matching** (gap 3).
6. **Structured refusals with reason codes** (gap 5), and the read-only guidance file that the 403
   points to. OpenShell injects it at `/etc/openshell/skills/` mode 0444, which is vhrn's "inject
   through a layer the agent only reads".
7. **Grant provenance and a decision log**, now confirmed by a third neighbour:
   - `source` (cli/tui/learn/recommend/build), `why`, `worktree`;
   - an append-only log of grants, revokes, declines and adoptions, recording *who* answered,
     which OpenShell doesn't.
8. **Per-launch qualification**: no agent before its controls are confirmed. foldyard does this
   only for gVisor today. A fast in-box check at `fy box up` and before `fy claude` (wall report,
   caps, `no_new_privs`) is the cheap version.
9. **The engineering items** (gaps 6 and 8): plugin load failures in `fy doctor`; the
   expected-skips ledger; the post-publish canary.
10. **Provider-profile shape** as the template for the credential-mechanism half of ADR-0024:
    profile-owned token endpoint, closed refresh-strategy enum, endpoint binding with path globs.

Deliberately **not** stealing:

| Not taking | Why |
| --- | --- |
| Supervisor-held real credentials in the VM | ADR-0007/0008 |
| Auto-approval of agent or mechanistic proposals | the box widening its own egress; ADR-0022 and the "grants live in the host store" rule |
| An image-carried policy | the box image is consumer-supplied (ADR-0014), so it would be a repo channel into host authority (ADR-0022) |
| A seccomp-notify broker inside the box | owning a helper in every workload; ADR-0011 bets on stock components |
| An SMT solver | foldyard's pattern language is decidable by set and glob comparison; stdlib-only hot path |
| Closing every connection on a policy change | ADR-0030 |
| Pinning h2 off | |
| `--network none` on the box | the in-box stack and compose network are ADR-0014's contract |
| A copy-in workspace | the operator runs git on the host against the live checkout |
| Gateway multi-tenancy (OIDC, RBAC, per-sandbox JWTs) | a single operator; ADR-0006 |
| Full OCSF | wrong user, 9k lines |
| Telemetry | on by default; local-first forbids it |

Also not taking their skills' length: their public CLI skill runs to 955 lines despite their own
"`--help` is authoritative" rule. foldyard's mechanical `test_agent_guide.py` is the better
anti-rot.

## Composability: OpenShell inside foldyard

The question this entry introduced to the template: could the agent run in an OpenShell sandbox
*inside* foldyard's VM, while the stack and the IDE backend stay foldyard's?

**Topologies.**

- **(a) Gateway and Podman driver inside the box**, using `CONTAINER_HOST`. This works
  mechanically, but the policy store then lives where VS Code extensions and the agent can
  rewrite it, which breaks ADR-0022 in spirit.
- **(b) Gateway on the host, Podman driver at the VM's forwarded rootless socket.** This is the
  one that holds.
  - The driver honours `socket_path` and `CONTAINER_HOST` (`socket_discovery.rs:50-53`) **[V]**.
  - Policy and approvals stay host-side.
  - The supervisor runs `netns=host` inside the VM, so its dial back to the gateway must fit the
    fy-wall's project port band (`machine-wall.sh:108`). That needs a `ports.py` reservation **[I]**.
- **(c) Their libkrun VM nested inside Lima.** Needs `/dev/kvm` in the guest (none on Apple M1/M2),
  pays the nested-virtualisation tax, and duplicates the boundary. Not recommended.

**What they need from foldyard's substrate.**

| Requirement | Met by foldyard's VM? |
| --- | --- |
| Rootless Podman 5 on cgroups v2 | **Yes**: the guest runs podman 5.8 **[V]** |
| No `NET_ADMIN` / `SYS_ADMIN` (isolation is `--network none` plus seccomp-notify) | **Yes** **[V]** |
| `CHOWN`/`SETUID`/`SETGID`/`SETPCAP`, briefly | **Yes** **[V]** |
| A namespaced `ip_unprivileged_port_start=0` sysctl for the 127.0.0.53 DNS relay | **Yes** **[V]** |
| Landlock ABI ≥ 3 | Fedora 44's kernel should qualify **[I]**: kernel version and LSM list not checked |
| A seccomp profile permitting user-notification | **[I]**: their qualification fails closed if not |
| **The gVisor posture** | **Probably not.** Creates through the box socket land on runsc, and the socket filter only strips runtime fields, so OpenShell's containers would run under gVisor **[V]**. gVisor is believed to implement neither Landlock nor seccomp user-notification **[I, unverified]**. If so, qualification refuses and nothing starts: fail-closed, not fail-open. gVisor and OpenShell are alternatives, not a stack. |

**Which layer owns what.**

- **Egress:**
  - The supervisor can chain to an upstream proxy (`upstream_proxy.rs`, configured per driver as
    `https_proxy`, `proxy_ca_bundle`, `proxy_connect_by_hostname`) **[V]**.
  - The chain needs **`proxy_connect_by_hostname = true`**. By default it CONNECTs to the
    validated IP, which foldyard's hostname allowlist refuses and its injection never matches.
  - Plain HTTP is never chained **[V]**: under the VM firewall it fails closed (breaking HTTP
    apt mirrors); without the firewall it bypasses foldyard entirely.
- **Credentials:** OpenShell must attach **no providers**, and foldyard injects (option A).
  - This works because foldyard's dummies carry no OpenShell placeholder marker, so they pass
    through untouched (`secrets.rs:52-58, 1333-1340`) **[V]**.
  - Option B, OpenShell injecting, puts real values in a supervisor inside the VM: ADR-0007 says
    no.
- **Policy and grants:** pin `proposal_approval_mode=manual` at gateway scope (gateway scope wins,
  `policy.rs:1365-1385`) **[V]**. Every new host then needs *two* grants, and OpenShell's refusals
  never reach foldyard's log, so the TUI and `fy allow learn` go blind to them **[I]**.

**What it would break.**

- **The agent's reach into the stack.** The rootless supervisor's host network cannot reach
  compose-network container IPs, only published ports **[I]**. So the agent loses `db:5432` and
  in-box `fy up`. Stack colocation (ADR-0002) exists precisely so the agent works against the
  stack.
- **Per-worktree attribution.** The upstream proxy setting is per gateway driver, so keeping it
  means one gateway per worktree.
- **h2**, on any endpoint OpenShell inspects (ALPN pinned to HTTP/1.1).
- **A new hole.** Anything holding the box socket can `podman exec` into the OpenShell workload and
  bypass its launcher **[I]**. The agent must live *only* in the OpenShell sandbox.
- **ADR-0014 bends.** OpenShell side-loads its static binary into any image, but refuses a root
  `USER` unless the policy sets `run_as_user`, and foldyard's box runs as root.
- **A live repo mount** needs `enable_bind_mounts` with admission off, which their own docs warn
  "can bypass workspace isolation".
- **`fy verify`** needs a row run through `openshell sandbox exec`.

**What the composition buys** that neither has alone:

- **An agent without the engine socket**: the structural fix for gap 7, and the one thing no cheap
  native change delivers.
- Program-scoped egress for the agent's tree.
- L7 method and path rules: for example, deny `git-receive-pack` to `github.com`. The operator
  pushes from the host, so the box never needs it.
- Policy DNS and a Landlock allowlist.
- All of this on top of foldyard's VM boundary, host-side injection and posture ladder, which
  OpenShell lacks.

**The cheapest spikes that would settle it.**

1. **Substrate (an hour):** in a walled Lima VM, run
   `podman run --rm --network none --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 --sysctl net.ipv4.ip_unprivileged_port_start=0 --entrypoint /openshell-sandbox <sandbox image> capability-probe`.
   It prints their full qualification report. Repeat through the runsc socket to answer the
   gVisor question.
2. **The chain (half a day):**
   - Set up a gateway on the host with the Podman driver at the example consumer's VM socket,
     `https_proxy` pointed at foldyard's proxy, `proxy_connect_by_hostname = true` and
     `proxy_ca_bundle` set to foldyard's CA.
   - Create one sandbox allowing `/usr/bin/curl` to `api.github.com`, and switch `github=app` on.
   - Expected:
     - `curl -H "Authorization: Bearer x" https://api.github.com/installation/repositories`
       returns 200, and foldyard's log shows `injected: true`;
     - a `python3` request is denied by OpenShell and absent from foldyard's log;
     - an IP CONNECT is refused by foldyard;
     - a `fy mode` switch mid-download keeps the tunnel.

**Verdict: feasible with caveats, not now.**

- The order matters: foldyard closes gaps 1 and 4 first, because the chain makes foldyard's proxy
  the last resolver.
- Then the accident-grade attribution (gap 1's first fix) gets most of the "agent tree vs IDE
  tree" value for a fraction of the cost.
- The composition becomes worth its price only if the threat model changes, so that the agent
  holding the engine socket stops being acceptable.
- At that point the honest comparison is OpenShell against foldyard's own design for a separate
  agent container with no socket, whose shape is the one OpenShell already uses (workload
  `--network none`, a sidecar holding the network and the policy, an authenticated Unix socket on
  a shared volume).

**The other direction** is not meaningful. OpenShell's VM driver shares no host directories, its
container drivers never mount the engine socket into a workload, and its unit has no place for a
compose stack. foldyard as an OpenShell compute driver would discard everything foldyard is for.

**The short reprise:**

- **As a reference, immediately:**
  - the security best-practices page (every control with its default, the risk of relaxing it,
    and a recommendation; the format foldyard's security.md should borrow);
  - the address-mode semantics;
  - the delta-of-findings review.
- **As a component, later and conditionally**, as above.
- **As a competitor, not on the unit.** The realistic risk is the one vhrn posed, at vendor scale:
  OpenShell defines what "a secure agent runtime" is expected to do (no network in the workload,
  per-program egress, L7 rules, structured denials). A reader comparing foldyard to it will look
  for those answers first.

## What OpenShell doesn't have (the daylight)

**What it lacks:**

- **The dev environment:** no compose stack beside the agent, no worktrees, no IDE integration
  beyond reaching a sandbox over SSH.
- **A posture system:** no axes or rungs, no TTLs, no emergency ladder, no auto-revert. Approved
  policy is permanent until removed.
- **Subscription auth for agents:** none.
- **A host-only custody guarantee** on its container drivers: resolved secrets live beside the
  workload.
- **Property-based testing or fuzzing**, and no hermetic subprocess guard.

**Deliberate choices that go the other way:**

- **No VM by default:** containers share the host kernel on Linux, and the libkrun driver is
  experimental and opt-in. foldyard's always-a-VM (ADR-0001, ADR-0027) is a choice OpenShell
  leaves to the operator.
- **No live mount of the checkout:** the workspace is an image, an upload (filtered by
  `.gitignore`, so gitignored implies not-for-the-agent) or a labelled volume.

**A sign of its pace:**

- The security best-practices page still describes a veth network namespace at `10.200.0.1`,
  while the architecture page describes `--network none` with seccomp-notify mediation. Both paths
  exist in code (RFC 0005's `current-shape.md`), but the docs disagree about which is the
  boundary.
- 0.1.0 was a no-upgrade-in-place release that recreated every sandbox.

That is fine for a fleet platform. It would not be fine for a laptop tool whose user has one
project and wants `fy up` to keep working.

**And the framing difference** that explains the rest: OpenShell's user is a platform team running
many agents for many people; its reader is a policy author. foldyard's user is one developer with
one project; its reader is the operator who also writes the code. OpenShell answers "how do we let
a thousand agents act safely"; foldyard answers "how do I develop this project with an agent
without handing it my machine".

## Sources

**Repo:** <https://github.com/NVIDIA/OpenShell>, reviewed at `1ad4e428a` (2026-10-01).
Apache-2.0. 1,593 commits from 2026-01-29 by 128 authors; stable `v0.1.2`.

**Docs read in full:**

- `README.md`
- `docs/about/{overview,architecture,support-matrix}.mdx`
- `docs/security/best-practices.mdx`
- `docs/upgrade/0-1-0.mdx`
- `docs/how-it-works/policies/{overview,schema,manage-policies,default-policy,advisor,prover,network-rules}.mdx`
- `docs/how-it-works/providers/{overview,profiles}.mdx`
- `docs/how-it-works/inference.mdx`
- `docs/how-it-works/sandboxes/{overview,runtimes,templates}.mdx`
- `docs/how-it-works/workspaces.mdx`
- `docs/extensibility/{overview,isolation-backends,drivers,gateway-interceptors}.mdx`
- `docs/observability/{logging,ocsf-json-export,telemetry}.mdx`
- `docs/sdk/api-errors.mdx`
- `docs/tutorials/{first-network-policy,github-push-access}.mdx`
- `rfc/0002`, `rfc/0005` (all four files), `rfc/0012`, `rfc/0014`
- `AGENTS.md`, `TESTING.md`, `CI.md`

**Docs skimmed:** `rfc/0001`, `0004`, `0009`, `0010`, `0011`, `0013`.

**Code read, by area:**

- **Network and credentials:**
  - `openshell-core/src/{net,secrets,provider_credentials}.rs`
  - `openshell-supervisor-network/src/{proxy.rs,proxy/destination.rs,upstream_proxy.rs,policy_dns/,identity.rs,procfs.rs,token_grant.rs,l7/{path,tls,rest}.rs,data/sandbox-policy.rego}`
  - `openshell-binary-identity`
- **Isolation:**
  - `openshell-sandbox/src/{main,process,network_broker}.rs`
  - `openshell-sandbox/src/sandbox/linux/{landlock,seccomp}.rs`
  - `openshell-isolation-interface/src/linux/{child_seccomp,seccomp_notify,workload_launcher}.rs`
- **Drivers:**
  - `openshell-driver-docker/src/lib.rs` (container specs)
  - `openshell-driver-podman/src/{container,config,socket_discovery}.rs`
  - `openshell-driver-vm/src/{runtime,procguard}.rs`
- **Policy and prover:**
  - `openshell-prover/src/{lib,model,finding,registry,containment}.rs`
  - `openshell-prover-cli/src/main.rs`
  - `openshell-server/src/grpc/policy.rs` (proposal, approval, delta, auto-approve)
  - `openshell-supervisor-network/src/policy_local.rs`
- **Practice:**
  - `openshell-conformance/src`
  - `openshell-supervisor-process/src/skills.rs`
  - `e2e/rust/tests/bypass_detection.rs`
  - `.github/workflows/release-canary.yml`
- `providers/{github,anthropic,claude-code,google-cloud}.yaml`

**foldyard cross-checks:**

- `src/foldyard/assets/proxy/egress_proxy.py` (in full; gaps 1, 3–5 and the re-verification
  table)
- `src/foldyard/box.py` (agent launch, box create; gap 7)
- `src/foldyard/config.py` (`project()`, `machine_firewall`, `_toml_ambient`)
- `src/foldyard/configpin.py` and `src/foldyard/exposure.py` (gap 2)
- `src/foldyard/allowlist.py`
- `src/foldyard/plugins/__init__.py` (gap 6)
- `src/foldyard/plugins/proxy.py`, `src/foldyard/supervisor.py` (host.env scrubbing)
- `src/foldyard/keyless.py`, `src/foldyard/plugins/github.py` (dummies)
- `src/foldyard/vscode.py`
- `src/foldyard/machine_backend.py`
- `src/foldyard/assets/machine-wall/machine-wall.sh`
- `src/foldyard/assets/sandbox/socket_filter.py`
- `tests/e2e_host.py`
- `.github/workflows/release.yml`
- [security.md](../security.md), [isolation-layers.md](../isolation-layers.md)
- ADRs 0002, 0006, 0007, 0008, 0011, 0014, 0022, 0023, 0024, 0026, 0027, 0030
