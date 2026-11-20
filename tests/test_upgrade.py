"""`fy machine upgrade`: a project VM's Fedora moved forward in place, its podman store kept.

Hermetic: a fake backend and a fake guest record what the run does, in order. The live run is
docs/guest-os.md's (a clone of a real project VM, Fedora 44 to 45 in 153 s, every volume, image
and container kept)."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from foldyard import upgrade
from foldyard.machine_backend import PROVISION_MARKER

# `limactl template copy --embed-all template:podman -`, the lines that matter (Lima 2.3.0).
TEMPLATE_45 = """
images:
- location: "https://download.fedoraproject.org/pub/fedora/linux/releases/45/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-45-1.4.x86_64.qcow2"
  arch: "x86_64"
- location: "https://download.fedoraproject.org/pub/fedora/linux/releases/45/Cloud/aarch64/images/Fedora-Cloud-Base-Generic-45-1.4.aarch64.qcow2"
  arch: "aarch64"
"""


def test_the_template_release_is_the_fedora_its_images_name():
    assert upgrade.template_release(TEMPLATE_45) == 45
    assert upgrade.template_release(TEMPLATE_45.replace("/45/", "/44/")) == 44
    assert (
        upgrade.template_release("images:\n- location: https://cloud.debian.org/x.qcow2\n") is None
    )
    assert upgrade.template_release("") is None


@pytest.mark.parametrize(
    ("guest", "template", "want", "expect"),
    [
        (("fedora", 44), 45, None, 45),  # the default: what a new VM would get
        (("fedora", 43), 45, None, 45),  # two releases: Fedora upgrades that far in one go
        (("fedora", 44), 45, 45, 45),
        (("fedora", 45), 45, None, "already"),
        (("fedora", 46), 45, None, "already"),
        (("fedora", 44), 45, 46, "Lima"),  # beyond what the installed Lima's template ships
        (("fedora", 44), 44, None, "already"),  # an old Lima: nothing newer to go to
        (("fedora", 42), 45, None, "two releases"),
        (("debian", 12), 45, None, "Fedora"),
        (("fedora", 44), None, None, "template"),
        (None, 45, None, "reach"),
    ],
)
def test_the_target_never_passes_the_installed_limas_own_template(guest, template, want, expect):
    got = upgrade.target(guest, template, want)
    if isinstance(expect, int):
        assert got == expect
    else:
        assert isinstance(got, str) and expect in got, got


SNAP = """release fedora 44
free 64424509440
volume devbox_claude_home 1024
volume tangible_postgres_data 52428800
image sha256:aaa
image sha256:bbb
container 1111 running
container 2222 exited
end
"""


def test_a_snapshot_reads_the_guests_listing():
    s = upgrade.parse_snapshot(SNAP)
    assert s.complete
    assert s.release == ("fedora", 44)
    assert s.free == 64424509440
    assert s.volumes == {"devbox_claude_home": 1024, "tangible_postgres_data": 52428800}
    assert s.images == frozenset({"sha256:aaa", "sha256:bbb"})
    assert s.containers == {"1111": "running", "2222": "exited"}
    assert s.running == ["1111"]


def test_a_snapshot_cut_short_or_with_an_unreadable_size_is_incomplete():
    # A failed ssh reads as empty output, and an empty baseline would match anything afterwards.
    assert not upgrade.parse_snapshot("").complete
    assert not upgrade.parse_snapshot(SNAP.replace("end\n", "")).complete
    assert not upgrade.parse_snapshot(SNAP.replace("1024", "")).complete
    assert not upgrade.parse_snapshot(SNAP.replace("release fedora 44\n", "")).complete


def test_the_comparison_names_whatever_went_or_changed_size():
    before = upgrade.parse_snapshot(SNAP)
    after = upgrade.parse_snapshot(
        SNAP.replace("release fedora 44", "release fedora 45").replace(
            "1111 running", "1111 exited"
        )
    )
    assert upgrade.differences(before, after) == []  # a release and a container's state may change
    gone = upgrade.parse_snapshot(
        SNAP.replace("volume devbox_claude_home 1024\n", "")
        .replace("52428800", "4096")
        .replace("image sha256:bbb\n", "")
        .replace("container 2222 exited\n", "")
    )
    diffs = upgrade.differences(before, gone)
    assert any("devbox_claude_home" in d and "missing" in d for d in diffs)
    assert any("tangible_postgres_data" in d and "50.0 MiB" in d and "4.0 KiB" in d for d in diffs)
    assert any("sha256:bbb" in d for d in diffs)
    assert any("2222" in d for d in diffs)


def test_the_boot_script_uses_only_limas_user_field_and_its_own_marker():
    # Lima renders a provision script as a Go template at every start: a stray `{{` breaks the
    # render. And a marker another script contains would let one recording delete the other.
    script = upgrade.render(45)
    assert script.splitlines()[1].startswith(upgrade.MARKER)
    assert set(re.findall(r"\{\{.*?\}\}", script)) == {"{{.User}}"}
    assert PROVISION_MARKER not in script
    assert "--releasever=45" in script


def test_the_boot_script_counts_as_done_only_after_its_own_transaction():
    # A failed transaction can leave the new release's os-release installed: the release number
    # mustn't read as done, only the script's own record of a dnf that succeeded.
    script = upgrade.render(45)
    assert not re.search(r"\[.*VERSION_ID", script)  # no condition reads the release
    assert script.index('report "done 45"') > script.index("dnf -y")


def test_the_boot_script_is_valid_bash():
    if shutil.which("bash") is None:
        pytest.skip("no bash on this host")
    res = subprocess.run(["bash", "-n"], input=upgrade.render(45), text=True, capture_output=True)
    assert res.returncode == 0, res.stderr


class _Backend:
    def __init__(self, calls, stop_ok=True, drop_ok=True):
        self.calls = calls
        self.running = True
        self.stop_ok = stop_ok
        self.drop_ok = drop_ok
        self.stops = 0

    def exists(self, name):
        return True

    def state(self, name):
        return "running" if self.running else "stopped"

    def stop(self, name):
        self.calls.append("stop")
        self.stops += 1
        ok = self.stop_ok or self.stops == 1  # stop_ok=False fails the second stop only
        self.running = not ok
        return ok

    def set_provision(self, name, script, marker):
        self.calls.append(f"record {marker.strip()}")
        return True

    def drop_provision(self, name, marker):
        self.calls.append(f"drop {marker.strip()}")
        return self.drop_ok

    def start(self, name, prefix=(), wait=None):
        self.calls.append(f"start {wait}" if wait else "start")
        self.running = True
        return True


class _Guest:
    """The VM user's shell: answers the snapshot (Fedora 44 before the upgrade boot, 45 after),
    the upgrade's state (one per read, the last repeating), which containers run, and records
    every other command."""

    def __init__(self, calls, states=("done 45",), after=None, stop_rc=0, still_running=(),
                 restart_fails=False, snapshot_rc=0):  # fmt: skip
        self.calls = calls
        self.upgraded = False
        self.stopped = False
        self.states = list(states)
        self.after = after
        self.stop_rc = stop_rc
        self.still_running = still_running
        self.restart_fails = restart_fails
        self.snapshot_rc = snapshot_rc
        self.restarted: list[str] = []

    def __call__(self, script):
        ok = subprocess.CompletedProcess([], 0, "", "")
        if script == upgrade.SNAPSHOT:
            # A database writes its last checkpoint as it stops: the volume a running one sat on
            # is bigger once the containers are down.
            text = SNAP.replace("52428800", "52531200") if self.stopped else SNAP
            if self.stopped:
                text = text.replace("1111 running", "1111 exited")
                for c in self.still_running:
                    text = text.replace(f"{c} exited", f"{c} running")
            if self.upgraded:
                text = self.after or text.replace("fedora 44", "fedora 45")
            return subprocess.CompletedProcess([], self.snapshot_rc, text, "")
        if script.startswith("cat "):
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            self.upgraded = state.startswith("done")
            return subprocess.CompletedProcess([], 0, state + "\n", "")
        if script.startswith("podman ps"):
            return subprocess.CompletedProcess([], 0, "\n".join(self.restarted) + "\n", "")
        self.calls.append(f"guest: {script.split()[0]} {script.split()[1]}")
        if script.startswith("podman stop"):
            self.stopped = True
            return subprocess.CompletedProcess([], self.stop_rc, "", "")
        if script.startswith("podman start"):
            if self.restart_fails:
                return subprocess.CompletedProcess([], 125, "", "no such container")
            self.restarted = script.split()[2:]
            self.restarted = [c for c in self.restarted if not c.startswith(">")]
        return ok


def _run(guest_kw=None, backend_kw=None, host_wall=False, **kw):
    calls: list[str] = []
    rc = upgrade.run(
        backend=_Backend(calls, **(backend_kw or {})),
        guest=_Guest(calls, **(guest_kw or {})),
        template=lambda: 45,
        ensure=lambda: calls.append("ensure"),
        host_wall=lambda: host_wall,
        sleep=lambda s: None,
        assume_yes=True,
        **kw,
    )
    return rc, calls


def test_an_upgrade_stops_records_boots_drops_and_comes_back_through_ensure():
    rc, calls = _run()
    assert rc == 0
    assert calls == [
        "guest: podman stop",  # containers first: a database gets its shutdown, not a power cut
        "stop",
        "record # fy-upgrade",
        f"start {upgrade.UPGRADE_WAIT}",
        "stop",
        "drop # fy-upgrade",
        "ensure",  # foldyard's own boot provisioning, the wall and the checks, as any start
        "guest: podman system",
        "guest: podman start",  # what ran before runs again
    ]


def test_the_baseline_is_the_store_once_its_containers_have_stopped():
    # Taken while the stack still ran, a database's shutdown would read as a changed volume
    # (seen live: Postgres's anonymous volume, 45.8 MiB before, 45.9 MiB after).
    rc, _ = _run()
    assert rc == 0


def test_a_failed_upgrade_still_drops_its_script_and_brings_the_vm_back():
    rc, calls = _run(guest_kw={"states": ("failed 45: dnf exited 1",)})
    assert rc == 1
    assert "drop # fy-upgrade" in calls and "ensure" in calls
    assert "guest: podman system" not in calls


def test_an_upgrade_still_running_when_the_start_gives_up_is_waited_for():
    # `limactl start` gives up on a VM that IS up when its boot scripts outlast the wait: the
    # guest's own record says when dnf is done.
    rc, calls = _run(guest_kw={"states": ("running 45", "running 45", "done 45")})
    assert rc == 0
    assert calls.count("stop") == 2  # the one before the upgrade boot, and only then the next


def test_an_upgrade_that_never_finishes_is_never_stopped_mid_transaction():
    rc, calls = _run(guest_kw={"states": ("running 45",)})
    assert rc == 1
    assert calls[-1] == f"start {upgrade.UPGRADE_WAIT}"  # no stop, no drop: dnf may still run


def test_containers_that_wont_stop_refuse_before_the_vm_is_touched():
    rc, calls = _run(guest_kw={"stop_rc": 125})
    assert rc == 1 and calls == ["guest: podman stop"]
    rc, calls = _run(guest_kw={"still_running": ("2222",)})
    assert rc == 1 and calls == ["guest: podman stop"]


def test_an_incomplete_snapshot_refuses_before_anything_changes():
    rc, calls = _run(guest_kw={"snapshot_rc": 255})
    assert rc == 1 and calls == []


def test_a_vm_that_wont_stop_or_keeps_the_script_is_left_stopped_and_explained():
    # Booted again with the script still recorded, the root transaction would run once more; a
    # VM still up on the old kernel would read as upgraded (os-release is already new).
    rc, calls = _run(backend_kw={"stop_ok": False})
    assert rc == 1 and "ensure" not in calls and "drop # fy-upgrade" not in calls
    rc, calls = _run(backend_kw={"drop_ok": False})
    assert rc == 1 and "ensure" not in calls


def test_containers_that_dont_come_back_fail_the_run():
    rc, _ = _run(guest_kw={"restart_fails": True})
    assert rc == 1


def test_a_store_that_doesnt_match_afterwards_fails_the_run():
    rc, calls = _run(
        guest_kw={
            "after": SNAP.replace("fedora 44", "fedora 45")
            .replace("52428800", "52531200")
            .replace("1111 running", "1111 exited")
            .replace("image sha256:bbb\n", "")
        }
    )
    assert rc == 1
    assert "ensure" in calls


def test_nothing_happens_when_the_target_is_refused():
    rc, calls = _run(release=46)  # beyond Lima's template
    assert rc == 1 and calls == []


def test_the_host_wall_refuses_before_anything_changes():
    # The upgrade boot needs the Fedora mirrors, which the host wall (rightly) doesn't allow, and
    # booting the VM outside its slice would leave it unwalled for the whole transaction.
    rc, calls = _run(host_wall=True)
    assert rc == 1 and calls == []


def test_too_little_free_space_refuses_before_anything_changes():
    calls: list[str] = []
    guest = _Guest(calls)
    tight = SNAP.replace("free 64424509440", "free 1073741824")
    rc = upgrade.run(
        backend=_Backend(calls),
        guest=lambda s: (
            subprocess.CompletedProcess([], 0, tight, "") if s == upgrade.SNAPSHOT else guest(s)
        ),
        template=lambda: 45,
        ensure=lambda: calls.append("ensure"),
        host_wall=lambda: False,
        sleep=lambda s: None,
        assume_yes=True,
    )
    assert rc == 1 and calls == []
