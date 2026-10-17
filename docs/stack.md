# Your compose stack in foldyard

[Quickstart Step 3](./quickstart.md#step-3--bring-your-stack-in) is the short version: point
`[project]` at your compose files, list your ports, run `fy up`. This page covers what you run
into when the stack is a real one: which variables your compose files can use, how worktrees get
a stack of their own, which paths compose may read, and the podman-compose behaviour that differs
from Docker's. Every key is in [configuration.md](./configuration.md); compose files that change
with the mode are [compose-overlays.md](./compose-overlays.md).

## What foldyard needs from your config

```toml
[project]
app = "api"                  # the service `fy shell` opens a bash shell in
app_port = "API_PORT"        # the [ports] key `fy open` opens
compose = ["compose.yml"]    # -f order, relative to the checkout

[ports]
API_PORT = 8080              # keys are the variable names your compose file reads
PG_PORT = 5544
```

In the compose file, the published (host) side of each port comes from that variable, and the
container side stays fixed:

```yaml
services:
  db:
    image: docker.io/library/postgres:16-alpine
    ports:
      - "${PG_PORT:-5544}:5432"
  api:
    build: { context: ./api }
    environment:
      DATABASE_URL: postgresql://postgres:example@db:5432/example
    ports:
      - "${API_PORT:-8080}:8080"
```

That's foldyard's own [example consumer](https://github.com/twistco/foldyard/tree/main/example),
trimmed. Services reach each other by service name (`db:5432`) on the project's network; the box
is attached to that network too, so it reaches them the same way (add those names to
[`[proxy] no_proxy`](./configuration.md#proxy) so the box doesn't send them to the proxy).

Two habits save trouble later:

- **Space your base ports at least 90 apart.** A worktree adds an offset of 1 to 89 to every
  base, so with `WEB_PORT = 3000` and `API_PORT = 3050`, a worktree with offset 50 publishes its
  web app on 3050, which is main's API port.
- **`fy shell` runs `bash`** in the `app` service, so that image needs bash.

`[project]` has a few more keys for stacks that need them: `ensure_dirs` (bind-mount sources to
create before `up`), `external_network` (below) and `compose_env` (below).

## Who runs compose

foldyard runs your engine's own compose command, and echoes each command line
(`+ podman compose -f …`) so you can copy it.

- **On podman (every dev machine):** `podman compose`, with its provider pinned to the
  **podman-compose** bundled in foldyard's install (version 1.6 or later). A
  `PODMAN_COMPOSE_PROVIDER` you set yourself wins.
- **On docker** (a CI runner with no podman, or `[engine] cli = "docker"`): `docker compose`.

If your CI runs `docker compose`, write files that work under both — the
[differences below](#podman-compose-differences-that-bite) are the ones that bite.

On podman, foldyard builds your images itself with `podman build`, then starts the stack with
`up --no-build`. Builds go through the proxy and the allowlist like everything else
([networking.md](./networking.md#image-builds-trusted-still-behind-the-allowlist)).
`fy build [svc]` builds without starting.

## Variables your compose files can use

| Variable | Value |
| --- | --- |
| each `[ports]` key | the base port, plus the worktree's offset |
| `PODMAN_PROJECT`, `COMPOSE_PROJECT_NAME` | the compose project: `[project] prefix`, plus `-<worktree>` in a worktree |
| `WORKTREE` | the worktree's name; empty in the main checkout |
| `FOLDYARD_CHECKOUT` | the checkout this stack belongs to (the worktree's, or main) |
| `MAIN_REPO` | the main checkout, the same for every worktree |

Variables the current mode sets (from the plugins you use) are there too, and so is the project
`.env` beside your first compose file, which compose reads itself.

**Nothing else from your shell reaches the compose client.** It gets only what it needs to run
(`PATH`, `HOME`, locale, the engine's and compose's own `COMPOSE_*`/`PODMAN_*` settings) and the
names you list in `[project] compose_env`. Any name in the secrets file foldyard keeps on your
computer (`host.env`) is stripped, unless you list it in `compose_env`. The compose files can be
edited from the box, and a `${NAME}` in one hands that value to a container, so each
`compose_env` name widens what the box can read; `fy config widenings` lists them.

`MAIN_REPO` is how every worktree's stack shares one directory, say a folder of database dumps
fetched once:

```yaml
    volumes:
      - ${MAIN_REPO}/dumps:/dumps:ro
```

A `just` recipe that drives compose itself can start with `eval "$(foldyard shellenv)"`: it
exports the same variables, sets `ENGINE` (`podman` or `docker`), and defines a `COMPOSE` array
holding the compose command with every `-f`.

## Worktrees get their own stack

`fy worktree add <name>` makes a checkout for a branch. Run `fy up` from inside it (or
`WORKTREE=<name> fy up` from anywhere) and it gets a stack of its own, in the same VM:

- **Its own project**, `<prefix>-<name>`: its own containers, network and named volumes, so its
  database is separate from main's.
- **Offset ports.** The offset is a hash of the name, from 1 to 89. Two names can land on the
  same offset; pin one (1 to 89) in the main checkout's `foldyard.local.toml` under
  `[worktree-offsets]` and adopt it ([configuration.md](./configuration.md#ports)), or set
  `WT_OFFSET`.
- **Its own compose files.** The `-f` paths, and the relative paths inside those files (build
  contexts, bind mounts), resolve against the worktree. A compose edit in a worktree changes
  that worktree's stack at its next `fy up`, and leaves main's alone until the change is merged.

What the stacks share: the VM, its image store (so a worktree's first build reuses main's layer
cache), and any named volume you give a fixed `name:`. Compose otherwise prefixes a volume with
the project, which is what keeps each stack's data apart:

```yaml
volumes:
  pgdata: {}                      # one per checkout: <project>_pgdata
  pnpm-store:
    name: myapp-pnpm-store        # one for every checkout
```

A `[box] caches` entry mounts its volume under exactly the name you give it, so a stack service
that uses the same fixed name shares that cache with every box.

`fy nuke` in a worktree removes its containers and its own volumes and keeps the shared ones; in
the main checkout it removes the shared ones too. `fy worktree remove <name>` tears down the
worktree's stack and box before deleting the checkout.

## Which paths compose may read

When you run `fy up` on your computer, the compose client runs there too, and it opens some files
by path: `env_file` and `label_file`, build contexts, Dockerfiles, `additional_contexts` and
`ssh` keys, `include` and `extends` targets, and `secrets` and `configs` files. **Each must be
inside the checkout** (or, from a worktree, the main checkout). Otherwise `fy up` and `fy build`
refuse, naming each path, because the compose files are editable from the box and the client
would hand those files' contents to a container the box can see. A remote build context (a git
or https URL) is refused too: the client would fetch it from your computer. Paths in an
`include` or `extends` target must be literal, with no `${…}`, since only the top-level files are
checked after compose fills in variables.

**Bind mounts are different:** the engine resolves them inside the VM, which holds only your
repo and its worktrees (at the same paths as on your computer). A bind mount of `~/.aws` or
`/etc/hosts` from your computer has nothing there to find. Put what a service needs in the repo,
or give it a credential through a mode ([below](#keep-secrets-out-of-compose)).

Run from inside the box, the compose client runs in the VM, where none of your files are, so
these checks don't apply.

## podman-compose differences that bite

As of podman-compose 1.6.0:

- **No `rm` subcommand.** `compose rm -sf <svc>` exits 2 ("invalid choice: 'rm'"). Under a
  `>/dev/null 2>&1 || true` that failure is invisible and the container survives. Remove it
  through the engine: `"$ENGINE" rm -f <container>`.
- **`ps` takes no `-a`** ("unrecognized arguments: -a"), and doesn't need it: podman-compose's
  `ps` already lists stopped containers. In a script that runs on both, filter the engine
  instead: `"$ENGINE" ps -a --filter "label=com.docker.compose.project=$PODMAN_PROJECT"`.
- **Default container names differ**: `<project>_<svc>_1` here, `<project>-<svc>-1` under
  docker compose. If scripts name containers, set `container_name: ${PODMAN_PROJECT}-<svc>`.
- **`logs -n 50` means `--names`**, not a line count, so the 50 is taken for a service name. Use
  `--tail 50`.
- **Blank a variable with `''`, not `!reset`.** `!reset` removes a key that an earlier file set,
  but on a key no earlier file sets it misbehaves: the tag is left in the rendered config, or a
  value a later file sets is dropped. It also only removes an `environment:` entry, so a value
  the service gets from its `env_file` stays. `FOO: ''` behaves the same under both providers
  and overrides `env_file`; it sets an empty string, so your app must treat empty as unset.
- **`nocopy` works only in the long volume syntax.** The short form `vol:/path:nocopy` makes
  podman-compose fail with `unknown mount option nocopy`, and the whole stack stays down. The
  long form (`volume: { nocopy: true }`) is ignored by podman-compose and honoured by docker
  compose.
- **Editing an `env_file`'s contents doesn't count as a config change.** podman-compose's change
  check covers the file's path, not what's in it, so `fy up` leaves the container running with
  the old values. Recreate it: `fy down`, then `fy up` (named volumes are kept).

## Named volumes

### Don't leave image content at a shared volume's mount point

When a container mounts a named volume that is still empty, the engine first copies in whatever
the image has at that path. With several services sharing one volume, all of them can see it
empty at once and race to create the same directories, and `fy up` fails with an error like
`failed to mkdir …/_data/<dir>: file exists`. It needs a volume that doesn't exist yet, so it
shows up on a CI runner, after a fresh `fy worktree add`, or after `fy nuke`, and even then only
sometimes. The fix is in the image: remove what it leaves at the mount point (say,
`RUN rm -rf /root/.cache/uv` for a shared uv cache). `nocopy` in the long syntax helps only
under docker compose (above).

### Dependency directories go stale

A named volume over `node_modules` or `.venv` keeps installed dependencies off the shared mount,
but it holds whatever was installed when it was filled. Switch to a branch whose lockfile moved
and the old dependencies are still there, and `git status` can't show it, since the volume isn't
in the tree. The symptom is an API the new version has failing in that container, while CI
passes. Have the service install from the lockfile, frozen, when it starts, and restart it after
a dependency change; for the box's own `[box] shadow_volumes`, re-run the frozen install in the
box ([configuration.md](./configuration.md#box)).

## Recreating, and output that looks wrong but isn't

- **A running container keeps the command and environment it was created with**, so a
  `restart` doesn't pick up a compose change. `fy up` does: it recreates each service whose
  config changed, and its running dependents. A mode change does it for you
  ([modes.md](./modes.md#modes-update-the-running-stack)). `fy state` names any container whose
  config no longer matches.
- **`fy down` ends with an error that the network is still in use.** That's expected: the box is
  attached to the project's network, so compose can't remove it. The containers are gone. To
  quiet it, set `[project] external_network = true` and declare the network in your compose file
  as `networks: { default: { external: true, name: "${COMPOSE_PROJECT_NAME}_default" } }`;
  foldyard then creates and removes the network itself.
- **With the VM firewall on**, podman gives every container proxy settings whose `NO_PROXY`
  covers only loopback and your computer. A service that calls a sibling by name with an HTTP
  client that honours them needs its own `NO_PROXY`
  ([networking.md](./networking.md#node-and-npm)).

## Keep secrets out of compose

Don't put real secrets in your compose file's `environment:` or in an `env_file`, even a
gitignored one. A value there sits in the container for its whole life, and anything holding the
engine socket, the box included, reads it with `inspect`. Give the service a dummy value and let
a switch inject the real one at the proxy on your computer, only while the mode is on: an
`[[inject]]` entry for a header-borne token ([configuration.md](./configuration.md#inject)), or a
plugin's switch for more ([modes.md](./modes.md)). The full credential model is in
[security.md](./security.md#the-credential-story-nothing-at-rest-injected-in-flight).
