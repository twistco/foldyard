# foldyard example consumer

A minimal, self-contained stack — **Postgres + a FastAPI api** — that exercises foldyard
end to end without any project-specific machinery. It is the "bring your own compose stack"
payload the original design called for, and the fixture we dogfood `foldyard up | verify |
box` against (including headless, inside a nested-KVM sibling container — see below).

```
example/
├── foldyard.toml        # the consumer config foldyard reads
├── compose.yml          # db (postgres) + api (built from ./api)
├── compose.feature.yml  # posture overlay — layered when fakedep=on (walkthrough below)
├── api/                 # the FastAPI app + its Dockerfile
├── box.Dockerfile       # a minimal dev-box image (satisfies the PLAN §7.4 contract)
└── .gitignore           # ignores foldyard's .dev-mode.json mirror
```

## What it deliberately does NOT lock down

This fixture covers the **stack** tier: compose, ports, overlays, the mode system, worktrees. It
declares no `[proxy]` and no `[machine].wall`, so the box's egress is direct — unrouted,
unobserved, unfiltered. That's one notch below what `foldyard init` writes for a new project
(lima + wall + an *enforcing* proxy allowlist) and two below
[`../example-lima-wall`](../example-lima-wall/).

The omission is structural, not simplification. The egress proxy runs **on the host**, and
declaring `[proxy]` makes every box route through it: `fy box up` then hard-fails unless a CA
exists that only the host-side supervisor (`fy host`) can generate — and that supervisor
deliberately refuses to run anywhere but the host. This example has to run where no host side
exists: inside a dev box, inside a CI container, inside the nested-KVM rig (see below). A fixture
that required a full host side would stop being the thing we dogfood everywhere.

So read *this* one for how a consumer wires a stack, and `../example-lima-wall` for the
locked-down posture — wall, enforced allowlist, keyless agent. What `fy init` hands a new
project is the second, not the first.

## Run it with foldyard

Copy the example OUT of the foldyard checkout first — a foldyard project must be its own git
repo (the machine mounts the checkout; a `foldyard.toml` nested inside a bigger repo makes
preflight abort `fy up` with this same recipe). This mirrors what CI does (see
`foldyard/CLAUDE.md`'s nested-engine recipe):

```bash
cp -r foldyard/example ~/fy-example && cd ~/fy-example
git init && git add -A && git commit -m init
foldyard up                 # bring the stack up (builds the api image on first run)
curl localhost:8080/        # {"service":"foldyard-example-api","status":"ok"}
curl localhost:8080/db      # {"db":"reachable","version":"PostgreSQL 16…"}  ← DB wiring
foldyard ps                 # the two services
foldyard verify             # the isolation battery (VM boundary; box posture when in-box)
foldyard box up             # the dev box (engine socket + this checkout mounted)
foldyard box shell          # a shell inside it; `foldyard verify` here runs box posture too
foldyard down               # stop the stack
```

Ports come from `[ports]` in `foldyard.toml` (`API_PORT=8080`, `PG_PORT=5544`); in-container
ports are fixed, so a second worktree stack gets offset host ports and never collides.

## Posture walkthrough — modes, overlays, requirements (zero secrets)

The flow above is posture-less. This one drives foldyard's **mode system** against the same
stack, using the bundled zero-secret rig declared in `foldyard.toml` (`[plugins.fakecred]`):
`fakecred` stands in for a credential-granting axis (a real daemon, TTLs, a capability
probe), `fakedep` for a behaviour axis that *consumes* it — the same shape as a real
consumer's gcp + llm pair, with no secret anywhere. Run on the host (mode writes are
host-only, by design):

```bash
foldyard mode                          # the axes at their zero-secret resting defaults
foldyard mode fakedep=on               # REFUSED — needs the fake credential; the message
                                       # carries the atomic fix ↓
foldyard mode fakecred=on fakedep=on   # accepted together (never trapped mid-escalation)
foldyard up                            # now ALSO layers compose.feature.yml onto -f
curl localhost:8080/                   # …,"feature":"on"} ← the posture, observable
foldyard mode fakecred=off fakedep=off
foldyard up                            # back to rest; "feature":"off"
```

Where each piece of that is declared:

- the **axes** come from the fakecred plugin — loading it is just `[plugins.fakecred]` in
  `foldyard.toml`; real credential plugins (gcp, github, …) contribute theirs the same way.
- the **overlay** is pure config: `[[overlay]] when = { fakedep = "on" }` layers
  `compose.feature.yml` ([../docs/compose-overlays.md](../docs/compose-overlays.md)).
- the **refusal** is a declarative requirement evaluated by the registry. *This* one ships
  in-code on the plugin's axis (intrinsic — fakedep exists to consume fakecred). When your
  own wiring creates the coupling — say your overlay routes LLM traffic through Vertex, so
  `llm=live` only works under `gcp=sa` — declare it beside that overlay as config instead:

  ```toml
  [[require]]
  axis = "llm"
  when = ["record", "live"]
  needs = "gcp"
  accepts = ["sa", "user"]
  reason = "the runtime-SA identity"
  ```

  Schema + the intrinsic-vs-wiring rule: [../docs/configuration.md](../docs/configuration.md).

TTL expiry, the settle cascade, capability DEGRADED/heal, and the `fy clock` fast-forward
are the same rig one level deeper: [../docs/testing-modes.md](../docs/testing-modes.md).

## Why it lives here — nested validation

foldyard's machine/box/daemon lifecycle is "Mac-only" to validate *from inside a project's
own dev box*, because creating a machine or box there would collide with the live one. The fix
(proven 2026-06-14, see [../docs/nested-virt.md](../docs/nested-virt.md)):
the Apple-Silicon (M3+/macOS 15+) `podman machine` runs the **libkrun** provider, which
exposes **`/dev/kvm`** into the guest. A throwaway sibling container launched over the
socket with `--device /dev/kvm` therefore gets real, hardware-accelerated nested
virtualization — an isolated engine to run this example's full `up`/`box`/`verify` flow
headlessly, no Mac round-trip. This example is the workload that nested environment runs.
