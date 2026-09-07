# foldyard — the locked-down (lima + wall) example

The most-isolated posture foldyard offers, as a runnable fixture. Where [`../example`](../example)
is the minimal, backend-agnostic dogfood stack, this one turns on **every** isolation layer and
demonstrates the single caveat they introduce.

```
example-lima-wall/
├── foldyard.toml     # backend=lima · [machine].wall · [proxy] default_deny · [claude] keyless
├── compose.yml       # api + worker (worker shows the no_proxy caveat)
├── box.Dockerfile    # same minimal box image as ../example
└── test_network.sh   # HOST-RUN test + diagnostics harness (run after `fy up`)
```

## What it demonstrates

| layer | config | effect |
|-------|--------|--------|
| concurrent VM | `backend = "lima"` | a per-project Lima VM (not the one podman machine) |
| **fail-closed egress** | `[machine].wall = true` | nftables default-deny in the VM; the Mac proxy is the ONLY way out |
| enforced allowlist | `[proxy] default_deny = true` | non-allowlisted hosts are refused (403) at the proxy |
| shared allowlist | `[proxy] recommend` | the repo ASKS; the first `fy up` offers each host, you answer (`fy allow sync --yes` to take them all) — grants stay in the host-side store |
| keyless auth | `[claude] keyless = "oauth"` | the real token is injected at the Mac proxy, never in the box |

Nothing in this checkout can grant egress: `[proxy] allow` no longer exists (a grant list in a
file the box can write is one the box can widen). The recommendations cover the box image, its
bootstrap and the stack's images; Claude Code's installer hosts come from the `[claude]` plugin
itself, and the injector host (`api.anthropic.com`) is allowed implicitly. Answers live in the
host-side allow-store, outside the mount.

The routing that makes it work: under lima, `host.containers.internal` points at the VM, not the
Mac, so foldyard addresses the Mac proxy at Lima's guest→host gateway `192.168.5.2`
(`config.host_alias()`). The wall opens egress *only* to that gateway on the daemon ports.

## Requirements

A **Mac with Lima** (`brew install lima`) and foldyard installed on the host
(`just foldyard install` — the `[host]` extra pulls mitmproxy for the proxy). This example can't
run in CI or a dev box: it needs a real Lima VM (that's why `../example`, not this one, is the
nested-KVM/CI dogfood fixture).

## Run it

**Copy the example OUT of the foldyard checkout first.** A foldyard project must be its own git
repo: the machine mounts the checkout and the box commits in it, so running in-place would bind
the *enclosing* repo as the checkout (preflight aborts `fy up` with this same recipe — a nested
foldyard.toml is deliberately unsupported rather than half-working):

```bash
cp -r foldyard/example-lima-wall ~/fy-wall-example && cd ~/fy-wall-example
git init && git add -A && git commit -m init

fy up                       # creates the lima VM, provisions the wall, brings the stack up
fy box up                   # prompts ONCE (hidden) for a `claude setup-token` OAuth token
bash test_network.sh        # the full network test + diagnostics — paste the output back
```

`test_network.sh` checks the paths that only exist once the VM+wall+box are live and that have
never run on real hardware: VM→Mac proxy reachability, container→Mac under pasta, the wall's
fail-closed property (incl. the hardening probes — a public-IP `:53` connect refused so the
port-53 tunnel is closed, local-resolver DNS still working, and a `--network=host` container's
subuid egress still caught), box egress (allowed vs blocked), and the worker's intra-stack call.
It's non-destructive and re-runnable, and prints WHY each check matters so a paste-back is a
full diagnosis.

## The no_proxy caveat (see `compose.yml`)

With the wall on, foldyard sets proxy env on the VM's rootless podman service, and podman
propagates it into stack containers. So a container calling another **by compose service name**
(`worker` → `http://api:8090`) would try to reach it *through* the Mac proxy — which can't resolve
an in-stack name. The fix is a per-service `no_proxy` listing the in-stack names; the `worker`
service shows it. Delete that line and re-run `test_network.sh` to watch check **E** flip to FAIL.

## Turning the wall off

Comment out `wall = true` in `foldyard.toml` and `fy up` again — the wall is removed from the VM
on the next start (it reconciles to the config). Egress then falls back to cooperative routing
(the box still uses the proxy via its env, but nothing enforces it), exactly like the podman
backend. There's no separate command: toggling the wall *is* editing the toml.
