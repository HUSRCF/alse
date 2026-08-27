# What this project can claim, and the shape of the paper

Written 2026-08-27, after Experiment P withdrew the scheduling claim, the
2x2 tested both named defences and failed them, and the cost model turned
the negative result into a computable boundary. Experiment `expB` is
running against that boundary as this is written; nothing here depends on
its outcome and the two branches are both stated.

## The one-sentence problem

The original thesis was "an adaptive SLO-aware spatial partitioning
scheduler for diffusion co-serving". Four pre-registered campaigns say
that scheduler loses to five lines of strict priority, by 83% to 434% on
deadline miss, with intervals excluding zero under the seed cluster
bootstrap. That is not a result to bury and it is not a paper on its own.

## Why the negative result is not the end of it

**Every campaign before 2026-08-27 measured a mechanism that could not
have won.** A round is paced by its slowest member, so under the round
barrier a 32-step burst takes 17.7 s at 4+28, 25.8 s at 16+16, 50.4 s at
24+8 and 99.7 s at 28+4 -- against a 5.34 s deadline, where 32+0 takes
3.71 s. No split can meet the deadline. That is arithmetic on the
measured cost model, not a scheduling judgement, and it reframes 2.1, 2.3
and 3.6 as measurements of a mechanism with no feasible operating point.

With the barrier off and a pipelining cap of 16, `24+8` runs 12 steps per
round and finishes the burst in **4.7 s** -- the only feasible partition.
The 2x2 ran at a cap of 8, which gives it 6.3 s and misses. Our own cap
is what prevented the one split that could have worked, and that is
recorded as our error rather than as a limitation.

## The thesis the evidence actually supports

> **Die-seconds are not deadlines.** Spatial partitioning buys
> *throughput*, measured in die-seconds; a deadline is paid in *width*
> for a burst served serially. So partitioning helps exactly when the
> critical tenant can be squeezed into a narrow permanent slice, and
> loses when its deadline forces it to take nearly the whole die anyway
> -- at which point you may as well give it all of the die and get out of
> the way.

The evidence for the first half is direct and was measured before the
theory: `deadline_quota` holds **more** die-seconds than priority
(45.0% against 40.8%), completes the same number of requests (41.5
against 41.6), and misses **0.99 against 0.12**.

The boundary follows in closed form. Over one burst period `T`, priority
gives the batch tenant `32 x (T - 3.71)` unit-seconds and a persistent
24+8 partition gives `8 x T`, so partitioning wins when `T < 4.95 s`.
Measured inter-burst gaps are 6.79 s at load 0.6 and about 3.9 s at 1.05.
**The boundary sits between the two loads this project has been measuring
all along**, which is the proposed explanation for four consistent
negative results.

This also reconciles our result with the literature. Work reporting gains
from GPU spatial partitioning operates at looser deadlines or on
throughput-shaped SLOs -- the region where `T < 32S/q*`. We measured the
other side of the same boundary and called it a failure for two months.

## Contribution order

1. **The mechanism, measured.** Masks install exactly and read back on
   `gfx1201`; mismatched tenants pay 1.00-1.06 per side; all five splits
   Pareto-dominate rotation. The hardware offers a real gain.
2. **The phenomenon.** Process-latched bistable co-run penalty on RDNA4
   -- 1.297x or 1.949x, no overlap, identifiable from one step, 0 of 8
   processes flipping across nine episodes -- and **absent on gfx90a**,
   where 32 processes give 1.2336 with sd 0.0030.
3. **The negative result.** Despite (1), spatial partitioning loses to
   strict priority across four pre-registered campaigns, and the two
   obvious defences -- the round barrier and a two-grant action space --
   were tested and are not the explanation.
4. **The reconciliation.** (1) and (3) are both true because they are
   measured in different currencies. This is the intellectual core.
5. **The boundary.** Derived in closed form, crossed on hardware,
   measured on both sides.

## What gets cut

The probe, the sticky probe, the SLO-aware layer, the dual ledger and the
drift envelope are measured-null or measured-harmful: 26 of 30
configurations identical to the ablation, a probe whose benefit was
avoiding an envelope that is itself a net cost. They become a short
"what we removed and why" section, not contributions. A method section
that promises five mechanisms and then explains away four of them reads
worse than a paper that never promised them.

## The two branches

If `expB` confirms the boundary -- loss at 0.6, win at 1.05 on plan.md's
own second branch -- the paper is a **conditional positive**: spatial
partitioning beats priority inside a region we can compute, and here is
the region. A conditional result with a stated precondition is harder to
overturn than an unconditional win.

If it does not, 3.6 is unconditional and the paper is a **measurement
study plus a rigorous negative result**, strengthened by having named and
falsified four separate defences of our own method: the barrier, the
action space, the pipelining cap, and the feasibility test. That is a
much stronger negative than "we tried and it did not work".

Either way the title's second half -- "a scheduler that measures can ride
it" -- has no evidence and goes. Something closer to:

> **Die-Seconds Are Not Deadlines: When GPU Spatial Partitioning Loses to
> Priority, and the Boundary Where It Wins**

## Method notes that belong in the paper, not in a footnote

Four traps this project fell into and documented, each with the
measurement that exposed it:

* **The floor was not a floor.** `exclusive_fcfs` was read as "no
  partitioning" for the project's whole life; the registry rotates every
  round, so it is whole-die time-slicing. Strict priority had never been
  run.
* **The runtime's synchronisation cost ten times the hardware's
  contention.** 44% against 3.4% on the same cell. A scheduler
  experiment can measure its own barrier and call it a property of the
  mechanism.
* **The paired units were not independent.** `build_trace` seeds on the
  seed alone, so the two loads of one seed are the same arrival sequence
  rescaled in time -- verified, the gap ratio is constant to 1e-9. Half
  the effective n, and Experiment A's only positive verdict did not
  survive the correction.
* **Policies collapse.** 26 of 30 configurations identical between the
  method and its ablation; a handful of cells decide the sign, and the
  sign flipped between campaigns.
