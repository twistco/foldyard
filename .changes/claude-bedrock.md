---
section: Added
bump: minor
experimental: true
---

- **Claude Code can run on Amazon Bedrock, keyless.** Add a `[claude.bedrock]` table with
  `region`, plus `keyless = "bedrock"`, to point the box's Claude Code at Bedrock. With
  `fy mode claude=on`, the proxy writes a Bedrock API key as the bearer on the region's
  Bedrock hosts. The box only ever holds a dummy. The key is either:
  - a long-term key from `host.env`, pasted once; or
  - with `[claude.bedrock].profile`, a short-term key minted on your computer from that AWS
    profile through the AWS CLI v2. This works with SSO, `aws login` and role profiles, and the
    key lapses with the session. `fy mode` shows the switch degraded with the fix when it does.

  Without `keyless`, the table still points Claude at Bedrock, and the box finds AWS
  credentials itself (`/setup-bedrock`). In the box, `fy verify` checks for the dummy and for no
  AWS keys. Why this shape, and where an AWS metadata emulator fits later: ADR-0035.
