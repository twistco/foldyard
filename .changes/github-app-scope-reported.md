---
section: Added
bump: minor
---

- **foldyard shows what a GitHub App installation grants, and tells you when that changes.**
  foldyard no longer caps a `github-app` token's scope (the installation's permissions are the
  box's scope), so it reports them instead. While the switch is on, its probe reads the
  installation's permissions and which repositories it covers, as the App, minting nothing.
  `fy config widenings` shows the last reading under the row, flagging any `write` or `admin`
  permission ("not yet probed" until the switch has been on), and `fy mode` and `fy tui` show it
  beside the switch. All of them read a record on your computer and never call GitHub. When a
  reading differs from the last one, for example after an org owner accepted a new permission on
  github.com, the supervisor logs one line naming what was added, removed or changed level and
  sends one desktop notification; the first reading is a baseline. An uninstalled or suspended
  installation now shows the switch as DEGRADED, naming `installation_id`.
