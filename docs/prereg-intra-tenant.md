# Pre-registration: does intra-tenant concurrency make partitioning viable?

Written 2026-09-03, after expB returned verdict 3 and 3.8 was derived,
before any cell of this one was run.

## The one path 3.8 leaves open

3.8 is not a verdict on a scheduler. The registry offers **one request
per tenant per round**, every request of a burst carries **one absolute
deadline**, so a burst of four is served serially whatever the split. On
the measured cost model with pipelining at cap 16:

    4+28   burst 17.66 s      24+8   burst  6.30 s
    8+24   burst  9.73 s      28+4   burst 12.47 s
    16+16  burst  6.44 s      32+0   burst  3.70 s

Against a 5.34 s deadline **no split meets it, and no cap can change
that** -- `24+8` already grants a budget of twelve to a request with
eight steps. Widening a slice cannot shorten a serial burst. Only
stopping the serialisation can.

So every campaign that measured partitioning losing -- 2.1, 2.3, 3.6,
3.7, 3.8 -- measured a runtime rule, not the mechanism. This experiment
changes the rule.

## The arm

`concurrent_quota_c4`: the critical tenant's 24 units divided among four
of its own requests, six units each, on four disjoint masks; the batch
tenant keeps 8. `concurrent_quota_c2` is the same at two requests of
twelve units. Both are `POLICY_FACTORIES` entries and are **methods**.

The runtime gains `requests_per_tenant`, defaulting to 1, and the
registry's `ready` gains `per_tenant`, defaulting to 1. At the defaults
every prior campaign's behaviour is unchanged; 1072 tests hold that.

Each in-flight slot needs its own adapter, because two executors sharing
one clobber its `_step_index` -- the adapter's own docstring records the
symptom, a hang at 3% GPU with both threads alive. The adapters share the
pipeline, so the extra cost is a scheduler built `from_config` and one
set of prompt embeds, **not a second copy of the weights**. That is the
same construction `--share-weights` already validated for Gate B-AMD's
co-run evidence.

## The prediction, with its arithmetic, and why it is not confident

Burst completion at `24+8`, charging the **same-model co-run penalty** to
the intra-tenant pair because that is what 1.3 measures, and 1.06 to the
mismatched pair:

    penalty        c=1      c=2      c=4
    none (ideal)  6.67 s   3.34 s   5.01 s
    fast  1.297   6.67 s   6.67 s   5.01 s
    slow  1.949   6.67 s   6.67 s   6.67 s

**Only `24+8` at concurrency 4 survives the fast state, at 5.01 s against
5.34 s -- a 6% margin. In the slow state it misses.**

So the prediction is: `concurrent_quota_c4` meets the deadline in
fast-state cells and misses in slow-state ones, and `c2` does neither.
**1.3's bistability becomes the thing that decides whether the scheduling
result holds** -- which is the first time the phenomenon and the
scheduler have been coupled by anything but narrative.

A 6% margin on a predicted cost is thin. 3.5 established that a more
accurate cost model did not produce better decisions, and three separate
derivations in this project have now been wrong about what a deadline is
paid in. I do not predict verdict 1.

## Design

Conditions unchanged from P, the 2x2 and expB so the cells stay
commensurable: mismatched workload, burst 4, deadline base `burst`,
slack 1.5, 40 urgent nominal, envelope off, cap 16.

    regimes  arrivals, backlog
    loads    0.6, 1.05
    seeds    0..9   -- ten, the independent unit being the seed
    arms     exclusive_priority, concurrent_quota_c4,
             concurrent_quota_c2, fixed_split_24

Forty groups, 160 cells. `fixed_split_24` is carried as the `c=1` control:
it is the same 24+8 split with the serialisation left in, so the
difference between it and `c4` is intra-tenant concurrency and nothing
else.

All intervals are the **seed cluster bootstrap**, 10000 resamples,
seed 0, resampling whole seeds and keeping both loads together.

## Verdicts, fixed in advance

Against `exclusive_priority`, per regime, on plan.md's global bar:

1. **Intra-tenant concurrency makes partitioning viable.** `c4` reaches a
   miss rate no worse than priority's (interval crossing zero or better)
   **and** video goodput at least +10% with the interval excluding zero.
   Then 3.6, 3.7 and 3.8 are narrowed to the serial-burst runtime, and
   the paper has a positive systems result with a named precondition.
2. **It helps but not enough.** `c4` beats `fixed_split_24` on miss with
   the interval excluding zero, but does not reach priority. Then the
   serialisation is quantified as a cost and the claim is stated in those
   words.
3. **It does not help.** `c4` does not beat `fixed_split_24`. Then the
   last open path is closed, 3.6 stands unconditional, and the
   negative-result shape is the honest one.

## Reported whatever the verdict

* The drawn co-run state **per cell**, and **the verdicts recomputed on
  fast-state cells alone**, because the prediction says the state decides
  it. Declared here so a fast-only split cannot be chosen after seeing
  the pooled number.

  Per cell, not per group, and that is a real weakening of the design.
  `requests_per_tenant` is a runtime setting, so one process cannot hold
  two values of it and the four arms cannot share a process the way A, P,
  the 2x2 and expB's arms did. Each arm therefore draws its own co-run
  state and pays its own model load and isolated measurement. A state
  difference between arms is a confound this design cannot remove, only
  report. It is recorded here rather than discovered later.
* `grant_shapes` and the urgent-quota histogram, so "did it ever issue
  the grant" is counted rather than assumed -- the check that caught
  expB.
* Wins, losses, exact ties, worst loss, sign test.

## What would make this experiment invalid

* Any safety failure, including a non-disjoint mask across the four
  intra-tenant slices. The runtime's per-round check covers it and the
  masks are constructed disjoint rather than assumed.
* `concurrent_quota_c4` never granting four requests at once --
  `grant_shapes` records it.
* Memory: four SDXL adapters plus CogVideoX must fit 34.2 GB. They share
  the pipeline's weights, so the expectation is that they do; if a cell
  OOMs the arm is reported as infeasible on this card rather than as a
  loss.
* Fewer than 10 seeds at a given (regime, load).
