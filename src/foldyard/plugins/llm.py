"""llm plugin — the ``llm`` axis: mock/cassette vs REAL LLM traffic.

  off     default — the app mocks AI and the data services replay Claude-authored
          cassettes; no LLM egress, no Langfuse, zero secrets
  record  real Vertex calls (via ADC — requires ``gcp=sa``); every completion is also
          written as a cassette draft (``TANGIBLE_LLM_MODE=record``)
  live    real Vertex calls, no cassette drafts (``TANGIBLE_LLM_MODE=`` empty)

Like auth0-sim this axis grants NO credential and runs NO daemon: it's a pure app-behaviour
swap. What credential (if any) real traffic consumes is the CONSUMER's wiring, not this
plugin's: Tangible routes ``record``/``live`` through Vertex/ADC, so its ``foldyard.toml``
declares ``[[require]] axis = "llm" … needs = "gcp"`` next to the overlays that create that
coupling — a consumer on another provider would declare a different requirement (or none).
This plugin carries no guard code, reads no other plugin's posture, and names no provider.
The overlays are consumer-supplied (``[plugins.llm]``); for Tangible they flip ``AI_MOCK_MODE``
off on the app and extend ADC + ``TANGIBLE_LLM_MODE`` to the data services. Langfuse deliberately
rides this axis too: the data services get their ADC (and thus their runtime GSM fetch of
the Langfuse keys) from the *llm* overlay, not the identity overlay, so a cassette run
under ``gcp=sa`` still traces nowhere.

DECLARED plugin (loads only on a ``[plugins.llm]`` table), and consumer-shaped like
auth0-sim: it migrates into the consumer repo via ``[plugins].load`` in spinout Phase 2
(decisions D3/D4). Stdlib only (loads on the recipe hot path).
"""

from __future__ import annotations

from .. import config
from . import Axis, Plugin

_BLURB = {
    "off": "LLM mocked/cassettes — no AI egress, zero secrets",
    "record": "REAL Vertex LLM + cassette drafts (needs gcp=sa)",
    "live": "REAL Vertex LLM, no cassette drafts (needs gcp=sa)",
}


class LlmPlugin(Plugin):
    name = "llm"

    def axes(self) -> list[Axis]:
        # Self-gate on the consumer declaring [plugins.llm] (registry plan Step D — the plugin
        # already only LOADS when the table is declared, Step C; belt-and-suspenders for a
        # bare/extra-injected plugin). Without a compose override the rungs would change nothing.
        if not config.llm_declared():
            return []
        return [
            Axis(
                name="llm",
                rungs=("off", "record", "live"),
                blurb=_BLURB,
                # No `requires` here: what credential real record/live traffic consumes is
                # WIRING, declared consumer-side. Tangible's foldyard.toml carries
                # `[[require]] axis = "llm" … needs = "gcp"` beside the Vertex overlays that
                # create the coupling; another consumer's provider may need a different axis
                # or none, and this plugin stays provider-agnostic.
            )
        ]

    # The llm compose overlays are declared as `[[overlay]]` entries in foldyard.toml now
    # (config-only, matched on their `when`) — see docs/compose-overlays.md:
    #   llm=record|live → compose.llm.yml   (real Vertex; record also writes cassette drafts)
    #   llm=live        → compose.llm-live.yml layered AFTER it (drops the drafts; later -f wins)
    # This plugin keeps only the axis declaration; the gcp requirement rides the same config
    # (`[[require]]`), beside those overlays.
