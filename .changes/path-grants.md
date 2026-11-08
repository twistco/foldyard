---
section: Added
bump: minor
---

- **A grant can name one path of a host: `fy allow add storage.googleapis.com/chrome-for-testing-public/`.**
  It lets the box read under that prefix and nothing else on the host: GET and HEAD with no body,
  checked request by request, and never tunnelled undecrypted. It's for a granted host that
  redirects to a shared one, such as `cdn.playwright.dev` sending Playwright's browser download to
  one bucket of `storage.googleapis.com`. The only option before was to grant the whole shared
  host, which reaches every bucket on it. A refusal after a granted host's redirect now names that
  host and the path grant to make, in its `403` and in the Network Log (`redirected from …`). The
  proxy still doesn't follow redirects for the box: that would let any granted host with an open
  redirect send the box anywhere. See `fy docs networking`, "Path grants".
