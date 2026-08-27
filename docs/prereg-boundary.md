# Pre-registration: where the boundary between partitioning and priority is

Written 2026-08-27, after the 2x2 and after finding that its pipelining
cap was set from the wrong ratio, and before any cell of this one was run.

## Why there is a boundary at all

Four campaigns say spatial partitioning loses to strict priority. The
cost model says why, and the same arithmetic says where it would stop
losing.

A round is paced by its slowest member. With the barrier on, a 32-step
burst takes 17.7 s at 4+28, 25.8 s at 16+16 and 50.4 s at 24+8 against a
**5.34 s** deadline, while 32+0 takes 3.71 s. **No split can meet the
deadline while the barrier is on.** Every campaign this project ran before
today was under that barrier, so 2.1, 2.3 and 3.6 measured a mechanism
that was arithmetically incapable of winning.

With the barrier off a request runs `floor(peer_step / own_step)` steps
per round, capped. Then:

    split   own    peer    budget   rounds   burst      verdict
    4+28   0.521  0.552       1       32     17.7 s     misses
    8+24   0.269  0.608       2       16      9.7 s     misses
    16+16  0.158  0.805       5        7      5.6 s     misses
    24+8   0.125  1.574      12        3      4.7 s     **makes it**
    28+4   0.121  3.116      16*       2      6.2 s     misses

    * capped at 16; the raw ratio is 25.8

**24+8 is the only feasible partition, and the 2x2 ran at a cap of 8**,
which gives it 8 steps per round and 6.3 s -- outside the deadline. The
experiment's cap is what prevented the one split that could have worked.

## The boundary, derived before measuring it

Over one burst period `T`, priority gives the batch tenant
`32 x (T - 3.71)` unit-seconds and a persistent 24+8 partition gives it
`8 x T`. Partitioning wins on batch throughput when

    8T > 32(T - 3.71)   <=>   T < 4.95 s

Measured inter-burst gaps on the traces that ran: **median 6.79 s at load
0.6** and about **3.9 s at load 1.05**.

**Prediction: partitioning loses at load 0.6 and wins at load 1.05.** The
boundary sits between the two loads this project has been measuring all
along, which is the proposed explanation for four consistent negative
results at a workload that happened to sit on the wrong side of it.

## Arms

    exclusive_priority     the opponent, unchanged from P and the 2x2
    pipelined_quota        smallest quota feasible UNDER PIPELINING; cap 16
    fixed_split_24         24+8 always, chosen once -- does the choosing matter?
    step_matched_pairing   continuity with P and the 2x2

`pipelined_quota` is new. `deadline_quota` asked
`remaining x own_step <= left`, which ignores the peer and therefore
called 16+16 feasible at 5.34 s when it needs 25.8 s. Ten unit tests hold
the corrected rule, including one that pins the cap error: at cap 8 the
same deadline sends it back to the whole die.

`fixed_split_24` is carried because if a split chosen once matches the
adaptive one, the result is "partition at 24+8" and the scheduler is
again decoration -- the same finding Experiment A was built for, and it
must be able to land here too.

## Conditions

    regimes  arrivals, backlog
    load     0.6, 1.05          -- the two sides of the predicted boundary
    burst    4, deadline base burst, slack 1.5, 40 urgent nominal
    seeds    0..9
    runtime  --max-steps-per-round 16, --drift-tolerance 1000000

The cap and the policy's `cap` must be the same number. A policy
reasoning about a budget the runtime will not grant is predicting someone
else's scheduler; 16 in both.

Four arms per group, one process. **Forty groups, 160 cells**, about five
hours.

Grants are now recorded per cell -- `ledger.grant_shapes` and
`ledger.urgent_units_histogram`. The 2x2 could not tell whether its
six-action policy ever issued an asymmetric grant, because nothing
recorded them and that was the whole question.

## Metrics and verdicts, fixed in advance

All intervals are the **seed cluster bootstrap**, 10000 resamples, seed 0,
resampling whole seeds and keeping both loads of a drawn seed together.

The criterion is **plan.md's own global success threshold**, second
branch, because the first is about rescuing a miss rate and this
experiment is about buying batch throughput at an unchanged miss rate:

> urgent SLO miss rate 相对下降至少 20%，video goodput 下降不超过 5%；
> **或 相同 SLO miss rate 下 video goodput 提升至少 10%**

Evaluated per load, against `exclusive_priority`:

1. **The boundary is real.** At load 1.05 `pipelined_quota` meets the bar
   -- miss no worse with the interval crossing zero or better, and video
   goodput at least +10% with the interval excluding zero -- and at load
   0.6 it does not. Then the derivation holds, 3.6 is narrowed to the
   region `T > 4.95 s`, and the paper's claim is a boundary rather than a
   verdict.
2. **Partitioning wins at both loads.** The bar is met at 0.6 as well.
   Stronger than predicted and the derivation is wrong about where the
   boundary sits; both are reported, and the derivation is corrected in
   public rather than quietly.
3. **Partitioning wins at neither.** Then the corrected policy and the
   raised cap did not rescue it, 3.6 stands unconditional, and the
   negative-result shape is the honest one for the paper.

## What would make this experiment invalid

* Any safety failure in any round.
* Fewer than 10 seeds landing at a given (regime, load).
* **`pipelined_quota` never issuing 24+8.** `grant_shapes` records it. If
  the policy does not reach the split the derivation is about, the
  experiment tests something else and says so.
* The drawn co-run state is reported per group; if every group draws
  `fast` again the claim is scoped to that state, as A, A3, P and the 2x2
  are.

## What is predicted, and what is not

Predicted: loss at 0.6, win at 1.05, on the second branch of the bar.

Not predicted: the miss rate. At 24+8 the burst finishes in 4.7 s against
5.34 s, a 12% margin on a predicted cost, and the co-run penalty on this
pair is 1.00-1.06. That margin is thin enough that a miss-rate regression
would not surprise me, and if it comes the second branch of the bar is
not met and verdict 3 follows. Written down so that a thin margin is not
described afterwards as an expected one.
