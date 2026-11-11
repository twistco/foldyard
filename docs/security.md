# Security model — the boundary, the threat model, and `fy verify`

The [README's third idea](https://github.com/twistco/foldyard/blob/main/README.md#why) is *isolation
without hiding*: safety comes from a real boundary, not from concealment, and you can test the
boundary yourself. This page covers what the boundary is, what it defends against, what it doesn't,
and what `fy verify` proves. The reasoning behind each design call lives in the
[ADRs](./adrs/README.md). Terms are defined in the [glossary](./glossary.md).

## The boundary: a disposable VM that mounts only your repo

Everything that runs untrusted code — installs, build scripts, your compose stack, the agent — runs
inside **the VM**: a throwaway Linux virtual machine running rootless Podman that mounts **only your
repo** (plus its worktrees folder). No home directory, no other checkouts, no keys. Two properties
combine ([ADR-0001](./adrs/0001-rootless-podman-vm-isolation-boundary.md)):

- **Rootless engine.** A container's "root" maps to an unprivileged user *inside the VM*, so the
  classic `--privileged --pid=host` breakout to VM root is refused at the source.
- **Repo-only mounts.** The VM's mount table holds your repo and nothing else from your
  computer.[^mounts] Even with full control of the VM, there is no other file of yours to reach.

On the lima backend the VM also shares two directories of foldyard's own, both outside the repo and
both empty of secrets ([ADR-0033](./adrs/0033-a-to-box-and-a-from-box-beside-the-checkout.md)): a
**from-box** the box writes (agent transcripts leave the checkout through it; your computer reads
them without following a link) and a **to-box** your computer writes and the box can only read. The
to-box is read-only *outside* the guest — the hypervisor's share refuses the write — so even root in
the VM can't change what your computer published there; foldyard mounts it only on `vz` with
virtiofs and `qemu` with 9p, where that holds. Never the state directory above them, which holds
`host.env` and the keys.

\[^mounts\]: Strictly, the repo, its worktrees folder and — on the lima backend — the from-box and,
where the share can enforce read-only, the to-box, both described below: folders of foldyard's own
that never hold a secret. Wherever these docs say the VM "mounts only your repo", this is what they
mean.

So a container escape — a kernel bug, a runtime bug — lands the attacker in a disposable VM whose
only contents are a repo they could already read. Not on your computer, not near your keys, browser
profile or other projects. That is also why handing the agent the VM's container socket is safe:
what the socket can reach is bounded to the repo and the VM's containers.

**The VM is required on Linux too.** Rootless Podman run directly on Linux sees your whole
filesystem, so anything holding its socket could mount `~/.ssh` into a new container. The VM is what
limits the socket to "repo + containers", so there is no VM-less backend
([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)).

## Threat model

![What a bad package can reach: the same malicious install run in three places. On your laptop it reads your keys and tokens straight away, reaches your other repos and files, can push with your git credentials and sends data anywhere. In a typical dev container on a rootful engine, keys are often within reach and your other files are one escape away. In a sandboxed VM that mounts only the repo, keys and other files aren't there and a push is refused; with the VM firewall on ([machine] firewall = true, Lima backend), traffic leaves only through a logging proxy. The repo itself is reachable everywhere, so keep secrets out of it.](https://github.com/twistco/foldyard/raw/main/docs/assets/foldyard-blast-radius.svg)

What foldyard is built against — not the agent as such, but what comes in with development:

- **Malicious packages and install scripts** (`npm install`, PyPI, postinstall hooks). They run
  freely *inside the VM* and find: the repo (which the
  [clean-repo precondition](#the-precondition-a-clean-repo) keeps free of secrets), no SSH keys, no
  tokens, no credential helpers, no push access. Your home directory, other repos and long-lived
  secrets aren't inside, so they can't be reached.
- **Prompt-injected agents.** The agent has real autonomy — shells, the stack, the container socket
  — but everything it holds is bounded to the VM. A hijacked agent can damage the local checkout
  (recoverable); it can't push, delete the remote, or take credentials that were never there.
- **Build hooks and dev servers.** Anything a `just build` or `next dev` pulls in runs in the same
  VM under the same rules.

**The one overlap window.** If you switch on access (say `fy mode github=on`) and *then* run an
untrusted install step, install-time code and a live credential path overlap. The token still never
enters the box (see below), and it is short-lived and scoped — but while the switch is on, code in
the box can *use* that access. Keep switches off while installing things you don't trust yet.

**Worktrees share that window.** A project's worktrees run in one VM, and every box in it holds the
VM's container socket — so a box can reach its sibling worktrees' boxes, and the token services and
proxy listener of any worktree. Each worktree keeps its own mode, which stops a permission meant for
one branch being used on another *by accident*; it is not a wall between them. The VM is the
boundary: a switch on in one worktree is usable from every box of that project. Give work you don't
trust its own project (its own VM), not just its own worktree. The same socket lets a box read what
the VM user can, which includes the VM's whole journal.

### What code in the box can still reach on your computer

foldyard runs nothing from the repo on your computer: consumer scripts such as
`[project].worktree_init` run in a container, and where a script's output affects your computer (the
VS Code attach config, say), foldyard writes it itself from validated data. And **`foldyard.toml` is
not live input to your computer**: the supervisor runs from a copy you *adopted*, stored outside the
repo, so an edit to `[proxy]` or `[[inject]]` does nothing until you accept it at the next `fy up`
or `fy config adopt` ([ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)). Nor is the worktrees
folder: your computer keeps its own list of worktrees, written only by `fy worktree add`, and runs
nothing for a checkout that isn't on it — `fy worktree list` and `fy doctor` name any that aren't.
Three channels remain ([ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md)):

- **Git's own hooks run on your computer, and foldyard doesn't manage them.** `lefthook.yml`,
  `.git/hooks/*` and `.git/config` (aliases, and since git 2.54 `[hook]` entries) can be written
  from the box and run on your computer the next time you use git in that checkout. foldyard can't
  close this without owning your `.git`, so treat a shared checkout's hooks as code you are choosing
  to run. foldyard's own git on your computer (`fy worktree add` and `remove`) runs none of them,
  and no `core.fsmonitor` command either, and `fy worktree add` says when it skipped any. Nor does
  it check files out or read them on your computer, since git runs the filter drivers the shared
  config names (Git LFS's, or one planted from a box) for every file it checks out or compares:
  `fy worktree add` only records the worktree there and checks its files out in a container, and
  `fy worktree remove` checks for uncommitted work, and saves it, in a container too
  ([git](./git.md)). So foldyard's git on your computer runs no filter driver at all. It fetches
  nothing either. `.git/config` can turn the repo into a partial clone, whose missing objects git
  downloads on demand over a transport that same config chooses, such as an `ssh` command of its
  own. So every git `fy worktree add` and `remove` run on your computer has on-demand fetching and
  every transport switched off; the rest of foldyard's git there reads only refs and config, never
  an object, so it has nothing to fetch. On a git older than 2.44, which can't switch the first off,
  `fy worktree add` and `remove` refuse to work in a repo with a promisor remote:
  `extensions.partialClone`, or any `remote.<name>.promisor` or `remote.<name>.partialclonefilter`,
  in `.git/config` or any file it includes. The `[hook]` entries are switched off by name: foldyard
  reads every file `.git/config` includes, whatever the include's condition (a git that
  `fy worktree add` starts meets conditions yours doesn't), and runs no git when one of them can't
  be read. One gap remains: a hook added to `.git/config` while the command runs can still slip
  past. Your own git on your computer runs all of this, filters included, as it always has.
- **The compose client reads your compose files on your computer.** Your stack runs in the VM, but
  the compose client that starts it runs on your computer, and the compose files are editable from
  the box. foldyard gives the client a minimal environment (never `host.env`; add names with
  `[project] compose_env`, which `fy config widenings` lists) and refuses to act on compose files
  that name paths outside the checkout — `env_file:`, build contexts, `include:` and the like. That
  check is a list of known keys, and the client re-opens each path after foldyard checked it, so it
  narrows this channel rather than closing it. `[machine] compose = "vm"` closes it by running the
  client in the VM, where those paths hold nothing of yours. It is experimental, and it will become
  the default once it has been proven ([configuration](./configuration.md#machine)).
- **Registry logins stay on your computer.** A client there sends its logins to the VM's API service
  with each pull and build, all of them for a build, and the service keeps them in a temp file a box
  can read while it runs. So foldyard's clients get none, whatever `podman login` has stored. A
  project can name `FY_REGISTRY_AUTH_FILE` in `[project] compose_env`, for a file you export under
  that name ([stack](./stack.md#private-images)): a box can read that file's login during a build,
  so keep it narrow. Before this, every login on your computer was exposed that way. Your own
  recipes that run `"$ENGINE" compose` or `podman build` aren't foldyard's clients: they still send
  yours.
- **Repo text that steers the agent.** `[claude].system_prompt` is prepended to every teammate's
  agent, and `[claude.settings]` / `[codex.config]` are passed to the agent CLI (`--settings` /
  `-c`), where a `hooks` or `permissions` entry steers it harder than any prompt. This runs in the
  box, not on your computer, but it is still repo content steering a trusted actor.
  `fy config widenings` lists all three. They are adopted like the rest of the file — but adopting
  means a person reading a diff, which is weaker than "can't happen".

foldyard itself is installed on your computer as a uv tool (`uv tool install foldyard`), a frozen
copy the repo mount can't change ([ADR-0020](./adrs/0020-post-extraction-consumption-model.md)). The
exception is developing foldyard itself with an editable install: if that source checkout is also
mounted into a VM, the box can edit the code the supervisor runs. `just pin <hash>` in a foldyard
checkout narrows that: an editable install from a private clone outside the mount, frozen at the
commit you name. It protects `fy` and the supervisor from then on. That run of the recipe, and every
other `just` recipe, still runs the checkout's code on your computer.

## The credential story: nothing at rest, injected in flight

The resting state is **zero secrets in the VM**: no key files, no token environment variables, no
`~/.netrc`, no credential helpers — nothing to steal. When you need real access, you switch it on
(`fy mode gcp=logs github=on`) and three mechanisms keep the grant contained:

- **Tokens are made on your computer and injected at the proxy.** The box holds only a dummy value;
  the proxy on your computer replaces it with a real, short-lived token as the request passes
  through. The real token never enters the box — not as an environment variable, not as a file
  ([ADR-0007](./adrs/0007-credential-injection-at-egress-proxy.md)). A fully compromised box can use
  the access only while the switch is on, and only against the injected host; it never *holds* the
  credential. The one exception is Google Cloud's metadata emulator: Google's client libraries fetch
  a token and attach it themselves, so a token of an hour or less does sit in the box while the
  `gcp` switch is on ([gcp.md](./gcp.md)).
- **Emergency levels expire.** Levels that act as *you* have a mandatory TTL (one hour by default)
  and switch themselves back off ([ADR-0005](./adrs/0005-secretless-by-default-posture-axes.md)).
- **Control lives outside the VM.** The current mode is stored in your home directory, where nothing
  in the box can reach it, so the box can't raise its own access. The same goes for the adopted
  config ([ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)). The supervisor's daemons are the
  only path a credential takes: **no daemon running, no credential flows**
  ([ADR-0006](./adrs/0006-host-side-enforcement-single-supervisor.md)).
- **The credential owns its scope.** What an injected token can do is decided where the credential
  is: a GitHub App token carries exactly the App installation's permissions, which only change on
  github.com with an org owner's approval. foldyard doesn't narrow or cap them, so a shared App
  hands the box every purpose it serves — use one App per purpose, installed only on the
  repositories it serves
  ([ADR-0031](./adrs/0031-credentials-are-protocol-kinds-the-credential-owns-its-scope.md)). It
  reports them instead: `fy config widenings` shows the permissions last read from the installation
  and when, flagging `write`, `admin` and any level foldyard doesn't recognise, and when a reading
  changes (no foldyard file changed, so nothing else would say so) the supervisor logs it and sends
  a notification. A probe that can't read the scope leaves the last reading marked as possibly stale
  and logs that once, so a frozen record can't pass for a current one.
- **Token services are built in, never repo code.** The `[[inject]]` kinds foldyard ships (`static`,
  `github-app`, `gh-cli`), the Codex refresh flow, or an installed plugin are the only ones that
  run. There is no config key naming a command to run and no token-service path inside the repo:
  otherwise any write to the checkout — an agent, a postinstall script, a branch you checked out to
  review — would run as you, next to your credentials. Secrets are declared with `[[secret]]`, whose
  `how` hint foldyard **prints for you to run** and never runs itself
  ([ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md)).

**Keep real secrets out of your compose file's `environment:` and `env_file`.** A value there sits
in the container for its whole life: any code running in it reads `/proc/<pid>/environ`, and
anything holding the engine socket, the box included, reads it with `inspect`. Give the service a
dummy and let a switch inject the real value at the proxy ([modes.md](./modes.md); a stack
container's traffic reaches the proxy only with the VM firewall on; more in [stack.md](./stack.md)).

## Network: what's cooperative, what's enforced

The box's traffic goes through the proxy on your computer because of proxy environment variables.
That gives full visibility — every destination logged, requests decrypted, and an optional
[allowlist](./networking.md). But routing by environment variable is **cooperative**: tools that
honour it are logged; malicious code can unset `HTTPS_PROXY` and connect directly. foldyard doesn't
call that filtering, because it isn't
([ADR-0009](./adrs/0009-monitoring-cooperative-enforcement-locked.md)).

**Enforcement is the VM firewall, `[machine] firewall = true`** (Lima backend): nftables rules
inside the VM that *refuse* traffic not going through the proxy, so the proxy becomes the only way
out. They are installed as root when the VM boots, from your adopted config, and the same boot
script removes the VM user's passwordless sudo. Root in the VM exists only at boot, so a container
escape that lands as the VM user can't remove the firewall — only a VM-kernel exploit can, and even
that reaches no credential. On Linux, `[machine] host_firewall` adds the same rules on your computer
around the VM process, which survives even that. Without the firewall, QUIC (UDP 443) can bypass the
proxy. Details are in [networking.md](./networking.md).

**The proxy refuses git pushes over HTTPS.** "The box doesn't push" rests first on there being no
credential in it, but a token can still turn up (one in a `.env`, a pasted token, a credential
bridge nobody has found yet). So the proxy also refuses a push by its URL alone, whatever credential
rides along: git's smart-HTTP push is a `GET …/info/refs?service=git-receive-pack` then a
`POST …/git-receive-pack`, and the proxy answers either with a `403` that git shows as
`remote: refused by the foldyard proxy - the box doesn't push …`. It does this on any host, not just
GitHub, and logs a blocked row (`fy allow` doesn't offer it: no grant lets a push through). Fetches
are untouched. It recognises those two URLs in the spellings a server still routes to receive-pack
(case, `;params`, trailing and dot segments, backslashes), but a server that exposes push under some
other URL altogether — a custom CGI gateway, say — isn't covered. Two more limits: it is git's
protocol only — a write through an API (`api.github.com`'s contents or refs endpoints) is the
injected credential's own scope, as above — and the proxy can't see inside a
[`passthrough`](./networking.md) tunnel, so a push to a host listed there isn't refused
(`fy config widenings` names the well-known git hosts that are). The refusal lifts while a mode that
can push is on — today the emergency `gh-cli` switch ("push possible") — but only on the hosts that
switch injects its token on (`api.github.com` for `gh-cli`), and comes back when it turns off.
Everywhere else a push is still refused while it is on: a credential that reached the box some other
way can't use the switch to push to GitLab, Bitbucket or even `github.com`.

## `fy verify`: prove it, don't trust it

`fy verify` is the isolation self-test: run it any time; it exits non-zero on any failure, so CI can
use it. Run it *inside* the box (`fy box shell`, then `fy verify`) for the full set; outside the box
only the VM-boundary checks run. What it checks:

- **VM boundary** (over the container socket): the engine reports **rootless**; a
  `--privileged --pid=host` container **can't read the VM's PID-1 namespace** (the known breakout,
  attempted and refused); and a **mount audit** — the VM's own mount table carries no path from your
  computer beyond the repo and worktrees mounts and foldyard's to-box and from-box, matched exactly.
  Where there is a to-box, two more rows: the VM's table shows it read-only, and a `--privileged`
  container that binds it **read-write** still can't create a file in it — an actual write, refused
  (by `EROFS`, `EPERM` or `EACCES` — any refusal counts), since the guest's own `ro` flag is one
  remount away for root in the VM. Inside the box, the box's own `/run/foldyard/to-box` must refuse
  a write too.
- **Credential checks** (inside the box): no SSH agent forwarded, no git-credential bridge to your
  computer (`GIT_ASKPASS`, or a git `credential.helper` that calls back through Dev Containers), no
  editor-attach bridge sockets in `/tmp`, `/tmp/user/*`, `$XDG_RUNTIME_DIR` or `$TMPDIR`, no
  `~/.ssh` private keys, no `~/.netrc`, and a dry-run push to `origin` (`git push --dry-run`, which
  sends nothing) is **refused** for lack of a credential — the push handshake needs write access, so
  this holds for a public origin too. Where the proxy refuses that push itself (an HTTPS origin it
  decrypts), git never gets as far as offering a credential, so verify asks git directly instead
  (`git credential fill` for origin's push URL) and fails if it finds one. With the VM firewall on,
  an SSH origin (`git@host:path` or `ssh://…`) never gets that far: ssh doesn't use the proxy, so
  the wall refuses its connection. Verify then proves that directly, for every push URL
  (`git remote get-url --push --all origin`, so `pushurl` and `insteadOf` apply): the host and port
  ssh would dial (`ssh -G`, so your ssh config's `HostName` and `Port` apply) must resolve, the
  proxy must answer, and a direct connection to that port must be refused. That passes as
  `SSH push impossible: the VM firewall refuses github.com:22 (Connection refused)`. A connection
  that fails any other way (it times out, or an ICMP reply says "No route to host") is a warning,
  `SSH push blocked: github.com:22 (timed out), not a refusal …`: the wall answers with a reset, so
  anything else looks like a network turning ssh's port away upstream, which can't be credited to
  the wall and won't follow you to the next network. A `ProxyCommand` or `ProxyJump`, a name that
  doesn't resolve, a connection that succeeds, or no firewall leaves the row unproven, as before.
  These are built into foldyard's core, so a missing or broken plugin can't weaken them.
- **Push refusal** (inside a box that routes through the proxy): a push's first request
  (`GET https://github.com/…/info/refs?service=git-receive-pack`) sent through the box's proxy must
  come back as the proxy's own `403`. The emergency `gh-cli` switch doesn't change that (it lifts
  the refusal only on `api.github.com`, where its token goes); were a push-capable switch to inject
  on `github.com` itself, the row would say so instead of passing or failing. A `github.com`
  tunnelled blind by `passthrough` is a `WARN`.
- **Dummy checks** (from the credential plugins): each `[[inject]]` row's `box_env` dummies must
  still hold their dummy value, in *any* mode, and the variables a known client reads a token from
  (`GH_TOKEN`, `GITHUB_TOKEN`) must be unset or a declared dummy in every box; the real credential
  stays on your computer. A push over the API is possible exactly when an injected token's own scope
  allows it (a GitHub App with `contents: write`, say) — `fy config widenings` shows where each
  credential is delivered and what a GitHub App's was last read to grant. Active **emergency levels
  print a banner**, so an escalation is never invisible.
- **Firewall probes** (Lima with `[machine] firewall = true`): direct connections from the box that
  ignore the proxy — to a public IP on port 443 *and* on 53, the DNS-tunnel case — must be refused.
  A reachable proxy is checked first, so a box that is simply offline doesn't pass.
- **Your computer's loopback** (on your computer, Lima with `[machine] firewall = true`): verify
  opens a throwaway service on your loopback, and a probe container tries to reach it directly at
  the address Lima gives the VM *and* through the proxy, which runs on your computer. The service
  must see no connection. The probe's request to the proxy's own liveness address is the positive
  control, so a container with no way out doesn't pass
  ([networking](./networking.md#services-on-your-computer)).

### When a check fails

Each row names *what* failed. What it usually means, and what to do:

- **`probe image … could not run — the boundary battery DID NOT EXECUTE`** — the first check failed,
  so none of the boundary checks ran. The probe image (`alpine`, or `VERIFY_IMG`) must reach the VM:
  with the firewall on, the proxy must be up (`fy host restart`, or `fy up`), or pre-pull the image,
  or point `VERIFY_IMG` at one already in the VM. Not an isolation failure — an unproven result.
- **`the probe image's wget … is neither busybox's nor GNU Wget 1.x`** — the loopback row's direct
  attempt must skip every proxy, which is known only for those two (an environment variable or a
  wgetrc can name one; the proxy would refuse the gateway and read as the wall refusing it).
  `VERIFY_IMG` points at an image with another wget, or none: use an alpine or busybox one. An
  unproven result, not an isolation failure.
- **`engine is NOT reported rootless`** — the VM was created rootful, or verify reached a socket
  that isn't foldyard's. Check which socket the section header names, then `fy doctor`; rebuild a
  rootful VM with `fy machine recreate`.
- **`host PID1 namespace READABLE … (breakout!)`** — the known escape worked. Treat the VM as
  compromised and stop using it (`fy machine stop`) until you know why. The usual cause is the
  socket belonging to an engine on your computer rather than the VM's, not a kernel bug.
- **`VM exposes host paths: …`** — the VM mounts more than the repo and the worktrees folder (a
  backend default that shares your whole home, or a mount added by hand). Mounts are set when the VM
  is created: fix the `[machine]` config, then `fy machine recreate`.
  **`could not read the VM mount table … UNPROVEN`** means the probe failed, not that anything
  leaked. Under gVisor the row reads `N/A`; run `fy verify` on your computer for the audit.
- **to-box rows:**
  - `a --privileged container binding the to-box read-write CREATED …` — something in the VM can
    write what only your computer should: the share isn't read-only outside the guest. Look for
    `mounts` or `mountType` in `~/.lima/_config/override.yaml` (Lima's most permissive `writable`
    wins), then `fy machine stop && fy up`, which records foldyard's read-only to-box again and
    refuses while an override still widens it. The row names the file it made (and removed).
  - `to-box shows rw in the VM's mount table` (`WARN`) — the guest's own flag was dropped, by root
    in the VM or a hand-made remount; the write row is the verdict, and it still passed.
  - `the to-box write probe did not run — read-only UNPROVEN` — the probe container failed; the same
    causes as the probe-image row above.
  - `/run/foldyard/to-box took a write` (in the box) — the box's own bind isn't `:ro`: recreate the
    box (`fy box recreate`).
- **`SSH_AUTH_SOCK set` / `git-credential bridge to the host` /
  `git has a credential helper that asks the host` / `editor-attach bridge sockets`** — a VS Code
  attach forwarded your computer's credentials into the box. See
  [below](#editor-attach-credential-bridges).
- **`git credential helpers UNPROVEN`** — `git config` couldn't read the box's config (or timed
  out), so verify can't say no helper calls back to your computer. Run
  `git config --show-origin --get-regexp '^credential\..*helper$'` in the box to see why.
- **`~/.ssh key material` / `~/.netrc present`** — a credential is in the box. foldyard doesn't put
  it there: find the mount or `[box]`/bootstrap step that copies it in, remove it, then
  `fy box down` and `fy box up`.
- **`a dry-run push to origin was ACCEPTED — the box can push`** — a credential with write access to
  `origin` reached it from the box. `fy state` shows what the mode grants and `fy config widenings`
  where credentials are delivered. A network timeout does *not* pass this row; an unreachable remote
  proves nothing.
- **`git push refused by the foldyard proxy, but git HOLDS a credential for origin`** — the proxy
  stopped the push, but git has a credential it would have sent: a credential helper's, a stored
  one, one written into the remote's URL (`git remote get-url --push origin`), or an askpass. Remove
  it — the proxy's refusal is a backstop, not the boundary.
  **`… a credential for origin is UNPROVEN`** means `git credential fill` timed out or failed for
  another reason (the row says which), or origin's push URL couldn't be read.
- **`git remote UNREACHABLE for non-credential reasons`** — the push never got as far as asking for
  a credential: no egress, or a blocked host. The row ends with the reason (ssh's own line for an
  SSH origin). Under the VM firewall git over HTTPS goes through the proxy like everything else in
  the box, so a refused `github.com` is the allowlist's (`fy allow add github.com`). An SSH origin
  behind the firewall shows up here only when the refusal couldn't be proven: its name didn't
  resolve, ssh is configured to go through a `ProxyCommand` or `ProxyJump`, the proxy didn't answer,
  or the connection to the SSH port succeeded (a grant, or a firewall that isn't enforcing: see the
  VM firewall rows).
- **Push refusal rows:**
  - `a git push through the proxy was NOT refused` — the proxy decrypted the request and let it
    reach GitHub. Check `fy mode` on your computer (a push-capable switch injecting on `github.com`
    reads as "allowed by the active mode", not this), then `fy host restart`.
  - `a git push was refused, but not as a push` — the running proxy predates the push refusal:
    `fy host restart` on your computer.
  - `github.com is tunnelled blind` (`WARN`) — it is in `[proxy] passthrough` (the `@vcs` bundle has
    it), so the proxy can't see a push to it. A decision, not a breach; take it out of `passthrough`
    if you want pushes refused there too.
  - `git push refusal by the proxy UNPROVEN` — no answer came back through the proxy (not running, a
    timeout, or a TLS failure: the box doesn't trust the proxy's CA), or the box's `HTTPS_PROXY` is
    set but isn't an `http(s)://host[:port]` URL verify can read (the row names its shape, without
    any `user:password@`). `fy host` on your computer shows whether the proxy runs.
- **`… possible real credential in the box`** (e.g. `GH_TOKEN` is more than its dummy) — the box's
  environment carries a real credential. `fy mode` shows the mode, `fy state` desired against
  observed; a box created under older config is recreated with `fy box down` and `fy box up`.
- **`box is NOT under gVisor`** — the box was created before `[machine] runtime = "gvisor"` was
  adopted, or through the wrong socket. `fy box down` and `fy box up` recreate it.
- **VM firewall rows:**
  - `no HTTPS_PROXY in the box` — the box predates the firewall config: `fy box down`, `fy box up`.
  - `the permitted path … is unreachable too` — the box has no egress at all, so the refusals prove
    nothing. `fy host` shows whether the supervisor runs; `fy host restart` starts it.
  - `direct egress … CONNECTED` — the firewall isn't enforcing in the VM. It is installed at boot,
    so run `fy machine stop`, then `fy up`, which also checks the VM's own firewall report and
    refuses on a mismatch, printing where the boot log is.
- **Loopback rows:**
  - `a container REACHED a service on your computer's loopback` — either the VM firewall isn't
    enforcing (the row above, from inside the box, says which) or the proxy dialled your loopback
    for it. `fy allow list` shows the grants: one naming `127.0.0.1` with this port can't exist for
    a port verify picked just now, so suspect the foldyard install the supervisor runs
    (`fy host restart` after an upgrade).
  - `the probe couldn't reach the proxy through the gateway either` — no way out at all, so the
    refusal proves nothing: `fy host` shows whether the supervisor runs.
- **Health `WARN`s** are advisory (`fy logs <service>`) and never change the result.

#### Editor-attach credential bridges

VS Code's Dev Containers attach (`fy code`, or a manual "Attach to Running Container") runs its
server in the box and, by default, opens several channels back to your computer:

- your SSH agent (`vscode-ssh-auth-*.sock`), forwarded into every terminal;
- the git extension's askpass (`GIT_ASKPASS` over `vscode-git-*.sock`), which for github.com hands
  over the GitHub session VS Code is signed in with;
- Dev Containers' own IPC (`vscode-remote-containers-ipc-*.sock`). Its host side runs
  `git credential fill` and the Docker credential helpers on your computer for whatever asks,
  without consulting any setting. By default it also writes a `credential.helper` into the box's
  `~/.gitconfig` that calls it;
- the `code` CLI's IPC (`VSCODE_IPC_HOOK_CLI` over `vscode-ipc-*.sock`), which drives the host's VS
  Code, including opening URLs there — a way around the egress proxy.

Settings only switch off the *container* end of these: a process that finds a socket can still ask
the host directly. So `fy code` removes the credentials at the source where it can, and treats the
rest as defaults:

- **SSH:** `fy code` starts VS Code with foldyard's own *empty* ssh-agent, so there is nothing to
  forward.
- **Git, on your computer:** `fy code` starts VS Code with an empty git config (`GIT_CONFIG_GLOBAL`
  pointing at an empty file, `GIT_CONFIG_NOSYSTEM=1`, the rest of `GIT_*` dropped), so the host side
  of Dev Containers' IPC finds no credential helper to run — not your `~/.gitconfig`'s, nor the
  Xcode gitconfig's `osxkeychain`. VS Code is also started from the instance's own state directory,
  with `GIT_CEILING_DIRECTORIES` above it, never from your checkout: the checkout's `.git/config` is
  writable from the box, and a `credential.helper = !cmd` there would otherwise run on your
  computer. The instance's local terminals get your git config back. Each worktree's instance is
  also a fresh VS Code profile with no GitHub sign-in; signing in there (for Copilot, say) gives the
  askpass socket a token to hand out again.
- **Settings:** `fy code` pins, at user and machine scope, `git.terminalAuthentication` and
  `git.useIntegratedAskPass` off, `github.gitAuthentication` off, and
  `dev.containers.gitCredentialHelperConfigLocation` to `"none"`, with
  `dev.containers.dockerCredentialHelper`, `copyGitConfig` and `githubCLILoginWithToken` off. If the
  instance's own `settings.json` can't be rewritten (comments, invalid JSON), `fy code` refuses to
  launch rather than run unpinned. `.vscode/settings.json` in the repo can turn the `git.*` and
  `github.*` keys back on, and `fy verify` fails a checkout that does
  (`workspace settings re-enable the git-credential bridge`).
- **In the box:** `~/.config/foldyard/harden.sh`, sourced on the first line of `~/.bashrc`, unsets
  the bridge variables in every bash shell and starts a reaper that deletes all four kinds of socket
  as they appear, in `/tmp`, `/tmp/user/*`, `$XDG_RUNTIME_DIR` and `$TMPDIR`. A box still running an
  older reaper gets the new one the next time `harden.sh` runs. This also covers a manual attach
  that didn't go through `fy code`, but it is a mitigation, not a wall: a process already in the box
  can watch for a socket and hard-link it before the reaper's next pass. That's why the credentials
  are removed on your computer first.

If a row still fails:

1. Attach with `fy code` rather than attaching manually.
2. Run `fy box shell` — it restarts the reaper (a socket can survive up to one second before it is
   deleted).
3. If the box predates this protection, `fy box up` applies it to the running box.
4. Use bash: other shells don't source `harden.sh`.
5. Not from an attach at all? Look for a mount or `[box]`/bootstrap step that copies a credential in
   (`fy config widenings` lists what the adopted config allows), remove it, then `fy box down` and
   `fy box up`.

### What verify doesn't prove

**`fy config widenings`** answers the question verify doesn't: not "is the mode what it claims?" but
"what did we agree to, and where is it written?" — the `passthrough` hosts left undecrypted (with
`@all` resolved to its real count), where each credential is injected, which agent prompt is shared
and which is yours, and any key that reads as security config but is no longer honoured.

verify exercises the escapes we know about. A pass means the known breakouts are refused and the
mode is what it claims, not that no unknown escape exists. Nor does it prove the to-box holds
against **root in the VM**: the box never has that, so verify can only write from a container. That
proof is foldyard's own host-tier test (`tests/test_mounts_e2e.py`), which runs as root at boot in a
real VM, remounts the to-box read-write, tries every kind of write, and checks the file on your
computer is unchanged. It also doesn't scan your repo for committed secrets — that is your
precondition, below.

## The precondition: a clean repo

foldyard mounts your repo into the VM, so "zero credentials in the box" holds only when the repo
carries **config, not credentials**. foldyard can mount a clean repo; it can't clean a dirty one.
Before you start, scan your history with [gitleaks](https://github.com/gitleaks/gitleaks) or
[TruffleHog](https://github.com/trufflesecurity/trufflehog), move anything they find into `host.env`
behind a switch, and re-scan regularly — see
[the README's TL;DR](https://github.com/twistco/foldyard/blob/main/README.md#tldr).

## Reporting a vulnerability

Report anything you believe is exploitable privately, as described in
[SECURITY.md](https://github.com/twistco/foldyard/blob/main/SECURITY.md) — not as a public issue.
Non-sensitive bugs go to the [issue tracker](https://github.com/twistco/foldyard/issues).
