"""The build gate — an image build the egress wall refused, turned into one question per host.

Under the in-VM wall an image build (`fy box build`, the stack's `fy up` build) reaches the proxy
as a trusted build: tunnelled, not decrypted, and still walled (ADR-0029's amendment). When the
wall refuses it, the tool reports the URL it ASKED for, which is often not the host that was
refused: a CDN redirects inside the build's tunnel, where the proxy can't see the redirect
(`cdn.playwright.dev` → `storage.googleapis.com`, seen live). Naming the real host used to mean
reading the egress log by hand.

So the gate reads the refusals the proxy attributed to the build (its secret flags them)
and, on the host with a terminal, offers each one — once / session / permanent / no — then
retries. A retry is cheap for a build (the layer cache replays everything before the failing
step), and a redirect chain reveals one hop per attempt, so it loops, bounded. Whatever is
granted comes back as `[proxy] recommend` lines, so the team is offered the same hosts at their
own `fy up` — the operator's consent, shared, never a grant written into the repo.

Being a build is proved, not claimed: the proxy tunnels a connection, attributes its refusals to
the build and applies the BUILD-SCOPED grants (so the runtime wall stays narrower) only when it
presents a secret minted for that build (the password in the build's proxy URL; build-args are
never persisted into the image), recorded host-side only as a hash and revoked when the build
ends. The fixed `fy-build` user alone — which anyone can read, the box included — earns none of
it: the box used to present it to turn a decrypted request into a hostname-only tunnel row. What
remains is one build's window (ADR-0029): the box can read a running build's secret through the
engine socket.

A build can also fail with NOTHING refused at the proxy: a tool that ignores the proxy env (Node's
own fetch without NODE_USE_ENV_PROXY — corepack ≥ 0.35 is one) dials out directly, and the VM
firewall refuses it before the proxy could log a row. The guest keeps those refusals readable
without root (``machine.wall_denies``), so a failed build names them too: the destination IP and
port, the tool to look for, and why there is nothing to grant.

In the box the gate stays out of the way: grants are host-side and the log is outside the mount.
An in-box build IS the box, so it gets the plain proxy URL: decrypted like any box request (its
steps trust the proxy CA through the walled VM's runtime wrapper), on runtime grants only.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import allowlist, config, mountwrite

# A retry per hop of a redirect chain; a chain longer than this is worth a human look.
MAX_ATTEMPTS = 4
# `once` sized for a build: the default 120 s can lapse between the grant and the retried step
# reaching the network again (a cold layer, an apt install before it).
ONCE_TTL_SECONDS = allowlist.OFFER_ONCE_SECONDS

# A build's secret outlives a crashed `fy` by at most this; a finished build revokes its own.
TOKEN_TTL_SECONDS = 6 * 3600

_ANSWERS = {"o": "once", "once": "once", "s": "session", "session": "session"}
_ANSWERS |= {"p": "permanent", "permanent": "permanent"}


def build_log() -> Path:
    """The egress log a build's rows land in: the MAIN listener's (the port builds use)."""
    from .plugins import proxy  # lazy: the registry import stays off this module's import path

    return config.main_log_dir() / proxy._PROXY_LOG


def tokens_file() -> Path:
    """The live build secrets, as SHA-256 hashes → expiry: project-wide, like the main proxy port
    builds use. Read by the proxy addon (``BUILD_TOKENS_FILE``); never holds a secret itself."""
    return config.build_tokens_file()


def _tokens_update(change: Callable[[dict], None]) -> None:
    """Read-modify-write the token file under an exclusive lock: builds in two worktrees issue and
    revoke against the one project-wide file, and an unlocked interleave loses a token (that build
    can't use its grants) or writes a revoked one back (live for up to the TTL)."""
    import fcntl

    path = tokens_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f".{path.name}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _tokens_update_locked(path, change)


def _tokens_update_locked(path: Path, change: Callable[[dict], None]) -> None:
    try:
        doc = json.loads(path.read_text())
        tokens = doc.get("tokens") if isinstance(doc, dict) else None
    except (OSError, ValueError):
        tokens = None
    tokens = tokens if isinstance(tokens, dict) else {}
    now = datetime.now(UTC)
    for digest, expires in list(tokens.items()):  # drop what a crashed build left behind
        try:
            if datetime.fromisoformat(expires) <= now:
                del tokens[digest]
        except (TypeError, ValueError):
            del tokens[digest]
    change(tokens)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"tokens": tokens}, indent=2) + "\n")
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _issue_token() -> str:
    token = secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now(UTC) + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat()

    def add(tokens: dict) -> None:
        tokens[digest] = expires

    _tokens_update(add)
    return token


def _revoke_token(token: str) -> None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    _tokens_update(lambda tokens: tokens.pop(digest, None))


def _refusals(since: datetime) -> list[dict]:
    """This build's still-ungranted refusals. Reads only the log files written since the build
    started (the live file, and a backup if the build rotated it) — whole files, not a tail: a
    busy box can push a build's rows out of any fixed tail."""
    log = build_log()
    paths = [
        p
        for p in [*config.rotated_logs(log), log]
        if p.exists() and p.stat().st_mtime >= since.timestamp() - 1
    ]
    return allowlist.build_refusals(allowlist.read_log_rows(paths), since)


# The wall's report is a ring of recent refusals; a build is measured by where it stood before.
_Mark = tuple[str, int] | None
# How long a failed build waits before reading it: a tool's last refusals land just before it
# exits, and the guest writes them a moment after the kernel logs them (seen live, 2026-10-16).
WALL_SETTLE_SECONDS = 1.0


def _wall_mark() -> _Mark:
    """Where the guest's refusal report stands now: ``(boot, last seq)``, ``("", -1)`` for an
    empty report, ``None`` when there is no report to read (then a build is never blamed)."""
    from . import machine  # lazy: the VM module isn't needed unless a build runs

    rows = machine.wall_denies()
    if rows is None:
        return None
    if not rows:
        return ("", -1)
    return (str(rows[-1].get("boot", "")), rows[-1]["seq"])


def _bypasses(mark: _Mark) -> list[dict]:
    """The wall's refusals since ``mark``, by destination: ``{dst, port, proto, count}``, first
    seen first. Anything walled in the VM counts — the build's steps, and the box meanwhile."""
    from . import machine

    if mark is None:
        return []
    time.sleep(WALL_SETTLE_SECONDS)
    boot, seq = mark
    out: dict[tuple, dict] = {}
    for row in machine.wall_denies() or []:
        if boot and row.get("boot") == boot and row["seq"] <= seq:
            continue
        key = (row["dst"], row.get("port"), row.get("proto") or "")
        entry = out.setdefault(key, {"dst": key[0], "port": key[1], "proto": key[2], "count": 0})
        entry["count"] += 1
    return list(out.values())


def _report_bypasses(bypassed: list[dict], what: str, echo: Callable[[str], None]) -> None:
    total = sum(e["count"] for e in bypassed)
    echo(
        f"✗ the VM firewall refused {total} connection{'s' if total > 1 else ''} that skipped "
        f"the proxy while the {what} ran:"
    )
    for e in bypassed:
        where = f"{e['dst']}:{e['port']}" if e["port"] is not None else e["dst"]
        proto = f"/{e['proto']}" if e["proto"] else ""
        echo(f"    {where}{proto} ({e['count']}×)")
    echo("  A tool ignored the proxy settings (HTTPS_PROXY), so the proxy never saw these and")
    echo("  there is no host to grant. Usual suspects: Node's own fetch (foldyard sets")
    echo("  NODE_USE_ENV_PROXY=1 in build steps, unless the image sets it itself), a")
    echo("  postinstall script with its own HTTP client, a raw socket. More: `fy docs networking`.")


def _describe(entry: dict) -> str:
    tool = f" · {entry['uas'][0]}" if entry["uas"] else ""
    return f"{entry['host']} ({entry['count']}×{tool})"


def _offer(refused: list[dict], prompt: Callable[[str], str], echo: Callable[[str], None]):
    granted: list[dict] = []
    whys = allowlist.build_recommendations()
    for entry in refused:
        why = f" — {whys[entry['host']]}" if whys.get(entry["host"]) else ""
        answer = (
            prompt(
                f"  allow {_describe(entry)}{why} for builds?  [o]nce ({ONCE_TTL_SECONDS // 60} "
                "min) · [s]ession · [p]ermanent · [n]o (default): "
            )
            .strip()
            .lower()
        )
        level = _ANSWERS.get(answer)  # exact matches only; anything else is no
        if level is None:
            echo(f"    ({entry['host']} not granted)")
            continue
        ttl = ONCE_TTL_SECONDS if level == "once" else None
        allowlist.grant(entry["host"], level, ttl, build=True)
        echo(f"  ✓ {entry['host']} allowed for builds ({level})")
        granted.append(entry)
    return granted


def run(
    build: Callable[[str | None], int],
    *,
    what: str,
    interactive: bool | None = None,
    prompt: Callable[[str], str] = input,
    echo: Callable[[str], None] = print,
) -> int:
    """Run ``build`` under the gate; returns the final exit code. ``build`` receives the proxy URL
    it should pass as its proxy build-args (``None``: builds don't cross the wall)."""
    from .plugins import proxy

    if config.in_box() or proxy.build_proxy_url() is None:
        # No host-side log to read, or builds don't cross the wall. In the box: the plain proxy.
        return build(proxy.build_proxy_url())
    token = _issue_token()
    try:
        return _run(lambda: build(proxy.build_proxy_url(token)), what, interactive, prompt, echo)
    finally:
        _revoke_token(token)


def _run(
    build: Callable[[], int],
    what: str,
    interactive: bool | None,
    prompt: Callable[[str], str],
    echo: Callable[[str], None],
) -> int:
    if interactive is None:
        interactive = sys.stdin.isatty()
    shared: list[dict] = []
    rc = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        since = datetime.now(UTC).replace(microsecond=0)
        mark = _wall_mark()
        rc = build()
        refused = _refusals(since)
        if rc == 0:
            if refused:
                hosts = ", ".join(_describe(e) for e in refused)
                echo(
                    f"• the allowlist refused {hosts} during the {what}; the build found another "
                    "way"
                )
            break
        bypassed = _bypasses(mark)
        if bypassed:
            _report_bypasses(bypassed, what, echo)
        if not refused:
            break
        hosts = ", ".join(e["host"] for e in refused)
        echo(f"✗ the allowlist refused {hosts} during the {what}")
        if not interactive:
            for e in refused:
                echo(f"    fy allow add {e['host']} --build --level once --ttl {ONCE_TTL_SECONDS}")
            echo("  No terminal here — grant with the commands above, then build again.")
            break
        granted = _offer(refused, prompt, echo)
        if not granted:
            break
        shared += granted
        if attempt < MAX_ATTEMPTS:
            echo(f"▶ retrying the {what}")
    else:
        echo(f"✗ still refused after {MAX_ATTEMPTS} attempts — look at the hosts above")
    if shared and not (interactive and _share(shared, prompt, echo)):
        echo(
            "  To offer these to the team at their own `fy up`, add to foldyard.toml (each `why`"
            " is what was SEEN — reword it before you commit):"
        )
        echo("\n".join(allowlist.recommend_block(shared, when="build")))
    return rc


def _share(shared: list[dict], prompt: Callable[[str], str], echo: Callable[[str], None]) -> bool:
    """Offer to write ``shared`` into this checkout's ``[proxy] recommend``; True once written.

    If the checkout matched the adopted copy, this edit is the only difference and the operator
    just made it, so it is adopted at once, pinned to exactly the bytes written. An edit landing in
    between (the box writes the checkout) makes that adoption fail, not include it. A checkout that
    had already drifted is left to the adoption gate at the next `fy up`: the edit would otherwise
    carry changes the operator never reviewed."""
    from . import configpin

    hosts = ", ".join(e["host"] for e in shared)
    answer = prompt(
        f"  add {hosts} to foldyard.toml's [proxy] recommend (for builds), so the team is offered "
        f"{'them' if len(shared) > 1 else 'it'}?  [y]es · [n]o (default): "
    )
    if answer.strip().lower() not in ("y", "yes"):
        return False
    cfg = config.current()
    drift = configpin.inspect(cfg)
    original = drift.tree.get("foldyard.toml")
    edited = allowlist.with_recommends(original.decode() if original else "", shared, when="build")
    if edited is None:
        echo("  ✗ couldn't add them safely to foldyard.toml — here are the lines instead:")
        return False
    path = cfg.repo_root / "foldyard.toml"
    current = path.read_bytes() if path.exists() else None
    if current != original:
        echo("  ✗ foldyard.toml changed while you answered — here are the lines instead:")
        return False
    mountwrite.write(cfg.repo_root, "foldyard.toml", edited.encode())  # not via a planted link
    note = "reword each `why` (what was SEEN), then commit"
    if drift.adopted and not drift.changed:
        tree = {**drift.tree, "foldyard.toml": edited.encode()}
        try:
            configpin.adopt(cfg, reviewed=configpin.digest(tree))
        except configpin.ReviewStale:
            echo(
                "  ✓ added to foldyard.toml — it changed again, so adopt at the next `fy up`; "
                + note
            )
            return True
        echo(f"  ✓ added to foldyard.toml and adopted (the change is yours); {note}")
    else:
        echo(
            "  ✓ added to foldyard.toml — the checkout already differed from what your computer"
            f" runs, so review and adopt it at the next `fy up`; {note}"
        )
    return True
