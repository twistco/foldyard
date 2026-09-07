# ADR-0010 — Engine = podman everywhere via CONTAINER_HOST; docker only as fallback

- **Status:** Accepted (2026-06, "docker vs podman" investigation; buildah build path added in
  PR #36) — implemented
- **Sources:** HANDOVER.md ("docker vs podman in the dev box", key decision 2),
  PLAN.md §7.1 (engine note), src/foldyard/stack.py engine/context resolution

## Context

foldyard's substrate is a rootless **podman machine** (ADR-0001) — the machine lifecycle has no
docker equivalent, so podman is non-negotiable at that layer. But the dev box historically drove
the stack with the `docker` CLI: the machine's docker-compat socket is bind-mounted into the box
at `/var/run/docker.sock`, and `docker version` there reports a podman server. Meanwhile plain
`podman` *inside* the box defaults to broken local (nested) mode — the box's hardening blocks a
nested engine (read-only `/proc/sys`, no `/dev/fuse`; see [ADR-0017](./0017-nested-virt-validation-strategy.md)). So
the question was which CLI foldyard itself should shell, and how to make one answer hold on the
Mac, in the box, and in CI.

The investigation found the missing piece: `CONTAINER_HOST=unix:///var/run/docker.sock podman …`
drives the machine socket natively (podman's auto remote mode), and `podman compose` works via
the docker-compose provider. Empirically validated in-box: `podman compose ps` sees the same
stack docker created; `podman run --privileged --pid=host` is refused; `podman info` reports
rootless.

## Decision

**podman is foldyard's engine everywhere, made true by exporting the machine socket under both
names.** Concretely:

- `config.engine()` (`src/foldyard/config.py`) resolves `FOLDYARD_ENGINE` env → `[engine].cli` →
  **podman if installed, else docker**. All engine calls go through the resolved CLI.
- `stack._context()` (`src/foldyard/stack.py`) exports the machine's socket as **both**
  `DOCKER_HOST` and `CONTAINER_HOST`: podman reads `CONTAINER_HOST`, docker reads `DOCKER_HOST`.
  That lets plain `podman` reach the engine everywhere — including the box, where local podman
  would otherwise be broken — while any remaining docker caller keeps working.
- **podman-compose is Foldyard's bundled provider; docker stays the CI fallback, not the engine.**
  Foldyard installs `podman-compose` in its tool venv and exports its absolute path through
  `PODMAN_COMPOSE_PROVIDER`, avoiding Podman's default preference for a global docker-compose.
  The box image keeps a docker CLI as a compat client; on hosts with no podman at all
  (GitHub-hosted runners), `engine()` degrades
  to docker and `docker compose` runs the same stack (ADR-0017 relies on this for the CI e2e).
- **Builds are concurrent native buildah jobs on the podman engine** (added in PR #36,
  `stack._podman_build()`): `podman compose --build` shells out to the docker-compose provider
  with `DOCKER_BUILDKIT=0` hard-set, falling back to the classic builder — which rejects compose
  `additional_contexts`. So `fy up` resolves the build graph from plain `compose config`
  (parsing podman-compose's normalized YAML output — no `--format json`),
  builds each independent service concurrently with `podman build` (buildah handles `--build-context`, cache mounts,
  multi-stage targets natively — no BuildKit/buildx sidecar, no Docker dependency), tags images
  with podman-compose's exact `<project>_<service>` names, then runs `up --no-build` so it reuses
  them. On the docker engine, compose's default BuildKit builder handles additional contexts, so
  that path never enters `_podman_build`.
- **The box ships a podman *remote* client pinned near the machine's version.** The consumer box
  image installs `podman-remote` and symlinks it to `podman` — that symlink is what flips the
  box's `engine()` onto podman. The version must track the machine's (~5.8): the remote
  wire-format for `--build-context` changed between 5.4 and 5.8, and a stale client fails with
  "invalid additional build context format".

## Consequences

- One engine story across Mac, box, and worktrees: foldyard echoes `"$ENGINE" …` commands the
  user can copy-paste, and the golden tests (ADR-0017 tier 2) assert podman command sequences
  deterministically (`conftest` pins `FOLDYARD_ENGINE=podman`).
- `CONTAINER_HOST` is load-bearing. Dropping it, or making `engine()` "prefer docker" again,
  breaks the verified in-box `up`/`ps`/`verify` — this is on the HANDOVER don't-break list.
- Engine-schema differences are handled where they exist rather than papered over: `verify`'s
  rootless probe tries docker's `.SecurityOptions` *and* podman's `.Host.Security.Rootless`; the
  box-image build adds `--format docker` only under podman (so Dockerfile `SHELL [...]` is
  honoured).
- "Is there a podman binary?" is useless as a host test — the box has one. Host-vs-box detection
  is `config.in_box()` (IN_DEVBOX / preset docker-compat DOCKER_HOST), never `which("podman")`.
- The docker CLI remains in the box image as a compatibility client for consumer-owned workflows,
  but Foldyard's own `podman compose` calls select its bundled podman-compose provider.
- Machine backends (ADR-0011) keep this decision intact: every backend hands back a libpod
  socket, and the same dual-name export makes both CLIs work against whichever backend is
  active.

## Rejected alternatives

- **docker-first `engine()`** (the pre-investigation state): made the box's engine identity a
  lie (a docker CLI talking to podman), forked behaviour between Mac and box, and left podman's
  native builder — the only one that can do compose `additional_contexts` here — unused.
- **BuildKit/buildx or a `moby/buildkit` sidecar for the additional-contexts problem:** adds a
  Docker dependency and a second builder daemon where buildah already does everything needed,
  natively, over the existing socket.
- **A nested engine in the box** so plain podman "just works" locally: blocked by the box's own
  hardening, and undesirable — the box driving the *machine's* engine over the socket is the
  isolation model ([ADR-0017](./0017-nested-virt-validation-strategy.md)).
