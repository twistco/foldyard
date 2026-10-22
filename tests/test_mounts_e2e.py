"""ADR-0033's host-tier proof: the to-box is read-only against ROOT in the guest.

`fy verify` can't prove this — the box never has root in the guest — so a real VM does: a TEST-ONLY
boot script, recorded under its own marker (the ADR-0032 precedent: it never mentions foldyard's
markers and always exits 0), runs as root at boot, remounts the to-box read-write and tries every
kind of write through it: create, append, overwrite, truncate, chmod, chown, an mtime change, a
symlink, a hard link, mkdir, rename and delete. Each must be refused, and the file your computer
seeded must be unchanged ON THE HOST — bytes, mode and inode. Root in the guest stays boot-time
only: nothing here runs `sudo` in the guest.

The red-first control is the same VM with the to-box recorded `writable: true` (started with
`limactl start` itself, since `fy up` would put the read-only flag back): the writes land, and
`fy verify` — run with the socket preset so it doesn't re-record the mount set first — FAILS its
to-box write row. That is the ADR's second negative beside test_verify_e2e's home mount.

The first test is the to-box's other property, FRESHNESS: what your computer writes there, a
reader in the guest sees promptly — after it has read (and so cached) the file. Lima mounts a
read-only 9p share with `cache=fscache` by default, which caches data and attributes and doesn't
revalidate, on the assumption that a read-only share never changes; the to-box is read-only to the
guest but written by your computer. Harmless for the monitor sink (evidence, read now and then),
wrong for anything a box POLLS — the posture mirror, a later tenant (ADR-0033 § Freshness). The
reader is the VM user over `limactl shell`: the box's `:ro` bind goes through the same guest-kernel
client, so its cache is the box's. On 9p with Lima's default cache the test is a STRICT xfail, so
it fails if 9p turns out fresh after all, and becomes a plain test once `9p.cache` is pinned.

Host tier (tests/e2e_host.py). In CI this is Lima on QEMU with 9p (QEMU's 9p server refuses every
write on a `readonly=on` export); on a macOS host it is vz with virtiofs (the ADR's vz test, done
by hand on Lima 2.1.3). The VM is left running, with the test script removed and foldyard's
read-only to-box recorded again, as every later module expects.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time

import pytest

from e2e_host import (
    LIMA_SOCK,
    VM,
    _adopt_on_host,
    ensure_vm,
    example_copy,
    fy,
    fy_ok,
    host_tier,
    lima_edit,
    lima_list,
    lima_shell,
    state_dir,
)

pytestmark = host_tier

MARKER = "# fy-test-mounts "
RESULT = "/run/fy-test/to-box-ro"
TO_BOX = state_dir() / "to-box"
SEED = TO_BOX / "fy-e2e-seed"
SEEDED = b"published by your computer\n"

# Each op: a name and the shell it runs as root, `D` the to-box. Ordered so the destructive ones
# come last (in the writable control, a rename that lands moves the seed away from the rest).
_OPS = (
    ("create", ': > "$D/fy-e2e-new"'),
    ("append", 'echo x >> "$D/fy-e2e-seed"'),
    ("overwrite", 'echo x > "$D/fy-e2e-seed"'),
    ("truncate", 'truncate -s 0 "$D/fy-e2e-seed"'),
    ("chmod", 'chmod 600 "$D/fy-e2e-seed"'),
    ("chown", 'chown 0:0 "$D/fy-e2e-seed"'),
    ("mtime", 'touch -d @0 "$D/fy-e2e-seed"'),
    ("symlink", 'ln -s /etc/passwd "$D/fy-e2e-link"'),
    ("hardlink", 'ln "$D/fy-e2e-seed" "$D/fy-e2e-hard"'),
    ("mkdir", 'mkdir "$D/fy-e2e-dir"'),
    ("rename", 'mv "$D/fy-e2e-seed" "$D/fy-e2e-moved"'),
    ("delete", 'rm "$D/fy-e2e-seed"'),
)


def _script() -> str:
    lines = [
        "#!/bin/sh",
        f"{MARKER}1",
        "# TEST-ONLY (tests/test_mounts_e2e.py): as root, try every write through the to-box.",
        "# Never mentions another boot script's marker; always exits 0.",
        f"D={shlex.quote(str(TO_BOX))}",
        "mkdir -p /run/fy-test",
        f"R={RESULT}",
        ': > "$R"',
        "i=0",
        'while [ $i -lt 60 ] && ! grep -q " $D " /proc/mounts; do sleep 1; i=$((i+1)); done',
        'grep " $D " /proc/mounts | head -1 | sed "s/^/table /" >> "$R"',
        'if mount -o remount,rw "$D" 2>/dev/null; then echo "remount done" >> "$R";',
        'else echo "remount refused" >> "$R"; fi',
    ]
    for name, cmd in _OPS:
        lines.append(
            f'if ( {cmd} ) 2>/dev/null; then echo "{name} LANDED" >> "$R"; '
            f'else echo "{name} refused" >> "$R"; fi'
        )
    lines += ['echo "finished" >> "$R"', "exit 0", ""]
    return "\n".join(lines)


def _record_script() -> None:
    entry = json.dumps({"mode": "system", "script": _script()}, ensure_ascii=False)
    lima_edit(f".provision = ((.provision // []) + [{entry}])")


def _drop_script() -> None:
    lima_edit(
        f'.provision = [(.provision // [])[] | select((.script // "") | test("{MARKER.strip()}")'
        " | not)]"
    )


def _set_to_box_writable(on: bool) -> None:
    value = "true" if on else "false"
    lima_edit(f"(.mounts[] | select(.location == {json.dumps(str(TO_BOX))}) | .writable) = {value}")


def _results() -> dict[str, str]:
    out = lima_shell("cat", RESULT, timeout=60).stdout
    assert "finished" in out, f"the test boot script did not finish:\n{out}"
    got = {}
    for line in out.splitlines():
        name, _, verdict = line.partition(" ")
        got[name] = verdict
    return got


def _seed() -> os.stat_result:
    for leftover in TO_BOX.glob("fy-e2e-*"):
        if leftover.is_dir() and not leftover.is_symlink():
            leftover.rmdir()
        else:
            leftover.unlink()
    SEED.write_bytes(SEEDED)
    SEED.chmod(0o644)
    return SEED.stat()


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("mounts"))
    _adopt_on_host(r)
    ensure_vm(r)
    row = next(i for i in lima_list(VM) if i.get("name") == VM)
    cfg = row.get("config") or {}
    if (row.get("vmType"), cfg.get("mountType")) not in (("qemu", "9p"), ("vz", "virtiofs")):
        pytest.skip(f"no to-box on {row.get('vmType')}/{cfg.get('mountType')} (ADR-0033)")
    shared = [m for m in cfg.get("mounts") or [] if m.get("location") == str(TO_BOX)]
    assert shared and shared[0].get("writable") is not True, cfg.get("mounts")
    yield r
    fy(["machine", "stop"], r, timeout=300)
    _drop_script()
    _set_to_box_writable(False)
    ensure_vm(r)  # foldyard's own read-only to-box, recorded and verified again
    _seed()
    SEED.unlink()


# Seconds: generous for a box polling its posture; a cache that never revalidates misses it by far
# more. Rounds stop at the first stale one: past it, the rest only wait out the window again.
FRESH_WITHIN = 5.0
ROUNDS = 10

# Runs under the guest's python3 (stdlib only), driven line by line over stdin: `prime` reads the
# file (so the guest has it cached before your computer's write), `want <text>` re-opens and reads
# it until <text> is there or FRESH_WITHIN passes. Replies `primed` / `seen <ms> <missing>` /
# `stale <missing> <bytes>` — `missing` counts reads that found no file (a rename's stale entry).
_READER = r"""
import os, sys, time
path, within = sys.argv[1], float(sys.argv[2])
def read():
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None
for line in sys.stdin:
    cmd, _, arg = line.rstrip("\n").partition(" ")
    if cmd == "prime":
        read()
        print("primed", flush=True)
        continue
    want, t0, missing, data = arg.encode(), time.monotonic(), 0, None
    while time.monotonic() - t0 < within:
        data = read()
        if data is None:
            missing += 1
        elif want in data:
            break
        time.sleep(0.05)
    if data is not None and want in data:
        print(f"seen {int((time.monotonic() - t0) * 1000)} {missing}", flush=True)
    else:
        print(f"stale {missing} {len(data or b'')}", flush=True)
"""


def _to_box_cache() -> tuple[str, str]:
    """The to-box mount's effective share type and 9p cache mode (``""`` when unset)."""
    row = next(i for i in lima_list(VM) if i.get("name") == VM)
    cfg = row.get("config") or {}
    mount = next(m for m in cfg.get("mounts") or [] if m.get("location") == str(TO_BOX))
    return str(cfg.get("mountType") or ""), str((mount.get("9p") or {}).get("cache") or "")


def _rounds(path, write) -> list[str]:
    """Up to ROUNDS of prime → your computer's write → want, against the guest reader."""
    reader = subprocess.Popen(
        ["limactl", "shell", VM, "--", "python3", "-c", _READER, str(path), str(FRESH_WITHIN)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert reader.stdin and reader.stdout
    replies = []
    try:
        for k in range(ROUNDS):
            reader.stdin.write("prime\n")
            assert reader.stdout.readline().strip() == "primed"
            text = f"fy-e2e-fresh-{time.time_ns()}-{k}"
            write(text)
            reader.stdin.write(f"want {text}\n")
            replies.append(reader.stdout.readline().strip())
            if not replies[-1].startswith("seen "):
                break
    finally:
        reader.kill()
        reader.wait()
    return replies


@pytest.mark.parametrize("how", ["append", "replace"])
def test_what_your_computer_writes_to_the_to_box_reaches_the_guest_promptly(repo, request, how):
    mount_type, cache = _to_box_cache()
    if mount_type == "9p" and cache in ("", "fscache", "loose"):
        request.applymarker(
            pytest.mark.xfail(
                strict=True,
                reason=f"9p cache={cache or 'fscache (Lima default)'} caches without revalidating; "
                "pinned with the posture mirror's move to the to-box (ADR-0033 § Freshness)",
            )
        )
    path = TO_BOX / f"fy-e2e-fresh-{how}"
    path.write_text("first version\n")
    try:
        if how == "append":  # the monitor sink's shape: signed lines added to one file

            def write(text: str) -> None:
                with path.open("a") as f:
                    f.write(text + "\n")
        else:  # the posture mirror's shape: mountwrite.write, a temp file renamed over the name

            def write(text: str) -> None:
                tmp = path.with_name(f".{path.name}.tmp")
                tmp.write_text(text + "\n")
                os.replace(tmp, path)

        replies = _rounds(path, write)
    finally:
        path.unlink(missing_ok=True)
    print(f"(to-box freshness, {how}, {mount_type} cache={cache or 'default'}: {replies})")
    stale = [r for r in replies if not r.startswith("seen ")]
    assert stale == [], f"the guest read a stale to-box {len(stale)}/{ROUNDS} times: {replies}"


def test_guest_root_cannot_write_through_the_to_box(repo):
    before = _seed()
    fy_ok(["machine", "stop"], repo, timeout=300)
    _record_script()
    ensure_vm(repo)
    got = _results()
    assert "ro" in got.get("table", "").split()[3].split(","), got  # Lima's fstab: guest-side ro
    landed = [name for name, _ in _OPS if got.get(name) != "refused"]
    assert landed == [], f"guest ROOT wrote through the read-only to-box: {landed}\n{got}"
    after = SEED.stat()
    assert SEED.read_bytes() == SEEDED
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
    )
    assert sorted(p.name for p in TO_BOX.glob("fy-e2e-*")) == ["fy-e2e-seed"]


def test_a_writable_to_box_lets_guest_root_write_and_verify_fails(repo):
    # The control: the same harness against a to-box recorded writable must SEE the writes land —
    # else the refusals above prove nothing about the mount. And `fy verify` must FAIL it.
    _seed()
    fy_ok(["machine", "stop"], repo, timeout=300)
    _set_to_box_writable(True)
    try:
        subprocess.run(["limactl", "start", "--tty=false", VM], check=True, timeout=900)
        got = _results()
        assert got.get("create") == "LANDED", got
        assert (TO_BOX / "fy-e2e-new").exists()
        run = fy(["verify"], repo, timeout=300, env_extra={"DOCKER_HOST": f"unix://{LIMA_SOCK}"})
        assert run.rc != 0, f"verify PASSED against a writable to-box:\n{run.out}"
        assert "the to-box is writable from the VM" in run.out, run.out
        assert "ALL PASS" not in run.out
    finally:
        fy(["machine", "stop"], repo, timeout=300)
        _set_to_box_writable(False)
