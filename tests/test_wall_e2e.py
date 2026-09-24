"""The two walls, live, via `fy up`: `[machine] firewall` (nftables default-deny INSIDE the VM,
provisioned as root at boot) and `[machine] host_firewall` (the host-side table matched by the VM's
own cgroup slice — the tier the guest has no reach into). What they must prove: direct guest
egress refused by name, DNS still resolving, the host proxy the only way out, the api still
served through it — and the stale-provisioning refusal: once the VM booted walled, a `fy` verb
run WITHOUT the wall config is refused rather than quietly talking to a differently-provisioned
VM. Ported from the rig's scripted run (docs/lima-wall-machine-integration.md §2–3).

The host wall is the OPERATOR'S install (ADR-0028: foldyard never elevates on the host), so
this module plays the operator: the first `fy up` must be REFUSED before it boots anything (the
slice is not set up), then the fixture runs the commands `fy machine host-firewall` printed — the
root ones under `sudo -n`, the user-level ones as itself, verbatim: that is the whole contract —
and `fy up` passes. The table is bound to the persistent user slice the VM runs under; the
module removes the install and the slice at the end, again from the verb's printed lines.

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

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from e2e_host import (
    VM,
    Run,
    _adopt_on_host,
    _probe_db,
    ensure_vm,
    example_copy,
    fy,
    fy_ok,
    host_tier,
    lima_shell,
)

WALL = {"MACHINE_FIREWALL": "1", "MACHINE_HOST_FIREWALL": "1"}
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
    # Nothing set up yet: `fy up` is REFUSED before it boots anything — the slice the wall would
    # match is not there, and a launch verb never sets it up. Kept for the test below.
    refused = fy(["up"], r, env_extra=WALL)
    # The operator's install: the verb sets up the user slice (its one user-level change, said
    # out loud) and prints the root files + lines. It exits 1 here (not enforcing yet) — its
    # output is the contract, not its exit code.
    shown = fy(["machine", "host-firewall"], r, timeout=120, env_extra=WALL)
    for line in _steps(shown.out):
        _run_step(line)
    fy_ok(["up"], r, env_extra=WALL)
    _OPERATOR.update(refused=refused, shown=shown)
    yield r
    try:
        fy(["down"], r, timeout=180, env_extra=WALL)
        fy(["machine", "stop"], r, timeout=300, env_extra=WALL)
        ensure_vm(r)  # re-provisions WITHOUT the walls for the next module
    finally:
        # The install must not outlive the module whatever the recovery did — it would fence
        # the next module's VM and any local run after this one. The uninstall steps are the
        # verb's too (root, then the user slice — the VM is stopped by now, so `--now` on the
        # slice stops nothing); a leftover table is deleted by hand as the last resort.
        # Reported, never silent: a failure here is a sudo/nft problem the operator must hear.
        shown = fy(["machine", "host-wall", "--uninstall"], r, timeout=120, env_extra=WALL)
        for line in _steps(shown.out):
            _run_step(line, must=False)
        gone = subprocess.run(
            ["sudo", "-n", "nft", "delete", "table", "inet", TABLE], capture_output=True, text=True
        )
        if gone.returncode != 0 and "No such file or directory" not in gone.stderr:
            print(f"⚠ could not remove host wall table {TABLE}: {gone.stderr.strip()}")


_OPERATOR: dict[str, Run] = {}  # what the fixture saw on the way in, for the first test


def _steps(out: str) -> list[str]:
    """The command lines the verb printed — the operator's copy-paste block (indented by four,
    `sudo …` or `systemctl --user …`/`rm …`), nothing else."""
    return [
        ln.strip()
        for ln in out.splitlines()
        if ln.startswith("    ") and ln.strip().split(" ")[0] in ("sudo", "systemctl", "rm")
    ]


def _run_step(line: str, must: bool = True) -> None:
    """Run one printed step: a `sudo` line with `sudo -n` (the runner's sudo is passwordless),
    anything else as the user. The fixture IS the operator, and runs what it was shown."""
    words = shlex.split(line)  # the verb quotes paths for a shell
    argv = ["sudo", "-n", *words[1:]] if words[0] == "sudo" else words
    res = subprocess.run(argv, capture_output=True, text=True)
    if must:
        assert res.returncode == 0, f"{line}\n{res.stderr}"


def test_fy_up_is_refused_until_the_operator_installs_the_host_wall(repo):
    refused, shown = _OPERATOR["refused"], _OPERATOR["shown"]
    assert refused.rc != 0, f"fy up ran without a host wall set up:\n{refused.out}"
    assert "not set up" in refused.out and "fy machine host-firewall" in refused.out, refused.out
    assert "starting lima machine" not in refused.out, refused.out  # refused BEFORE booting
    # what the operator was shown: the slice it set up (said once), the table and the unit in
    # full, then exactly the four root steps
    assert shown.rc != 0 and "NOT enforcing" in shown.out, shown.out
    assert "written and enabled (no root)" in shown.out, shown.out
    assert "socket cgroupv2" in shown.out and "ExecStart=" in shown.out, shown.out
    assert [ln.split()[1] for ln in _steps(shown.out)] == [
        "install",
        "install",
        "systemctl",
        "systemctl",
    ]


def test_host_wall_is_enforcing_once_installed(repo):
    # the verb, the probe on `fy up`, and doctor's row all agree — all three PROBE, none reads
    shown = fy(["machine", "host-firewall"], repo, timeout=120, env_extra=WALL)
    assert shown.rc == 0 and "✓ enforcing" in shown.out, shown.out
    doc = fy(["doctor"], repo, timeout=120, env_extra=WALL)
    rows = [ln for ln in doc.out.splitlines() if "host wall" in ln]
    assert rows and "enforcing (" in rows[0] and "NOT" not in rows[0], doc.out


def test_host_wall_table_is_loaded_for_the_vms_own_slice(repo):
    # the operator may read the table (root); foldyard never does
    table = subprocess.run(
        ["sudo", "-n", "nft", "list", "table", "inet", TABLE], capture_output=True, text=True
    )
    assert table.returncode == 0, table.stderr
    assert f"fy-machine-{VM}.slice" in table.stdout, table.stdout
    assert "reject" in table.stdout, table.stdout
    unit = subprocess.run(
        ["systemctl", "is-active", f"fy-host-wall-{VM}.service"], capture_output=True, text=True
    )
    assert unit.stdout.strip() == "active", unit.stdout


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
    # A toolchain host on the default passthrough list is tunnelled: TLS verifies end to end
    # against the REAL certificate, as image pulls from the guest need (ADR-0029).
    tunnelled = lima_shell(
        "curl",
        "-sS",
        "-m",
        "20",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "https://pypi.org/simple/",
    )
    assert tunnelled.stdout.strip() == "200", f"{tunnelled.stdout!r} {tunnelled.stderr!r}"
    # Any other host is decrypted (ADR-0029), and the guest holds no proxy CA — so it is reached
    # through the proxy but presents the proxy's certificate: verification fails, the relay works.
    decrypted = lima_shell(
        "curl", "-sS", "-m", "20", "-o", "/dev/null", "-w", "%{http_code}", "https://example.com"
    )
    assert "certificate" in decrypted.stderr, f"{decrypted.stdout!r} {decrypted.stderr!r}"
    relayed = lima_shell(
        "curl", "-sS", "-k", "-m", "20", "-o", "/dev/null", "-w", "%{http_code}",
        "https://example.com",
    )  # fmt: skip
    assert relayed.stdout.strip() == "200", f"{relayed.stdout!r} {relayed.stderr!r}"


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
