"""The host-tier shards' timing table — per module, in run order, from pytest's JUnit report.

Each host e2e shard (.github/workflows/foldyard-e2e.yml) runs pytest with ``--junitxml`` and then
``python tests/tools/e2e_timings.py <junit.xml> --title '<job · shard>' >> $GITHUB_STEP_SUMMARY``,
so the run's page says where a shard's minutes went without opening a 2000-line log. A testcase's
``time`` is setup + call + teardown (pytest's default ``junit_duration_report``), so a module's sum
carries its module-scoped fixtures too — the VM recreates and the ``fy up``s that ARE the cost.
Stdlib only: it runs after a failed or cancelled pytest, from whatever Python the runner has.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Module:
    module: str
    seconds: float = 0.0
    passed: int = 0
    failed: int = 0
    skipped: int = 0


def modules(junit: Path) -> list[Module]:
    """One row per module, in the order the report first names it (pytest's run order). An
    absent or unreadable report — pytest never got as far as writing one — is no rows."""
    try:
        root = ET.parse(junit).getroot()
    except (OSError, ET.ParseError):
        return []
    rows: dict[str, Module] = {}
    for case in root.iter("testcase"):
        # `tests.test_x` or, for a test in a class, `tests.test_x.TestThing`: the module is the
        # last dotted part named like one.
        parts = (case.get("classname") or "?").split(".")
        name = next((p for p in reversed(parts) if p.startswith("test_")), parts[-1])
        row = rows.setdefault(name, Module(name))
        row.seconds += float(case.get("time") or 0)
        if case.find("failure") is not None or case.find("error") is not None:
            row.failed += 1
        elif case.find("skipped") is not None:
            row.skipped += 1
        else:
            row.passed += 1
    return list(rows.values())


def _clock(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def render(rows: list[Module], title: str) -> str:
    lines = [f"### {title}", ""]
    if not rows:
        lines.append("_no test report — pytest did not finish writing one; see the job log._")
        return "\n".join(lines)
    lines += [
        "| module | time | passed | failed | skipped |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        lines.append(
            f"| {r.module} | {_clock(r.seconds)} | {r.passed} | {r.failed} | {r.skipped} |"
        )
    total = Module(
        "total",
        sum(r.seconds for r in rows),
        sum(r.passed for r in rows),
        sum(r.failed for r in rows),
        sum(r.skipped for r in rows),
    )
    lines.append(
        f"| **total** | **{_clock(total.seconds)}** | {total.passed} | {total.failed} | {total.skipped} |"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The host-tier shards' timing table.")
    parser.add_argument("junit", type=Path)
    parser.add_argument("--title", default="host e2e")
    args = parser.parse_args(argv)
    print(render(modules(args.junit), args.title))
    return 0


if __name__ == "__main__":
    sys.exit(main())
