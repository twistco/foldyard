# GCE metadata-server emulator

The two halves of the built-in `gcp` plugin's credential path. What it does for a user, how to
set it up and how to check it: `fy docs gcp` ([docs/gcp.md](../../../../docs/gcp.md)).

- **`server.py`** — the emulator. Runs in a container on the stack network (the consumer's
  compose `metadata` profile, server staged into `.devbox-foldyard/` by `GcpPlugin.stage_assets`).
  Resolves the caller by source IP → container → `gcp.serviceAccount` label, then asks the
  minter. Source-IP resolution has to live on the network: a container→host request is NAT'd, so
  the host only ever sees the gateway. Stdlib only, so it runs on a stock python image.
- **`minter.py`** — the token service, on the host. Checks the SA against `GCP_SA_ALLOWLIST` and
  mints with `gcloud auth print-access-token`. The supervisor runs it by path from a per-project
  snapshot (`gcp._launch_minter_path`), so it never imports foldyard; the liveness marker it
  returns is duplicated in `gcp._MINTER_MARKER`, and a round-trip test holds the two together.

Each script's env contract is in its module docstring. Tests: `tests/test_minter.py`,
`tests/test_gcp_metadata_server.py`, and the live `tests/test_gcp_metadata_box_e2e.py`.
