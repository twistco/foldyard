# ADR-0031 — Credentials are protocol kinds plus data; the credential owns its scope

- **Status:** Accepted (2026-10-05). Amends
  [ADR-0023](./0023-no-host-executed-code-from-the-repo-mount.md) (its "`[plugins.github].permissions`
  is capped by the package default" rule is withdrawn) and extends
  [ADR-0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md)'s "data, not classes" from
  axes to credentials. The open questions were settled on acceptance (see
  [Settled](#settled)): no `permissions` narrowing; a clean break, `[plugins.github]` refused with
  its replacement rows; the agent plugins and GCP left for later. Implemented: decisions 1, 2, 3
  and 6, and of 5 the `box_env` dummies with their `fy verify` row and the held-credential answer
  (`plugins/kinds.py`, `plugins/inject.py`, `credscope.py`; the github plugin is deleted).
  Decision 4 (overlapping rules refused) landed in #54 (`Registry.inject_overlaps`).
- **Sources:** the 2026-10-05 incident in a consumer's box (below); the host supervisor log of that
  morning. Related: [0007](./0007-credential-injection-at-egress-proxy.md) (credentials are
  attached at the proxy), [0008](./0008-keyless-agent-auth.md) (the dummy-in-the-box pattern),
  [0014](./0014-consumer-supplied-box-image.md) (the consumer owns the box image),
  [0022](./0022-host-runs-the-adopted-config.md) (the host runs the adopted config; reporting is part
  of the fix).

## Context

**The incident.** An operator gave their GitHub App read access to Actions and Checks, and an org
owner accepted the change on the installation. An agent in the box still got 403 from every CI
endpoint, because foldyard's `github-app` minter asks GitHub for a token scoped to
`{pull_requests: write, issues: write}` whatever the App holds. The agent then added
`actions`/`checks` to `[plugins.github].permissions` and the operator adopted it. From then on every
GitHub call returned 401: the minter refused the map before contacting GitHub
(`$GH_APP_PERMISSIONS may only NARROW the default (issues, pull_requests) — 'actions' isn't one of
them`), the proxy forwarded the box's dummy token, and GitHub answered "Bad credentials". The agent
spent the morning blaming the App installation. The installation had in fact granted both
permissions; the two refusals were foldyard's own, and the reason sat only in the host's supervisor
log. (Answering a failed mint in the proxy instead of forwarding the dummy is fixed separately; it
applies to every rule and doesn't depend on this decision.)

**Why the cap existed.** It was added on 2026-08-13, when the host still read `foldyard.toml` live
from the checkout. Anything that could write the checkout, the box included, could then have set
`permissions = { contents = "write" }`. GitHub caps a token at what the installation holds, which
didn't help when the App held more than the box should have. So the package default became the
ceiling. [ADR-0022](./0022-host-runs-the-adopted-config.md) then closed the channel itself: a tree
edit is inert until an operator adopts it. The cap now guards a door that adoption already guards,
and it has a cost the incident made visible. The box's GitHub scope is decided in **three** places
(the App's settings, foldyard's hardcoded ceiling, the consumer's `permissions` map) that have to
agree, and only one of them (the App) is where the operator went to change it.

**The wider pressure.** The GitHub support is two modules: `plugins/github.py` (483 lines) and the
`github_app_token` minter (289). Sorting the plugin's hooks by what they know about:

| knows about | hooks |
| --- | --- |
| the **protocol** (an App JWT exchanged for an installation token) | the minter; `secrets` (the PEM); the `doctor` PEM shape check; the capability probe |
| **GitHub the service** | the three-level switch (`off`/`app`/`user`); `env_defaults` (app id, installation id, repo, permissions); `derive_env` (`GH_INJECT`); `box_args` (the dummy `GH_TOKEN`); `box_bootstrap` (installing `gh`); `verify_checks` ("comment-only App token"); `box_doctor_checks`; `doctor_fixes` |

The first row can't be data. ADR-0023 forbids a minter command in config, so a JWT exchange has to
ship as package code. The second row is a service integration, and every service an agent needs
(trackers, registries, CI providers, design tools) would argue for one of its own. Each one
carries policy foldyard then has to maintain, such as the permission list this incident tripped on.

The agent plugins read differently. In `claude.py` and `codex.py` the credential half is already a
kind plus data: a static token through the same wiring `[[inject]]` uses, or the Codex ChatGPT
refresh minter. Their other half (the agent's home volumes, `CLAUDE_CONFIG_DIR`, bound-out
transcripts, egress recommendations) is box ergonomics for an agent, not credential handling, and
this ADR leaves it alone.

## Decision

1. **A credential is an `[[inject]]` rule with a `kind`.** A kind is a token **protocol**, shipped
   as package code with a fixed shape of data fields: `static` (the default, today's behaviour),
   `github-app` and `gh-cli`. (`codex-chatgpt` stays inside the codex plugin for now — see
   [Settled](#settled), 3.) A new service whose token is static needs only config.
   A new protocol is a new kind, upstreamed or shipped as an entry-point plugin, as ADR-0023 already
   says. The secret's name stays derived from the switch (`FY_INJECT_<SWITCH>`), so a repo edit
   can't aim a rule at another rule's secret.

   ```toml
   [[inject]]
   switch = "github"              # `fy mode github=on`; PEM in host.env as FY_INJECT_GITHUB (base64)
   host = "api.github.com"
   kind = "github-app"
   app_id = "4008762"
   installation_id = "139125083"
   repositories = ["Tangible"]    # optional: a subset of the installation's repos

   [[inject]]
   switch = "github-write"        # a second App, with write access, kept on a timer
   host = "api.github.com"
   kind = "github-app"
   app_id = "…"
   installation_id = "…"
   emergency = true
   ```

2. **The credential owns its scope; foldyard neither narrows nor caps it.** For `github-app`, the
   App installation's permissions *are* the box's scope. For a different scope, the operator creates
   a different App and gives it its own switch. `[plugins.github].permissions` goes with the
   whole table, which joins `exposure.IGNORED_KEYS` and stops the launch (see
   [Settled](#settled), 2), so a config still carrying it is told so. `repositories` stays as data:
   GitHub refuses a repository outside the installation, so it can only narrow. This puts the scope
   decision somewhere nothing in the checkout can reach, and where widening needs an org owner to
   accept GitHub's own permissions review. That is a stronger anchor than an adopted file, and
   adopting a file was already the only gate the cap still added.

3. **foldyard reports the scope instead of enforcing it.** The `github-app` kind's capability probe
   already authenticates as the App. It also reads the installation's permissions
   (`GET /app/installations/{id}`), and the switch's status and `fy config widenings` show them,
   flagging any `write`/`admin` level. When the permissions differ from the last probe, the
   supervisor logs one line and raises one notification, as it does for adopted-config drift. A
   widening on github.com then can't happen silently, even though no foldyard file changed.

4. **Two active rules may not inject on the same host and path.** The `off`/`app`/`user` switch got
   mutual exclusion for free; independent switches don't. `fy mode` refuses to turn a rule on while
   another active rule overlaps it, and names that rule. This generic check replaces the
   service-shaped switch.

5. **What every rule gets, whatever its kind:** a dummy in the box's environment (a `box_env` field,
   e.g. `{ GH_TOKEN = "x" }`, so turning the switch on needs no box recreate); the held-credential
   answer while the switch is off; the mint-failure answer when its minter fails; and a `fy verify`
   row asserting that no real credential is in the box's environment. **What a kind adds:** its
   minter, an offline shape check for its secret, and optionally a capability probe.

6. **Service tooling is the consumer's.** Installing `gh` moves to the consumer's box image
   ([ADR-0014](./0014-consumer-supplied-box-image.md)). foldyard's image-agnostic installer step
   goes with it.

## Consequences

- **The incident can't recur.** With one place deciding scope, accepting the permissions on the
  installation is the whole change. A token that still can't reach an endpoint gets GitHub's own
  403, naming the missing permission, and that permission is on the App's settings page.
- **A new service no longer needs a plugin.** A static token is a few lines of `[[inject]]`, and a
  new token protocol is one kind. `plugins/github.py` is deleted, not kept as a shim (see
  [Settled](#settled), 2). The kinds stay a closed set of fixed shapes, which is the boundary
  ADR-0023 drew against config-DSL creep: no claim templates, no JSON paths.
- **`fy verify` claims less, and says so.** Today it reports a "comment-only App token". Under this
  decision foldyard can't know that, because an App with `contents: write` would be a deliberate
  choice. Pushes over git are still refused: injection is on `api.github.com` only, and the core's
  push-refused check is unchanged. A write through the REST API (the contents or git-data
  endpoints), however, is possible exactly when the operator's App grants it. Verify keeps
  asserting that no real credential is in the box and points at the reported scope (decision 3).
- **Trust moves to how Apps are set up.** An App shared with an unrelated workflow (a release bot,
  say) hands the box that workflow's permissions. The docs must say plainly: one App per purpose,
  installed on only the repositories it serves. Decision 3 makes a shared App visible, but
  doesn't prevent it.
- **More Apps, more keys.** Each scope is another App to create, another installation for an org
  owner to approve, and another PEM in `host.env`. That's the price of the scope living in one
  place, and the approval step is the review that place provides.
- **Breaking for consumers of `[plugins.github]`**: the switch name, its levels and the key all
  change. `fy mode github=app` becomes `fy mode github=on`, and the `user` emergency level becomes
  its own `gh-cli` rule with `emergency = true`.

## Rejected alternatives

- **Raise the ceiling to admit read-only CI permissions** (`actions`, `checks`, `statuses` at
  `read`). It fixes this incident and keeps the shape that caused it: a GitHub permission list in
  foldyard that each new need edits, one service at a time.
- **Trust the adopted `permissions` map, with no ceiling.** Safe under ADR-0022, but the map
  remains a second place that has to agree with the App. That mismatch is what failed here, and a
  per-service field like it is the start of the slope.
- **A host-owned ceiling** (a setting outside the checkout that raises the cap). The strictest
  option, and it adds a third place rather than removing one.
- **Inherit the App's scope without reporting it.** Simpler than decision 3, but a permission
  added on github.com, months later and for another reason, would then reach the box with no
  line anywhere saying so.

## Settled

The open questions, as the operator decided them on acceptance (2026-10-05):

1. **Optional `permissions` narrowing as data? No.** It could only narrow below the App, so it was
   safe — but it is the second place that has to agree with the App, which is what this decision
   removes, and a separate App already gives a narrower scope. A row carrying `permissions` is
   refused like any field its kind doesn't take.
2. **Clean break or a one-release shim? A clean break.** As ADR-0023 did with `[[inject]] minter`:
   a `[plugins.github]` table (adopted, or still in the tree) makes the launch gate refuse, and the
   refusal prints the `[[inject]]` rows that replace it, computed from the table — a `github-app`
   row with the old `repo` as `repositories` and `box_env = { GH_TOKEN = "x" }`, and a commented
   `gh-cli` emergency row for the old `user` level — along with the PEM's new `host.env` name
   (`FY_INJECT_<SWITCH>`) and why `permissions` has no successor. The table is also an
   `IGNORED_KEYS` entry, so the doctor row and `fy config widenings` name it. The known consumers
   are few; a shim would have kept the plugin alive for a release for little.
3. **The agent plugins' credential halves: not in this change.** `claude.py` and `codex.py` keep
   their keyless wiring as it is, so the user-facing kinds are `static`, `github-app` and `gh-cli`,
   and the Codex ChatGPT refresh stays internal to the codex plugin. Moving them is a later step,
   with their held-credential answers' provider-specific error shapes as kind data.
4. **GCP stays out of scope.** It emulates the metadata server rather than injecting at the proxy;
   the same "the credential owns its scope" reasoning about its service-account allowlist would be
   a separate decision.
