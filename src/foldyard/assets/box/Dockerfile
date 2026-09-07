# foldyard's GENERIC dev-box image — the package's built-in default box.
#
# Used when a consumer's foldyard.toml declares no [box].image, so any project can
# `foldyard init --box-only && foldyard box up` with NO Dockerfile to author. It is the
# `example/box.Dockerfile` generalised; a real consumer still points [box].image at their
# own toolchain image when they need one.
#
# foldyard's box contract (ADR-0014) is deliberately tiny — the image must provide:
#   • an engine client that speaks the mounted socket  → podman (installed below)
#   • git                                              → installed below
#   • uv                                               → so `foldyard box up` can
#       `uv tool install foldyard` at box-up. uv provisions its OWN managed Python, so the
#       image needs no system python/pip (uv-first, not python-first).
#
# The build CONTEXT is THIS packaged dir (not the consumer repo): the generic box must not
# depend on any repo contents. foldyard injects the rest at box-up — the foldyard CLI, the
# socket, env (CONTAINER_HOST), and (with a credential plugin) the proxy CA + mode mirror.
#
# Debian, deliberately (was quay.io/podman/stable, which is Fedora — chosen for its free
# podman, not its distro, and the distro is what consumers actually live with):
#   • apt pulls from ONE stable host (deb.debian.org) — an egress allowlist can name it,
#     where dnf's metalink mirror system redirects to arbitrary hosts no allowlist can;
#   • Playwright (and most dev tooling) treats Debian/Ubuntu as first-class —
#     `playwright install-deps` works here and does not on Fedora;
#   • `[[box.tools]]` recipes match the apt one-liners most docs hand out.
# The engine CLIENT the contract needs is one apt install away (below) — the box only ever
# talks to the MOUNTED socket; it never runs a nested engine.
FROM debian:trixie-slim

# uv as a standalone binary — Astral's recommended image-copy install (pinnable, no curl).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# podman = the engine client for the mounted socket (CONTAINER_HOST is injected at box-up);
# ca-certificates so git/uv/curl verify TLS; curl for the static-binary bootstrap steps.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git podman ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
