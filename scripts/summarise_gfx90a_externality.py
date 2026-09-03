#!/usr/bin/env python3
"""Turn the gfx90a co-run runs into the table trace_sim reads.

The gfx1201 table is keyed by ``(own_units, peer_units)`` and holds the
slowdown of the tenant with ``own_units``. One co-run therefore fills two
entries, and the three splits measured here fill five: (13,91) (91,13)
(26,78) (78,26) (52,52).

Reported per GCD as well as pooled, because the 2026-08-25 probe resolved
a deterministic 0.6% device-to-device difference on this machine and a
table that hides it is claiming a precision it does not have.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", type=Path)
    ap.add_argument("--maskable-units", type=int, default=104)
    args = ap.parse_args()

    entries: dict[tuple[int, int], list[tuple[str, float]]] = {}
    solo: dict[int, list[float]] = {}
    for path in sorted(args.directory.glob("*.json")):
        payload = json.loads(path.read_text())
        # v1 is the schema the gfx1201 table was built from; v2 adds a
        # top-level summary and an overlap-sufficiency flag. Both carry
        # the same per-trial fields, and this reads those, so the two
        # architectures' tables are computed the same way.
        if not str(payload.get("schema_version", "")).startswith(
                "burstserve.amd-inproc-corun/"):
            continue
        overlap = payload.get("overlap") or {}
        if overlap and overlap.get("sufficient_overlap") is False:
            print(f"  SKIPPED {path.name}: insufficient overlap "
                  f"({overlap.get('overlap_fraction_of_longer_window')})")
            continue
        units = payload["units"]
        a, b = int(units["a"]), int(units["b"])
        gcd = path.stem.split("_g")[-1]
        for side, own, peer in (("left", a, b), ("right", b, a)):
            for trial in payload["trials"]:
                # A side with fewer than two samples inside the window
                # has no externality; it is absent rather than zero.
                value = trial[side].get("externality")
                if value is None:
                    continue
                entries.setdefault((own, peer), []).append(
                    (gcd, 1.0 + value))
                solo.setdefault(own, []).append(
                    trial["solo_p50_s"][side])

    if not entries:
        print(f"no in-process co-run payloads in {args.directory}")
        return 1

    print(f"{'own+peer':>10}  {'n':>3}  {'mean':>7}  {'sd':>7}  "
          f"{'min':>7}  {'max':>7}   per GCD")
    table = {}
    for key in sorted(entries, key=lambda k: (-k[0], k[1])):
        values = [v for _, v in entries[key]]
        by_gcd: dict[str, list[float]] = {}
        for gcd, value in entries[key]:
            by_gcd.setdefault(gcd, []).append(value)
        detail = "  ".join(
            f"g{gcd}:{statistics.mean(vs):.4f}"
            for gcd, vs in sorted(by_gcd.items()))
        sd = statistics.stdev(values) if len(values) > 1 else 0.0
        print(f"{key[0]:4d}+{key[1]:<5d}  {len(values):3d}  "
              f"{statistics.mean(values):7.4f}  {sd:7.4f}  "
              f"{min(values):7.4f}  {max(values):7.4f}   {detail}")
        table[key] = round(statistics.mean(values), 4)

    missing = []
    for own in sorted({k[0] for k in table} | {k[1] for k in table}):
        peer = args.maskable_units - own
        if own != peer and (own, peer) not in table:
            missing.append(f"{own}+{peer}")
    if missing:
        print(f"\n  missing entries: {', '.join(missing)}")

    print("\nMEASURED_EXTERNALITY_GFX90A: dict[tuple[int, int], float] = {")
    for key in sorted(table, key=lambda k: k[0]):
        print(f"    {key}: {table[key]},")
    print("}")

    print("\nsolo per-call p50 by width, seconds (a cross-check on the "
          "quota curve):")
    for width in sorted(solo):
        print(f"  {width:4d}u  {statistics.mean(solo[width]):8.4f}  "
              f"n={len(solo[width])}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
