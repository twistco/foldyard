# GCE metadata-server emulator — per-container SA, prod-parity, no token files

Gives each container on the stack a **short-lived token for its OWN service account**,
exactly the way production does it: the app queries the GCE metadata server for its
attached SA's token. No key, no token file, no per-app dev hack — the same code path as
Cloud Run. It replaces every "put a service-account JSON somewhere and mount it" pattern.

## How it works

```text
 container (app / dev box)                on-machine                     Mac
 ┌──────────────────────────┐     ┌───────────────────────────┐   ┌────────────────────┐
 │ google-auth / gcloud  →   │     │ metadata-emulator (server  │   │ minter.py          │
 │ GET …/service-accounts/   │────▶│  .py): source-IP → caller  │──▶│ (run by `fy host`):│
 │   default/token           │ net │  container → its           │   │  allowlist +       │
 │ GCE_METADATA_HOST set     │     │  gcp.serviceAccount label  │   │  gcloud impersonate│
 │   → believes it's on GCE  │◀────│  → asks the Mac to mint    │◀──│  gcloud creds HERE │
 └──────────────────────────┘     └───────────────────────────┘   └────────────────────┘
```

- **`server.py`** runs as a container ON the stack network (the consumer's compose
  `metadata` profile). It resolves the caller by **source IP** → container → the
  container's `gcp.serviceAccount` label, then fetches a token from the Mac minter,
  caching per-SA. Source-IP resolution _must_ live on-network: a container→Mac request is
  NAT'd through the VM's network stack, so the Mac only ever sees the gateway IP.
- **`minter.py`** runs on the **Mac**, where the gcloud credentials live. It validates the
  requested SA against `GCP_SA_ALLOWLIST` and mints a ≤1h impersonated token. Long-lived
  credentials never enter the VM; only the short token crosses the boundary.
- Each container declares its identity with a **`gcp.serviceAccount` label**. Resolution
  goes through the trusted daemon, not the request, so a container cannot claim another's
  SA. The dev box additionally carries `gcp.userEscalatable`, which is what makes the
  TTL-bound `gcp=user` emergency rung reachable for the box and no one else.

## Configuring it

Everything project-specific comes from `[plugins.gcp-metadata]` in `foldyard.toml`; the
package hardcodes no project, SA or allowlist. See `docs/configuration.md` for the full
key list, and `docs/modes.md` for what each rung grants.

```toml
[plugins.gcp-metadata]
project   = "acme-staging"            # REQUIRED — the emulator refuses to start without it
sa_labels = { app = "app-runtime", box = "log-reader", worker = "worker-runtime" }
```

`sa_labels` maps a ROLE to the local-part of an SA email; the emulator and the minter both
derive `<label>@<project>.iam.gserviceaccount.com` from it. `app` and `box` are the two
roles foldyard knows by name (the app containers and the dev box); every other role is a
data-plane service that resolves its own identity.

The rung decides which of those SAs the minter will allowlist:

| rung | allowlisted |
| --- | --- |
| `gcp=off` | nothing — the minter isn't running, the emulator returns "no token" |
| `gcp=logs` | the `box` SA only (read-only logging for the dev box) |
| `gcp=sa` | + the `app` SA and every other declared role |
| `gcp=user` | + your own identity, for the dev box only, TTL-bound |

## Prerequisites on the cloud side

1. **The SAs exist**, and each has exactly the roles its rung is meant to grant — the
   `box` SA in particular should be read-only, since a dev box holding it is the normal
   resting state under `gcp=logs`.
2. **The Mac identity holds `roles/iam.serviceAccountTokenCreator` on every allowlisted
   SA.** That grant is the whole trust chain: minting is impersonation, so an operator who
   doesn't have it gets a clear gcloud error rather than a token. If your organisation
   gates that behind just-in-time elevation, `fy doctor` reports the live grant and its
   remaining window.

## Running it

```bash
fy mode gcp=logs     # or gcp=sa / gcp=user ttl=30m   (Mac only — posture is host-side)
fy host              # runs the minter; credentials stay on the Mac
fy up                # the compose `metadata` profile is derived from the mode
fy box up            # GCE_METADATA_HOST is derived from the mode
```

`fy mode` is the front door: the profile, the allowlist and the box env all derive from
the rung, so there is nothing to keep in sync by hand. Explicit env still wins if you need
to drive the pieces individually while debugging.

## What the app has to change

Nothing, if it uses ADC. Google's client libraries honour `GCE_METADATA_HOST`, and the
token the emulator returns **is** the runtime SA — no second impersonation, exactly like
Cloud Run. What a consumer usually does need is to stop overriding ADC for the offline
loop: unset any explicit credentials file and any emulator hosts (GCS, BigQuery) on the
rungs that talk to real infrastructure. Compose `environment:` values can't be unset from
an env-file, so that belongs in a posture overlay (`[[overlay]]`, keyed on the rung) —
see `docs/compose-overlays.md`.

Signed URLs work: the emulator serves `/service-accounts/default/email`, and the SA holds
`signBlob` on itself.

## Security

- gcloud credentials never leave the Mac; only ≤1h impersonated tokens cross the boundary.
- The minter refuses any SA not in `GCP_SA_ALLOWLIST`; `MINTER_SECRET` guards the port.
- The emulator reads the engine socket only to map caller IP → container → label.
- Per-caller identity comes from the label resolved via the daemon, never from the request.
- The emulator has **no default project**: an unset `GCP_PROJECT` is a startup failure, not
  a fallback to somebody else's project id.
