---
section: Security
bump: patch
---

- **`fy code` no longer lets the box ask your computer for git credentials.** Dev Containers
  creates an IPC socket in the box on every attach. Its host side runs `git credential fill`
  (and the Docker credential helpers) on your computer for anything that asks, and no setting
  switches that off. So a process in the box could fetch, say, your keychain's GitHub token and
  push with it. `fy code` now launches VS Code with an empty git config, so there's no helper on
  your side for it to run. It also starts VS Code from the instance's own state directory rather
  than your checkout, whose box-writable `.git/config` could otherwise name a helper to run on
  your computer. Your git config comes back in the instance's local terminals.
- **`fy code` pins the rest of the editor's credential forwarders off**, at user and machine
  scope: `dev.containers.gitCredentialHelperConfigLocation` (`"none"`),
  `dev.containers.dockerCredentialHelper`, `dev.containers.copyGitConfig`,
  `dev.containers.githubCLILoginWithToken` and `github.gitAuthentication`. A consumer no longer
  needs `"github.gitAuthentication" = false` in its own `[vscode.settings]`.
- **`fy code` refuses to launch when it can't rewrite the instance's `settings.json`** (comments
  or invalid JSON in it). Before, it warned and launched with none of its pins. Fix the file or
  delete it, and the next `fy code` copies your own VS Code's settings in again.
- **The in-box reaper also deletes Dev Containers' and the `code` CLI's IPC sockets**, in
  `/tmp/user/*`, `$XDG_RUNTIME_DIR` and `$TMPDIR` as well as `/tmp`. A running box swaps its old
  reaper for the new one the next time `harden.sh` runs. `fy verify` checks the same places, and
  gains a row that fails when the box's git has a `credential.helper` calling back through Dev
  Containers.
- **The egress proxy refuses git pushes over HTTPS, on any host.** A push's two requests
  (`…/info/refs?service=git-receive-pack`, then `POST …/git-receive-pack`) get a `403` that git
  shows as `remote: refused by the foldyard proxy - the box doesn't push …`; fetches are untouched.
  "The box doesn't push" no longer depends on no credential ever reaching it — a token in a
  `.env`, a pasted one, or a bridge like the ones above. The refusal lifts while a push-capable
  mode is on (the emergency `gh-cli` switch) and returns when it turns off. The proxy can't see
  inside a `passthrough` tunnel, so `fy config widenings` now names the git hosts (`github.com`,
  `gitlab.com`, … — the `@vcs` bundle has them) where a push can't be refused. `fy verify` gains a
  row that sends a push's first request through the proxy and expects the refusal, and, where the
  proxy refuses its dry-run push, asks git for a credential for `origin` directly (`git credential
  fill`) and fails if one is there.
