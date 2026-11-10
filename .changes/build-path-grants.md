---
section: Added
bump: minor
---

- **A path grant can be for image builds, and `[proxy] recommend` takes paths.** Previously, a
  build that needed one bucket of a shared host got the whole host: Playwright's browser download
  redirects from `cdn.playwright.dev` to `storage.googleapis.com/chrome-for-testing-public/`, and
  all of `storage.googleapis.com` lets a build step upload to any bucket there. The box can edit
  that step's Dockerfile. Now `fy allow add storage.googleapis.com/chrome-for-testing-public/
  --build` grants builds reads under that path only. The proxy decrypts the build's connection to
  check each path; on a walled VM, build steps already trust the proxy's CA. When a build is
  refused, the build gate offers the refused request's first directory, and `h` grants the whole
  host instead. `[proxy] recommend` accepts `host/path/` entries, `when = "build"` included. A
  grant of the whole host also counts as answering one.
- **A build the allowlist refuses is now decrypted, like the box's own requests.** The proxy
  answers the CONNECT itself, so the refusal row in the log names the path the build asked for,
  not only the host. A build step that doesn't trust the proxy's CA (Java, `apt` over HTTPS, the
  gVisor posture) now fails with a certificate error rather than a `403`. Its refusal is still
  logged and offered by host, as before.
- **`fy config widenings` names `[proxy] recommend` entries it ignores.** An entry that is
  neither a host nor a path grant used to be dropped silently. Now it is listed, and the
  `config widenings` doctor row warns.
