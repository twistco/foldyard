# Modes and credentials — how the box gets access, and why you can't grant it

The VM's resting state is **zero secrets**: no key files, no token env vars, no credential
helpers. Nothing to lift, so nothing to leak. Access is a *mode* the human sets on their computer
(the host), and it reaches you as capability, never as a secret you hold.

## Switches and levels

A mode is a set of independent **switches**, each at a **level**. Which switches exist is per
project — `fy mode` prints them with a one-line blurb per level; there is no fixed list to
memorise. Typical shapes: an off/on injector for one API, or a ladder like
`off → logs → sa → user` where each level grants more.

- **The default level is the secretless one.** A switch at rest costs nothing and grants nothing.
- **Emergency levels are TTL-bound.** "Act as me" levels expire (default an hour) and switch back
  to the default on their own. If something worked earlier and now 401s, check `fy mode` first —
  a lapse looks exactly like a broken credential.
- `fy mode` also shows **DEGRADED**: the level is set, but the host's check says the credential
  behind it isn't working right now (an expired grant, revoked access). And **BLOCKED**: the
  host-side service behind it couldn't start (a missing secret, a port in use). That's the
  difference between "not asked for" and "asked for, not currently working".

## How a credential reaches you (and doesn't)

The real token is made **on the host** and added to your request in flight by the egress proxy.
The box holds a dummy value at most. So:

- reading the env var gives you a placeholder — that's expected, not a misconfiguration;
- the access only exists while the switch is on, and only against the host it's scoped to;
- a fully compromised box can *use* the access during that window but can never *hold* it;
- with a switch off, the proxy answers a request carrying its placeholder itself, with a 401
  naming the switch and the fix (say `fy mode claude=on`, or `fy mode github=on` behind `gh`;
  always run on the host) — nothing reaches the provider. Codex on a ChatGPT subscription then
  stops on `workspace routing discovery unauthorized (401)`, which hides that text: check
  `fy mode` before debugging the credential.
- with the switch on but the host unable to mint the token, the proxy answers with a 502 whose
  message starts "foldyard could not mint a credential for …" and quotes the host-side reason.
  The request never left (unless the message says the host refused the one it carried — then it
  went once and drew a 401). Either way nothing in the box can fix it: pass the message to the
  human (the fix is on their computer; `fy host logs` there has the details).
- one host and path gets one credential. If `fy mode` says two switches both inject on it, the
  proxy is injecting neither, so a 401 there is that, not a broken credential: ask the human to
  turn one off, with the exact `fy mode` command the message gives (a switch's resting level
  isn't always `off`).

Some services in the stack instead get an identity from a local metadata emulator — same idea:
the container is handed short-lived capability, not a key.

## What to do when you need access

You cannot change the mode from the box (`fy mode <switch>=<level>` refuses here — the
authoritative state is in the host's home, outside the mount). So:

1. Run `fy mode` and read the blurbs; work out the exact level you need.
2. Ask for it explicitly, with the reason and the command:
   *"To read staging logs I need `fy mode gcp=logs` on your computer (expires in an hour)."*
3. If a **secret** is missing rather than a level — the host may need it pasted once. `fy mode`
   asks for it when the switch goes on (and `fy box up` asks as a backstop), storing it on their
   computer at 0600. `fy doctor` on the host names which one.
4. If the switch is on and the service answers **403**, the credential's own scope is the limit,
   not foldyard — once you've ruled out a rate limit. Read the 403 first (`gh api -i …`): a
   rate limit says so in its `message` ("API rate limit exceeded", "secondary rate limit") and
   carries `x-ratelimit-*` / `retry-after` headers, so wait rather than ask for access. A missing
   permission says "Resource not accessible by integration", and its
   `X-Accepted-Github-Permissions` header names the permission. A GitHub App token carries
   exactly what the App's installation grants (foldyard neither narrows nor caps it), so ask the
   human to grant that permission on the App (an org owner approves it), or for a separate App
   with its own switch.
   Editing `foldyard.toml` can't widen it — there is no permissions key to add. The human can
   see what the App was last read to grant with `fy config widenings` on their computer (or
   beside the switch in their `fy mode`); the box can't read that record, so ask rather than
   guess.

## Reading the current grant surface

`fy config widenings` lists what this project's config asks the host to allow: which hosts are
forwarded without decryption (`passthrough`), where each mechanism delivers a credential (armed
*and* declared but not yet armed), which agent prompts are shared with the team vs personal, and
any config key that reads as a control but is no longer honoured.

Depth: `fy docs modes`, `fy docs security`, `fy docs adr-0005` (why switches are data), `fy docs
adr-0007` (injection at the proxy), `fy docs adr-0008` (keyless agent auth).
