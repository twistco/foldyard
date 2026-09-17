"""`fy reclaim`, live: the sweeps `up` runs only under low headroom, run unconditionally
(`stack.reclaim_now`, `force=True`) — so no fake disk pressure is needed to see them act.

The three properties DEVELOPMENT.md calls load-bearing, on a real store: a removed worktree's
TAGGED images (`{prefix}-{worktree}_{service}`, both provider spellings) are swept — prune can
never see them; the main project's own image and the pulled base images survive — a plain
prune, never `-a`; and the layer cache survives — the next `fy up` builds `Using cache`. On a
fresh runner the `until=24h` guard makes the dangling prune itself a no-op, which is the point of
the guard: nothing younger than a day is ever swept.

Host tier (tests/e2e_host.py). Lima 2.2.0, Fedora 44 guest (podman 5.8.4), host CLI 4.9.3.
"""

from __future__ import annotations

import pytest

from e2e_host import (
    PROJECT,
    _adopt_on_host,
    engine,
    ensure_vm,
    example_copy,
    fy,
    fy_ok,
    host_tier,
    state_dir,
)

pytestmark = host_tier

IMAGE = f"localhost/{PROJECT}_api:latest"  # podman-compose's `{project}_{service}`
# What a removed worktree `gone` leaves behind: podman-compose's `_` spelling and docker
# compose's `-` spelling — one store can hold both (stack._orphan_project_images).
ORPHANS = (f"localhost/{PROJECT}-gone_api:latest", f"localhost/{PROJECT}-gone-api:latest")
KEPT = (IMAGE, "docker.io/library/python:3.12-slim", "docker.io/library/postgres:16-alpine")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("reclaim"))
    _adopt_on_host(r)
    ensure_vm(r)
    fy_ok(["up"], r)
    yield r
    for ref in ORPHANS:
        engine("rmi", ref)  # whatever a failing test left
    fy(["down"], r, timeout=180)


def _images() -> set[str]:
    out = engine("images", "--format", "{{.Repository}}:{{.Tag}}")
    assert out.returncode == 0, out.stderr
    return set(out.stdout.split())


def test_reclaim_drops_a_removed_worktrees_images_and_keeps_everything_else(repo):
    for ref in ORPHANS:
        tagged = engine("tag", IMAGE, ref)
        assert tagged.returncode == 0, tagged.stderr
    before = _images()
    assert set(ORPHANS) <= before and set(KEPT) <= before, before

    out = fy_ok(["reclaim"], repo, timeout=300)
    assert "reclaiming engine-store space" in out.out, out.out
    assert f"{len(ORPHANS)} image(s) belong to removed worktrees" in out.out, out.out
    assert " rmi " in out.out and "prune -a" not in out.out.split("still low")[0], out.out

    after = _images()
    assert not (set(ORPHANS) & after), after
    assert set(KEPT) <= after, after


def test_the_next_up_still_builds_from_the_layer_cache(repo):
    log = state_dir() / f"build-{PROJECT}.log"
    fy_ok(["up"], repo)
    text = log.read_text()
    assert "Using cache" in text, text[-3000:]
