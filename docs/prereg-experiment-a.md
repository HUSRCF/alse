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

    load   0.6, 1.05
    burst  4
    seed   0, 1, 2, 3, 4

The two ends of the main grid's own load axis (0.6, 0.85, 1.05), so the
cells are commensurable with it. Two loads, because the whole case for
choosing a split at run time is that the best split moves with the
offered load. If the best fixed split is the same at 0.6 as at 1.05 --
where the offered load exceeds capacity and a wrong split should hurt
most -- a deployer picks it once and there is nothing left to choose.

## Two workload regimes

The 405-cell grid's trace has both tenants runnable for only part of the
horizon: 26.1% at load 0.6, 37.1% at 0.85, 44.9% at 1.05, measured
across all 405 cells rather than assumed. For the rest of the time one
tenant holds the whole die and every partitioning policy behaves
identically. A question about *choosing* a split cannot be settled on a
timeline that is three-quarters no-op -- it divides every effect by four
before asking whether it is significant, and it is the most likely
reason the nine policies in the grid sit within 0.34 to 0.39 of each
other.

So the experiment runs two regimes:

* **arrivals** -- the grid's own trace, unchanged. Commensurable with
  the 405 cells, and the weaker test of the question.
* **backlog** -- the video tenant has a standing queue and is never
  idle, so the split decision is live for the whole horizon. This is
  also the setting spatial partitioning exists for: latency-critical
  work colocated with throughput work that always has more to do.

Backlog cells stop 10 s after the last urgent arrival rather than the
default 120 s. Goodput is steps per second of actual run, and a tenant
that never drains would otherwise spend half the cell running solo,
diluting the contention the regime was built to create.

The verdicts below are evaluated in each regime separately. If they
disagree, that disagreement is the result, and it is reported as "the
scheduler's value depends on how often the tenants actually coexist"
rather than by choosing the regime that flatters us.

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

## The simulator is not a prediction of this

`trace_sim.simulate` was run over these arms before the hardware
campaign, to check the `fixed_split_16` == `static_even` identity
end to end. It passed, byte for byte, in all 50 cells. Its miss rates
should not be read as a forecast:

    simulator, load 0.6, all five fixed splits   0.9018
    hardware, load 0.6, static_even (75 cells)   0.3658

The simulator was given service times of 0.87 s and 2.6 s, which the
cell runner measures on the card instead of assuming, so the gap is
partly an input error. But a factor of 2.5 on the primary metric is
worth stating rather than quietly dropping: it is a fresh instance of
withdrawal 3.5, where a more accurate cost model did not produce better
decisions. Nothing in this pre-registration depends on a simulated
number, and no simulated cell will be reported as a result.

One simulated observation is worth checking against the hardware,
because it would be a finding either way: all five fixed splits had
*identical* urgent miss rates, to four decimals, in every seed, while
their video goodput ranged over a factor of two. If that holds on the
card it means the split cannot trade urgent latency for video
throughput at all in this workload -- the urgent tenant makes its
deadline when the video tenant is idle and misses when it is not,
whatever slice it holds. That would settle verdict 1 by a different
route than expected, and it is recorded here so it counts as a
prediction rather than a rationalisation.

The prediction is about the **arrivals** regime only. The simulator ran
before the backlog regime existed, on the grid's own trace, so the
arrivals cells are the ones that can confirm or refute it. The first
backlog cells already separate the splits -- 0.2500 for `fixed_split_4`
against 0.1750 for `fixed_split_8` at load 0.6, seed 0 -- and that is
not evidence against a prediction made about a different workload.
