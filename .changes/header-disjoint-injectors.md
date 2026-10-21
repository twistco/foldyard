---
section: Changed
bump: minor
---

- **Two injectors may share a host when they write different headers, and the proxy routes each
  request by the placeholder it carries.** Keyless Claude on its OAuth token (`authorization` on
  `api.anthropic.com`) and an `[[inject]]` row writing `x-api-key` there, with a `box_env`
  placeholder, can now both be on, so an evaluation harness in the box can call the Anthropic API
  with its own key beside Claude Code. Before, `fy mode` refused the pair, because the proxy injected
  the first rule matching a host and path whatever the request carried. Now, where several rules
  match, it injects only the one whose placeholder the request carries in that rule's header, and
  answers a request carrying neither or both with a 502 naming the switches (the Network Log marks
  it `no credential named`). A host with one rule injects as before, except that a request
  carrying only another credential's placeholder, in another header, is never sent on the rule's
  credential: it gets that switch's "turn it on" answer if it is off, or a 502 saying the path is
  outside that credential's `path_prefix` (marked `placeholder out of scope`). Two injectors
  writing the same header, or one whose row has no `box_env`, are still refused. Give the row's placeholder
  a variable only the harness reads: Claude Code prefers `ANTHROPIC_API_KEY` over its OAuth token.
  See `fy docs configuration`.
