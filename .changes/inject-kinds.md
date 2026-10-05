---
section: Added
bump: minor
---

- **An `[[inject]]` row names its token protocol with `kind`, and can bake dummies into the box
  with `box_env`.** `static` (the default — every existing row) is unchanged; `github-app` mints a
  GitHub App installation token from `app_id`/`installation_id` (and optional `repositories`);
  `gh-cli` injects your own `gh` token and must be `emergency = true`. The GitHub kinds are pinned
  to `api.github.com`. Each kind takes a fixed set of fields, so a typo or a field the kind doesn't
  take now stops foldyard loading the config, naming what is allowed — check your rows if `fy`
  refuses one. `box_env = { NAME = "dummy" }` is what a client in the box needs before it sends the
  header the proxy overwrites; it's baked whenever the box routes through the proxy, so turning the
  switch on needs no box recreate, and `fy verify` fails a box where it holds anything else. A
  `github-app` row brings the old plugin's checks with it: the key's presence and shape in
  `fy doctor`, the "can we still act as the App?" probe on `fy mode`, and the in-box check that the
  token really reaches requests.
