"""The build gate — an image build the egress wall refused, turned into one question per host.

Under the in-VM wall an image build (`fy box build`, the stack's `fy up` build) reaches the proxy
as a trusted build: tunnelled, not decrypted, and still walled (ADR-0029's amendment). When the
wall refuses it, the tool reports the URL it ASKED for, which is often not the host that was
refused: a CDN redirects inside the build's tunnel, where the proxy can't see the redirect
(`cdn.playwright.dev` → `storage.googleapis.com`, seen live). Naming the real host used to mean
reading the egress log by hand.

So the gate reads the refusals the proxy attributed to the build (the build marker flags them)
and, on the host with a terminal, offers each one — once / session / permanent / no — then
retries. A retry is cheap for a build (the layer cache replays everything before the failing
step), and a redirect chain reveals one hop per attempt, so it loops, bounded. Whatever is
granted comes back as `[proxy] recommend` lines, so the team is offered the same hosts at their
own `fy up` — the operator's consent, shared, never a grant written into the repo.

In the box the gate stays out of the way: grants are host-side and the log is outside the mount.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from . import allowlist, config

# A retry per hop of a redirect chain; a chain longer than this is worth a human look.
MAX_ATTEMPTS = 4
# `once` sized for a build: the default 120 s can lapse between the grant and the retried step
# reaching the network again (a cold layer, an apt install before it).
ONCE_TTL_SECONDS = 900

_ANSWERS = {"o": "once", "once": "once", "s": "session", "session": "session"}
_ANSWERS |= {"p": "permanent", "permanent": "permanent"}


def build_log() -> Path:
    """The egress log a build's rows land in: the MAIN listener's (the port builds use)."""
    from .plugins import proxy  # lazy: the registry import stays off this module's import path

    return config.main_log_dir() / proxy._PROXY_LOG


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


def _describe(entry: dict) -> str:
    tool = f" · {entry['uas'][0]}" if entry["uas"] else ""
    return f"{entry['host']} ({entry['count']}×{tool})"


def _offer(refused: list[dict], prompt: Callable[[str], str], echo: Callable[[str], None]):
    granted: list[dict] = []
    for entry in refused:
        answer = (
            prompt(
                f"  allow {_describe(entry)}?  [o]nce ({ONCE_TTL_SECONDS // 60} min) · "
                "[s]ession · [p]ermanent · [n]o (default): "
            )
            .strip()
            .lower()
        )
        level = _ANSWERS.get(answer)  # exact matches only; anything else is no
        if level is None:
            echo(f"    ({entry['host']} not granted)")
            continue
        allowlist.grant(entry["host"], level, ONCE_TTL_SECONDS if level == "once" else None)
        echo(f"  ✓ {entry['host']} allowed ({level})")
        granted.append(entry)
    return granted


def run(
    build: Callable[[], int],
    *,
    what: str,
    interactive: bool | None = None,
    prompt: Callable[[str], str] = input,
    echo: Callable[[str], None] = print,
) -> int:
    """Run ``build`` (returns its exit code) under the gate; returns the final exit code."""
    from .plugins import proxy

    if config.in_box() or proxy.build_proxy_url() is None:
        return build()  # no host-side log to read, or builds don't cross the wall
    if interactive is None:
        interactive = sys.stdin.isatty()
    shared: list[dict] = []
    rc = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        since = datetime.now(UTC).replace(microsecond=0)
        rc = build()
        refused = _refusals(since)
        if rc == 0:
            if refused:
                hosts = ", ".join(_describe(e) for e in refused)
                echo(f"• the wall refused {hosts} during the {what}; the build found another way")
            break
        if not refused:
            break
        hosts = ", ".join(e["host"] for e in refused)
        echo(f"✗ the egress wall refused {hosts} during the {what}")
        if not interactive:
            for e in refused:
                echo(f"    fy allow add {e['host']} --level once --ttl {ONCE_TTL_SECONDS}")
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
        echo("\n".join(allowlist.recommend_block(shared)))
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
        f"  add {hosts} to foldyard.toml's [proxy] recommend, so the team is offered "
        f"{'them' if len(shared) > 1 else 'it'}?  [y]es · [n]o (default): "
    )
    if answer.strip().lower() not in ("y", "yes"):
        return False
    cfg = config.current()
    drift = configpin.inspect(cfg)
    original = drift.tree.get("foldyard.toml")
    edited = allowlist.with_recommends(original.decode() if original else "", shared)
    if edited is None:
        echo("  ✗ couldn't add them safely to foldyard.toml — here are the lines instead:")
        return False
    path = cfg.repo_root / "foldyard.toml"
    current = path.read_bytes() if path.exists() else None
    if current != original:
        echo("  ✗ foldyard.toml changed while you answered — here are the lines instead:")
        return False
    path.write_text(edited)
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
            "  ✓ added to foldyard.toml — the checkout already differed from what the host runs,"
            f" so review and adopt it at the next `fy up`; {note}"
        )
    return True
