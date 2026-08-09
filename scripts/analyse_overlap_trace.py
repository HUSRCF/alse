#!/usr/bin/env python3
"""How much of each co-run episode had two queues running at once?

Reads rocprofv3's kernel trace and answers the week 9-10 clause directly:
for each episode, what fraction of the busy time had kernels from two
different hardware queues executing simultaneously.

Two definitions, kept apart because they answer different questions:

  * **busy** -- time when at least one kernel is running. Denominator.
  * **overlapped** -- time when kernels from two or more distinct queues
    are running. Numerator.

Idle gaps in the trace segment the episodes, so no clock has to be
aligned between rocprof and the host. A gap threshold well under the
inter-episode sleep and well over any within-episode pause separates
them; the segmentation is reported so a wrong threshold is visible rather
than silent.

The prediction under test, written before the trace was read: the first
two episodes show an overlap fraction near zero and the later ones do
not. If both are high, the step-time account of the transient -- each
side costing the sum of the two solo steps -- is wrong about why.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def load(path: Path):
    rows = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                rows.append((int(row["Start_Timestamp"]),
                             int(row["End_Timestamp"]),
                             row["Queue_Id"]))
            except (KeyError, ValueError):
                continue
    rows.sort()
    return rows


def segment(rows, gap_ns: int):
    """Split on idle gaps in the union of the kernel intervals."""
    segments, current, last_end = [], [], None
    for start, end, queue in rows:
        if last_end is not None and start - last_end > gap_ns:
            segments.append(current)
            current = []
        current.append((start, end, queue))
        last_end = max(last_end or end, end)
    if current:
        segments.append(current)
    return segments


def coverage(rows):
    """Busy nanoseconds, and nanoseconds with two or more queues live.

    A sweep over interval endpoints rather than a bucketed sample: kernels
    here run for tens of microseconds and a coarse bucket would report
    overlap that is only two kernels sharing a bucket.
    """
    events = []
    for start, end, queue in rows:
        events.append((start, 1, queue))
        events.append((end, -1, queue))
    events.sort()
    live: dict[str, int] = {}
    busy = overlapped = 0
    previous = None
    for stamp, delta, queue in events:
        if previous is not None and stamp > previous:
            span = stamp - previous
            queues = sum(1 for count in live.values() if count > 0)
            if queues >= 1:
                busy += span
            if queues >= 2:
                overlapped += span
        live[queue] = live.get(queue, 0) + delta
        previous = stamp
    return busy, overlapped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--run", type=Path,
                        help="the run's own JSON, for the validity check")
    parser.add_argument("--gap-ms", type=float, default=400.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    rows = load(args.trace)
    segments = segment(rows, int(args.gap_ms * 1e6))
    # Segments before the episodes are warm-up and solo runs, and they
    # have one queue by construction. Episodes are the segments that
    # contain more than one queue's work.
    reported = []
    for index, chunk in enumerate(segments):
        busy, overlapped = coverage(chunk)
        queues = sorted({q for _, _, q in chunk})
        reported.append({
            "segment": index,
            "kernels": len(chunk),
            "queues": queues,
            "wall_ms": (chunk[-1][1] - chunk[0][0]) / 1e6,
            "busy_ms": busy / 1e6,
            "overlapped_ms": overlapped / 1e6,
            "overlap_fraction": overlapped / busy if busy else 0.0,
        })

    multi = [r for r in reported if len(r["queues"]) > 1]
    payload = {
        "schema_version": "burstserve.amd-overlap-analysis/v1",
        "question": ("what fraction of each co-run episode had two queues "
                     "running at once"),
        "trace": str(args.trace),
        "gap_ms": args.gap_ms,
        "segments": reported,
        "co_run_segments": [r["segment"] for r in multi],
    }
    if len(multi) >= 3:
        early = [r["overlap_fraction"] for r in multi[:2]]
        late = [r["overlap_fraction"] for r in multi[2:]]
        payload["early_overlap"] = sum(early) / len(early)
        payload["late_overlap"] = sum(late) / len(late)
        # Stated before the trace was read.
        payload["serialised_then_overlapping"] = (
            payload["early_overlap"] < 0.05
            and payload["late_overlap"] > 0.30)
    if args.run and args.run.exists():
        run = json.loads(args.run.read_text())
        payload["transient_reproduced_under_profiler"] = run.get(
            "transient_reproduced")
        payload["episode_externalities"] = [
            {m: e[m]["externality"] for m in run["models"]}
            for e in run["episodes"]
        ]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"{'seg':>4} {'kernels':>8} {'queues':>7} {'busy ms':>10} "
          f"{'overlap ms':>11} {'fraction':>9}")
    for row in reported:
        print(f"{row['segment']:4d} {row['kernels']:8d} "
              f"{len(row['queues']):7d} {row['busy_ms']:10.1f} "
              f"{row['overlapped_ms']:11.1f} {row['overlap_fraction']:9.3f}")
    if "early_overlap" in payload:
        print(f"\nfirst two co-run segments: "
              f"{payload['early_overlap']:.3f} overlap")
        print(f"later co-run segments:     "
              f"{payload['late_overlap']:.3f} overlap")
        print(f"serialised then overlapping: "
              f"{payload['serialised_then_overlapping']}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
