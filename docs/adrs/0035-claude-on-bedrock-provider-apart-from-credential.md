# ADR-0035 — Claude on Bedrock: the provider is apart from the credential; AWS identity comes later

- **Status:** Accepted (2026-10-29). Implemented, experimental (`[claude] keyless = "bedrock"`):
  `[claude.bedrock]`, the `bedrock` keyless kind, the short-term key minter
  (`plugins/bedrock_token.py`) and `awsauth.py`. The AWS metadata emulator (decision 4) is not.
- **Sources:** Claude Code's Amazon Bedrock and LLM-gateway docs (code.claude.com, read
  2026-10-29); AWS's Bedrock API keys guide and its `aws-bedrock-token-generator` 1.1.0 source.
  Related: [0007](./0007-credential-injection-at-egress-proxy.md),
  [0008](./0008-keyless-agent-auth.md), [0015](./0015-plugin-framework-per-consumer-registry.md)
  (the gcp metadata emulator), [0023](./0023-no-host-executed-code-from-the-repo-mount.md),
  [0031](./0031-credentials-are-protocol-kinds-the-credential-owns-its-scope.md).

## Context

A workshop attendee wanted Claude Code on Amazon Bedrock in a foldyard box, and foldyard had no
way to say so: `[claude]` assumed Anthropic's API. Claude Code reaches Bedrock three ways:

1. **The AWS credential chain**, every request SigV4-signed in the client.
2. **A Bedrock API key** in `AWS_BEARER_TOKEN_BEDROCK`, sent as `Authorization: Bearer …` with no
   signing. A long-term key belongs to an IAM user (AWS: "exploration only"); a short-term one is
   a SigV4-presigned `CallWithBearerToken` URL, base64-encoded, that carries the signer's
   permissions for up to 12 hours.
3. **Gateway mode** (`CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`): unsigned requests, for something in
   front to sign.

Option 3 looks like the proxy's job, but SigV4 signs a hash of the body, and the proxy writes
credentials at `requestheaders`, before the body exists, with `stream_large_bodies` on — the
2026-09-23 fix for a >1 MiB conversation reaching Anthropic with the dummy. Re-signing would mean
buffering every Bedrock request and a new per-request rule type in the addon. Option 2 is the
header rewrite ADR-0008 already does.

The attendee's next step is likely to be AWS for the app containers too, as Tangible uses GCP: the
metadata emulator gives each container its own short-lived identity. That needs room in the
config now, so the first cut doesn't have to be undone.

## Decision

1. **The provider is a table; the credential is `keyless`.** `[claude.bedrock]` (`region`,
   required) points Claude Code at Bedrock: `CLAUDE_CODE_USE_BEDROCK=1` and `AWS_REGION` in the
   box. `keyless` still says where the credential comes from, and `"bedrock"` is a third value
   beside `"api-key"` and `"oauth"`. An Anthropic credential with the Bedrock table is refused at
   launch, as is `keyless = "bedrock"` without it.
2. **Keyless Bedrock is the bearer key at the proxy.** The box holds a dummy
   `AWS_BEARER_TOKEN_BEDROCK`. `claude=on` writes the real bearer on the region's two hosts:
   `bedrock-runtime.<region>` for model calls and `bedrock.<region>` for the inference-profile
   lookups. Each host is one rule, minting on its own. The held answer, `fy verify`'s dummy check
   and the axis all work as for the Anthropic modes. Verify also fails if any `AWS_*` key is set.
3. **The key is minted from your own AWS login when you name a profile.** With
   `[claude.bedrock].profile`, `plugins/bedrock_token.py` asks the AWS CLI v2 for the profile's
   credentials (`aws configure export-credentials`: SSO, `aws login`, roles and static profiles
   alike). It presigns the token itself; the result is byte-identical to AWS's generator, pinned
   by a known-answer test. Each key is signed for an hour and reported to the proxy for at most
   55 minutes, so a lapsed session stops working within minutes. A capability probe shows the
   lapse on the switch. Without `profile`, a long-term key comes from `host.env` through
   `static_token`. The command is fixed package code, like `gh-cli`'s `gh auth token`. Config
   names only which profile, so ADR-0023 holds.
4. **Later: AWS identity is a metadata twin of `gcp`, and Claude rides it without keyless.**
   Without `keyless`, a `[claude.bedrock]` box resolves AWS credentials itself. Today that means
   `/setup-bedrock` in the box. Later, an `aws` axis would run an on-network emulator of the
   container-credentials endpoint (not IMDS: no link-local address to route, no token dance)
   behind a host minter. The minter would assume an allowlisted role per container label, as
   `gcp.serviceAccount` does. Claude Code then signs its own requests with ≥15-minute role
   credentials, and the proxy needs no signing.

   Two things are already in place for it. `awsauth.py` holds the host half — a profile's
   credentials and SigV4 presigning — with no Bedrock in it. And nothing in `[claude.bedrock]`
   names a credential source.

   Two things are left for the twin to settle. The SDKs accept a plain-http credentials URL only
   on loopback and the ECS/EKS link-local addresses, so delivery is a `credential_process` in the
   box's AWS config, or TLS under the box's CA. And it must choose between the proxy minting role
   credentials or calling `aws sts assume-role`.

## Consequences

- Claude Code on Bedrock is config only: the table, `keyless`, and `fy mode claude=on`. The real
  credential stays on your computer in both key modes.
- `awsauth.py` is foldyard's first SigV4 code. It is stdlib, and tested only against AWS's
  generator offline. The live path (a real account, an SSO session lapsing mid-session) is
  unexercised, hence the experimental entry.
- An organisation that denies `bedrock:CallWithBearerToken` can't use keyless Bedrock. It needs
  decision 4, or option 3's signing proxy, which this ADR declines for now.
- Two mints per key period (one per host). The AWS CLI runs twice an hour: cheap, and it keeps the
  addon's one-rule-one-cache model.
- Changing `[claude.bedrock]` changes the box's environment, so it needs `fy box down && fy box
  up`. The switch itself doesn't, because the dummy is ambient (ADR-0008, ADR-0031 decision 5).

## Rejected alternatives

- **SigV4 re-signing at the proxy** (gateway mode). Covers bearer-denying organisations, but it
  buffers large request bodies the proxy now streams, adds a per-request rule type to the addon,
  and moves a signing loop onto the event loop's hot path. Revisit if the twin doesn't land and
  the denial turns out to be common.
- **Two `[[inject]]` rows** (already possible). Works for a long-term key, but each row derives its
  own `FY_INJECT_<SWITCH>`, so it means two switches and the key stored twice. It can't mint
  short-term keys, and `[claude]` would still recommend and seed the Anthropic hosts.
- **A `bedrock` `[[inject]]` kind** instead of a keyless value. Cleaner for a non-Claude Bedrock
  client in the box, but Claude needs `CLAUDE_CODE_USE_BEDROCK` and the region in its own box env
  anyway. The kind can come later, sharing `bedrock_token` and `awsauth`.
- **Mounting `~/.aws` or exporting keys into the box.** A long-lived credential where the agent
  runs, which ADR-0008 exists to end.
