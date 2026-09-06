# ADR-0007 — Credential injection at the egress proxy: the token never enters the box

- **Status:** Accepted (2026-06; multi-injector rule set late 2026-06; addon packaged with
  PR #23, 2026-06-30) — implemented
- **Sources:** docs/history/PLAN.md §4.2, docs/history/agent-editor-plugins-plan.md, docs/history/network-forcing-HANDOVER.md,
  src/foldyard/assets/proxy/egress_proxy.py

## Context

Some postures need the box to reach a real API with a real credential — GitHub as an App,
Anthropic/OpenAI for keyless agents (ADR-0008), any header- or query-authenticated service. The
naive grant puts the token *in* the box (env var, mounted file), where every piece of untrusted
code can read it, log it, or exfiltrate it for its lifetime. An earlier iteration exec-injected
a token *file* into the container — better, but the secret still materialised inside the
boundary.

The box already routes all egress through a host-side mitmproxy (always-on: passthrough when
`capture=off`, MITM-decrypt when `capture=on`), with the proxy CA mounted in. That chokepoint
sits on the Mac, outside the blast radius — exactly where a credential should be attached.

## Decision

**Inject credentials host-side, in flight, at the egress proxy.** The mitmdump addon
(`src/foldyard/assets/proxy/egress_proxy.py` — packaged, and snapshot-staged to `state_dir()`
at each daemon launch so a git checkout can never rewrite the file under a live proxy) rewrites
matching requests as they pass:

- **The box only ever holds a dummy.** The client sends *something* (a dummy env var / config
  value bakes the header into existence); the proxy **overwrites** it with the real value. The
  token never materialises in the box — not as env, not as a file. Injection targets a header
  (default `Authorization`) or a **URL query param** (e.g. Penpot's `?userToken=`), optionally
  scoped by `path_prefix` so one host can serve an app and an injected endpoint.
- **Minters produce the value host-side:** each rule names a command printing
  `{"value": ..., "ttl": ...}` — a GitHub App installation-token minter, the `static_token`
  host.env reader, the Codex ChatGPT refresh-minter. Tokens are short-lived and scoped; the
  addon caches per rule and re-mints just before the reported TTL. A `value_prefix` (e.g.
  `"Bearer "`) is prepended at injection, so host.env keeps the bare token.
- **401s are handled by re-mint + re-issue *inside the hook*, not mitmproxy replay.** On an
  upstream 401 the matching rule force-re-mints, and the addon re-issues the request itself
  (`requests` in a thread) and hands the retried response back as `flow.response` — at most
  once, loop-guarded. Why not `replay.client`: modern mitmproxy refuses to replay a *live* flow
  ("Can't replay live flow"), and a replayed copy is detached from the original client — the
  waiting caller would still see the 401. The in-hook re-issue makes token rotation invisible
  to the box: it sent one request and got a 200.
- **A rule SET, not a single injector** (`INJECT_RULES`, a JSON list the proxy plugin emits):
  each rule carries its own host, minter, token cache, and 401 policy, so github + claude +
  codex + any config-only `[[inject]]` axis can all be `on` at once through one mitmdump. This
  lifted the original single-minter limit that made injectors mutually exclusive. Legacy single
  `INJECT_*` env survives as a one-rule fallback.
- **Injector hosts are always decrypted**, whatever the capture mode — the header can't be
  rewritten through a blind tunnel — and are identified by SNI or, when a client dials a bare
  IP, by the CONNECT target. Everything else follows the capture axis (decrypt+log vs
  SNI-only passthrough). The addon can *cooperatively* denylist non-allowlisted hosts on the
  proxy path, but that only bites traffic that chooses to route through the proxy; the real
  default-deny wall is enforced at the VM layer when `[machine].wall` is enabled (ADR-0009),
  not by the addon itself.

The addon is deliberately **standalone** — it never imports foldyard; its whole contract is env
vars and files — so it runs under mitmdump's interpreter and stays testable in isolation.

## Consequences

- A leaked *request* leaks at most one short-lived, narrowly-scoped token; a compromised box
  can use the credential only for as long as its posture axis is on (ADR-0005) and only against
  the injected host — it can never *hold* it.
- Token rotation and 401 recovery are invisible to in-box clients; long-lived agent sessions
  survive minter refreshes without re-auth.
- New injectors are cheap: `[[inject]]` in `foldyard.toml` is config-only (host + header or
  query_param; the token var is derived from the axis), no Python (see
  `src/foldyard/plugins/inject.py`).
- Costs accepted: the box must trust the proxy CA (mounted read-only at box-up); injection
  rides proxy env vars, which cooperative software honors — enforcement against a hostile
  process is the separate wall work (ADR-0009); and the proxy is on the request hot path, so
  minter failures degrade that one host (logged, never crash the proxy).

## Rejected alternatives

- **Real credentials in box env or mounted files** — readable by all untrusted code for its
  whole lifetime; revocation requires a re-bake. The model the product exists to end.
- **Exec-injecting a token file into the running container** (the historical predecessor) —
  the secret still lands inside the boundary, and rotation means re-injection.
- **mitmproxy client replay for 401s** — rejected on a live flow, and detached from the
  original client (see above); the in-hook re-issue is the only shape that returns the retried
  response to the caller that is still waiting.
- **One-injector-per-proxy** (the shipped first cut) — forced github/claude/codex/penpot to be
  mutually exclusive; replaced by the rule set once more than one injector was real.
