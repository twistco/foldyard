# cq: design notes, findings and follow-ups

Contributor notes for foldyard's [cq](https://github.com/mozilla-ai/cq) integration: why it is
shaped the way it is, what we found out about cq and its neighbours while building it, and what
is still open. The user-facing description is the cq section of
[configuration.md](./configuration.md); the code is `plugins/cq.py` (box wiring, the `cq` axis),
`cqhook.py` (session hooks) and `cqbackfill.py` (`fy cq backfill`). Findings are dated: cq and
cq.exchange move quickly.

## What foldyard does, and why

- **Hosted cq.exchange only.** `keyless.CQ_ADDR` is fixed to `https://cq.exchange`, on purpose:
  the hostname decides where the egress proxy adds the operator's cq key, so it must not be
  something the repo can change. A self-hosted node would need that address to come from the
  ADOPTED config, with the key injected for its API host (see the follow-ups).
- **The key never enters the box** (the `cq` axis, ADR-0031's keyless pattern). With `cq=off`,
  cq is anonymous and its writes wait in its local store.
- **Review happens in cq.exchange's queue.** A proposal lands `pending` and `private`, and `query`
  returns approved units only, so a proposed unit is invisible to agents (checked live
  2026-10-16: absent from an anonymous query, from a keyed query by its author, and from
  `cq status`) until someone approves it there. `fy cq backfill` therefore proposes straight away
  rather than adding a review step of its own (operator's choice, 2026-10-16).
- **Backfill mines where the transcripts are, and proposes where the key is.** On the host it
  mines this project's host sessions with the operator's own `claude` and hands only candidates
  to the box (`.devbox-cq/candidates/`); the box proposes them through the proxy. Host
  transcripts never enter the box, and the host never runs `cq`. Because the host miner runs
  with the operator's login and writes a file the box reads, its instructions are cq's reflect
  prompt as shipped in `assets/cq/` (pinned to `CQ_VERSION`), never anything from the checkout,
  and a candidate containing a `host.env` value is withheld.
- **cq records no provenance.** A unit has no field linking it to the session it came from, and
  the API cannot answer "was this session reflected on?". foldyard keeps its own record
  (`.devbox-cq/backfill.json`, keyed `agent:session-id`), shared by host and box.
- **Duplicates are merged before proposing.** The same learning turns up in every session that
  worked on that problem (a 3-session dry run returned two learnings twice each), so one more
  locked-down call folds them, and is refused unless it accounts for every input exactly once.

## Findings (2026-10-16)

### Tiers on cq.exchange

The live app has an organisation tier, but no one can create one themselves. Its JavaScript bundle
carries `/organization`, `/invitations/accept` and `/admin/orgs`, four roles (owner, admin,
reviewer, member) and an org review queue; the Terms (14 July 2026) speak of "a shared tenant
namespace". But `createOrg` is reachable only from the platform-admin page, invitations are
limited to the org's verified email domains, and accepting one is one-way: "your personal
workspace is disabled and does not return if you leave or the organization is deleted". Nothing
targets open-source projects. Promotion to the public commons is "handled by platform admins";
outsiders cannot nominate yet (Mozilla.ai blog, 21 May 2026). The GitHub README still lists org
namespaces as roadmap.

Untested: whether anyone but the author can see or approve a personal pending unit, and how an
org's review queue treats backfilled proposals. Worth knowing before pointing backfill at an org.

### Self-hosting

cq's open-source server (`server/`, FastAPI, SQLite by default or Postgres) has no organisations,
roles or per-user scoping: approved units are readable without logging in (`GET /knowledge` has
no auth dependency), every logged-in user sees the whole pending queue and can approve any of it,
and users are created by a seeding script (no sign-up, no OAuth). It does not federate with the
cq.exchange commons. Multiple teams is an open issue (mozilla-ai/cq#156).

Hosting it cheaply: Fly.io runs the published image unchanged (SQLite on a volume, about
$2–4/month); Vercel Hobby with Neon or Supabase is the realistic $0 option (non-commercial only;
small changes; no semantic search); Cloud Run with Neon runs it unchanged on a free tier. The cq
client gives up after 5 s per request, which rules out hosts that sleep (Render free) unless
migrations move out of startup. Cloudflare Workers would be a rewrite (no threads in Python
Workers; the 10 ms CPU limit against bcrypt).

### Alternatives

Nothing mature matches cq's model (structured learnings, proposed by agents, reviewed by humans,
shared across teams). The closest are small: acq (a cq fork adding curation and voting),
data-olympus (proposals merged by humans, in git), OpenKnowledgebase (a reviewed wiki),
team-memory-mcp (confirm/correct confidence, no gate). The large projects (Mem0, Letta,
Zep/Graphiti, Cognee, claude-mem, agentmemory, Basic Memory) are personal or app memory with no
review step; Cognee has the most complete access control to build on. Stack Overflow for Agents
(June 2026) is the nearest concept, but proprietary.

**Google's Open Knowledge Format (OKF)** (v0.1 June 2026, v0.2 July) is the interesting
neighbour: knowledge as markdown files with YAML frontmatter in a directory, and in v0.2
provenance (`sources`), a trust tier from `verified` (unverified, machine-confirmed,
human-reviewed), lifecycle (`status`, `stale_after`) and `generated: {by, at}`. A bundle in the
repo gives a project-level tier cq.exchange lacks: sharing is git, review is the pull request.
Tooling exists (e.g. okf-agent-memory: git-native, MCP, a bundle registry). The catch for
foldyard: the box can write the repo, so the merge to main is the trust boundary, as for
`CLAUDE.md`.

## Known gaps

- **Versions and upstream links.** Most backfilled learnings are library bugs or workarounds that
  hold for particular versions, and a cq unit has no version or link field (`context` is
  `languages`, `frameworks`, `pattern`). The miner is now told to state exact versions the
  transcript shows, and to say when the cause is a library bug, with the upstream issue URL if
  the session found one, all in the text; the merge keeps versions apart. Structured fields would
  need cq (see follow-ups).
- **Resumed sessions.** The record holds a session id, not how far it was mined, so a session
  resumed after mining is never mined again.
- **No check against the commons.** Reflect's own Step 5 queries cq for a close match before
  proposing; backfill doesn't, so a learning cq already has becomes a fresh pending unit.
- **Discovery needs a grant.** `cq.exchange` is offered at box-up but not granted by default, and
  cq reports the proxy's refusal only as "unexpected status 403", dropping foldyard's explanation.
- **Self-hosted nodes are not supported** (`CQ_ADDR` is fixed; see above).

## Follow-ups

- Propose structured version applicability and an `upstream` link for units on mozilla-ai/cq.
- Backfill could tag library-bug candidates and draft an issue for the library's own tracker.
- Store each session's mined length in the record and re-mine only what a resumed session added.
- Optionally pre-check candidates against cq (`cq query`) and drop or `confirm` close matches.
- If self-hosting becomes a need: `CQ_ADDR` from the adopted config, its API host as the injection
  target, and the egress grants to match.
- Consider an OKF output for backfill: candidates as unverified concepts in the repo, reviewed by
  pull request, alongside or instead of cq.
