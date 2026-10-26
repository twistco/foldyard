"""attribution.py — who a proxy row's stamp names (ADR-0034, step 1: attribute and record).

The forwarder (:mod:`foldyard.forwarder`) stamps each proxy connection with where it came from,
and the proxy keeps the verified stamp on the row as ``origin``: a bridge container's ADDRESS and
the bridge it sits on, or — from the VM's own namespace — a container's id, ``build`` or ``vm``.
Addresses are reused, so a stamp is evidence, and what it names is decided here, on the host, at
read time — never stored — so a resolution fix applies to stored history too.

Two parts:

* **Snapshots** (:func:`run`, a supervisor thread). While attribution is on and the VM runs, the
  engine is asked every :data:`POLL_EVERY` seconds which containers run, and — when that set
  changes, and at least every :data:`SNAPSHOT_EVERY` — for each one's addresses on each network
  and the bridge interface that network uses. A snapshot is appended to
  ``<state dir>/logs/origin-snapshots.jsonl`` when it differs from the last one written or that
  one is older than :data:`SNAPSHOT_EVERY`. Names and compose labels in it are what the engine
  reports — claims, whatever the creator set, exactly as for the guest monitor's — and so is an
  address: the engine is the only one who knows which container holds it.
* **Resolution** (:class:`Resolver`, pure): a row takes the snapshot before it if that still holds
  its address, else the one after, and the two disagreeing is ``ambiguous``; nothing is carried past
  a snapshot that dropped it (ADR-0032's rule for namespaces). A row no snapshot explains keeps its
  raw stamp. A container id (the VM listener's stamp) is a kernel fact — podman named the scope —
  and only its NAME is looked up.

Stdlib only; the Network Log panel imports this on the registry hot path.
"""

from __future__ import annotations

import bisect
import json
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config

POLL_EVERY = 5.0
SNAPSHOT_EVERY = 60.0
MAX_BYTES = 8 << 20  # rotate the snapshot file past this; one backup is kept
_CLAIM_KEYS = ("name", "project", "service")


def snapshots_file() -> Path:
    return config.state_dir() / "logs" / "origin-snapshots.jsonl"


def wanted() -> bool:
    try:
        return config.machine_attribution() == "record"
    except SystemExit:
        return False


# ── snapshots: the engine's word, every few seconds ───────────────────────────────────


def _engine(*args: str) -> subprocess.CompletedProcess:
    from . import devmode  # import-light; only the supervisor's thread needs the engine env

    return subprocess.run(
        [config.engine(), *args], capture_output=True, text=True, timeout=30,
        env=devmode._engine_env(),
    )  # fmt: skip


def running_ids() -> list[str] | None:
    res = _engine("ps", "-q", "--no-trunc")
    if res.returncode != 0:
        return None
    return sorted(line.strip() for line in res.stdout.splitlines() if line.strip())


def interfaces() -> dict[str, str] | None:
    """network name → its bridge interface (what the forwarder's ``net`` names)."""
    res = _engine("network", "ls", "--format", "json")
    if res.returncode != 0:
        return None
    try:
        rows = json.loads(res.stdout or "[]")
    except ValueError:
        return None
    out = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("name") and row.get("network_interface"):
            out[str(row["name"])] = str(row["network_interface"])
    return out


def describe(ids: list[str], ifaces: dict[str, str]) -> dict | None:
    """``{"endpoints": [[interface, address, id], …], "containers": {id: claims}}``."""
    if not ids:
        return {"endpoints": [], "containers": {}}
    res = _engine("inspect", *ids)
    if res.returncode != 0:
        return None  # one stopped between ps and inspect, or a real failure: the caller asks ps
    try:
        found = json.loads(res.stdout or "[]")
    except ValueError:
        return None
    endpoints, containers = [], {}
    for c in found if isinstance(found, list) else []:
        cid = str(c.get("Id") or "")
        if not cid:
            continue
        labels = (c.get("Config") or {}).get("Labels") or {}
        containers[cid] = {
            "name": str(c.get("Name") or "").lstrip("/"),
            "project": str(labels.get("com.docker.compose.project", "")),
            "service": str(labels.get("com.docker.compose.service", "")),
        }
        nets = (c.get("NetworkSettings") or {}).get("Networks") or {}
        for net, info in nets.items() if isinstance(nets, dict) else []:
            ip = (info or {}).get("IPAddress")
            if ip and net in ifaces:
                endpoints.append([ifaces[net], str(ip), cid])
    return {"endpoints": sorted(endpoints), "containers": containers}


class Snapshotter:
    """One poll at a time (the supervisor's thread): asks the engine, writes when it should."""

    def __init__(self, path: Path, now: Callable[[], float] = time.time) -> None:
        self.path = path
        self.now = now
        self.ids: list[str] | None = None
        self.last: dict | None = None
        self.last_written = 0.0
        self.last_described = 0.0
        self.error = ""

    def poll(self) -> bool:
        """True when a snapshot was written."""
        t = self.now()
        ids = running_ids()
        if ids is None:
            raise OSError("the engine didn't answer `ps`")
        if ids == self.ids and t - self.last_described < SNAPSHOT_EVERY:
            return False
        ifaces = interfaces()
        if ifaces is None:
            raise OSError("the engine didn't list its networks")
        snap = describe(ids, ifaces)
        if snap is None:
            again = running_ids()
            if again is None:
                raise OSError("the engine didn't answer `ps`")
            if again != ids:
                return False  # one came or went between ps and inspect: the next poll asks again
            raise OSError("the engine didn't describe its containers")
        self.ids, self.last_described = ids, t
        if snap == self.last and t - self.last_written < SNAPSHOT_EVERY:
            return False
        self._append({"t": round(t, 3), **snap})
        self.last, self.last_written = snap, t
        return True

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.path.stat().st_size > MAX_BYTES:
                self.path.replace(self.path.with_suffix(".1.jsonl"))
        except OSError:
            pass
        with self.path.open("a") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def run(log: Callable[[str], None], stopping: Callable[[], bool]) -> None:
    """The supervisor's snapshot loop (a daemon thread): idle unless attribution is on and the
    lima VM runs. Logs a failure once per distinct message, never raises."""
    from . import machine

    snap = Snapshotter(snapshots_file())
    said = ""
    while not stopping():
        msg = ""
        try:
            if wanted() and machine.BACKEND.name == "lima" and machine.state() == "running":
                snap.poll()
        except Exception as e:
            msg = f"attribution: snapshotting the engine's containers failed: {e}"
        if msg and msg != said:
            log(msg)
        said = msg
        for _ in range(int(POLL_EVERY / 0.2)):
            if stopping():
                return
            time.sleep(0.2)


def start(log: Callable[[str], None], stopping: Callable[[], bool]) -> threading.Thread:
    t = threading.Thread(target=run, args=(log, stopping), name="origin-snapshots", daemon=True)
    t.start()
    return t


# ── resolution: at read time, over the snapshots ──────────────────────────────────────


@dataclass(frozen=True)
class Who:
    """What a row's stamp names. ``kind``: ``container`` (resolved), ``build``, ``vm``,
    ``ambiguous`` (the snapshots either side disagree), ``unresolved`` (a stamp no snapshot
    explains: a container too short-lived to be seen, or snapshots not running), ``unattributed``
    (no stamp: not through the forwarder) or ``forged`` (a stamp that didn't verify)."""

    kind: str
    label: str
    cid: str = ""
    project: str = ""  # the engine-reported compose project (a claim)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.cid or self.label}"


def _ts(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _claim_label(cid: str, claims: dict | None) -> str:
    name = (claims or {}).get("name") or ""
    return name or f"container {cid[:12]}"


class Resolver:
    """The snapshots, indexed for lookups by time."""

    def __init__(self, records: list[dict]) -> None:
        good = [r for r in records if isinstance(r.get("t"), int | float)]
        good.sort(key=lambda r: r["t"])
        self.times = [float(r["t"]) for r in good]
        self.maps: list[dict[tuple[str, str], str]] = []
        self.claims: dict[str, dict] = {}
        for r in good:
            m = {}
            for ep in r.get("endpoints") or []:
                if isinstance(ep, list) and len(ep) == 3:
                    m[(str(ep[0]), str(ep[1]))] = str(ep[2])
            self.maps.append(m)
            for cid, claims in (r.get("containers") or {}).items():
                if isinstance(claims, dict):
                    self.claims[str(cid)] = {k: str(claims.get(k, "")) for k in _CLAIM_KEYS}

    def _address(self, net: str, ip: str, t: float) -> tuple[str, str]:
        """``(kind, id)`` for an address at time ``t`` (see the module docstring)."""
        i = bisect.bisect_right(self.times, t)
        before = self.maps[i - 1].get((net, ip)) if i > 0 else None
        after = self.maps[i].get((net, ip)) if i < len(self.maps) else None
        if before and after and before != after:
            return "ambiguous", ""
        cid = before or after
        return ("container", cid) if cid else ("unresolved", "")

    def who(self, origin: object, ts: object) -> Who | None:
        """What ``origin`` (a row's ``origin`` field) names at the row's ``ts``; ``None`` when
        the row carries none (attribution off when it was written)."""
        if origin is None:
            return None
        if origin == "unattributed":
            return Who("unattributed", "unattributed")
        if not isinstance(origin, dict):
            return Who("forged", "a stamp that didn't verify")
        via = origin.get("via")
        if via == "bridge":
            ip, _, _port = str(origin.get("src", "")).rpartition(":")
            net = str(origin.get("net", ""))
            t = _ts(ts)
            kind, cid = self._address(net, ip, t) if t is not None else ("unresolved", "")
            if kind == "container":
                claims = self.claims.get(cid)
                return Who(kind, _claim_label(cid, claims), cid, (claims or {}).get("project", ""))
            if kind == "ambiguous":
                return Who(kind, f"{ip} on {net} (two containers held it around then)")
            return Who(kind, f"{ip} on {net}")
        if via == "vm":
            what = origin.get("who")
            if what == "container":
                cid = str(origin.get("ctr", ""))
                claims = self.claims.get(cid)
                return Who("container", _claim_label(cid, claims), cid,
                           (claims or {}).get("project", ""))  # fmt: skip
            if what == "build":
                return Who("build", "a build step")
            return Who("vm", "the VM itself (an image pull, the VM user)")
        return Who("forged", "a stamp of an unknown shape")


_CACHE: dict[Path, tuple[tuple, Resolver]] = {}


def resolver(path: Path | None = None) -> Resolver:
    """The snapshots on disk (the live file and its one backup), re-read when either changes."""
    path = path or snapshots_file()
    files = [path.with_suffix(".1.jsonl"), path]
    stamp = []
    for f in files:
        try:
            st = f.stat()
            stamp.append((st.st_ino, st.st_size, st.st_mtime_ns))
        except OSError:
            stamp.append(None)
    key = tuple(stamp)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    records = []
    for f in files:
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
    res = Resolver(records)
    _CACHE[path] = (key, res)
    return res


def origin_key(origin: object) -> str | None:
    """The identity a row's stamp carries, independent of time — what the Network Log's index
    counts per host: ``bridge <net> <ip>``, ``ctr <id>``, ``build``, ``vm``, ``unattributed`` or
    ``forged``; ``None`` for a row with no ``origin``."""
    if origin is None:
        return None
    if origin == "unattributed":
        return "unattributed"
    if not isinstance(origin, dict):
        return "forged"
    if origin.get("via") == "bridge":
        ip = str(origin.get("src", "")).rpartition(":")[0]
        return f"bridge {origin.get('net', '')} {ip}"
    if origin.get("via") == "vm":
        what = origin.get("who")
        if what == "container":
            return f"ctr {origin.get('ctr', '')}"
        return "build" if what == "build" else "vm"
    return "forged"


def origin_of_key(key: str) -> object:
    """The inverse of :func:`origin_key`, enough for :meth:`Resolver.who`."""
    if key in ("unattributed", "forged"):
        return key
    kind, _, rest = key.partition(" ")
    if kind == "bridge":
        net, _, ip = rest.partition(" ")
        return {"via": "bridge", "net": net, "src": f"{ip}:0"}
    if kind == "ctr":
        return {"via": "vm", "who": "container", "ctr": rest}
    return {"via": "vm", "who": kind}


def snapshots_age(path: Path | None = None) -> float | None:
    """Seconds since the last snapshot was written; ``None`` when there is none."""
    try:
        return time.time() - (path or snapshots_file()).stat().st_mtime
    except OSError:
        return None
