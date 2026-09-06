"""gcp-metadata emulator — the two halves of the GCE metadata mechanism the gcp plugin owns.

  minter.py   runs on the Mac (holds the gcloud creds); the supervisor runs it (fy host).
  server.py   an on-stack container (the compose `metadata` profile bind-mounts it in) that
              resolves caller-IP → container label → token via the minter.

They share one protocol (the SA allowlist, the user-escalatable flag, the token contract), so
they live together here rather than split across the foldyard CLI and the compose stack — the
stack reaches server.py purely as a consumer (the bind-mount), it doesn't own it. Both are
stdlib-only scripts run by path (not imported); this package marker just keeps them discoverable.
"""
