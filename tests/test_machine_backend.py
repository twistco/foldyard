"""machine_backend.py — the Lima backend behind machine.py, with `limactl` mocked: the
`--set` override (sizing + isolation mounts), JSON-lines parsing, state normalisation, and the
forwarded-socket path. Liveness (`responsive`) and orphan reaping use REAL AF_UNIX sockets and
files — the failure they exist to catch is a socket file that exists but serves nothing, which a
mock cannot tell apart from a healthy one."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from foldyard import machine_backend as mb


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_guest_socket_path(monkeypatch):
    # Lima forwards a rootless socket whose uid matches the host's (Lima maps guest uid → host uid).
    monkeypatch.setattr(mb.os, "getuid", lambda: 501)
    assert mb.LimaBackend().guest_socket() == "/run/user/501/podman/podman.sock"


@pytest.mark.parametrize("name", ["podman", "native"])
def test_the_retired_block_names_the_backend_and_the_way_over(name):
    block = mb.retired_block(name)
    assert f'backend = "{name}"' in block and "ADR-0036" in block and "fy config adopt" in block
    assert not hasattr(mb, "PodmanBackend") and not hasattr(mb, "NativeBackend")


# ── LimaBackend ───────────────────────────────────────────────────────────────────────


def _json_lines(*objs):
    return "\n".join(json.dumps(o) for o in objs) + "\n"


def test_lima_lists_through_a_template_never_the_json_lines_path(monkeypatch):
    # `limactl list --json` re-reads its own output through a bufio.Scanner (64 KiB a line), and
    # each line carries the instance's whole config: with the wall's and the monitor's boot
    # scripts recorded, `list` fails "token too long", prints nothing — and every lookup read "no
    # such VM" (Lima 2.2.0, reproduced; the host tier's first monitor run, 2026-10-20). The
    # template path writes each instance directly. Splitting on "\n" only: Go's JSON leaves
    # U+0085 unescaped, which str.splitlines() would break a row on.
    seen = []
    big = {"name": "acme", "status": "Running", "dir": "/h/.lima/acme",
           "config": {"provision": [{"script": "x" * 100_000 + "\u0085"}]}}  # fmt: skip
    row = json.dumps(big, ensure_ascii=False) + "\n"  # as Go writes it: U+0085 raw
    monkeypatch.setattr(mb, "_run", lambda cmd: seen.append(cmd) or _Proc(0, row))
    assert mb.LimaBackend().state("acme") == "running"
    assert seen[-1][:4] == ["limactl", "list", "--format", "{{json .}}"]
    assert "--json" not in seen[-1]


def test_lima_state_and_exists_parse_json_lines(monkeypatch):
    rows = _json_lines(
        {"name": "tangible", "status": "Running", "dir": "/h/.lima/tangible"},
        {"name": "homelab", "status": "Stopped", "dir": "/h/.lima/homelab"},
    )
    monkeypatch.setattr(mb, "_run", lambda cmd: _Proc(0, rows))
    be = mb.LimaBackend()
    assert be.exists("tangible") is True
    assert be.exists("absent") is False
    assert be.state("tangible") == "running"
    assert be.state("homelab") == "stopped"
    assert be.state("absent") == ""


def test_lima_socket_is_forwarded_podman_sock(monkeypatch):
    rows = _json_lines({"name": "tangible", "status": "Running", "dir": "/h/.lima/tangible"})
    monkeypatch.setattr(mb, "_run", lambda cmd: _Proc(0, rows))
    assert mb.LimaBackend().socket("tangible") == "unix:///h/.lima/tangible/sock/podman.sock"


def test_lima_memory_is_verbatim_mib_never_rounded_gib():
    # A 2-decimal GiB ("3.91GiB" for 4000 MiB) is not a whole number of MiB — Apple's
    # Virtualization.framework rejects the VM at EVERY boot ("memorySize is not a multiple of
    # 1 megabyte"), bricking it until deleted. MiB must pass through verbatim.
    be = mb.LimaBackend()
    assert '.memory = "4000MiB"' in be._set_expr(
        {"cpus": "4", "memory": "4000", "disk": "60"}, [mb.Mount("/r", "/r")]
    )
    assert mb.LimaBackend._memory_mib("bogus") == 8192  # safe default


def test_lima_set_expr_pins_sizing_and_replaces_mounts():
    be = mb.LimaBackend()
    expr = be._set_expr(
        {"cpus": "4", "memory": "8192", "disk": "60"},
        [
            mb.Mount("/Users/x/repo", "/Users/x/repo"),
            mb.Mount("/Users/x/repo-wt", "/Users/x/repo-wt"),
        ],
    )
    assert ".cpus = 4" in expr
    assert '.memory = "8192MiB"' in expr
    assert '.disk = "60GiB"' in expr
    # both mounts present, writable, and the ONLY mounts (replaces the template's defaults)
    assert expr.count('"writable": true') == 2
    assert '"location": "/Users/x/repo"' in expr
    assert '"location": "/Users/x/repo-wt"' in expr


# ── vmType pinning: the hypervisor is a decision, not a runtime.GOOS accident ──────────


def _lima_info(monkeypatch, vmtypes, rc=0):
    """Stub `limactl info` with the given registered drivers."""
    monkeypatch.setattr(
        mb,
        "_run",
        lambda cmd: _Proc(rc, json.dumps({"vmTypes": vmtypes}) if rc == 0 else ""),
    )


def test_vmtype_prefers_vz_when_the_host_registers_it(monkeypatch, fresh_config):
    _lima_info(monkeypatch, ["qemu", "vz", "krunkit"])
    assert mb.LimaBackend().resolve_vmtype() == "vz"


def test_vmtype_falls_back_to_qemu_when_vz_absent(monkeypatch, fresh_config):
    # A Linux host: Lima registers only qemu, so that is what gets pinned.
    _lima_info(monkeypatch, ["qemu"])
    assert mb.LimaBackend().resolve_vmtype() == "qemu"


def test_vmtype_never_auto_selects_krunkit(monkeypatch, fresh_config):
    # krunkit is experimental and separately installed — it must be named, never inferred.
    _lima_info(monkeypatch, ["krunkit"])
    assert mb.LimaBackend().resolve_vmtype() == ""


def test_vmtype_explicit_config_wins_over_preference(monkeypatch, fresh_config):
    _lima_info(monkeypatch, ["qemu", "vz"])
    monkeypatch.setenv("MACHINE_VMTYPE", "krunkit")
    assert mb.LimaBackend().resolve_vmtype() == "krunkit"


def test_vmtype_unresolvable_pins_nothing(monkeypatch, fresh_config):
    # No limactl / a limactl too old to report vmTypes: degrade to Lima's own default rather
    # than emitting a vmType it can't parse.
    _lima_info(monkeypatch, [], rc=1)
    be = mb.LimaBackend()
    assert be.resolve_vmtype() == ""
    assert ".vmType" not in be._set_expr({"cpus": "4", "memory": "8192", "disk": "60"}, [], "")


def test_set_expr_emits_resolved_vmtype():
    expr = mb.LimaBackend()._set_expr(
        {"cpus": "4", "memory": "8192", "disk": "60"}, [mb.Mount("/r", "/r")], "vz"
    )
    assert '.vmType = "vz"' in expr


@pytest.mark.parametrize("vmtype, mount_type", [("vz", "virtiofs"), ("qemu", "9p")])
def test_set_expr_pins_the_mount_type_that_enforces_read_only_outside_the_guest(vmtype, mount_type):
    # ADR-0033: a Lima default change (or a qemu VM left on virtiofs, whose virtiofsd has no
    # --readonly) would silently downgrade the to-box to guest-only enforcement.
    expr = mb.LimaBackend()._set_expr({"cpus": "4", "memory": "8192", "disk": "60"}, [], vmtype)
    assert f'.mountType = "{mount_type}"' in expr


def test_set_expr_pins_no_mount_type_where_none_enforces():
    expr = mb.LimaBackend()._set_expr({"cpus": "4", "memory": "8192", "disk": "60"}, [], "krunkit")
    assert ".mountType" not in expr


def test_set_expr_carries_each_mounts_writable_flag():
    expr = mb.LimaBackend()._set_expr(
        {"cpus": "4", "memory": "8192", "disk": "60"},
        [mb.Mount("/r", "/r"), mb.Mount("/s/to-box", "/s/to-box", writable=False)],
        "vz",
    )
    assert '"location": "/r", "mountPoint": "/r", "writable": true' in expr
    assert '"location": "/s/to-box", "mountPoint": "/s/to-box", "writable": false' in expr


def test_set_expr_pins_the_to_box_9p_cache_and_nothing_else():
    # Lima mounts a READ-ONLY 9p share with cache=fscache, which never revalidates: what your
    # computer writes into the to-box stayed invisible to the guest (tests/test_mounts_e2e.py).
    expr = mb.LimaBackend()._set_expr(
        {"cpus": "4", "memory": "8192", "disk": "60"},
        [mb.Mount("/r", "/r"), mb.Mount("/s/to-box", "/s/to-box", writable=False, cache="mmap")],
        "qemu",
    )
    assert (
        '"location": "/s/to-box", "mountPoint": "/s/to-box", "writable": false, '
        '"9p": {"cache": "mmap"}' in expr
    )
    assert expr.count('"9p": {') == 1  # a mount with no cache of foldyard's carries none


def _lima_row(monkeypatch, **config):
    rows = _json_lines(
        {"name": "acme", "status": "Stopped", "vmType": "qemu", "config": {**config}}
    )
    monkeypatch.setattr(mb, "_run", lambda cmd: _Proc(0, rows))


def test_lima_mount_set_reads_the_effective_config_with_writable_flags(monkeypatch):
    # `limactl list`'s `config` is lima.yaml merged with Lima's default/override files — where the
    # most permissive `writable` wins — so it is what the VM will really share.
    _lima_row(
        monkeypatch,
        mountType="9p",
        mounts=[
            {"location": "/r", "mountPoint": "/r", "writable": True},
            {
                "location": "/s/to-box",
                "mountPoint": "/s/to-box",
                "writable": False,
                "9p": {"cache": "fscache"},  # Lima's default for a read-only 9p share
            },
            {"location": "/x"},  # Lima's defaults: mountPoint = location, read-only
        ],
    )
    be = mb.LimaBackend()
    got = be.mount_set("acme")
    assert got == [
        mb.Mount("/r", "/r"),
        mb.Mount("/s/to-box", "/s/to-box", writable=False),
        mb.Mount("/x", "/x", writable=False),
    ]
    assert [m.cache for m in got or []] == ["", "fscache", ""]
    assert be.mount_type("acme") == "9p"
    assert be.vmtype("acme") == "qemu"


def test_lima_mount_set_unknown_when_the_instance_cant_be_read(monkeypatch):
    monkeypatch.setattr(mb, "_run", lambda cmd: _Proc(1, ""))
    assert mb.LimaBackend().mount_set("acme") is None


def test_lima_set_mounts_replaces_foldyards_entries_and_keeps_anyone_elses(monkeypatch):
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _Proc(0)

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    ok = mb.LimaBackend().set_mounts(
        "acme",
        [mb.Mount("/r", "/r"), mb.Mount("/s/to-box", "/s/to-box", writable=False)],
        "9p",
        ["/r", "/s/to-box"],
    )
    assert ok
    cmd = seen["cmd"]
    assert cmd[:4] == ["limactl", "edit", "--tty=false", "acme"]
    expr = cmd[cmd.index("--set") + 1]
    assert '(.location // "") != "/r"' in expr and '(.mountPoint // "") != "/s/to-box"' in expr
    assert '"location": "/s/to-box", "mountPoint": "/s/to-box", "writable": false' in expr
    assert expr.endswith('| .mountType = "9p"')


def test_lima_create_pins_the_vmtype(monkeypatch, fresh_config):
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _Proc(0)

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    _lima_info(monkeypatch, ["qemu", "vz"])
    mb.LimaBackend().create(
        "acme", {"cpus": "4", "memory": "8192", "disk": "60"}, [mb.Mount("/r", "/r")]
    )
    assert '.vmType = "vz"' in seen["cmd"][seen["cmd"].index("--set") + 1]


def test_lima_create_uses_podman_template_and_set(monkeypatch):
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _Proc(0)

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    ok = mb.LimaBackend().create(
        "tangible", {"cpus": "4", "memory": "8192", "disk": "60"}, [mb.Mount("/r", "/r")]
    )
    assert ok is True
    cmd = seen["cmd"]
    assert cmd[:4] == ["limactl", "create", "--tty=false", "--name"]
    assert "template://podman" in cmd
    assert cmd[cmd.index("--set") + 1].startswith(".cpus = 4")


def test_lima_mounts_scans_config_yaml(monkeypatch, tmp_path):
    home = tmp_path
    inst = home / ".lima" / "tangible"
    inst.mkdir(parents=True)
    (inst / "lima.yaml").write_text(
        'mounts:\n- location: "/Users/x/repo"\n  mountPoint: "/Users/x/repo"\n  writable: true\n'
    )
    monkeypatch.setattr(mb.Path, "home", staticmethod(lambda: home))
    assert "/Users/x/repo" in mb.LimaBackend().mounts("tangible")


# ── liveness: `state()` is a flag, `responsive()` is a probe ───────────────────────────


@pytest.fixture
def sockdir():
    """A SHORT-pathed temp dir. AF_UNIX paths cap at ~104 bytes on macOS and pytest's
    `tmp_path` (/private/var/folders/…/pytest-of-…/test_name0) can blow that on its own."""
    d = Path(tempfile.mkdtemp(dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _listening(path: Path):
    """A socket someone is actually serving — returned so the caller keeps it open."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(1)
    return s


def _stale(path: Path) -> Path:
    """The file a killed process leaves behind: bound (so the path exists), then abandoned
    without unlinking. `exists()` says yes; a connect is refused."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()
    return path


def test_socket_alive_only_for_a_served_socket(sockdir):
    live = sockdir / "live.sock"
    keep = _listening(live)
    try:
        assert mb.socket_alive(f"unix://{live}") is True
        # The whole point: a STALE socket file exists on disk but serves nothing. An
        # `exists()`-based check would call this alive and we'd be back to the raw dial error.
        assert (stale := _stale(sockdir / "stale.sock")).exists()
        assert mb.socket_alive(f"unix://{stale}") is False
        (regular := sockdir / "machine.log").write_text("not a socket")
        assert mb.socket_alive(f"unix://{regular}") is False
        assert mb.socket_alive(f"unix://{sockdir}/absent.sock") is False
        assert mb.socket_alive("") is False
    finally:
        keep.close()


def test_responsive_probes_the_forwarded_socket(monkeypatch, sockdir):
    sock = sockdir / "podman.sock"
    monkeypatch.setattr(mb.LimaBackend, "socket", lambda self, name: f"unix://{sock}")
    be = mb.LimaBackend()
    keep = _listening(sock)
    try:
        assert be.responsive("acme") is True
    finally:
        keep.close()
    # Socket file still on disk, nothing serving it — the exact state a crashed start leaves.
    assert be.responsive("acme") is False


def test_reap_orphans_is_a_noop_by_default():
    assert mb.LimaBackend().reap_orphans("acme") == []


# ── boot provisioning (lima): recorded in the stored config, applied by Lima at every boot ──


def test_lima_set_provision_edits_the_stopped_instance_with_yq(monkeypatch):
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _Proc(0)

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    script = "#!/bin/bash\n# fy-provision abc123\necho ✓ hi\n"  # the wall asset has such glyphs
    assert mb.LimaBackend().set_provision("acme", script) is True
    cmd = seen["cmd"]
    assert cmd[:4] == ["limactl", "edit", "--tty=false", "acme"]  # edit refuses a RUNNING VM
    expr = cmd[cmd.index("--set") + 1]
    # replaces any previous foldyard entry (never stacks them) and appends this one as a root
    # boot script — Lima's `mode: system` runs on every boot
    assert '"mode": "system"' in expr
    assert "fy-provision" in expr and "select(" in expr
    # the script travels as a JSON string literal, UTF-8 intact — a `\uXXXX` escape lands in
    # lima.yaml VERBATIM (yq keeps it), so the guest would echo the escape, not the glyph
    assert json.dumps(script, ensure_ascii=False) in expr
    assert "\\u2713" not in expr


def test_lima_set_provision_reports_failure(monkeypatch):
    monkeypatch.setattr(mb.subprocess, "run", lambda *a, **k: _Proc(1))
    assert mb.LimaBackend().set_provision("acme", "#!/bin/bash\n# fy-provision x\n") is False


def test_lima_provision_id_reads_the_marker_from_the_stored_config(monkeypatch, tmp_path):
    home = tmp_path
    inst = home / ".lima" / "acme"
    inst.mkdir(parents=True)
    (inst / "lima.yaml").write_text(
        "provision:\n- mode: system\n  script: |\n    #!/bin/bash\n"
        "    # fy-provision abc123\n    echo hi\n"
    )
    monkeypatch.setattr(mb.Path, "home", staticmethod(lambda: home))
    assert mb.LimaBackend().provision_id("acme") == "abc123"
    assert mb.LimaBackend().provision_id("nope") == ""  # no config → nothing recorded


def test_lima_provision_id_reads_each_marker_separately(monkeypatch, tmp_path):
    # Two foldyard boot scripts share lima.yaml — the wall/sudo one and the guest monitor's — and
    # each id is read by its own marker, so recording one never reads as the other.
    inst = tmp_path / ".lima" / "acme"
    inst.mkdir(parents=True)
    (inst / "lima.yaml").write_text(
        "provision:\n- mode: system\n  script: |\n    #!/bin/bash\n    # fy-provision abc123\n"
        "- mode: system\n  script: |\n    #!/bin/bash\n    # fy-monitor def456\n"
    )
    monkeypatch.setattr(mb.Path, "home", staticmethod(lambda: tmp_path))
    assert mb.LimaBackend().provision_id("acme") == "abc123"
    assert mb.LimaBackend().provision_id("acme", "# fy-monitor ") == "def456"


def test_lima_set_provision_replaces_only_its_own_markers_entry(monkeypatch):
    seen = {}
    monkeypatch.setattr(mb.subprocess, "run", lambda cmd, *a, **k: seen.update(cmd=cmd) or _Proc(0))
    script = "#!/bin/bash\n# fy-monitor def456\n"
    assert mb.LimaBackend().set_provision("acme", script, "# fy-monitor ") is True
    expr = seen["cmd"][seen["cmd"].index("--set") + 1]
    # the select drops entries carrying THIS marker only — the wall/sudo script is kept
    assert 'test("# fy-monitor ") | not' in expr
    assert "fy-provision" not in expr


def test_every_provision_marker_reads_the_same_as_a_regex():
    # set_provision's yq `test()` takes the marker as a REGEX; a `.` or `(` in a future marker
    # would match — and so delete — another script's entry
    from foldyard import monitor

    for marker in (mb.PROVISION_MARKER, monitor.MARKER):
        assert not set(marker) & set(".^$*+?()[]{}|\\"), marker


# ── the host-side wall's input (lima): the VM's host pids ──────────────────────────────────


def test_lima_ssh_target_comes_from_the_instances_ssh_config(monkeypatch, tmp_path):
    monkeypatch.setattr(mb.Path, "home", lambda: tmp_path)
    inst = tmp_path / ".lima" / "acme"
    inst.mkdir(parents=True)
    (inst / "ssh.config").write_text(
        'Host lima-acme\n  IdentityFile "/h/.lima/_config/user"\n  IdentityFile "/h/.ssh/id_ed25519"\n'
        "  User dain\n  Hostname 127.0.0.1\n  Port 60022\n"
    )
    t = mb.LimaBackend().ssh_target("acme")
    assert t is not None
    assert (t.user, t.port, t.identity) == ("dain", 60022, "/h/.lima/_config/user")
    assert mb.LimaBackend().ssh_target("absent") is None


def test_lima_vm_pid_reads_the_drivers_pid_file(monkeypatch, tmp_path):
    monkeypatch.setattr(mb.Path, "home", lambda: tmp_path)
    inst = tmp_path / ".lima" / "acme"
    inst.mkdir(parents=True)
    assert mb.LimaBackend().host_pids("acme") == []  # stopped: no pid file at all
    assert mb.LimaBackend().vm_pid("acme") == 0
    (inst / "ha.pid").write_text("4242\n")
    assert mb.LimaBackend().vm_pid("acme") == 4242  # the hostagent shares QEMU's cgroup
    (inst / "qemu.pid").write_text("4343\n")
    assert mb.LimaBackend().host_pids("acme") == [4343, 4242]  # …but the VMM comes first
    assert mb.LimaBackend().vm_pid("acme") == 4343
    (inst / "qemu.pid").write_text("garbage\n")
    assert mb.LimaBackend().host_pids("acme") == [4242]
    (inst / "qemu.pid").unlink()
    (inst / "vz.pid").write_text("4242\n")  # vz: the VMM IS the hostagent — listed once
    assert mb.LimaBackend().host_pids("acme") == [4242]


def test_lima_start_runs_under_the_given_prefix(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc(0, "")

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    monkeypatch.setattr(mb.LimaBackend, "_wait_for_socket", lambda self, name: True)
    assert mb.LimaBackend().start("acme", prefix=["systemd-run", "--scope"]) is True
    assert seen["cmd"] == ["systemd-run", "--scope", "limactl", "start", "acme"]
    assert mb.LimaBackend().start("acme") is True
    assert seen["cmd"] == ["limactl", "start", "acme"]


# ── lima: a hostagent outliving its driver is waited for, then reaped, before `start` ──────
#
# Lima's hostagent notices a dead QEMU, flips the instance to Stopped and exits — but not
# atomically: `limactl start` inside that window refuses with "host agent is running but driver
# is not" (the WSL2 runner, 3.5× slower than Linux, hit it; 2026-09-18). The window is closed by
# the backend's own start, so both `ensure` paths (a plain start on "stopped", the revive on a
# dead socket) are covered.


@pytest.fixture
def lima_pids(tmp_path, monkeypatch):
    """Lima's instance dir with ``ha.pid`` / ``qemu.pid``, liveness scripted per pid, kills
    recorded, no sleeping."""
    inst = tmp_path / ".lima" / "acme"
    inst.mkdir(parents=True)
    monkeypatch.setattr(mb.Path, "home", lambda: tmp_path)
    alive: dict[int, list[bool]] = {}  # pid → the answers `_alive` gives, in order (last repeats)
    kills: list[tuple[int, int]] = []

    def fake_alive(pid):
        answers = alive.get(pid, [False])
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(mb, "_alive", fake_alive)
    monkeypatch.setattr(mb.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)
    monkeypatch.setattr(mb.LimaBackend, "_HOSTAGENT_EXIT_WAIT", 2.0)
    # `limactl list`'s verdict on the instance — a stopped one unless a test says otherwise
    monkeypatch.setattr(
        mb.LimaBackend, "_instance", lambda self, n: {"name": n, "status": "Stopped"}
    )

    def write(ha=None, qemu=None, vz=None):
        if ha:
            (inst / "ha.pid").write_text(f"{ha}\n")
        if qemu:
            (inst / "qemu.pid").write_text(f"{qemu}\n")
        if vz:
            (inst / "vz.pid").write_text(f"{vz}\n")

    return write, alive, kills


def test_lima_reap_waits_for_a_hostagent_that_is_already_leaving(lima_pids):
    write, alive, kills = lima_pids
    write(ha=500, qemu=600)
    alive[600] = [False]  # the driver is dead…
    alive[500] = [True, True, False]  # …and the hostagent goes on its own two polls later
    assert mb.LimaBackend().reap_orphans("acme") == []
    assert kills == []  # never signalled: it left by itself


def test_lima_reap_signals_a_hostagent_that_lingers(lima_pids, monkeypatch):
    write, alive, kills = lima_pids
    write(ha=500, qemu=600)
    alive[600] = [False]
    alive[500] = [True]  # never leaves on its own
    ticks = iter(range(0, 100))
    monkeypatch.setattr(mb.time, "monotonic", lambda: float(next(ticks)))
    assert mb.LimaBackend().reap_orphans("acme") == [500]
    assert kills == [(500, mb.signal.SIGTERM), (500, mb.signal.SIGKILL)]


def test_lima_reap_never_touches_a_healthy_vm(lima_pids):
    write, alive, kills = lima_pids
    write(ha=500, qemu=600)
    alive[600] = [True]  # driver alive: a running VM, whatever the hostagent is doing
    alive[500] = [True]
    assert mb.LimaBackend().reap_orphans("acme") == []
    assert kills == []


def test_lima_reap_never_touches_a_running_vz_vm(lima_pids, capsys):
    # On macOS Lima runs the VM on Virtualization.framework INSIDE the hostagent: `vz.pid` names
    # the hostagent's own pid and there is no `qemu.pid`. Reading "no qemu.pid" as "driver dead"
    # made every live vz VM look like a hostagent that outlived its driver — waited on, then
    # SIGTERMed. On vz the driver is alive exactly when the hostagent is.
    write, alive, kills = lima_pids
    write(ha=500, vz=500)
    alive[500] = [True]
    assert mb.LimaBackend().reap_orphans("acme") == []
    assert kills == [] and "outlived" not in capsys.readouterr().err


@pytest.mark.parametrize("left_running", [True, False])
def test_lima_stop_forces_a_vm_the_graceful_stop_left_running(lima_pids, monkeypatch, left_running):
    # A hung vz VM (the revive's case: flag `running`, socket dead) can ignore the graceful stop,
    # and on vz its hostagent IS the VM — so the reap above rightly never touches it. Lima's own
    # `stop --force` does, by its own records; only when the graceful one left it running.
    write, alive, kills = lima_pids
    write(ha=500, vz=500)
    alive[500] = [left_running]  # after the graceful stop: still running, or gone
    ran: list[list[str]] = []
    monkeypatch.setattr(mb, "_run", lambda cmd, **kw: ran.append(cmd) or _Proc(0, ""))
    assert mb.LimaBackend().stop("acme") is True
    forced = ["limactl", "stop", "--force", "acme"]
    assert ran == [["limactl", "stop", "acme"], *([forced] if left_running else [])]
    assert kills == []  # never a signal of ours: Lima kills its own instance


@pytest.mark.parametrize("left_running", [True, False])
def test_lima_stop_forces_a_driver_whose_hostagent_died(lima_pids, monkeypatch, left_running):
    # QEMU alive with its hostagent gone is Lima's `Broken`, which the graceful stop refuses
    # outright ("expected status Running") — so a hostagent-only liveness check left the driver
    # running. Forced by Lima's own records exactly when something it recorded outlived it.
    write, alive, kills = lima_pids
    write(ha=500, qemu=600)
    alive[600] = [left_running]
    ran: list[list[str]] = []
    rc = {"stop": 1, "--force": 0}
    monkeypatch.setattr(mb, "_run", lambda cmd, **kw: ran.append(cmd) or _Proc(rc[cmd[-2]], ""))
    assert mb.LimaBackend().stop("acme") is left_running
    forced = ["limactl", "stop", "--force", "acme"]
    assert ran == [["limactl", "stop", "acme"], *([forced] if left_running else [])]
    assert kills == []


def test_lima_stop_bounds_the_graceful_stop(lima_pids, monkeypatch):
    # Lima's graceful stop waits up to 3 min 10 s for the hostagent's exit event, then up to 3 min
    # for the instance — a hung VM would hold the revive that long before `--force`. Bounded, and
    # a stop that runs out reads as failed, so the liveness check decides.
    write, alive, _kills = lima_pids
    write(ha=500, vz=500)
    alive[500] = [True]
    calls: list[tuple[list[str], float | None]] = []

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append((cmd, timeout))
        if "--force" not in cmd:
            raise mb.subprocess.TimeoutExpired(cmd, timeout or 0)
        return _Proc(0, "")

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    assert mb.LimaBackend().stop("acme") is True
    assert calls == [
        (["limactl", "stop", "acme"], mb.LimaBackend._GRACEFUL_STOP_WAIT),
        (["limactl", "stop", "--force", "acme"], None),
    ]
    assert mb.LimaBackend._GRACEFUL_STOP_WAIT <= 90


def test_lima_reap_is_a_no_op_without_pid_files(lima_pids):
    _write, _alive, kills = lima_pids
    assert mb.LimaBackend().reap_orphans("acme") == []
    assert kills == []


@pytest.mark.parametrize("status", ["Broken", "Stopped"])
def test_lima_start_forces_a_broken_instance_stopped_first(lima_pids, monkeypatch, status):
    # A hung vz VM: its hostagent (which IS the VM on vz) stops answering, and Lima then reports
    # the instance `Broken` — not Running, so `ensure` takes the START path, not the revive. The
    # reap rightly leaves a live vz hostagent alone, and `limactl start` refuses a broken
    # instance ("errors inspecting instance"). Seen live on vz, 2026-09-28: `limactl stop
    # --force` — Lima ending its own instance — recovered it in 3 s.
    write, alive, kills = lima_pids
    write(ha=500, vz=500)
    alive[500] = [True]
    monkeypatch.setattr(mb.LimaBackend, "_instance", lambda self, n: {"name": n, "status": status})
    ran: list[list[str]] = []
    monkeypatch.setattr(mb, "_run", lambda cmd, **kw: ran.append(cmd) or _Proc(0, ""))
    monkeypatch.setattr(mb.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or _Proc(0, ""))
    monkeypatch.setattr(mb.LimaBackend, "_wait_for_socket", lambda self, name: True)
    assert mb.LimaBackend().start("acme") is True
    forced = [["limactl", "stop", "--force", "acme"]] if status == "Broken" else []
    assert ran == [*forced, ["limactl", "start", "acme"]]
    assert kills == []


def test_lima_start_reaps_before_limactl(lima_pids, monkeypatch, capsys):
    write, alive, _kills = lima_pids
    write(ha=500, qemu=600)
    alive[600] = [False]
    alive[500] = [True, False]
    seen = []
    monkeypatch.setattr(mb.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or _Proc(0, ""))
    monkeypatch.setattr(mb.LimaBackend, "_wait_for_socket", lambda self, name: True)
    assert mb.LimaBackend().start("acme") is True
    assert seen == [["limactl", "start", "acme"]]
    assert "hostagent" in capsys.readouterr().err  # waited: said so


@pytest.mark.parametrize(
    ("recorded", "want"),
    [
        (None, True),  # absent: Lima's default is to propagate
        ("propagateProxyEnv: false\n", False),
        ("propagateProxyEnv: true\n", True),
        ("# propagateProxyEnv: false\n", True),  # the template's commented example
        ("provision:\n- script: |\n    propagateProxyEnv: false\n", True),  # not top level
    ],
)
def test_lima_reads_proxy_propagation_from_the_stored_config(monkeypatch, tmp_path, recorded, want):
    monkeypatch.setattr(mb.Path, "home", lambda: tmp_path)
    inst = tmp_path / ".lima" / "acme"
    inst.mkdir(parents=True)
    (inst / "lima.yaml").write_text("cpus: 4\n" + (recorded or ""))
    assert mb.LimaBackend().proxy_propagation("acme") is want


def test_lima_proxy_propagation_of_a_vm_with_no_config_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(mb.Path, "home", lambda: tmp_path)
    assert mb.LimaBackend().proxy_propagation("absent") is None


@pytest.mark.parametrize(("on", "value"), [(False, "false"), (True, "true")])
def test_lima_set_proxy_propagation_edits_the_stored_config(monkeypatch, on, value):
    seen = []
    monkeypatch.setattr(
        mb.subprocess,
        "run",
        lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    assert mb.LimaBackend().set_proxy_propagation("acme", on)
    assert seen == [
        ["limactl", "edit", "--tty=false", "acme", "--set", f".propagateProxyEnv = {value}"]
    ]


def test_lima_listing_tells_a_failed_list_from_no_vms(monkeypatch):
    # `fy ls` must not read "limactl failed" as "every VM is gone" (#181 review).
    monkeypatch.setattr(mb, "_run", lambda cmd: _Proc(0, ""))
    assert mb.LimaBackend()._listing() == []
    monkeypatch.setattr(mb, "_run", lambda cmd: _Proc(127, ""))
    assert mb.LimaBackend()._listing() is None
    assert mb.LimaBackend()._instances() == []
