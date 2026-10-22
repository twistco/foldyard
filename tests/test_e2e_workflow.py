"""The live e2e workflow's contract with the host-tier modules (.github/workflows/foldyard-e2e.yml).

The host tier runs as SHARDS: each host job (`lima-host-e2e` on Linux, `wsl2-host-e2e` inside WSL2)
is a matrix whose every entry is its own runner, VM and pytest process over the modules it lists.
A module listed in no shard runs nowhere and nothing goes red to say so; a module listed twice
pays a VM's worth of time for nothing. So the matrix is pinned here, in the hermetic suite, against
the modules themselves: a host-tier module is test_e2e.py (the example + worktree stacks, which the
host jobs run on a real VM) and every `test_*_e2e.py` built on the host-tier substrate
(`e2e_host`). The in-box proxy/box e2es stay `live-e2e`'s (`-k e2e` in a container).

Beside the matrix: the substrate's two switches the jobs set (`FY_E2E_MODULES`, the session's base
VM), the timing table each shard writes to its run page, and the WSL2 job's env forwarding.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import e2e_host
from tools import e2e_timings

TESTS = Path(__file__).resolve().parent
WORKFLOW = TESTS.parent / ".github" / "workflows" / "foldyard-e2e.yml"
HOST_JOBS = ("lima-host-e2e", "wsl2-host-e2e")


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _shards(job: str) -> dict[str, list[str]]:
    return {s["name"]: s["modules"] for s in _jobs()[job]["strategy"]["matrix"]["shard"]}


def _host_tier_modules() -> set[str]:
    found = {"tests/test_e2e.py"}
    for path in TESTS.glob("test_*_e2e.py"):
        if re.search(r"^(from e2e_host import|import e2e_host)", path.read_text(), re.M):
            found.add(f"tests/{path.name}")
    return found


def test_the_host_tier_is_what_the_jobs_ran_before_sharding():
    # The discovery itself, pinned once: the thirteen modules the unsharded jobs listed by name.
    assert {Path(m).stem.removeprefix("test_").removesuffix("_e2e") for m in _host_tier_modules()} == {
        "e2e", "basevm", "box", "host_daemons", "machine", "monitor", "mount_visibility",
        "mounts", "probes", "reclaim", "verify", "wall", "worktree",
    }  # fmt: skip


@pytest.mark.parametrize("job", HOST_JOBS)
def test_every_host_tier_module_runs_in_exactly_one_shard(job):
    shards = _shards(job)
    listed = [m for modules in shards.values() for m in modules]
    twice = sorted({m for m in listed if listed.count(m) > 1})
    assert not twice, f"{job}: in more than one shard: {twice}"
    missing = sorted(_host_tier_modules() - set(listed))
    assert not missing, f"{job}: a host-tier module in no shard runs nowhere: {missing}"
    stray = sorted(set(listed) - _host_tier_modules())
    assert not stray, f"{job}: not a host-tier module (or no such file): {stray}"


@pytest.mark.parametrize("job", HOST_JOBS)
def test_each_shard_hands_its_module_list_to_pytest_and_the_substrate(job):
    # One list per shard, read by pytest AND by e2e_host.runs_last — never two that can drift.
    env = _jobs()[job]["env"]
    assert env["FY_E2E_MODULES"] == "${{ join(matrix.shard.modules, ' ') }}"
    steps = [s.get("run", "") for s in _jobs()[job]["steps"]]
    assert any("pytest" in run and "$FY_E2E_MODULES" in run for run in steps), steps


@pytest.mark.parametrize("job", HOST_JOBS)
def test_each_shard_runs_its_vm_on_four_cpus(job):
    assert str(_jobs()[job]["env"]["MACHINE_CPUS"]) == "4"


def test_the_wsl2_job_forwards_every_job_env_var_into_the_distro():
    # Job env is WINDOWS process env: a variable WSLENV doesn't name never reaches the distro.
    env = _jobs()["wsl2-host-e2e"]["env"]
    forwarded = {entry.split("/")[0] for entry in env["WSLENV"].split(":")}
    assert set(env) - {"WSLENV"} <= forwarded, sorted(set(env) - {"WSLENV"} - forwarded)


def test_one_aggregate_job_needs_every_other_job():
    jobs = _jobs()
    assert set(jobs["e2e"]["needs"]) == set(jobs) - {"e2e"}


# ── the substrate's switches ─────────────────────────────────────────────────────────────


def test_runs_last_is_true_only_for_the_final_module_the_session_declared():
    env = {"FY_E2E_MODULES": "tests/test_e2e.py tests/test_probes_e2e.py tests/test_monitor_e2e.py"}
    here = "/home/runner/foldyard/tests/test_monitor_e2e.py"
    assert e2e_host.runs_last(here, env)
    assert not e2e_host.runs_last("/x/tests/test_probes_e2e.py", env)
    assert not e2e_host.runs_last(here, {"FY_E2E_MODULES": "tests/test_monitor_e2e.py tests/a.py"})


@pytest.mark.parametrize("value", [None, "", "   "])
def test_runs_last_without_a_declared_list_is_false(value):
    # A local run (nothing declared) restores the VM as always.
    env = {} if value is None else {"FY_E2E_MODULES": value}
    assert not e2e_host.runs_last("/x/tests/test_monitor_e2e.py", env)


def test_the_session_base_vm_is_named_by_both_variables():
    env = {"FY_BASE_VM_NAME": "fy-ci-base", "FY_BASE_VM_STAMP": "/tmp/fy-ci-base-vm.json"}
    base = e2e_host.session_base_vm(env)
    assert base == e2e_host.SessionBase("fy-ci-base", Path("/tmp/fy-ci-base-vm.json"))


@pytest.mark.parametrize(
    "env",
    [{}, {"FY_BASE_VM_NAME": "fy-ci-base"}, {"FY_BASE_VM_STAMP": "/tmp/s.json"},
     {"FY_BASE_VM_NAME": "", "FY_BASE_VM_STAMP": "/tmp/s.json"}],
)  # fmt: skip
def test_without_both_variables_there_is_no_session_base(env):
    assert e2e_host.session_base_vm(env) is None


# ── the timing table ─────────────────────────────────────────────────────────────────────

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
<testcase classname="tests.test_machine_e2e" name="test_a" time="61.5"/>
<testcase classname="tests.test_machine_e2e.TestLifecycle" name="test_b" time="20.0"><failure message="x"/></testcase>
<testcase classname="tests.test_wall_e2e" name="test_c" time="0.4"><skipped message="no nft"/></testcase>
<testcase classname="tests.test_e2e" name="test_d" time="125.0"/>
<testcase classname="tests.test_e2e" name="test_e" time="3.1"><error message="teardown"/></testcase>
</testsuite></testsuites>
"""


def test_the_timing_table_sums_each_module_in_run_order(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(JUNIT)
    rows = e2e_timings.modules(junit)
    assert [(r.module, r.passed, r.failed, r.skipped) for r in rows] == [
        ("test_machine_e2e", 1, 1, 0),
        ("test_wall_e2e", 0, 0, 1),
        ("test_e2e", 1, 1, 0),
    ]
    assert [round(r.seconds, 1) for r in rows] == [81.5, 0.4, 128.1]


def test_the_timing_table_renders_markdown_with_a_total(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(JUNIT)
    text = e2e_timings.render(e2e_timings.modules(junit), title="lima host e2e · L1")
    lines = text.splitlines()
    assert lines[0] == "### lima host e2e · L1"
    assert "| test_machine_e2e | 1:22 | 1 | 1 | 0 |" in lines
    assert lines[-1] == "| **total** | **3:30** | 2 | 2 | 1 |"


def test_the_timing_table_says_so_when_pytest_left_no_report(tmp_path):
    text = e2e_timings.render(e2e_timings.modules(tmp_path / "absent.xml"), title="W1")
    assert "no test report" in text
