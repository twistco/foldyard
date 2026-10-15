---
section: Added
bump: minor
---

- **Node's `fetch()` and npm's install scripts go through the proxy without project wiring.**
  Node's `fetch()` ignores `HTTPS_PROXY` unless `NODE_USE_ENV_PROXY` is set, so under the VM
  firewall it failed with an `ECONNREFUSED` against a raw IP that named no proxy. An install
  script that fetches with its own client (supabase's CLI download) reads only
  `npm_config_https_proxy`. The box now gets `NODE_USE_ENV_PROXY=1`, `npm_config_https_proxy`
  and `npm_config_proxy` (defaults: `[box].env` may override each), and with the VM firewall on
  every stack container gets the two npm settings. Stack containers don't get
  `NODE_USE_ENV_PROXY`: it would send calls between Node services to the proxy, which can't
  resolve a service name. An app container that wants it sets it itself, with a `NO_PROXY`
  naming the stack's services (`fy docs networking`, "Node and npm"). The stack containers'
  settings are part of the VM's boot setup, so `fy up` refuses a running VM until you run
  `fy machine stop && fy up` once.
- **`fy doctor` points out compose settings that override what foldyard provides.** With the VM
  firewall on, foldyard gives every container the proxy settings, the proxy's CA and npm's proxy
  settings, and a value your compose file sets wins without a word, including a proxy address
  that has gone stale or a mount of `.devbox-ca`. The new `stack env overrides` row names each
  one per service. It's advisory: a value you need, such as a private CA bundle, can stay. Empty
  values aren't flagged, since blanking the proxy is how a stack-only service stays off it. If
  your project wired any of these by hand before foldyard provided them, you can drop them now.
- **The networking guide says when stack containers trust the proxy, and how to fix Turbopack.**
  Only the VM firewall gives containers the proxy's CA, so a container you point at the proxy
  yourself needs it on. `next build` fetching Google Fonts ignores `NODE_EXTRA_CA_CERTS`;
  `NEXT_TURBOPACK_EXPERIMENTAL_USE_SYSTEM_TLS_CERTS=1` makes it read `SSL_CERT_FILE`.
