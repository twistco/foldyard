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
- a fully compromised box can *use* the access during that window but can never *hold* it.

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

## Reading the current grant surface

`fy config widenings` lists what this project's config asks the host to allow: which hosts are
forwarded without decryption (`passthrough`), where each mechanism delivers a credential (armed
*and* declared but not yet armed), which agent prompts are shared with the team vs personal, and
any config key that reads as a control but is no longer honoured.

Depth: `fy docs modes`, `fy docs security`, `fy docs adr-0005` (why switches are data), `fy docs
adr-0007` (injection at the proxy), `fy docs adr-0008` (keyless agent auth).
