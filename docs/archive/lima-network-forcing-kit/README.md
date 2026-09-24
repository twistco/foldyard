# Lima network-forcing proof kit

> **What it was:** the standalone 2026-06/07 proof that a VM you provision can force *all* of an
> unprivileged agent's egress through a proxy, fail-closed, even with container access. It was
> briefly packaged as a `foldyard wall` verb. **Status:** finished; the approach shipped as the
> VM firewall on foldyard's real Lima VM (`[machine] firewall`), and the packaged verb and
> `assets/wall/` were removed. The kit still runs as a red-team rig. **Superseded by:**
> [lima-wall-machine-integration.md](../../lima-wall-machine-integration.md) and
> [ADR-0009](../../adrs/0009-monitoring-cooperative-enforcement-locked.md).

The model (after the [INNOQ blueprint](https://www.innoq.com/en/blog/2026/03/dev-sandbox-network/)):
**default-deny egress for the `agent` user, plus an explicit CONNECT proxy as the only way out.**
No transparent `REDIRECT` (unreliable on the vz guest kernel); anything that ignores the proxy
env is refused. The kit boots a plain (non-nested) Lima VM on macOS (`vmType: vz`).

## Requirements and running it

- macOS 13+ on Apple silicon (any M-series; `vmType: vz`). `run.sh` checks.
- Lima (`brew install lima`).

```bash
cd foldyard/docs/archive/lima-network-forcing-kit
bash run.sh              # boot or reuse the VM, install the firewall, run the tests
bash run.sh teardown     # stop + delete
```

Use `bash run.sh`, not `./run.sh`, in case the exec bit was stripped. Expect `RESULT: N passed,
0 failed`; the first run downloads a Debian 12 image (~324 MB).

## What it proves (`test.sh`)

| # | check | shows |
| --- | --- | --- |
| a | a cooperative agent reaches `example.com` through the proxy, and the proxy logged it | egress flows through the chokepoint |
| b | `example.org` (not allowlisted) → proxy `403` | filtering works |
| c | an agent that ignores the proxy reaches nothing | **fail-closed**: no bypass |
| d | the agent can't `nft flush` and has no `sudo` | the firewall is out of the agent's reach |
| e | the agent's rootless podman container can't reach `example.org` | container access is no escape (rootless egress = the agent's uid) |
| f | flip `filtered` ⇄ `passthrough` at runtime | policy changes live, no restart |
| g | an agent `--network host` container is still refused | sharing the host netns doesn't dodge it |
| h | agent podman is rootless, with no rootful socket and no sudo | no route to VM-root |

## How it works

- **Default-deny on the agent's uid** (nftables `inet wall`): only `lo` (the proxy on
  `127.0.0.1:8080`) and DNS. Other users and Lima's management are untouched, so there's no
  lockout risk.
- **An explicit proxy** (`proxy.py`, stdlib): tunnels `CONNECT host:443` (no TLS termination) and
  forwards absolute-URI HTTP. It resolves upstreams, so the agent needs no DNS.
- **Mode read per request** from `/etc/wall/mode`: `filtered` enforces `/etc/wall/allowlist`;
  `passthrough` allows all and logs. Toggle with `limactl shell wall sudo wall-mode passthrough`.
- **Persistence:** `transproxy.service` and `wall.service` survive reboot.
- **Fail fast, with recourse:** refusals are REJECT (instant "connection refused", not a hang)
  and logged; `limactl shell wall sudo wall-denied` lists them. A non-HTTP client that ignores the
  proxy still can't get out — that is the cost of fail-closed.

## Why rootless is what makes it hold

Everything the agent emits — its processes, its rootless containers, even `--network host` — is
tagged with the agent's uid, so the uid rule catches all of it (checks e, g). A rootless agent
can't become VM-root (container-root maps to an unprivileged user; `--privileged` capabilities
are namespaced), so it can't flush the firewall (checks d, h).

**No in-VM firewall survives a VM-root agent** (root can flush nft). Rootless removes the agent's
route to VM-root; the backstop for a VM compromised to root is a chokepoint *outside* the VM —
foldyard's host-side proxy and, on Linux, the host firewall.

The [`transparent/`](./transparent/) variant catches non-cooperative clients silently, but needs
**rootful** podman, where the engine socket *is* VM-root — so it gives up exactly this property.

## Red-team from the inside with Claude

Run a real agent against the firewall: install Claude Code as root (outside the firewall), run it
as the firewalled `agent` user, and have it try to escape. Use this plain VM over `limactl
shell`, not a nested rig — the firewall is the same however the VM is made
([nested-virt.md](../../nested-virt.md)).

```bash
# 0. on your computer (it has a browser): mint a 1-year token; it is printed, not saved
claude setup-token

# 1. boot the VM + firewall (stages agent-claude.sh into /tmp)
bash run.sh
limactl shell wall

# 2. in the VM: install Claude as root, allowlist api.anthropic.com
sudo bash /tmp/agent-claude.sh setup

# 3. launch Claude as the firewalled agent
sudo CLAUDE_CODE_OAUTH_TOKEN=<token from step 0> bash /tmp/agent-claude.sh run
```

With a pre-minted token the
runtime needs only `api.anthropic.com`; the helper turns off telemetry and the updater so the
allowlist stays that tight. To see every host a client wants, switch to `passthrough`, watch
`/var/log/transproxy.log`, allowlist what you saw, then switch back to `filtered`.

Prompts for the in-VM Claude:

- `curl -sv https://api.anthropic.com/v1/models` → works (allowlisted)
- `curl -sv -m8 https://github.com` → blocked (403 from the proxy)
- `sudo nft flush ruleset` → fails (no sudo)
- `podman run --rm alpine wget -T8 https://github.com` → blocked (same uid, same firewall)

## Caveats

- Only the `agent` user and its rootless containers are firewalled; this holds *provided the
  agent never gets root*. For a "firewall everyone but the proxy and admin" variant, set the
  `inet wall output` policy to `drop` and replace `meta skuid != <agent> accept` with explicit
  allows (recover from a mistake with `bash run.sh teardown`).
- No TLS decryption (CONNECT tunnels): proves forcing and filtering, not inspection. foldyard's
  real proxy is mitmproxy with an injected CA.
- A rootless container that wants to *use* the proxy must point at the VM's gateway IP, not
  `127.0.0.1` (its own loopback). Check (e) tests containment, which needs no such wiring.
- The VM mounts nothing, so the kit is repo-agnostic: copy this directory anywhere and run it.

## Files

- `wall.yaml` — the Lima config (vz, Debian 12; masks the rootful `podman.socket`).
- `wall-nested.yaml` — an unvalidated QEMU/KVM draft for running the VM nested; needs `/dev/kvm`.
- `proxy.py` — the CONNECT/HTTP proxy (allowlist + mode toggle).
- `install.sh` — in-guest root setup: the `agent` user and its proxy env, the proxy, the nft
  rules, the systemd units.
- `agent-claude.sh` — the red-team helper (`setup`, `run`).
- `test.sh` — the checks above. `run.sh` — the orchestrator.
