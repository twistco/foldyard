# Security model — the boundary, the threat model, and `fy verify`

The [README's third thesis](../README.md#three-theses) is *isolation without hiding*: safety
comes from a real boundary, not from concealment, and you can test the boundary yourself. This
page is the depth behind that claim — what the boundary is, what it defends against, what it
honestly doesn't, and exactly what `fy verify` proves. The "why" behind each design call lives
in the [ADRs](./adrs/); this page links them rather than restating them.

## The boundary: a disposable VM that mounts only your repo

Everything that runs untrusted code — installs, build scripts, your compose stack, the agent —
lives inside a **rootless Podman machine**: a throwaway Linux VM that mounts **only your repo**
(plus its worktrees root). No `$HOME`, no other checkouts, no keys. Two properties combine
([ADR-0001](./adrs/0001-rootless-podman-vm-isolation-boundary.md)):

- **Rootless engine.** A container's "root" maps to an unprivileged user *inside the VM*, so
  the classic `--privileged --pid=host` breakout to VM-root is refused at the source, not
  filtered after the fact.
- **Repo-only mounts.** The VM's mount table contains your repo and nothing else. There is no
  host filesystem to reach *even at VM level*.

So a container escape — a kernel bug, a runtime bug, whatever — lands the attacker inside a
disposable Linux VM whose only contents are a repo they could already read. Not on your
machine, not near your keys or browser profile or other projects. That's also why handing an
agent the in-VM engine socket is safe: the socket's power is bounded to the repo and the VM's
containers, never the host.

**The VM is required even on Linux.** Native rootless podman has no repo-only blast radius:
the engine sees your whole filesystem, so anything holding the socket could mount `~/.ssh`
into a new container. The VM is what makes "socket = repo + containers only" true at all —
which is why `backend = "native"` exists only as an explicit opt-in with a stated weaker
profile, never the default.

## Threat model

What foldyard is built against — not the agent as such, but what rides in with development:

- **Malicious packages and lifecycle scripts** (`npm install`, PyPI, postinstall hooks).
  They execute with full freedom *inside the yard* and wake up to: the repo (which the
  clean-repo precondition keeps boring), no ssh keys, no tokens, no credential helpers, no
  push access. They cannot reach your home directory, your other repos, or any long-lived
  secret, because none of those exist inside the boundary.
- **Prompt-injected agents.** Same story: the agent has real autonomy — shells, the stack,
  the engine socket — but every capability it holds is bounded to the VM. A hijacked agent
  can scribble on the local checkout (recoverable); it cannot push, delete the remote, or
  lift credentials that were never there.
- **Build hooks and dev servers.** Anything a `just build` or `next dev` pulls in runs in
  the same cage under the same rules.

**The one honest overlap window:** if you declare a posture (say `github=app`) and *then* run
an untrusted install step, install-time code and a live injected path coincide. The token
itself still never enters the box (see below) and it's short-lived and scoped — but during
that window, in-yard code can *use* the granted access. Keep postures off while installing
things you don't trust yet.

**What in-yard code can still reach on the host.** No artifact FOLDYARD drives is executed
host-side any more: consumer scripts that only touch the mount (`[project].worktree_init`, the
editor-config generator) run in a container, and where their output has a host effect, foldyard
writes it from a validated document rather than letting the script write it. And **`foldyard.toml`
is not live input to the host**: the supervisor reconciles from a copy you adopted, outside the
mount, so an edit to `[proxy]`/`[[inject]]` is inert until an operator adopts it at the next
`fy up`/`fy host` ([ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)). Three channels remain
— the reasoning is in
[ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md):

- **Git's own hooks still are host execution, and foldyard doesn't manage them.** `lefthook.yml`,
  `.git/hooks/*` and `.git/config` aliases are box-writable in a shared checkout and run on the
  *host* the next time you run git there. Foldyard can't close this without owning your `.git`, so
  it's stated rather than fixed: treat a box-shared checkout's hooks as code you're choosing to run.
- `[claude].system_prompt` is repo-controlled text prepended to every colleague's agent, and
  `[claude.settings]` / `[codex.config]` are repo-controlled CLI config passed to the same agent
  (`--settings` / `-c`), where a `hooks` or `permissions` entry steers it harder than any prompt.
  Not host exec — it runs in the box, whose blast radius the VM already owns — but the same "repo
  content steers a privileged actor" shape. `fy config widenings` lists all three under *agent
  steering*. (They're pinned like the rest of the file, so a change is adopted rather than picked
  up — but adopting is a human reading a diff, which is a weaker guarantee than "can't happen".)
- **Foldyard's own code, while it lives in the consumer repo.** Under the in-repo carve-out
  ([ADR-0013](./adrs/0013-in-repo-carve-out-until-extraction.md)) the package is installed
  `--editable` from the checkout, so it is box-writable, and a Mac-side `fy up` deliberately adopts
  changed code by bouncing the supervisor (the code fingerprint). Config pinning does not close
  this; the standalone extraction does, by making the installed copy independent of the mount.

## The credential story: nothing at rest, injected in flight

The yard's resting state is **zero secrets anywhere**: no key files, no token env vars, no
`~/.netrc`, no credential helpers — nothing to lift, so nothing to leak. When you need real
access, you declare a posture (`fy mode gcp=logs github=app`) and three mechanisms keep the
grant honest:

- **Tokens are minted host-side and injected at the proxy.** The box holds only a dummy
  value; the Mac-side egress proxy overwrites it with a real, short-lived, narrowly-scoped
  token as the request passes through. The real token never enters the yard — not as env,
  not as a file ([ADR-0007](./adrs/0007-credential-injection-at-egress-proxy.md)). A fully
  compromised box can use the access only while the posture is on, and only against the
  injected host; it can never *hold* the credential.
- **Emergencies are TTL-bound.** High-privilege "act as me" rungs get a mandatory expiry
  (default 1 h) and auto-revert to the secretless default — they structurally cannot linger
  ([ADR-0005](./adrs/0005-secretless-by-default-posture-axes.md)).
- **Enforcement lives outside the blast radius.** Authoritative posture state sits in your
  host home, where nothing in the yard can reach it; the box cannot escalate its own posture.
  So does the **config the host reconciles from** — the adopted `foldyard.toml` — because a file
  in the mount that decides where credentials get injected is a posture the yard could set for
  itself ([ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)).
  The host-side daemons are the only path a credential takes, so **no daemon running ⇒ no
  credential flows** ([ADR-0006](./adrs/0006-host-side-enforcement-single-supervisor.md)).
- **The daemons run installed code, not repo code.** Minters are packaged kinds
  (`github-app`, `gh-cli`, the Codex refresh flow, `static_token`); there is no
  `minter = "<command>"` config key and no minter path inside the mount. This is load-bearing:
  a minter living in the repo would mean any write to the checkout — an agent, a package
  postinstall, a branch you checked out to review — executed as you, next to the credentials,
  on the next mint. Secrets are declared with `[[secret]]`, whose `how` hint foldyard **prints
  for you to run** and never executes. The decision that established this, and the surface it
  covers, is
  [ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md).

## Network: what's cooperative, what's enforced

The box's egress routes through the host-side proxy via proxy environment variables. That
gives you full visibility — every destination logged, optional decrypt-and-log, an optional
default-deny allowlist on the proxy path. But env-var routing is **cooperative**: software
that honors it is logged; malicious code can unset `HTTPS_PROXY` and go direct. Foldyard
will not call that filtering, because it isn't
([ADR-0009](./adrs/0009-monitoring-cooperative-enforcement-locked.md)).

**Enforcement is `[machine].wall = true`** (Lima backend): a fail-closed firewall provisioned
into the VM itself, so traffic that ignores the proxy env is *rejected*, not silently missed —
the proxy becomes the only way out. One residual to know about: QUIC/UDP-443 can bypass a
CONNECT proxy in cooperative-only setups (the wall closes this); depth on capture modes and
the wall's rules is in [networking.md](./networking.md).

## `fy verify`: prove it, don't trust it

`fy verify` is the credibility check — a battery you can run any time, exiting non-zero on
any failure (CI-usable). Run it from a shell *inside* the box (`fy box shell`, then
`fy verify`) for the full battery; outside the box only the VM-boundary checks run. What it
asserts, grouped:

- **VM boundary** (over the engine socket): the engine reports **rootless**; a
  `--privileged --pid=host` container **cannot read the host's PID-1 namespace** — the
  known breakout, actively attempted and refused; no `/Users` visible inside a
  `--privileged` container; and a **mount audit** — the VM's mount table is free of any
  host home path.
- **Credential-agnostic backstops** (inside the box): no SSH agent forwarded, no `~/.ssh`
  private-key material, no `~/.netrc`, and `git ls-remote origin` **fails** — the box can't
  even reach the remote to push, by construction. These live in foldyard's core so no absent
  or broken plugin can weaken them.
- **Mode-aware posture checks** (from the credential plugins): each mechanism asserts its
  own posture — e.g. for `github`, the token in the box is never more than the ambient
  dummy `x` in *any* mode (a real-looking token fails verify everywhere; the real one stays
  host-side, and under `github=off` no inject rule exists so the dummy grants nothing). The
  `gh` CLI and dummy are inert box plumbing, pre-positioned like the proxy CA so the axis
  flips live host-side — access is the host proxy's decision, not the box env's.
  Active **emergency modes print a banner** so a TTL-bound escalation is never invisible.
- **Wall probes** (only under `lima` + `wall = true`): direct, proxy-ignoring connections
  from the box to a public IP on 443 *and* on 53 (the DNS exfil-tunnel class) must be
  refused. One caveat verify states itself: an offline host also fails these probes, so a
  fast rejection is the healthy signature and a long timeout is the suspicious one.

Alongside it, **`fy config widenings`** answers the question verify doesn't: not "is the posture
what it claims?" but "what did we agree to, and where is it written?" — the hosts `passthrough`
exempts from capture (with `@all` resolved to its real count), where each mechanism delivers its
credential, which agent prompt is shared and which is yours, and any key that reads as security
config but is no longer honoured.

**What verify deliberately does not prove.** It exercises the escapes we know about — it
raises assurance, it doesn't prove a negative. A pass means the known breakouts are refused
and the posture is what it claims, not that no unknown escape exists. It also doesn't (yet)
scan your repo for committed secrets — that's your precondition, below — though folding a
secret scan into verify is on the roadmap.

## The precondition: a clean repo

Foldyard mounts your repo into the yard, so the zero-credential guarantee holds exactly when
the repo carries **config, not credentials**. Foldyard can mount a clean repo; it can't clean
a dirty one. Before adopting, scan your history with
[gitleaks](https://github.com/gitleaks/gitleaks) or
[TruffleHog](https://github.com/trufflesecurity/trufflehog), move anything they find into a
posture, and re-scan routinely — see
["Before you adopt"](../README.md#before-you-adopt-a-clean-repo) in the README.

## Reporting a vulnerability

Security reports will go through a `SECURITY.md` policy, landing with the standalone
extraction. Until then, use the project's issue tracker for non-sensitive reports.
