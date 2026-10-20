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
  it `no credential named`); a host with one rule injects exactly as before. Two injectors writing
  the same header, or one whose row has no `box_env`, are still refused. Give the row's placeholder
  a variable only the harness reads: Claude Code prefers `ANTHROPIC_API_KEY` over its OAuth token.
  See `fy docs configuration`.
