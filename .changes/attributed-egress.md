---
section: Added
bump: minor
experimental: true
---

- **`[machine] attribution = "record"` makes the egress proxy know which container sent each
  request** (Lima, with `firewall = true`; default off). A root forwarder in the VM stands where
  every container still has its own address, and is made the only way from the VM to the proxy
  and the token services on your computer; it adds a signed note of where each request came from
  — the container's address and network, or for build steps, pulls and `--network host`
  containers, the container id, `build` or the VM itself. The network log keeps it as each row's
  `origin`: the Network Log shows who used each host and who was refused, and `fy allow learn`
  names the service that asked. Nothing is decided on it yet. The VM firewall's own
  boot script doesn't change, so turning it on stales no other VM; changing it is
  `fy machine stop && fy up`. With it on, `podman run` defaults to a bridge network, a container
  on an explicit pasta network can't reach the proxy (its traffic can't be told apart from any
  other container's), and a forwarder that isn't serving makes `fy up` refuse rather than hand over
  a box that can reach nothing. On a `vz` VM the forwarder also works round a freeze in Lima's
  user-mode network (gVisor's netstack drops a SYN that reuses a recently closed connection,
  google/gvisor#15013): it resets upstream connections the proxy closed first and re-dials a
  connect that hasn't answered in 2 s. Why: until now every container in the VM — an eval harness's
  sandbox as much as the box — was the same client to the proxy
  ([ADR-0034](https://github.com/twistco/foldyard/blob/main/docs/adrs/0034-the-proxy-knows-which-container-asked.md)).
