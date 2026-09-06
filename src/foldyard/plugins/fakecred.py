"""fakecred plugin — a zero-secret TESTING axis pair for the mode/state machinery itself.

The mode substrate's interesting behaviours — TTL expiry + the settle cascade, daemon
start/stop/restart on posture change, capability probes going DEGRADED and recovering,
orphan reaping — all normally need real credentials (a PAM grant, a GitHub App) to observe
live. This plugin fakes exactly enough of a credential mechanism to exercise every one of
them on a real host with `fy host` + `fy mode` + `fy clock`, with zero secrets anywhere
(docs/testing-modes.md is the walkthrough):

  fakecred   off | on | user     the "credential": on runs the fake minter daemon; user is
                                 an EMERGENCY rung (TTL + auto-revert, like gcp=user)
  fakedep    off | on            a dependent behaviour rung: `on` REQUIRES fakecred≠off
                                 (a mode_issues error, like llm≠off requires gcp=sa) — so a
                                 fakecred TTL lapse exercises the expiry settle cascade

The daemon is ``fakecred_minter.py`` (stdlib HTTP, staged per-project like the gcp minter,
so orphan reaping works on it too); the capability probe reads
``<state_dir>/fakecred-capability`` — ``echo lapsed >`` it to watch the axis degrade,
``echo ok >`` to watch it recover. DECLARED plugin: loads only on ``[plugins.fakecred]``.
Stdlib only."""

from __future__ import annotations

import sys
from pathlib import Path

from .. import config
from . import Axis, CapabilityProbe, Plugin, Requires

FAKECRED_DIR = Path(__file__).resolve().parent

_BLURB = {
    "off": "no fake credential — zero secrets (like everything else here)",
    "on": "fake minter serves dummy tokens (testing the daemon/probe machinery)",
    "user": "EMERGENCY (fake): TTL-bound, auto-reverts — testing expiry + settle",
}

_DEP_BLURB = {
    "off": "dependent behaviour off",
    "on": "dependent behaviour on (requires fakecred≠off — testing the settle cascade)",
}


def capability_file() -> Path:
    """The toggle the probe (and the fake minter's /token) read: missing or ``ok`` = capable;
    anything else = lapsed, with the content shown as the DEGRADED detail."""
    return config.state_dir() / "fakecred-capability"


def _staged_minter() -> Path:
    """Per-project staged launch path — the project-scoped cmdline marker that makes the
    supervisor's orphan reaping (``_is_our_daemon``) work on this daemon too."""
    return config.state_dir() / "fakecred_minter.py"


class FakecredPlugin(Plugin):
    name = "fakecred"

    def axes(self) -> list[Axis]:
        return [
            Axis(
                name="fakecred",
                rungs=("off", "on", "user"),
                blurb=_BLURB,
                daemon="fake-minter",
                emergency=("user",),
            ),
            Axis(
                name="fakedep",
                rungs=("off", "on"),
                blurb=_DEP_BLURB,
                # The dependency that exercises the settle cascade: like llm≠off without
                # gcp=sa, a dependent rung whose credential axis rests at default can only
                # fail. Declared as data so the rig exercises the same Requires path real
                # consumers use.
                requires=(
                    Requires(
                        when=("on",),
                        axis="fakecred",
                        accepts=("on", "user"),
                        reason="the fake credential",
                    ),
                ),
            ),
        ]

    def daemons(self, mode: dict) -> dict[str, dict]:
        if mode.get("fakecred", "off") == "off":
            return {}
        port = config.fakecred_port()
        return {
            f"fake-minter{config.worktree_suffix()}": {
                "label": "fake credential minter (testing)",
                "port": port,
                "stage": [(str(FAKECRED_DIR / "fakecred_minter.py"), str(_staged_minter()))],
                "cmd": [sys.executable, str(_staged_minter())],
                "env": {
                    "LISTEN_PORT": str(port),
                    "FAKECRED_CAPABILITY_FILE": str(capability_file()),
                },
                "requires": [],
            }
        }

    def capability_probes(self, mode: dict) -> list[CapabilityProbe]:
        if mode.get("fakecred", "off") == "off":
            return []

        def _check() -> tuple[bool, str]:
            # Only a MISSING file is the healthy resting state; any other read failure (perms,
            # a directory) propagates — the supervisor's probe runner surfaces it as failing.
            try:
                text = capability_file().read_text().strip()
            except FileNotFoundError:
                return True, "fake capability ok (no lapse file)"
            if text in ("", "ok"):
                return True, "fake capability ok"
            return False, f"fake capability lapsed ({text}) — `echo ok > {capability_file()}`"

        # Short interval: this axis exists to watch the machinery react, so feedback in ~2s.
        return [CapabilityProbe(axis="fakecred", name="fakecred", check=_check, interval=2.0)]
