# ADR-0029 — The proxy always decrypts: the `capture` axis is removed

- **Status:** Accepted (2026-09-22). Implemented (on `main` since #31): `ProxyPlugin`
  contributes no axis and always emits `CAPTURE_MODE=full`; `[proxy] passthrough` is the one
  decryption control.
- **Sources:** the loopback measurement below (2026-09-22); the prior-art deep-dives of
  [gondolin](https://github.com/earendil-works/gondolin) (at `29fa74d`, v0.12.0) and
  [vhrn](https://github.com/aravind-n/vhrn) (at `0e3926d`, v0.5.1), which sit at the two ends of
  this choice — written up as
  [prior-art/gondolin.md](https://github.com/twistco/foldyard/blob/main/docs/prior-art/gondolin.md)
  and [prior-art/vhrn.md](https://github.com/twistco/foldyard/blob/main/docs/prior-art/vhrn.md).
  Related:
  [0007](./0007-credential-injection-at-egress-proxy.md) (the proxy is where credentials are
  attached, so it decrypts injector hosts regardless),
  [0009](./0009-monitoring-cooperative-enforcement-locked.md) (capture is visibility, not
  enforcement), [0022](./0022-host-runs-the-adopted-config.md) (`passthrough` is read from the
  adopted copy).

## Context

What the always-on egress proxy did with HTTPS it wasn't injecting was a posture axis, `capture`:

- `off` (the default): blind-tunnel every host, log one SNI row per connection.
- `on`: decrypt and log every request, *except* the `[proxy] passthrough` hosts (`@all` by default,
  ~200 toolchain hosts), which stayed tunnelled.

So there were two controls over one question — "which hosts does the proxy decrypt?" — and the
default answered "none". The operator had to learn both, and the one they reached for first
(`capture`) was the coarser. The only argument for `off` anyone could state was the cost of
decrypting, and nobody had measured it.

### The measurement

A TLS upstream on loopback, the real `egress_proxy.py` under `mitmdump` 12.2.3, curl as the
client; aarch64 with the ARMv8 crypto extensions (the Apple-silicon class the proxy usually runs
on). Direct is the baseline; "per new connection" is the increment over direct.

| path | 256 MiB download | per new connection | per request, kept-alive |
| --- | --- | --- | --- |
| direct | ~2700 MB/s | — | 0.15 ms |
| proxy, blind tunnel (`capture=off`) | ~2000 MB/s | +2.7 ms | 0.29 ms |
| proxy, decrypting, as shipped | ~270 MB/s | +5.8 ms | 0.91 ms |
| proxy, decrypting, `stream_large_bodies=1m` | ~590 MB/s | +5.2 ms | 0.88 ms |

- **The cipher is not the cost.** AES-GCM is hardware-accelerated; the gap is mitmproxy's Python
  handling each chunk on one event loop — one core for every flow of a checkout.
- **~590 MB/s is ~4.7 Gbit/s**, beyond the link most operators download over. It shows under
  many parallel bulk downloads on fast fibre, which is what the toolchain bundles already tunnel.
- **Per request it is ~3 ms on a new connection, ~0.6 ms on a kept-alive one** — under a second
  across a thousand-request install.
- **The large cost was a bug, not decryption:** mitmproxy buffers each body whole unless
  `stream_large_bodies` is set, and foldyard didn't set it — a 2 GB download sat in the proxy's
  memory before the box saw a byte. Fixed alongside this ADR.

### What the neighbours chose

The two closest tools sit at the two ends, and each names the price:

- **gondolin** terminates *everything* — every flow is parsed, which buys content-level policy and
  placeholder secrets on every host, and costs protocol reach (HTTP/1.x only; h2/QUIC break).
- **vhrn** terminates *nothing* — hostname policy at CONNECT only, which buys protocol reach and a
  minimal proxy, and costs any view of what travels: no credential injection, no path/method
  policy, no defence against exfiltration to an allowed domain. Its threat model says so.

foldyard needs termination for injection (ADR-0007) and wants reach for the toolchain, so it
belongs in the middle — but *one* middle, not a switch between two.

## Decision

1. **The proxy always decrypts and logs**, except the hosts on `[proxy] passthrough`, which are
   blind-tunnelled with an SNI row. That is exactly what `capture=on` did; it is now the only
   behaviour. Injector hosts are decrypted even if a passthrough entry covers them (unchanged).
2. **The `capture` axis is removed.** `fy mode capture=…` is refused with the reason (a
   `RETIRED_AXES` entry in `devmode`), and a stale `capture` key in a state file is ignored, as
   any unknown axis always was — no migration step.
3. **`[proxy] passthrough` is the one knob**, and its role is stated as two things: the fast lane
   for trusted bulk hosts, and the escape hatch for a host that *cannot* be decrypted — it pins its
   certificate, needs a client certificate, or ships its own trust roots.
4. **Large bodies stream** (`--set stream_large_bodies=1m`). The 401 re-issue skips a request whose
   body was streamed (there is nothing left to re-send) and hands the 401 back to the client.
   *Amended 2026-10-06:* the threshold left a body of **unknown** length buffered until 1 MiB had
   piled up, so a streamed model reply (`text/event-stream`, chunked) reached the box all at once
   when it ended. The addon now streams every 2xx/3xx response from its first byte (its
   `responseheaders` hook); the threshold still governs request bodies and error responses, the
   two things the addon reads.
5. The addon keeps `CAPTURE_MODE=passthrough` as a standalone option (it is documented env for
   anyone running the script directly); foldyard no longer asks for it.

## Consequences

- **Every existing checkout starts decrypting on upgrade.** The box already trusts the proxy CA —
  always-route mounted it and installed it system-wide (Phase A′) — so a well-behaved client sees
  no change. A client with its own trust store that worked only because nothing was decrypted
  will now fail TLS against a non-toolchain host; the fix is a `passthrough` entry, and the
  release note must say so.
- **Image builds are trusted, not decrypted** (amended 2026-09-23). "The box already trusts the
  CA" missed the *build*: under the in-VM wall a `fy box build` RUN step also egresses through the
  proxy, and a build container has no CA — Playwright's browser download from `cdn.playwright.dev`
  died on `UNABLE_TO_VERIFY_LEAF_SIGNATURE`. `fy box build` now passes the proxy URL as build-args
  with a marker user (`http://fy-build:fy-build@…`; proxy build-args need no `ARG` line and are
  not persisted into the image), and the addon blind-tunnels a CONNECT carrying it, logging a
  `passthrough` row flagged `build`. **The wall still applies** — the marker is checked only
  after the CONNECT is granted. It was a marker, not a credential: the box could present it too,
  and gained only an undecrypted tunnel to a host it could already reach — a visibility
  concession, which ADR-0009 classes as not enforcement (narrowed 2026-10, below). The stack's own image builds
  (`fy up`/`fy build`, the native podman path) carry the marker too; the running stack's
  containers take the VM's unmarked proxy env and stay decrypted — and until 2026-10 had no CA,
  so their HTTPS to a decrypted host failed verification. The walled VM's boot provisioning now
  gives every container the CA through a root-owned podman default (docs/networking.md). A refused build is reported and
  offered host by host, then retried (`foldyard.buildgate`; docs/networking.md). Grants made there
  are **build-scoped**: the proxy honours them only for a connection presenting a live per-build
  secret the gate mints (hashed host-side, revoked when the build ends), so the runtime wall stays
  narrower and the public marker unlocks none of them. The residual: during a build, the box could
  inspect the build container through the engine socket and read that build's secret — a window of
  one build, where a fixed marker would be permanent.
- **The tunnel needs the build's secret too** (amended 2026-10-06). The bare marker still let the
  box choose its own visibility: `fy-build:<anything>@` in its proxy URL turned a decrypted request,
  logged with path and User-Agent, into a hostname-only tunnel row (shown live, 2026-10-04 — the
  allowlist held; what was fetched went unseen). The proxy now tunnels a connection, and flags its
  rows `build` (what the gate offers the operator), only when it presents a live per-build secret;
  the `fy-build` user without one is an ordinary box request. The concession narrows to the same
  one-build window as the grants. **A build the box starts is the box**: it gets the plain proxy
  URL and is decrypted like any box request. Rejected: minting a secret for an in-box build — the
  box would hold a working marker again.
- **Build steps trust the CA** (amended 2026-10-06). Decrypted, an in-box build's RUN steps had
  no CA: podman gives a build the containers.conf drop-in's mounts but not its env (the spike on
  podman 5.8.7), so `apk`, `curl`, `pip`, `uv` and `npm` failed against every host off
  `passthrough` (seen live on foldyard-example). The walled boot provisioning now fronts crun with
  a root-owned wrapper (the drop-in keeps the runtime's NAME and changes its path, so podman's
  per-runtime behaviour is unchanged): on a create whose spec mounts the CA it adds the four
  variables the spec doesn't set, so an image's ENV, a Dockerfile `ENV` and a create's own env
  still win. It reaches every create — a RUN step from the host or the box, a docker-compat
  create — and nothing lands in an image, since the spec is the runtime's, not a layer. Weighed
  and rejected: binding the combined bundle over the distro store (a RUN step that installs
  `ca-certificates` fails renaming over a mount point, and an image's own private CA is
  replaced); `podman build --env` (persisted into the image config); a Dockerfile `ARG` or
  `--mount` (a consumer edit). Not covered: GnuTLS clients (Debian `apt` over HTTPS, `wget`),
  which read only the image's store, Java and NSS stores, and the gVisor posture's runtime,
  which the wrapper doesn't front. The same change fixed the combined bundle on Fedora 44, which
  has no `/etc/pki/tls/certs/ca-bundle.crt`: it held the proxy CA alone, so an `SSL_CERT_FILE`
  client failed every `passthrough` host.
- **Image pulls relied on `passthrough`** (until 2026-10). A pull is the guest podman's own
  traffic, through the VM-wide proxy env: no build marker, and then no proxy CA. A decrypted
  registry blob host failed it with x509 "unknown authority" (the Lima host e2e, when Docker Hub
  served a blob from `production.cloudfront.docker.com`, which `@containers` lacked), so the
  registry hosts had to stay in the default bundles;
  `test_image_pull_hosts_are_tunnelled_by_the_default_passthrough` pins them. **Amended
  2026-10-06:** the walled boot provisioning now also puts the CA in the guest's own trust store
  (root, at boot, one fixed anchor so a rotated CA replaces the old one; taken out on an unwalled
  boot), which podman verifies pulls against. A decrypted registry now verifies, so the registry
  hosts stay in the default for bulk speed and for a VM provisioned without a CA — no longer for
  correctness. Whether the default should shrink is a separate, measured decision.
- **The default narrows to `["@jvm", "@linux"]`** (amended 2026-10-06). Once the box, every
  container, build steps and the guest's pulls trust the CA, the speed argument was the one left
  for `@all`, and it rested on a loopback figure. Measured on foldyard-example (macOS vz, a
  ~620 Mbit/s link, P = `@all`, D = everything decrypted, medians of 3): `npm ci` (518 packages)
  7.5 s vs 7.5 s; `uv` torch + numpy + pandas (~700 MB) 41.5 vs 44.0; `cargo fetch` (285 crates)
  2.9 vs 3.7 (runs 2.2–4.5); `podman pull` 3.9 vs 3.9; Playwright's browser 11.9 vs 11.4; two torch
  installs at once 81.8 vs 81.0; a shallow linux clone 29.1 vs 29.6; `go install` 18.3 vs 17.6.
  The proxy's CPU roughly triples (5 → 15 s for the torch install) and the log gains a row per
  request (~116 KB per `npm ci`) — neither binds. Everything that ran decrypted worked except
  Java (PKIX: its own trust store) and, in the image, GnuTLS `apt` over HTTPS — hence `@jvm` and
  `@linux`. Streamed replies, held until 1 MiB before #61, now arrive at once (first event 0.4–0.6
  s decrypted), so `@anthropic` needn't be tunnelled either; Claude Code's native installer and
  `claude update` (`claude.ai`, `downloads.claude.ai`), the npm route, `rustup` and gcloud all
  worked decrypted, gcloud's own transports once the box exports
  `CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE`. A browser's NSS store still needs the CA imported. A
  consumer who declared `passthrough` keeps it; `["@all"]` restores the old default.
- **The egress log grows**: a request row per request rather than a row per connection, and full
  paths — query strings included — on the host. The log stays outside the mount and rotation
  bounds it (5 MiB × 6 by default). A token carried in a URL now lands there; the `query_param`
  injection case is the one foldyard itself causes (the gondolin review's gap 3) and is still
  open.
- **One axis fewer** in the posture dashboard, `fy mode`, the TUI and the reconciler; the proxy
  daemon's spec no longer changes with posture, so it restarts only for a real change (a wall
  toggle, a secret, an adopted `passthrough`). Since [0030](./0030-the-proxy-reloads-instead-of-restarting.md)
  none of those restart it either: they reach the running proxy through its live file.
- **A User-Agent on every decrypted row** becomes available by default — what an observe-first
  allowlist can use to say *which tool* reached a host, without instrumenting the box.

## Rejected

- **Keep `capture`, default it to `on`.** Keeps the second control for no stated use; the one real
  use of `off` (a host that breaks under decryption) is per-host, which `passthrough` already is.
- **Decrypt everything, drop `passthrough`** (gondolin's end). Loses the escape hatch for
  undecryptable hosts and puts all bulk toolchain traffic through the single Python loop.
- **Decrypt nothing** (vhrn's end). Incompatible with injection, and discards the request-level view
  this proxy exists to give.
