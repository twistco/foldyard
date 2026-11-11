"""`fy ls` (foldyard.projects): every project on this computer, read without starting anything."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import foldyard
from foldyard import cli, config, configpin, projects, supervisor
from foldyard.machine_backend import LimaBackend

FP = "fingerprint-now"


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Two adopted projects (``alpha`` with a VM, ``beta`` without one yet), each with its own
    state dir; Lima, the supervisor and the children all stubbed."""
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOLDYARD_STATE_DIR", raising=False)
    monkeypatch.delenv("FOLDYARD_PROJECT", raising=False)
    monkeypatch.delenv("PODMAN_MACHINE", raising=False)
    monkeypatch.delenv("MACHINE_BACKEND", raising=False)
    checkouts, tomls = [], {}
    for name in ("alpha", "beta"):
        root = tmp_path / "work" / name
        root.mkdir(parents=True)
        (root / "foldyard.toml").write_text("")
        checkouts.append(root)
        tomls[root] = {"project": {"name": name}, "machine": {"backend": "lima"}}
    monkeypatch.setattr(configpin, "adopted_checkouts", lambda: list(checkouts))
    monkeypatch.setattr(configpin, "adopted_toml", lambda root: tomls.get(root))
    monkeypatch.setattr(configpin, "inspect", lambda cfg: SimpleNamespace(changed=False))
    vm_dir = tmp_path / "lima" / "alpha"
    vm_dir.mkdir(parents=True)
    (vm_dir / "diffdisk").write_bytes(b"x" * 8192)
    instances = {
        "alpha": {"name": "alpha", "status": "Running", "cpus": 4, "memory": 8 * 2**30,
                  "dir": str(vm_dir)},
    }  # fmt: skip
    monkeypatch.setattr(projects, "_lima_instances", lambda: instances)
    provisioned = {"alpha"}
    monkeypatch.setattr(
        LimaBackend, "provision_id", lambda self, name: "id" if name in provisioned else ""
    )
    sup = {"running": {"alpha"}, "fp": FP, "ready": {"pid": 7, "at": "", "jobs": {}}}

    def running() -> bool:
        return config.project() in sup["running"]

    monkeypatch.setattr(supervisor, "_supervisor_running", running)
    monkeypatch.setattr(supervisor, "_holder_info", lambda: (7, sup["fp"]))
    monkeypatch.setattr(supervisor, "_read_ready", lambda: sup["ready"])
    monkeypatch.setattr(supervisor, "readiness", lambda record, pid, needed: ("ready", []))
    monkeypatch.setattr(supervisor, "_code_fingerprint", lambda: FP)
    asked: list[str] = []
    child: dict[str, Any] = {"doc": {"unavailable": "", "workspaces": [{"name": "main", "box": True,
             "containers": 3}, {"name": "feat", "box": False, "containers": 0}],
             "agents_live": 2, "agents_total": 3}, "rc": 0, "stderr": ""}  # fmt: skip

    def run(cmd, **kw):
        asked.append(str(kw["cwd"]))
        assert cmd[-3:] == ["ls", "--here", "--json"] and "-P" in cmd  # selfspawn, never bare -m
        return subprocess.CompletedProcess(
            cmd, child["rc"], json.dumps(child["doc"]), child["stderr"]
        )

    monkeypatch.setattr(projects.subprocess, "run", run)
    return SimpleNamespace(
        checkouts=checkouts, tomls=tomls, instances=instances, provisioned=provisioned,
        sup=sup, child=child, asked=asked, tmp=tmp_path,
    )  # fmt: skip


def _row(listing, name):
    return next(p for p in listing.projects if p.project == name)


def test_a_running_vm_is_asked_through_its_checkout_a_missing_one_is_not(host):
    got = projects.collect()
    alpha, beta = _row(got, "alpha"), _row(got, "beta")
    assert host.asked == [str(host.checkouts[0])]  # only the running VM's project
    assert alpha.vm.running and alpha.vm.cpus == 4 and alpha.vm.disk_used
    assert alpha.supervisor == "up" and alpha.agents_live == 2
    assert [w.name for w in alpha.workspaces or [] if w.box] == ["main"]
    assert beta.vm.status == "missing" and beta.supervisor == "down" and beta.workspaces is None
    assert any("doesn't exist" in n.text for n in beta.notes)
    out = projects.render(got)
    assert "running (4 CPU, 8 GiB)" in out and "main" in out


def test_a_running_vm_without_its_supervisor_is_flagged(host):
    host.sup["running"] = set()
    alpha = _row(projects.collect(), "alpha")
    assert alpha.supervisor == "down"
    assert [(n.level, n.fix) for n in alpha.notes] == [("✗", "fy host restart")]


def test_a_supervisor_on_older_code_wants_a_restart(host):
    host.sup["fp"] = "older"
    alpha = _row(projects.collect(), "alpha")
    assert any(
        "different foldyard code" in n.text and n.fix == "fy host restart" for n in alpha.notes
    )


def test_a_failing_job_is_named(host, monkeypatch):
    monkeypatch.setattr(
        supervisor, "readiness", lambda *a: ("fail", ["proxy: FAILING — port taken"])
    )
    alpha = _row(projects.collect(), "alpha")
    assert alpha.supervisor == "failing"
    assert any("proxy: FAILING" in n.text for n in alpha.notes)


def test_a_wedged_supervisor_reads_stale(host, monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "readiness",
        lambda *a: ("wait", ["the supervisor's last tick was 90s ago — it isn't ticking"]),
    )
    alpha = _row(projects.collect(), "alpha")
    assert alpha.supervisor == "stale" and alpha.notes[0].fix == "fy host restart"


def test_below_a_projects_floor_it_says_so_and_asks_no_child(host, monkeypatch):
    monkeypatch.setattr(foldyard, "__version__", "0.5.0")
    host.tomls[host.checkouts[0]]["project"]["min_foldyard_version"] = "0.9.0"
    host.tomls[host.checkouts[1]]["project"]["recommended_foldyard_version"] = "0.6.0"
    got = projects.collect()
    alpha, beta = _row(got, "alpha"), _row(got, "beta")
    assert host.asked == []  # its child would only be refused by the floor
    assert alpha.notes[0].level == "✗" and "needs foldyard ≥ 0.9.0" in alpha.notes[0].text
    assert beta.notes[0].level == "⚠" and "recommends foldyard 0.6.0" in beta.notes[0].text
    assert alpha.notes[0].fix == "uv tool upgrade foldyard"


def test_config_drift_and_a_vanished_worktree_are_flagged(host, monkeypatch):
    from foldyard import worktree_registry

    monkeypatch.setattr(configpin, "inspect", lambda cfg: SimpleNamespace(changed=True))
    gone = str(host.tmp / "gone")
    monkeypatch.setattr(worktree_registry, "_load", lambda main: {"old": gone})
    monkeypatch.setattr(worktree_registry, "checkouts", lambda main: {})
    alpha = _row(projects.collect(), "alpha")
    fixes = [n.fix for n in alpha.notes]
    assert "fy config diff" in fixes and "fy worktree remove old" in fixes


def test_only_foldyards_own_unclaimed_vms_are_left_over_and_the_base_is_its_own_line(
    host, monkeypatch
):
    from foldyard import basevm

    host.instances["old-proj"] = {"name": "old-proj", "status": "Stopped", "dir": ""}
    host.instances["mine"] = {"name": "mine", "status": "Running", "dir": ""}  # not foldyard's
    host.instances[basevm.NAME] = {"name": basevm.NAME, "status": "Stopped", "dir": ""}
    host.provisioned.update({"old-proj", basevm.NAME})
    monkeypatch.setattr(basevm, "read_stamp", lambda: None)
    got = projects.collect()
    assert [lo.what for lo in got.leftovers] == ["VM old-proj (stopped)"]
    assert got.leftovers[0].fix == "`limactl delete old-proj`"
    assert got.base_vm is not None and got.base_vm["status"] == "Stopped"
    out = projects.render(got)
    assert "mine" not in out and "Base VM foldyard-base: stopped" in out


def test_an_adoption_whose_checkout_is_gone_is_left_over(host, monkeypatch, tmp_path):
    root = tmp_path / "pins"
    for i, (checkout, worktree) in enumerate(
        [(str(tmp_path / "vanished"), ""), (str(host.checkouts[0]), ""), ("/x/wt", "w")]
    ):
        pin = root / f"p{i}"
        pin.mkdir(parents=True)
        doc = {"checkout": checkout, "worktree": worktree}
        (pin / configpin.ADOPTED_MARKER).write_text(json.dumps(doc))
    monkeypatch.setattr(configpin, "_pin_root", lambda: root)
    got = projects.collect()
    assert [(lo.what, lo.why) for lo in got.leftovers] == [
        (f"adopted config for {tmp_path / 'vanished'}", "the checkout is gone")
    ]


def test_a_childs_answer_is_sanitised_and_its_failure_reported(host):
    host.child["doc"]["unavailable"] = "\x1b]52;c;evil\x07engine slow"
    host.child["doc"]["workspaces"].append({"name": "\x1b[2Jx", "box": True, "containers": "3"})
    alpha = _row(projects.collect(), "alpha")
    assert "\x1b" not in alpha.unavailable
    assert all("\x1b" not in w.name for w in alpha.workspaces or [])
    assert alpha.workspaces is not None and alpha.workspaces[-1].containers is None

    host.child.update(rc=1, stderr="✗ something broke\n")
    alpha = _row(projects.collect(), "alpha")
    assert alpha.unavailable == "✗ something broke" and alpha.workspaces is None
    assert "boxes and agents unknown: ✗ something broke" in projects.render(
        projects.Listing([alpha], [], None)
    )


def test_here_never_boots_a_stopped_vm(monkeypatch):
    from foldyard import devmode, stack

    monkeypatch.setattr(stack, "machine_down", lambda: "lima machine 'x' is stopped")
    monkeypatch.setattr(devmode, "workspaces", lambda: pytest.fail("asked the engine"))
    assert projects.here() == {"unavailable": "lima machine 'x' is stopped"}


def test_here_reports_boxes_stacks_and_agents(monkeypatch):
    from foldyard import agents, devmode, stack

    monkeypatch.setattr(stack, "machine_down", lambda: None)
    monkeypatch.setattr(
        devmode,
        "workspaces",
        lambda: [
            {"name": "main", "project": "p", "registered": True, "containers": 2, "devbox": True},
            {"name": "w", "project": "p-w", "registered": True, "containers": 0, "devbox": False},
            {"name": "u", "project": "p-u", "registered": False, "containers": 0, "devbox": False},
        ],
    )
    monkeypatch.setattr(devmode, "_engine_env", lambda: {})
    alive = agents.Session("a", "", "claude", "", "", 0, True, None)
    dead = agents.Session("b", "", "claude", "", "", 0, False, 0)
    asked: list[str] = []
    monkeypatch.setattr(agents, "sessions_in", lambda b: asked.append(b.name) or [alive, dead])
    got = projects.here()
    assert asked == ["p-devbox"]
    assert got["workspaces"] == [
        {"name": "main", "box": True, "containers": 2},
        {"name": "w", "box": False, "containers": 0},
    ]
    assert (got["agents_live"], got["agents_total"], got["unavailable"]) == (1, 2, "")


def test_ls_refuses_in_a_box(monkeypatch, capsys):
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert projects.ls(here_only=False, as_json=False) == 1
    assert "on your computer" in capsys.readouterr().err


def test_no_projects_says_how_one_appears(host, monkeypatch):
    monkeypatch.setattr(configpin, "adopted_checkouts", lambda: [])
    host.instances.clear()
    assert "No projects yet" in projects.render(projects.collect())


def test_json_round_trips(host):
    doc = json.loads(projects.to_json(projects.collect()))
    assert {p["project"] for p in doc["projects"]} == {"alpha", "beta"}
    assert doc["projects"][0]["vm"]["name"] == "alpha"


def test_fy_ls_runs_outside_a_project(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FOLDYARD_REPO", raising=False)
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "in_project", lambda: False)
    seen: list = []
    monkeypatch.setattr(projects, "ls", lambda **k: seen.append(k) or 0)
    result = CliRunner().invoke(cli.app, ["ls", "--json"])
    assert result.exit_code == 0, result.output
    assert seen == [{"here_only": False, "as_json": True}]


def test_size_reads_like_a_person_would_write_it():
    assert [projects._size(n) for n in (512, 8192, 3 * 2**20, 31 * 2**30)] == [
        "512 B",
        "8 KiB",
        "3.0 MiB",
        "31.0 GiB",
    ]
    assert projects._short(str(Path.home() / "w" / "x")) == "~/w/x"


# --- #181 review: never guess, never write -----------------------------------------------------


def test_a_failed_lima_listing_reads_unknown_never_missing(host, monkeypatch):
    host.provisioned.add("old-proj")
    monkeypatch.setattr(projects, "_lima_instances", lambda: None)
    got = projects.collect()
    alpha = _row(got, "alpha")
    assert alpha.vm.status == "unknown"
    assert not any("doesn't exist" in n.text for n in alpha.notes)
    assert host.asked == []  # a VM that may not be running is never asked
    assert not [lo for lo in got.leftovers if lo.what.startswith("VM ")]
    assert "limactl list" in projects.render(got)


def test_no_delete_advice_while_a_projects_vm_is_unknown(host, monkeypatch):
    # beta's adopted config doesn't read, so its VM can't be named: old-proj may be it.
    host.tomls[host.checkouts[1]] = None
    host.instances["old-proj"] = {"name": "old-proj", "status": "Running", "dir": ""}
    host.provisioned.add("old-proj")
    got = projects.collect()
    vms = [lo for lo in got.leftovers if lo.what.startswith("VM old-proj")]
    assert len(vms) == 1 and vms[0].fix == ""
    assert "beta" in vms[0].why
    assert "limactl delete" not in projects.render(got)


def test_here_reads_the_engine_without_writing_anything(monkeypatch):
    from foldyard import agents, box, devmode, stack

    monkeypatch.setattr(stack, "machine_down", lambda: None)
    monkeypatch.setattr(
        devmode,
        "workspaces",
        lambda: [{"name": "main", "project": "p", "registered": True, "containers": 1,
                  "devbox": True}],
    )  # fmt: skip
    # box.engine_env resolves the stack, which pins core.fileMode into .git/config.
    monkeypatch.setattr(box, "engine_env", lambda: pytest.fail("resolved the stack"))
    monkeypatch.setattr(stack, "pin_filemode", lambda main: pytest.fail("wrote .git/config"))
    monkeypatch.setattr(devmode, "_engine_env", lambda: {"CONTAINER_HOST": "unix:///nonexistent"})
    seen: list = []
    monkeypatch.setattr(agents, "sessions_in", lambda b: seen.append(b.env) or [])
    got = projects.here()
    assert got["unavailable"] == "" and seen == [{"CONTAINER_HOST": "unix:///nonexistent"}]


def test_here_publishes_no_counts_when_the_engine_doesnt_answer(monkeypatch):
    from foldyard import agents, devmode, stack

    monkeypatch.setattr(stack, "machine_down", lambda: None)
    monkeypatch.setattr(
        devmode,
        "workspaces",
        lambda: [{"name": "main", "project": "p", "registered": True, "containers": None,
                  "devbox": False}],
    )  # fmt: skip
    monkeypatch.setattr(agents, "sessions_in", lambda b: pytest.fail("asked a box"))
    got = projects.here()
    assert got == {"unavailable": "the engine didn't answer"}


def test_an_empty_listing_still_says_lima_didnt_answer():
    # With nothing adopted and nothing found, "no projects" alone would hide that the search for
    # left-over VMs never ran (CodeRabbit on #181).
    out = projects.render(projects.Listing([], [], None, lima_unavailable=True))
    assert "`limactl list` didn't answer" in out
