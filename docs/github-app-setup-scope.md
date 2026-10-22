# GitHub App setup in one command — design sketch

**Status: proposal.** Nothing here is built, and the verb names are placeholders. The manual
walkthrough it would shorten is [github-app.md](./github-app.md).

## What it replaces

Steps 1–4 of the walkthrough are by hand: tick a dozen permission boxes, untick the webhook,
generate and download a private key, base64 it, paste it, install the App, read the installation
id out of the address bar, and write the `[[inject]]` row. The mistakes it invites are the ones
that matter: a permission wider than the job (Contents: write is one click away), the Client ID
where the App ID belongs, a `.pem` left in `~/Downloads`.

GitHub's [App manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
lets a tool pre-fill the registration, so the operator reviews it and clicks once. foldyard can
then collect the key and the installation itself, and hand back the row.

## The flow

```bash
fy github-app create [--org twistco] [--job review] [--read-code] [--switch github] [--name …]
```

1. **Host only, interactive.** It refuses inside the box (`config.in_box()`) and without a TTY.
2. **The manifest comes from foldyard's code.** Nothing from the checkout: permissions and
   URLs decided by `foldyard.toml` would be repo config choosing what the host registers
   ([ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)). The manifest:
   - `name` (default `<project>-foldyard`, editable on GitHub's page), `url` (the docs site:
     the field is required);
   - `hook_attributes: {url: "https://example.invalid/", active: false}` (a hook's `url` is
     required even when inactive);
   - `public: false`, `default_events: []`, `default_permissions` from the job preset (below);
   - `redirect_url: http://127.0.0.1:<port>/created`, and no `setup_url`, `callback_urls` or
     `request_oauth_on_install`.
3. **A one-shot listener** on `127.0.0.1` at a random port, with a random `state`
   (`secrets.token_urlsafe(32)`), alive for at most 10 minutes.
4. **The browser opens `http://127.0.0.1:<port>/start/<state>`.** It's a local page whose form
   POSTs the manifest to `https://github.com/organizations/<org>/settings/apps/new?state=<state>`
   (or `/settings/apps/new` for a personal account). The page exists because GitHub takes the
   manifest only as a POSTed form field, which a URL can't carry.
5. **The operator reviews it on GitHub**, may rename it, and clicks **Create GitHub App**.
6. **GitHub redirects to `/created?code=…&state=…`.** The listener compares `state` in constant
   time, answers "done, back to your terminal", and stops. It exchanges the code at once, since
   the code alone is the credential for the next call.
7. **The conversion.** `POST /app-manifests/{code}/conversions` takes no auth. From the reply
   foldyard keeps `id`, `slug`, `owner.login`, `permissions` and `pem`. It discards
   `client_secret`, `webhook_secret` and `client_id`, which are never printed or stored.
8. **It checks what was created.** The reply's `permissions` must equal the preset. If they
   don't, it says so loudly and lists both, and goes on.
9. **It stores the key.** The PEM is checked with `github_app_token.decode_pem`, then appended as
   base64 to `host.env` as `FY_INJECT_<SWITCH>` through `keyless.append_host_env`. An existing
   value is never overwritten without `--replace`, because that would be another App's key.
10. **The install.** It opens `https://github.com/apps/<slug>/installations/new`, where the
    operator chooses **Only select repositories**. foldyard polls `GET /app/installations` with
    the App's JWT (`github_app_token.app_jwt`) every 3 s, for up to 10 minutes. When the
    installation appears, it makes an installation token (`installation_token`), lists
    `GET /installation/repositories`, and warns if the selection is `all`.
11. **It prints the row** with `app_id`, `installation_id`, `repositories` (the chosen names) and
    `box_env`, and the next steps: paste it into `foldyard.toml`, adopt it, run
    `fy box down && fy box up`, then `fy mode github=on`. It doesn't edit `foldyard.toml`
    itself: that is repo config, and the adopt gate is the one way it reaches the host.

**Why polling, not a setup URL.** A `setup_url` is saved in the App's settings, so every later
install or reconfigure would bounce to a localhost port nobody is listening on. GitHub also warns
that the `installation_id` it appends can be spoofed. Asking GitHub with the App's own JWT avoids
both.

## Job presets

Metadata: read is always added. Nothing here goes past what the walkthrough's table lists, and
no preset holds Contents: write, Administration, Workflows, Secrets or Actions: write.

| `--job` | permissions |
| --- | --- |
| `review` (default) | pull requests: write · checks: read · actions: read · commit statuses: read |
| `review` + `--read-code` | the same, plus contents: read (makes `gh pr view --json reviews` work) |
| `issues` | `review`, plus issues: write |

Anything else is changed on GitHub after creation. That friction is deliberate: widening goes
through the App's settings page, where the capability check sees it and
`fy config widenings` reports it ([ADR-0031](./adrs/0031-credentials-are-protocol-kinds-the-credential-owns-its-scope.md)).

## Reusing an App

One App has at most one installation per account, so a second repository joins the existing
installation rather than getting its own. Each project's row still narrows its tokens with
`repositories`.

```bash
fy github-app use --app-id 4008762 --installation-id 139125083 [--key-from tangible]
```

- **Adding the repository.** It opens the installation's configure page, because adding a
  repository needs a person: the App's own credentials can't, and fine-grained tokens are refused
  there. foldyard polls the installation's repositories until this one shows up.
- **The key.** It prompts for it, as `fy mode github=on` does. Or `--key-from <project>` copies
  `FY_INJECT_GITHUB` from that project's own `host.env`: both files belong to the operator, on
  the same computer.
- **The row.** It prints it, as above.

## Security properties

- **The listener treats every request as hostile.** The VM can reach the host's loopback on some
  setups (Lima's user-mode network forwards `192.168.5.2` to the host; the VM wall opens only the
  daemon port band there, so an unwalled VM is the exposed case). Both paths carry the unguessable
  `state`, and a request with a wrong path or state gets a 404 without ending the wait. One good
  callback ends it.
- **The key is never printed and never on a command line.** It goes from the reply into
  `host.env` and nowhere else.
- **No elevation.** It needs no root ([ADR-0028](./adrs/0028-no-elevation-on-the-host-operator-applies.md)),
  and runs no code from the checkout ([ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md)).
- **What's left over.** The redirect URL, code included, stays in the browser's history. The code
  expires within the hour and has been exchanged by then, but GitHub doesn't say whether it is
  single-use.

## Fallbacks

- **No browser, or a remote host over SSH.** It prints the start page's URL. Either forward the
  port (`ssh -L`) or use `--paste`: foldyard writes the start page to a local file to open
  anywhere, the redirect then fails in that browser, and the operator pastes the address-bar URL
  back. foldyard checks `state` and takes the code from it.
- **WSL2.** A Windows browser reaches the distro's `127.0.0.1` through WSL's localhost
  forwarding, which is on by default. `webbrowser.open` is tried first, and the URL is printed
  either way.

## Testing

- **Pure tests:** the manifest per preset and account type (golden JSON), the permission diff,
  and the printed row, which must pass `inject._validated`.
- **The listener, in-process:** a good state, a wrong state, a wrong path, and a timeout. GitHub
  calls go through an injectable opener, like `github_app_token._opener`, so the suite stays
  hermetic.
- **The docs.** `tests/test_agent_guide.py` fails if they name the verb before it exists.
- **Live, by hand.** GitHub needs a person to click, so the PR carries a checklist: create on a
  personal account, install on one repository, `fy mode github=on`, and a `github injection` row
  in `fy doctor` reporting the token.

## Open questions

1. **Names.** Is it `fy github-app create` / `use`, or a step in `fy init`?
2. **Presets.** Are these the right set? Should `--permission NAME=LEVEL` exist, with a denylist,
   or does the friction stay?
3. **The row.** Print it, as above, or write it into `foldyard.toml` and let the adopt prompt show
   the diff?
4. **Org registration.** Which roles may register an App for an org (owner, App manager)? GitHub's
   manifest pages don't say. If someone lacks the role, is the answer "ask an owner to run it", or
   "register it on your account and transfer it"?
5. **Unverified with GitHub:** the `installations/new` URL by slug, whether `redirect_url` is
   kept in the App's settings, and whether the code is single-use.
