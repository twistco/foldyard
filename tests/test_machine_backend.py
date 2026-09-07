"""machine_backend.py — the backends behind machine.py. The host CLIs (`podman`,
`limactl`) are mocked. PodmanBackend: assert `mounts()` reads the on-disk machine config
(NOT the broken `inspect .Mounts` template, absent on podman 5.x libkrun/applehv) and that
list/state parse correctly. NativeBackend: assert host socket resolution. LimaBackend: assert the
`--set` override (sizing + isolation mounts), JSON-lines parsing, state normalisation, and the
forwarded-socket path. Liveness (`responsive`) and orphan reaping use REAL AF_UNIX sockets and
files — the failure they exist to catch is a socket file that exists but serves nothing, which a
mock cannot tell apart from a healthy one."""

from __future__ import annotations

import json
import shutil
import signal
import socket
import tempfile
from pathlib import Path

import pytest

from foldyard import machine_backend as mb


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


# ── get_backend selection ─────────────────────────────────────────────────────────────


def test_get_backend_selects_by_name():
    assert isinstance(mb.get_backend("podman"), mb.PodmanBackend)
    assert isinstance(mb.get_backend("lima"), mb.LimaBackend)
    assert isinstance(mb.get_backend("native"), mb.NativeBackend)


def test_get_backend_unknown_falls_back_to_podman(capsys):
    be = mb.get_backend("virtualbox")
    assert isinstance(be, mb.PodmanBackend)
    assert "unknown" in capsys.readouterr().err.lower()


def test_concurrency_capability():
    assert mb.PodmanBackend().supports_concurrent() is False  # macOS one-VM-at-a-time
    assert mb.LimaBackend().supports_concurrent() is True
    assert mb.NativeBackend().supports_concurrent() is True


def test_guest_socket_paths(monkeypatch):
    # podman machine exposes the docker-compat socket; Lima forwards a rootless socket whose
    # uid matches the host's (Lima maps guest uid → host uid).
    assert mb.PodmanBackend().guest_socket() == "/run/docker.sock"
    monkeypatch.setattr(mb.os, "getuid", lambda: 501)
    assert mb.LimaBackend().guest_socket() == "/run/user/501/podman/podman.sock"
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/501")
    assert mb.NativeBackend().socket("ignored") == "unix:///run/user/501/podman/podman.sock"
    assert mb.NativeBackend().guest_socket() == "/run/user/501/podman/podman.sock"


def test_native_backend_honours_preexported_socket(monkeypatch):
    monkeypatch.setenv("CONTAINER_HOST", "unix:///tmp/podman.sock")
    assert mb.NativeBackend().socket("ignored") == "unix:///tmp/podman.sock"
    assert mb.NativeBackend().guest_socket() == "/tmp/podman.sock"


# ── PodmanBackend ─────────────────────────────────────────────────────────────────────


def test_podman_mounts_reads_targets_from_on_disk_config(monkeypatch, tmp_path):
    # podman 5.x libkrun `machine inspect` has NO `.Mounts` field (the template errors), but
    # the ConfigDir json always records the Mounts array — mounts() must read THAT.
    (tmp_path / "tangible.json").write_text(
        json.dumps(
            {
                "Mounts": [
                    {"Target": "/Users/x/workspace/Tangible"},
                    {"Target": "/Users/x/workspace/Tangible-worktrees"},
                ]
            }
        )
    )
    monkeypatch.setattr(mb, "_run", lambda cmd: _Proc(0, str(tmp_path)))
    assert mb.PodmanBackend().mounts("tangible") == [
        "/Users/x/workspace/Tangible",
        "/Users/x/workspace/Tangible-worktrees",
    ]


def test_podman_mounts_empty_when_inspect_fails(monkeypatch):
    monkeypatch.setattr(mb, "_run", lambda cmd: _Proc(125, ""))
    assert mb.PodmanBackend().mounts("tangible") == []


def test_podman_mounts_empty_when_config_unreadable(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "_run", lambda cmd: _Proc(0, str(tmp_path)))
    assert mb.PodmanBackend().mounts("tangible") == []  # tangible.json doesn't exist


def test_podman_list_running_parses_only_running(monkeypatch):
    monkeypatch.setattr(
        mb, "_run", lambda cmd: _Proc(0, "tangible|true\nhomelab|false\nfoo|true\n")
    )
    assert mb.PodmanBackend().list_running() == ["tangible", "foo"]


def test_podman_state_normalises(monkeypatch):
    monkeypatch.setattr(mb, "_run", lambda cmd: _Proc(0, "Running\n"))
    assert mb.PodmanBackend().state("x") == "running"
    monkeypatch.setattr(mb, "_run", lambda cmd: _Proc(125, ""))
    assert mb.PodmanBackend().state("x") == ""


# ── LimaBackend ───────────────────────────────────────────────────────────────────────


def _json_lines(*objs):
    return "\n".join(json.dumps(o) for o in objs) + "\n"


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
    assert be.list_running() == ["tangible"]


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
        {"cpus": "4", "memory": "4000", "disk": "60"}, [("/r", "/r")]
    )
    assert mb.LimaBackend._memory_mib("bogus") == 8192  # safe default


def test_lima_set_expr_pins_sizing_and_replaces_mounts():
    be = mb.LimaBackend()
    expr = be._set_expr(
        {"cpus": "4", "memory": "8192", "disk": "60"},
        [("/Users/x/repo", "/Users/x/repo"), ("/Users/x/repo-wt", "/Users/x/repo-wt")],
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
        {"cpus": "4", "memory": "8192", "disk": "60"}, [("/r", "/r")], "vz"
    )
    assert '.vmType = "vz"' in expr


def test_lima_create_pins_the_vmtype(monkeypatch, fresh_config):
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _Proc(0)

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    _lima_info(monkeypatch, ["qemu", "vz"])
    mb.LimaBackend().create("acme", {"cpus": "4", "memory": "8192", "disk": "60"}, [("/r", "/r")])
    assert '.vmType = "vz"' in seen["cmd"][seen["cmd"].index("--set") + 1]


def test_lima_create_uses_podman_template_and_set(monkeypatch):
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _Proc(0)

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    ok = mb.LimaBackend().create(
        "tangible", {"cpus": "4", "memory": "8192", "disk": "60"}, [("/r", "/r")]
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


def _podman_run(config_dir="", sock="", ps=""):
    """A cmd-aware `_run` stub: podman's two inspect templates plus the process listing."""

    def run(cmd):
        if cmd[0] == "ps":
            return _Proc(0, ps)
        if "{{.ConfigDir.Path}}" in cmd:
            return _Proc(0 if config_dir else 125, config_dir)
        if "{{.ConnectionInfo.PodmanSocket.Path}}" in cmd:
            return _Proc(0, sock)
        return _Proc(0, "")

    return run


def test_podman_responsive_probes_the_api_socket(monkeypatch, sockdir):
    api = sockdir / "acme-api.sock"
    monkeypatch.setattr(mb, "_run", _podman_run(sock=str(api)))
    be = mb.PodmanBackend()
    keep = _listening(api)
    try:
        assert be.responsive("acme") is True
    finally:
        keep.close()
    # Socket file still on disk, nothing serving it — the exact state a crashed start leaves.
    assert be.responsive("acme") is False


def test_native_backend_is_always_responsive(monkeypatch):
    # No VM ⇒ no half-started VM to detect, and nothing `ensure` could restart. Must pair with
    # NativeBackend.state()'s unconditional "running".
    monkeypatch.setenv("CONTAINER_HOST", "unix:///nonexistent/podman.sock")
    assert mb.NativeBackend().responsive("ignored") is True


def test_reap_orphans_is_a_noop_by_default():
    assert mb.LimaBackend().reap_orphans("acme") == []


# ── orphan reaping (podman) ───────────────────────────────────────────────────────────

_CONFIG = {
    "Mounts": [{"Target": "/Users/x/workspace/acme"}],
    "ImagePath": {"Path": "/Users/x/.local/share/containers/podman/machine/libkrun/tng-arm64.raw"},
    "LibKrunHypervisor": {
        "KRun": {"VirtualMachine": {"bootloader": {"efiVariableStorePath": "/m/efi-bl-tng"}}}
    },
}


def _with_config(tmp_path, sock="/tmp/podman/tng-api.sock", ps="", config=None):
    (tmp_path / "tng.json").write_text(json.dumps(_CONFIG if config is None else config))
    return _podman_run(config_dir=str(tmp_path), sock=sock, ps=ps)


def test_machine_paths_come_from_podmans_own_records_not_the_name(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "_run", _with_config(tmp_path))
    paths = mb.PodmanBackend()._machine_paths("tng")
    assert paths == {
        "/Users/x/.local/share/containers/podman/machine/libkrun/tng-arm64.raw",
        "/m/efi-bl-tng",  # found however deep the provider nests it
        "/tmp/podman/tng-api.sock",
    }
    # The repo mount must NOT be a match token — it appears in half the argvs on a dev host.
    assert "/Users/x/workspace/acme" not in paths


_PS = """\
  101   501 /opt/podman/bin/krunkit --cpus 8 --device virtio-blk,path=/Users/x/.local/share/\
containers/podman/machine/libkrun/tng-arm64.raw --restful-uri tcp://localhost:50513
  102   501 /opt/podman/bin/gvproxy -listen-vfkit unix:///tmp/podman/tng-api.sock
  103   501 /opt/podman/bin/krunkit --device virtio-blk,path=/Users/x/.local/share/containers/\
podman/machine/libkrun/tng-two-arm64.raw
  104     0 /opt/podman/bin/krunkit --device virtio-blk,path=/Users/x/.local/share/containers/\
podman/machine/libkrun/tng-arm64.raw
  105   501 grep -r /Users/x/.local/share/containers/podman/machine/libkrun/tng-arm64.raw
"""


def test_orphan_pids_requires_our_uid_a_vm_binary_and_this_machines_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "_run", _with_config(tmp_path, ps=_PS))
    monkeypatch.setattr(mb.os, "getuid", lambda: 501)
    # 101 hypervisor (disk image) + 102 forwarder (api socket) — and nothing else:
    #  103 a SIBLING machine whose name merely shares our prefix — killing it would take down
    #      another project's VM, the reason matching is by recorded path, not by name;
    #  104 another user's process; 105 not a VM binary at all (a grep that quotes the path).
    assert mb.PodmanBackend()._orphan_pids("tng") == [101, 102]


def test_orphan_pids_never_guesses_when_the_machine_paths_are_unknown(monkeypatch, tmp_path):
    # Unreadable config AND no socket path → we cannot identify this machine's processes, so we
    # must signal NOTHING rather than fall back to matching on the name.
    monkeypatch.setattr(mb, "_run", _podman_run(config_dir="", sock="", ps=_PS))
    monkeypatch.setattr(mb.os, "getuid", lambda: 501)
    assert mb.PodmanBackend()._orphan_pids("tng") == []


class _Kills:
    """A fake `os.kill` over a set of pids, recording every (pid, signal) it is asked for."""

    def __init__(self, *, stubborn=()):
        self.sent: list[tuple[int, int]] = []
        self.dead: set[int] = set()
        self.stubborn = set(stubborn)

    def __call__(self, pid: int, sig: int) -> None:
        if sig == 0:  # a liveness check, not a signal
            if pid in self.dead:
                raise ProcessLookupError(pid)
            return
        self.sent.append((pid, sig))
        if sig == signal.SIGKILL or pid not in self.stubborn:
            self.dead.add(pid)


def test_reap_orphans_terminates_then_kills_what_ignores_sigterm(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "_run", _with_config(tmp_path, ps=_PS))
    monkeypatch.setattr(mb.os, "getuid", lambda: 501)
    monkeypatch.setattr(mb.time, "sleep", lambda _s: None)
    kills = _Kills(stubborn=[102])
    monkeypatch.setattr(mb.os, "kill", kills)

    assert mb.PodmanBackend().reap_orphans("tng") == [101, 102]
    assert (101, signal.SIGTERM) in kills.sent
    assert (101, signal.SIGKILL) not in kills.sent  # went quietly — never escalated
    assert (102, signal.SIGTERM) in kills.sent
    assert (102, signal.SIGKILL) in kills.sent  # ignored the term; the whole point of the reap


def test_reap_orphans_survives_a_process_that_exits_between_listing_and_signalling(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(mb, "_run", _with_config(tmp_path, ps=_PS))
    monkeypatch.setattr(mb.os, "getuid", lambda: 501)
    monkeypatch.setattr(mb.time, "sleep", lambda _s: None)

    def gone(pid, sig):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(mb.os, "kill", gone)
    assert mb.PodmanBackend().reap_orphans("tng") == [101, 102]  # no traceback


def test_unlink_dead_sockets_spares_live_ones_the_log_and_a_sibling_machine(
    monkeypatch, sockdir, tmp_path
):
    api = sockdir / "tng-api.sock"
    _stale(api)
    _stale(sockdir / "tng.sock")
    _stale(sockdir / "tng-gvproxy.sock-krun.sock")
    (sockdir / "tng.log").write_text("console log")
    sibling = _stale(sockdir / "tng-two.sock")  # another project's machine, same $TMPDIR
    live = sockdir / "tng-gvproxy.sock"
    keep = _listening(live)

    monkeypatch.setattr(mb, "_run", _with_config(tmp_path, sock=str(api)))
    try:
        mb.PodmanBackend()._unlink_dead_sockets("tng")
    finally:
        keep.close()

    assert not api.exists() and not (sockdir / "tng.sock").exists()
    assert not (sockdir / "tng-gvproxy.sock-krun.sock").exists()
    assert live.exists()  # served → untouchable, even mid-reap
    assert (sockdir / "tng.log").exists()  # not a socket
    assert sibling.exists()  # `tng` must not sweep `tng-two`'s files


def test_unlink_dead_sockets_noop_when_the_api_path_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "_run", _with_config(tmp_path, sock=""))
    mb.PodmanBackend()._unlink_dead_sockets("tng")  # must not raise
