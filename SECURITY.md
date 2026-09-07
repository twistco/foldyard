# Security policy

foldyard is a security tool: its job is to keep a coding agent's blast radius inside a throwaway
VM and to keep credentials on the host. A bug in that boundary matters more than any feature.

## Reporting a vulnerability

Email **security@tangible.finance**. Please do not open a public issue for anything you believe
is exploitable. Include what you observed, how to reproduce it, and which foldyard version and
host platform you were on (`fy --version` once it exists; until then the installed wheel's
version). We will acknowledge the report and keep you informed while we work on it.

## What counts

Anything that lets the box side do what only the host side should:

- **The VM boundary** — a process in the box reaching the host filesystem, host processes, or
  a host socket other than the one engine socket it is handed.
- **Credential exposure** — a token, key or cookie the host holds becoming readable inside the
  box, in the network log, or in a transcript. Injection at the egress proxy is designed so the
  box only ever sees a placeholder.
- **Egress** — traffic leaving the box that bypasses the proxy or the wall, or a `default_deny`
  posture that lets something through.
- **Config trust** — any path by which editing `foldyard.toml` in the checkout changes what the
  host does without an operator adopting it (see ADR-0022 and ADR-0023 in `docs/adrs/`).
- **Supply chain** — the box bootstrap or the host install fetching something it should not.

`docs/security.md` describes the threat model and what `fy verify` proves; if you can make
`verify` pass while one of the claims above is false, that is a report we want.

## Supported versions

Pre-1.0: only the latest release on PyPI receives fixes.
