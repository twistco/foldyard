"""experimental.py — what foldyard ships as EXPERIMENTAL, and what would graduate each one.

"Experimental" makes one of two claims, and they graduate differently:

- **may change** — the interface (a config key, an output format, a payload) isn't frozen. It
  graduates when a maintainer decides to freeze it: a decision, not evidence.
- **unproven in use** — the interface is settled but nobody has lived with it yet. It graduates on
  evidence. foldyard has no telemetry, so in practice that is a maintainer's own day-to-day use on
  a real project — and it proves the feature for that kind of project only.

Each entry names the config that turns the feature on, what is unproven, and what would graduate
it, so the label is a to-do list rather than a mood: without the last field, labels either stay
forever (and readers learn to ignore them) or are dropped quietly with nothing having changed.

Everything that SAYS experimental derives from here: the ``experimental`` row of ``fy doctor``
(what the adopted config turns on — :func:`doctor_row`) and the ``*(experimental)*`` tags in
docs/configuration.md (``tests/test_experimental.py`` holds docs and register together, both
ways). A changelog fragment introducing one carries ``experimental: true`` in its frontmatter
(``.changes/README.md``). Graduating one is deleting its entry and its doc tag, plus a
``.changes/`` entry under Changed that says so.

Stdlib only; reads config through the ambient readers (on the host, the ADOPTED snapshot).
"""

from __future__ import annotations

from dataclasses import dataclass

MAY_CHANGE = "may change"
UNPROVEN = "unproven in use"


@dataclass(frozen=True)
class Feature:
    key: str  # "<table>.<field>", as docs/configuration.md lists it
    value: str  # the value that turns it on
    reader: str  # the `config` reader that resolves it (env var included), by name
    kinds: tuple[str, ...]  # MAY_CHANGE and/or UNPROVEN
    unproven: str  # what isn't known yet — shown by `fy doctor`
    graduates_when: str  # what would end it — for maintainers; reviewed when it's met
    doc_mark: str  # the exact text in docs/configuration.md that carries its tag

    @property
    def label(self) -> str:
        table, field = self.key.split(".", 1)
        return f'[{table}] {field} = "{self.value}"'


REGISTER: tuple[Feature, ...] = (
    Feature(
        key="machine.runtime",
        value="gvisor",
        reader="machine_runtime",
        kinds=(UNPROVEN,),
        unproven=(
            "no maintainer runs a project under it day to day yet, and CI can't (it needs nested "
            "virtualisation); the box's runtime strip has been seen to matter only once, on a "
            "podman 6 guest"
        ),
        graduates_when=(
            "a maintainer has run a real project under it day to day for a month, with nothing "
            "filed against it in that time still open"
        ),
        doc_mark="- **`runtime`** *(experimental)*",
    ),
    Feature(
        key="machine.vmtype",
        value="krunkit",
        reader="machine_vmtype",
        kinds=(UNPROVEN,),
        unproven=(
            "krunkit is experimental upstream, macOS/arm64 only, and not covered by CI; foldyard "
            "never picks it on its own"
        ),
        graduates_when=(
            "upstream calls krunkit stable, and a maintainer has run a real project on it day to "
            "day for a month with nothing filed against it in that time still open"
        ),
        doc_mark="`krunkit` *(experimental)*",
    ),
    Feature(
        key="machine.monitor",
        value="observe",
        reader="machine_monitor",
        kinds=(MAY_CHANGE, UNPROVEN),
        unproven=(
            "its event format, `fy monitor --json` output and hook payload aren't frozen; it "
            "passes the host-tier e2e on Linux and WSL2 hosts, but no maintainer runs it day to "
            "day yet"
        ),
        graduates_when=(
            "the event format and hook payload are declared stable (docs/monitor-pipeline.md's "
            "open questions settled), and a maintainer has run it on a real project day to day "
            "for a month with nothing filed against it in that time still open"
        ),
        doc_mark="- **`monitor`** *(experimental)*",
    ),
)


def turned_on() -> list[Feature]:
    """The register's features the current config turns on. A value the reader refuses is
    preflight's to report, not this one's: it reads as off here."""
    from . import config

    on = []
    for f in REGISTER:
        try:
            value = getattr(config, f.reader)()
        except SystemExit:
            continue
        if value == f.value:
            on.append(f)
    return on


def doctor_row() -> tuple[bool | None, str, str]:
    """``(ok, name, detail)`` for ``fy doctor``. Always green: turning on an experimental feature
    is a choice, not a fault — the row exists so the choice is never forgotten."""
    on = turned_on()
    if not on:
        return True, "experimental", "none turned on"
    detail = "; ".join(f"{f.label} — {', '.join(f.kinds)}: {f.unproven}" for f in on)
    return True, "experimental", detail
