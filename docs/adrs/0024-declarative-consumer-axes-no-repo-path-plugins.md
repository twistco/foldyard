# ADR-0024 — Consumer customisation is declarative data: no repo-path plugin loading, no trust store

- **Status:** Accepted (2026-08-29) — **supersedes
  [ADR-0019](./0019-consumer-repo-plugins-trust-model.md)**. The deletion half stands as decided;
  the replacement surface (`[[axis]]`, the conflict relation, `[localhost_tls]`) is **not yet
  built**, so `auth0_sim.py` and `llm.py` still ship as declared built-ins today.
- **Sources:** spinout-plan D-C; the host-exec surface audit (internal working note). Related:
  [0015](./0015-plugin-framework-per-consumer-registry.md) (the plugin framework this bounds),
  [0022](./0022-host-runs-the-adopted-config.md) (the config channel),
  [0023](./0023-no-host-executed-code-from-the-repo-mount.md) (the same principle applied to
  credential code).

## Context

[ADR-0019](./0019-consumer-repo-plugins-trust-model.md) decided to let a consumer point
`[plugins].load` at a Python file in their own repo, guarded by a host-side, hash-acknowledged
trust store — the direnv model. Two things happened between that decision and the extraction that
argue against building it, and a third removed the reason it was wanted.

**1. It re-opens the class the codebase just spent five rounds closing.** The audit behind
[ADR-0023](./0023-no-host-executed-code-from-the-repo-mount.md) did not gate host-executed
repo-mount code; it *deleted* every instance — both minters, `[[inject]].minter`, `worktree_init`,
the VS Code generator — and the codebase got smaller. `[plugins].load` proposes re-opening exactly
that class in its most privileged form: arbitrary consumer Python imported into the supervisor
process, next to the minters and `host.env`.

**2. The trust store as specified is weaker than what was later built for config.** ADR-0019 is
check-hash-then-import-*from the mount*. [ADR-0022](./0022-host-runs-the-adopted-config.md)
concluded, for `foldyard.toml`, that the only sound form is *adopt a snapshot outside the mount and
never read the working tree again* — because the path that matters (unattended daemon relaunch) has
no TTY to re-acknowledge at. A gate that re-reads the mount at use time fails on the same path.

**3. The migration that justified it turns out not to need plugins at all.** ADR-0019 scheduled the
two Tangible-shaped built-ins to migrate out of the package as the "API-sufficiency proof". Reading
them:

- **`llm.py` (66 lines) implements exactly one hook — `axes()`** — returning a single axis with
  three rungs and a blurb. Its overlays are already `[[overlay]]` config; its GCP coupling is
  already `[[require]]` config. It is *data wearing a class*.
- **`auth0_sim.py` (158 lines)** implements `axes()`, `mode_issues()`, `doctor_checks()` and
  `doctor_fixes()` — no daemons, no `derive_env`, no `compose_overlays`, those having been
  superseded by `[[overlay]]` beforehand.

So the proof would have proved nothing about the hooks that make plugins powerful.

## Decision

**Add a declarative `[[axis]]` table and delete both built-in plugins.** No `[plugins].load`, no
trust store, no `fy plugins trust`, no new doctor tier — because data is not code. This fits the
pattern the config surface already converged on: `[[overlay]]`, `[[require]]`, `[[secret]]` and
`[[inject]]` are all consumer-declared data evaluated by package-side code.

That deletes `llm.py` outright and reduces `auth0_sim.py` to two residues, each of which gets a
home rather than an exception:

- **The `auth0`×`storage` conflict warning** becomes a **conflict relation** alongside `Requires`:
  `axis="auth0", when=["sim"], conflicts="storage", at=["staging"], severity="warn", message="…"`.
  It cannot be a `[[require]]` row, because `Requires` means *"while the owner is at rung X, the
  other axis must be at one of `accepts`"* and an absent axis satisfies nothing — correct for a
  dependency, wrong for a conflict, which must warn only when the other axis IS at a rung. This is
  the first `[[…]]` table whose payload is prose, and it extends an evaluator that is already
  property-tested over generated constraint graphs.
- **The mkcert/nss doctor checks and fixes** are absorbed into a small **core** `[localhost_tls]`
  check set with a configurable `cert_dir`. These cannot become config: `DoctorFix.cmd` is a
  command the host executes, and a consumer-declared command is precisely the `minter = "<command>"`
  hole ADR-0023 closed. Keeping the commands package-side keeps both the checks and the fix
  buttons. The sim-container bounce is dropped — `stack.up()` already bounces the sim when
  `auth0=sim`.

**Why local TLS is "core", since core needs a boundary.** foldyard hosts a consumer's stack *with
compose*, and a compose-hosted stack is overwhelmingly a **web** stack: services on localhost ports,
a browser as the client. Anything reaching for a third-party IdP, a `Secure` cookie, a service
worker or WebAuthn hits a browser rule demanding HTTPS on a non-`localhost` host. So "the
developer's browser trusts the certs this stack serves" sits inside foldyard's job the way "the box
can reach the engine" does. It is a bounded claim: foldyard **checks for and points at** the local
TLS toolchain — it does not become a CA and does not touch the trust store itself.

**Entry-point plugins remain the third-party tier** ("installed = trusted" — installing a
distribution into the tool venv is already an explicit, host-side act), plus one cheap doctor
guard: warn if a loaded plugin's `__file__` resolves inside the repo mount, since an editable
install from the mount reopens the hole by the back door.

## Consequences

- **A consumer can no longer ship host-side hook *code* with config alone.** The routes are:
  declare an axis as data, upstream the behaviour, or publish an entry-point plugin and install it
  into the tool venv. Combined with ADR-0023, foldyard now has **no** path by which repo contents
  become host-executed code — a property worth stating as a whole rather than per-field.
- **The package stops carrying one consumer's shape.** `auth0_sim` and `llm` were Tangible-specific
  built-ins living in a general-purpose tool; they leave without landing anywhere else.
- **Two new surfaces to document and property-test**: the conflict relation and `[localhost_tls]`.
  Small, but they are the honest cost of the deletion — this ADR is not free, it is *cheaper*.
- **The counter-pressure to watch is the same one ADR-0023 named**: config-DSL creep. Each new
  `[[…]]` table is a step toward a programming language in TOML. The boundary that keeps it honest
  is a small set of tables with fixed shapes, and the entry-point plugin — with its friction — for
  anything that doesn't fit.
- **A wish-list item, recorded so it isn't re-derived.** A PyPI-installable replacement for
  `brew install mkcert nss` would ride the tool venv foldyard already owns and work on Linux hosts.
  The *generation* half is easy in pure Python (`trustme`, or `cryptography`); the half that earns
  mkcert its keep is **trust-store installation** (the macOS keychain via `security
  add-trusted-cert`, plus NSS via `certutil` for Firefox/Chrome), which is irreducibly
  platform-specific. Do **not** solve it by reusing the egress proxy's mitmproxy CA: that CA is
  trusted inside the box only, and promoting the key that sits beside a live intercepting proxy
  into the host's system trust store is a materially worse posture than mkcert's own CA, which
  signs nothing but localhost leaves.

## Rejected alternatives

- **Build ADR-0019 as designed** (path loading + hash-acknowledged trust store). Sound as a
  design — its detailed shape is preserved in ADR-0023's rejected alternatives, including the
  copy-the-acked-bytes-out-of-the-mount correction — but it has no customer. Count the customers
  before building the gate; there were two, and both were data.
- **Drop the `auth0`×`storage` warning entirely.** Free, and the boundary stays clean, but it loses
  a real guard on a one-command mistake with cloud-write consequences.
- **Ship auth0-sim as a small consumer distribution installed via an entry point.** Works today,
  but it must be installed from outside the mount or the hole returns, and it adds an onboarding
  step plus a reinstall per edit — a lot of ceremony for two hooks.
- **A declarative `[[check]]` table with a closed predicate vocabulary** (`which = "mkcert"`,
  `file = "…"`) plus a printed `how` hint — the `[[secret]]` pattern. Buildable and safe (data with
  a fixed evaluator, not a command), but it loses the fix buttons and adds a config surface, and
  the `which`/`file` vocabulary is a mild host-probing primitive that would want naming in its own
  ADR if ever built.
