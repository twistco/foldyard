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


def test_a_project_still_naming_the_podman_backend_is_retired_not_missing(host):
    # Its Lima VM doesn't exist and mustn't be offered as one `fy up` would make: that `fy up`
    # refuses, and says how to move over.
    host.tomls[host.checkouts[1]]["machine"]["backend"] = "podman"
    beta = _row(projects.collect(), "beta")
    assert beta.vm.status == "retired" and beta.vm.backend == "podman"
    assert [n.fix for n in beta.notes if n.level == "✗"] == ["fy machine ensure"]
    assert not any("doesn't exist" in n.text for n in beta.notes)


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
    assert seen == [{"here_only": False, "as_json": True, "verbose": False}]


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


# --- leftovers: adoptions, state folders, and `fy prune` --------------------------------------


def _adoption(root: Path, name: str, checkout: Path | str, worktree: str = "") -> Path:
    pin = root / name
    pin.mkdir(parents=True)
    doc = {"checkout": str(checkout), "worktree": worktree}
    (pin / configpin.ADOPTED_MARKER).write_text(json.dumps(doc))
    return pin


@pytest.fixture
def pins(host, monkeypatch):
    """The adoption root, with one adoption per case beside the two live projects'."""
    root = host.tmp / "pins"
    monkeypatch.setattr(configpin, "_pin_root", lambda: root)
    emptied = host.tmp / "work" / "emptied"  # the folder is there, its foldyard.toml isn't
    emptied.mkdir(parents=True)
    alive_wt = host.tmp / "work" / "alpha-worktrees" / "live"
    (alive_wt).mkdir(parents=True)
    (alive_wt / "foldyard.toml").write_text("")
    return SimpleNamespace(
        root=root,
        alpha=_adoption(root, "alpha-1", host.checkouts[0]),
        gone=_adoption(root, "gone-2", host.tmp / "vanished"),
        gone_wt=_adoption(root, "feat-3", host.tmp / "work" / "alpha-worktrees" / "feat", "feat"),
        live_wt=_adoption(root, "live-4", alive_wt, "live"),
        emptied=_adoption(root, "emptied-5", emptied),
    )


def _adoptions(listing) -> list:
    return [lo for lo in listing.leftovers if lo.kind == "adoption"]


def test_an_adoption_is_left_over_when_its_checkout_or_worktree_is_gone(pins):
    got = {lo.path: lo for lo in _adoptions(projects.collect())}
    assert set(got) == {str(pins.gone), str(pins.gone_wt), str(pins.emptied)}
    assert got[str(pins.gone)].prunable and got[str(pins.gone_wt)].prunable
    # A folder that only lost its foldyard.toml is reported, never pruned: a box can delete the
    # file, and forgetting the adoption would turn the next review into a whole-file one.
    assert not got[str(pins.emptied)].prunable
    assert "foldyard.toml" in got[str(pins.emptied)].why


def test_a_state_folder_no_adopted_project_uses_is_left_over(host, pins):
    state = host.tmp / "home" / ".foldyard"
    for name in ("alpha", "beta", "adopted", "cache", "worktrees"):
        (state / name).mkdir(parents=True)
    (state / "homelab").mkdir()
    (state / "homelab" / "host.env").write_text("TOKEN=x\n")
    (state / "backup").mkdir()
    (state / "backup" / "images.tar").write_bytes(b"x" * 4096)
    (state / "ports.json").write_text("{}")
    folders = {lo.what: lo for lo in projects.collect().leftovers if lo.kind == "folder"}
    assert set(folders) == {"folder ~/.foldyard/backup", "folder ~/.foldyard/homelab"}
    assert "host.env" in folders["folder ~/.foldyard/homelab"].why
    assert "host.env" not in folders["folder ~/.foldyard/backup"].why
    assert not any(lo.prunable for lo in folders.values())


def test_a_kept_adoptions_state_folder_is_never_called_unused(host, pins, monkeypatch):
    # A box can delete foldyard.toml, which drops its project from the adopted checkouts. The
    # adoption is kept on purpose, and so is its state: listing `~/.foldyard/<project>/` (and its
    # host.env) as unused would invite an `rm -rf` of a live project's credentials (the Claude
    # review on #185). Its name comes from the adopted snapshot, else the folder's name.
    state = host.tmp / "home" / ".foldyard"
    for name in ("alpha", "beta", "emptied", "nas"):
        (state / name).mkdir(parents=True)
        (state / name / "host.env").write_text("TOKEN=x\n")
    folders = {lo.what for lo in projects.collect().leftovers if lo.kind == "folder"}
    assert "folder ~/.foldyard/emptied" not in folders  # no snapshot: the folder's name
    assert "folder ~/.foldyard/nas" in folders

    real = configpin.adopted_toml
    emptied = host.tmp / "work" / "emptied"
    monkeypatch.setattr(
        configpin,
        "adopted_toml",
        lambda root: {"project": {"name": "nas"}} if Path(root) == emptied else real(root),
    )
    folders = {lo.what for lo in projects.collect().leftovers if lo.kind == "folder"}
    assert "folder ~/.foldyard/nas" not in folders  # the name its adopted config declares


def test_a_big_folder_is_sized_only_so_far(host, pins, monkeypatch):
    # `fy ls` stays quick: a folder of many files (a backup) is walked up to a budget and its
    # size said to be at least what was counted (the Claude review on #185).
    monkeypatch.setattr(projects, "SIZE_WALK_FILES", 3)
    big = host.tmp / "home" / ".foldyard" / "backup"
    big.mkdir(parents=True)
    for i in range(5):
        (big / f"part-{i}").write_bytes(b"x" * 10)
    size, whole = projects._tree_size(big)
    assert not whole and size > 0
    (folder,) = [lo for lo in projects.collect().leftovers if lo.what.endswith("/backup")]
    assert folder.why.startswith("at least ")


def test_no_state_folders_are_judged_under_a_relocated_state_dir(host, pins, monkeypatch):
    (host.tmp / "home" / ".foldyard" / "homelab").mkdir(parents=True)
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(host.tmp / "elsewhere"))
    assert not [lo for lo in projects.collect().leftovers if lo.kind == "folder"]


def test_foldyards_own_folders_in_the_state_root_are_the_ones_ls_skips():
    # Every folder the package names straight under ~/.foldyard must be in projects.OWN_FOLDERS,
    # or `fy ls` would call it left over.
    import re

    src = Path(projects.__file__).parent
    named = set()
    for path in src.rglob("*.py"):
        named |= set(re.findall(r'"\.foldyard"\)?\s*/\s*"([^"%.]+)"', path.read_text()))
    assert named and named <= set(projects.OWN_FOLDERS)


def test_ls_counts_adoptions_and_lists_them_only_when_asked(pins):
    listing = projects.collect()
    out = projects.render(listing)
    assert "2 adoptions whose checkout is gone — `fy prune` forgets them" in out
    assert "1 adoption whose folder no longer holds a foldyard.toml" in out
    assert str(pins.gone) not in out and "vanished" not in out
    verbose = projects.render(listing, verbose=True)
    assert "vanished" in verbose and "alpha-worktrees/feat" in verbose


def test_json_counts_adoptions_and_lists_them_only_when_asked(pins):
    listing = projects.collect()
    doc = json.loads(projects.to_json(listing))
    assert doc["adoptions_left_over"] == 3
    assert all(lo["kind"] != "adoption" for lo in doc["leftovers"])
    full = json.loads(projects.to_json(listing, verbose=True))
    assert full["adoptions_left_over"] == 3
    assert sum(lo["kind"] == "adoption" for lo in full["leftovers"]) == 3


def _prune(answer: str | None, **kw) -> tuple[int, list[str]]:
    asked: list[str] = []

    def ask(question: str) -> bool:
        asked.append(question)
        return answer == "y"

    return projects.prune(ask=ask if answer is not None else None, **kw), asked


def test_prune_forgets_the_gone_adoptions_once_you_say_yes(pins, capsys):
    rc, asked = _prune("y")
    assert rc == 0 and len(asked) == 1 and "2" in asked[0]
    assert not pins.gone.exists() and not pins.gone_wt.exists()
    for kept in (pins.alpha, pins.live_wt, pins.emptied):
        assert kept.is_dir()
    out = capsys.readouterr().out
    assert "vanished" in out  # what it removes is shown before the question


def test_prune_removes_nothing_on_no(pins):
    rc, _ = _prune("n")
    assert rc == 0 and pins.gone.is_dir() and pins.gone_wt.is_dir()


def test_prune_without_a_terminal_removes_nothing_and_says_yes(pins, capsys, monkeypatch):
    monkeypatch.setattr(projects.sys.stdin, "isatty", lambda: False, raising=False)
    rc, _ = _prune(None)
    assert rc == 1 and pins.gone.is_dir()
    assert "fy prune --yes" in capsys.readouterr().err


def test_prune_yes_asks_nothing(pins):
    rc, asked = _prune("n", yes=True)
    assert rc == 0 and asked == [] and not pins.gone.exists() and not pins.gone_wt.exists()


def test_prune_keeps_a_checkout_that_came_back_before_the_answer(pins, host):
    back = host.tmp / "vanished"

    def ask(question: str) -> bool:
        back.mkdir()  # the drive was plugged back in while you read the list
        return True

    assert projects.prune(ask=ask) == 0
    assert pins.gone.is_dir() and not pins.gone_wt.exists()


def test_prune_names_what_it_leaves_and_how_to_remove_it(pins, host, capsys):
    host.instances["old-proj"] = {"name": "old-proj", "status": "Stopped", "dir": ""}
    host.provisioned.add("old-proj")
    _prune("y")
    out = capsys.readouterr().out
    assert "not removed" in out.lower() and "`limactl delete old-proj`" in out


def test_prune_with_nothing_to_forget_says_so(host, monkeypatch, capsys):
    monkeypatch.setattr(configpin, "_pin_root", lambda: host.tmp / "none")
    rc, asked = _prune("y")
    assert rc == 0 and asked == []
    assert "nothing to prune" in capsys.readouterr().out.lower()


def test_prune_refuses_in_a_box(monkeypatch, capsys):
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert projects.prune(yes=True) == 1
    assert "on your computer" in capsys.readouterr().err


def test_fy_prune_runs_outside_a_project(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FOLDYARD_REPO", raising=False)
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "in_project", lambda: False)
    seen: list = []
    monkeypatch.setattr(projects, "prune", lambda **k: seen.append(k) or 0)
    result = CliRunner().invoke(cli.app, ["prune", "--yes"])
    assert result.exit_code == 0, result.output
    assert seen == [{"yes": True}]


def test_fy_ls_passes_verbose(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "in_project", lambda: False)
    seen: list = []
    monkeypatch.setattr(projects, "ls", lambda **k: seen.append(k) or 0)
    assert CliRunner().invoke(cli.app, ["ls", "--verbose"]).exit_code == 0
    assert seen == [{"here_only": False, "as_json": False, "verbose": True}]
