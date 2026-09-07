# Posture modes — secretless by default, credentials on demand

The yard starts with zero credentials, and that never changes by accident. When you need
real access — logs from your cloud project, a GitHub App that can comment on PRs, a keyless
coding agent — you *declare a posture*, and host-side machinery outside the yard makes just
that much access flow. Turn it off (or let the TTL do it) and you're back to zero.

This page is the how-to: the model, the commands, and what's actually enforcing it.

## The model: axes and rungs

A posture is a set of **axes**, each a ladder of **rungs** from "zero secrets" up to
"emergency, my own identity". Rung 0 is always the zero-secret resting state — what an
unset, invalid, or expired value reads as. Credential ladders name it `off`; an axis whose
resting state is a swap rather than a toggle names it for what it is (a `storage` axis might
rest at `local`).

```bash
fy mode                        # the dashboard: every axis, its rung, daemon health, TTLs
fy mode github=app             # declare a posture (host only)
fy mode github=user ttl=30m    # emergency rung: mandatory TTL (default 1h, max 8h)
fy tui                         # the same posture as a live dashboard, with one-click changes
```

`fy mode` works inside the box too — you can always *see* the posture — but setting it is
host-only, by construction (below).

## Where your axes come from

Axes are contributed by plugins, and a plugin contributes nothing until your `foldyard.toml`
declares it. Core ships a `github` axis (`off | app | user`), an `[[inject]]` table for
generic header injection, a `capture` axis on the proxy, and the agent axes (`claude`,
`codex`) that appear only when you configure keyless auth. Everything else — cloud identity,
auth simulators, LLM routing — comes from the plugins your project opts into: no plugin in
the config, no axis in the dashboard.

So `fy mode`'s output is a picture of *your* project. As one real-world example, a consumer
running foldyard with its own gcp/storage/auth0/llm plugins sees axes like
`gcp=off|logs|sa|user` and `llm=off|record|live` next to the core `github` axis — those
rungs are that consumer's plugins, not foldyard's.

## Enforcement lives on the host

A mode is a *desired posture*, not a capability. The state lives in two files:

- **Authoritative:** `~/.foldyard/<project>/…/dev-mode.json` on the host — deliberately
  outside the shared repo mount, so nothing inside the VM or box can escalate its own
  posture by editing a file. Only `fy mode` on the host writes it.
- **Read-only mirror:** a gitignored `.dev-mode.json` in the checkout, refreshed on every
  mode change and every supervisor heartbeat, so box sessions can see the posture. Purely
  informational — nothing ever grants access based on it.

What actually grants access is the **host daemons**: token minters and the egress proxy,
run by `fy host` — one foreground supervisor process per project, visible in one terminal,
stopped by Ctrl-C. `fy up` launches it for you when a mode demands daemons; a newer launch
replaces a stale or out-of-date holder. The supervisor re-reads the mode file every couple
of seconds and reconciles: raise a rung and the daemon starts, drop it and the daemon stops.

This is the whole security story in one line: **no daemon running means no credential
flows, whatever any file inside the yard says.**

## Tokens never enter the yard

When a posture is on, the minters produce short-lived, scoped tokens on the host, and the
egress proxy injects them into requests *in flight* — the upstream API sees the credential,
the box never does. Real secrets the minters need live in `~/.foldyard/<project>/host.env`
(owner-only, outside the repo).

Two guard rails keep escalation honest:

- **Emergency rungs auto-revert — and land coherent.** Rungs marked emergency (your own
  identity, push access) carry a mandatory TTL. Every reader treats a lapsed TTL as the
  default rung, and the supervisor kills the daemon and writes the axis back down at expiry
  — an emergency can never quietly become your resting posture. If the revert would strand
  a *dependent* axis on a combination that can only fail, the same write settles it down to
  its default too (the cascade is logged), so expiry always lands on a posture that works
  offline.
- **Incoherent combinations are refused.** Some rungs only function alongside another
  axis's posture. `fy mode` checks the whole prospective posture and refuses a combination
  that can't work — and the error message carries the exact `fy mode a=x b=y` command that
  fixes it, since updates apply atomically. Combinations that work but are probably not
  what you want print a warning and apply.

## Capability is observed, not assumed — DEGRADED

A mode says what you *want*; whether the credential chain behind it actually works right
now is a separate fact that can silently lapse (the PAM grant behind `gcp=sa`, your ADC, a
rotated token). Plugins contribute **capability probes** for their active rungs — the
continuous version of their doctor checks — and the supervisor runs the due ones each tick,
publishing results to the host state (`capabilities.json`) and each up box's mirror. A
failing chain renders on every posture surface as

```text
gcp     sa    … ⚠ DEGRADED — impersonation of app-runtime failing: … PAM lapsed? just gcp-elevate
```

instead of surfacing as request-time 401s an hour later. Deliberately observation-only: a
lapse never changes the mode (your declared intent survives the lapse and heals in place),
and a probe never grants anything — enforcement stays exactly where it was.

The posture surfaces are pull, though — mid-session (the 12h PAM grant expiring under a
running stack) nobody is watching `fy mode`. So the supervisor also reacts to an axis's
**merged verdict flipping** (diffed against the last published `capabilities.json`, so a
lapse+heal spanning a supervisor restart still counts):

- **either edge** posts a macOS notification (`osascript`; opt out with
  `[host] notifications = false`) — the DEGRADED one carries the fix from the probe detail;
- **a heal** additionally restarts the consumer's `[resnapshot_on_capability]` services for
  that axis (on a worker thread, off the heartbeat-owning tick). Services that snapshot
  credentials once at boot — a startup secret fetch frozen into import-time state — only
  pick a healed chain up by rebooting, so the operator's whole job is the fix the
  notification names (`just gcp-elevate`); the bounce is automatic.

Still observation-only in the security sense: the reaction restarts *containers*, it never
grants, blocks, or writes mode state.

`fy up` reads the same published claim (`devmode.degraded_capabilities`) and prints any
active-axis lapse as the LAST lines of its summary — an `up` under a degraded chain must not
finish looking healthy, and a steady lapse posts no fresh notification for it to catch. The
TUI raises the same claim as a banner on the Mode tab, stacked with (not replacing) the
emergency-rung banner.

## `fy state` — every tier, desired vs observed

Posture state lives on several tiers with different refresh lifetimes (the authoritative
file, the box mirror, env baked into running containers, host daemons, the capability
chain). `fy state` prints one desired → observed row per tier and exits non-zero on drift —
"which tier is stale" as one command instead of archaeology:

```text
  ✓ posture     gcp=sa llm=record → authoritative (…/dev-mode.json)
  ✗ daemons     gcp-minter listening on :41100 → DOWN — run `fy host` on the Mac
  ✗ capability  gcp=sa capability chain works → DEGRADED — … just gcp-elevate
  ✓ stack       overlays: compose.identity.yml, compose.llm.yml → 14 containers on the posture overlays
  ✓ box         dev box env matches the posture → current
```

## Testing all of this without secrets

The `[plugins.fakecred]` testing axis pair + `fy clock` fast-forward exercise every
behaviour above (expiry, the settle cascade, probes degrading/recovering, daemon lifecycle)
on a real host with zero credentials — see [testing-modes.md](./testing-modes.md).

## Keyless agents

If you run a coding agent in the box with `keyless` configured (`[claude]` or `[codex]`),
you get an on/off axis for it:

```bash
fy mode claude=on
```

The first `fy box up` after configuring keyless prompts **once**, on the host, with a
hidden prompt: paste your key or token, it's classified by prefix (no "which kind?"
questions) and stored in `host.env`. The box gets only a **dummy** credential — enough to
make the client send an auth header — and the proxy overwrites that header with the real
value in flight. Never pasted in the box, never in the repo, never in scrollback.

What each keyless mode does:

| keyless mode | dummy in the box | header the client sends | proxy rewrites on | real value lives in |
|---|---|---|---|---|
| `[claude] api-key` | `ANTHROPIC_API_KEY` env | `x-api-key` | `api.anthropic.com` | `host.env` |
| `[claude] oauth` | `CLAUDE_CODE_OAUTH_TOKEN` env | `authorization: Bearer …` | `api.anthropic.com` | `host.env` |
| `[codex] api-key` | `OPENAI_API_KEY` env | `Authorization: Bearer …` | `api.openai.com` | `host.env` |
| `[codex] chatgpt` | a dummy `~/.codex/auth.json` | `Authorization: Bearer …` | `chatgpt.com` (codex API path) | the host's real `auth.json`, refreshed by the minter |

With the axis `off`, the box holds only the dummy and can't reach the provider at all.

One box-shaped caveat: toggling keyless bakes (or removes) the dummy credential in the box
environment, which takes a `fy box up` to land — the dashboard tells you when the running
box's env is out of date. Everything else about a mode change needs no box restart.

## Per-worktree posture

Each worktree declares its own posture: its own mode file, its own mirror, its own daemon
ports — so one branch can sit at `github=app` while another stays fully offline. What stays
shared is identity: one `host.env`, one proxy CA, and the one supervisor, which serves every
worktree whose box is up.

## Modes reconcile the running stack live

A posture can shape your compose stack — enable a profile, layer an overlay file, flip an
env var a service reads. When you change a mode, foldyard re-renders the affected services
of an already-running stack for you: no manual `down`/`up`, and no surprise either — a mode
change never *starts* a stack that wasn't running. Recipes pick posture-derived env up as
defaults (`${K:-v}`), so anything you export explicitly still wins.

## Writing your own axis

An axis is a small plugin: declare the rungs and blurbs, and contribute the daemons, proxy
rules, env, and doctor checks your mechanism needs — the substrate (`fy mode`, `fy host`,
the TUI, `fy verify`) picks all of it up through the registry. See the plugin guide
(`docs/plugins.md`, coming) for the hooks.
