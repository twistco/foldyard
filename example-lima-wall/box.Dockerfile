# Dev-box image for the locked-down example — identical to ../example/box.Dockerfile.
# The wall is a MACHINE property (nftables in the VM); the box image is unchanged by it.
# See ../example/box.Dockerfile for the box-contract rationale.
#
# Debian matters MORE here than in the plain example: apt pulls from one stable host
# (deb.debian.org) that `[proxy] recommend` can name, where dnf's metalink mirror system
# redirects to arbitrary hosts no allowlist can — i.e. a Fedora base and an enforced
# allowlist can't both be true.
FROM debian:trixie-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN apt-get update \
 && apt-get install -y --no-install-recommends git podman ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
