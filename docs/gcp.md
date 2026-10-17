# Google Cloud access — the metadata emulator

On Google Cloud, code finds its credentials by asking the **metadata server**: a well-known
address on every VM and Cloud Run service that answers "here is a token for the service account
you run as". Google's client libraries and `gcloud` do this by themselves; it's part of
**Application Default Credentials** (ADC), the lookup order they all follow.

foldyard's built-in `gcp` plugin gives your stack the same thing. A small **metadata emulator**
runs on the stack's network. When code in the box or in one of your services asks it for a token,
it works out which container is asking, and a token service on your computer makes a short-lived
token for that container's identity. No key file, no token in the repo, no dev-only code path:
the code that runs on Cloud Run runs here unchanged.

Like every credential, it's off until you switch it on ([modes.md](./modes.md)).

## How it works

```text
 a container (box or service)          in the VM                         your computer
 ┌─────────────────────────┐    ┌─────────────────────────────┐    ┌──────────────────────┐
 │ client library / gcloud │    │ metadata-emulator           │    │ token service        │
 │ GET …/default/token  ───┼───▶│ caller's IP → its container │───▶│ checks the SA list,  │
 │ (GCE_METADATA_HOST says │    │ → its gcp.serviceAccount    │    │ runs gcloud as you   │
 │  where the server is)   │◀───┼─ label → asks for a token   │◀───┼─ → short-lived token │
 └─────────────────────────┘    └─────────────────────────────┘    └──────────────────────┘
```

- **Each container's identity is a label.** A container carries `gcp.serviceAccount=<email>`,
  the service account it should act as. (A **service account**, or SA, is a Google identity for
  software rather than a person.) The emulator looks the label up through the engine, by the
  caller's IP address, so a request can't claim someone else's identity. A container without the
  label gets no token.
- **The token service runs on your computer**, started by the supervisor. It makes tokens by
  **impersonating** the service account with your own `gcloud` login: Google lets you mint a
  token for an SA you've been granted that right on. It only does so for the SAs the current
  level allows. Your login never enters the VM; only the short-lived token does.
- **Tokens are short-lived.** A token lasts an hour at most, and clients are told to fetch a new
  one every minute, so a mode change reaches running code within about a minute.

## The `gcp` switch

| level | the box gets | your services get |
| --- | --- | --- |
| `off` (default) | no token | no token |
| `logs` | the `box` SA (meant to be read-only, e.g. reading Cloud Logging) | no token |
| `sa` | the `box` SA | each service its own SA, from its label |
| `user` | **your own** Google identity — emergency, time-limited | the same as `sa` |

`user` is an **emergency level**: it acts as you, so it always has a TTL (one hour if you give
no `ttl=`, eight at most). Only the box can get your token: it carries a second label,
`gcp.userEscalatable=1`, which foldyard sets on the box and nothing else. Your services keep
their own SAs, exactly as under `sa`. When the TTL runs out, the switch goes back to `off`, not
to `sa`, and any switch that needs `gcp` drops to its default with it.

**Lowering the level stops new tokens; it doesn't cancel old ones.** Within about a minute of
`fy mode gcp=off` nothing in the VM can get a token. A token that was already handed out stays
valid at Google until it expires (an hour at most).

**Each worktree has its own token service** on its own port, following that worktree's mode. A
worktree at `gcp=off` gets nothing, even while another runs at `sa`.

**`gcp` is identity only.** It decides which Google identity is reachable, not how your app
behaves. Whether the app talks to real Cloud Storage or an emulator, real Vertex or recorded
answers, is your own switches' job, usually an `[[overlay]]` per behaviour that requires `gcp=sa`
(see `[[require]]` in [configuration](./configuration.md#require)). So a fully offline test run
and a stack browsing real data can share a box.

The plugin has one such switch built in: **`storage`** (`local` by default, or `staging`). It
shows up only when an `[[overlay]]` in your config uses it (`when = { storage = "staging" }`);
your overlay does the actual swap, and a `[[require]]` row should tie it to `gcp=sa`. At
`storage=staging` foldyard also turns on a `cloudsql` compose profile, for a Cloud SQL proxy
service if your compose file defines one.

## Setting it up

### On Google Cloud

1. **Create the service accounts**, each with only the roles its level should grant. The `box`
   SA is what the box holds under `gcp=logs`, the level you'd leave on day to day, so keep it
   read-only.
2. **Give yourself `roles/iam.serviceAccountTokenCreator` on each of them.** That grant is the
   whole trust chain: without it you get a clear `gcloud` error instead of a token. If your
   organisation hands it out just in time (Google's Privileged Access Manager), `fy doctor`
   shows your active grant and how long it has left. It looks for the entitlement
   `pam-entitlement` in your project; set `PAM_ENTITLEMENT` to its full name if yours differs.
3. **For signed URLs** (Cloud Storage), the signing SA must be allowed to sign as itself
   (`iam.serviceAccounts.signBlob`, e.g. Token Creator on itself). The emulator serves the SA's
   email, which is what the client libraries need to sign.

### On your computer

Install the `gcloud` CLI and log in with `gcloud auth login`: the token service runs `gcloud` as
you. Then check:

```bash
fy doctor          # gcloud installed, an active account, application-default credentials, PAM
fy doctor --deep   # also impersonates the box SA and the app SA for real
```

If the application-default row fails, `gcloud auth application-default login` fixes it.

### In `foldyard.toml`

```toml
[plugins.gcp-metadata]
project = "my-gcp-project"
sa_labels = { app = "app-runtime", box = "log-reader", worker = "worker-runtime" }
```

Declaring the table loads the plugin; the `gcp` switch appears once `project` is set. Each
`sa_labels` entry maps a role to the first part of an SA email, giving
`<label>@<project>.iam.gserviceaccount.com`. `box` is the box's SA and `app` your app's; every
other role is one more service's own SA. Defaults and env overrides are under
[`[plugins.<name>]`](./configuration.md#pluginsname).

Your computer runs the **adopted config**, so adopt the change (`fy config adopt`, or answer
the prompt at the next `fy up`). The box's wiring is set when the box is created, so a box that
existed before the table needs one `fy box down` then `fy box up`. After that, changing the
level never needs a new box.

### In your compose file

**The emulator service.** foldyard ships the emulator's code but not its service, so your compose
file defines it. The service must be called `metadata-emulator` and sit in the `metadata`
profile, which foldyard turns on at every level but `off`:

```yaml
services:
  metadata-emulator:
    profiles: ["metadata"]
    hostname: metadata-emulator
    image: python:3.12-slim              # any image with python3; the server needs no packages
    command: ["python", "/srv/server.py"]
    security_opt: ["label=disable"]      # lets it read the engine socket on an SELinux guest
    environment:
      GCP_PROJECT: my-gcp-project        # required; it refuses to start without one
      GCP_MINTER_URL: ${GCP_MINTER_URL}  # set by foldyard: this worktree's token service
    volumes:
      - ${FOLDYARD_CHECKOUT}/.devbox-foldyard/server.py:/srv/server.py:ro
      - /run/user/1000/podman/podman.sock:/var/run/docker.sock
```

- `fy up` copies the server into the gitignored `.devbox-foldyard/` in your checkout, because the
  VM mounts only your repo. If you set `[project] dev_vm_dir`, it lands under that directory.
- The last line mounts the VM's engine socket, so the emulator can look up callers. Use the path
  the box mounts: on the lima backend `/run/user/<uid>/podman/podman.sock`, where `<uid>` is
  your user id on your computer (`id -u`), or your `[box] sock_in_vm` if you set one.

**Your services.** Give each service that needs Google its label and point its client libraries
at the emulator, in an overlay that applies at `sa` and `user`:

```toml
[[overlay]]
file = "compose.gcp.yml"
when = { gcp = ["sa", "user"] }
```

```yaml
# compose.gcp.yml
services:
  api:
    labels:
      gcp.serviceAccount: app-runtime@my-gcp-project.iam.gserviceaccount.com
    environment:
      GCE_METADATA_HOST: metadata-emulator:80
      GCE_METADATA_IP: metadata-emulator:80      # Python google-auth's "am I on GCE?" check
      METADATA_SERVER_DETECTION: assume-present  # Node google-auth-library: skip that check
```

- **The label must name an SA the level allows**: the `app` SA or another `sa_labels` role.
  Never give a service `gcp.userEscalatable`: that is what lets the box receive your own token.
- **`metadata.google.internal` doesn't resolve in the VM**, and `169.254.169.254` leads nowhere.
  Clients find the emulator only through those variables. `gcloud` reads another one,
  `GCE_METADATA_ROOT`, so set it too in a service that runs `gcloud`. Code that hardcodes the
  usual address won't find it.
- **Remove whatever would win over ADC**: a `GOOGLE_APPLICATION_CREDENTIALS` file, or emulator
  hosts for Cloud Storage or BigQuery on the levels meant to reach real services. Blank them
  with `''` in the overlay ([compose-overlays.md](./compose-overlays.md)).
- Listing `user` beside `sa` keeps the overlay set identical across the two, so escalating
  doesn't recreate your stack.

The box needs none of this: foldyard gives it its label, `gcp.userEscalatable=1`,
`GCE_METADATA_HOST`, `GCE_METADATA_IP` and `GCE_METADATA_ROOT`, and leaves `metadata-emulator`
off the proxy.

## Turning it on

```bash
fy mode gcp=logs            # on your computer
fy mode gcp=sa
fy mode gcp=user ttl=30m    # emergency: your own identity in the box
fy mode gcp=off
```

That's all. Within a couple of seconds the supervisor starts or stops the token service, and a
running stack is brought in line: the emulator started or stopped, overlays applied. The
emulator is started even when the rest of the stack is down, so the box can get tokens from a
fresh worktree. `fy state` shows any layer that hasn't caught up, and `fy tui` has a
**GCP Tokens** tab listing every token request: which identity, and whether it was granted.
Tokens themselves are never logged.

## Check it works

From the box:

```bash
curl -s -H 'Metadata-Flavor: Google' \
  "http://$GCE_METADATA_HOST/computeMetadata/v1/instance/service-accounts/default/email"
curl -s -o /dev/null -w '%{http_code}\n' -H 'Metadata-Flavor: Google' \
  "http://$GCE_METADATA_HOST/computeMetadata/v1/instance/service-accounts/default/token"
```

The first prints the box SA; under `gcp=user` it still does, though the token is yours. The
second prints `200`. Anything else is a 404: drop `-o /dev/null -w …` to read the reason, which
is the token service's own message. Then try a real call, e.g. `gcloud logging read --project
my-gcp-project --limit 1` if your box image has `gcloud`.

**From a service**, the same works as that service's SA. To read a secret from Secret Manager,
for example (Python standing in for `jq`, which slim images often lack):

```bash
TOKEN=$(curl -s -H 'Metadata-Flavor: Google' \
  "http://$GCE_METADATA_HOST/computeMetadata/v1/instance/service-accounts/default/token" |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')
curl -s -H "Authorization: Bearer $TOKEN" "https://secretmanager.googleapis.com/v1/\
projects/my-gcp-project/secrets/my-secret/versions/latest:access"
```

The value is base64 in `payload.data`. Calls to Google's APIs leave through the proxy like any
other traffic, so with the allowlist enforced their hosts need a grant
([networking.md](./networking.md)).

## What it protects, and what it doesn't

- **Your login stays on your computer.** Only tokens of an hour or less reach the VM, and only for
  the SAs the current level allows. The token service listens on your computer's loopback
  address only.
- **The box does hold a token here.** Unlike an `[[inject]]` switch, where the proxy adds the
  credential in flight and the box never sees it, a Google token sits in the box's memory while
  the box uses it. It's short-lived and stops being renewed when you lower the level.
- **Labels aren't a wall against the box.** They stop one container asking for another's
  identity, but the box holds the engine socket and can start a container with any label. Treat
  every SA a level allows as reachable from the box while that level is on, which is why the
  `box` SA should be read-only and `user` is time-limited.

## When something's off

- **DEGRADED.** The supervisor checks the chain every couple of minutes: that its token service
  still answers on its port, and that it can really mint the identity the level promises (the
  `box` SA at `logs`, the `app` SA at `sa`, both your own token and the `app` SA at `user`). A
  failure shows as DEGRADED on `fy mode` with the fix — usually `gcloud auth login`, or renewing
  a lapsed permission grant
  ([modes.md](./modes.md#degraded--the-switch-is-on-but-the-credential-stopped-working)).
- **BLOCKED**, for `gcp`, almost always means something else holds the token service's port
  ([modes.md](./modes.md#blocked--the-token-service-never-started)).
- **"held by another process, not the token service".** Something, often an editor's automatic
  port forwarding, has taken the port on your computer's loopback address. The box's token
  requests then hang. Stop the forward, then run `fy host restart`.
- **A 404 saying `no gcp.serviceAccount label for the calling container`.** The calling container
  has no label: check your overlay applies at this level (`fy state`).
- **A 404 saying `SA not in allowlist`.** The label names an SA this level doesn't allow, or a
  typo of one. `fy logs metadata-emulator` shows each refusal with its reason.
- **A Python or curl client in a service gets a 502 for the emulator.** With the VM firewall on,
  every container gets `HTTP_PROXY` and a `NO_PROXY` covering only loopback and the host gateway,
  so a client that proxies plain HTTP sends the emulator call to the proxy, which can't resolve
  stack names. Add `metadata-emulator` to that service's `NO_PROXY` and `no_proxy`, keeping
  the existing entries ([networking.md](./networking.md#node-and-npm)).
- **A long call to a Google API dies at about 60 seconds** with `Server disconnected` or an
  HTTP/2 framing error. That's Google: its front ends close a connection that has carried no
  bytes for about 60 seconds on `*.googleapis.com`, even mid-request, and it happens from a
  computer with no VM at all. A long, non-streaming request (a slow model answer, say) hits it.
  Stream the response, or ask for output that keeps bytes flowing (such as a model's thinking
  summaries).
