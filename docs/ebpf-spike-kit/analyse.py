"""Read a Tetragon JSON export + the workload's MARK/CGROUP lines; print what the spike asks.

    python3 analyse.py tetragon.json workload.out

Stdlib only. Judges every event against the container ids the workload printed (ground truth),
never against Tetragon's own `docker` field alone.
"""

import json
import statistics
import sys
from collections import Counter


def main(events_path: str, workload_path: str) -> None:
    marks: dict[str, tuple[str, str]] = {}
    for line in open(workload_path):
        parts = line.split()
        if parts[:1] == ["MARK"]:
            marks[parts[2]] = (parts[1], parts[3] if len(parts) > 3 else "")
        elif parts[:1] in (["CGROUP"], ["SURFACE"]):
            print(line.rstrip())
    ids = {k: v[1][:31] for k, v in marks.items() if k in ("box", "sibling", "flat", "forge")}
    who = {v: k for k, v in ids.items()}

    events = [json.loads(line) for line in open(events_path) if line.strip()]
    start, end = marks["box"][0][:23], marks["done"][0][:23]

    def body(e: dict) -> tuple[str, dict]:
        kind = next(k for k in e if k.startswith("process_"))
        return kind, e[kind]

    in_run = [e for e in events if start <= e["time"][:23] <= end]
    print(f"\nevents in run window: {len(in_run)}")

    print("\nfile/connect events (kprobe), with Tetragon's attribution:")
    for e in in_run:
        kind, b = body(e)
        if kind != "process_kprobe":
            continue
        p = b["process"]
        arg = b.get("args", [{}])[0]
        what = arg.get("file_arg", {}).get("path") or arg.get("sock_arg", {}).get("daddr")
        claimed = who.get((p.get("docker") or "")[:31], p.get("docker") or "-")
        print(
            f"  {e['time'][11:23]} {b['function_name']:<26} {p.get('binary'):<22} {what:<16}"
            f" uid={p.get('uid')} reported-as={claimed}"
        )

    print("\nexecs of the test payloads, by reported container:")
    c: Counter = Counter()
    for e in in_run:
        kind, b = body(e)
        p = b["process"]
        if kind == "process_exec" and p.get("binary") in ("/bin/cat", "/bin/sleep", "/bin/true"):
            c[(p["binary"], who.get((p.get("docker") or "")[:31], p.get("docker") or "-"))] += 1
    for (binary, claimed), n in sorted(c.items()):
        print(f"  {n:>4}  {binary:<10} reported-as={claimed}")

    lo, hi = marks["churn-start"][0][:23], marks["churn-end"][0][:23]
    churn = [e for e in in_run if lo <= e["time"][:23] <= hi]
    sizes = [len(json.dumps(e)) for e in churn]
    print(
        f"\nchurn (500 execs): {len(churn)} events, median {statistics.median(sizes):.0f} B/event,"
        f" {sum(sizes) / 1e6:.2f} MB"
    )
    noisy = Counter(body(e)[1]["process"].get("binary") for e in churn)
    print("  busiest binaries:", noisy.most_common(4))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
