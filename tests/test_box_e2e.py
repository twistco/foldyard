"""The dev box, live on the VM: `fy box up` (build the example's box image, create the box,
bootstrap foldyard into it, warm the shadow volume), then the two in-box facts the don't-break
list guards — in-box `fy ps` reaches the engine via the socket foldyard mounts + the
`CONTAINER_HOST` it exports (plain podman's in-box local mode is broken; only that env makes it
work), and in-box `fy verify` is ALL PASS, including the dev-box posture checks that only run
inside (no SSH key material, `git push` refused) — then `fy box down`.

Host tier (tests/e2e_host.py). The box bind-mounts the checkout at its host path, and a VM's mount
set is init-only, so the module RECREATES the VM from its own example copy first (`fy machine
recreate --yes`) — the one module that needs the VM to mount its copy; the others' stacks ship
their build context over the socket. The example's origin is this PUBLIC repo, on purpose: the
push probe used to be `ls-remote`, which a public origin answers with no credential (so this
fixture had to name a nonexistent private one — docs/archive/verify-false-pass.md, 2026-09-13).
It is a dry-run push now, refused without write access on any repo; a public origin is the case
that proves it. Lima 2.2.0, Fedora 44 guest.
"""

from __future__ import annotations

import subprocess
import time

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
# A sibling worktree registered BEFORE the box is created: main's box masks its declared
# dependency dir (`[box].shadow_volumes`, the example's `api/.venv`) read-only (#91).
OTHER = "other"
# Worktrees added while the box RUNS: `fy worktree add` masks LATE's dir in it without a recreate
# (crossinject); LINKED's dir is a symlink the box could have planted, which nothing follows.
LATE = "late"
LINKED = "linked"
# One `fy worktree add` makes for real, once the box image exists: its files are checked out in a
# container from that image, never on your computer.
FRESH = "fresh"


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("box"))
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/twistco/foldyard.git"],
        cwd=str(r),
        check=True,
    )
    _adopt_on_host(r)
    other = _other(r)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", f"wt/{OTHER}", str(other)], cwd=str(r), check=True
    )
    fy_ok(["worktree", "add", OTHER, "--yes"], r, timeout=300)  # registers it (the host's list)
    _adopt_on_host(other)  # so the teardown's `worktree remove` can resolve its stack
    fy_ok(["machine", "recreate", "--yes"], r, timeout=900)  # so the VM mounts THIS copy
    export_vm_socket()
    fy_ok(["up"], r)
    fy_ok(["box", "up"], r, timeout=1500)
    yield r
    fy(["box", "down"], r, timeout=300)
    for name in (FRESH, OTHER, LATE, LINKED):  # FRESH: if its test failed
        if _other(r, name).exists():
            fy(["worktree", "remove", name, "--yes", "--force"], r, timeout=600)
    fy(["down"], r, timeout=180)


def _other(repo, name=OTHER):
    return repo.parent / f"{repo.name}-worktrees" / name


def test_box_ps_shows_the_running_box(repo):
    ps = fy_ok(["box", "ps"], repo, timeout=120)
    assert BOX in ps.out, ps.out


def test_in_box_fy_ps_reaches_the_engine_over_container_host(repo):
    inside = fy_ok(["box", "exec", "fy ps"], repo, timeout=300)
    assert _service_is_listed(inside.out, PROJECT, "api"), inside.out


def test_in_box_verify_is_all_pass(repo):
    inside = fy_ok(["box", "exec", "fy verify"], repo, timeout=600)
    assert "ALL PASS" in inside.out, inside.out
    assert "dev-box checks — skipped" not in inside.out, inside.out
    # ADR-0033: the box was created with the to-box, so all three of its rows ran from in here
    for row in (
        "to-box mounted read-only in the VM",
        "to-box refuses a write from a --privileged container binding it read-write",
        "/run/foldyard/to-box refuses a write",
    ):
        assert row in inside.out, inside.out


def test_another_checkouts_dependency_dir_is_masked_read_only_with_the_note(repo):
    # From main's box an install in the worktree would have landed on your computer's disk: the
    # dir is read-only there (root included — the box runs as root), and says where to run it.
    venv = _other(repo) / "api" / ".venv"
    note = fy_ok(["box", "exec", f"cat {venv}/RUN-IT-IN-ITS-OWN-BOX"], repo, timeout=120)
    assert "WORKTREE=<name> fy box exec '<command>'" in note.out, note.out
    write = fy(["box", "exec", f"touch {venv}/x"], repo, timeout=120)
    assert write.rc != 0 and "Read-only file system" in write.out, write.out
    # Its own dir stays a writable shadow volume, as before.
    probe = f"{repo}/api/.venv/.fy-e2e"
    fy_ok(["box", "exec", f"touch {probe} && rm {probe}"], repo, timeout=120)


def test_in_box_box_exec_reaches_a_sibling_box_and_says_where_to_start_it(repo):
    # The note's command form, run where it's meant to: from main's box, at the worktree. Its box
    # isn't up, so the in-box CLI names it — resolved over the socket — and says that `fy box up`
    # runs on your computer.
    sibling = fy(
        ["box", "exec", f"cd {_other(repo)} && WORKTREE={OTHER} fy box exec true"],
        repo,
        timeout=300,
    )
    assert sibling.rc != 0, sibling.out
    assert f"{PROJECT}-{OTHER}-devbox not running" in sibling.out, sibling.out
    assert "on your computer" in sibling.out, sibling.out


def test_a_worktree_added_while_the_box_runs_is_masked_in_it_without_a_recreate(repo):
    # Its mounts were fixed at create, before LATE existed: `fy worktree add` adds the mask to the
    # running box (foldyard's helper in the VM, as the VM user) — same read-only mount, same note.
    added = fy_ok(["worktree", "add", LATE], repo, timeout=300)
    assert f"masked '{LATE}' in {BOX} without recreating it" in added.out, added.out
    assert "don't mask its dependency dirs" not in added.out, added.out
    _adopt_on_host(_other(repo, LATE))  # so the teardown's `worktree remove` can resolve its stack
    venv = _other(repo, LATE) / "api" / ".venv"
    note = fy_ok(["box", "exec", f"cat {venv}/RUN-IT-IN-ITS-OWN-BOX"], repo, timeout=120)
    assert "WORKTREE=<name> fy box exec '<command>'" in note.out, note.out
    write = fy(["box", "exec", f"touch {venv}/x"], repo, timeout=120)
    assert write.rc != 0 and "Read-only file system" in write.out, write.out
    # The box's root can't take it off — no CAP_SYS_ADMIN in its namespace, as for a create-time
    # mask — and it's still read-only after every attempt.
    for attempt in (f"umount {venv}", f"umount -l {venv}", f"mount -o remount,rw {venv}"):
        tried = fy(["box", "exec", attempt], repo, timeout=120)
        assert tried.rc != 0, f"{attempt}: {tried.out}"
    again = fy(["box", "exec", f"touch {venv}/x"], repo, timeout=120)
    assert again.rc != 0 and "Read-only file system" in again.out, again.out
    assert not (venv / "x").exists()  # nothing reached your computer's disk
    # Reading it back agrees, though `inspect` never lists it: the already-up `fy box up` has
    # nothing to add or nag about, and the box's own doctor reads its mount table.
    up = fy_ok(["box", "up"], repo, timeout=600)
    assert "doesn't mask" not in up.out and "without recreating it" not in up.out, up.out
    doctor = fy(["box", "exec", "fy doctor"], repo, timeout=600)
    assert "box shadowing" in doctor.out, doctor.out
    assert "lets an install land on your computer's disk" not in doctor.out, doctor.out


def test_a_symlinked_dir_in_an_added_worktree_is_never_masked_through(repo):
    # A dependency dir that's a link (the box writes the worktrees root, so it could plant one) is
    # refused on your computer's walk before anything reaches the VM, and the box is named with the
    # recreate instead.
    wt = _other(repo, LINKED)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", f"wt/{LINKED}", str(wt)], cwd=str(repo), check=True
    )
    elsewhere = repo.parent / "linked-elsewhere"
    elsewhere.mkdir()
    (wt / "api" / ".venv").symlink_to(elsewhere)
    added = fy_ok(["worktree", "add", LINKED, "--yes"], repo, timeout=300)
    _adopt_on_host(wt)
    assert f"couldn't mask '{LINKED}' in {BOX}" in added.out and "symlink" in added.out, added.out
    assert f"don't mask its dependency dirs ([box].shadow_volumes): {BOX}." in added.out
    assert list(elsewhere.iterdir()) == []  # nothing made through the link
    mounted = fy(["box", "exec", f"grep -c ' {wt}/' /proc/self/mountinfo"], repo, timeout=120)
    assert mounted.out.strip().splitlines()[-1] == "0", mounted.out


def _git(repo, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def test_worktree_add_checks_the_files_out_in_the_yard(repo):
    # The host records the worktree (`--no-checkout`); a one-off container from the box image
    # writes the files and the index, over the same mount the box uses (#148's follow-up).
    fresh = _other(repo).parent / FRESH
    added = fy_ok(["worktree", "add", FRESH], repo, timeout=300)
    assert "checking the worktree out in the VM" in added.out, added.out
    assert "worktree ready" in added.out, added.out
    # The branch carries main's committed config and main is adopted: adopted on the spot, no
    # prompt (there is no terminal here) — the next `fy up` needs no adoption of its own.
    assert "adopted — identical to main's" in added.out, added.out
    assert _git(fresh, "rev-parse", "--abbrev-ref", "HEAD").strip() == f"wt/{FRESH}"
    assert (fresh / "compose.yml").read_text() == (repo / "compose.yml").read_text()
    # Written as the box writes the checkout — owned by you on your computer, like the rest.
    assert (fresh / "compose.yml").stat().st_uid == (repo / "compose.yml").stat().st_uid
    index = repo / ".git" / "worktrees" / FRESH / "index"
    before = _git(fresh, "ls-files", "--debug", "compose.yml")
    started = time.monotonic()
    status = _git(fresh, "status", "--porcelain")
    took = time.monotonic() - started
    # The VM-written index against your computer's view of the files: printed for the record
    # (does your kernel's stat data match the VM's?), asserted only for what matters — clean.
    print(f"first host `git status`: {took:.2f}s\n index entry before:\n{before}")
    print(f" after:\n{_git(fresh, 'ls-files', '--debug', 'compose.yml')}")
    assert status == "", status
    assert index.is_file()
    # …and from main's box (which mounts the worktrees root), through foldyard's git shim, which
    # seeds its own index-box from that index on first use (ADR-0021): clean too.
    probe = (
        f'out=$(git -C {fresh} status --porcelain) && [ -z "$out" ] && echo FY-CLEAN || echo "$out"'
    )
    inside = fy_ok(["box", "exec", probe], repo, timeout=300)
    assert "FY-CLEAN" in inside.out, inside.out
    # `remove` asks a container from the same image whether there's uncommitted work, never git on
    # your computer (which would run the shared config's `clean` filters for every file it
    # compares): an edit and an untracked file keep the worktree…
    (fresh / "compose.yml").write_text((fresh / "compose.yml").read_text() + "# edited\n")
    (fresh / "notes.txt").write_text("not committed\n")
    kept = fy(["worktree", "remove", FRESH, "--yes"], repo, timeout=600)
    assert kept.rc == 1, kept.out
    assert f"checking '{FRESH}' for uncommitted work in the VM" in kept.out, kept.out
    assert "uncommitted changes or untracked files" in kept.out, kept.out
    assert (fresh / "notes.txt").is_file()
    # …and --force parks them on a ref the container's commit and your computer's ref write make
    # between them (the object written in the VM, read here), then removes it.
    removed = fy_ok(["worktree", "remove", FRESH, "--force"], repo, timeout=600)
    assert f"worktree '{FRESH}' removed" in removed.out, removed.out
    assert not fresh.exists()
    (ref,) = _git(repo, "for-each-ref", "--format=%(refname)", f"refs/fy/removed/{FRESH}-*").split()
    assert ref in removed.out, removed.out
    assert _git(repo, "show", f"{ref}:notes.txt") == "not committed\n"
    assert _git(repo, "show", f"{ref}:compose.yml").endswith("# edited\n")
    assert (
        _git(repo, "rev-parse", f"{ref}^").strip() == _git(repo, "rev-parse", f"wt/{FRESH}").strip()
    )
