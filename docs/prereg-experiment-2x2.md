# Pre-registration: do the two named defects explain the loss to priority?

Written 2026-08-27, after Experiment P withdrew the scheduling claim and
before any cell of this one was run.

## What P settled and what it did not

`exclusive_priority` beats `step_matched_pairing` on urgent miss by
+134% under arrivals and +84.5% under backlog, intervals excluding zero
under the seed cluster bootstrap, better in 15 of 20 cells. Under
arrivals it dominates: identical video goodput, identical Jain. Claim 3.6
withdraws the scheduling claim on that evidence.

It withdraws it **for the scheduler that was built**, and that scheduler
has two named defects, neither of which is a property of spatial
partitioning:

1. **The round barrier.** `Runtime.tick` advances every granted request
   by exactly one step per round and a round costs the maximum of its
   steps. Pairing a 0.113 s step beside a 0.521 s one therefore
   rate-limits the fast tenant to the slow one's step rate. Measured on
   one cell: policies that paired ran 2.14 steps/s, the same policies
   when they declined to pair ran 3.84 steps/s -- **a 44% cost, where the
   hardware co-run penalty on that same cell was 3.4%**.
2. **The action space is two grants.** `step_matched_pairing` returns
   `{first: 16, second: 16}` or `{one: 32}`; `deadline_aware` the same
   pair. Meanwhile 1.5 measures five splits, every one of which
   Pareto-dominates rotation, and the most asymmetric gives the largest
   urgent gain: 4+28 is +71.4% urgent against +1.4% video. **The policy
   has never issued the grant its own hardware evidence says is best.**

So "adaptive spatial partitioning loses to priority" is established for a
two-action policy under a lockstep barrier. Whether it is established for
spatial partitioning is exactly what this experiment asks.

## The design

A 2x2, with `exclusive_priority` as the fixed opponent in every cell.

|  | 2 actions (`step_matched_pairing`) | 6 actions (`deadline_quota`) |
| --- | --- | --- |
| **barrier on** (`--max-steps-per-round 1`) | the refuted scheduler; replicates P | action space fixed only |
| **barrier off** (`--max-steps-per-round 8`) | barrier fixed only | both fixed |

`deadline_quota` is implemented in `policies.py` and declared a **method**
in `METHOD_POLICIES`: it gives the latency-critical tenant the *smallest*
quota from `{4, 8, 16, 24, 28}` that still makes its deadline on
predicted costs, everything else to the batch tenant, and the whole die
when no split makes it. Twelve unit tests hold that it reaches all six
grants where the two existing policies reach only `{16, 32}`.

The cap of 8 steps per round does not bind at the measured ratio: the
0.521/0.113 ratio floors to 4.

Conditions, unchanged from A and P so the cells stay commensurable:

    regimes  arrivals, backlog (--video-backlog --drain-grace-s 10)
    load     0.6, 1.05
    burst    4, deadline base burst, slack 1.5, 40 urgent nominal
    seeds    0..9  -- TEN, not five
    envelope off (--drift-tolerance 1000000)

Three arms per group, one process, sharing the drawn co-run state:
`exclusive_priority`, `step_matched_pairing`, `deadline_quota`.
**Eighty groups, 240 cells** -- 10 seeds x 2 barrier settings x 2 regimes
x 2 loads x 3 arms -- about seven hours of card time at the 5-6 minutes
per three-arm group that A3 and P measured.

**Ten seeds, and the reason is a defect found on 2026-08-27.**
`build_trace` seeds on `spec.seed` alone, so one seed at load 0.6 and at
load 1.05 is the same arrival sequence rescaled in time -- verified, the
inter-burst gap ratio is constant to 1e-9. The independent unit is the
seed. Experiment A ran five seeds, its ten "pairs" were five clusters,
and its one positive verdict did not survive the correction. Running five
again would repeat that.

## The analysis, fixed here rather than after

**All intervals are the cluster bootstrap over seeds**, resampling whole
seeds and keeping both loads of a drawn seed together; 10000 resamples,
seed 0. The naive per-cell bootstrap will not be reported as a result.
Paired by (regime, load, seed). Primary metric urgent deadline miss rate,
lower better; secondary video goodput; Jain on tenant die-seconds
reported for every arm because P showed the fairness defence turns on it.

Distribution reported for every comparison whatever the interval does:
wins, losses, exact ties, worst loss, sign test. A3 showed an effect that
is asymmetric in magnitude rather than frequency, and a mean alone hides
that.

## Verdicts, fixed in advance

Let `B` be the barrier-off, six-action cell -- both defects removed.

1. **The defects explain it.** `B` beats `exclusive_priority` on miss with
   the cluster interval excluding zero and video goodput no worse than
   -5% relative. Then spatial partitioning does beat priority once the
   implementation is not in the way, 3.6 is narrowed to the scheduler it
   named, and the paper has a positive systems result.
2. **One defect explains part of it.** `B` does not beat priority, but at
   least one single-fix cell improves on the refuted cell
   (barrier-on, two-action) with the interval excluding zero. Then the
   claim is "the barrier cost X and the action space cost Y, and it is
   still not enough", stated in those words.
3. **Neither defect explains it.** No cell beats priority and no
   single-fix cell improves on the refuted cell with an interval
   excluding zero. Then the loss to priority is a property of spatial
   partitioning on this workload rather than of our implementation, 3.6
   stands unnarrowed, and the measurement/negative-result shape is the
   honest one.

The -5% video bound is the same point-estimate rule A and P used and is
not relaxed if it is what fails.

## Two predictions, recorded so they count

**On the barrier.** Turning it off should raise total steps per second in
the paired cells by something approaching the measured 44%, most visibly
in backlog where the tenants coexist for the whole horizon. If steps/s
does *not* rise, the fix does not do what it was written to do and the
whole barrier-off column means nothing -- that is a validity condition,
not a result.

**On the outcome.** I do not predict verdict 1. `exclusive_priority`
reached 0.1608 at arrivals load 0.6 while every partitioning arm sat
above 0.25, and the gap is 134%. Recovering that from a 44% throughput
defect and a wider action space is a large ask. The honest expectation is
verdict 2. It is written here so that verdict 1, if it lands, is a
surprise on the record rather than a confirmation.

## A wiring check built into the design

`exclusive_priority` grants one request per round, so `_step_budget`
returns 1 for it regardless of `max_steps_per_round` -- the flag cannot
reach it. Its miss rate and goodput must therefore agree between the two
runtime configurations, within the noise of a fresh co-run draw. **If
they do not, something other than the barrier changed between the
columns, and the comparison is void.**

## What would make this experiment invalid

* Any safety failure in any round.
* Fewer than 10 seeds landing at a given (regime, load, runtime config).
* `exclusive_priority` differing between the two runtime configurations
  beyond the co-run draw -- see above.
* Steps per second not rising in the barrier-off column.
* `deadline_quota` issuing a grant outside `{4, 8, 16, 24, 28, 32}` or
  failing to exhaust the die; twelve unit tests and the runner's
  per-round disjointness check both hold this.
* The drawn co-run state is reported per group; if every group draws
  `fast` again the claim is scoped to that state, as A, A3 and P are.
