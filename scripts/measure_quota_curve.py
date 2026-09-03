#!/usr/bin/env python3
"""Solo per-step cost as a function of quota, through the stream path.

``MEASURED_QUOTA_SECONDS`` is keyed by model alone and every entry carries
``maskable_units: 32``. A second SKU needs its own curve, and it cannot be
taken with Gate B's sweep: that sweep fixes a cell's quota with
``ROC_GLOBAL_CU_MASK``, and on gfx90a that path silently rounds 52 up to
64 and 78 up to 104 (`experiments/probes/gfx90a/mask_contract_20260903.json`).
``hipExtStreamCreateWithCUMask`` honours both exactly, and it is also the
path the scheduler actually runs on, so the curve is measured there.

Reuses ``run_amd_matrix_cell.isolated_service`` rather than timing steps
again here. This project has had two step-timing defects -- a half-die
figure recorded as the full-die one, and a peak inflated by asynchronous
execution -- and neither would have happened in a single implementation.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from burstserve.masked_streams import MaskedStreamPool          # noqa: E402
import run_amd_mismatched_corun as harness                      # noqa: E402
from run_amd_matrix_cell import isolated_service                # noqa: E402

SCHEMA = "burstserve.quota-curve/v1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sdxl")
    ap.add_argument("--maskable-units", type=int, default=32)
    ap.add_argument("--quotas", default="")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--repeats", type=int, default=5,
                    help="measurements per quota. The CV over these is "
                         "reported and Gate B's threshold is 5%%.")
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("--width", type=int, default=768)
    ap.add_argument("--video-height", type=int, default=480)
    ap.add_argument("--video-width", type=int, default=720)
    ap.add_argument("--frames", type=int, default=9)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    units = args.maskable_units
    quotas = ([int(q) for q in args.quotas.split(",") if q]
              or [max(1, round(units * f / 32)) for f in (4, 8, 16, 24, 28, 32)])
    quotas = sorted({min(units, max(1, q)) for q in quotas})

    import torch
    began = time.perf_counter()
    pipeline = harness.build_pipeline(args.model, drop_text_encoders=False)
    adapter = harness.make_adapter(args.model, pipeline, args, seed=1000)
    harness.free_text_encoders(pipeline)
    pool = MaskedStreamPool(harness.make_stream, maskable_units=units)

    # Kernel compilation is a property of the process, not of the quota:
    # the first width used once read 1773 ms for an 8-unit step and moved
    # a solo MAPE from 3% to 24%. Pay it at the widest quota before
    # anything is measured.
    harness.warm(adapter, pool, units, args)

    props = torch.cuda.get_device_properties(0)
    rows = []
    for quota in quotas:
        stream = pool.for_quota(quota)
        installed = bin(stream.installed_mask).count("1")
        service, step_p99 = [], []
        for _ in range(args.repeats):
            s, p = isolated_service(adapter, pool, args.steps, args, torch,
                                    quota=quota)
            service.append(s)
            step_p99.append(p)
        mean = statistics.mean(service)
        cv = (statistics.stdev(service) / mean) if len(service) > 1 else 0.0
        row = {
            "quota": quota,
            # The readback, not the request. A width that was not honoured
            # reads back wider, and on gfx90a's process path several do.
            "installed_units": installed,
            "mask_honoured": installed == quota,
            "requested_mask": hex(stream.requested_mask),
            "installed_mask": hex(stream.installed_mask),
            "service_s": mean,
            "service_s_all": service,
            "step_p99_s": statistics.mean(step_p99),
            "step_seconds": mean / args.steps,
            "cv": cv,
            "meets_cv_threshold": cv <= 0.05,
        }
        rows.append(row)
        print(f"  quota {quota:>4} -> installed {installed:>4} "
              f"{'ok' if row['mask_honoured'] else 'NOT HONOURED':<13} "
              f"step {row['step_seconds']*1000:8.2f} ms  cv {cv:6.2%}",
              flush=True)

    full = next((r for r in rows if r["quota"] == units), None)
    payload = {
        "schema_version": SCHEMA,
        "model": args.model,
        "maskable_units": units,
        "path": "hipExtStreamCreateWithCUMask",
        "steps": args.steps,
        "repeats": args.repeats,
        "workpoint": ({"height": args.height, "width": args.width}
                      if args.model == "sdxl"
                      else {"frames": args.frames,
                            "height": args.video_height,
                            "width": args.video_width}),
        "device": {"name": props.name,
                   "arch": getattr(props, "gcnArchName", None),
                   "multi_processor_count": props.multi_processor_count},
        "torch": torch.__version__,
        "hip": getattr(torch.version, "hip", None),
        "step_seconds_at_full": full["step_seconds"] if full else None,
        "worst_cv": max(r["cv"] for r in rows) if rows else None,
        "all_masks_honoured": all(r["mask_honoured"] for r in rows),
        "rows": rows,
        "wall_clock_s": time.perf_counter() - began,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1))
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
