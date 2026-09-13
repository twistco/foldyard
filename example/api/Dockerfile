# The example api image, built from api/uv.lock — the SAME locked set the dev box's shadowed
# `api/.venv` installs (foldyard.toml `[box].warmup`) and a developer's own `uv sync` does, so
# the three environments can never drift apart.
FROM docker.io/library/python:3.12-slim

# uv as a standalone binary — Astral's recommended image-copy install (as box.Dockerfile does).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/

WORKDIR /app
# The image's own interpreter (no managed-python download), bytecode compiled at build time.
ENV UV_PYTHON=/usr/local/bin/python3 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Lockfile first, so the dependency layer caches across app edits. --frozen: an image build
# must never re-resolve (nor rewrite the lock); --no-dev: runtime deps only.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY main.py .

EXPOSE 8080
CMD ["/app/.venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
