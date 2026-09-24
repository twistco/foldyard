---
name: bootstrap-devbox
description: Grow a project's foldyard config from INSIDE the locked-down starter box — discover the stack, deps, and toolchain by observation against the real repo, then codify them into foldyard.toml + a box.Dockerfile. Use when a fresh `foldyard init` box needs a real stack/tools/image, or the box is missing something — e.g. "set up the dev box for this repo", "the box doesn't have <tool>", "wire up my compose stack", "codify my box into a Dockerfile".
---

# Bootstrap a foldyard project from inside its box

`foldyard init` writes a LOCKED-DOWN starter: a dev box with no stack yet, in a VM (Lima by
default) that mounts only this repo, its egress held by the VM firewall to one way out — the
allowlisting proxy on the human's computer — and no push credential. An agent (you) can run
*inside* it. That's the point of this skill: you grow the config from the safe sandbox,
discovering what the project needs by trying the real thing and watching it fail, then
codifying the result. You never touch the host, and a broken experiment is thrown away on
the next `foldyard box up`.

**You are (probably) already inside the box.** The header of `foldyard.toml` lays out the
order — box → agent → stack + features. This skill is step 3 onward: turning "install and
configure until it works" into committed `foldyard.toml` + `box.Dockerfile` changes.

## Ground rules (the isolation is the point — don't break it)

- **Never** put a credential, token, or SSH key in `foldyard.toml`, the Dockerfile, or the
  repo. foldyard is secretless: real auth is injected at the proxy on the human's computer
  (keyless), never baked in. If a step needs a secret, that's a `keyless`/proxy concern on the host, not an image layer.
- **Egress is firewalled + allowlisted.** If a download fails with a proxy `403`/refusal, the
  host isn't on the allowlist — don't try to get around the proxy or the VM firewall. Grants
  live in the allow-store on the human's computer, deliberately NOT in the repo (a
  `[proxy] allow` list in `foldyard.toml` is IGNORED — the box must not widen its own
  allowlist). You CAN recommend: add the host to `[proxy] recommend` in `foldyard.toml` with a
  one-line `why` — after your edit passes the adoption gate, the human is OFFERED each entry
  (at `fy up` / `fy allow sync` / the allowlist pane in `fy tui`), and nothing is granted
  without their per-host yes. For an immediate unblock, ask them to `fy allow add <host>` or
  grant it live in `fy tui`'s Network Log. `fy verify` should keep passing throughout.
- **Keep image layers about TOOLING; keep repo-tracking deps in `[box].warmup`** so a lockfile
  bump doesn't invalidate the image cache.

## The loop

### 1. Confirm where you are

```bash
fy verify            # the isolation holds (rootless, firewalled egress, no push credential)
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

**c) The egress allowlist** → a scaffolded project starts in a *learn window* (`fy init` seeds
`[proxy] enforce = "learn"`): for the first hour nothing is refused, every host that *would* be
is recorded, and then the allowlist enforces by itself. The human reviews and grants what it
recorded with `fy allow learn`, and can open a new window with `fy allow enforce learn` while
you're discovering a dependency's egress. Either way, collect every host the build/test/run
legitimately reaches (the learn record, or blocks in `fy tui`'s Network Log) and COMMIT them as
`[proxy] recommend` entries, each with its `why`:

```toml
[proxy]
recommend = [
  { host = "registry.npmjs.org", why = "npm install (frontend deps)" },
]
```

Recommendations are offers, not grants: every teammate (and the human here) is asked per host
at `fy up` / `fy allow sync` / the allowlist pane in `fy tui`, and can answer yes / session /
never. That's the committed half; the answers stay host-owned. Keyless hosts are allowed
implicitly — never list them, and neither list an agent's install hosts: a declared `[claude]` /
`[codex]` contributes its own (foldyard knows where its installers fetch from).

### 4. Rebuild from the codified config and verify

Prove the Dockerfile + config reproduce the hand-built box. **The rebuild is the human's, not
yours:** `fy box down` from inside the box tears down the box you are running in, and the new
config only takes effect once they approve it on their computer anyway. Commit, then ask them to
run, on their computer:

```bash
fy config diff                              # read your foldyard.toml changes…
fy config adopt                             # …and approve them
fy box down
fy up                                       # the wired stack (skip if there is none)
fy box up                                   # builds box.Dockerfile, starts the new box
```

Then, back in the new box:

```bash
<the project build + test command>          # succeeds with no manual installs
fy verify                                   # isolation battery still passes (incl. the VM firewall)
```

If something's missing, you skipped a recipe line — add it and rebuild. Once a clean rebuild runs
the real workflow with no manual steps, delete the scratch recipe (`rm .foldyard-box-recipe.sh`)
and commit `foldyard.toml` + `box.Dockerfile` (+ `compose.yml`).
