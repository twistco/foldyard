# GitHub access with a GitHub App

The box has no GitHub credential by default. To let an agent in it read a pull request, answer
review comments or check CI, give it a **GitHub App** through a `github-app` `[[inject]]` row.
Once the switch is on, `gh` in the box works against GitHub's API as the App. The App's private
key stays on your computer, the token is minted there, and the proxy adds it to requests in
flight, so the box only ever holds a placeholder.

This page walks through it once, end to end. The row's full reference is in
[configuration.md](./configuration.md#inject); how switches work is in [modes.md](./modes.md).

## Why an App

- **You choose exactly what it can do.** An App has its own permissions, set per area (pull
  requests, issues, checks…), and is installed only on the repositories you pick. A personal
  token carries everything you can do.
- **Its tokens are short-lived.** foldyard mints an installation token on your computer, good for
  an hour, and mints a fresh one before it expires. Nothing long-lived ever reaches the box.
- **The installation's permissions are the scope.** foldyard asks GitHub for no narrower
  permissions and caps none: what the App installation grants is what the box can do
  ([ADR-0031](./adrs/0031-credentials-are-protocol-kinds-the-credential-owns-its-scope.md)). The
  one place to decide it is the App's settings on github.com, where widening it needs an org
  owner's approval. foldyard reports what it sees, and tells you when it changes
  ([below](#seeing-the-scope)).
- **It belongs to no one person.** Comments and replies show as the App, and removing access is
  one uninstall.

One App per purpose. If two jobs need different permissions, make two Apps, each with its own row
and switch: a shared App hands the box both jobs' permissions.

## 1. Register the App

On github.com, create the App under the account that owns the repositories: for an organization,
its **Settings → Developer settings → GitHub Apps → New GitHub App** (an org owner does this, or
someone they've made an App manager); for your own repositories, the same under your personal
settings.

- **Name** and **Homepage URL**: anything; the URL is not used.
- **Webhook**: untick **Active**. foldyard never receives events.
- **Where can this GitHub App be installed?**: **Only on this account**.
- **Repository permissions**: only what the job needs. **Metadata: Read-only** is always added.

| job | repository permissions |
| --- | --- |
| read and answer PR reviews and comments | Pull requests: Read and write · Issues: Read and write (a PR's conversation comments go through the issues API) |
| open pull requests | Pull requests: Read and write (the branch must already be on GitHub) |
| read CI results | Checks: Read-only · Actions: Read-only · Commit statuses: Read-only |
| read code through the API | Contents: Read-only |

Leave everything else at **No access**, in particular **Contents: Read and write**,
**Administration**, **Workflows** and **Secrets**. Contents write would let the box change
branches through the API even though `git push` from the box doesn't work (see
[what works](#what-works-from-the-box)).

After creating it, note the **App ID** on its General page (a number; not the Client ID). Then,
further down the same page, **Generate a private key**. Your browser downloads a `.pem` file, and
GitHub keeps no copy: you'll paste it into foldyard in step 4.

## 2. Install it on only the repositories it needs

On the App's page, **Install App**, pick the account, then **Only select repositories** and choose
the repository (or few) the box works on. After installing, the page's address ends in the
**installation id**: `…/settings/installations/7654321`.

## 3. Add the `[[inject]]` row, and adopt it

In `foldyard.toml`:

```toml
[[inject]]
switch = "github"                  # `fy mode github=on`; the key is host.env's FY_INJECT_GITHUB
kind = "github-app"
app_id = "123456"
installation_id = "7654321"
repositories = ["my-repo"]         # optional: narrow the token to some of the installation's repos
box_env = { GH_TOKEN = "x" }       # so `gh` in the box sends a header for the proxy to replace
```

- `app_id` and `installation_id` are identifiers, not secrets, so they're committed.
- `repositories` takes bare names, not `my-org/my-repo`. It can only narrow: GitHub refuses a
  repository the installation doesn't cover.
- There is no `host` to set: the kind always injects on `api.github.com` and refuses any other.
- There is no `permissions` key, and foldyard refuses to load a row with a field the kind doesn't
  take.

The switch name also names the secret: `FY_INJECT_<SWITCH>`, uppercased, so `FY_INJECT_GITHUB`
here. You can't choose another variable, so a repo edit can't point the row at a different
secret.

The row tells your computer where to send a credential, so it does nothing until you **adopt**
it: answer the prompt at your next `fy up`, or read `fy config diff` and run `fy config adopt`
([the adopted config](./configuration.md#the-adopted-config-what-the-host-actually-runs)). An
edit made in the box changes nothing until you do.

`box_env` is baked in when the box is created. If the box was already running, recreate it once:
`fy box down && fy box up`. After that, turning the switch on or off needs no recreate.

Installing `gh` in the box is your box image's job.

## 4. Switch it on, and give it the key

On your computer:

```bash
fy mode github=on
```

The first time, foldyard sees that `FY_INJECT_GITHUB` is missing and asks for it on your terminal
before switching, with a hidden prompt. `host.env` holds one `KEY=VALUE` per line, so the key goes
in as single-line base64:

```bash
base64 < my-app.private-key.pem | tr -d '\n'
```

Paste the output. foldyard checks it decodes to a whole private key (a half-pasted one is refused
and nothing is stored) and writes it to `host.env` in `~/.foldyard/<project>/`, readable only by
you. You can also add the line there yourself; the next mint reads it. Then put the `.pem`
somewhere safe, such as your password manager, or delete it: you can always generate another.

If your team keeps the key in a vault, retarget the prompt's "get it with" hint with a `[[secret]]`
row naming the same variable. The hint is printed for you to run, never run by foldyard:

```toml
[[secret]]
var = "FY_INJECT_GITHUB"
how = "op read op://dev/github-app/private-key | base64 | tr -d '\\n'"
```

`fy doctor` on your computer shows a `github PEM` row (is the key there?) and a `github PEM shape`
row (does it decode to a key?). Both are offline checks. Whether GitHub accepts the key is the
switch's capability check, below.

## 5. Check it from the box

In the box:

```bash
fy doctor     # "github injection — token reaching requests (rate limit 5000/h)"
fy verify     # GH_TOKEN is the dummy: the real credential stays on your computer
```

The `github injection` row asks GitHub for its rate limit. An anonymous caller gets 60 requests an
hour and the App far more, so the limit alone shows whether the token is really being added. If it
says "NOT injected" while `fy mode` shows the switch on, run `fy host restart` on your computer;
`fy doctor` there names the credential's problem.

Then try what the App is for, with REST calls through `gh api`:

```bash
R=repos/my-org/my-repo
gh api $R/pulls/42 --jq '{title, sha: .head.sha, mergeable_state}'
gh api $R/pulls/42/reviews --paginate --jq '.[] | {user: .user.login, state, body}'
gh api $R/pulls/42/comments --paginate --jq '.[] | {id, path, line, body}'   # inline comments
gh api $R/issues/42/comments --paginate --jq '.[] | {user: .user.login, body}'
gh api -X POST $R/pulls/42/comments/<comment-id>/replies -f body='Fixed in <sha>.'
gh api $R/commits/<sha>/check-runs --jq '.check_runs[] | {name, status, conclusion}'
```

Replies are public and signed by the App.

GitHub names the permission an endpoint needs in each response's `X-Accepted-GitHub-Permissions`
header. `gh api -i <endpoint>` shows it, which is a quick way to see whether a call fits the App
before widening anything. Finally, confirm what should fail does fail: `git push` from the box,
or a call that needs a permission you didn't grant (a 403).

## What works from the box

- **REST through `gh api` works**, within the App's permissions.
- **`gh pr view --json …` may not.** `gh pr view` and `gh pr list` use GitHub's GraphQL API, and
  fields such as `reviews`, `commits` and `statusCheckRollup` go through commit objects that need
  **Contents: Read**. Without it the whole query fails with "Resource not accessible by
  integration". Use the REST calls above, or grant Contents: Read if the App should read code
  anyway.
- **`git push` never works through this row**, whatever the App holds. The token is added only on
  `api.github.com`; git talks to `github.com`, which gets nothing. Push from your computer. The
  same goes for a PR: push the branch from your computer, then the box can open the PR with
  `gh api -X POST $R/pulls -f head=<branch> -f base=main -f title=… -f body=…`.

When a call fails:

- **403 "Resource not accessible by integration"**: the token got there, but the App lacks that
  permission. The `X-Accepted-GitHub-Permissions` header names it. Change the App on github.com,
  not `foldyard.toml`.
- **401 starting ``foldyard: `github` is off``**: the switch is off. The proxy answered and sent
  nothing upstream. Run `fy mode github=on` on your computer.
- **502 "foldyard could not mint a credential for api.github.com …"**: minting failed on your
  computer, and the message quotes why. `fy host logs` there has the details.

More on these answers: [modes.md](./modes.md#keyless-agents).

## Seeing the scope

While the switch is on, its capability check runs every 5 minutes. It signs in as the App (minting
nothing), checks the key works and the installation still exists, and reads the installation's
permissions. A failing check shows the switch as DEGRADED in `fy mode`, naming the fix: a rotated
or wrong key, an uninstalled App (`installation … not found`), or a suspended installation
([DEGRADED](./modes.md#degraded--the-switch-is-on-but-the-credential-stopped-working)).

The permissions it read are recorded on your computer. `fy config widenings` shows them under the
row, with any `write` or `admin` permission flagged:

```text
    api.github.com   ← [[inject]] github (github-app; App private key: $FY_INJECT_GITHUB)  [ON]
      …
      scope (probed 2026-10-17T09:12:00+00:00, 3m ago): checks:read, issues:WRITE, metadata:read, pull_requests:WRITE — selected repositories
      ⚠ write or admin: issues, pull_requests — what the box can change through this credential
```

`fy mode` and `fy tui` show the same reading beside the switch. Both read the record and never call
GitHub. Before the switch has first been on, the row says "not yet probed". When the permissions
change, say after an org owner accepted a new one on github.com, the supervisor logs one line
naming what changed (`fy host logs`) and sends one desktop notification.

## Rotating the key, and switching it off

To rotate: on the App's page, generate a new private key, replace the `FY_INJECT_GITHUB` line in
`~/.foldyard/<project>/host.env` with the new key's base64, and check `fy mode` doesn't show the
switch DEGRADED (the check runs every 5 minutes). Then delete the old key on GitHub. A token
already minted lasts at most an hour.

To stop access: `fy mode github=off` stops the proxy adding the token at once. To take it away for
good, uninstall the App or remove the repository from the installation on github.com.

## Gotchas

- **An installation can't have zero repositories.** GitHub needs at least one selected
  repository; to cut every repository off, uninstall.
- **Changing permissions needs acceptance.** After you edit the App's permissions, an org owner
  accepts them on the installation before the token carries them.
- **Managing the installation's repositories through the API** (for example from
  infrastructure-as-code) needs a classic personal access token: fine-grained ones get a 403 on
  that endpoint. The install page on github.com always works.
- **Only one credential per host and path at a time.** Two rows that inject on `api.github.com`
  (two Apps, or an App and the `gh-cli` row below) can't both be on. `fy mode` refuses the second
  and names the first; swap in one command, for example `fy mode github=off github-user=on`.

## Other ways in

- **`kind = "gh-cli"`** injects **your own** `gh` token (`gh auth token` on your computer): full
  user authority, writes to anything you can write included. It must be `emergency = true`, so
  each `on` expires (`fy mode github-user=on ttl=30m`). Keep it for the rare task the App can't do,
  and switch it off after.
- **`kind = "static"`** echoes a long-lived token from `host.env`, such as a fine-grained personal
  access token, with `host = "api.github.com"` and `value_prefix = "Bearer "`. It works, but the
  token lives long and acts as a person, which is what an App avoids.

Both are in [configuration.md](./configuration.md#inject).
