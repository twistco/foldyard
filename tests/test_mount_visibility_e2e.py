"""The shared mount's cross-kernel visibility, on this host's real VM — the git heal's premise.

githeal (ADR-0021, amended 2026-09-27): the BOX builds a healed index and renames it into place next
to the shared one; the HOST reads it and installs it. That is sound only if a rename done in the
VM's kernel reaches the host whole — never torn, never briefly missing, never older than a version
already seen. Measured on virtiofs (macOS, Lima `vz`); this is the same check on whatever this
host's Lima shares the checkout over (9p on the Linux and WSL2 runners), so the design never rests
on one platform's behaviour.

The reverse direction does NOT hold and the design never depends on it: a file the HOST replaces
reads as missing from the VM for up to ~1 s on virtiofs (a stale entry) — which is why every offer
the box makes is a new name, and nothing box-side re-reads a name the host rewrites.

Host tier (tests/e2e_host.py). Works in a scratch dir inside one of the VM's writable mounts.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from e2e_host import VM, ensure_vm, example_copy, host_tier, lima_shell, lima_status

pytestmark = host_tier

VERSIONS = 200
SIZE = 2 << 20  # a large monorepo's index
HDR = 128

# Runs under the guest's python3 (stdlib only): VERSIONS payloads, each a header "<seq> <sha256>"
# + a random body, written to a temp name and renamed over `proposed` — the shim's own pattern.
_WRITER = r"""
import hashlib, os, sys, time
d, n, size = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
target = os.path.join(d, "proposed")
for seq in range(n):
    body = os.urandom(size)
    data = f"{seq} {hashlib.sha256(body).hexdigest()}".encode().ljust(128) + body
    with open(target + ".tmp", "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(target + ".tmp", target)
    time.sleep(0.01)
"""


@pytest.fixture(scope="module")
def scratch(tmp_path_factory):
    """A fresh dir inside a writable mount of the running VM (same path on both sides)."""
    if "Running" not in lima_status():
        ensure_vm(example_copy(tmp_path_factory.mktemp("visibility")))
    listed = subprocess.run(
        ["limactl", "list", VM, "--json"], capture_output=True, text=True, check=True
    ).stdout
    mounts = json.loads(listed.splitlines()[0]).get("config", {}).get("mounts") or []
    shared = [
        Path(m["location"]).expanduser()
        for m in mounts
        if m.get("writable") and m.get("mountPoint") in (None, m["location"])
    ]
    if not shared:
        pytest.skip(f"{VM} has no writable mount at the same path on both sides")
    d = shared[0] / f".fy-visibility-{int(time.time())}"
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_a_vm_side_rename_reaches_the_host_whole_and_in_order(scratch):
    target = scratch / "proposed"
    stats = {"reads": 0, "torn": 0, "short": 0, "missing_after_first": 0, "backwards": 0}
    last, seen = -1, set()
    stop = threading.Event()

    def read() -> None:
        nonlocal last
        while not stop.is_set():
            try:
                data = target.read_bytes()
            except FileNotFoundError:
                if last >= 0:
                    stats["missing_after_first"] += 1
                continue
            stats["reads"] += 1
            if len(data) < HDR + SIZE:
                stats["short"] += 1
                continue
            head = data[:HDR].split()
            if len(head) != 2 or hashlib.sha256(data[HDR:]).hexdigest() != head[1].decode():
                stats["torn"] += 1
                continue
            seq = int(head[0])
            if seq < last:
                stats["backwards"] += 1
            last = max(last, seq)
            seen.add(seq)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        wrote = lima_shell(
            "python3", "-c", _WRITER, str(scratch), str(VERSIONS), str(SIZE), timeout=600
        )
        deadline = time.time() + 10  # the last version must surface on the host, promptly
        while last != VERSIONS - 1 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        reader.join()
    assert wrote.returncode == 0, wrote.stderr
    summary = {**stats, "versions_seen": len(seen), "last_seq": last}
    print(f"(visibility over {VM}'s mount: {summary})")
    assert stats["reads"] > 0, summary
    assert stats["torn"] == stats["short"] == 0, summary  # never a partial file
    assert stats["missing_after_first"] == 0, summary  # a rename never leaves a gap
    assert stats["backwards"] == 0, summary  # never an older version after a newer one
    assert last == VERSIONS - 1, summary  # and the newest one arrives
