# Claims and evidence

What this project can currently assert, what it cannot, and what would
overturn each. One line per claim, with the run that supports it and the
scope it holds in.

The decision log is chronological and long; `plan.md` carries five layers
of correction. This file is the flat view. Where the two disagree, this
one is later.

Hardware throughout: one Radeon AI PRO R9700 (`gfx1201`, 32 maskable
units, 34.2 GB), single SKU by decision on 2026-08-09. SDXL at 768x768,
CogVideoX-2b at 480x720x9 frames.

---

## 1. Established

### 1.1 CU-mask partitioning works and is exact

| | |
| --- | --- |
| **Claim** | Masks install as requested, cover the die, stay disjoint under a live scheduler, and reconfiguring one does not disturb work in flight. |
| **Evidence** | Gate A-AMD: 32 mask bits, 192 cells accepted, `rejected` empty, TPC→SM map covering all 32 units without repeats. Mask attestation per round against the manifest. 10,000 action switches with every mask read back, p99 10.1 µs on a busy device, latent byte-identical to an unswitched reference. |
| **Scope** | `hipExtStreamCreateWithCUMask` on gfx1201. |
| **Falsifier** | A readback that differs from the request, or a latent that differs after a mask change. Both are checked every run and fail loudly. |

### 1.2 The scheduled runtime computes what the baseline computes

| | |
| --- | --- |
| **Claim** | Suspending and re-granting a request between denoising steps, at a different mask width each time, produces the same latent as ASLE's uninterrupted loop, byte for byte. |
| **Evidence** | `run_amd_asle_bitexact`: ASLE's `run_urgent` inlined as the reference, same work through the executor with a suspension between every step, SHA-256 of the final latent identical. Per-step trail kept so a divergence would be locatable. |
| **Scope** | Deterministic mode, SDXL, 8 steps, 1024x1024. |
| **Falsifier** | Any hash mismatch. There is no tolerance. |

### 1.3 The co-run penalty is bistable and latches per process

| | |
| --- | --- |
| **Claim** | Two tenants of the same model on disjoint 16+16 masks pay either 1.297x or 1.949x their solo step time. The state is drawn per process, never changes within one, and is identifiable from a single step. |
| **Evidence** | Device-span instrument. Fast 1.287–1.303, slow 1.923–1.962, no overlap. 0 of 8 processes flipped across nine later episodes each, with fresh adapters and re-acquired streams every episode. First step against episode median: mean absolute error 0.00% over 72 episodes. Interposing a different mask pair never disturbed the state (16 of 16 checks). |
| **Scope** | Same-model co-location, 16+16, 768, **gfx1201 (RDNA4) only**. Mismatched pairings show no draw. |
| **Cross-architecture** | **Absent on gfx90a (MI250X, CDNA2, 104 units).** Same instrument, same model, 52+52, 32 processes rotating across four GCDs: 1.2336, sd 0.0030, range 1.228–1.238, none above gfx1201's slow threshold. At the gfx1201 rate of 16.9%, 0 of 32 has probability 0.0027. The four GCDs differ deterministically by 0.6% with non-overlapping ranges, so the instrument resolves far less than a 50% bimodality and sensitivity does not explain the negative. |
| **Falsifier** | A process observed in both states; a third cluster; a state that is not readable within a few steps. |

### 1.4 The draw rate is about one process in six, and unstable

| | |
| --- | --- |
| **Claim** | The slow state occurs in 27 of 160 processes, 16.9%, Clopper-Pearson [11.4%, 23.6%] — but the rate is not stable between campaigns. |
| **Evidence** | Four campaigns: 10/44, 7/20, 3/36, 7/60. Fisher between the largest disagreement gives p = 0.025. |
| **Scope** | Same-model 16+16 on this card. |
| **Not claimed** | Any predictor of the draw. Five candidates have been proposed and retracted: working set, power cap, uncontrollable bistability, launch stagger, prior use of a full-die mask. |

### 1.5 Partitioning mismatched tenants is nearly free, and both gain

| | |
| --- | --- |
| **Claim** | SDXL beside CogVideoX-2b costs 1.00–1.06 per side at every split, and partitioning Pareto-dominates rotation: both tenants advance faster than under an equal share of the whole die. |
| **Evidence** | Five splits, five processes each, three episodes, solo and co-run both by device span; 15 measurements per entry, every range within 0.005. Gains: 4+28 gives +71.4% and +1.4%; 8+24 +65.3% and +8.5%; 16+16 +42.2% and +22.8%; 24+8 +17.7% and +29.5%; 28+4 +3.6% and +30.9%. |
| **Scope** | These two models at these workpoints. The split decides how the gain divides, not whether there is one. |
| **Falsifier** | Any split where a side loses against its rotation share. |

### 1.6 Step-matched pairing carries the method

| | |
| --- | --- |
| **Claim** | On the workloads measured, what separates this scheduler from non-pairing baselines is the step-matched pairing itself. The probe adds nothing once a defect of ours is removed — see 3.4. |
| **Evidence** | 405 cells: pairing beats FCFS by 6.5% (interval touching zero), static partition and EDF by about 3% (intervals excluding it), and FCFS by 17% at overload. Same-model, envelope off: step-matched 0.2276 against probing 0.2286, statistically identical over 12 paired seeds. |
| **Scope** | Both workloads, this card. |
| **Falsifier** | A workload where the probe separates from step-matched pairing with the envelope off. |

### 1.6b Masking beats the primitives that already exist

| | |
| --- | --- |
| **Claim** | Two tenants on two full-die streams, with the hardware arbitrating, are no better than serialising them. CU-mask partitioning beats both. |
| **Evidence** | 12 seeds, mismatched workload, arms alternating per seed with the order flipped on odd seeds. Unmasked against partition: urgent miss +0.0333, [+0.0079, +0.0592], +10.9%, and video goodput also worse, both intervals excluding zero. Unmasked against time-slicing: -0.0121, [-0.0891, +0.0581], spanning zero. |
| **Scope** | SDXL beside CogVideoX-2b, load 0.6, burst 4, this card. |
| **Why it is here** | It is the "why not just use concurrent streams" arm, and it is measured on the workload where partitioning does best rather than where the comparison would flatter. |
| **Falsifier** | A workload where unmasked concurrency matches masked partitioning. |

### 1.7 A wrong prediction meets a right measurement

| | |
| --- | --- |
| **Claim** | Predictor error of ±20% causes no safety failure. |
| **Evidence** | Error injected into the prediction the policy reads and nowhere else; a test asserts the observed side is unchanged at every error. Three safety invariants named in code and checked every round: over-committing the die, charging from the cost model when a measurement existed, charging a measurement taken at another quota. Zero failures across 50 cells at five error levels, and zero again across 84 cells with the envelope both on and off. Fast-state cells only, since the draw confounded the first reading. |
| **Scope** | Same-model, load 0.6, burst 4, n = 4–5 per level. |
| **Falsifier** | Any of the three invariants breaking. |
| **Refuted, not merely withdrawn** | The degradation comparison — 4.6% against 23% — was an artifact of the defective envelope. Sweeping all five error levels inside one process, 10 seeds complete at every level, **over-prediction improves both metrics**: at +20% urgent miss falls 0.2634 → 0.2204, [-0.0812, -0.0086], and video goodput rises [+0.0015, +0.0288]. A 20%-pessimistic cost model beats the calibrated one here. See 3.5. |

### 1.8 The runtime holds its operational clauses

| | |
| --- | --- |
| **Claim** | Scheduler decision p99 under 1 ms; zero weight bytes moved after residency; an hour of concurrent load without leak, deadlock or OOM; Jain 0.99915 on tenant die-time. |
| **Evidence** | Measured 11 µs in the week 7–8 loop and 16.6 µs in the matrix cells. Soak: 714 admitted, 714 completed; the leak threshold is bytes per completed request, not per second, because the old one passed while leaking 1.284 MB per request. |
| **Scope** | This runtime on this card. |
| **Not claimed** | That Jain depends on the accounting currency — see 3.1. |

---

## 2. Negative results

These were pre-registered and failed. They are results, not gaps.

### 2.1 plan.md's primary Pareto claim fails on the full grid

405 cells, nine policies, all of plan.md's grid except load 0.3. Against
the strongest non-oracle baseline, chosen by rule rather than by eye, the
method is **0.56% worse** — [+0.0000, +0.0056] over 45 paired
configurations — where the bar is a 20% improvement.

Against weaker baselines it does win: FCFS by 6.46% though that interval
touches zero, static partition and EDF by about 3% with intervals that
exclude it. At overload (1.05, burst 4) the pairing family beats FCFS by
17%. **Step-matched pairing is what carries this, not the probe.**

Registered before the coverage cells ran and honoured: the overload
result is reported as a fact about overload and does not convert the
frozen-subset failure into a pass.

**How much of the grid could have separated the policies at all.** The
two tenants are both runnable for 26.1% of the horizon at load 0.6,
37.1% at 0.85 and 44.9% at 1.05 -- computed over all 405 cells from the
video tenant's request count, its measured isolated service time and the
cell horizon, not assumed. For the remaining time one tenant holds the
whole die and every partitioning policy issues the same grant, so the
grid tests the split decision on between a quarter and a half of its own
timeline. That does not rescue the claim -- a method that needs a
friendlier workload has to be measured on one and say so -- but it does
bound what this grid can be read as having settled, and it is the most
likely reason the nine policies sit inside 0.34 to 0.39 of each other.
Experiment A (`docs/prereg-experiment-a.md`) measures the same question
with the video tenant backlogged, where the decision is live for the
whole horizon. It has since run, and it closes this hedge rather than
cashing it. Two things came back, and only the first is good news.

With the tenants coexisting for the whole horizon the policies do
separate by a factor of five on miss rate instead of sitting inside 0.34
to 0.39, so the compression really was a property of this grid's
workload. But **the method still does not beat the strongest baseline
there.** By the same declared rule, in all four of Experiment A's
evaluations the strongest baseline is `step_matched_pairing` and
`slo_aware_partitioning` is worse than it by +0.3%, +0.4%, +1.3% and
+1.7%. The friendlier workload was the stated reason to expect a
different answer, it was measured, and the answer is the same one. 2.1
stands, and no longer stands with an outstanding excuse. See 2.3.

### 2.2 The probe does not help, and the grid cannot show it hurts

**Corrected 2026-08-25.** This section previously read "+0.56% worse ...
with the interval excluding zero on the wrong side. Small, and not
noise." The interval does not exclude zero, and it cannot: its lower
bound is pinned at exactly zero by the shape of the data, not by the
size of the effect.

Of the 45 paired configurations, the probe and step-matched pairing
produce **identical miss rates in 42**. Three differ, all in the same
direction: +0.0455, +0.0263, +0.0417. The mean difference is +0.0025
(+0.56% relative), and the bootstrap 95% interval is [+0.0000, +0.0056]
at every resampling seed tried. A resample of 45 configurations draws
none of the three non-zero ones with probability (42/45)^45 = 4.48%,
which is more than 2.5%, so the 2.5th percentile is exactly zero as a
matter of arithmetic. No amount of resampling moves it.

What the grid supports: the probe **does not help**, and its three
non-zero differences all point the wrong way — a sign test on three of
three is p = 0.125 one-sided, which is not significance. What it does
not support: that the probe hurts.

The same pinning applies to the sticky variant: 2 of 45 configurations
differ, P(miss all) = 12.9%.

The mechanism argued from 1.5 still stands as a mechanism -- mismatched
pairings cost 1.02–1.06, so there is nothing for a contention manager to
manage, while a spurious fire still costs a good pairing -- but it is now
an explanation for an absence of benefit rather than for a measured
harm.

### 2.2b The grid resolves three behaviours, not nine

Counting paired configurations where two policies give exactly the same
miss rate, out of 45:

| | vs step-matched pairing |
| --- | --- |
| sticky probing | 43 identical |
| probing | 42 identical |
| slo-aware | 40 identical |
| measured-pairs-only | 29 identical |
| static even | 29 identical |
| deadline-aware | 28 identical |
| exclusive FCFS | 24 identical |
| oracle | 7 identical |

`static_even`, `measured_pairs_only` and `deadline_aware` agree with each
other in 42–43 of 45. The four pairing policies agree with each other in
39–43 of 45. So the nine policies are three distinguishable behaviours
plus an oracle, and any comparison inside a group rests on a handful of
configurations however many cells were run.

This is what makes the comparisons that do survive worth stating:
step-matched pairing against exclusive FCFS differs in 21 configurations
(−6.98%, [−0.0757, −0.0006]), against static-even in 16 (−3.23%,
[−0.0256, −0.0059]), against deadline-aware in 17 and against
measured-pairs-only in 16 (both −3.76%, intervals excluding zero). Those
intervals can exclude zero; the within-family ones structurally cannot.

---

### 2.3 Experiment A: the scheduler beats every fixed split, and clears the pre-registered bar in one of four evaluations

| **Claim** | Choosing the split at run time is worth a large amount when the tenants genuinely coexist. It is not established that it beats a split chosen once, in those words. |
| --- | --- |
| **Evidence** | 400 hardware cells, pre-registered in `docs/prereg-experiment-a.md` before any ran. Four evaluations (2 campaigns x 2 regimes), verdicts 1, 1, **3**, 1. |

A1 ran with this project's drift envelope on (tolerance 0.15), A2 with
it off (1e6) after A1's first group showed the fixed-split sweep was
ranking splits by whether they trip the envelope. Both were run to
completion under the same pre-registration; neither replaces the other.

    A1 arrivals   1. no contribution        miss CI crosses zero
    A1 backlog    1. no contribution        miss -39.4%, video -6.5%
    A2 arrivals   3. SCHEDULING CONTRIBUTION  miss -22.0%, video +6.3%
    A2 backlog    1. no contribution        miss -60.1%, video -5.5%

`step_matched_pairing` is the best adaptive arm in all four by the
pre-registered rule, and it has the lowest miss rate of every non-oracle
arm in all twenty groups. The direction never reverses. In backlog with
the envelope off it is 0.1898 against 0.9909 for the best fixed split at
load 0.6, and the fixed splits do not merely run late: `fixed_split_28`
finishes 2.6 of 40 urgent requests at load 1.05 where
`step_matched_pairing` finishes all 40. An unfinished request counts as
a miss and the denominator is admitted requests, so that is the
measurement rather than a scoring artefact.

**What blocks verdict 3 is video goodput, not miss rate.** Both backlog
rows have miss-rate intervals far from zero and fail on the -5% video
bound at -6.5% and -5.5%. The bound is a point-estimate rule, both point
estimates fail it, and it stands. Both video intervals cross zero
(A2's is [-0.1626, +0.0086]), so the data does not establish the video
loss either -- it establishes that the trade cannot be called free at
ten pairs.

**Three assumptions in the design that the data refutes.** The best
fixed split does not move with load, so `static-oracle` collapses onto
`static-deploy` in three of four and verdict 2 has no target anywhere.
`exclusive_fcfs`, pre-registered as the floor, beats every fixed split
under arrivals at load 0.6 in both campaigns -- static partitioning is
worse than no partitioning in the regime commensurable with the 405-cell
grid. And the simulator's recorded prediction that all five splits would
share one miss rate under arrivals is refuted, with spreads of 0.1850,
0.1062, 0.1283 and 0.0233.

**The weakest joint, stated rather than left to a reviewer.** Against
`exclusive_fcfs` -- not a pre-registered comparison -- the arrivals win
is -13.6% [-0.2086, +0.0173] in A1 and -15.9% [-0.2182, +0.0050] in A2:
it does not exclude zero. The single verdict-3 result was measured
against a comparator set that excludes the arm which performs best in
that regime. Under backlog the same comparison is -56.9% / -58.7% on
miss at a cost of 25% of video goodput.

**The comparison the declared rule actually asks for.** Everything above
compares `step_matched_pairing` against the fixed splits, because that is
what the pre-registration fixed. But `matrix_results.BASELINE_POLICIES`
declares `step_matched_pairing` a **baseline** and the method to be
`slo_aware_partitioning`, `probing_partitioning` and
`sticky_probing_partitioning`. Under that declaration `strongest_baseline`
returns `step_matched_pairing` in all four evaluations, and the method
against it is:

    A1 arrivals   +0.3%  [+0.0000, +0.0050]   9 of 10 identical
    A1 backlog    +0.4%  [-0.0058, +0.0092]   7 of 10 identical
    A2 arrivals   +1.7%  [+0.0000, +0.0217]   8 of 10 identical
    A2 backlog    +1.3%  [+0.0000, +0.0125]   8 of 10 identical

Worse in all four, never better. Of the forty paired configurations
thirty-two are **exactly identical**, and of the eight that differ the
method is worse in seven; a sign test on those eight is p = 0.070. Three
of the four lower bounds are exactly `+0.0000` for the reason 2.2 was
corrected for: with 8 or 9 of 10 differences equal to zero, a bootstrap
resample draws no non-zero difference with probability 0.107 to 0.349,
well above 0.025, so the 2.5th percentile is zero as arithmetic rather
than as an effect size.

This is 2.1 replicated on the workload 2.1 nominated as its own escape
route -- +0.56% there, +0.3% to +1.7% here -- and it is the answer to
"is there a confirmed benefit" for the probe and SLO-aware layer
specifically: no, and now measured twice under conditions chosen to
favour it.

**Coverage.** All twenty groups drew the `fast` co-run state. The base
rate over 1024 recorded cells is 923 fast to 101 slow, so twenty in a
row is unremarkable at p = 0.12; the cost is coverage, not validity.
Experiment A measures the fast state only. Every cell is `safe: true`
with no safety failures, cell durations vary 0.8-1.7% between arms so
the goodput denominator is not doing the work, and the pre-registered
`fixed_split_16 == static_even` in-cell wiring check was vacuous --
`static_even` was not one of the ten arms, so the condition could not
fire, and the identity is covered only by unit test.

---

## 3. Withdrawn

### 3.1 That fairness depends on the accounting currency

Charging wall-seconds or step-count instead of units × seconds moves
neither the miss rate nor Jain. Step-count's miss delta is exactly zero
with an interval of [0, 0] — the schedules were identical, decision for
decision. Sixteen seeds per arm, all restricted to fast-state processes.

The mechanism may exist; **this workload cannot test it**. The policy
reads the tenant charge only in a deficit-rotation tie-break, and at two
tenants with roughly symmetric widths that tie-break almost never changes
a decision. Testing it would need sustained asymmetric widths, and
designing a workload so a difference appears is one step from choosing
the parameterisation that flatters. There is no real trace to appeal to.

Jain stays as an observation. The dependence claim is gone.

### 3.2 That partitioning raises utilisation by 18.6%

The figure came from an externality measured across whole `pipeline(...)`
calls, VAE decode included, applied to a per-step decision. Per step the
same pairing is bistable at 1.297 and 1.949, and the verdict swings from
+11.5% to −25.5% with the draw. Superseded by 1.3, 1.5 and 1.6.

### 3.3 That a mismatched pairing has a two-episode serialised transient

Measured at 6.29x for two episodes and fitted across five splits within
3–9% by `solo_a + solo_b`. The span instrument reads flat to three
decimals in every episode of every split. The fit was real and it was
fitting a variable that held one stale value per episode. See 5.

---

### 3.4 That the dual-ledger probe is worth about 13%

Measured at −13.14% and −14.20% with the drift envelope on. With the
envelope off the probe does nothing: +0.41%, [−0.0211, +0.0256] over 12
paired seeds, the interval spanning zero and the point estimate on the
wrong side.

The envelope is itself a net cost — turning it off improves every policy,
all four intervals excluding zero — and it bought no measurable safety:
zero safety failures with it on (36 cells) and off (48 cells). Its
profile-missing branch never fired at all, because every pairing the
policies formed had a table entry.

So the probe's mechanism was correctly identified — it cuts serial
fallbacks by 73–81% — and the significance was backwards. Those fallbacks
are harmful, and they are harmful because they are ours. The probe is a
workaround for a mechanism this project introduced.

The best configuration measured is the envelope off and no probe.

**Not refuted, untested:** the envelope's safety value. Only its drift
branch has ever fired, and only on workloads where the cost model was
accurate enough that firing was a mistake. A workload containing pairings
the cost model has no entry for would test the other branch. This project
does not currently have one.

### 3.5 That the cost model's accuracy is what makes the scheduler work

Weeks went into calibrating quota curves and externality tables. On this
workload a model biased 20% pessimistic beats the calibrated one on both
metrics — urgent miss 0.2634 → 0.2204 and video goodput up 0.6%, both
intervals excluding zero, 10 seeds paired inside one process.

The planner is robust to ±20% predictor error, which is what plan.md's
clause asks for. It is simply not robust *because* the model is accurate.

## 4. Open

- **What predicts the co-run draw.** Five candidates retracted. The rate
  moves between campaigns at p = 0.025.
- **Why externality-blind reduces serial fallbacks.** It should increase
  them, and a unit test on a faithful pairing shows it doing so. The
  payload now splits fallbacks by cause; twelve seeds per arm are
  running. Criterion stated: drift-caused holds rising under blind with
  profile-caused holds flat confirms the mechanism; both falling refutes
  it.
- **Whether any of this holds on another vendor's mask implementation.**
  See 6.

---

## 5. Methodological note, and the one that cost the most

Every per-step co-run number in this project came from the adapters'
deferred event read, which reports a step's time one step late so a
per-step `synchronize` does not drain the pipeline into the measurement.
That read only lands when the previous step's events have retired. When
the measured side's steps are short it never lands, and the variable
holds **one stale value for a whole episode**.

It produced a two-episode transient with a quantitative account — each
side costing the sum of the two solo steps — that fitted five splits
within 3–9% over a 3x range of the sum. The fit was real. The thing it
fitted was a stale variable.

What caught it was not reasoning. It was running two instruments over the
same steps in the same process and finding one report ten times the other
inside an interval that physically contained it. The containment made the
contradiction unarguable.

The same class of error appears nine times in the decision log: a
property of the measurement presenting itself as a property of the thing
measured. The instruments that now guard against it:

- measure the null before varying parameters;
- state the discriminating criterion, and its direction, before running;
- a single co-run measurement is not evidence;
- the measurement architecture must match the claimed deployment
  architecture;
- byte comparison catches races that timing checks pass, because races
  make steps faster;
- make the nuisance variable identical across arms rather than trusting
  it to balance — seed with position, deadline with arm, and co-run state
  with arm each confounded a campaign before this was routine.

---

## 6. Threats to validity

**Single vendor, two architectures.** The single-SKU decision of
2026-08-09 stood until 2026-08-25, when a second AMD machine made a
cross-architecture check possible: MI250X, gfx90a, CDNA2, 104 units,
against the R9700's gfx1201, RDNA4, 32 units.

What that bought, and what it did not. The masking mechanism and the
partitioning gain both carry over — masks install bit-exact on gfx90a and
partitioning pays +5.6% against rotation there. The **bistability does
not**: 32 processes at 1.2336 with sd 0.0030 and none slow, where
gfx1201 gives two states 50% apart. So 1.3 is now a claim about RDNA4,
measured against a counter-example rather than asserted of one card.

Still open, and it is the same shape as before one architecture became
two: no NVIDIA measurement exists. Whether the SM-mask path behaves like
gfx1201 or like gfx90a is unknown, and two AMD architectures disagreeing
with each other is a reason to expect the question to matter, not a
reason to consider it settled.

On **2026-08-26** the single-SKU decision was extended to Gate B as well,
which until then had still read that Gate B-AMD supplemented rather than
replaced it. So there is now no acceptance clause anywhere that requires
an NVIDIA measurement, and this paragraph is the only place the gap is
recorded. That is the intended destination — cross-SKU consistency is a
piece of evidence, not a box, and more cells on the same card cannot
supply it — but it is worth being explicit that the requirement was
withdrawn rather than met. The narrow reading was kept: `G={1,4,8,16}`,
pinned/pageable, NUMA, PCIe direction and the compute/HBM/PCIe probe
co-runners are in Gate B's text for reasons unrelated to vendor, and they
remain open rather than travelling out with the SKU clause.

**One card, so comparisons are paired in time, not across cards.** The
die warms 43 to 72.8 °C in three and a half minutes and solo step time
rises 5% with it. Arms are interleaved within a process and the isolated
service measurement is shared across a group, because measuring it per
arm handed the first arm a 2.4% tighter deadline.

**Same-model co-location is not plan.md's primary workload.** 1.6 and 1.7
are measured there because the mechanism is inert on the mismatched one.
That is stated wherever those numbers appear.

**Small n on the slow state.** Ten processes. The interval excludes zero;
it is not tight.

**Memory budgets are not yet restated for this card.** 24/20/16 GB was
the 4090's regime; the R9700 has 34.2 GB. The requirement — three budgets
spanning weights-resident, barely-resident and must-evict — is unchanged
and the values are not yet justified.
