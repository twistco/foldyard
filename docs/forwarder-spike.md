# In-VM attributing forwarder — spike (2026-10-22)

[ADR-0034](./adrs/0034-the-proxy-knows-which-container-asked.md) proposes a root forwarder inside
the Lima guest's rootless-podman network namespace. It would be the only way to the host-side
egress proxy, and it would stamp each connection with where it came from. This is the record of
one run that settles the ADR's facts 1–7 on a real guest. It asks: does the forwarder work without
serious caveats, and what does it cost? It also measures the cheaper alternative the ADR weighs
against, a per-container secret in each container's proxy URL, enough to compare the two honestly.

Nothing here is product code, and foldyard is unchanged. The prototype (a ~300-line stdlib
`asyncio` forwarder, a mitmproxy addon that verifies the stamp, a benchmark client, and the nft
tables) was kept out of the repository. Its shape is described below in enough detail to rebuild
it.

## Where it ran

| | |
| --- | --- |
| Host | macOS 27.0.1 on Apple M3 Max (16 cores), Lima 2.1.3, `vmType: vz`, Lima's user-mode network (guest gateway `192.168.5.2`) |
| VM | a throwaway `template:podman` instance, 4 vCPU / 4 GiB, no foldyard provisioning |
| Guest | Fedora 44 Cloud, kernel `6.19.10-300.fc44.aarch64`, SELinux enforcing, podman 5.8.9, netavark 1.17.2, aardvark-dns 1.17.0, passt/pasta `0^20260120`, crun 1.27, nftables 1.1.6, python 3.14.3; no `slirp4netns` installed |
| Proxy | mitmdump 12.2.3 (foldyard's own install), `--listen-host 127.0.0.1`, `flow_detail=0`, `stream_large_bodies=1m` (foldyard's flags), plus a stamp-verifying addon |
| Origin | a threaded Python HTTP and HTTPS (self-signed) server on the host's loopback, so the internet's speed doesn't enter the numbers |
| gVisor | runsc `release-20260817.0` (foldyard's pinned release), checksum-verified |

The spike VM kept cloud-init's `NOPASSWD:ALL`, and root work was done with `limactl shell … sudo`.
On a foldyard VM, all of it (installing the forwarder and its key, the nft tables, the
`containers.conf` drop-ins) would go into a recorded boot script under its own marker, as
[ADR-0032](./adrs/0032-a-root-collector-in-the-guest.md) does for the monitor. Host ports were in
47100–47191, away from foldyard's band.

## The prototype, in brief

- **Finding the namespace.** Podman bind-mounts the rootless netns at
  `/run/user/<uid>/containers/networks/rootless-netns/rootless-netns`, but only inside its own
  rootless mount namespace. In the VM's mount namespace that path is an empty file. Root reaches
  the real one through podman's pause process (`catatonit -P`, pid in
  `/run/user/<uid>/libpod/tmp/pause.pid`), as `/proc/<pause>/root/run/user/<uid>/…/rootless-netns`.
  `nsenter --net=` and `os.setns()` both accept that path.
- **Catching the traffic without changing the proxy URL.** In the rootless netns, the forwarder
  installs its own table, beside netavark's:
  `table ip fy_fwd { chain pre { type nat hook prerouting priority dstnat - 10; ip daddr 192.168.5.2 tcp dport { <proxy ports> } redirect to :47190 } }`.
  A bridge container's connection to the gateway's proxy port then lands on the forwarder.
  `SO_ORIGINAL_DST` gives back the original port, so one listener serves every worktree's port
  and connects upstream to the same one. Containers keep today's proxy URL.
- **Two namespaces, one process.** The main thread `setns()`es into the rootless netns, creates
  and binds the listening socket, and `setns()`es back. A socket keeps the namespace it was made
  in, so every upstream `connect()` is made from the VM's own namespace, as root.
- **The stamp.** The forwarder reads the head up to `\r\n\r\n` (64 KiB cap, 10 s timeout). It
  drops every `X-Fy-Origin` line the client wrote and appends its own:
  `X-Fy-Origin: v=1; src=10.89.0.6:58516; net=podman1; boot=9e54f453; seq=1; mac=<hmac-sha256>`.
  The `mac` is computed over the fields under a 32-byte key the host keeps, which was copied
  root-only (`0600`) to `/etc/fy-fwd/`. After the head it splices bytes both ways and parses
  nothing. `net` is the bridge interface that owns the address the client dialled.
- **The proxy side.** The addon verifies the HMAC in `http_connect` (CONNECT) and in
  `requestheaders` (absolute form). It records `ok`, `forged` or `unattributed`, and removes the
  header before anything goes upstream. A request inside a CONNECT tunnel inherits the
  CONNECT's verdict. A stamp header found inside a tunnel is the client's own, so it is
  stripped and flagged.

## Findings

### 1. Re-making the connection removes the client (fact 1): confirmed

A bridge container (`10.89.0.4`, network `spikenet`) held a connection to `192.168.5.2:47100`:

```
guest, VM's own netns (sudo ss):
ESTAB 192.168.5.15:44618  192.168.5.2:47100  users:(("pasta",pid=3231,fd=11))
host (lsof -nP -iTCP:47100):
limactl 63321 … TCP 127.0.0.1:57744->127.0.0.1:47100 (ESTABLISHED)   # limactl hostagent of the spike VM
```

In the VM's own namespace the socket is pasta's, from the VM's address. On the host the proxy's
peer is the instance's `limactl hostagent`, from a loopback port of its own. The only place the
container's address survives is inside the rootless netns. There, netavark masquerades bridge
traffic to the netns's copy of `192.168.5.15`, and that netns's conntrack still holds the
original (`src=10.89.0.4 … sport=36417`). The port pasta then uses on the other side (44618) is
unrelated to it, so even a root reader of conntrack can't join the two.

### 2. The rootless namespace's lifecycle (fact 2): it must be followed, and that is a race

- **Created** by the first container that joins any bridge network. Podman starts a pasta
  (`--config-net … --no-map-gw … --map-guest-addr 169.254.1.2`) for it and writes `info.json`,
  `ref-count` and `rootless-netns-conn.pid` beside it. Bridge gateways appear in it one by one as
  netavark sets up each network (`podman1` for `spikenet`; `podman0`, `10.88.0.1`, appeared
  later, when the default network was first used).
- **Torn down** when the last bridge container stops. pasta exits, the files go, and the
  directory is removed, though it sometimes lingers empty. Test for `ref-count` or the netns
  file, never for the directory. The pause process survives.
- **A listener can't be kept across a re-creation.** The forwarder's socket keeps the old
  namespace alive, but nothing routes into it any more, so the forwarder has to follow.
- **nsfs inodes are reused.** Once the forwarder released the old socket, the next namespace came
  back with the same inode (`4026532492`) several times. The detection that works is: hold the
  old listener until a new namespace appears, then compare inodes. A held namespace's inode
  can't be reused, and the runs then alternated `…492` / `…561` as expected. The netns cookie
  (`SO_NETNS_COOKIE`) would be the other way to tell them apart. The prototype polls (`stat` of
  the path through the pause process). inotify was not tried.
- **The first container after a teardown races the forwarder.** In the harness, each run
  starts a container whose first action is a proxied request, into a freshly created namespace.
  With the narrowed firewall in place (finding 3), a lost race is a refused connection, never
  unstamped egress:

  | forwarder | succeeded |
  | --- | --- |
  | polls every 100 ms | 7 / 12 |
  | polls every 500 ms | 1 / 12 |
  | polls every 20 ms | 24 / 24 |
  | polls every 500 ms, crun wrapper waits for it | 12 / 12 (each run took 0.37–0.53 s, against ~0.14 s without the wait) |

  The wrapper is a variant of the walled VM's existing crun wrapper. On a create, it waits
  (bounded at 3 s) until `/run/fy-fwd/netns`, which the forwarder writes world-readable, names
  the namespace's current inode. The wrapper runs inside podman's rootless mount namespace, so it
  sees the bind mount directly. This is for reliability only: a container that skips it races
  and fails closed.
  In normal use the box sits on a bridge network and keeps the namespace alive. The race
  then only affects the first container after everything bridged has stopped, such as `fy up`
  from cold.
- Following is cheap. A follow took 2.5–15 ms (nft table plus listener). The 20 ms poll cost the
  forwarder about 50–60 ms of CPU every 3 s at idle, roughly 2% of a core.

### 3. One process in two namespaces, admitted by uid alone (fact 3): works

The firewall was a copy of `fy_wall` from `machine-wall.sh` with one change: the daemon-port
rule for the walled principals (`ip daddr $GW tcp dport { … } accept`) was removed. The
existing `meta skuid != { 501, 524288-1074266111 } accept` already lets every system uid
through, so the root forwarder needs no rule of its own:

| client | result |
| --- | --- |
| bridge container (through the forwarder) | 200 |
| the VM user's shell, direct | refused (`Could not connect`, 0 ms) |
| default `podman run` (per-container pasta) | refused |
| `--network host` container, root inside (uid 501) | refused |
| `--network host` container, `--user 1000` (a subuid) | refused |
| root in the VM | 200, unattributed |

Any root process gets through, as it does today. That is the existing exemption, not a new hole.

### 4. The stamp end to end: verifiable, unforgeable from a container, but bound to the first head only

- An absolute-form `GET` carrying two client-written stamps (one a well-formed fake with
  `mac=deadbeef`): the forwarder logged `stripped=2`. The proxy saw exactly one stamp,
  `verdict: ok` with the container's own `src=10.89.0.179`. The origin's echo of what it received
  had no `X-Fy-Origin` line.
- A CONNECT with a client `--proxy-header "X-Fy-Origin: forged"` and an inner
  `-H "X-Fy-Origin: inner"`: `stripped=1` on the CONNECT, `verdict: ok`. The inner header was
  flagged as the client's and stripped, and the request inside the tunnel inherited the CONNECT's
  stamp.
- `Proxy-Authorization` passes through untouched. A build-style `http://fy-build:s3cret@…` URL
  reached mitmdump as `Basic ZnktYnVpbGQ6czNjcmV0`. The client matrix (finding 11) reached a
  credential-checking fake proxy through the forwarder with the credential intact and stamped.
- **Gap: a second request head on the same connection is not stamped.** git answered a 407 by
  sending a second CONNECT on the same connection with the credential. The first CONNECT
  arrived stamped and the second unstamped, because the forwarder only reads the first head.
  The same is true of the second absolute-form request on a keep-alive connection. The proxy
  must therefore bind the stamp to the client connection, not to the request. Every later
  request on that connection inherits it, and any stamp header in a later head is stripped as
  the client's. That extends the ADR's "the proxy attributes connections" from CONNECT tunnels
  to keep-alive and 407 retries.

### 5. Other network modes (fact 4)

- **Bridge as the rootless default works.** A system drop-in `[containers] netns = "bridge"`
  put a plain `podman run` on the default `podman` network (`10.88.0.2`, `podman0`), and its
  request arrived stamped `net=podman0`. Without the drop-in, a plain run gets its own pasta
  netns.
- **`--network host` is attributed correctly, but needs a second listener and a firewall line.**
  In the VM's own netns, `table ip fy_fwd_host { chain out { type nat hook output priority dstnat - 10; meta skuid { 501, 524288-1074266111 } ip daddr 192.168.5.2 tcp dport { … } redirect to :47191 } }`
  sends the walled principals' connections to a forwarder listener on `127.0.0.1:47191`. The
  forwarder finds the peer socket's inode in `/proc/net/tcp`, then its owning process, then the
  outermost `libpod-<64hex>.scope` in that process's cgroup:

  | client | stamp |
  | --- | --- |
  | `hostctr` (`--network host`, root inside), `exec wget` | `ctr=5eaabd422ba7` ✓ (its id) |
  | `hostctr2` (`--network host --user 1000`, subuid 525287), `exec wget` | `ctr=00524e1ad6bf` ✓ |
  | `podman run --rm --network host curl …` | `ctr=7c8a148a76f1` (its own) |
  | the VM user's shell | `ctr=not-a-container` |

  The first try was refused by `fy_wall`. After `redirect`, the packet's destination is
  `127.0.0.1`, but the hook's output interface is still `eth0`
  (`fy-wall-deny OUT=eth0 … DST=127.0.0.1 DPT=47191`), so `oifname "lo" accept` doesn't match.
  The wall needs `ip daddr 127.0.0.1 tcp dport <listener> accept` for the walled principals.
  Lima's default port forwarding also published that listener on your computer's loopback
  (`limactl … TCP 127.0.0.1:47191 (LISTEN)`, the spike VM's hostagent). foldyard sets no
  `portForwards`. A connection that comes in that way has no daemon port as its original
  destination, so the prototype refused it. A product forwarder should still bind somewhere
  Lima ignores, or the instance should exclude the port. The `/proc` fd scan costs time in
  proportion to the number of processes. A product
  forwarder would ask `sock_diag` for the socket's cgroup id instead.
- **Explicit `--network pasta`.** Without the host-netns redirect it is refused, which fails
  closed as the ADR expects. With the redirect in place, which builds need (finding 6), its
  traffic arrives at the second listener from **pasta** running in
  `user.slice/podman-<pid>.scope`, the podman CLI's scope rather than the container's. It is
  stamped `not-a-container`, the same as the VM user's own processes. Refusing it, as the ADR
  intends, needs a rule in the forwarder (a pasta helper outside `rootless-netns-*.scope`).
  The firewall can't do it by uid.
- **`--network slirp4netns`:** the template has no `slirp4netns` binary, so the container
  doesn't start at all. **`--network none`:** no egress, as before.

### 6. Builds (fact 5): their RUN steps never enter the bridge namespace

- A rootless `podman build` RUN step runs in the **VM's own network namespace**
  (`net:[4026531833]`, the real `eth0` with `dynamic` DHCP), with or without the bridge-default
  drop-in. The step's process sits in `user@501.service/app.slice/crun-buildah-buildah<N>.scope/container`,
  not under a `libpod-` scope.
- `podman build --network spikenet` fails: `setup network: cannot use networks as rootless`.
  A build can't be put on the bridge.
- So a build reaches the proxy only through the host-netns listener of finding 5. With it, the
  RUN step's `curl` got 200, was stamped `net=host; ctr=not-a-container`, and its
  `Proxy-Authorization` (the build secret) arrived intact. Without it the same step got `000`.
  The second listener is therefore required, not an option for `--network host` alone. Pulls
  by the podman service (the VM user) take the same path.
- The forwarder can tell a build step apart by its `crun-buildah-*.scope`. Host-started builds
  remain distinguishable only by their secret, since a box can also build through the socket.

### 7. Cost (fact 6): no measurable penalty beyond noise; the forwarder replaces pasta's work rather than adding to it

**Method.** The client ran in a `python:3.13-alpine` container on the bridge network, with a
long-running container keeping the namespace alive. Each sample is a **new TCP connection** to
the proxy, timed from `connect()` to the end of the response:

- `small`: an absolute-form `GET` for 100 bytes, `Connection: close`.
- `connect`: `CONNECT` + a TLS handshake through mitmproxy's interception (which mints a leaf
  certificate and makes its own TLS connection upstream) + `GET`.
- `bulk`: a 256 MiB `GET`.

There were two arms. **direct**: `192.168.5.2:47103`, today's path, re-made by the rootless
netns's pasta. **fwd**: `192.168.5.2:47100`, redirected to the forwarder. Both are listeners
of one mitmdump process, and the addon runs on both. The arms alternated within each round. The
table is the second pass: 5 rounds, after the origin's listen backlog was raised from Python's
default of 5. Each cell is the median of the per-round values, with [min–max] across rounds.

| test | workers | conns/round | p50 ms direct | p50 ms fwd | p90 ms direct | p90 ms fwd | p99 ms direct | p99 ms fwd |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| small | 1 | 300 | 2.05 [1.85–2.43] | 1.99 [1.94–2.56] | 2.44 [2.22–3.79] | 2.68 [2.26–5.95] | 3.05 [2.76–7.35] | 3.21 [2.82–19.6] |
| small | 8 | 2,000 | 10.1 [8.97–11.1] | 10.2 [10.0–11.5] | 11.2 [9.95–12.5] | 11.5 [11.4–12.5] | 13.4 [11.7–19.1] | 14.9 [12.7–25.5] |
| CONNECT+TLS | 1 | 150 | 9.10 [8.49–11.0] | 9.04 [8.51–11.6] | 10.4 [9.56–12.4] | 10.4 [9.28–13.1] | 14.4 [12.3–49.3] | 12.5 [9.92–38.3] |
| CONNECT+TLS | 8 | 400 | 45.7 [44.8–47.6] | 45.4 [41.8–48.0] | 54.7 [51.5–73.5] | 48.3 [46.9–51.0] | 88.0 [85.2–90.7] | 70.7 [51.3–68,320] |

| bulk | direct MB/s | fwd MB/s |
| --- | --- | --- |
| 1 client, 3 × 256 MiB per round | 331.5 [293.7–343.4] | 342.3 [10.5–358.4] |
| 4 parallel, 8 × 256 MiB per round (aggregate) | 269.4 [29.3–289.6] | 287.0 [54.5–301.2] |

The low minima are rounds hit by the stall in finding 8. The first pass (3 rounds, the origin's
backlog at 5) had the forwarder slightly slower at the median: `small`×1 p50 2.06 → 2.41 ms
(+0.35), `CONNECT`×1 p50 8.93 → 9.73 ms (+0.8). That is the worst difference observed. The
first pass also had a few 502s on 8-worker CONNECTs, in both arms (11 and 8 reported), because the
origin then handshook TLS inside `accept()`. The second pass had none.

**Throughput ceiling.** It isn't the forwarder. A 256 MiB transfer straight to the origin, with
no proxy, was 347–350 MB/s through pasta, 360–367 MB/s through the forwarder, and 350 MB/s from a
root process in the VM's own namespace with neither. Lima's user-mode network is the cap.

**CPU.** Measured as `utime+stime` from `/proc/<pid>/stat` while 1 GiB moved:

| arm | the rootless netns's pasta | the forwarder |
| --- | --- | --- |
| direct, raw | 2,230 ms | 60 ms (idle polling) |
| fwd, raw | 0 ms | 2,210 ms |
| direct, through mitmdump | 2,170 ms | 50 ms |
| fwd, through mitmdump | 0 ms | 2,160 ms |

The forwarder's upstream leaves from the VM's own namespace, so proxied bytes no longer pass
through pasta. A stdlib `asyncio` copy loop costs about what pasta's C does here, ~2.2 s of CPU
per GiB, about 70% of a core at full speed. Per new connection, the forwarder spent 0.34–0.67 ms
of CPU (median over rounds; up to 5.3 ms in rounds with a stall). At the incident's ~1,100
requests a minute (~18/s) that is about 1% of a core. The forwarder sustained 680–770 new
connections a second in the 8-worker rounds without a stall, about 40 times that rate. `os.splice` would cut the
copy cost further; it was not tried.

### 8. A side finding: the stall is Lima's user-mode network, in both arms

Stalls of **7–69 s** (most often 68.1–69.1 s, then 35.8 s) hit connections in both arms: 12 of 48 runs on
the direct arm and 10 of 48 on the forwarder arm, across both passes. On the direct arm the
container's own `connect()` waited. On the forwarder arm the forwarder's upstream `connect()`
waited. The same happens with no container, pasta, forwarder or proxy involved. From the VM's
own namespace, 8 threads × 500 connections straight to the host origin
(`192.168.5.2:47105`): **16 of 4,000 connects took 68.2–68.4 s, all 8 workers at once**, and the
other connects took 0.20 ms (p50). One thread × 2,000 at ~510/s saw none. The same burst on the
host against `127.0.0.1:47105` saw 0 of 12,000. The host had only ~1,200 sockets in `TIME_WAIT`
when one began, so it isn't ephemeral-port exhaustion.
The durations match Linux's SYN retransmit schedule (`tcp_syn_retries = 6`: 15, 31, 63 s) plus
~5 s: whole seconds in which Lima's user-mode network answered no new connection.

This is very likely the mechanism behind the 2026-10-22 stall, though this run doesn't prove it.
It is not the proxy's doing, the forwarder doesn't cause it, and neither alternative fixes it. A
forwarder could at least retry its own upstream connect on a short timeout instead of the
kernel's backoff. That was not tried.

### 9. gVisor (fact 7)

A runsc container (`--runtime runsc-fy`, foldyard's flags `--ignore-cgroups --host-uds=all`,
`label=disable`) on the bridge network reached the proxy through the forwarder and was stamped
with its own address (`src=10.89.0.196`, `net=podman1`). gVisor's netstack sends through the
container's own netns, which sits on the bridge like any other. A runsc container with
`--network host` had no usable network in this rootless setup: no interfaces listed, and
connects failed at once. It fails closed. That wasn't investigated further.

### 10. Failure modes: closed, and a restart re-follows

With the forwarder stopped and its tables left in place, a bridge container and a host-network
container both got an immediate reset (`000`, 0.15 ms). Never unstamped egress. A restarted
forwarder found the live namespace and served again within a second. Its nft load is an
add, though, so each restart appended a duplicate redirect rule (2, then 3). A product
forwarder must replace its table atomically.

### 11. The alternative, measured: a secret in each container's proxy URL

- **The wrapper sees what it needs.** The walled VM's crun wrapper, given the same drop-in shape
  as `91-fy-proxy-ca-runtime.conf`, is called as
  `crun … create --bundle …/overlay-containers/<64hex>/userdata --pid-file … <64hex>`. The
  container id is its last argument. `config.json` holds the final environment, the image's
  `ENV` and the create's `-e` merged (`['HTTP_PROXY=http://192.168.5.2:47100']`). So it can
  write a per-container credential into the proxy variables, and it can rewrite a value that a
  compose `environment:` or a Dockerfile `ENV` set, as long as that value names the gateway.
  The ADR's "silently drops the credential" holds only for proxies set in a tool's own config
  file (`.npmrc`, `pip.conf`, git's `http.proxy`) or at run time.
- **It runs as the VM user.** Inside podman's user namespace it is uid 0, with `uid_map`
  `0 501 1`, so any key it can read, the box can read through the socket. Even with a root
  signer, the credential ends up as a bearer secret in each container's environment, which any
  socket holder can read with `podman inspect` and hand to any other container. Under the
  forwarder, using another container's identity means sending from inside it.
- **Which clients send a credential from the proxy URL.** Each client below ran on the bridge
  network (so through the forwarder) against a fake proxy on the host that answers 407 to a
  request without `Proxy-Authorization` and 502 to one with it:

  | client | sends the URL's credential |
  | --- | --- |
  | curl 8.16 (`HTTPS_PROXY`, CONNECT and absolute form) | yes, preemptively |
  | busybox wget 1.37 | yes. It sends `GET https://…` to the proxy instead of CONNECT |
  | pip (python 3.13), Python `urllib` | yes |
  | uv 0.13.5 | yes |
  | npm (Node 24), from `HTTPS_PROXY` or `npm_config_https_proxy` | yes |
  | Node 24 `fetch` with `NODE_USE_ENV_PROXY=1` | yes |
  | Go 1.25 `net/http` (`ProxyFromEnvironment`) | yes |
  | apt (Debian trixie) | yes |
  | git 2.49 | only after a 407: the first CONNECT has none, and the retry on the same connection does (and, per finding 4, arrives unstamped) |
  | JVM 21 `HttpClient`, environment only | ignores `HTTPS_PROXY` entirely: dials direct and is refused by the firewall |
  | JVM 21 with `-Dhttps.proxyHost/Port` and `-Dhttp.proxyUser/Password` | reaches the proxy, sends no credential, fails with 407 |

## Verdict

**The forwarder is viable with caveats. There is no performance penalty that this setup can
measure, and under the operator's rule the fallback should not be preferred.**

**Penalty.** New-connection latency: the p50s of the two arms were within ±0.4 ms of each other
in the 5-round pass (2.05 vs 1.99 ms for a small request, 9.10 vs 9.04 ms for CONNECT+TLS). The
worst observed was +0.8 ms (CONNECT+TLS p50, first pass), and +1.5 ms at p99 (`small`×8).
Throughput: 331 → 342 MB/s for one client and 269 → 287 MB/s for four, both under Lima's
~350 MB/s cap. CPU: ~2.2 s per GiB, which is what pasta spends today and no longer spends on
these bytes, plus 0.3–0.7 ms per new connection. The prototype is Python. Nothing here shows it
becoming the bottleneck at the incident's load or at 40 times it.

**Caveats a design has to carry:**

1. **The namespace has to be followed.** Hold the old listener until the new namespace appears
   (inodes are reused otherwise). The first container after a full teardown races the forwarder
   and fails closed. The crun wrapper's bounded wait removes the race, at a cost of up to one
   poll interval.
2. **A second listener in the VM's own namespace is required**, not optional. It is how build
   RUN steps (always in that namespace), pulls, `--network host` and explicit pasta reach the
   proxy. Its firewall line is `ip daddr 127.0.0.1 tcp dport <listener> accept`, not
   `oifname "lo"`. It attributes by the owning process's cgroup: a container's `libpod-` scope,
   a build's `crun-buildah-` scope, or the VM user.
3. **Explicit pasta can't be refused by the firewall** once builds are admitted. Its helper
   process is in the podman CLI's or service's scope. The forwarder has to refuse it, or record
   it as the VM user's.
4. **Bind the stamp to the connection at the proxy.** A 407 retry or a second keep-alive request
   arrives unstamped, and git relies on the former.
5. **Daemon ports that aren't proxies** (token services that containers call directly) must also
   be redirected and forwarded, or they lose reach. Not tested.
6. Keep the VM-namespace listener out of Lima's automatic port forwarding. Replace the nft
   tables atomically on restart. Use `sock_diag` rather than an fd scan for the
   host-netns peer. Consider inotify over polling.
7. **Lima only.** On the podman backend nothing here applies, and everything stays unattributed.
8. **gVisor with `--network host`** had no network at all. It fails closed, and the cause was
   not investigated.

**Why not the fallback.** It is cheaper to build: no new daemon, and every network mode
alike. But the credential is a bearer secret in each container's environment, which any engine
socket holder can read and hand to another container. The key is readable by the VM user unless
a root signer is added. The JVM can't use it at all, git needs a 407 challenge, and proxies set in
tool config files bypass it. None of the forwarder's caveats is fundamental. The fallback's main
weakness is.

**Separately**, the ~68 s freeze in Lima's user-mode network (finding 8) deserves its own
investigation. It reproduced without containers, pasta, the forwarder or the proxy.

## Not done

- The real foldyard provisioning: the boot script, the actual `fy_wall`, the proxy's own addon.
- The guest monitor's join.
- Non-proxy daemon ports.
- inotify following, `os.splice` and `sock_diag`.
- The QEMU `vmType` and x86_64 (this was vz on arm64 only).
- A Linux or WSL2 host.
- podman 6.
- Load from real image builds: the load here was synthetic connections at the same and higher
  rates.
- Why runsc `--network host` had no network.
- The root cause of finding 8.

## Re-running

The prototype isn't in the repository. To rebuild it, start from this page. Rebuild order:

1. `limactl create --tty=false --name fy-spike-fwd --cpus 4 --memory 4 template:podman`, then
   `limactl start fy-spike-fwd`.
2. On the host, start the origin and a mitmdump with the stamp addon and two listeners
   (`--mode regular@<fwd port> --mode regular@<direct port>`, loopback only), with its own
   `--set confdir=…`.
3. In the guest as root, install the key (`0600`) and the forwarder.
   - The forwarder follows the namespace through the pause process and loads `fy_fwd` there.
   - It also listens on `127.0.0.1` in the VM's own namespace.
   - Then load `fy_fwd_host` and the narrowed `fy_wall`.
4. Optionally, add the `netns = "bridge"` drop-in and the crun wait wrapper.
5. Run the clients from bridge containers.

Stop the VM afterwards (`limactl stop fy-spike-fwd`). Never point any of this at a foldyard
VM: it needs root in the guest, which foldyard's boot provisioning removes.
