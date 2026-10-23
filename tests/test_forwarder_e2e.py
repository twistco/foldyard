"""The attributing forwarder (`[machine] attribution = "record"`, ADR-0034) on a real walled VM.

`fy up` with attribution on (and the VM firewall, which it needs) records the forwarder's boot
script under its own marker beside the wall's (the VM is stopped first, so both apply), boots, and
refuses to go on unless the guest reports the forwarder serving. Then, through the REAL supervisor-
run mitmdump and its addon:

- a bridge container's request reaches the proxy stamped with that container's own address, on
  the bridge it sits on — and a stamp the container wrote itself is stripped, never trusted;
- a `--network host` container is stamped with its own container id (the VM listener, by cgroup);
- a build step's request is stamped as a build;
- a container that dials the forwarder's listener directly, and one on an explicit pasta network,
  never reach the proxy (refused, and never logged as anyone's);
- the box's uid can't read the key or stop the forwarder;
- a verb run without the attribution config is refused as stale, and turning it off removes the
  forwarder at the next boot.

What this pins that only a real guest can: the uid-scoped policy routes carry the walled
principals' gateway connections past the UNCHANGED VM firewall (its `oifname "lo"` rule) to the
DNAT, sock_diag + the cgroup walk on the shipped kernel, the rootless namespace following, the OCI
hook in the directory podman actually reads, Lima leaving 127.42.0.1 unforwarded. Validated by hand
on a Fedora 44 / podman 5.8.9 / kernel 6.19.10 `template:podman` guest under vz on arm64
(2026-10-23) before this module ran in CI (QEMU/KVM on x86_64 there).

Host tier (tests/e2e_host.py), plus `mitmdump` (the firewall refuses a consumer with nothing routing
the box, so the copy declares `[proxy]`). The module ends with the VM re-provisioned with neither
the firewall nor the forwarder (the `off` recording stays, which every later module's `fy up`
already matches).
"""

from __future__ import annotations

import json
import shutil
import time

import pytest

from e2e_host import (
    LIMA_DIR,
    _adopt_on_host,
    engine,
    ensure_vm,
    example_copy,
    fy,
    fy_ok,
    host_tier,
    lima_shell,
    state_dir,
)

pytestmark = [
    host_tier,
    pytest.mark.skipif(shutil.which("mitmdump") is None, reason="the firewall needs [proxy]"),
]

ON = {"MACHINE_FIREWALL": "1", "MACHINE_ATTRIBUTION": "record"}
ALPINE = "docker.io/library/alpine"
GW = "192.168.5.2"


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("forwarder"), extra_toml="\n[proxy]\n")
    _adopt_on_host(r)
    ensure_vm(r)
    fy_ok(["machine", "stop"], r, timeout=300)  # both recordings apply at boot
    up = fy_ok(["up"], r, timeout=1800, env_extra=ON)
    assert "✓ attributing forwarder: serving" in up.out, up.out
    yield r
    # Normally the last test already turned it off; this is the recovery when it didn't get that
    # far. Never asserts: a failure here must not hide the test that failed.
    if "serving" in _guest("cat /run/fy-fwd/state 2>/dev/null"):
        fy(["down"], r, timeout=180, env_extra=ON)
        fy(["machine", "stop"], r, timeout=300, env_extra=ON)
        fy(["up"], r, timeout=1800)
        fy(["down"], r, timeout=180)


def _guest(cmd: str) -> str:
    r = lima_shell("bash", "-c", cmd, timeout=120)
    return (r.stdout + r.stderr).strip()


def _log() -> list[dict]:
    path = state_dir() / "main" / "logs" / "egress.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _rows_for(marker: str, timeout: float = 30) -> list[dict]:
    """The egress rows whose path names ``marker`` (each request asks for a path of its own)."""
    deadline = time.time() + timeout
    rows: list[dict] = []
    while time.time() < deadline:
        rows = [r for r in _log() if marker in str(r.get("path", ""))]
        if rows:
            return rows
        time.sleep(1)
    return rows


def _ask(marker: str) -> str:
    """A shell line a container runs: one proxied cleartext request naming ``marker`` (its proxy
    is the one the walled VM's podman gives every container — unchanged by the forwarder)."""
    return f"wget -q -T 15 -O /dev/null -Y on http://example.com/{marker} 2>&1; true"


def test_all_three_boot_scripts_keep_their_own_markers(repo):
    text = (LIMA_DIR / "lima.yaml").read_text()
    assert "# fy-provision " in text and "# fy-forwarder " in text


def test_doctor_shows_the_forwarder_serving(repo):
    doc = fy(["doctor"], repo, timeout=300, env_extra=ON)
    rows = [ln for ln in doc.out.splitlines() if "forwarder" in ln]
    assert rows and "serving" in rows[0] and "NOT" not in rows[0], doc.out


def test_a_bridge_container_is_stamped_with_its_own_address(repo):
    marker = f"fy-e2e-bridge-{int(time.time())}"
    out = engine(
        "run", "--rm", ALPINE, "sh", "-c",
        "ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1; " + _ask(marker),
        timeout=180,
    )  # fmt: skip
    assert out.returncode == 0, out.stdout + out.stderr
    ip = out.stdout.split()[0]
    rows = _rows_for(marker)
    assert rows, f"no egress row for {marker}"
    origin = rows[-1].get("origin")
    assert isinstance(origin, dict), rows[-1]
    assert origin["via"] == "bridge" and origin["src"].split(":")[0] == ip, origin
    assert origin["net"].startswith("podman"), origin


def test_a_stamp_the_container_wrote_is_stripped_and_never_trusted(repo):
    marker = f"fy-e2e-forged-{int(time.time())}"
    forged = "v=1; via=vm; src=10.0.0.1:1; who=build; boot=x; seq=1; mac=" + "0" * 64
    out = engine(
        "run", "--rm", ALPINE, "sh", "-c",
        f"wget -q -T 15 -O /dev/null -Y on --header 'X-Fy-Origin: {forged}' "
        f"http://example.com/{marker} 2>&1; true",
        timeout=180,
    )  # fmt: skip
    assert out.returncode == 0, out.stdout + out.stderr
    rows = _rows_for(marker)
    assert rows and isinstance(rows[-1].get("origin"), dict), rows
    assert rows[-1]["origin"]["via"] == "bridge", rows[-1]  # the forwarder's, not the client's


def test_a_host_network_container_is_stamped_with_its_container_id(repo):
    marker = f"fy-e2e-hostnet-{int(time.time())}"
    run = engine(
        "run", "-d", "--network", "host", ALPINE, "sh", "-c",
        f"sleep 2; {_ask(marker)}; sleep 60",
        timeout=180,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    cid = run.stdout.strip()
    try:
        rows = _rows_for(marker, timeout=60)
        assert rows, f"no egress row for {marker}"
        origin = rows[-1].get("origin")
        assert isinstance(origin, dict) and origin["via"] == "vm", rows[-1]
        assert origin["who"] == "container" and origin["ctr"] == cid, origin
    finally:
        engine("rm", "-f", cid, timeout=60)


def test_a_build_step_is_stamped_as_a_build(repo, tmp_path):
    marker = f"fy-e2e-build-{int(time.time())}"
    (tmp_path / "Containerfile").write_text(f"FROM {ALPINE}:3.20\nRUN {_ask(marker)}\n")
    out = engine("build", "--no-cache", "-t", "fy-e2e-fwd-build", str(tmp_path), timeout=600)
    try:
        assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
        rows = _rows_for(marker)
        assert rows, f"no egress row for {marker}"
        origin = rows[-1].get("origin")
        assert isinstance(origin, dict) and origin["who"] == "build", rows[-1]
    finally:
        engine("rmi", "-f", "fy-e2e-fwd-build", timeout=60)


def test_the_listener_dialled_directly_and_an_explicit_pasta_network_never_reach_the_proxy(repo):
    # Direct to the bridge listener: no daemon port behind it, so refused. Explicit pasta: its
    # traffic is re-made by a helper outside any container's cgroup — whose, nothing can say — so
    # refused rather than recorded as the VM's (spike finding 5).
    marker = f"fy-e2e-pasta-{int(time.time())}"
    direct = engine(
        "run", "--rm", ALPINE, "sh", "-c",
        "gw=$(ip route | awk '/default/ {print $3}'); "
        "printf 'GET http://example.com/ HTTP/1.1\\r\\nHost: example.com\\r\\n\\r\\n' "
        "| nc -w 5 $gw 4748 | head -c 64; echo END",
        timeout=180,
    )  # fmt: skip
    assert "HTTP/" not in direct.stdout, direct.stdout
    pasta = engine("run", "--rm", "--network", "pasta", ALPINE, "sh", "-c", _ask(marker))
    assert pasta.returncode == 0, pasta.stderr
    time.sleep(3)
    assert not [r for r in _log() if marker in str(r.get("path", ""))]
    assert "pasta helper" in _guest("journalctl -u fy-fwd --no-pager 2>/dev/null | tail -50") or (
        "refused" in _guest("cat /run/fy-fwd/stats")
    )


def test_the_box_uid_cannot_read_the_key_or_stop_the_forwarder(repo):
    assert "Permission denied" in _guest("cat /etc/fy-fwd/forwarder.key 2>&1")
    assert "Access denied" in _guest("systemctl stop fy-fwd.service 2>&1")
    assert _guest("systemctl is-active fy-fwd.service") == "active"
    assert "Operation not permitted" in _guest("ip rule del priority 4747 2>&1")


def test_a_verb_without_the_attribution_config_is_refused_as_stale(repo):
    plain = fy(["ps"], repo, timeout=120, env_extra={"MACHINE_FIREWALL": "1"})
    assert plain.rc != 0, plain.out
    assert "STALE forwarder provisioning" in plain.out and "fy machine stop" in plain.out


def test_turning_it_off_removes_the_forwarder_at_the_next_boot(repo):
    fy(["down"], repo, timeout=180, env_extra=ON)
    fy_ok(["machine", "stop"], repo, timeout=300, env_extra=ON)
    fy_ok(["up"], repo, timeout=1800)  # neither the firewall nor attribution
    try:
        assert _guest("cat /run/fy-fwd/state") == "off"
        gone = _guest(
            "ls /usr/local/libexec/fy-fwd /usr/share/containers/oci/hooks.d/fy-fwd-wait.json "
            "/etc/containers/containers.conf.d/93-fy-fwd-bridge.conf 2>&1"
        )
        assert gone.count("No such file") == 3, gone
        assert "4747" not in _guest("ip rule")
        assert _guest("systemctl is-active fy-fwd.service") != "active"
        # and a plain container reaches the host gateway's daemon ports directly again — no
        # firewall here, so nothing but the forwarder's own (now absent) table would refuse it
        out = engine("run", "--rm", ALPINE, "sh", "-c", "ip -4 -o addr show eth0", timeout=180)
        assert "10.88." not in out.stdout  # bridge is no longer the default network
    finally:
        fy(["down"], repo, timeout=180)
