"""The dev box, live on the VM: `fy box up` (build the example's box image, create the box,
bootstrap foldyard into it, warm the shadow volume), then the two in-box facts the don't-break
list guards — in-box `fy ps` reaches the engine via the socket foldyard mounts + the
`CONTAINER_HOST` it exports (plain podman's in-box local mode is broken; only that env makes it
work), and in-box `fy verify` is ALL PASS, including the dev-box posture checks that only run
inside (no SSH key material, `git push` refused) — then `fy box down`.

Host tier (tests/e2e_host.py). The box bind-mounts the checkout at its host path, and a VM's mount
set is init-only, so the module RECREATES the VM from its own example copy first (`fy machine
recreate --yes`) — the one module that needs the VM to mount its copy; the others' stacks ship
their build context over the socket. The example's origin is set to a PRIVATE (nonexistent)
remote: a public one answers `ls-remote` without credentials and reads as pushable
(docs/verify-false-pass.md, 2026-09-13). Lima 2.2.0, Fedora 44 guest.
"""

from __future__ import annotations

import subprocess

import pytest

from e2e_host import (
    PROJECT,
    _adopt_on_host,
    _service_is_listed,
    example_copy,
    export_vm_socket,
    fy,
    fy_ok,
    host_tier,
)

pytestmark = host_tier

BOX = f"{PROJECT}-devbox"


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("box"))
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/twistco/fy-e2e-private-fixture.git"],
        cwd=str(r),
        check=True,
    )
    _adopt_on_host(r)
    fy_ok(["machine", "recreate", "--yes"], r, timeout=900)  # so the VM mounts THIS copy
    export_vm_socket()
    fy_ok(["up"], r)
    fy_ok(["box", "up"], r, timeout=1500)
    yield r
    fy(["box", "down"], r, timeout=300)
    fy(["down"], r, timeout=180)


def test_box_ps_shows_the_running_box(repo):
    ps = fy_ok(["box", "ps"], repo, timeout=120)
    assert BOX in ps.out, ps.out


def test_in_box_fy_ps_reaches_the_engine_over_container_host(repo):
    inside = fy_ok(["box", "exec", "fy ps"], repo, timeout=300)
    assert _service_is_listed(inside.out, PROJECT, "api"), inside.out


def test_in_box_verify_is_all_pass(repo):
    inside = fy_ok(["box", "exec", "fy verify"], repo, timeout=600)
    assert "ALL PASS" in inside.out, inside.out
    assert "dev-box posture — skipped" not in inside.out, inside.out
