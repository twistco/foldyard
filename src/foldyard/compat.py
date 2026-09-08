#!/usr/bin/env python3
"""The consumer's declared foldyard version window — a hard floor and a soft nudge.

``foldyard.toml`` is the consumer repo's contract with the tool, so the *version* it needs
belongs there too, next to everything else it declares::

    [project]
    min_foldyard_version         = "0.4.0"   # refuse to run below this
    recommended_foldyard_version = "0.6.0"   # nudge, never block

Both are raised by the same commit that needs them, which is the point: a branch adding a
setting an older ``fy`` cannot honour raises the floor atomically with the setting.

**Why a floor has to be a hard stop.** ``config.py`` reads the TOML with ``.get()`` and no
schema — unknown keys are tolerated by construction. So an old ``fy`` against a new
``foldyard.toml`` does not fail: it silently ignores the new keys and does the old thing.
That is invisible, and a warning is not enough for invisible.

**Why the nudge is declarative rather than a PyPI lookup.** ``fy`` runs on the Mac *and*
inside the box, where egress is default-deny through the proxy — a version check would need
an allowlist hole punched in the zero-egress posture to power a cosmetic message. And the
repo's opinion of "current" is the more useful one anyway: a consumer pins CI deliberately
so it does not float with someone else's release, so nudging toward a version that consumer
has never tested is noise. Set ``FOLDYARD_NO_VERSION_NUDGE=1`` to silence the nudge; the
floor ignores it.

**Known limit, stated because it is inherent.** A floor only protects from the release that
implements it onward — any older ``fy`` ignores the key entirely and always will. This
mechanism cannot rescue the migration that motivated it; it earns its keep on the next one.

Kept stdlib-only and import-light: ``config`` is on the recipe hot path
(``python3 -m foldyard mode env`` runs per `just` recipe).
"""

from __future__ import annotations

import os
import re
import sys

_NUM = re.compile(r"\d+")

#: Verbs the NUDGE is allowed to speak on — the ones that start a working session, run a
#: handful of times a day. The floor ignores this set entirely.
#:
#: A warning printed on every invocation is filtered out by the reader within a day, and takes
#: the rest of foldyard's stderr with it; the people it annoys most would then set
#: FOLDYARD_NO_VERSION_NUDGE and never see one again, including the nudge that mattered. So it
#: is spent where it will be read. `fy doctor` reports the window unconditionally for anyone
#: who wants to ask.
NUDGE_VERBS = frozenset({"up", "box", "host"})


def _parse(raw: str | None) -> tuple[int, ...] | None:
    """A version as a comparable tuple, or ``None`` when it cannot be ordered.

    Deliberately tolerant rather than PEP 440: leading ``v`` is dropped and a trailing
    pre-release marker is ignored, so ``0.2.0rc1`` satisfies a ``0.2.0`` floor. Anything with
    no leading numeric component is ``None``. So is anything carrying a PEP 440 local segment
    (a ``+``) — which covers ``"0+unknown"``, what ``foldyard.__version__`` returns from a bare
    source-tree import *so that a floor does not trust it*, and equally a local dev build whose
    number says nothing about what it contains. Unparseable must mean "cannot tell", never
    "very old".
    """
    if not isinstance(raw, str) or not raw or "+" in raw:
        return None
    head = raw.lstrip("vV")
    parts: list[int] = []
    for chunk in head.split("."):
        m = _NUM.match(chunk)
        if m is None:
            break
        parts.append(int(m.group()))
    return tuple(parts) or None


def version_gate(
    installed: str,
    minimum: str | None,
    recommended: str | None,
    *,
    quiet_nudge: bool = False,
    in_box: bool = False,
) -> tuple[str | None, bool]:
    """Pure policy: ``(message, should_abort)`` for one set of versions.

    Pure so the policy is testable without an install, a config or a terminal.
    """
    have = _parse(installed)
    floor = _parse(minimum)
    want = _parse(recommended)

    if floor is not None and (have is None or have < floor):
        # An unknown installed version cannot be *proven* to violate the floor, and refusing
        # to run a source checkout is worse than the drift the floor guards against — so say
        # so loudly and let it through.
        return _floor_message(installed, minimum, in_box=in_box, certain=have is not None), (
            have is not None
        )

    if want is not None and have is not None and have < want and not quiet_nudge:
        return (
            f"▸ foldyard {installed} is older than the {recommended} this repo expects. "
            f"{_fix(in_box)}"
            "  (silence: FOLDYARD_NO_VERSION_NUDGE=1)",
            False,
        )

    return None, False


def _fix(in_box: bool) -> str:
    """The upgrade instruction for where you are.

    Inside the box, ``uv tool install`` is the wrong advice twice over: the box has no egress
    for PyPI under default-deny, and the bootstrap reinstalls foldyard to match the host at
    every ``fy box up`` — so a hand-install would be silently undone.
    """
    if in_box:
        return "Run `fy box up` from the Mac."
    return "Run `uv tool install --upgrade foldyard`."


def _floor_message(installed: str, minimum: str | None, *, in_box: bool, certain: bool) -> str:
    if not certain:
        return (
            f"▸ Could not determine the installed foldyard version ({installed!r}), and this "
            f"repo declares a floor of {minimum}. Proceeding, but behaviour is unverified. "
            f"{_fix(in_box)}"
        )
    return (
        f"✗ This repo needs foldyard >= {minimum}; you have {installed}.\n\n"
        f"  Its foldyard.toml declares settings an older fy ignores SILENTLY rather than\n"
        f"  failing on, so this stops here instead of running with half of them applied.\n\n"
        f"  {_fix(in_box)}"
    )


def gate_or_abort(verb: str | None = None) -> None:
    """Apply the declared window to this process; print and ``SystemExit(1)`` on a violation.

    ``verb`` is the top-level command being run; the nudge speaks only for :data:`NUDGE_VERBS`,
    the floor for all of them (a stale ``fy`` misreads the config that drives every verb, so
    the refusal cannot be scoped to a few).

    Best-effort by construction — a missing/unreadable ``foldyard.toml`` yields no declaration
    and therefore no opinion, which is what ``fy init`` in an empty directory needs.
    """
    from . import config

    try:
        minimum = config.min_foldyard_version()
        recommended = config.recommended_foldyard_version()
        if minimum is None and recommended is None:
            return
        from . import __version__

        message, abort = version_gate(
            __version__,
            minimum,
            recommended,
            quiet_nudge=(
                verb not in NUDGE_VERBS or bool(os.environ.get("FOLDYARD_NO_VERSION_NUDGE"))
            ),
            in_box=config.in_box(),
        )
    except Exception:  # pragma: no cover — a version check must never be the thing that breaks fy
        return
    if message:
        print(message, file=sys.stderr)
    if abort:
        raise SystemExit(1)
