"""The two walls, live, via `fy up`: `[machine].wall` (nftables default-deny INSIDE the VM,
provisioned as root at boot) and `[machine].host_wall` (the host-side table matched by the VM's
own cgroup scope — the tier the guest has no reach into). What they must prove: direct guest
egress refused by name, DNS still resolving, the host proxy the only way out, the api still
served through it — and the stale-provisioning refusal: once the VM booted walled, a `fy` verb
run WITHOUT the wall config is refused rather than quietly talking to a differently-provisioned
VM. Ported from the rig's scripted run (docs/lima-wall-machine-integration.md §2–3).

Host tier (tests/e2e_host.py) plus what the walls need: `mitmdump` (the `e2e` dependency group —
the wall refuses a consumer with nothing routing the box, so the example copy declares `[proxy]`),
and for the host wall `nft` + cgroup v2 + passwordless `sudo -n` (a Linux host; CI's runner) —
AND a kernel that has nftables' `socket` expression (`CONFIG_NFT_SOCKET`): the host table matches
the VM by `socket cgroupv2`, and a kernel without the expression refuses the rule with ENOENT.
Ubuntu's kernel has it; the stock WSL2 kernel does not (`# CONFIG_NFT_SOCKET is not set` on both
the 6.6 and 6.18 branches of microsoft/WSL2-Linux-Kernel), so on a WSL2 host this module SKIPS —
it must, because the fixture re-provisions the VM walled before it discovers the host side can't
load, and an error there leaves the next module refusing the stale provisioning (2026-09-17,
the first `wsl2-host-e2e` run). Lima 2.2.0, Fedora 44 guest. The module ends with the VM
re-provisioned WITHOUT the walls.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from e2e_host import (
    VM,
    _adopt_on_host,
    _probe_db,
    ensure_vm,
    example_copy,
    fy,
    fy_ok,
    host_tier,
    lima_shell,
)

WALL = {"MACHINE_WALL": "1", "MACHINE_HOST_WALL": "1"}
NO_PROXY_ENV: list[str] = [
    f"--env={v}=" for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
]
TABLE = f"fy_host_wall_{VM.replace('-', '_')}"  # hostwall.table_name — identifiers allow no '-'


def _host_wall_possible() -> bool:
    if shutil.which("nft") is None or not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        return False
    if shutil.which("sudo") is None:
        return False
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
        return False
    # The kernel half: load ONE `socket cgroupv2` rule into a throwaway table and drop it again.
    # `nft --check` only parses, so this has to reach the kernel — it is the same expression
    # hostwall emits, against a cgroup path every systemd host has.
    probe = "fy_e2e_probe_nft_socket"
    ruleset = (
        f"table inet {probe} {{\n"
        "  chain c { type filter hook output priority 0; policy accept;\n"
        '    socket cgroupv2 level 1 "user.slice" accept\n'
        "  }\n}\n"
    )
    loaded = subprocess.run(
        ["sudo", "-n", "nft", "-f", "-"], input=ruleset, text=True, capture_output=True
    )
    subprocess.run(["sudo", "-n", "nft", "delete", "table", "inet", probe], capture_output=True)
    return loaded.returncode == 0


pytestmark = [
    host_tier,
    pytest.mark.skipif(shutil.which("mitmdump") is None, reason="walls need [proxy] ⇒ mitmdump"),
    pytest.mark.skipif(
        not _host_wall_possible(),
        reason="host wall needs nft + cgroup v2 + sudo -n + a kernel with nft's socket expression",
    ),
]


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("wall"), extra_toml="\n[proxy]\n")
    _adopt_on_host(r)
    ensure_vm(r)
    # The boot provisioning (the sudo narrowing + the wall) is recorded on the STOPPED instance;
    # `fy up` then boots it walled, in its own scope, and loads the host table.
    fy_ok(["machine", "stop"], r, timeout=300)
    fy_ok(["up"], r, env_extra=WALL)
    yield r
    try:
        fy(["down"], r, timeout=180, env_extra=WALL)
        fy(["machine", "stop"], r, timeout=300, env_extra=WALL)
        ensure_vm(r)  # re-provisions WITHOUT the walls for the next module
    finally:
        # The host table must not outlive the module whatever the recovery did — it would fence
        # the next module's VM (a new scope, so nothing would match, but a stale table is still
        # a stale table) and any local run after this one. Reported, never silent: a delete that
        # fails here is a sudo/nft problem the operator needs to hear about.
        gone = subprocess.run(
            ["sudo", "-n", "nft", "delete", "table", "inet", TABLE], capture_output=True, text=True
        )
        if gone.returncode != 0 and "No such file or directory" not in gone.stderr:
            print(f"⚠ could not remove host wall table {TABLE}: {gone.stderr.strip()}")


def test_host_wall_table_is_loaded_for_the_vms_own_slice(repo):
    table = subprocess.run(
        ["sudo", "-n", "nft", "list", "table", "inet", TABLE], capture_output=True, text=True
    )
    assert table.returncode == 0, table.stderr
    assert f"fy-machine-{VM}.slice" in table.stdout, table.stdout
    assert "reject" in table.stdout, table.stdout


def test_direct_guest_egress_is_refused(repo):
    # Lima's environment.d gives the VM user the proxy env, so a bare curl would go through the
    # proxy; `--noproxy '*'` is the direct path the wall must refuse.
    direct = lima_shell(
        "curl", "-sS", "--noproxy", "*", "-m", "8", "-o", "/dev/null", "https://example.com"
    )
    assert direct.returncode != 0, f"direct egress from the guest succeeded:\n{direct.stderr}"


def test_dns_still_resolves_in_the_guest(repo):
    dns = lima_shell("getent", "hosts", "example.com")
    assert dns.returncode == 0 and "example.com" in dns.stdout, dns.stderr


def test_the_proxy_is_the_way_out(repo):
    via = lima_shell(
        "curl", "-sS", "-m", "20", "-o", "/dev/null", "-w", "%{http_code}", "https://example.com"
    )
    assert via.stdout.strip() == "200", f"{via.stdout!r} {via.stderr!r}"


def test_the_api_is_still_served_through_the_walled_stack(repo):
    # Under the wall podman propagates the VM's proxy env into every container, so the probe
    # container's wget would ask the host proxy for `api` — a compose name the proxy cannot
    # resolve (502). Clearing the proxy env is what a stack service calling a sibling must do
    # too: the ONE caveat the wall introduces (example-lima-wall/compose.yml, `no_proxy`).
    assert "reachable" in _probe_db(env_args=NO_PROXY_ENV)


def test_a_verb_without_the_wall_config_is_refused_on_the_walled_vm(repo):
    # No MACHINE_WALL here: the config now wants un-walled provisioning, the running guest reports
    # the walled one — refused with the restart advice, never silently served.
    plain = fy(["ps"], repo, timeout=120)
    assert plain.rc != 0, f"fy ps without the wall config ran against the walled VM:\n{plain.out}"
    assert "provisioning" in plain.out, plain.out
    assert "fy machine stop" in plain.out, plain.out
