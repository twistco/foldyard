# Transparent container-egress firewall

> **What it was:** a variant of the [proof kit](../README.md) that firewalls the *container
> subnet* transparently instead of a user. **Status:** a finished experiment, not adopted — it
> needs rootful podman, which gives up the rootless property foldyard relies on. **Superseded
> by:** the VM firewall on foldyard's real Lima VM
> ([lima-wall-machine-integration.md](../../../lima-wall-machine-integration.md)), which stays
> rootless and uid-based.

The parent kit firewalls a *user*. This one firewalls **container egress**, the shape where the
agent runs inside a container and any box it starts through the podman socket is another
container on the same network:

- **Transparent:** container `:80`/`:443` is redirected to the in-VM proxy in PREROUTING (OUTPUT
  proved unreliable). Containers need no proxy env, so Go, gRPC and raw sockets are caught too.
- **Default-deny FORWARD** for the bridge subnet (DNS, established and the redirect only);
  everything else REJECTed and logged (`wall-denied`).
- **netavark `firewall_driver = "none"`**, so the kit owns the whole ruleset, masquerade included.

## Run it (its own `wallt` VM)

```bash
cd foldyard/docs/archive/lima-network-forcing-kit/transparent
bash run-cwall.sh             # start/reuse + install + test
bash run-cwall.sh teardown    # stop + delete
```

## Checks (`test-cwall.sh`, read from the proxy's ALLOW/BLOCK log)

| # | check | shows |
| --- | --- | --- |
| a | a container with no proxy env reaching `example.com` is logged `ALLOW` | transparent forcing |
| b | `example.org` is logged `BLOCK` | filtering |
| c | a **sibling spawned through the podman socket** to `example.org` is also `BLOCK` | a box the agent starts is contained too |
| d | `example.com:81` (not redirected) is dropped | no escape via another port |
| e | `passthrough` allows `example.org`; `filtered` blocks it again | runtime toggle |

## Why it wasn't adopted

It uses **rootful** podman so containers sit on a bridge (egress via FORWARD). Rootful containers
are real VM-root, and the engine socket is VM-root: a privileged container flushes the rules.
foldyard's stack is rootless in the VM (pasta, egress as the user's uid), where the parent kit's
uid rule is the match.

If you run it: expect a debug round (`limactl shell wallt sudo nft list ruleset`,
`sudo journalctl -u tproxy`, `sudo tail -f /var/log/transproxy.log`). If containers have no
connectivity at all, check the masquerade rule and `ip_forward` first. No TLS decryption here
either (SNI/Host peek and splice).
