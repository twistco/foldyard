"""The weekly randomised property run (.github/workflows/property-explore.yml) and its parts.

Nothing here runs on the week it matters, so it is pinned in the fast suite: the `explore` profile
is randomised and keeps its database where the workflow caches it, the workflow runs every module
that uses hypothesis, the failure summary pulls each falsifying example out of pytest's report in
the forms hypothesis prints them, and the notifier posts the summary only from a file — or, in a
dry run, prints it and posts nothing.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from hypothesis import settings
from hypothesis.database import DirectoryBasedExampleDatabase

from conftest import EXPLORE_EXAMPLES, hypothesis_profile
from pbt import scaled_examples
from tools import property_failures as pf

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
WORKFLOW = ROOT / ".github" / "workflows" / "property-explore.yml"
NOTIFIER = ROOT / ".github" / "scripts" / "notify-property-failure.sh"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> list[dict]:
    return _workflow()["jobs"]["explore"]["steps"]


def _step(name: str) -> dict:
    return next(s for s in _steps() if s.get("name") == name)


# ── the profile ─────────────────────────────────────────────────────────────────────────


def test_explore_is_randomised_with_a_larger_budget_and_its_database_on():
    explore, ci = settings.get_profile("explore"), settings.get_profile("ci")
    assert not explore.derandomize
    assert EXPLORE_EXAMPLES > 10 * ci.max_examples
    # A manual run's override, else the default.
    override = os.environ.get("FOLDYARD_HYPOTHESIS_MAX_EXAMPLES")
    assert explore.max_examples == int(override or EXPLORE_EXAMPLES)
    assert explore.deadline is None
    assert explore.suppress_health_check == ci.suppress_health_check
    assert isinstance(explore.database, DirectoryBasedExampleDatabase)


@pytest.mark.parametrize(
    ("env", "profile"),
    [
        ({}, "dev"),
        ({"CI": "true"}, "ci"),
        ({"CI": "true", "FOLDYARD_HYPOTHESIS_PROFILE": "explore"}, "explore"),
        ({"FOLDYARD_HYPOTHESIS_PROFILE": "explore"}, "explore"),
    ],
)
def test_the_named_profile_wins_over_ci(env, profile):
    assert hypothesis_profile(env) == profile


def test_a_tests_own_budget_scales_with_the_profile_and_only_explore_changes_it():
    # dev and ci both run hypothesis's default 100, so there the explicit budgets mean what
    # they say; under explore each grows by the same factor as the default.
    assert settings.get_profile("ci").max_examples == settings.get_profile("dev").max_examples
    assert settings.get_profile("dev").max_examples == 100
    before = hypothesis_profile(os.environ)
    try:
        settings.load_profile("explore")
        assert scaled_examples(300) == 300 * settings.get_profile("explore").max_examples // 100
        settings.load_profile("dev")
        assert scaled_examples(300) == 300
    finally:
        settings.load_profile(before)


# ── the workflow ────────────────────────────────────────────────────────────────────────


def _hypothesis_modules() -> set[str]:
    return {
        f"tests/{p.name}"
        for p in TESTS.glob("test_*.py")
        if re.search(r"^(from hypothesis|import hypothesis)", p.read_text(), re.M)
        and p.name != Path(__file__).name
    }


def test_the_workflow_runs_every_module_that_uses_hypothesis():
    listed = _workflow()["env"]["PROPERTY_MODULES"].split()
    assert sorted(listed) == sorted(_hypothesis_modules())
    run = _step("Property tier (explore profile)")
    assert run["env"]["FOLDYARD_HYPOTHESIS_PROFILE"] == "explore"
    assert "$PROPERTY_MODULES" in run["run"] and "--junitxml" in run["run"]


def test_the_cache_holds_the_explore_profiles_database():
    db = settings.get_profile("explore").database
    assert isinstance(db, DirectoryBasedExampleDatabase)
    path = Path(db.path).relative_to(ROOT).as_posix()
    restore, save = _step("Restore the example database"), _step("Save the example database")
    assert restore["with"]["path"] == save["with"]["path"] == path
    # Each run saves its own entry and the next restores the newest by the shared prefix.
    assert restore["with"]["key"] == save["with"]["key"]
    assert "github.run_id" in save["with"]["key"]
    assert save["with"]["key"].startswith(restore["with"]["restore-keys"])
    assert save["if"] == "always()"


def test_it_runs_weekly_and_by_hand_with_a_dry_run():
    on = _workflow()[True]  # YAML 1.1 reads the bare key `on` as a boolean
    assert len(on["schedule"]) == 1
    minute, _hour, dom, month, dow = on["schedule"][0]["cron"].split()
    assert minute not in ("0", "*") and dom == month == "*" and dow.isdigit()
    assert set(on["workflow_dispatch"]["inputs"]) == {"max_examples", "dry_run"}


def test_the_notifier_names_one_login_and_writes_only_issues():
    wf = _workflow()
    assert re.fullmatch(r"[A-Za-z0-9-]+", wf["env"]["NOTIFY_LOGIN"])
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["explore"]["permissions"] == {"contents": "read", "issues": "write"}
    notify = _step("Tell the operator")
    assert notify["run"] == ".github/scripts/notify-property-failure.sh"
    assert notify["if"].startswith("failure()")


def test_no_run_script_interpolates_an_expression():
    # Anything reaching a shell goes through env: an expression expanded into `run:` is code.
    for step in _steps():
        assert "${{" not in step.get("run", ""), step.get("name")


# ── the failure summary ─────────────────────────────────────────────────────────────────

PLAIN = """\
    def test_x(scenario):
>       assert False
E       AssertionError: lost
E       Failing test case: test_x(
E           scenario=[('start', 1), ('stop', 1)],
E       )
E
E       You can reproduce this test case by temporarily adding @reproduce_failure('6.163.0', b'AX') as a decorator on your test function

tests/test_x.py:9: AssertionError"""

GROUP = """\
  + Exception Group Traceback (most recent call last):
  | ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
  +-+---------------- 1 ----------------
    | Traceback (most recent call last):
    | AssertionError: one
    | Falsifying example: test_y(
    |     n=1,
    | )
    +---------------- 2 ----------------
    | Traceback (most recent call last):
    | AssertionError: two
    | Falsifying example: test_y(
    |     n=2,
    | )
    +------------------------------------"""

STATEFUL = """\
E       AssertionError: drifted
E       Falsifying example:
E       state = ModeStateModel()
E       state.set_mode(wt='', axis='ident', rung='sa')
E       state.teardown()

tests/test_reconcile_model.py:300: AssertionError"""


def test_an_example_is_taken_whole_without_pytests_margin():
    (block,) = pf.examples(PLAIN)
    assert block.splitlines()[0] == "Failing test case: test_x("
    assert "    scenario=[('start', 1), ('stop', 1)]," in block.splitlines()
    assert block.endswith("as a decorator on your test function")
    assert "AssertionError" not in block and "tests/test_x.py" not in block


def test_each_failure_in_an_exception_group_is_its_own_example():
    assert pf.examples(GROUP) == [
        "Falsifying example: test_y(\n    n=1,\n)",
        "Falsifying example: test_y(\n    n=2,\n)",
    ]


def test_a_stateful_example_keeps_its_steps():
    (block,) = pf.examples(STATEFUL)
    assert block.splitlines() == [
        "Falsifying example:",
        "state = ModeStateModel()",
        "state.set_mode(wt='', axis='ident', rung='sa')",
        "state.teardown()",
    ]


def _junit(tmp_path: Path, cases: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite>{cases}</testsuite></testsuites>')
    return path


def test_the_summary_names_each_failing_test_with_its_examples(tmp_path):
    from xml.sax.saxutils import escape

    junit = _junit(
        tmp_path,
        '<testcase classname="tests.test_x" name="test_ok" time="1"/>'
        f'<testcase classname="tests.test_x" name="test_x" time="1">'
        f'<failure message="AssertionError: lost">{escape(PLAIN)}</failure></testcase>',
    )
    log = tmp_path / "pytest.log"
    log.write_text(
        "platform linux\nhypothesis profile 'explore' -> deadline=None, print_blob=True, "
        "max_examples=5000, suppress_health_check=()\n"
    )
    text = pf.summary(junit, log, "https://run/1", "abc")
    assert text.startswith(
        "The randomised property run (property-explore.yml) failed: https://run/1"
    )
    assert "- `tests.test_x::test_x`" in text and "test_ok" not in text
    assert "```text\nFailing test case: test_x(" in text
    assert "- hypothesis profile: explore, max_examples=5000" in text


def test_without_a_report_the_summary_carries_the_logs_tail(tmp_path):
    log = tmp_path / "pytest.log"
    log.write_text("\n".join(f"line {i}" for i in range(500)))
    text = pf.summary(tmp_path / "missing.xml", log, "https://run/1", "")
    assert "pytest left no failing test" in text
    assert "line 499" in text and "line 0\n" not in text


def test_generated_text_cannot_close_its_fence():
    hostile = "```\n@daaain ping\n````"
    fenced = pf.fence(hostile)
    assert fenced.startswith("`````text\n") and fenced.endswith("\n`````")


def test_the_summary_is_truncated_to_fit_an_issue(tmp_path):
    huge = "E       Falsifying example: test_x(\n" + "E           n=1,\n" * 20000
    junit = _junit(
        tmp_path,
        f'<testcase classname="tests.test_x" name="test_x"><failure>{huge}</failure></testcase>',
    )
    text = pf.summary(junit, None, "https://run/1", "")
    assert len(text) < 65536
    assert "more characters in the run's log" in text


# ── the notifier ────────────────────────────────────────────────────────────────────────


def _notify(
    tmp_path: Path, *, dry_run: bool, existing: str = "", labels: str = ""
) -> tuple[subprocess.CompletedProcess, str]:
    summary = tmp_path / "summary.md"
    # Untrusted text: it must reach gh as a file and never be run.
    summary.write_text(f"falsified $(touch {tmp_path}/pwned) `touch {tmp_path}/pwned2`\n")
    calls = tmp_path / "gh.calls"
    stub = f"""
gh() {{
  local a prev=""
  printf '%s|' "$@" >> {calls}; echo >> {calls}
  for a in "$@"; do [[ $prev == --body-file ]] && cp "$a" {tmp_path}/posted.md; prev=$a; done
  [[ $1 == issue && $2 == list ]] && printf '%s' "{existing}"
  [[ $1 == label && $2 == list ]] && printf '%s\n' {labels}
  return 0
}}
"""
    env = {
        "PATH": "/usr/bin:/bin",
        "SUMMARY_FILE": str(summary),
        "NOTIFY_LOGIN": "daaain",
        "ISSUE_LABEL": "property-tests",
        "GITHUB_REPOSITORY": "twistco/foldyard",
        "DRY_RUN": "true" if dry_run else "false",
    }
    out = subprocess.run(
        ["bash", "-c", stub + 'source "$0"', str(NOTIFIER)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert not (tmp_path / "pwned").exists() and not (tmp_path / "pwned2").exists()
    return out, calls.read_text() if calls.exists() else ""


@pytest.mark.spawns("bash")
def test_a_dry_run_prints_the_issue_and_posts_nothing(tmp_path):
    out, calls = _notify(tmp_path, dry_run=True)
    assert out.returncode == 0, out.stderr
    assert "would create label property-tests if missing" in out.stdout
    assert "@daaain: the randomised property run found a failure." in out.stdout
    assert "falsified $(touch" in out.stdout
    # Only the read-only lookup.
    assert [c.split("|")[:2] for c in calls.splitlines()] == [["issue", "list"]]


@pytest.mark.spawns("bash")
def test_with_no_open_issue_it_opens_one_assigned_and_mentioning_the_login(tmp_path):
    out, calls = _notify(tmp_path, dry_run=False)
    assert out.returncode == 0, out.stderr
    verbs = [c.split("|")[:2] for c in calls.splitlines()]
    assert verbs == [["issue", "list"], ["label", "list"], ["label", "create"], ["issue", "create"]]
    create = calls.splitlines()[-1].split("|")
    assert create[create.index("--assignee") + 1] == "daaain"
    assert create[create.index("--label") + 1] == "property-tests"
    assert (tmp_path / "posted.md").read_text().startswith("@daaain: ")


@pytest.mark.spawns("bash")
def test_an_existing_label_is_used_not_recreated(tmp_path):
    # --search matches substrings: only the exact name counts as the label being there.
    out, calls = _notify(tmp_path, dry_run=False, labels="property-tests-old property-tests")
    assert out.returncode == 0, out.stderr
    verbs = [c.split("|")[:2] for c in calls.splitlines()]
    assert verbs == [["issue", "list"], ["label", "list"], ["issue", "create"]]


@pytest.mark.spawns("bash")
def test_with_an_open_issue_it_comments_there(tmp_path):
    out, calls = _notify(tmp_path, dry_run=False, existing="42")
    assert out.returncode == 0, out.stderr
    verbs = [c.split("|")[:3] for c in calls.splitlines()]
    assert verbs == [["issue", "list", "-R"], ["issue", "comment", "42"]]
    assert "falsified $(touch" in (tmp_path / "posted.md").read_text()


@pytest.mark.spawns("bash")
def test_a_login_that_is_not_one_is_refused(tmp_path):
    summary = tmp_path / "s.md"
    summary.write_text("x\n")
    env = {
        "PATH": "/usr/bin:/bin",
        "SUMMARY_FILE": str(summary),
        "NOTIFY_LOGIN": "daaain; rm -rf /",
        "ISSUE_LABEL": "property-tests",
        "GITHUB_REPOSITORY": "twistco/foldyard",
    }
    out = subprocess.run(["bash", str(NOTIFIER)], capture_output=True, text=True, env=env)
    assert out.returncode == 1
    assert "not a GitHub login" in out.stdout
