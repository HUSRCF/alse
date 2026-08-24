# Pre-registration: does the scheduler beat a split chosen once?

Written 2026-08-24, before any cell of this experiment was run. The
criterion, the direction, and the three possible verdicts are fixed here
so that whichever one lands is reported as the result rather than
selected after seeing the numbers. Five claims in this project have
already been withdrawn; three of them were withdrawn because the
criterion was chosen after the measurement.

## The question

Every partitioning policy in `policies.py` chooses a split at run time.
The grid shows spatial partitioning is worth a great deal: on the
mismatched workload both tenants gain at every split, and the largest
entry is +71.4% urgent against +1.4% video. None of that establishes
that *choosing* the split is worth anything. If a split fixed at
deployment time matches the adaptive policies, the contribution belongs
to the partitioning mechanism and the scheduler is decoration.

This is the first question a reviewer asks, and we do not know the
answer.

## Arms

Ten policies per cell, all in one process so they share the drawn
co-run state and the thermal history.

Static (`fixed_split_N`, urgent tenant gets N of 32 units):

    fixed_split_4  fixed_split_8  fixed_split_16  fixed_split_24  fixed_split_28

`fixed_split_16` is `static_even` by construction and is tested for that
identity, so it doubles as a wiring check inside every cell.

Adaptive:

    deadline_aware  step_matched_pairing  slo_aware_partitioning

References:

    exclusive_fcfs              floor: no partitioning at all
    oracle_shortest_remaining   ceiling: knows the remaining work

`probing_partitioning` and `sticky_probing_partitioning` are excluded.
Their measured benefit was avoiding our own drift envelope, which is
itself a net cost; running them here would add two arms whose result is
already known and would cost an hour a cell.

## Conditions

The mismatched primary workload, unchanged from the main grid so the
cells are commensurable with the 405 already measured: urgent = SDXL,
8 steps; video = CogVideoX-2b, 30 steps; deadline base `burst`, slack
1.5; 40 urgent requests per cell.

    load   0.6, 0.9
    burst  4
    seed   0, 1, 2, 3, 4

Two loads, because the whole case for choosing a split at run time is
that the best split moves with the offered load. If the best fixed split
is the same at 0.6 and 0.9, a deployer picks it once and there is
nothing left to choose.

## Metrics and direction

Primary: urgent deadline miss rate, lower is better.
Secondary: video goodput in steps per second, higher is better.
Paired by (load, burst, seed); 95% CI by bootstrap over the paired
differences, 10000 resamples, seed 0.

Two static comparators, both computed by rule:

* **static-deploy** — the fixed split with the best mean miss rate over
  all cells. One number a deployer could actually have picked, and what
  `strongest_baseline` now returns.
* **static-oracle** — per (load, burst), the best fixed split for that
  configuration. Not deployable; it is the upper bound on what any
  static choice could achieve, and the adaptive policies have to beat it
  for "choosing at run time" to mean anything.

## Verdicts, fixed in advance

Let `M` be the best adaptive policy by mean miss rate, chosen by the same
rule among the three adaptive arms.

1. **No scheduling contribution.** `M` is not better than static-deploy:
   the paired CI on miss rate crosses zero, or is positive. Then the
   claim is "partition the die", the paper is about the measurement, and
   every adaptive policy is reported as a negative result alongside the
   five already withdrawn.

2. **Auto-tuning only.** `M` beats static-deploy with the CI excluding
   zero, but does not beat static-oracle. Then the scheduler's value is
   that it finds the right split without the deployer knowing the
   workload -- a real but modest claim, and it must be stated in exactly
   those words, not as "beats static partitioning".

3. **Scheduling contribution.** `M` beats static-oracle with the CI
   excluding zero and video goodput no worse than -5% relative. Only
   this verdict supports a system contribution.

Video goodput is checked in all three: a miss-rate win bought by
starving the video tenant is not a win, and the -5% bound is the same
one the main Pareto criterion used.

## What would make this experiment invalid

* `fixed_split_16` disagreeing with `static_even` in any cell. It is an
  identity, so a disagreement means the policy was not wired to the
  runtime that ran it.
* Any safety failure (non-disjoint masks, stale quota, mask readback
  mismatch) in any round -- these abort the cell in the runner already.
* Fewer than 5 seeds landing at a given (load, burst); the pairing is
  per configuration and an unpaired cell cannot enter the bootstrap.
* Both loads drawing the same co-run state in every seed, which would
  make the two conditions one condition wearing two labels. The drawn
  state is recorded per cell and will be reported.
