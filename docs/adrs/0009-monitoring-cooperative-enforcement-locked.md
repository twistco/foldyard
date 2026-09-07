# ADR-0009 — Monitoring is cooperative, enforcement is not: don't market filtering until it's the locked kind

- **Status:** Accepted (2026-06-13; layers landed through 2026-07-05) — capture shipped; addon
  default-deny shipped (config-gated); VM-level wall built, Mac-unvalidated
- **Sources:** PLAN.md §6 (honesty rules), docs/history/network-capture.md,
  docs/history/lima-network-forcing-spike.md, docs/lima-wall-machine-integration.md

## Context

foldyard routes the dev box's egress through a host-side mitmproxy (the same daemon that does
credential injection — ADR-0007). Routing works by environment variables: the box is created with
`HTTPS_PROXY` pointing at the host proxy and a mounted CA it trusts. That is **cooperative**
capture: any process that honours proxy env is logged; any process that doesn't — a Go static
binary, gRPC, raw sockets, or simply malicious code that unsets `HTTPS_PROXY` — goes direct out
of the VM's NAT and is invisible. On macOS the rootless-podman network path (pasta inside the VM,
gvproxy on the Mac) offers no netfilter chokepoint on the default network, so there is no cheap
way to *force* traffic to the proxy from outside.

A security tool that blurs this line is selling snake oil. The temptation is real: once a proxy,
a CA, a per-request JSONL log, and an allowlist exist, "egress filtering" is one README sentence
away — but an allowlist enforced only on traffic that *chooses* to route through the proxy stops
no attacker.

## Decision

**State the honesty rule as a design principle and hold every layer — and every doc — to it:
monitoring is cooperative, enforcement is not. Filtering is not marketed until it is the locked
kind, where the proxy is the *only* way out.** The network story is built as three explicit
layers, each labelled with what it actually guarantees:

1. **Phase A/A′ capture (shipped, cooperative).** The box **always** routes through the always-on
   proxy (`ProxyPlugin.derive_env` in `src/foldyard/plugins/proxy.py` always sets `FY_PROXY`;
   `fy up` launches the host supervisor). The `capture` axis is a host-side decision about what
   the daemon *does*, not whether the box routes: `capture=off` = TLS-passthrough (real certs
   end-to-end, SNI/host-level log rows), `capture=on` = full MITM decrypt-and-log, with a
   `[proxy] passthrough` trusted-host list to keep the toolchain quiet. Flipping it never needs a
   box recreate. This is **visibility**, and the docs say so.
2. **Addon default-deny allowlist (shipped, config-gated, still cooperative).** With
   `[proxy] default_deny` on, the packaged addon (`src/foldyard/assets/proxy/egress_proxy.py`)
   refuses any CONNECT/request to a host not on the allowlist. Grants are leveled
   (`once`/`session`/`permanent`, `src/foldyard/allowlist.py`) and **host-side only** — the store
   lives outside the repo mount, so nothing in the box can grant its own egress — and the daemon
   re-reads the resolved `allow-effective.json` per request (mtime-cached), so a grant lands live
   with no restart. This is a *wall on the proxy path*: honest policy for cooperative traffic,
   zero guarantee against a client that skips the proxy.
3. **`[machine].wall = true` — the locked kind (BUILT, Mac-unvalidated).** For the Lima backend
   (ADR-0011), an nftables default-deny wall is provisioned into the real machine VM
   (`src/foldyard/assets/machine-wall/machine-wall.sh`, reconciled by `machine.wall_sync()` on
   every ensure/recreate/start): default-deny the VM user's uid **and its rootless subuid range**,
   open only loopback, DNS to local resolvers, and the Mac gateway (`config.host_alias()` →
   Lima's `192.168.5.2`) on this project's allocated daemon port band (`src/foldyard/ports.py`).
   Egress that ignores the proxy env is REJECTED, not silently missed — fail-closed. `fy verify`
   probes it from the box (direct `1.1.1.1:443` *and* `:53` must be refused). Status is stated
   plainly: code-complete, unit/golden-covered, wall provisioning seen on one real Mac
   (2026-07-05), but the rule battery (`example-lima-wall/test_network.sh`) still wants a real
   Lima VM run — so it ships **opt-in, default off**.

The corollary rules: README/marketing language must track layer 3's *validated* status, never
layer 1–2's feature list; and the spike's own conclusion is kept — no in-VM wall holds against
VM-root, so real credentials stay on the Mac regardless (the wall is enforcement for the
unprivileged agent, defense-in-depth against escalation; the outside-the-VM backstop is that
tokens are injected in flight and never enter the box — ADR-0007).

## Consequences

- Users get an accurate threat model: the Network Log and the allowlist are audit/UX surfaces;
  only lima + wall turns them into enforcement. `fy doctor`/`fy verify` claims are scoped
  accordingly (verify's wall probes run only under lima+wall).
- Capture became always-on plumbing rather than a bolted-on mode — routing is fixed at box
  create, policy flips host-side — which is exactly the shape the wall reuses (routing stays
  fixed; only proxy policy and nft rules change).
- The wall is Lima-only by construction: podman machine's immutable Fedora-CoreOS appliance
  can't be provisioned this way (the finding of the 2026-06-24 spike — the enabler is VM
  *provisioning ownership*, not any network stack). Preflight enforces `wall = true` ⇒
  `backend = "lima"`.
- An earlier packaged proof rig (`fy wall` verb + `assets/wall/`) was retired in PR #36 when the
  real-machine integration landed — foldyard no longer ships a separate proof VM; the standalone
  red-team kit stays under `docs/lima-network-forcing-kit/`.
- Known residuals stay documented, not hidden: QUIC/UDP-443 can bypass a CONNECT proxy (the wall
  closes this under lima; cooperative-only setups are told), and the fuller red-team battery runs
  only in `test_network.sh`, not per-`fy up`.

## Rejected alternatives

- **Market the addon allowlist as egress filtering.** It filters only proxy-routed traffic;
  claiming enforcement would be exactly the dishonesty this ADR exists to prevent.
- **Host-side (macOS) interception instead of an in-VM wall.** gvproxy emits plain host sockets;
  there is no per-container packet path on the Mac to hook — observation (pcap) or DNS games
  only. Confirmed in the network-capture investigation.
- **Run the proxy inside the VM** (earlier draft of the lima integration): drags creds, minters,
  and the CA lifecycle into the very VM the agent lives in, breaking creds-never-leave-the-Mac.
  Routing to the Mac gateway fixes every consumer at once; the proxy never moved.
- **Wrap the agent process with a kernel connect() lock (Nono-style Landlock/Seatbelt).**
  Per-process, same-kernel — cannot wrap a VM from outside, and covers only one process tree,
  not the stack. The VM-boundary analogue *is* nftables default-deny.
