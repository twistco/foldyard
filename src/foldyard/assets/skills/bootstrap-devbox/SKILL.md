---
name: bootstrap-devbox
description: Grow a project's foldyard config from INSIDE the locked-down starter box — discover the stack, deps, and toolchain by observation against the real repo, then codify them into foldyard.toml + a box.Dockerfile. Use when a fresh `foldyard init` box needs a real stack/tools/image, or the box is missing something — e.g. "set up the dev box for this repo", "the box doesn't have <tool>", "wire up my compose stack", "codify my box into a Dockerfile".
---

# Bootstrap a foldyard project from inside its box

`foldyard init` writes a LOCKED-DOWN starter: a stack-less Lima dev box, egress walled to a
Mac allowlisting proxy, only this repo mounted, no push credential — and an agent (you) can
run *inside* it. That's the point of this skill: you grow the config from the safe sandbox,
discovering what the project needs by trying the real thing and watching it fail, then
codifying the result. You never touch the host, and a broken experiment is thrown away on
the next `foldyard box up`.

**You are (probably) already inside the box.** The header of `foldyard.toml` lays out the
order — box → agent → stack + features. This skill is step 3 onward: turning "install and
configure until it works" into committed `foldyard.toml` + `box.Dockerfile` changes.

## Ground rules (the isolation is the point — don't break it)

- **Never** put a credential, token, or SSH key in `foldyard.toml`, the Dockerfile, or the
  repo. foldyard is secretless: real auth is injected at the Mac proxy (keyless), never baked
  in. If a step needs a secret, that's a `keyless`/proxy concern on the host, not an image layer.
- **Egress is walled + allowlisted.** If a download fails with a proxy `403`/refusal, the host
  isn't on the allowlist — don't try to disable the wall. Grants live in the HOST-side
  allow-store, deliberately NOT in the repo (a `[proxy] allow` list in `foldyard.toml` is
  IGNORED — the box must not widen its own wall). You CAN recommend: add the host to
  `[proxy] recommend` in `foldyard.toml` with a one-line `why` — the host OFFERS each entry
  to the operator (at `fy up` / `fy allow sync` / the TUI's wall pane) after your edit passes
  the adoption gate, and nothing is granted without their per-host yes. For an immediate
  unblock, ask the operator to `fy allow add <host>` or grant it live in `fy tui`'s Network
  Log. `fy verify` should keep passing throughout.
- **Keep image layers about TOOLING; keep repo-tracking deps in `[box].warmup`** so a lockfile
  bump doesn't invalidate the image cache.

## The loop

### 1. Confirm where you are

```bash
fy verify            # the cage is intact (rootless, walled egress, no push credential)
cat foldyard.toml    # the starter — note what's still commented
```

You're root inside the box at the repo checkout. The base is Debian
(`debian:trixie-slim`): `apt-get` for system packages, `uv` for Python, install node your way.

### 2. Discover the stack + deps interactively — KEEP A TRANSCRIPT

Do the real thing the project needs — install deps, build, run tests, start the app/services.
When it fails for a missing tool/lib, install it **in the box** and retry. Record every command
that worked, in order, in a scratch file on the mounted repo so it survives:

```bash
echo 'apt-get install -y --no-install-recommends gcc libpq-dev' >> .foldyard-box-recipe.sh
```

If the project has services (a DB, an API), sketch them as a `compose.yml` and bring them up
with `fy up` as you go — that's the stack you'll wire into `foldyard.toml`. Stop when the real
workflow (build + tests + app/services) runs end to end.

### 3. Codify — three edits, all committed

**a) The stack** → uncomment/add in `foldyard.toml` (the header's step 3):

```toml
[project]
app = "api"                  # the service `fy shell` targets + the browsable-URL hint
compose = ["compose.yml"]

[ports]                      # host ports keyed by the env var your compose reads
API_PORT = 8080
```

**b) The image** → translate the recipe into a `box.Dockerfile` layered on the generic base,
grouping installs into single `RUN` layers and cleaning caches:

```dockerfile
# <project> dev box — codified from .foldyard-box-recipe.sh, layered on foldyard's generic base.
FROM debian:trixie-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
RUN apt-get update \
 && apt-get install -y --no-install-recommends git podman ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
# --- project additions (from the interactive session) ---
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
```

then point `foldyard.toml` at it:

```toml
[box]
image = { dockerfile = "box.Dockerfile", tag = "<prefix>-box:latest" }
```

(Use your project's `prefix` from `[project]`.) Leave `uv sync` / `npm install` to `[box].warmup`,
not the image — they track the lockfiles on the mount.

**c) The egress allowlist** → a scaffolded project ENFORCES from the start (`fy init` seeds
`[proxy] default_deny = true`; `fy allow wall off` turns it back to observe-only while you're
still discovering a dependency's egress). Collect every host the build/test/run legitimately
reaches (watch `fy tui`'s Network Log for blocks) and COMMIT them as `[proxy] recommend` entries,
each with its `why`:

```toml
[proxy]
recommend = [
  { host = "registry.npmjs.org", why = "npm install (frontend deps)" },
]
```

Recommendations are offers, not grants: every teammate (and this machine's operator) is asked
per host at `fy up` / `fy allow sync` / the TUI's wall pane, and can answer yes / session /
never. That's the committed half; the answers stay host-owned. Keyless hosts are allowed
implicitly — never list them, and neither list an agent's install hosts: a declared `[claude]` /
`[codex]` contributes its own (foldyard knows where its installers fetch from).

### 4. Rebuild from the codified config and verify

Prove the Dockerfile + config reproduce the hand-built box:

```bash
fy box down
fy box up                                   # now builds box.Dockerfile + brings the wired stack up
fy box shell -c '<the project build + test command>'   # succeeds with no manual installs
fy verify                                   # isolation battery still passes (incl. the wall check)
```

If something's missing, you skipped a recipe line — add it and rebuild. Once a clean rebuild runs
the real workflow with no manual steps, delete the scratch recipe (`rm .foldyard-box-recipe.sh`)
and commit `foldyard.toml` + `box.Dockerfile` (+ `compose.yml`).
