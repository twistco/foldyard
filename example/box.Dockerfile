# Minimal foldyard dev-box image for the example consumer.
#
# foldyard's box contract (PLAN §7.4) is deliberately tiny — the image must provide:
#   • an engine client that speaks the mounted socket  → podman (installed below)
#   • git                                              → installed below
#   • uv                                               → so `foldyard box up` can
#       `uv tool install foldyard` at box-up. uv provisions its OWN managed Python, so the
#       image needs no system python/pip (uv-first, not python-first).
# Optional (only if you drive the stack FROM INSIDE the box, the agent-autonomy persona):
# a compose provider. foldyard injects the rest at box-up — the foldyard CLI, the socket,
# env (CONTAINER_HOST), and (with a credential plugin) the proxy CA + mode mirror.
#
# A real consumer points [box].image at their own toolchain image; this is the smallest
# thing that satisfies the contract for the example.
FROM debian:trixie-slim

# uv as a standalone binary — Astral's recommended image-copy install (pinnable, no curl).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# podman = the engine CLIENT for the mounted socket (the box never runs a nested engine).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git podman ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
