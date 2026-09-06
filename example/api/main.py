"""A minimal FastAPI app for the foldyard example stack.

Two endpoints: a static liveness check, and one that opens a real connection to the
Postgres service so `foldyard up` demonstrably wires a multi-service stack together.
Kept dependency-light on purpose — this is a fixture, not a reference app.
"""

from __future__ import annotations

import os

import psycopg
from fastapi import FastAPI

app = FastAPI(title="foldyard example api")

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:example@db:5432/example"
)


@app.get("/")
def root() -> dict[str, str]:
    # EXAMPLE_FEATURE arrives via the fakedep=on posture overlay (compose.feature.yml) —
    # the README's mode walkthrough makes the posture observable here.
    return {
        "service": "foldyard-example-api",
        "status": "ok",
        "feature": os.environ.get("EXAMPLE_FEATURE", "off"),
    }


@app.get("/db")
def db() -> dict[str, str]:
    """Prove the DB wiring: connect to Postgres and return its version string."""
    with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute("select version()")
        row = cur.fetchone()
    return {"db": "reachable", "version": row[0] if row else "unknown"}
