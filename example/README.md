# foldyard example consumer

A minimal, self-contained stack — **Postgres + a FastAPI api** — that exercises foldyard
end to end without any project-specific machinery. It is the "bring your own compose stack"
payload the original design called for, and the fixture foldyard's own tests run `fy up`,
`fy verify` and `fy box` against — in CI and on a nested-KVM rig (see below).

```
example/
├── foldyard.toml        # the consumer config foldyard reads
├── compose.yml          # db (postgres) + api (built from ./api)
├── compose.feature.yml  # mode overlay — layered when fakedep=on (walkthrough below)
├── api/                 # the FastAPI app — a uv project (pyproject + uv.lock) + its Dockerfile
├── box.Dockerfile       # a minimal dev-box image (an engine client, git, python/uv)
└── .gitignore           # ignores foldyard's .dev-mode.json mirror + a host-side .venv
```

## What it deliberately does NOT lock down

This fixture covers the **stack**: compose, ports, overlays, the mode system, worktrees. It
declares no `[proxy]` and no `[machine] firewall`, so the box's egress is direct — unrouted,
unobserved, unfiltered. That's one notch below what `foldyard init` writes for a new project
(lima + the [VM firewall](../docs/glossary.md#network) + a proxy
[allowlist](../docs/glossary.md#network) that learns for an hour, then enforces) and two below
[`../example-lima-wall`](../example-lima-wall/).

The omission is structural, not simplification. The egress proxy runs **on the host**, and
declaring `[proxy]` makes every box route through it: `fy box up` then fails unless a CA exists,
and only the supervisor on the host generates one (it starts with `fy up`; `fy host restart`
replaces it) — the supervisor refuses to run anywhere but the host. This example also has to run
where there is no host side: inside a dev box and inside CI's box-like container. The host-tier
tests that need the proxy and the firewalls add `[proxy]` to their own throwaway copy.

So read *this* one for how a consumer wires a stack, and `../example-lima-wall` for the
locked-down setup — VM firewall, enforced allowlist, keyless agent. What `fy init` hands a new
project is closer to the second than the first.

## Run it with foldyard

Copy the example OUT of the foldyard checkout first — a foldyard project must be its own git
repo (the machine mounts the checkout; a `foldyard.toml` nested inside a bigger repo makes
preflight abort `fy up` with this same recipe). CI's host tier does the same (see
[DEVELOPMENT.md](../DEVELOPMENT.md#ci-githubworkflowsfoldyardyml--foldyard-e2eyml)):

```bash
cp -r foldyard/example ~/fy-example && cd ~/fy-example
git init && git add -A && git commit -m init
foldyard up                 # bring the stack up (builds the api image on first run)
curl localhost:8080/        # {"service":"foldyard-example-api","status":"ok"}
curl localhost:8080/db      # {"db":"reachable","version":"PostgreSQL 16…"}  ← DB wiring
foldyard ps                 # the two services
foldyard verify             # the isolation battery (VM boundary; box checks when in-box)
foldyard box up             # the dev box (engine socket + this checkout mounted)
foldyard box shell          # a shell inside it; `foldyard verify` here runs the box checks too
foldyard down               # stop the stack
```

Ports come from `[ports]` in `foldyard.toml` (`API_PORT=8080`, `PG_PORT=5544`); in-container
ports are fixed, so a second worktree stack gets offset host ports and never collides.

**Dependencies in the box.** `api/` is a uv project, and `foldyard.toml`'s `[box]` table wires
the in-tree-artefact pattern every real consumer needs for its `.venv` / `node_modules` /
`target`: `shadow_volumes` masks `api/.venv` with a per-box named volume, `warmup` fills it with
a frozen `uv sync` at box-up, and `env` pins uv's link mode and refuses re-locking. So inside the
box `cd api && uv run uvicorn main:app` runs from the box's own venv on the VM disk, never from a
`.venv` on the shared mount: the box's packages never land on the host tree, the host's never
leak into the box, and `api/Dockerfile` installs from the same `uv.lock`. Details and the
node/pnpm/cargo shapes: [docs/configuration.md](../docs/configuration.md).

## Mode walkthrough — modes, overlays, requirements (zero secrets)

The flow above leaves every switch at rest. This one drives foldyard's **mode system** against
the same stack, using the bundled zero-secret rig declared in `foldyard.toml`
(`[plugins.fakecred]`): `fakecred` stands in for a credential-granting
[switch](../docs/glossary.md#credentials) (a real token service, TTLs, a capability probe),
`fakedep` for a behaviour switch that *consumes* it — the same shape as a real consumer's gcp +
llm pair, with no secret anywhere. Run it on the host (mode changes are host-only, by design):

```bash
foldyard mode                          # the switches at their zero-secret defaults
foldyard mode fakedep=on               # REFUSED — needs the fake credential; the message
                                       # carries the atomic fix ↓
foldyard mode fakecred=on fakedep=on   # accepted together (never trapped mid-escalation)
foldyard up                            # now ALSO layers compose.feature.yml onto -f
curl localhost:8080/                   # …,"feature":"on"} ← the mode, observable
foldyard mode fakecred=off fakedep=off
foldyard up                            # back to rest; "feature":"off"
```

Where each piece of that is declared:

- the **switches** come from the fakecred plugin — loading it is just `[plugins.fakecred]` in
  `foldyard.toml`; real credential plugins (gcp, github, …) contribute theirs the same way.
- the **overlay** is pure config: `[[overlay]] when = { fakedep = "on" }` layers
  `compose.feature.yml` ([../docs/compose-overlays.md](../docs/compose-overlays.md)).
- the **refusal** is a declarative requirement evaluated by the registry. *This* one ships
  in-code on the plugin's switch (intrinsic — fakedep exists to consume fakecred). When your
  own wiring creates the coupling — say your overlay routes LLM traffic through Vertex, so
  `llm=live` only works under `gcp=sa` — declare it beside that overlay as config instead:

  ```toml
  [[require]]
  switch = "llm"
  when = ["record", "live"]
  needs = "gcp"
  accepts = ["sa", "user"]
  reason = "the runtime-SA identity"
  ```

  Schema + the intrinsic-vs-wiring rule: [../docs/configuration.md](../docs/configuration.md).

TTL expiry, the settle cascade, capability DEGRADED/heal, and the `fy clock` fast-forward
are the same rig one level deeper: [../docs/testing-modes.md](../docs/testing-modes.md).

## Where the tests run it

foldyard's machine, box and supervisor lifecycle can't be tested from inside a project's own dev
box: creating a VM or a box there would collide with the live one. So the example is the workload
for two places that can:

- **CI's host tier** (`lima-host-e2e` on Linux, `wsl2-host-e2e` inside WSL2) boots a real
  Lima/QEMU VM from a throwaway copy of this directory and runs `tests/test_*_e2e.py` against it —
  `fy up`, `fy box up`, `fy verify`, the firewalls, worktrees. See
  [DEVELOPMENT.md](../DEVELOPMENT.md#test-tiers).
- **A nested-KVM rig**, for what a runner can't reach (gVisor as the box runtime, arm64, laptop
  timings): [../docs/nested-virt.md](../docs/nested-virt.md).
