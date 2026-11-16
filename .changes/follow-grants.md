---
section: Added
bump: minor
---

- **Follow grants: the box can read where a trusted path redirects, without granting the target
  host.**
  `fy allow add api.github.com/repos/o/r/actions/ --redirects-to '*.blob.core.windows.net/actions-results/'`
  lets the box read the exact URL a redirect from under that path names, query included, for a
  minute: `GET`/`HEAD`, no body, over HTTPS, when one of the targets covers it. Nothing else on the
  target host is allowed. GitHub's job logs and artifacts are the case that needed it: they redirect
  to one of about two hundred storage accounts, so a list of path grants went stale, and
  `*.blob.core.windows.net` would be every storage account, anyone's included. A repo can recommend
  one with `redirects_to = [...]` in `[proxy] recommend`. It's offered and declined apart from a
  grant of the same path. `fy allow list`, `fy allow remove`, `fy config widenings`, the Network Log
  (`followed from <host>`) and the TUI's allowlist pane all show it. The source has to be a path,
  never a whole host: you're trusting where that path redirects. See `fy docs networking` and
  ADR-0029's 2026-11-16 amendment.
