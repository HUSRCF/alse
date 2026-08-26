# Pre-registration: does spatial partitioning beat simply prioritising?

Written 2026-08-26, after Experiment A completed and while A3 was still
running, before any cell of this one was run.

## The defect this exists to repair

Every comparator set in this project -- the 405-cell grid, Experiment A,
Experiment A3 -- treats `exclusive_fcfs` as "no partitioning at all", the
floor. It is not the floor a practitioner would build. `ready()` returns
one candidate per tenant in `self._order`, and `Runtime.tick` calls
`registry.rotate()` at the end of every round, so `states[0]` alternates
between tenants. `exclusive_fcfs` gives the whole die to whichever tenant
the rotation is pointing at, which is **whole-die time-slicing between
tenants**, not first-come-first-served and not priority. Its own
docstring says "the time-slicing baseline"; the name says otherwise, and
the name is what the comparator sets were reasoned about.

So the obvious heuristic for a latency-critical tenant beside a batch one
-- *give the urgent tenant the whole die whenever it has work* -- has
never been measured, in any regime, in any campaign. `deadline_aware` is
not it: it defaults to `static_even` and goes exclusive only for a
request that misses while sharing and makes it while exclusive, one at a
time, so it cannot see that a burst of four needs the die for 3.6 s.

This is not a small omission. It is the baseline the scheduling claim has
to survive, and its absence is the most likely reason every margin in
this project shrank each time a simpler comparator was added.

## The arm

`exclusive_priority`, implemented in `policies.py` and registered in
`BASELINE_POLICIES`: whole die to the runnable request that carries the
earliest deadline; if none carries a deadline, whole die to the first
runnable request. No partitioning, ever. Latency-critical is read as
"carries a deadline" rather than as the tenant name `urgent`, so the
policy is not wired to this workload's labels.

## The prediction, with its mechanism, recorded before the run

The urgent deadline is `1.5 x 0.905 x 4 = 5.43 s` from the burst's
arrival, and every member of a burst carries that same absolute
deadline. The runtime offers one request per tenant per round, so a burst
of four is served serially whatever the policy. At the full 32 units that
is `4 x 0.905 = 3.62 s`, which **fits inside 5.43 s with 1.81 s to
spare**. Under rotation the urgent tenant gets roughly every other round,
which doubles it to about 7.2 s and the last two members of a burst
cannot make it.

Measured over the ten arrivals configurations, bursts arrive with a
median gap of 2.67 to 16.10 s against a 3.62 s serial burst, and the
number of bursts arriving before the previous one could have finished is
1 to 7 of 6 to 15 per cell at load 0.6, and 2 to 10 at load 1.05 -- so
queueing between bursts is a minority effect at 0.6 and closer to half at
1.05.

**Prediction: `exclusive_priority` gives an urgent miss rate far below
every arm measured so far in the arrivals regime -- below 0.15 at load
0.6, against 0.4097 for `step_matched_pairing` and 0.2002 for the
oracle.** If that lands, the scheduling contribution claimed for spatial
partitioning in that regime is a claim about a comparator set that
omitted the obvious policy.

This is recorded as a prediction so it counts as one either way. This
project's record on predictions is bad in an instructive direction: the
simulator predicted 0.9018 where the hardware gave 0.3658, and 1.7's
degradation comparison was refuted by its own re-run. A mechanism that
adds up on paper has been wrong here before.

What the prediction does **not** cover: video goodput. Under priority the
batch tenant runs at full width whenever the urgent tenant is idle rather
than at half width continuously, and which of those yields more steps per
second of cell is not obvious from the arithmetic. No direction is
predicted for it.

## Design

Two regimes, as in Experiment A, because the answer may differ and that
difference would be the result:

* **arrivals** -- the grid's own trace.
* **backlog** -- the video tenant never idles.

Envelope off (`--drift-tolerance 1000000`), the configuration A2 used and
this project's best known one.

    load   0.6, 1.05
    burst  4
    seeds  0, 1, 2, 3, 4

Four arms in one process per group so they share the drawn co-run state
and the thermal history:

    exclusive_priority       the arm this experiment is about
    exclusive_fcfs           what the project has been calling the floor
    step_matched_pairing     the strongest baseline in all four of A's
                             evaluations, by the declared rule
    slo_aware_partitioning   the declared METHOD

Twenty groups, eighty cells. Seeds 0-4 are reused deliberately: this is a
comparison *within* each group, run in one process, so it does not pool
with A's cells and does not need to.

## Metrics and verdicts, fixed in advance

Primary: urgent deadline miss rate, `step_matched_pairing` minus
`exclusive_priority`, paired by (load, seed), 95% bootstrap CI over 10
pairs per regime, 10000 resamples, seed 0. Secondary: video goodput.

1. **Partitioning contributes beyond priority.** The interval excludes
   zero on the negative side and video goodput is no worse than -5%
   relative. Only this supports "spatial partitioning is worth something
   that prioritising is not".
2. **Not established.** The interval crosses zero. Then the scheduling
   claim for that regime reduces to "no better than prioritising the
   urgent tenant", and it must be stated in those words.
3. **Prioritising is better.** The interval excludes zero on the positive
   side. Then the spatial-partitioning *scheduling* claim is withdrawn
   for that regime and reported as a negative result alongside the five
   already withdrawn. The same comparison is then run for the declared
   METHOD and reported too.

The `-5%` video bound is the same point-estimate rule A used and is not
relaxed if it is what fails.

## What this does not touch

Claims 1.5 and 1.6b are not in scope and are not threatened by any
outcome here. 1.5 is that partitioning two mismatched tenants costs
1.00-1.06 per side and both gain against rotation; 1.6b is that masked
partitioning beats two unmasked full-die streams with intervals excluding
zero. Both are statements about the *mechanism's* throughput, measured
without a deadline metric. A priority policy that wins on miss rate says
nothing about either.

What it would threaten is the claim that *choosing a split at run time*
is worth anything on the SLO metric -- which is the claim Experiment A
was built to test and which one of A's four evaluations supported.

## The one legitimate defence, named in advance so it is not invented later

Strict priority has no fairness bound at all: it starves the batch tenant
for as long as the urgent tenant has work. Claim 1.8 reports Jain 0.99915
on tenant die-time for this runtime, and if the contract the paper
commits to includes a fairness bound then the comparison has to be stated
with it.

That defence is only worth anything if it is stated as a *different
claim* -- "as good on SLO and bounded on fairness" -- rather than used to
avoid reporting the SLO comparison. It is written here, before the
numbers, so that if it is reached for afterwards it can be checked
against what was anticipated.

## What would make this experiment invalid

* Any safety failure in any round.
* Fewer than 5 seeds landing at a given (regime, load).
* `exclusive_priority` granting more than one request in any round, or
  granting less than the full die. Nine unit tests hold this and the
  runtime records every grant.
* The drawn co-run state is reported per group; if all groups draw one
  state, the claim is scoped to it, as A's is.
