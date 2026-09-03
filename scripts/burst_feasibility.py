"""Can a burst meet its deadline under a spatial split, on this device?

3.8 is a structural result and it was published as a hand-computed table.
This is that table as a program, so it can be checked, re-run when a cost
table changes, and pointed at the second SKU.

The model, which is the runtime's and not a simplification of it:

* the registry offers **one request per tenant per round**, so a burst of
  `n` requests is served serially whatever the split;
* a round is paced by its slowest member, so it costs
  `max(own_step, peer_step)`;
* with the round barrier off, a request runs
  `min(cap, floor(peer_step / own_step), steps_remaining)` steps per
  round -- and a request with eight steps cannot use a budget larger
  than eight, which is why raising the cap stops helping.

Externality is **off by default and that is a floor, not a neutral
choice**: a co-run is slower than a solo, so every partitioned burst
below is optimistic and the exclusive column is exact. Pass
``--externality`` to apply the measured table where one exists.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.trace_sim import (  # noqa: E402
    DEVICE_TABLES,
    QuotaCostModel,
    UnmeasuredPairing,
    externality,
)


def splits_for(model: QuotaCostModel) -> list[int]:
    """Every measured quota that leaves something for the peer."""
    return sorted(q for q, _ in model.measured_curve
                  if q < model.maskable_units)


def row(urgent: QuotaCostModel, video: QuotaCostModel, quota: int,
        steps: int, burst: int, cap: int, use_externality: bool,
        concurrency: int = 1, same_model_penalty: float = 1.0,
        peer_penalty: float = 1.0, device: str = "gfx1201") -> dict:
    """One split's burst completion, optionally with intra-tenant slices.

    ``concurrency`` divides the urgent tenant's own quota among that many
    of its own requests, on disjoint masks -- the arrangement expC tests.
    It is the only thing here that shortens a serial burst, because the
    burst then runs in ``ceil(burst / concurrency)`` batches instead of
    ``burst`` of them. Each slice pays the SAME-MODEL co-run penalty to
    its siblings, which is what 1.3 measures and is bistable on gfx1201.
    """
    peer_quota = urgent.maskable_units - quota
    # The policy takes ``critical[:concurrency]`` and divides among what
    # it took, so asking for more slices than the burst has requests does
    # not make the slices narrower -- it just leaves the extra unused.
    # Dividing by ``concurrency`` here charged a width nobody would grant.
    active = max(1, min(concurrency, burst))
    slice_quota = max(1, quota // active)
    own = urgent.step_seconds(slice_quota)
    peer = video.step_seconds(peer_quota)
    if use_externality:
        own *= externality(quota, peer_quota, device=device)
        peer *= externality(peer_quota, quota, model="cogvideox-2b",
                            device=device)
    else:
        peer *= peer_penalty
    if active > 1:
        own *= same_model_penalty
    budget = max(1, min(cap, int(peer // own), steps))
    rounds = math.ceil(steps / budget)
    round_cost = max(own, peer)
    batches = math.ceil(burst / active)
    return {
        "split": f"{quota}+{peer_quota}",
        "slice_quota": slice_quota,
        "slice_measured": urgent.is_measured(slice_quota),
        "own_s": own, "peer_s": peer, "budget": budget,
        "rounds_per_request": rounds,
        "request_s": rounds * round_cost,
        "burst_s": batches * rounds * round_cost,
    }


def exclusive(urgent: QuotaCostModel, steps: int, burst: int,
              concurrency: int = 1, same_model_penalty: float = 1.0) -> dict:
    """The whole die, optionally split among the tenant's own requests.

    With no peer there is nothing pacing a round, so each slice simply
    runs its own steps and the burst takes ``ceil(burst / c)`` batches of
    them. This is the arrangement the measured curves make interesting:
    on gfx1201 four ways finishes in 2.79 s against 3.70 s served
    serially, and on gfx90a 3.12 s against 3.83 s. Eight ways needs a
    burst of eight to be usable at all -- the policy takes only as many
    slices as it has requests, so ``concurrency`` is capped at ``burst``
    here as it is there -- and at that burst gfx1201 improves to 5.41 s
    while gfx90a costs 8.43 s against 7.66 s for not splitting, because
    that die's efficiency optimum sits at a quarter of it (1.10).

    ``same_model_penalty`` is what it turns on, and it is a **pairwise**
    number being used for an N-way arrangement -- 1.297 is 1.3's pair at
    16+16. `scripts/run_amd_nway_corun.py` measures the real one.
    """
    full = urgent.maskable_units
    active = max(1, min(concurrency, burst))
    slice_quota = max(1, full // active)
    own = urgent.step_seconds(slice_quota)
    if active > 1:
        own *= same_model_penalty
    batches = math.ceil(burst / active)
    return {
        "split": f"{full}+0",
        "slice_quota": slice_quota,
        "slice_measured": urgent.is_measured(slice_quota),
        "own_s": own, "peer_s": None, "budget": steps,
        "rounds_per_request": steps,
        "request_s": steps * own,
        "burst_s": batches * steps * own,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="gfx1201",
                        choices=sorted(DEVICE_TABLES))
    parser.add_argument("--urgent-model", default="sdxl")
    parser.add_argument("--video-model", default="cogvideox-2b")
    parser.add_argument("--steps", type=int, default=8,
                        help="denoising steps in one urgent request")
    parser.add_argument("--burst", type=int, default=4,
                        help="urgent requests arriving together, sharing "
                             "ONE absolute deadline")
    parser.add_argument("--cap", type=int, default=16,
                        help="max_steps_per_round with the barrier off")
    parser.add_argument("--slack", type=float, default=1.5)
    parser.add_argument("--deadline-s", type=float, default=None,
                        help="use a measured deadline instead of the one "
                             "derived from the curve. The campaigns take "
                             "the base from the ISOLATED REQUEST LATENCY "
                             "measured in the cell, which on gfx1201 was "
                             "0.89 s against the curve's 0.924 -- so the "
                             "derived deadline here is 5.54 s where the "
                             "runs used 5.34 s, and the derived one is "
                             "the more generous of the two.")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="intra-tenant slices: the urgent tenant's "
                             "quota divided among this many of its own "
                             "requests on disjoint masks. 1 is the "
                             "runtime as it stands and as 3.8 measured it")
    parser.add_argument("--same-model-penalty", type=float, default=1.0,
                        help="co-run penalty between the intra-tenant "
                             "slices. 1.297 is 1.3's fast state on "
                             "gfx1201, 1.949 its slow one, 1.2336 gfx90a")
    parser.add_argument("--peer-penalty", type=float, default=1.0,
                        help="mismatched penalty charged to the video "
                             "tenant when --externality is not used")
    parser.add_argument("--externality", action="store_true",
                        help="apply the measured co-run penalty; without "
                             "it every partitioned row is a floor")
    args = parser.parse_args()

    urgent = QuotaCostModel.for_model(args.urgent_model, device=args.device)
    video = QuotaCostModel.for_model(args.video_model, device=args.device)
    full = urgent.step_seconds(urgent.maskable_units)
    deadline = (args.deadline_s if args.deadline_s is not None
                else args.slack * (args.steps * full) * args.burst)

    rows, skipped = [], []
    for quota in splits_for(urgent):
        try:
            rows.append(row(urgent, video, quota, args.steps, args.burst,
                            args.cap, args.externality, args.concurrency,
                            args.same_model_penalty, args.peer_penalty,
                            args.device))
        except UnmeasuredPairing:
            # Named rather than dropped: a table that quietly loses its
            # widest split reads as coverage it does not have.
            skipped.append(f"{quota}+{urgent.maskable_units - quota}")
    rows.append(exclusive(urgent, args.steps, args.burst, args.concurrency,
                          args.same_model_penalty))

    print(f"device {args.device}  urgent {args.urgent_model} "
          f"({args.steps} steps x {args.burst}) beside {args.video_model}"
          + (f"  concurrency {args.concurrency} at penalty "
             f"{args.same_model_penalty}" if args.concurrency > 1 else ""))
    derived = "measured" if args.deadline_s is not None else (
        f"{args.slack} x {args.steps} x {full:.5f} s x {args.burst}")
    print(f"deadline = {derived} = {deadline:.2f} s"
          f"{'' if args.externality else '   [externality OFF: floor]'}")
    print(f"  {'split':>8}  {'own':>7}  {'peer':>7}  {'bud':>3}  "
          f"{'rnds':>4}  {'burst':>7}   verdict")
    best = None
    for entry in rows:
        peer = ("      -" if entry["peer_s"] is None
                else f"{entry['peer_s']:7.3f}")
        verdict = "makes it" if entry["burst_s"] <= deadline else "misses"
        # A slice whose width was never measured is priced by the Amdahl
        # branch, which is refuted outright on gfx90a (1.10). Marked, not
        # dropped, so the row is readable and not quietly trusted.
        mark = "" if entry.get("slice_measured", True) else " *"
        print(f"  {entry['split']:>8}  {entry['own_s']:7.3f}  {peer}  "
              f"{entry['budget']:3d}  {entry['rounds_per_request']:4d}  "
              f"{entry['burst_s']:7.2f}   {verdict}{mark}")
        if entry["peer_s"] is not None:
            if best is None or entry["burst_s"] < best["burst_s"]:
                best = entry
    if any(not r.get("slice_measured", True) for r in rows):
        print("\n  * slice width not in the measured curve; priced by the "
              "Amdahl fit,\n    which 1.10 refutes on gfx90a")
    if skipped:
        print(f"\n  skipped, no measured externality: {', '.join(skipped)}")
    margin = (best["burst_s"] - deadline) / deadline
    print(f"\nbest partitioned {best['split']} at {best['burst_s']:.2f} s, "
          f"{margin * 100:+.1f}% against the deadline; "
          f"exclusive {rows[-1]['burst_s']:.2f} s")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
