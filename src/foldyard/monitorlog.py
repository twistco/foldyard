"""monitorlog.py — the guest monitor's events on the host (ADR-0031, slice 2).

Two halves, deliberately apart:

* **The pull** (:func:`pull_once`, run on a thread of the host supervisor by :func:`run`):
  fetches what the guest relay (``assets/monitor/relay.py``) spooled since the last pull, over
  the backend's ssh as the VM user, in BYTES (a decoded stream would make every byte offset after
  the first multi-byte character wrong). Each line is checked against the relay key
  (:func:`monitor.relay_key`) and its per-boot sequence, then appended — verified records only,
  with what was missing or forged recorded as such — to a bounded store under the state dir. The
  host never trusts the transport: whatever sits between the root relay and here (the VM user's
  ssh session included) can drop or delay lines, and the sequence shows it; it can't forge them.
  New container ids are described by asking the engine once (name, compose project) and that is
  stored too — as a CLAIM: names and labels are whatever the container's creator set.

* **The join** (:func:`attribute`, pure): which container did each event. The relay ships each
  event's mount-namespace inode and, separately, kernel snapshots of inode → container; the join
  happens here, at read time, over the stored evidence — so it can be improved and re-run without
  touching the VM. See :func:`attribute` for the rules, including why a namespace number on its
  own is never trusted across time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import config
from .sandbox import SshBackend, _ssh_argv

SPOOL = "/var/lib/fy-monitor/spool"
FETCH_BUDGET = 8_000_000  # bytes per pull; a full one is followed by another at once
STORE_BYTES = 32 << 20
STORE_BACKUPS = 4
PULL_EVERY = 5.0


def store_path() -> Path:
    return config.state_dir() / "logs" / "monitor.jsonl"


def _cursor_path() -> Path:
    return config.state_dir() / "monitor-cursor.json"


# ── the cursor: where the host has read up to, and how it went ───────────────────────────


@dataclass
class Cursor:
    path: str = ""  # "<epoch>-<boot>/<first seq>.jsonl", relative to the spool
    off: int = 0
    last: dict[str, int] = field(default_factory=dict)  # boot -> last seq stored
    described: list[str] = field(default_factory=list)  # container ids already asked about
    pulled: float = 0.0  # last successful pull (host time)
    error: str = ""  # the last pull's failure, "" when it worked
    stored: int = 0
    gaps: int = 0  # sequence numbers that never arrived
    forged: int = 0  # lines that failed the HMAC
    behind: bool = False  # the last pull filled its budget: more is waiting

    @classmethod
    def load(cls) -> Cursor:
        try:
            raw = json.loads(_cursor_path().read_text())
            return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        path = _cursor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self)))
        tmp.replace(path)


# ── the pull ──────────────────────────────────────────────────────────────────────────


def fetch_script(cursor_path: str, cursor_off: int, budget: int = FETCH_BUDGET) -> str:
    """Run in the guest as the VM user: every spool segment at or after the cursor, from the
    cursor's offset, each as ``@@ <path> <offset> <bytes>`` then exactly that many bytes, at most
    ``budget`` bytes in all. Segment paths sort chronologically by construction (an epoch-prefixed
    boot dir, then the zero-padded first seq), so a string compare is the cursor test. Nothing in
    it writes: the VM user can't, and doesn't need to."""
    cur, off = shlex.quote(cursor_path), int(cursor_off)
    return f"""
cd {SPOOL} 2>/dev/null || exit 0
budget={int(budget)}
find . -mindepth 2 -maxdepth 2 -name '*.jsonl' | sort | while IFS= read -r p; do
  p=${{p#./}}
  [[ "$p" < {cur} ]] && continue
  off=0; [[ "$p" == {cur} ]] && off={off}
  size=$(stat -c %s "$p" 2>/dev/null) || continue
  [ "$size" -gt "$off" ] || continue
  n=$((size - off)); [ "$n" -gt "$budget" ] && n=$budget
  printf '@@ %s %s %s\\n' "$p" "$off" "$n"
  tail -c +$((off + 1)) "$p" | head -c "$n"
  budget=$((budget - n)); [ "$budget" -gt 0 ] || break
done
"""


def parse_fetch(out: bytes) -> list[tuple[str, int, bytes]]:
    """``[(path, offset, bytes)]`` from :func:`fetch_script`'s output. A chunk shorter than its
    header promised (the segment shrank — pruned mid-read) is kept as far as it goes."""
    chunks, i = [], 0
    while i < len(out):
        nl = out.find(b"\n", i)
        if nl < 0 or not out.startswith(b"@@ ", i):
            break
        _, path, off, n = out[i:nl].decode().split(" ")
        start = nl + 1
        data = out[start : start + int(n)]
        chunks.append((path, int(off), data))
        i = start + len(data)
    return chunks


def verify(key: bytes, boot: str, line: bytes) -> tuple[int, dict] | None:
    """``(seq, record)`` for a line the relay signed with ``key``, else ``None``."""
    try:
        seq_s, mac, body = line.decode().split(" ", 2)
        seq = int(seq_s)
    except ValueError:
        return None
    want = hmac.new(key, f"{boot}:{seq}:{body}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, want):
        return None
    try:
        return seq, json.loads(body)
    except ValueError:
        return None


def ingest(cur: Cursor, key: bytes, chunks: list[tuple[str, int, bytes]], now: float) -> list[dict]:
    """The store records for ``chunks``, advancing ``cur`` past every COMPLETE line (a trailing
    half-line — the relay mid-write, or the fetch budget — is left for the next pull)."""
    out: list[dict] = []
    for path, off, data in chunks:
        boot = path.split("/", 1)[0].split("-", 1)[-1]
        complete = data[: data.rfind(b"\n") + 1]
        for line in complete.splitlines():
            got = verify(key, boot, line)
            if got is None:
                cur.forged += 1
                out.append({"k": "forged", "boot": boot, "path": path, "recv": now})
                continue
            seq, rec = got
            last = cur.last.get(boot, -1)
            if seq <= last:
                continue  # already stored (a re-read after a crash between store and cursor)
            if seq > last + 1:
                cur.gaps += seq - last - 1
                out.append({"k": "gap", "boot": boot, "from": last + 1, "to": seq - 1, "recv": now})
            cur.last[boot] = seq
            out.append({**rec, "boot": boot, "seq": seq, "recv": now})
        if complete or path != cur.path:
            cur.path, cur.off = path, off + len(complete)
    return out


def _append(records: list[dict]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > STORE_BYTES:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path.replace(path.with_name(f"{path.stem}.{stamp}{path.suffix}"))
        for old in config.rotated_logs(path)[:-STORE_BACKUPS]:
            old.unlink(missing_ok=True)
    with path.open("a") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def _engine_describe(ids: list[str]) -> dict[str, dict | None]:
    """What the engine says about ``ids``: name + compose project/service — CLAIMS, whatever the
    creator set. ``None`` for an id the engine no longer has (a short-lived container already
    gone: the box's own business, in the operator's reading)."""
    from . import devmode  # import-light module; only the pull needs the engine env

    try:
        res = subprocess.run(
            [config.engine(), "ps", "-a", "--no-trunc", "--format",
             "{{.ID}}\t{{.Names}}\t{{json .Labels}}"],
            capture_output=True, text=True, timeout=15, env=devmode._engine_env(),
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return {}
    if res.returncode != 0:
        return {}
    known: dict[str, dict] = {}
    for line in res.stdout.splitlines():
        cid, _, rest = line.partition("\t")
        name, _, raw = rest.partition("\t")
        labels = devmode._parse_labels(raw)
        known[cid] = {
            "name": name,
            "project": labels.get("com.docker.compose.project", ""),
            "service": labels.get("com.docker.compose.service", ""),
        }
    return {cid: known.get(cid) for cid in ids}


def _fetch(target, script: str) -> bytes:
    res = subprocess.run(
        [*_ssh_argv(target), f"bash -c {shlex.quote(script)}"], capture_output=True, timeout=60
    )
    if res.returncode != 0:
        raise OSError(res.stderr.decode(errors="replace").strip() or f"exit {res.returncode}")
    return res.stdout


def pull_once(
    backend: SshBackend, name: str, key: str, now: Callable[[], float] = time.time
) -> int:
    """One pull: fetch → verify → store → describe new containers → advance the cursor. Returns
    the number of records stored; failures are recorded on the cursor (the doctor row reads it)
    and re-raised for the caller to log."""
    cur = Cursor.load()
    try:
        target = backend.ssh_target(name)
        if target is None:
            raise OSError("no ssh route into the VM")
        chunks = parse_fetch(_fetch(target, fetch_script(cur.path, cur.off)))
        cur.behind = sum(len(data) for _, _, data in chunks) >= FETCH_BUDGET
        t = now()
        # the relay keys its HMAC with the key file's text (the hex string), so the host does too
        records = ingest(cur, key.encode(), chunks, t)
        new_ids = sorted(
            {cid for r in records if r.get("k") == "scopes" for ids in r["map"].values()
             for cid in ids} - set(cur.described)
        )  # fmt: skip
        if new_ids:
            records.append({"k": "engine", "recv": t, "containers": _engine_describe(new_ids)})
            cur.described = (cur.described + new_ids)[-2000:]
        if records:
            _append(records)
        cur.stored += len(records)
        cur.pulled, cur.error = t, ""
        cur.save()
        return len(records)
    except Exception as e:
        cur.error = f"{type(e).__name__}: {e}"[:300]
        cur.save()
        raise


def run(log: Callable[[str], None], stopping: Callable[[], bool]) -> None:
    """The supervisor's pull loop (a daemon thread): every :data:`PULL_EVERY` seconds while the
    monitor is wanted and the VM runs. Logs a failure once per distinct message, never raises."""
    from . import machine, monitor

    said = ""
    while not stopping():
        msg = ""
        behind = False
        try:
            if monitor.wanted() and machine.state() == "running":
                pull_once(machine.BACKEND, machine.MACHINE, monitor.relay_key())
                behind = Cursor.load().behind
        except Exception as e:
            msg = f"guest monitor: pulling events failed: {e}"
        if msg and msg != said:
            log(msg)
        said = msg
        if behind:
            continue  # catching up: the guest's spool is bounded, a backlog left there is lost
        for _ in range(int(PULL_EVERY / 0.2)):
            if stopping():
                return
            time.sleep(0.2)


def start(log: Callable[[str], None], stopping: Callable[[], bool]) -> threading.Thread:
    t = threading.Thread(target=run, args=(log, stopping), name="monitor-pull", daemon=True)
    t.start()
    return t


# ── the join ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Event:
    """One normalised event (the schema `fy monitor` prints). ``who`` is how it was attributed:
    ``container`` (a kernel snapshot names its scope), ``vm`` (the VM's own namespace: a VM
    process, e.g. podman, conmon, pasta, sshd), ``ambiguous`` (the snapshots disagree, or one
    namespace held two scopes — never resolved by guessing) or ``unattributed`` (no snapshot
    holds its namespace: a container too short-lived to be seen, or one outside podman's scopes —
    read it as the dev box's own activity)."""

    time: str
    boot: str
    seq: int
    kind: str  # exec | exit | connect | file | other
    binary: str
    args: str
    detail: str
    uid: int | None
    pid: int | None
    who: str
    container: str = ""  # the 64-hex id, when who == "container"
    name: str = ""  # engine-reported (a claim)
    worktree: str = ""  # derived from the engine-reported compose project / box name (a claim)


def _kind(body: dict, kind: str) -> tuple[str, str]:
    if kind == "process_exec":
        return "exec", ""
    if kind == "process_exit":
        return "exit", str(body.get("status", "") or body.get("signal", "") or "")
    if kind == "process_kprobe":
        fn = body.get("function_name", "")
        arg = (body.get("args") or [{}])[0]
        if fn == "tcp_connect":
            s = arg.get("sock_arg", {})
            return "connect", f"{s.get('daddr', '?')}:{s.get('dport', '?')}"
        if "file_arg" in arg:
            args = body.get("args") or []
            mask = args[1].get("int_arg") if len(args) > 1 else None
            what = {2: "write", 4: "read"}.get(mask or 0, f"mask {mask}")
            ret = (body.get("return") or {}).get("int_arg")
            outcome = (
                "" if ret in (0, None) else f" → {ret}"
            )  # a refused access, as the kernel said
            return "file", f"{what} {arg['file_arg'].get('path', '?')}{outcome}"
        return "other", fn
    return "other", kind


def worktree_of(meta: dict | None, prefix: str) -> str:
    """The worktree a container CLAIMS to belong to: its compose project (``<prefix>`` = main,
    ``<prefix>-<wt>``) or the box name foldyard gives (``<project>-devbox``)."""
    if not meta:
        return ""
    project = meta.get("project") or ""
    name = meta.get("name") or ""
    if not project and name.endswith("-devbox"):
        project = name[: -len("-devbox")]
    if project == prefix:
        return "main"
    if project.startswith(prefix + "-"):
        return project[len(prefix) + 1 :]
    return ""


def attribute(records: Iterable[dict], prefix: str) -> list[Event]:
    """Join events to containers, per boot, over the stored records (in store order).

    For each event, look at the LAST snapshot before it and the FIRST snapshot after it, and use
    only one that contains the event's mount namespace:

    * the snapshot before, when it still holds the namespace — the container was alive then;
    * otherwise the snapshot after — the relay snapshots within a second of an unseen namespace,
      so a container that started just before its first event is named by the next one;
    * both, disagreeing → ``ambiguous``. A namespace number is freed with its container and can
      be handed to the next one; a mapping is therefore only ever trusted from the snapshot
      nearest the event, never carried forward past a snapshot that no longer had it.

    The VM's own namespace is ``vm``; anything else unmatched is ``unattributed``. Duplicate
    (boot, seq) records — a re-read after a crash — count once.
    """
    by_boot: dict[str, list[dict]] = {}
    meta: dict[str, dict | None] = {}
    seen: set[tuple[str, int]] = set()
    for r in records:
        k = r.get("k")
        if k == "engine":
            meta.update(r.get("containers") or {})
            continue
        if k in ("ev", "scopes") and "boot" in r:
            key = (r["boot"], int(r["seq"]))
            if key in seen:
                continue
            seen.add(key)
            by_boot.setdefault(r["boot"], []).append(r)

    out: list[Event] = []
    for boot, recs in by_boot.items():
        recs.sort(key=lambda r: int(r["seq"]))
        snaps = [r for r in recs if r["k"] == "scopes"]
        vm = {str(s.get("vm")) for s in snaps}
        si = 0  # index of the first snapshot AFTER the current record
        for r in recs:
            if r["k"] == "scopes":
                si += 1
                continue
            ev = r.get("e") or {}
            kind = next((k for k in ev if k.startswith("process_")), "")
            body = ev.get(kind) or {}
            proc = body.get("process") or {}
            inode = str(((proc.get("ns") or {}).get("mnt") or {}).get("inum") or "")
            before = snaps[si - 1]["map"].get(inode) if si > 0 else None
            after = snaps[si]["map"].get(inode) if si < len(snaps) else None
            ids = before or after
            if inode in vm:
                who, cid = "vm", ""
            elif before and after and before != after:
                who, cid = "ambiguous", ""
            elif ids and len(ids) == 1:
                who, cid = "container", ids[0]
            elif ids:
                who, cid = "ambiguous", ""
            else:
                who, cid = "unattributed", ""
            what, detail = _kind(body, kind)
            m = meta.get(cid) if cid else None
            out.append(
                Event(
                    time=ev.get("time", ""),
                    boot=boot,
                    seq=int(r["seq"]),
                    kind=what,
                    binary=proc.get("binary", ""),
                    args=proc.get("arguments", ""),
                    detail=detail,
                    uid=proc.get("uid"),
                    pid=proc.get("pid"),
                    who=who,
                    container=cid,
                    name=(m or {}).get("name", "") if m else "",
                    worktree=worktree_of(m, prefix),
                )
            )
    return out


READ_BYTES = 8 << 20  # how much of the store's tail `recent` joins over


def _tail(max_bytes: int = READ_BYTES) -> list[dict]:
    """The stored records in the last ``max_bytes`` of the store (reaching into the newest backup
    when the live file is shorter), oldest first. Wider than ``config.tail_jsonl``'s window on
    purpose: the join needs the snapshots around the events it shows, and the relay snapshots
    once a minute — so it reads megabytes, not the last few hundred lines."""
    paths = [*config.rotated_logs(store_path())[-1:], store_path()]
    chunks: list[bytes] = []
    budget = max_bytes
    for path in reversed(paths):
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                start = max(0, f.tell() - budget)
                f.seek(start)
                data = f.read()
        except OSError:
            continue
        if start > 0:
            data = data[data.find(b"\n") + 1 :]  # drop the partial first line
        chunks.insert(0, data)
        budget -= len(data)
        if budget <= 0:
            break
    out = []
    for line in b"".join(chunks).splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def recent(limit: int = 50, prefix: str | None = None) -> list[Event]:
    """The last ``limit`` attributed events, joined over the tail of the store."""
    events = attribute(_tail(), prefix if prefix is not None else config.project_prefix())
    return events[-limit:]


# ── `fy monitor` ──────────────────────────────────────────────────────────────────────


def _who(e: Event) -> str:
    if e.who == "container":
        where = e.worktree or "?"
        return f"{where}:{e.name or e.container[:12]}"
    return e.who


def show(limit: int = 40, worktree: str = "", as_json: bool = False) -> int:
    """`fy monitor`: the last ``limit`` events the guest monitor recorded, attributed. Host only:
    the store lives on the host, outside the box's reach."""
    if config.in_box():
        print("✗ run on your computer — the monitor's events are kept there, out of the box")
        return 1
    events = recent(limit=100_000)
    if worktree:
        events = [e for e in events if e.worktree == worktree]
    events = events[-limit:]
    if as_json:
        for e in events:
            print(json.dumps(asdict(e)))
        return 0
    cur = Cursor.load()
    if not events:
        print("no monitor events yet" + (f" for worktree '{worktree}'" if worktree else ""))
    for e in events:
        what = e.detail or e.args
        print(f"{e.time[11:19]}  {_who(e):<28.28}  {e.kind:<7}  {e.binary:<28.28}  {what[:80]}")
    age = f"{time.time() - cur.pulled:.0f}s ago" if cur.pulled else "never"
    print(
        f"— last pull {age}; {cur.stored} stored, {cur.gaps} lost in transit, {cur.forged} "
        "failed their signature. Names and worktrees are what the engine reports (claims); "
        "`unattributed` is a container too short-lived to be seen — the box's own activity."
    )
    if cur.error:
        print(f"⚠ the last pull failed: {cur.error}")
    return 0
