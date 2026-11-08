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
- **In `fy tui`'s Network Log, `a` on a refused request offers a path grant.** You pick one of the
  request's directories, the path itself, or the whole host, then how long. Before, `a` worked only
  on a host row and granted the whole host.
- **`y` in the Network Log copies the request under the cursor as JSON** (on a host, its listed
  requests), through your terminal's clipboard support. Box-written text in it is escaped to
  printable ASCII.
