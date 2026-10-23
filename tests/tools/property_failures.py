"""The weekly property run's failure summary — what the notifier posts, as one markdown file.

.github/workflows/property-explore.yml runs the property tier with ``--junitxml`` and tees its output
to a log; on a failure this turns the two into the summary
.github/scripts/notify-property-failure.sh posts (or, in a dry run, prints): the run's URL, the
failing tests, and each one's falsifying example(s) as hypothesis printed them, truncated.

The JUnit report is the primary source, one ``<failure>``/``<error>`` per test with its own text,
so an example is never attributed to the wrong test. Without one (pytest killed by the step's
timeout, or a crash before it wrote a report) the summary carries the log's tail instead.

Everything read here is untrusted text: a falsifying example holds whatever a strategy generated.
It only ever lands inside a code fence longer than any backtick run in it, where markdown renders
nothing and an ``@name`` notifies no one. Stdlib only: it runs after a failed pytest, from whatever
Python the runner has.
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Per block and in all: a GitHub issue body or comment holds 65,536 characters.
BLOCK_LIMIT = 4000
TOTAL_LIMIT = 40000
LOG_TAIL_LINES = 80

# Where hypothesis's report on a failing case starts: "Falsifying example:" (and its explicit and
# stateful forms) before 6.15x, "Failing test case:" after.
_MARKER = re.compile(r"^\s*(Falsifying (explicit )?example|Failing test case)\b")
# The margin pytest puts on an exception's lines: `E   ` for the error, `    | ` inside an
# exception group (hypothesis raises one for several distinct failures in one test).
_MARGIN = re.compile(r"^(E(?: |$)|\s*\|(?: |$))")


@dataclass
class Failure:
    test: str
    examples: list[str] = field(default_factory=list)


def _test_id(case: ET.Element) -> str:
    """``tests.test_x::test_y`` (``tests.test_x.Class::runTest`` for a class): JUnit's own two
    fields, which name the test without guessing the file path back from the dotted one."""
    return f"{case.get('classname', '')}::{case.get('name', '')}"


def examples(text: str) -> list[str]:
    """Each falsifying-example block in one test's failure text, de-margined and dedented, with
    the ``@reproduce_failure`` line that follows it when hypothesis printed one."""
    found: list[str] = []
    block: list[str] | None = None
    for line in text.splitlines():
        margin = _MARGIN.match(line)
        content = line[margin.end() :] if margin else None
        if block is not None:
            ended = (
                content is None
                or _MARKER.match(content)
                or content.lstrip().startswith(("Traceback", "+-"))
            )
            if not ended:
                assert content is not None
                block.append(content)
                continue
            found.append(textwrap.dedent("\n".join(block)).strip())
            block = None
        if content is not None and _MARKER.match(content):
            block = [content]
    if block is not None:
        found.append(textwrap.dedent("\n".join(block)).strip())
    return found


def failures(junit: Path) -> list[Failure]:
    """One per failing or erroring test, in report order."""
    out = []
    for case in ET.parse(junit).getroot().iter("testcase"):
        for kind in ("failure", "error"):
            for node in case.findall(kind):
                text = "\n".join(filter(None, [node.get("message", ""), node.text or ""]))
                out.append(Failure(_test_id(case), examples(text)))
    return out


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… ({len(text) - limit} more characters in the run's log)"


def fence(text: str, lang: str = "text") -> str:
    """``text`` in a code fence no backtick run inside it can close."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def summary(junit: Path | None, log: Path | None, run_url: str, commit: str, budget: str) -> str:
    parts = [f"The randomised property run (property-explore.yml) failed: {run_url}", ""]
    if commit:
        parts.append(f"- commit: `{commit}`")
    if budget:
        parts.append(f"- budget: max_examples={budget} (the explore profile, tests/conftest.py)")
    found = failures(junit) if junit and junit.is_file() else []
    if found:
        parts += ["", "### Failing tests", ""]
        parts += [f"- `{f.test}`" for f in found]
        parts += ["", "### Falsifying examples", ""]
        for f in found:
            parts += [f"#### `{f.test}`", ""]
            if not f.examples:
                parts += ["(hypothesis printed no example: see the run's log)", ""]
            for ex in f.examples:
                parts += [fence(_clip(ex, BLOCK_LIMIT)), ""]
        parts += [
            "The example database is cached between runs, so next week's run replays these "
            "first. To replay one anywhere, add its `@reproduce_failure` line to the test.",
        ]
    else:
        tail = (
            log.read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:]
            if log and log.is_file()
            else []
        )
        parts += [
            "",
            "pytest left no failing test in its report (a timeout, a crash, or a step before the "
            "tests failed). The log's last lines:",
            "",
            fence("\n".join(tail) or "(no log)"),
        ]
    return _clip("\n".join(parts).strip() + "\n", TOTAL_LIMIT)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The property run's failure summary, as markdown.")
    ap.add_argument("--junit", type=Path)
    ap.add_argument("--log", type=Path)
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--commit", default="")
    ap.add_argument("--budget", default="")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    args.out.write_text(summary(args.junit, args.log, args.run_url, args.commit, args.budget))
    return 0


if __name__ == "__main__":
    sys.exit(main())
