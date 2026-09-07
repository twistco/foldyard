# ADR-0023 — No host-executed code from the repo mount: credential minters are packaged KINDS

- **Status:** Accepted (2026-08-08) — implemented across `plugins/github_app_token.py`,
  `plugins/gh_cli_token.py`, `plugins/__init__.py` (`Secret`/`Registry.secrets`),
  `keyless.ensure_secret`, `worktree._init_in_yard`, `vscode._generate_attached_config` +
  `_ALLOWED_CONFIG_KEYS`, `allowlist.py` (host-owned grants *and* wall switch), `config.engine_cli`.
- **Sources:** the review of the finalised `github=app` PR-bot minter — "does the consumer-side
  minter hand the box a code-execution path to the Mac?"; five review rounds against the
  implementation. Related: [0007](./0007-credential-injection-at-egress-proxy.md) (where minting
  happens), [0012](./0012-uv-tool-distribution-no-mutable-daemon-source.md) (daemons must not run
  mutable working-tree source — this is that principle applied to what the daemons *call*),
  [0019](./0019-consumer-repo-plugins-trust-model.md) (narrowed by this ADR for credential code,
  then superseded outright by [0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md)),
  [0022](./0022-host-runs-the-adopted-config.md) (the same lens applied to the config CHANNEL).

## Context

Credential minting is host-side by design (ADR-0007): the token must be minted where the
long-lived identity lives and injected in flight, so it never enters the box. The supervisor
therefore runs minters as children of `mitmdump`, on the Mac, as the operator, in a process whose
environment holds **every** injectable secret for every axis (`load_host_env` merges the whole of
`host.env` into the supervisor's `os.environ` each tick, and a daemon's env is
`{**os.environ, **spec["env"]}`).

The `github=app` minter named its implementation as a path **inside the repo mount** —
`uv run --script <repo>/dev-stack/gh-app-token`. The command line was fixed by the package; the
code it executed was a working-tree file. Anything that can write the checkout — an in-box agent
running with prompts skipped, an `npm install`/`uv sync` lifecycle script, a branch the operator
checks out to review, all three explicitly in the threat model — therefore had arbitrary code
execution on the host, as the operator, next to the credentials.

Three details made it a reliable path rather than a theoretical one:

1. **It re-ran on a timer, unattended.** The token's TTL is an hour and the addon re-mints early,
   so while `github=app` was on the file executed roughly every 55 minutes, plus at every proxy
   relaunch and every 401 replay. Nobody had to run a command for a payload to fire.
2. **The rung was not TTL-bound.** Only emergency rungs expire; `app` persisted across sessions and
   reboots, because the PR bot is only useful if it stays on.
3. **`uv run --script` removed both accidental barriers.** It was chosen because the shared mount's
   ownership mapping strips the exec bit — which also means an attacker doesn't need it. And PEP 723
   inline metadata is honoured, so the payload could be a *dependency* fetched from the network at
   mint time, with no suspicious Python in the diff at all.

This falsified two claims the product makes: that "no daemon running ⇒ no credential flows,
whatever any file inside the yard claims" (the daemon's minter *was* a file inside the yard), and
that a hijacked agent "can scribble on the local checkout but cannot lift credentials" (scribbling
on the checkout was precisely how it lifted them).

**Fixing only the minter would have been theatre.** The same shape appeared six more times, each
repo-controlled and host-executed with no acknowledgement step: `[[inject]].minter` as an arbitrary
command string in committed config; `[project].dev_vm_dir` redirecting the minter path anywhere in
the repo; the `worktree_init` script run by `fy worktree add`; the VS Code attached-config
generator run by `fy code`; `[proxy] allow` (and later `default_deny`) folded into the host's
effective allowlist, so the box could widen — or switch off — its own wall by editing a file rather
than calling the verb that refuses in-box; and `[claude].system_prompt`, which is not host
execution but the same "repo content steers a privileged actor" shape.

## Decision

**Nothing under the repo mount executes on the host.** The rule is applied by asking one triage
question per surface — *does this actually need host privileges?* — which has exactly three answers:

1. **No → run it in the yard.** `worktree_init` copies gitignored config between two checkouts,
   both inside the mount; it needs zero host privileges. It runs in a throwaway container over the
   worktrees root (`worktree._init_in_yard`). The engine becomes a prerequisite and `fy worktree
   add` SKIPS with a retry hint when there's no image yet — deliberately **not** a host fallback,
   because "the yard wasn't ready" must never silently become "so we ran it on the Mac".
2. **Yes, but it needs no credentials → split it: read in the yard, write on the host.** The VS Code
   generator runs in the box (which `fy code` already requires to be up) and PRINTS a JSON document
   on stdout; foldyard parses it host-side and performs the one privileged write. The document is
   sanitized by an **allowlist** (`_ALLOWED_CONFIG_KEYS`), not a denylist: VS Code's attached-config
   schema includes lifecycle hooks and `initializeCommand` runs ON THE HOST, so a passthrough would
   have handed the repo back exactly the path this closed, and a denylist fails open the day the
   schema grows another hook. Dropped keys are named rather than silently swallowed; values with
   host consequences are pinned rather than trusted.
3. **Yes, irreducibly (credential minting) → it belongs in the package.** Minters are a finite set
   of named KINDS shipped as package modules (`static`, `gh-cli`, `github-app`, the Codex refresh
   flow, the gcp metadata minter), invoked as `<sys.executable> -m foldyard.plugins.<kind>`.
   `[[inject]] minter = "<command>"` is **removed**, not deprecated — a command string in committed
   config is the hole under a new name.

Four rules follow, and they are the ones to keep:

- **Config declares data; it never supplies a command.** A `[[secret]]` row carries a `how` hint —
  "where do I get this?" — which foldyard **PRINTS and never executes**. It can be a gcloud command,
  a 1Password item, a URL, or "ask Ops". Vault *fetching*, if it ever ships, is a fixed set of
  package-implemented `source =` kinds, never a command.
- **Doctor checks PRESENCE, offline; it does not check provenance.** "Is the credential there,
  read exactly as the supervisor will read it" plus a cheap offline shape check. Asking a vault
  instead drags its whole access chain onto the critical path, which is how the highest-value
  identity on this Mac came to gate the lowest-privilege capability in the system — a cloud
  elevation chain standing between you and posting a PR comment.
- **A minter's environment is an allowlist, not an inheritance.** Each minter runs with a small base
  (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, the proxy vars) plus the names its rule declared —
  otherwise one mint's worth of code execution reaches every axis's credential. This needs a field
  **separate from** `requires`, and conflating them fails in both directions: `requires` is the
  supervisor's spawn gate for the whole proxy daemon, so a credential in it means one missing key
  connection-refuses *every* box request under always-route; leave `env` implicit and the github
  minter can read the Anthropic key.
- **Repo config that reads as a control is host-owned.** Egress grants and the wall switch live in
  the host store at every level, never in `foldyard.toml`. `[plugins.github].permissions` is capped
  by the package default as a ceiling. `[engine].cli` is validated against the two engines that
  exist. `[[inject]].token_env` is DERIVED from the axis, and the derivation is checked for
  collisions — `pen-pot` and `pen_pot` both fold to `FY_INJECT_PEN_POT`, which is the hole the
  derivation closed, one rename away.

## Consequences

- **It closed by DELETION, not by building a gate** — and that's the transferable result. The
  obvious response to "repo code runs on the host" is to gate it; a full trust store was designed
  (below) before anyone counted the customers, and there were two. Removing the mechanism left
  nothing to gate, and the codebase got smaller rather than larger. Count the customers first.
- **What a consumer loses:** a bespoke vault or credential flow can no longer be added with config
  alone. The routes are (a) upstream a named kind, (b) ship an entry-point plugin — the
  "installed = trusted" tier, an explicit host-side act — or (c) put the secret in `host.env` by
  hand or via capture, with a declared `how` hint. This is a deliberate narrowing of ADR-0019's
  "plugins in your own repo" promise **for host-executed credential code specifically**; every
  other hook was unaffected at the time (ADR-0024 has since withdrawn the promise entirely, on
  this ADR's reasoning). It reads as the honest conclusion of ADR-0019's own reasoning: if
  whole-plugin trust is the only honest boundary, the credential hook is the one place not to offer
  a config-shaped bypass of it.
- **The counter-pressure to watch is config-DSL creep.** A `jwt-exchange` kind with claim templates
  and JSON paths is a step toward a programming language in TOML. The boundary that keeps it honest:
  a small set of named kinds each with a fixed shape, and for anything that doesn't fit, the
  entry-point plugin with its friction — not a more expressive schema.
- **Residuals, stated rather than claimed closed.** Git's own hooks (`lefthook.yml`, `.git/hooks/*`,
  `.git/config` aliases) are host execution from a box-writable checkout, and foldyard doesn't own
  `.git`; the status is scoped to foldyard-MANAGED artifacts. `[claude].system_prompt` remains
  repo-controlled prompt injection into a privileged actor. Both are in `docs/security.md`.
- **Per-field closure is not closure of the class.** Every fix here was per-FIELD, and the *channel*
  stayed open: the supervisor re-read `foldyard.toml` from the working tree every tick, so two
  fields nobody enumerated (`[proxy] passthrough`, `[[inject]] host`) rode it for months. That is
  what [ADR-0022](./0022-host-runs-the-adopted-config.md) acts on, and it is the general lesson —
  while a box-writable file is authoritative input to the host, the next field added re-opens the
  class.

## Rejected alternatives

- **Generalise ADR-0019's hash-acknowledged trust store to cover every host-executed consumer
  artifact.** Designed in detail before being made unnecessary, and worth recording because the
  design is sound if a genuinely bespoke host-side mechanism ever appears. Its shape: because the
  moment a repo file executes is often unattended (TTL expiry, proxy relaunch, 401 replay — no TTY
  to ask at), check-on-use is impossible, so the ack must **copy the acked bytes out of the mount**
  and the supervisor executes the snapshot; mint time consults the working tree not at all. The
  trust unit is a SET (the whole closure, `{path: sha256}`), keyed by ABSOLUTE path so an ack in
  main doesn't cover a worktree's different copy at the same repo-relative path. Transitive calls
  are not followed — no analysis of untrusted code can be sound (`sh -c "$(cat helper)"`, `node -e`,
  dynamic imports, PATH lookups, PEP 723 deps), and the snapshot root makes following unnecessary:
  an un-acked callee is simply ABSENT and fails loudly, so the closure is discovered empirically.
  Rejected as the primary fix because deletion beat it, and because it is friction paid forever.
- **Stage-and-pin the minter** (snapshot it into `state_dir` before each launch, as the mitmdump
  addon already is). Ten lines, and it closes the "edit it now, it runs within the hour" window —
  but it is hardening, not a boundary: a poisoned tree present at the next relaunch is snapshotted
  and run. Correct only as a companion to an ack, never as the fix.
- **Keep the PEP 723 minter and pin its dependencies** (`uv lock --script` + `--locked`). This does
  close a real hole — without a lock, a fully acked, never-modified minter executes newly resolved
  code tomorrow with no repo change and no re-ack — and it makes a one-line `dependencies = [...]`
  typosquat visible as a lock diff. But it cannot close the deeper problem: **pinning fixes *which*
  unreviewed code runs, not that it was reviewed.** A human acking 120 lines of Python thereby
  trusts three packages and their transitive graph. Only moving the deps into the tool venv — where
  they are installed by an explicit host act — closes that, which is this ADR.
- **Narrow the claim instead of fixing it** (rewrite the security docs, rely on posture-off
  discipline). Insufficient alone, since this boundary is the product's central claim — though the
  doc correction was required under every option and was made.
