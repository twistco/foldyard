---
section: Added
bump: minor
---

- **With an `[[inject]]` switch off, a request carrying its `box_env` dummy gets an answer naming
  the fix instead of the provider's "Bad credentials".** The proxy answers it itself with a 401
  whose JSON `message` says the switch is off and to run `fy mode <switch>=on` on your computer,
  which `gh` and most clients print. It matches where the row's rule would inject (host,
  `path_prefix`, header or `query_param`), and in a header the dummy may stand alone or follow one
  auth scheme (`gh` sends `token x`, others `Bearer x`). Anything else goes on as before: a
  public call with no credential, or a credential the box got some other way. The Network Log
  marks these rows as held and the first one raises a notification, as for Claude and Codex.
