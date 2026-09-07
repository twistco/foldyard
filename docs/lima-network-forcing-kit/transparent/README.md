# Transparent container-egress wall (the faithful Foldyard model)

The explicit kit (parent dir) walls a *user*. This variant walls the **container subnet**, which
is the real Foldyard topology: the agent runs *inside a container*, the app stack are containers,
and any box the agent stands up via the podman socket is *another container on the same network*.
So the wall is on **container egress**, and a spawned box can't escape it.

- **Transparent**: container `:80/:443` is REDIRECTed to the in-VM proxy in **PREROUTING** (the
  reliable redirect path — unlike OUTPUT, which failed for host-process traffic). Containers need
  **no proxy env**, so non-cooperative clients (Go, gRPC, raw sockets) are caught too.
- **Default-deny FORWARD** for the bridge subnet (only DNS + established + the redirect): no
  escape via another port; everything else is `REJECT`ed and logged (`wall-denied`).
- **The proxy is the only exempt egress** (host-ns process); its upstream is the single way out.
- **netavark `firewall_driver = "none"`** so we own the entire ruleset (no fighting netavark's
  own FORWARD/NAT rules) — we provide the redirect, the default-deny, and the masquerade.

## Run it (uses its own `wallt` VM — won't clash with the explicit kit's `wall`)

```bash
cd foldyard/docs/lima-network-forcing-kit/transparent
bash run-cwall.sh                 # start/reuse VM + install + test
bash run-cwall.sh teardown        # stop + delete
```

## Checks (`test-cwall.sh`, keyed on the proxy's ALLOW/BLOCK log — deterministic)

| # | Check | Demonstrates |
|---|-------|--------------|
| a | a container with **no proxy env** reaching `example.com` shows up as `ALLOW` in the proxy log | transparent forcing — catches non-cooperative clients |
| b | a container to `example.org` shows up as `BLOCK` | filtering |
| c | a **sibling box spawned via the podman socket** to `example.org` *also* shows up as `BLOCK` | a box the agent stands up is contained by the same wall — the key result |
| d | a container to `example.com:81` (non-redirected port) is dropped | no escape via another port |
| e | flip to `passthrough` → `example.org` now `ALLOW`ed; back to `filtered` blocks it | runtime toggle |

## Honest caveats / things to expect

- **More moving parts, less battle-tested than the explicit kit.** Plan on a debug round. Key
  diagnostics: `limactl shell wallt sudo nft list ruleset`, `... sudo journalctl -u tproxy`,
  `... sudo tail -f /var/log/transproxy.log`, `... sudo wall-denied`.
- **Rootful podman in the VM** is used so containers sit on a netavark bridge (egress via FORWARD,
  which is what the wall hooks). Foldyard's stack is *rootless-in-VM* (pasta, egress as the user) —
  there the uid-based explicit kit is the closer match; this variant models the rootful-bridge
  case (the "confirm rootful vs rootless in the VM" decision point of the original capture investigation).
- **`firewall_driver = "none"`** means we must provide masquerade ourselves (we do). If containers
  have *no* connectivity at all, that rule (or `ip_forward`) is the first thing to check.
- The proxy still does **no TLS decryption** (SNI/Host peek + splice). Add mitmproxy + a CA for
  decryption later; the forcing mechanism is unchanged.
