---
name: cloud-host
description: Run foldyard's host tier (a Lima VM, `fy machine`, `fy box up`, `fy up`, `fy verify`, the guest monitor) inside a Claude Code cloud session — a root shell in a container with no /dev/kvm and a TLS-intercepting egress proxy. Use before booting a VM from a cloud session, or when one fails there.
---

# foldyard's host tier in a Claude Code cloud session

A cloud session is a root shell in a container: no `/dev/kvm`, no systemd, and all egress
through an agent proxy that re-signs TLS with its own CA. foldyard's host tier still runs there:
QEMU falls back to software emulation (TCG), slowly. Each section below is a failure this
environment produces, why it happens, and the move that avoids it. Most of them are already
handled by `setup.sh`.

## Set up once per container

```bash
.claude/skills/cloud-host/setup.sh            # operator `fyhost`, foldyard from this checkout
O=/home/fyhost/.fy-cloud
$O/run 'git clone <project> ~/proj && cd ~/proj && fy init && fy config adopt'
```

`setup.sh` (root, idempotent) installs QEMU/OVMF/`qemu-img`/podman/`ssh`, creates the operator,
puts Lima 2.2.0 and an editable foldyard on its PATH, and writes two wrappers:

- **`$O/run '<command>'`** runs as the operator from an EMPTY environment plus `$O/env`. Use it
  for every `fy`/`limactl` call. `run` starts in the operator's home, so `cd` into the project
  in the same command.
- **`$O/box <machine> '<command>'`** runs inside the dev box, non-interactively, in the checkout.
  `fy box shell` needs a TTY. Set `FY_BOX` when the container isn't `<machine>-devbox`.

The project must live in the operator's home: the VM mounts only the checkout, as that user.

## What breaks here, and why

- **Lima refuses root.** Hence the operator. Don't add a CA to the system trust store or edit
  shell rc files to set it up: the session's auto-mode reviewer refuses both as persistence.
  The env file and wrappers do the same job per command.
- **`su` without `-` leaks root's environment.** That includes `GH_TOKEN`, cloud keys and the
  session proxy, all of which reach the supervisor and everything it starts. `run` uses `env -i`
  for this reason. If you bypassed it, `fy host restart` through `run` replaces the supervisor
  with a clean one.
- **No `ssh` client.** Lima's hostagent dies with `exec: "ssh": executable file not found`.
  `setup.sh` installs `openssh-client`.
- **`*_PROXY` must not reach Lima.** Lima copies the host's proxy variables into the guest
  (`propagateProxyEnv`). They override the wall's proxy (`192.168.5.2:<fy port>`), so the guest's
  podman pulls via the session proxy and the VM firewall refuses it:
  `proxyconnect tcp: dial tcp 192.168.5.2:<port>: connect: connection refused`. `$O/env` sets no
  proxy, which works because this host's "direct" egress is intercepted transparently. Check:
  `curl --noproxy '*' -sI https://pypi.org`. If that fails, the session has no direct path, and
  foldyard needs `propagateProxyEnv: false` in its Lima config first. A VM that booted with the
  proxy keeps it until `fy machine stop`, then a start from a clean environment.
- **Every upstream certificate is the session's.** So:
  - **foldyard's proxy (mitmdump):** without `ssl_verify_upstream_trusted_ca` in
    `~/.mitmproxy/config.yaml`, every host answers 502. In `fy host logs`:
    `Certificate verify failed: self-signed certificate in certificate chain`.
  - **The box:** it trusts foldyard's CA, not the session's. So a `[proxy] passthrough` host
    (tunnelled, not re-signed) fails inside the box with `invalid peer certificate: UnknownIssuer`.
    Add no passthrough hosts in a cloud session. The defaults (`@jvm`, `@linux`) are fine, since
    apt uses plain HTTP.
  - **curl:** it reads `CURL_CA_BUNDLE` before `SSL_CERT_FILE`, and root's points at a file the
    operator can't read. `$O/env` sets both.
- **No `/dev/kvm`: QEMU runs under TCG**, one to two orders of magnitude slower than KVM. Measured
  on a 4-vCPU session:

  | step | time |
  | --- | --- |
  | first boot (Fedora image, `dnf install podman`, wall, monitor) | over 10 min |
  | later boots | about 6.5 min |
  | packaged box image build | about 10 min |

  `limactl start` gives up after 10 minutes (`did not receive an event with the running status`)
  while the guest is still provisioning. The VM keeps running. Wait for it, then run
  `fy box up` / `fy up` again:

  ```bash
  $O/run 'until limactl shell --workdir / <machine> test -e /run/lima-boot-done; do sleep 20; done'
  ```

  Wait in a background command, never in a foreground `sleep`. Anything with its own timeout
  under ten minutes (an image build, a healthcheck) needs it raised or the step done by hand
  first.
- **No systemd `--user`.** `fy prereq` warns. Only `[machine] host_firewall` needs it; leave that
  off. The VM firewall (`[machine] firewall`) runs in the guest and works.
- **`fy verify` push check: `git remote UNREACHABLE … (timed out)`.** The checkout's `origin` is a
  local path (a clone of `/home/user/…`), which the box can't see. Point it at the real URL. The
  check then proves the push is refused for lack of credentials.
- **Harmless:** `failed to listen tcp: 127.0.0.1:5355 … address already in use`. That is Lima
  forwarding the guest's LLMNR port, which the session already holds. Also expected:
  `prereq`'s `✗ KVM` and `⚠ systemd --user`.

## The allowlist's learning window is shorter than a TCG build

The first `fy box up` opens a one-hour learning window. A VM boot, the box build and a project's
own image builds can take longer than that. Once enforcement resumes, the next fetch fails with
`403 Forbidden [IP: 192.168.5.2 <port>]`. Review what was recorded, grant it, and reopen the
window for as long as the work needs:

```bash
$O/run 'cd ~/proj && fy allow learn --yes && fy allow enforce learn --for 90m'
```

Then copy the granted hosts into `[proxy] recommend`, each with a real `why`.

## Docker Compose workloads in the box

To run a docker-compose workload from the box (for example an Inspect sandbox), install the
client with `[box] packages = ["docker-cli", "docker-compose"]`. It talks to the VM's podman over
the preset `DOCKER_HOST`. Two more settings are needed:

- **`DOCKER_BUILDKIT = "0"` in `[box].env`.** podman serves no BuildKit, so compose starts its own
  `moby/buildkit` container, and parallel service builds race to create it: `the container name
  "buildx_buildkit_default" is already in use`. With BuildKit off, compose uses the classic
  build API, which podman implements.
- **The docker CLI's `proxies` config for builds.** The walled VM gives every container the proxy
  variables and foldyard's CA (`containers.conf.d`), but a build sent over the docker API gets
  only the CA. Its `RUN` steps go direct and the VM firewall refuses them. Seed the box's docker
  config from `[box].bootstrap`:

  ```toml
  bootstrap = '''
  mkdir -p "$HOME/.docker-fy"
  printf '{"proxies":{"default":{"httpProxy":"%s","httpsProxy":"%s","noProxy":"localhost,127.0.0.1"}}}' "$HTTPS_PROXY" "$HTTPS_PROXY" > "$HOME/.docker-fy/config.json"
  '''
  ```

The box has no `pkill`, `pgrep` or `ps`. To stop a background job, walk `/proc/*/cmdline`. Build
the match pattern from two pieces (`pat=foo""bar`), or the loop's own command line matches it and
kills the loop.

## Where to look when something stalls

- **Lima, per VM** (under `~/.lima/<machine>/` as the operator):
  - `ha.stderr.log`: the hostagent, including each boot-script readiness probe.
  - `serial.log`: the guest console.
- **foldyard:**
  - `fy host logs`: the supervisor and the proxy, including upstream TLS errors.
  - `fy allow learn`: hosts refused, or that would have been.
  - `fy monitor`: the guest monitor's events, when `[machine] monitor = "observe"`.
- **The guest's own state**, readable without root:
  - `/run/lima-boot-done`
  - `/run/fy-wall/state`
  - `/run/fy-monitor/`
