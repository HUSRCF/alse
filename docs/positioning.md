# What this project can claim, and the shape of the paper

Written 2026-08-27, after Experiment P withdrew the scheduling claim, the
2x2 tested both named defences and failed them, and the cost model turned
the negative result into a computable boundary. Experiment `expB` was
running against that boundary as it was written.

**Revised 2026-09-03.** `expB` has run and returned verdict 3. Two
derivations this document was built on have since been withdrawn -- the
"only feasible partition at 4.7 s" and the unit-seconds boundary -- and
both are corrected in place below rather than quietly dropped. The second
SKU came back and produced a result the first cannot produce alone. What
survives is a stronger paper than the one this document first described,
and it is a different one.

## The one-sentence problem

The original thesis was "an adaptive SLO-aware spatial partitioning
scheduler for diffusion co-serving". **Five** pre-registered campaigns say
that scheduler loses to five lines of strict priority, by 83% to 524% on
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

~~With the barrier off and a pipelining cap of 16, `24+8` runs 12 steps
per round and finishes the burst in 4.7 s -- the only feasible
partition.~~

**Withdrawn 2026-09-03; the correction is the better result.** That
sentence modelled the burst as one 32-step request. It is four requests
of eight steps, the registry offers **one request per tenant per round**,
and a request with eight steps cannot use a per-round budget larger than
eight. So the burst is served serially whatever the split, `24+8`
finishes in **6.30 s**, and **no split meets the deadline at any cap** --
raising the cap cannot help, because `24+8` already grants twelve to a
request that has eight. Our cap was not what prevented the split that
could have worked; there was no such split. See 3.8, and
`scripts/burst_feasibility.py`, which computes the table and is pinned to
the published rows by a test.

This is a stronger claim than the one it replaces. "We set a parameter
badly" is an erratum; "the runtime's request model makes the mechanism
infeasible for this SLO" is a structural finding with a named
precondition for when it would not apply.

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

~~The boundary follows in closed form. Over one burst period `T`,
priority gives the batch tenant `32 x (T - 3.71)` unit-seconds and a
persistent 24+8 partition gives `8 x T`, so partitioning wins when
`T < 4.95 s`.~~

**Withdrawn 2026-09-03, by the error the thesis itself names.** That
derivation compared **unit-seconds** -- die share times time -- which is
not throughput any more than it is a deadline. Comparing steps per
second instead: priority gives the batch tenant `(1 - rho_u) / 0.515` and
`24+8` gives `1 / 1.574`, so partitioning wins on batch throughput when
**`rho_u > 0.673`**, about offered load 1.35. Neither load tested reaches
it. The corrected model tracks the measurements it applies to --
predicted 1.36 against measured 1.341 for priority at load 0.6.

That this document made the same error it was written to name is worth
keeping in the paper rather than editing out. It is the third instance:
`deadline_quota` charged a deadline in die-seconds, this boundary charged
throughput in unit-seconds, and `pipelined_quota` tested a per-burst
deadline against one request's cost. **When a policy or a derivation
reasons about a deadline, check what unit the deadline is on.**

This still reconciles our result with the literature, in the corrected
currency. Work reporting gains from GPU spatial partitioning operates at
looser deadlines or on throughput-shaped SLOs; we measured a
deadline-shaped SLO on a runtime that serves a tenant's burst serially,
and called it a scheduling failure for two months.

## Contribution order

Revised 2026-09-03. The old (5) -- "the boundary, crossed on hardware,
measured on both sides" -- is gone: `expB` returned verdict 3, the
boundary was never crossed, and the derivation that placed it was wrong.
What replaces it is better, because it is measured on two architectures
rather than derived on one.

1. **The mechanism, measured, on two architectures.** Masks install
   exactly and read back on `gfx1201` and on `gfx90a`; mismatched tenants
   pay 1.00-1.06 per side; all five splits Pareto-dominate rotation. The
   hardware offers a real gain and it is not one vendor's accident.
2. **The phenomenon.** Process-latched bistable co-run penalty on RDNA4
   -- 1.297x or 1.949x, no overlap, identifiable from one step, 0 of 8
   processes flipping across nine episodes -- and **absent on gfx90a**,
   where 32 processes give 1.2336 with sd 0.0030. Measured against a
   counter-example rather than asserted of one card.
3. **The gain has an optimum width, and where it sits is the
   architecture's.** On gfx1201 aggregate throughput rises monotonically
   in the number of equal ways and saturates; on gfx90a it **peaks at
   four ways and collapses at eight**, where CogVideoX-2b falls below not
   partitioning at all. The Amdahl form the cost model and Gate B's
   prediction clause both assume holds on RDNA4 and is **refuted** on
   CDNA2 -- the fitted serial term is negative, and the unit-seconds a
   step consumes fall from 13 to 26 units where the form requires them to
   rise. Held-out MAPE 24.9% and 28.1% against 5.6% and 4.6%, where the
   gate's bar is 10%. See 1.10.
4. **The negative result.** Despite (1), spatial partitioning loses to
   strict priority across five pre-registered campaigns, and three
   defences of our own method were named and falsified: the round
   barrier, a two-grant action space, and the pipelining cap.
5. **The structural explanation, on both architectures.** The registry
   offers one request per tenant per round, so a burst is served
   serially and only width shortens it. No split meets the deadline:
   gfx1201's best is 13.6% over, gfx90a's is **1.2% over**. The
   *fraction* travels -- three quarters of the die on both -- and the
   margin does not. This is the intellectual core, and (3) is why the
   margin differs: a scheduler cannot carry a split across these two
   devices, because their curves have different shapes and not merely
   different constants.

## What gets cut

The probe, the sticky probe, the SLO-aware layer, the dual ledger and the
drift envelope are measured-null or measured-harmful: 26 of 30
configurations identical to the ablation, a probe whose benefit was
avoiding an envelope that is itself a net cost. They become a short
"what we removed and why" section, not contributions. A method section
that promises five mechanisms and then explains away four of them reads
worse than a paper that never promised them.

## The branch that was taken

`expB` returned **verdict 3**: `fixed_split_24`, the persistent partition
the derivation was about, is dominated on both axes at both loads in both
regimes, every interval excluding zero. So the paper is the second
branch -- a **measurement study on two architectures plus a rigorous
negative result with a structural explanation** -- strengthened by
having named and falsified three defences of our own method, and by the
explanation being arithmetic on measured costs rather than a story.

The remaining open path is **intra-tenant concurrency**: dividing the
critical tenant's quota among its own requests on disjoint masks, which
is the only thing that can shorten a serial burst. `expC` is running
against it. Its arithmetic couples the scheduling result to (2) for the
first time -- it survives the fast co-run state at a 6% margin and misses
in the slow one -- and the number it turns on, the same-model penalty
with **three** peers rather than one, had never been measured.

**It has been now, and it did not survive at the size it was written
at.** On the solo curves with a pairwise stand-in, the whole die split
four ways within the priority tenant finished the burst in 2.79 s against
3.70 s serial on gfx1201 and 3.12 s against 3.83 s on gfx90a. With the
N-way penalty measured -- contribution (4), claim 1.11 -- gfx90a's best
is **two** ways at 3.55 s, **7.3%** over serial rather than 18.5%, and
four ways is 3.70 s. Eight ways needs a burst of eight to be reachable at
all, and there it costs **14.30 s** against 7.66 s for not splitting.

So the shape of the result holds and its size does not: between tenants,
partitioning loses to priority; *within* the priority tenant it helps,
by single digits rather than by tens of percent. **And where the optimum
sits is not what a pairwise model predicts** -- which is the contribution
rather than the disappointment. The model everyone uses is pairwise, and
the error it makes grows with the number of slices, which is exactly the
regime a partitioning scheduler operates in.

gfx1201's N-way penalty is still unmeasured and is queued behind `expC`.
If it behaves like gfx90a's, `prereg-intra-tenant.md`'s predicted 5.01 s
grows and `concurrent_quota_c4` misses the deadline rather than meeting
it with a 6% margin. The pre-registered verdicts do not move; the
prediction the pre-registration itself declined to back does.

The title's second half -- "a scheduler that measures can ride it" -- has
no evidence and goes. So does "the boundary where it wins": there is no
such boundary in the region measured, and saying so is the finding.
Something closer to:

> **Die-Seconds Are Not Deadlines: Why GPU Spatial Partitioning Loses to
> Priority for Deadline-Bound Bursts, on Two Architectures**

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
* **The analysis was not in the repository.** The seed cluster bootstrap,
  the win/loss counts and the sign tests were computed once per campaign,
  by hand, outside version control. Committing them regenerated every
  published number exactly -- and found two defects on the way, one of
  which silently halved any comparison pooled across the two regimes.
  Nothing published used one; nothing would have said so if it had.
* **The cost model's functional form was a vendor assumption.** Amdahl
  fits gfx1201 within Gate B's 10% and fails gfx90a at 25-28%, with a
  negative serial term. Every scheduling number in the project was
  produced by a policy reading a curve whose *shape* does not travel.
