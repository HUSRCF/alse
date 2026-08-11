# Gate C decision log

plan.md freezes the algorithm, formulas and action order at the end of
the Gate C stage: "算法、公式和 action 顺序在本阶段结束后冻结；之后修改
必须写 decision log."

The freeze is machine-checked by `scripts/freeze_gate_c_algorithm.py`
against `experiments/manifests/gate_c_algorithm_freeze.json`. Any change
to a frozen function, table or action order fails that check until an
entry is added below and the manifest is regenerated with `--write`.

## What is frozen

**Action order** — the three branches of `slo_aware_partitioning`, in
this order and no other:

1. Deadline override: the whole die to a request that misses if it
   shares and makes it if it does not.
2. Matched pairing: an even split when the two cheapest predicted step
   times are within 1.6x.
3. Deficit rotation: the whole die to the tenant furthest behind on
   quota-seconds.

**Functions** — the policy and every function its decisions and its
accounting are computed from: the seven policies, `simulate`,
`QuotaCostModel.step_seconds`, `jain_index`, `accounting_error`,
`peak_service_lag_quanta`, `canonical_bytes`, and `edf_whole_die`.

**Tables** — `MEASURED_MODELS`, `MEASURED_QUOTA_SECONDS` and
`MEASURED_EXTERNALITY`. These are measurements, not algorithm, but the
decisions read them, so a silent edit changes the scheduler as surely as
a changed branch.

## How it is checked, and why it takes two locks

The **structural** lock hashes each function's AST with docstrings
stripped. Hashing source text would fire on a reflowed comment, and a
check that cries wolf on prose trains people to re-freeze without
reading. Hashing the AST fires on a changed constant, branch order or
comparison — which is what "algorithm, formula and action order" names.

The **behavioural** lock is the canonical digest of three fixed traces,
one per branch of the action order. It catches what the structural lock
cannot: a change in a function the freeze does not list, in a default
resolved elsewhere, or in the measured tables.

Neither is sufficient. Changing the pairing tolerance from 1.6 to 1.7
breaks the structural lock and leaves all three behavioural digests
identical, because the only two models measured so far have step-time
ratios of 1.0 and 3.4 and both thresholds fall between them. A third
model with a ratio near 1.6 would make the behavioural lock sensitive to
it; until one exists, the structural lock is the only thing standing
between that constant and a silent edit.

## Entries

### 2026-08-06 — initial freeze

Frozen at commit `ac75629`, after Gate C criteria C1–C6 pass:

| Criterion | Evidence |
| --- | --- |
| Byte-identical replay under one seed | `canonical_bytes`, verified across three `PYTHONHASHSEED` values in separate processes |
| Canonical service accounting < 1% | 0.0000% on matched, mismatched and deadline traces |
| Backlogged Jain ≥ 0.98 | 1.000 on the matched backlogged trace |
| Service lag ≤ 2 quanta absent overrides | 0.00 matched, 1.03 mismatched, with `deadline_override_rounds == 0` confirmed by the simulator rather than self-reported |
| No avoidable miss on a feasible trace | 0, feasibility witnessed by preemptive EDF at full width |
| Safe degradation at ±5/10/20% predictor error | accounting < 1% and lag ≤ 2 at every error level; utilisation within 5% of exact |

Criterion 7 is this document.

The composition was not the first design. Two earlier policies each held
half the gate: `deadline_aware` met every avoidable deadline and lost
30% of the die on tenants whose step times differ by 3.4x, and
`step_matched_pairing` kept throughput on both matched and mismatched
traffic and missed a deadline it could have met. The frozen order
composes them; reversing it — pairing before checking deadlines — loses
the rescue, because a rescued request needs the whole die and pairing
has already given half of it away.

Known limits recorded at freeze time, not defects:

- Two models measured (SDXL, CogVideoX-2b). The 1.6 tolerance sits
  between their ratio of 3.4 and self-pairing at 1.0; it is not fitted
  to anything in between, because nothing in between has been measured.
- Concurrency is capped at two tenants, which is what the externality
  table covers. Measured penalties span 1.22x to 1.93x, so a third
  tenant's cost would be invented rather than read.
- The cold-model term is deferred to the runtime stage; these results
  assume a resident model.

### 2026-08-06 — pairing must be across tenants (post-freeze change #1)

**Changed:** `policies.step_matched_pairing`. It ranked every runnable
request by predicted step time and paired the two cheapest. It now takes
one candidate per tenant — that tenant's own cheapest request — and pairs
across tenants, ordered by quota-second deficit first.

**Why:** the old form could starve a tenant outright, and did so at every
predictor error level Gate C requires. With an exact predictor, equal
SDXL costs tie-break on request id, which alternates tenants, so the pair
was always cross-tenant and the defect was invisible. Under ±5% error two
of one tenant's requests can both predict cheapest, hold the die between
them, and leave the other tenant unserved for the entire run: peak
service lag 22.37 quanta against a bound of 2.

Throughput was **unchanged** at 1.0881 and accounting error stayed at
0.0000%, so neither the utilisation result nor the accounting check could
have detected it. Jain index over quota-seconds would have — this is the
case that motivated seeding unserved tenants at zero rather than letting
them be absent from the dict.

**How it was found:** not by the tests, which passed. `verify_gate_c.py`
scored C6 as FAIL because it evaluates lag on both the matched and the
mismatched trace, while the C6 test only evaluated the mismatched one.
The verifier being stricter than the test is the reason to write it
separately rather than have it call the test suite.

A second signature made the diagnosis quick: the reported lag was
*identical* at ±5%, ±10% and ±20%. Noise that changed a scheduling
outcome would not produce three identical numbers. It was a reordering,
and one seeded rng scaled by three different magnitudes gives the same
ordering every time.

**Correction to the initial freeze entry above:** C6 was recorded as
passing on 2026-08-06 on the strength of the test suite. That judgement
was incomplete — the criterion held on the mismatched trace and failed on
the matched one. The entry is left as written; this paragraph is the
correction.

**Regression:** `C6_SafeDegradationUnderPredictorError.
test_predictor_error_cannot_starve_a_tenant` checks lag, Jain and
non-zero service per tenant across three error levels and three predictor
seeds. Verified to fail on the old algorithm (9 subtests) and pass on the
new one.

**Freeze re-issued** after this entry. Only the structural hash of
`step_matched_pairing` changed. `slo_aware_partitioning` itself is
untouched, and — the part worth recording — **all three behavioural
digests were byte-identical before and after a change that turned total
starvation of a tenant into correct service.**

That is a hole in the behavioural lock, not a curiosity. All three frozen
traces ran with an exact predictor, and with an exact predictor the old
and new algorithms are genuinely identical: equal costs tie-break on
request id either way. The lock covered only the regime in which the
defect is invisible.

Fixed in the same commit by adding a fourth behavioural trace — matched
tenants under a ±5% predictor at seed 11 — which is the configuration
that separates them. Confirmed to differ between the two algorithms
rather than assumed to.

The lesson generalises past this entry: a behavioural lock is a claim
about the traces it runs, and traces chosen to demonstrate the intended
behaviour will systematically miss defects that only appear off the
intended path.

### 2026-08-06 — the SDXL cost table was wrong at every quota (post-freeze change #2)

**Changed:** `MEASURED_QUOTA_SECONDS["sdxl"]`, `MEASURED_MODELS["sdxl"]`
(both `step_seconds_at_full` and `serial_fraction`),
`MEASURED_EXTERNALITY[(16,16)]`, the default slack in
`feasible_deadline_trace`, and the load parameters of the predictor-
degradation scenario. `toy_bench` carried the same constant and is
updated with it.

**What was wrong.** `step_seconds_at_full` was 0.1521 s, commented
"1024x1024, 32 units". Gate B never measured SDXL at 1024 — every SDXL
figure in it is 768x768 — and 0.1521 is not the full-die per-step cost at
either resolution. It is closest to 768 at **16** units (0.1551), a
half-die number recorded as the full-die one. Everything else in the
curve was derived from it by scaling with *call* p50 ratios, and a call
is denoising steps plus a VAE decode whose cost does not scale with quota
the way the steps do, so those ratios are diluted: 1.503 at 16/32 where
the per-step measurement says 1.363.

Against a directly measured per-step curve the shipped table was high at
every quota: +47.2 / +50.2 / +47.2 / +45.2 / +42.2 / +41.6 / +34.2 /
+31.7 percent at 4 through 32 units. Not a constant offset, so not merely
the wrong scale — the shape was wrong too, and in the direction that
overstates how much a tenant loses by taking half the die.

**Evidence.** `step_ratio_sdxl_768_perstep_curve_20260806.json`, all
eight quotas, cv under 0.3%. Two independent cross-checks, because a
correction this large has to be the measurement's own claim:

| workpoint | transition probe (08-03) | this run (08-06) | apart |
| --- | --- | --- | --- |
| 768, 8 units | 263.3 ms | 268.75 ms | 2.1% |
| 768, 16 units | 155.1 ms | 157.50 ms | 1.5% |
| 768, 32 units | 112.4 ms | 115.52 ms | 2.8% |

And a third, from a different script again: the co-run driver measured
solo per-step at 16 units as 0.15748 s against the curve's 0.15750 s.

Gate B's own commit message states SDXL's step as "112.4 ms". The
measurements were right the whole time; the error was introduced when the
simulator's table was assembled from them.

**`serial_fraction` 0.391 → 0.4419.** The old value was fitted to call
latencies, where the decode contributes a serial term the steps do not
have. Carried onto the per-step curve it put the 15-unit extrapolation
*below* the measured 12-unit cost — a curve that is not monotone in
quota. Refitted with `fit_quota_latency` against the per-step points.

**`(16,16)` externality 1.223 → 1.234.** The old entry was a call-level
penalty measured at 512x512. The new one is per-step at 768x768, the mean
of the two sides (+22.4% and +24.5%). The dilution turned out to be small
— 1 to 3 percentage points — so this is the least consequential of the
corrections, but it was measured rather than assumed.

**Effect: the claim gets stronger, not weaker.** Partitioning's
utilisation on matched tenants goes from 1.0881 to 1.1888, a gain of
18.9% over the full die where the table said 8.8%. Both errors ran in the
same direction and neither was self-serving: a half-die step that is
cheaper than believed makes partitioning look better. The simulator had
been understating its own result. Lag stays 0.00 and Jain stays 1.000.

**Scenario retuning, and why it is not threshold-lowering.** Cheaper
steps de-congested two scenarios that were calibrated against the old
table:

- `feasible_deadline_trace` slack 1.35 → 1.10. At 1.35 the scheduler met
  every deadline *without ever invoking its override*, so the trace no
  longer exercised the behaviour Gate C's C4 tests. 1.10 is tighter, not
  looser: the override fires, the scheduler still misses nothing, and the
  non-intervening policy misses two.
- Predictor-degradation load 0.30 → 0.45 req/s. At 0.30 every policy met
  every deadline, which would have reported perfect robustness for a
  scheduler that has none. 0.45 is the *smallest* rate that tells
  informed from blind scheduling apart, i.e. the tightest available
  scenario; `test_the_scenario_is_sensitive_enough_to_tell_policies_apart`
  is what holds it to that.

**A defect this uncovered, recorded rather than fixed.** Around 0.60
req/s the deadline-aware policy misses *more* than the policy that never
intervenes — 35 against 26 — and it does so with an exact predictor, so
it is not a degradation problem. Giving one request the whole die delays
everything queued behind it, and the policy decides that trade one
request at a time without pricing the delay it imposes on the rest.
Pinned as `test_a_load_band_exists_where_intervening_is_worse`, so a fix
has to make that test fail rather than the defect persisting unexamined.
The retuned scenario sits at 0.45 because that is the smallest
discriminating load, not to avoid this band.

**Limits still standing:**

- CogVideoX-2b's curve is still call p50s. Its per-step data has only
  three points (8/16/32, from the transition probe) and against those the
  call ratios are accurate to 2.5%, unlike SDXL where the same check
  fails at 10.2%. Left as call-level deliberately, and marked as such in
  the source, rather than half-rebuilt.
- Externality entries other than (16,16) are still call-level at 512x512.
  The scheduler reads only (16,16) — it either splits evenly or gives the
  whole die to one tenant — so the others are used solely by the currency
  counterexample, whose argument is about non-linearity rather than
  absolute values.
- One externality table serves both models because CogVideoX-2b cannot
  be co-run on this card at all: one process peaks at 28.54 GB and two
  need 57 on 34.2 GB. Applying SDXL's penalties to it is an
  extrapolation across models.

**Freeze re-issued.** All three table hashes and all four behavioural
digests changed, as they should for a change of this size — unlike
post-freeze change #1, where the behavioural lock was blind and had to be
extended.

### 2026-08-06 — CogVideoX-2b rebuilt the same way (post-freeze change #3)

**Changed:** `MEASURED_QUOTA_SECONDS["cogvideox-2b"]` and
`MEASURED_MODELS["cogvideox-2b"]` (`step_seconds_at_full` 0.5171 →
0.51549, `serial_fraction` 0.276 → 0.2051). `toy_bench` follows.

The previous entry left this model on call p50s deliberately, because its
per-step data had only three points and the call ratios were accurate to
2.5% against them. The full curve has now been measured, so the reason to
leave it no longer holds and the two models are the same quantity for the
first time.

**Measured** at 9 frames, all eight quotas: 3116.34 / 1574.11 / 1059.00 /
805.19 / 687.64 / 608.12 / 551.75 / 515.49 ms, cv under 0.35%. Against
the transition probe from three days earlier: 0.21%, 0.18% and 0.11%
apart at 8, 16 and 32 units — closer agreement than SDXL managed, on a
model whose steps are 4.5x longer.

**The error was real but small**, unlike SDXL's: the call table ran 6.5%
low at 4 units, tapering to 0.3% high at 32. Its full-die constant was
right to 0.3% — this model's problem was only the dilution of the call
ratios, not a misfiled constant. That is why the same check that failed
at 10.2% on SDXL passed at 2.5% here, and why the correction moves
nothing that matters: matched-tenant utilisation stays 1.1888, mismatched
stays 1.0000, lag stays within bounds.

**A labelling defect found on the way.** The step-ratio driver named
every curve by `--sizes`, but video pipelines run at their native
resolution and ignore height/width — the cell already recorded null for
those fields. A CogVideoX curve would have been filed as "768x768" using
a value that had no effect on it. Fixed for future runs; this curve's
top-level key still reads "768" because it predates the fix, and the test
that guards it checks the cells (which record `frames: 9`, `height:
null`) rather than the key written over them.

**Cost tables are now bound to their probes.** `test_cost_table_
provenance.py` asserts every entry equals the run that produced it, and
checks the full-die constant against the 32-unit row specifically while
asserting it equals no *other* quota — which is the shape of the mistake
that was shipped, not merely the value it produced. None of today's
corrections would have survived a day with that test in place.

**Freeze re-issued.**

### 2026-08-06 — the pairing rule tests the wrong quantity (finding, not yet a change)

The frozen rule shares the die when two tenants' step times are within
1.6x. Four measured co-runs say that ratio does not decide the question.

| workpoint pair | step ratio | paired | rotating | pairing gain | rule pairs? |
| --- | --- | --- | --- | --- | --- |
| 768 vs 768 | 1.000 | 198.1 ms | 231.0 ms | **+16.6%** | yes ✓ |
| 1024 vs 1024 | 1.000 | 531.1 ms | 401.0 ms | **−24.5%** | yes ✗ |
| 1024 vs 1152 | 1.333 | 665.5 ms | 460.5 ms | −30.8% | yes ✗ |
| 1024 vs 1280 | 1.693 | 796.1 ms | 533.3 ms | −33.0% | no ✓ |

The first two rows are the control. Identical step times, identical
ratio of 1.000, opposite verdicts. Whatever decides this, it is not the
ratio the rule reads.

What moves instead is the externality: +22.4%/+24.5% per step at 768
against +71.2%/+81.1% at 1024. Both tenants hold the same 16 units in
both cases; the larger working set is what turns sharing from a 16.6%
win into a 24.5% loss. The rule's ratio test is a proxy for a quantity
that behaves differently from it.

**Where this leaves Gate C.** The simulation is internally consistent —
its cost table and its externality entry were both measured at 768, and
the +16.6% measured there is close to the +18.9% it computes. What
cannot be claimed is that the result carries to other workpoints. It
does not, and this is the measurement that shows it.

**Why the rule is not changed in this entry.** The correct test compares
makespans, `max(t_a(16)·e_a, t_b(16)·e_b)` against `t_a(32) + t_b(32)`,
which needs a predicted externality per workpoint. The table has exactly
one entry, at 768. Replacing a wrong proxy with a rule that requires
data the project does not have would trade a visible error for a hidden
one. The measurements come first.

Recorded now so the limit travels with the claim:

- Gate C's partitioning result holds at the workpoint it was measured
  at. It is not a statement about SDXL, and not about spatial
  partitioning in general.
- The 1.6 tolerance has no measurement supporting it as a threshold. It
  happens to give the right answer on the 1.693 pair and the wrong one
  on the other three.

**Follow-up: the transition is a step, not a slope.** Three more co-runs
at identical resolutions bracket it:

| workpoint | per-step externality | pairing gain |
| --- | --- | --- |
| 768 | +22.4% / +24.5% | **+16.6%** |
| 832 | +76.6% / +78.9% | −21.9% |
| 896 | +77.6% / +81.2% | −22.7% |
| 1024 | +71.2% / +81.1% | −24.5% |

Externality roughly triples between 768 and 832 and is then flat to
1024. Whatever changes, it changes over a 64-pixel step and not
gradually, which is the shape of a capacity threshold rather than of
growing contention.

The obvious candidate does not survive arithmetic. The card's L2 is
8192 KB, and SDXL's first UNet block at 320 channels in fp16 holds about
5.9 MB at 768, 6.9 MB at 832 and 10.5 MB at 1024 — so the jump happens
while a single tenant's first-block activation still fits, and the
two-tenant total already exceeded L2 at 768 where sharing was
profitable. Recorded as an unexplained step with the cache size noted,
not as a cache explanation: the numbers rule out the simple version of
that story, and no measurement here distinguishes the alternatives.

### 2026-08-06 — the workpoint threshold was a thermal artifact (retraction)

The two entries above report that pairing pays at 768 and loses at every
larger workpoint, that the transition is a 16-pixel step, and that the
frozen rule therefore tests the wrong quantity. **The first two of those
are wrong.** The measurements were confounded by the GPU's power and
thermal state, not by the workpoint.

768x768, 16+16 units, three independent runs of the same configuration:

| run | solo/step | co-run/step | externality |
| --- | --- | --- | --- |
| original (first co-run of the day, cold) | 157.5 ms | 192.7 ms | **+22.4%** |
| replicate (after hours of continuous runs) | 157.1 ms | 282.8 ms | **+80.1%** |
| replicate after cooling to 45 C | 159.0 ms | 192.7 ms | **+21.2%** |

Solo cost is identical across all three to within 1%. The two cold
co-runs agree to the millisecond. Only the hot one differs, and it
differs by 47%.

Thermal telemetry sampled during the cooled run explains it: junction
temperature reaches 90 C and the shader clock falls from 2567 to 2350
MHz while board power sits at 299-301 W against a 300 W cap. One tenant
does not reach the cap; two do. The penalty attributed to a larger
working set is the power limit throttling both tenants at once.

**What this invalidates.** Every co-run measured after the first — 784,
800, 816, 832, 896, 1024, and the two mismatched pairs — ran on a hot
card, in sequence, with no cooling between them. Their externality
figures are not comparable to the 768 measurement they were compared
against, and the "16-pixel step" was the boundary between the first run
of the day and everything after it. Those runs are kept as raw evidence
and marked; they are not deleted, and they are not usable as workpoint
comparisons.

**What survives.** The externality entry the simulator uses, 1.234 at
16+16 for 768, is confirmed rather than undermined: two cold runs agree
on it. Gate C's +18.9% rests on that entry and stands. What does not
survive is the claim that the result fails to carry to other
workpoints — that was measured against contaminated comparisons and is
now unknown, not false.

**What it opens.** The power cap is a real constraint on spatial
partitioning and a more interesting one than the working-set story it
replaces. Two tenants at 16 units each reach 300 W where one at 32 does
not, so the die's compute is available but its power budget is not. That
is a scheduling variable the design does not currently model.

**Method change, effective now.** Co-run measurements are comparable
only at a controlled thermal starting point. Every co-run used for a
workpoint comparison must record its starting junction temperature and
its power/clock trace, and runs at different thermal states must not be
compared. The 768 cooled run and its `thermal_768_cooled_20260806.log`
are the first to meet that standard.

This is the fourth time in this project that a property of the
measurement has been read as a property of the thing measured, and the
first time it reversed a conclusion I had already written into plan.md.

### 2026-08-06 — the co-run measurement is bistable (retraction of the thermal explanation, and a threat to the claim)

The entry above blames the power cap. That is also wrong, and the
correction matters more than either of the two it replaces.

Five repeats of one configuration — SDXL 768x768, 16+16 units, each from
a controlled thermal start at 41-47 C:

| run | start | peak | peak power | co-run/step | per-step externality |
| --- | --- | --- | --- | --- | --- |
| 1 | 41 C | 89 C | 320 W | 281.6 ms | **+77.6% / +80.0%** |
| 2 | 47 C | 90 C | 312 W | 192.0 ms | **+20.6% / +21.8%** |
| 3 | 47 C | 90 C | 309 W | 193.6 ms | **+22.2% / +21.8%** |
| 4 | 47 C | 89 C | 351 W | 280.1 ms | **+76.4% / +82.3%** |
| 5 | 47 C | 90 C | 318 W | 282.5 ms | **+77.5% / +80.0%** |

Two states, nothing between them. Within each state the runs agree to
about 1% and the per-sample cv is 0.2-0.9% in the low state. Thermal
telemetry does not separate them: every run peaks at 89-90 C, draws
309-351 W, and spans the same clock range. Nor does the CU mask —
requested, readback, popcount and multiProcessorCount are identical in
all five.

The high state's 1.77x is close to the 2.0x that complete serialisation
would give, which points at kernel concurrency degrading rather than at
contention increasing. What decides it is not yet known.

**Consequence for the claim, stated plainly.** Rerunning Gate C's
matched-tenant scenario with each state's externality:

- low state (2 of 5 runs): partitioning **+20.8%** over the full die
- high state (3 of 5 runs): partitioning **−17.8%**

The result the project reports depends on which state the die happens to
be in, and the majority of observed runs are in the state where spatial
partitioning loses. `MEASURED_EXTERNALITY[(16,16)] = 1.234` is the low
state — it is one of two modes, not a central value, and it is the
minority mode.

**What this does not change.** The cost tables are per-tenant solo
measurements and are unaffected: solo per-step is 1.957-1.992 s across
all five runs. The simulator's logic, the accounting, the lag bound and
the freeze are all untouched. Gate C's criteria are about the scheduler
given a cost model; what is now in question is the cost model's
externality entry and, through it, whether the utilisation claim holds
at all.

**Status of the claim: open.** Not withdrawn — the low state is real and
reproducible, and if the state proves controllable the claim stands with
a condition attached. Not established either. Determining what selects
the state is now the project's blocking question, ahead of any further
scheduler work.

**Earlier retractions in this thread, for the record.** The "workpoint
threshold" and the "power cap" explanations were both artifacts of
comparing single runs drawn from a bimodal distribution: the 768-cold
run happened to land low and everything it was compared against happened
to land high. Five repeats would have shown that at any point. Single
co-run measurements are not evidence and are not to be used as such.

### 2026-08-06 — the state is fixed at launch, and the low state is the minority

Six more repeats with widened telemetry, plus per-sample time series
from the earlier five.

**The state does not develop; it is there from the first sample.**

| run | first samples (ms/step) | middle | last |
| --- | --- | --- | --- |
| rep2 (low) | 200 200 200 200 200 | 192 192 191 191 192 | 192 192 192 192 192 |
| rep5 (high) | 312 313 313 312 313 | 272 270 268 266 265 | 284 283 282 281 225 |
| diag6 (high) | 312 315 322 304 292 | 290 290 290 292 291 | 291 290 288 291 291 |

A run is in one state or the other before its first measured step. This
rules out the explanation that a longer co-run window catches more fast
samples, which the window lengths had suggested (the two low runs used
90 s and the earlier low ones 150 s, while all six 60 s runs went high).

**Widened telemetry does not separate them either.** Memory clock
(96-1258 MHz), fabric clock (219 MHz fixed), SoC clock (417-1280 MHz)
and GPU utilisation (3-100%) are identical across all six high-state
runs, and no low-state run has this telemetry because none has occurred
since it was added.

**The distribution so far: 9 high, 2 low, out of 11.** Both low runs
came from the middle of one sequence. Nothing in the recorded conditions
distinguishes them from their neighbours.

**Consequence, at the current state of knowledge.** Weighting by
observed frequency rather than picking a mode, the expected externality
is about 1.68, and partitioning loses. The claim of a utilisation gain
requires the low state, and the low state is neither the majority nor
currently reproducible on demand.

What remains true: the low state is real, occurred twice, and both times
was internally stable to 0.2-0.9% cv. Something selects it at launch.
The search has narrowed to what differs between two processes' launches
-- queue assignment, allocation placement, or launch timing -- and away
from thermal state, power, clocks and the CU mask, all of which are now
measured and none of which separate the states.

### 2026-08-06 — the state is selected at launch, and one stagger reaches it reliably

The bistability responds to when the second process is launched.

| launch stagger | runs | state | per-step externality |
| --- | --- | --- | --- |
| 0 s | 3 | high, 3/3 | +77.8% / +75.3% / +73.7% |
| **5 s** | **3** | **low, 3/3** | **+22.8% / +23.5% / +23.6%** |
| 15 s | 3 | high, 3/3 | +83.4% / +72.9% / +75.9% |

The low-state runs are also the most internally stable measurements
taken so far: per-sample cv of 0.19%, 0.24% and 0.20%.

Against a base rate of 2 low in 11 unstaggered runs, three consecutive
low results at one stagger is not chance -- roughly 0.6% if the state
were being drawn independently. The stagger selects it.

**It is a window, not a point, and not monotone.** The finer scan:

| stagger | runs | state | externality | per-sample cv |
| --- | --- | --- | --- | --- |
| 0 s | 3 | high | +73.7 to +77.8% | 1.6-2.4% |
| 2 s | 2 | **low** | +24.3, +24.8% | 0.21-0.25% |
| 3 s | 2 | **low** | +24.8, +24.8% | 0.23-0.25% |
| 5 s | 5 | **low** | +22.8 to +24.9% | 0.19-0.25% |
| 8 s | 2 | high | +75.3, +79.8% | 2.2-2.7% |
| 10 s | 2 | high | +75.1, +75.3% | 2.4-2.9% |
| 15 s | 3 | high | +72.9 to +83.4% | 1.9-2.6% |

Nine of nine runs in 2-5 s land low; eleven of eleven outside it land
high. The low state is also an order of magnitude steadier per sample.

**What this most likely is.** The stagger separates process *launches*,
which in this harness means each co-run reloads SDXL from disk. Two to
five seconds is far shorter than that load, so the loads still overlap
heavily either way; what changes is which of the second process's
initialisation steps coincides with which of the first's. That points at
a resource claimed once during HIP context or allocator setup, not at
anything about running the model.

If so, the effect belongs to cold co-run measurement rather than to
steady-state serving: a runtime that keeps models resident does not
re-initialise a context per tenant switch, and would sit in the low
state permanently. That is testable -- two streams in one process with
disjoint masks, which is the Gate A arrangement -- and untested here, so
it stays a hypothesis.

What is not hypothetical: the measurement harness must stagger launches
into 2-5 s, and every co-run taken before this one had its state decided
by an unstaggered launch.

**What it changes.** The low state is now reproducible on demand, which
moves the utilisation claim from open to conditional: partitioning
delivers its gain when the tenants' launches are separated correctly,
and loses when they are not. That is a scheduling condition the runtime
can enforce, not a property of the hardware to be accepted -- which is a
far better position than the previous entry left it in.

It also means every co-run measurement taken before this one was
measuring launch alignment as much as pairing, and the 9-high/2-low
distribution reflects how the harness happened to launch, not how the
die happens to behave.

### 2026-08-06 — externality re-based on the staggered runs (post-freeze change #4)

**Changed:** `MEASURED_EXTERNALITY[(16,16)]` 1.234 → 1.2362.

Numerically this is nothing — 0.18% — and that is the point worth
recording. The entry was set from two co-runs before the bistability was
known, and it happened to land in the low state. It is now the mean of
**18 side measurements across nine co-runs launched 2-5 s apart**
(+21.8% to +24.9%, mean +23.62%), and the old value survives contact
with them.

The comparison that matters is not old against new but low against high:
20 side measurements from launches at 0, 8, 10 and 15 s give +72.9% to
+83.4%, mean +78.66%. The two sets do not overlap at all. A table entry
drawn from one unstaggered co-run would have recorded whichever state
that run fell into, and three quarters of unstaggered runs fall into the
one where partitioning loses.

Tests now assert the entry against the staggered set rather than a
single report, check that the two states remain disjoint, and check that
the entry is the low state rather than a mean over both — an average of
the two modes would describe neither while looking like the sober
choice.

The source comment records the condition alongside the number: a runtime
that does not control launch alignment does not get this value.

### 2026-08-06 — the launch stagger is not the control variable either (retraction)

A warmup control run at `stagger=0, warmup=3` — the exact setting that
produced three consecutive high-state results earlier — produced two
**low**-state results:

| setting | result |
| --- | --- |
| stagger 0, warmup 3 (earlier, 3 runs) | high, +73.7 to +77.8% |
| stagger 0, warmup 3 (this batch, 2 runs) | **low, +24.8, +26.4%** |
| stagger 0, warmup 30 (this batch, 2 runs) | high, +72.8, +76.6% |

So the stagger does not select the state. The clean 9/9 and 11/11 split
reported in the previous entry was real as data and wrong as
explanation: the batches happened to be ordered so that stagger
correlated with something else.

Looking at the batches by position rather than by parameter, two of the
three show early runs low and later runs high — the fine scan (2, 2, 3,
3, 5, 5 low; 8, 8, 10, 10 high) and this warmup batch (3, 3 low; 30, 30
high). The first stagger batch does not fit that either, going high-low-
high across 0, 5, 15 s. No single ordering explains all of it.

Ten consecutive runs at one fixed setting are now running, which is the
measurement that should have come first: it separates "the state is
drawn per run" from "the state depends on run history" without any
parameter to confound it.

**This is the fourth explanation retracted for this one phenomenon**
(working set, power cap, uncontrollable bistability, launch stagger).
The pattern in my own errors is consistent: each time I varied a
parameter, saw a clean split, and reported the parameter as the cause
without first establishing what the measurement does when nothing is
varied at all. The baseline comes first; that is the lesson, and it is
cheaper than any of the four investigations that skipped it.

Standing facts, unchanged by this retraction:

- The two states are real, disjoint, and each internally stable —
  low +21.8 to +26.4%, high +72.8 to +83.4%, cv 0.2% and 2% respectively.
- The state is fixed before the first measured step.
- Temperature, power, clocks, GPU utilisation and the CU mask do not
  distinguish them.
- `MEASURED_EXTERNALITY[(16,16)] = 1.2362` is the low-state mean over 20
  side measurements and is unaffected; what is unknown is how to
  guarantee the low state, not what its value is.

### 2026-08-06 — the baseline, and what it makes possible

Ten consecutive co-runs at one fixed setting (stagger 0, warmup 3,
seed 1, 768x768, 16+16):

```
seq01 +76.4%   seq02 +71.0%   seq03 +84.2%   seq04 +75.0%   seq05 +24.8%
seq06 +75.9%   seq07 +24.7%   seq08 +76.0%   seq09 +74.8%   seq10 +24.4%
```

Low at positions 5, 7 and 10. No ordering, no drift, no dependence on
run history. **The state is drawn independently per run, with a low-state
rate near 30%.**

That accounts for all four retracted explanations at once. Each was a
parameter varied across a handful of runs, a clean-looking split, and a
causal claim — and with a 30% base rate and six or seven such
experiments, a clean split appearing somewhere is ordinary rather than
surprising. The error was not any single inference; it was doing six
inferences without ever measuring the null.

**What this makes possible, and it is better than the explanations it
replaces.** The state is distinguishable at the first step: 192 ms
against 281 ms, a 46% difference, against a within-state cv of 0.25%.
A scheduler does not need to know *why* a pairing degrades in order to
notice that it has:

| policy | externality | pairing vs whole die |
| --- | --- | --- |
| take whatever comes | 1.6215 (30/70 mix) | **−9.5%** |
| detect and retry until low | 1.2362 | **+18.7%** |

Expected retries to reach the low state is 3.3, each costing roughly one
step — about 0.7 s of probing to secure a 18.7% steady-state gain on a
request that runs for tens of seconds. Detection is cheap because the
two states are far apart and each is internally tight.

This is a scheduler action the design does not currently have: **probe
the pairing, and re-form it if the first step lands in the slow state.**
It requires no explanation of the underlying cause, only that the states
stay disjoint and independently drawn, both of which are measured.

The claim's status: the +18.7% is available to a scheduler that probes,
and is not available to one that pairs blindly, which would lose 9.5%.
That is a design requirement rather than a caveat, and it is testable
without knowing what the hardware is doing.

### 2026-08-06 — probing added to the action order (post-freeze change #5)

**Changed:** the frozen policy becomes `probing_partitioning`, which is
`slo_aware_partitioning` plus two steps that exist because of the
measured pairing bistability. The action order gains two entries:

4. slow-state budget: exclusive to any deadline the slow pairing state
   would miss.
5. probe: drop a pairing whose observed step exceeds the predicted one
   by 1.4x, re-forming it next round.

**Why the simulator now draws a state.** Modelling the pairing penalty as
the fast figure was reporting a gain the hardware supplies 30% of the
time. `PairingStates` draws per pairing, and a pairing that is dropped
and re-formed draws again — which is what the hardware does and what
makes probing work at all. With the draw in place, blind pairing scores
0.896 against the whole die's 1.000, and probing scores 1.135.

**Why the deadline branch budgets against the slow state.** A pairing
that meets a deadline only on a good draw meets it three times in ten.
Anything that cannot afford the slow figure gets the die to itself,
where there is no draw. Without this, C5 fails outright: the feasible
deadline trace picked up three avoidable misses as soon as the draw was
modelled.

**Threshold.** 1.4 sits between the states, which are 1.45x apart. Fitted
to neither, because a threshold fitted to these two numbers is fitted to
this card.

**What C6 had to be re-read as.** The probe acts on a prediction, so
degrading the predictor must cost something — that is what a predictor
is for. At +/-20% error a slow pairing can look fast (the states are 45%
apart and the error band reaches 20%), and utilisation falls to about
88% of the exact-predictor run. Judged against a fixed 5% of its own
best case, the criterion would demand that information be worthless.
Judged as Gate C words it — safe degradation — the test is that
consulting a bad predictor leaves the scheduler better off than
consulting none, and it does: 0.98 against blind pairing's 0.896 at every
error level.

**A test that was wrong about the simulator, corrected.** C4 asserted
that a policy without deadline logic records no overrides. It records
five: the simulator judges the conditions rather than the policy's
intent, and exclusive_fcfs does hand the die to requests that need it.
That is the correct reading. What the detection must not do is fire
where no deadline exists, and that is what the test now checks.

### 2026-08-06 — what the probing policy assumes and has not shown

The probe is worth having only if re-forming a pairing draws a fresh
state. What the hardware has demonstrated is narrower: **relaunching the
processes** draws afresh, shown by ten runs at one fixed setting landing
independently.

The runtime this project is designing is one process, one context, one
stream per model. There, re-forming a pairing means changing a stream's
CU mask, not relaunching anything. Two questions follow, neither
answered:

1. Does the bistability exist at all with two streams in one process?
2. If it does, does changing a stream's mask redraw the state?

A "no" to either makes the probing action unimplementable as designed,
and Gate C's +13.5% would need recomputing against whatever remains. The
nearest evidence is the transition probe -- changing a mask mid-run costs
no measurable transient, MAPE 0.23% -- but that is single-tenant and says
nothing about a pairing state.

`PairingStates.forget` encodes the assumption explicitly rather than
burying it, so the simulator states what it is relying on. It is
labelled in the source and here; it is not evidence.

This is the highest-priority measurement outstanding.

### 2026-08-06 — the bistability is the harness, not the die (post-freeze change #6)

Two streams in one process, disjoint CU masks, which is the arrangement
the design specifies. Seventeen trials across two runs:

| run | trials | states |
| --- | --- | --- |
| first | 5 (after a cold first row) | fast, 5/5 |
| second | 12 | fast, 12/12 |

Every one fast, +21.1% to +28.7%, mean **1.2367** over 24 side
measurements — agreeing with the two-process fast state (1.2362) to
0.04%. At the two-process fast rate of 30%, seventeen fast trials has
probability 1.3e-9.

**The bistability belongs to the two-process measurement harness.** It is
not a property of the die, and the runtime does not inherit it. Four
explanations were retracted while it was assumed to be one.

**Changes.** `MEASURED_EXTERNALITY[(16,16)]` 1.2362 → 1.2367, now from
the arrangement that will actually run. `PairingStates` defaults to
disabled: the draw reproduces the two-process harness and is enabled
explicitly by the tests that study it. Gate C's utilisation returns to
**+18.6%** from the probing-and-retrying +13.5%, because there is nothing
to retry.

**The probe is kept, and two defects in it were found by this.**

First, it compared the observed step against the *solo* prediction while
the observed step includes the pairing penalty. A fast pairing already
costs 1.24x solo, so a 1.4 threshold left 13% of headroom and a -20%
prediction error crossed it: 106 spurious exclusive rounds. It now
compares against the paired expectation.

Second, and not fixable by a threshold: at +/-20% predictor error the
bands overlap. A fast pairing under-predicted reads as 1.25x; a slow one
over-predicted reads as 1.20x. No threshold both catches slow pairings
and spares fast ones there. `slow_factor` is 1.3, which resolves the
overlap toward inaction — past roughly 10% error the probe becomes a
no-op and the policy behaves as `slo_aware_partitioning`. Acting on a
prediction too noisy to support the action is how a probe becomes
damage.

With that, C6 returns to its original and stricter reading — within 5%
of the exact-predictor run — and holds at every error level, because the
probe stops acting rather than acting wrongly.

**What the probe is now for.** Nothing in the target arrangement, where
it is verifiably free (asserted). It remains because most of this
project's co-run evidence was collected two-process, and a deployment
that runs tenants as separate processes does have the problem: there,
probing scores 1.133 against blind pairing's 0.895.

### 2026-08-06 — the whole externality table remeasured in-process (post-freeze change #7)

Every entry replaced. The old table was call-level, at 512x512, with two
processes; the new one is per-step, at 768x768, in one process with two
masked streams — the arrangement that will run.

| own+peer | old | new | n (overlap samples) | error in the old |
| --- | --- | --- | --- | --- |
| 4+28 | 1.307 | **1.3383** | 21 | −2.3% |
| 8+24 | 1.495 | **1.3070** | 42 | +14.4% |
| 16+16 | 1.223 | **1.2367** | 24 sides | −1.1% |
| 24+8 | 1.280 | **1.1259** | 106 | +13.7% |
| 28+4 | 1.926 | **1.0706** | 113 | **+79.9%** |

**Two defects in the harness had to be fixed first**, and both were
found because asymmetric quotas made them visible where 16+16 had not.
The solo baseline was measured once on the narrow side's mask and both
sides divided by it, so an 8+24 pair reported −44.6% externality for the
wide side. And a fixed round count let the fast side finish early,
leaving the slow one measuring alone: 12.7% per-sample cv against 0.4%
for 16+16. Each side now has its own baseline and only samples
overlapping the peer's active period are counted. The first batch was
discarded rather than corrected — it measured the wrong comparison.

**The corrected curve is monotone in own-quota:** 1.338, 1.307, 1.237,
1.126, 1.071. The old table was not, and its non-monotonicity was the
documented reason `externality()` refuses to interpolate — "16+16 costs
less than 8+24 despite the larger share". That shape was an artifact of
the 1.926 entry. Interpolation is still refused, now because five points
do not establish a shape, and a test pins the monotonicity so a future
entry that breaks it is noticed.

**The currency counterexample survives, smaller.** At 4+28 a unit in the
small tenant's hands buys 1.29x the progress of one in the large
tenant's, where the old table said 2.2x. Separability still fails —
divergence 0.772 and 0.832 at the two uneven splits — so the argument
holds and its headline number was inflated by the same 1.926 entry.

Gate C's utilisation is unchanged at 1.1862, since the scheduler reads
only (16,16), which moved 1.1%.

### 2026-08-06 — CogVideoX-2b's co-run, which Gate B recorded as impossible (post-freeze change #8)

Gate B marked this NOT_MEASURED with a reason: one process peaks at
28.54 GB and two need 57 on a 34.2 GB card. The premise was two
processes. In the arrangement the runtime specifies -- one process, one
copy of the weights, two masked streams -- it needs **14.7 GB**, and the
measurement is available.

Six side measurements at 9 frames, 16+16: +26.3% to +30.6%, mean
**1.2891**. SDXL at the same split is 1.2367, so the penalty is
model-dependent by 4.2% and the shared table under-states it here.
`MEASURED_EXTERNALITY_BY_MODEL` holds the override; other splits fall
back to the SDXL table and are extrapolations across models, labelled as
such.

Three things had to be fixed to get there, each a difference between the
harness and a runtime rather than a workaround:

- **Two weight copies do not fit.** 29.2 GB of a 31.9 GB budget, then an
  OOM 380 MB into inference. A serving runtime does not hold a second
  transformer and a second T5 to serve a second tenant; sharing the
  components halves it.
- **The tokenizer is not reentrant.** Two pipelines encoding
  concurrently raise "Already borrowed". The prompt is now encoded once
  and passed as embeddings, which is also what a runtime does -- it does
  not re-encode per denoising round -- and it takes the text encoder out
  of a measurement that quota decisions do not affect.
- **from_pipe dispatches on the base class** and rejects
  DiffusionPipeline; the second pipeline is built from the first's
  components.

Reducing frames from 9 to 5 did not help and is worth recording: it
still OOMed, and allocated slightly *more*. The cost was the weights,
not the activations, which is why halving the weights worked and
shrinking the problem did not.

### 2026-08-07 — the per-stream mask does constrain the whole pipeline

plan.md chose two processes with `ROC_GLOBAL_CU_MASK` for Gate B's
co-run cells, and said why: it avoids the coupling between per-stream
masks and the framework's internal launches. A process mask binds every
kernel; a stream mask binds only what reaches that stream, and work
launched on the default stream would be unconstrained — making the
partition nominal.

Everything measured in-process rests on the stream mask: the externality
table the simulator now uses, and the CogVideoX-2b co-run Gate B could
not take. Reading the mask back proves the stream carries it, not that
the pipeline's kernels went there.

Measured directly — one process, one masked stream, one tenant, per-step
from CUDA events, against the two-process quota curve:

| units | in-process | two-process | ratio |
| --- | --- | --- | --- |
| 4 | 516.21 ms | 521.41 ms | **0.9900** |
| 8 | 265.71 ms | 268.75 ms | 0.9887 |
| 16 | 155.09 ms | 157.50 ms | 0.9847 |
| 32 | 112.51 ms | 115.52 ms | **0.9739** |

**The mask holds.** A leak helps most where the mask is tightest, so
escaping kernels would make the low-quota cells disproportionately fast
— 4 units pulled toward the full die's 115 ms. The measured trend runs
the other way: 4 units agrees to 1.0%, 32 units to 2.6%. The residual is
larger where the step is shortest, which is what a fixed per-call
overhead the two-process harness pays (subprocess launch and
synchronisation) looks like, and it is a difference of the harness
rather than of the mask.

Recording the criterion in advance mattered here. "Agreement" alone
would have been satisfied by any of these numbers; the direction of the
disagreement is what carries the argument, and it was named before the
run.

**A failure mode worth keeping.** The first attempt destroyed each
stream between quotas. torch's ExternalStream and its caching allocator
still refer to it, and the second measurement never produced a sample —
2.5 hours at 97% GPU, looking exactly like a slow measurement rather
than a hang. Streams are now kept alive for the process.

**Consequence for Gate B-AMD's co-run clause.** The clause requires "two
disjointly masked processes", and CogVideoX-2b is NOT_MEASURED under it
because two processes need 57 GB on a 34.2 GB card. The reason the
clause named processes was the risk this measurement rules out. The
decision to amend it belongs to the project owner and is recorded as
open; the evidence for amending is here rather than in an argument from
convenience.

### 2026-08-08 — the probe deadlocked on hardware (post-freeze change #9)

**Changed:** `probing_partitioning` now ignores an observation unless it
was taken at the quota it is judging, and `RequestState` carries
`observed_at_units` to make that checkable.

Found by running the loop on the card rather than in the simulator. The
scheduler paired in round 0 and never again, twenty-one rounds:

```
round 0: granted {0: 16, 1: 16}
round 1: rid=0 observed 1.855 s against a 0.157 s prediction -> exclusive
round 2: rid=0 observed 1.855 s  (unchanged)             -> exclusive
```

1.855 s was that request's **first step**, which carries kernel
compilation; the other request measured 0.105 s because it ran after
compilation had happened. The probe read the compilation as a slow
pairing and stopped pairing that request. Being unpaired, it never
produced a newer observation, so the verdict stood for the rest of the
run. A single anomalous sample excluded a request permanently.

Two things were wrong and only one is about compilation. The probe
compared an observation to a prediction without checking they referred
to the same quota, so a step measured *alone* could also condemn a
pairing. Requiring the observation to come from the quota being judged
fixes both, and needs no special case for first steps.

**Also fixed, in the harness rather than the policy:** the adapter
synchronised on the event it had just recorded, draining the pipeline
every step and putting the drain inside the measurement -- a 0.1155 s
step read as 0.256 s. Events are now read one step late, when the device
has certainly passed them. The first step still drains, because there is
no predecessor to hide behind and a charge that did not come from
measurement is worse than one bounded drain per request.

Both of these are the project's recurring failure in its runtime form: a
scheduler behaving correctly on what it was told, where what it was told
was an artefact of how the telling was measured.

### 2026-08-08 — the externality table is call-level, and I fixed only half of this on 08-06

Measuring co-run prediction error through the runtime's own path found
that the externality table describes a different quantity from the one
the scheduler pays. Measured per step, against each side's own solo
cost at the same width:

| units | table (call-level) | measured per-step | ratio |
| --- | --- | --- | --- |
| 8 | 1.307 | **1.376** | 1.05x |
| 16 | 1.2367 | **1.525** | 1.23x |
| 24 | 1.126 | **2.68** | 2.38x |

**This is the same defect I corrected on 2026-08-06 and corrected only
halfway.** That entry found `MEASURED_QUOTA_SECONDS` holding call p50s
while the scheduler charges per step, rebuilt it from per-step
measurements, and wrote at length about why the two are not
interchangeable -- a call is denoising steps plus a VAE decode, and the
decode does not scale the way the steps do. The externality table was
measured by the same script, `run_amd_inproc_corun.py`, timing the same
`pipeline(**call)`. It was left as it was.

The dilution has exactly the shape that argument predicts. The wider a
side's quota, the shorter its steps, the larger the decode's share of
its call, and the more the call-level figure understates the per-step
penalty: 1.05x at 8 units, 2.38x at 24.

**What it does to the claim.** Recomputing the 16+16 comparison from
measurements taken the way the runtime takes them:

    paired    157.8 ms x 1.525 = 240.7 ms per step for both tenants
    rotating  2 x 106.7 ms     = 213.5 ms

Partitioning **loses 11.3%** where Gate C, using the table, reports a
18.6% gain. Every utilisation figure in this project rests on the
call-level entry.

**Not yet corrected, deliberately.** Three points do not establish the
shape of a curve, and the last time this table was rebuilt on thin
evidence the rebuilt values needed correcting twice. What is needed
before touching it:

- per-step externality at every measured split, both directions
- repeated runs, since the first co-run measurements of this project
  turned out to be bimodal and single runs were not evidence
- a check of whether the lockstep harness inflates the wide side: the
  24-unit side finishes its step long before the 8-unit side finishes
  its own, and if the barrier makes it wait, some of that 2.68 is the
  harness rather than the die

The third is the one I would bet on being partly responsible, and it is
also the one that would be most convenient to believe, so it gets
measured rather than assumed.

**Status: the utilisation claim is open again.** Not withdrawn -- the
call-level figure was measured honestly and the per-step figure is not
yet established -- and not asserted. Gate C's criteria are unaffected as
statements about the scheduler given a cost model; what is in question
is the cost model, for the second time.

### 2026-08-08 — the lockstep hypothesis is refuted; the penalty is the die's

The previous entry offered an alternative to the per-step externality
being real: the 24-unit side finishes its step long before the 8-unit
side finishes its own, so a lockstep harness might be recording its wait
as contention. That was also the convenient explanation, so it was
measured.

Every step now carries its wall-clock window, and each row reports a
second figure computed only from steps that overlapped the peer:

| pair | side | units | table | all steps | overlapped only |
| --- | --- | --- | --- | --- | --- |
| 16+16 | a | 16 | 1.237 | 1.589 | **1.589** |
| 16+16 | b | 16 | 1.237 | 1.538 | **1.538** |
| 8+24 | a | 8 | 1.307 | 1.409 | **1.409** |
| 8+24 | b | 24 | 1.126 | 2.842 | **2.842** |
| 24+8 | a | 24 | 1.126 | 2.679 | **2.679** |
| 24+8 | b | 8 | 1.307 | 1.396 | **1.396** |

Identical to three decimal places in every row. The filter removes one
to four samples per side and moves no median, so the wide side was not
waiting -- it was contending.

**The hypothesis could only ever have covered one row anyway.** The
16+16 pair is symmetric: neither side waits for the other, and it
disagrees with the table by 24-28%. And 16+16 is the only entry Gate C's
scheduler reads.

solo MAPE is 2.67% in the same run, so the measurement path is sound and
the disagreement is specific to the co-run term.

**What remains before the table is rebuilt.** Three splits measured once
each is what the first co-run campaign had when it produced a bimodal
distribution that took four wrong explanations to sort out. Repeats
come first. The numbers above are recorded as evidence, not adopted.

### 2026-08-08 — measured per step, partitioning does not pay at this workpoint

Five repeats of 16+16, the only entry Gate C's scheduler reads, measured
through the runtime's own path:

| repeat | co-run per step | solo 16u | externality |
| --- | --- | --- | --- |
| 1 | 334.7 / 335.9 ms | 156.0 ms | 2.145 / 2.153 |
| 2 | 331.0 / 333.9 ms | 158.5 ms | 2.088 / 2.106 |
| 3 | 328.3 / 323.9 ms | 159.7 ms | 2.056 / 2.028 |
| 4 | 245.2 / 250.0 ms | 161.2 ms | 1.521 / 1.551 |
| 5 | 243.8 / 265.6 ms | 161.9 ms | 1.505 / 1.640 |

Bimodal again -- 1.554 over four sides, 2.096 over six -- but this time
the two modes do not change the conclusion, because **both sit above the
table's 1.2367**:

    table  1.2367   paired 197.2 ms vs rotating 216.0 ms   +9.5%
    low    1.554    paired 247.9 ms vs rotating 216.0 ms  -12.8%
    high   2.096    paired 334.3 ms vs rotating 216.0 ms  -35.4%

**At 768x768 with SDXL against itself, partitioning loses.** The +18.6%
this project has reported throughout came from a call-level externality
applied to a per-step decision, and the two differ by the VAE decode --
the same confusion corrected in the quota table on 08-06 and left
uncorrected here.

**What this does not touch.** Gate A's mask mechanism, Gate B's quota
curves, and every week 7-8 clause -- bit-exact latents against ASLE,
scheduler p99 of 11 us, zero weight bytes after residency, an hour
without leak or deadlock, Jain 0.99915 -- are measurements in their own
right and stand. Gate C's seven criteria are statements about a
scheduler given a cost model, and they still hold of that scheduler.
What fails is the cost model's co-run term, and with it the claim the
scheduler was built to support.

**What is not yet established.** That partitioning loses *in general*.
This is one workpoint (768), one model paired with itself, one split
(16+16), on one card. The measurements that would bound the claim
properly:

- other splits per step, both directions -- 8+24 already measures 1.40
  and 2.68, so the asymmetry may matter more than the symmetric case
- other workpoints, since the solo curve's shape varies with resolution
- SDXL against CogVideoX-2b, the mismatched case the scheduler is
  actually designed for, which no per-step measurement covers yet

**Status.** The utilisation claim as stated -- spatial partitioning
raises throughput over a full die -- is refuted at the workpoint it was
measured at. Whether a defensible version survives is a question about
where partitioning does pay, and that requires the measurements above
rather than a reinterpretation of these.

### 2026-08-08 (later) -- the co-run measurement process never exits

The first co-run MAPE run wrote its JSON at 01:28:36 and was still alive at
06:50, five hours later, holding 8.7 GB with 92 threads: 90 in
``futex_do_wait`` and 2 in ``kfd_wait_on_events``. GPU use 3%, power 11 W --
doing nothing, and impossible to distinguish from a slow run by looking at
it. Its output and log were already on disk, so killing it lost nothing.

Two consequences worth separating. The defect: a measurement script that
completes and does not exit leaves a resident process on a shared card, and
the previous failure of this shape (``hipStreamDestroy`` between quotas)
cost 2.5 hours before it was recognised as a hang rather than slowness. The
confound: that process was resident for the whole of the five-repeat
campaign, so those repeats were not made on a clean die.

Discovering it also exposed a flaw in how the repeats were run. The loop
passed ``--seed $i`` for i in 1..5, so **seed and position were the same
number**. The observed pattern -- externality 2.15, 2.10, 2.04, then 1.54,
1.57 -- is equally well described as "the first three runs" or "seeds 1-3",
and nothing in the data can separate them. Repeating a measurement five
times establishes nothing if the repeats vary a parameter in lockstep with
their order.

The re-measurement uses a palindromic seed order (1,2,3,4,5,5,4,3,2,1) so
each seed appears once early and once late, and samples clocks, junction
temperature and package power at 1 Hz alongside, so a state change can be
checked against the die rather than guessed at. Note the direction of the
original anomaly: solo rose monotonically (156.0 to 161.9 ms) while co-run
fell sharply, and a throttling story predicts both slowing together.

### 2026-08-08 (later still) -- decorrelated, the co-run measurement is unimodal

Ten runs on a cleaned die, seeds ordered 1,2,3,4,5,5,4,3,2,1 so each seed
appears once early and once late, clocks and temperatures sampled at 1 Hz
alongside:

| pos | seed | solo 16u | solo 32u | co-run/step | externality | junction |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 156.8 ms | 106.6 ms | 240.5 ms | 1.534 | 43.0 C |
| 2 | 2 | 157.9 | 107.4 | 243.1 | 1.539 | 50.3 |
| 3 | 3 | 159.8 | 108.4 | 249.6 | 1.562 | 55.7 |
| 4 | 4 | 160.8 | 109.0 | 249.7 | 1.552 | 60.8 |
| 5 | 5 | 161.6 | 107.8 | 244.6 | 1.513 | 63.6 |
| 6 | 5 | 164.4 | 108.9 | 252.3 | 1.535 | 66.1 |
| 7 | 4 | 163.0 | 109.4 | 246.6 | 1.513 | 67.9 |
| 8 | 3 | 162.2 | 109.6 | 259.1 | 1.597 | 69.7 |
| 9 | 2 | 164.7 | 110.3 | 255.4 | 1.550 | 71.4 |
| 10 | 1 | 164.7 | 109.9 | 253.8 | 1.541 | 72.8 |

**No bimodality.** Externality 1.5436, sd 0.0439. Position moves it by
+0.007 across the two halves and seed by -0.025 between seeds 1-3 and 4-5:
both null. Solo does drift, 156.8 to 164.7 ms, monotone and tracking a
30 C junction rise -- so that is thermal, and the ratio is flat through it
because numerator and denominator drift together.

**What the earlier 2.03-2.15 readings were is not established, and this is
deliberately left open.** The obvious candidate is the hung process that
was resident on the card for that whole campaign -- but it was equally
resident for that campaign's *low* readings, so it does not explain the
split within it. This project has already produced four wrong explanations
for a co-run bistability that turned out not to need one. A fifth guess
would cost more than the open question does.

**The verdict does not depend on resolving it**, because the clean,
controlled, unimodal value is still far above the table:

    per step, 16+16, 768x768, SDXL against itself
      partitioned  both tenants advance one step in  249.5 ms
      rotating     both tenants advance one step in  217.4 ms
      -> partitioning -12.8%,  where the table's 1.2367 predicts +8.8%

So the refutation of 2026-08-08 stands and is now on ten controlled runs
rather than one. The open splits -- other widths, other workpoints, and
the mismatched SDXL x CogVideoX pairing the scheduler was designed for --
are the next measurements, and none of them is answered by this one.

### 2026-08-08 (evening) -- the co-run penalty is bistable, and it latches

The stated criterion was: if queue pressure is the mechanism, co-run p50
rises monotonically with the number of live masked streams while solo
stays within noise. It was refuted -- and refuted in a way that says
something much more useful than a confirmation would have.

Two processes, identical script, identical stream counts at every point:

    rep1  corun  240.9  192.2  192.5  193.5  194.3    ext 1.51 -> 1.13
    rep2  corun  250.2  296.9  300.4  293.0  293.1    ext 1.51 -> 1.69

They agree at the first co-run and then move in **opposite directions**
to two different plateaus, and each stays on its plateau for every later
episode. Stream count is not the variable: both had 3, 7, 11, 19, 31.

Pooling every per-step 16+16 measurement in the project by episode index
makes the structure plain:

| | n | mean ext | range |
| --- | --- | --- | --- |
| first co-run in a process | 22 | **1.541** | 1.419-1.614 |
| later, fast plateau | 4 | 1.149 | 1.127-1.170 |
| later, slow plateau | 4 | 1.728 | 1.690-1.762 |
| later, five-split harness | 6 | 1.808 | 1.760-1.856 |

The first co-run in a process is the most reproducible number this
project has: 1.541, sd 0.043, over twelve independent processes. What
happens *after* it is drawn per process and then latched.

**This changes the verdict from a number into a state.** Against
rotation at 2 x 108.9 ms:

    first co-run   ext 1.541   paired 251.0 ms   partitioning -13.2%
    fast plateau   ext 1.149   paired 187.1 ms   partitioning +16.4%
    slow plateau   ext 1.728   paired 281.5 ms   partitioning -22.6%
    table (call)   ext 1.237   paired 201.5 ms   partitioning  +8.1%

**So this morning's refutation is retracted in strength, not direction.**
The -12.8% was real and reproducible, but it is the first-co-run state
rather than *the* value; a process that latches fast gains 16.4% and one
that latches slow loses 22.6%. Nor does this rescue the original +18.6%:
1.2367 was measured at call granularity, and it happens to fall between
the states rather than describe any of them.

What is established, and it is a stronger statement than either:
**spatial partitioning on this hardware does not have a throughput
constant.** A cost model with one externality per width pair is
calibrating against a latched coin flip, and which side it landed on is
invisible to it.

That is also, awkwardly for the original claim and well for the design,
exactly what the dual ledger was built for. Charging from prediction
would make the accounting exact by construction and blind to this; the
runtime charges from measurement, and ``probing_partitioning`` already
stops pairing when observed exceeds predicted by its tolerance. A
scheduler that measures can ride either state. A scheduler calibrated
against a constant cannot.

**Next, and none of it is answered by what is above.** How often each
state is drawn and whether it ever flips mid-process; how many steps are
needed to tell which state the runtime is in, since the probe action
needs that to be small; whether the latch survives across mask pairs, as
the five-split harness measuring 1.808 while holding the same stream
count as the 1.149 process suggests something the level sweep did not
capture.

Held over, unchanged and still unmeasured: the mismatched SDXL x
CogVideoX pairing. The adapter and harness for it are written and
synced; running them before the state question is settled would produce
another number nobody can interpret.

### 2026-08-08 (night) -- the state is drawn per pairing, latches, and is visible in one step

Eight processes, twelve co-run episodes each, solo remeasured before every
one, with 8+24 interposed at episodes 5 and 9:

| | n | mean ext | range |
| --- | --- | --- | --- |
| steady state, fast (6 of 8 processes) | 54 | **1.176** | 1.163-1.202 |
| steady state, slow (2 of 8 processes) | 18 | **1.776** | 1.732-1.850 |

Four things follow, and each was an open question this morning.

**It never flips.** 0 of 8 processes moved between states across nine
later episodes; the widest within-process spread was 0.106, against a
gap of 0.57 between states. Every episode built fresh adapters and
executors and re-acquired its streams, so re-forming a pairing is not a
fresh draw. That matters directly: ``probing_partitioning`` carries the
comment *"Re-forming it next round is a fresh draw; holding it is not"*,
and the hardware says otherwise.

**It is visible immediately.** Comparing each episode's first step
against its own median: mean absolute error 0.00%, max 0.00%, n=72. The
per-step series inside an episode is literally constant -- 194 194 194
194 194. A probe costs one step, not a window.

**It is per pairing, not per process.** 16+16 latched fast in p1 and p2
alike, while 8+24 landed at (1.11, 1.17) in p1 and (1.37, 2.86) in p2,
each consistent across both of its interpositions. And interposing 8+24
never disturbed the 16+16 state: sixteen before/after checks, sixteen
unchanged.

**The first co-run in a process is a surcharge on the drawn state, not a
third state.** Fast processes opened at 1.52-1.61 against a 1.176
plateau; slow ones opened at 2.04-2.09 against 1.776.

**That closes this morning's open question, and not the way it was
leaning.** The five-repeat campaign read 2.145, 2.153, 2.088, 2.106,
2.056, 2.028 and then 1.521, 1.551, 1.505, 1.640. Those are exactly
slow-state and fast-state *first* co-runs -- 2.04/2.09 and 1.52-1.61 in
this campaign. Three processes drew slow, two drew fast. The hung
process resident on the card had nothing to do with it, and the entry
above that named it "the obvious candidate" was wrong to suspect it. The
instinct not to assert it was right; the suspicion itself was not
evidence.

**The claim this supports.** Against rotation at 2 x 108.9 ms:

    steady state fast   ext 1.176   paired 192.8 ms   partitioning +13.0%
    steady state slow   ext 1.776   paired 293.9 ms   partitioning -25.9%

Partitioning pays 13% when the die grants it, the die granted it in six
of eight processes, the state is readable in a single step and never
flips. A scheduler that measures captures roughly +9.8% net of the
probe; a scheduler calibrated to a constant takes the coin flip. That is
a weaker headline than +18.6% and a true one, and it makes the dual
ledger load-bearing rather than decorative -- charging from prediction
would have made this invisible.

**Not established.** The rate. Two slow draws in eight is 25% with a
wide interval, and the ten decorrelation processes earlier today all
drew fast, which at 25% has probability 0.056. Whether the rate depends
on the harness, the pair, or something in the driver's state is
unmeasured, and no scheduler decision should rest on its value -- only
on the state being detectable, which it is.

**Defect opened against the frozen policy (would be post-freeze #10).**
The probe drops a slow pairing for one round and re-forms it the next,
on the stated rationale that re-forming redraws. It does not. The policy
therefore pays one degraded paired round in every two, indefinitely, in
a process that drew slow. A sticky verdict with backoff keeps the
recovery path -- nothing here rules out the state changing on timescales
longer than the ~2 minutes measured -- while removing the standing cost.

### Post-freeze change 10 -- extract the slow test, add a sticky variant beside the frozen policy

Two structural hashes move; no behavioural digest and no table does.

**What changed.** ``probing_partitioning``'s slow-state test is now
``policies._pairing_reads_slow``, called rather than inlined. The
extraction is verbatim -- both return on the first observation over
``expected * FAST_PAIRING_EXTERNALITY * slow_factor`` -- and the
behavioural lock confirms it: the frozen policy's decisions on all four
locked traces are byte-identical. The helper is added to
``FROZEN_FUNCTIONS``, because it is now on the frozen decision path and
the manifest's own note says a helper must be listed consciously.

``trace_sim.simulate`` passes the granted widths to
``PairingStates.factor_for``. Under the default (unlatched) mode they are
ignored, which is why the behavioural digests are unchanged.

**What did not change.** ``probing_partitioning`` still drops a slow
pairing for one round and re-forms it the next. The measurement says that
is wrong -- the state is keyed by the mask pair and does not redraw -- but
a frozen baseline is worth more as something to compare against than as
something to quietly improve. The alternative is
``make_sticky_probing_partitioning``: a slow verdict is remembered against
the pair of granted widths, with a backoff doubling from 1 s to 60 s.

The backoff is not decoration. Nothing measured rules out the state
changing on timescales longer than the two minutes observed, and a policy
that never re-probed could not find out. A permanent verdict would claim
more than the evidence supports.

It returns a fresh closure per call and is kept out of ``BASELINES``,
which the tests iterate: a policy carrying a verdict from one simulation
into the next is a coupling nobody would look for.

**On the constant that was not changed.** The obvious edit -- replace
``FAST_PAIRING_EXTERNALITY`` with the per-step 1.176 -- would be wrong,
and the reason is worth recording because it is not obvious. That
constant is not used as data in the policy; it is multiplied by
``slow_factor`` to make the threshold, 1.2367 x 1.3 = 1.608. Against the
per-step states that threshold is well placed: it keeps a fast pairing
(1.176) *and* its first-co-run surcharge (1.52-1.61), and catches a slow
one (1.776) *and* its surcharge (2.04-2.09). Substituting 1.176 moves the
threshold to 1.529 and starts discarding fast pairings on their first
step, which is the one step every pairing has. The per-step measurements
go beside the constant, as ``MEASURED_STEP_PAIRING_FAST`` and
``MEASURED_STEP_PAIRING_SLOW``, not into it.

#### What the sticky variant is worth, and what it bets on

Twelve seeds, six backlogged SDXL requests, utilisation against a whole
die of 1.0:

| policy | latched die (measured) | spread | unlatched (the old model) |
| --- | --- | --- | --- |
| exclusive, whole die | 1.0000 | 0.0000 | 1.0000 |
| blind pairing | 0.9664 | 0.4214 | 0.8955 |
| probing_partitioning (frozen) | 1.0367 | 0.3161 | **1.1346** |
| sticky_probing_partitioning | **1.0767** | 0.2561 | 1.0591 |

Under the die as measured, the sticky variant is four points better than
the frozen probe and steadier. That is roughly what the hardware numbers
predict: the fast state pays +13.0%, six of eight processes drew it, so
about +9.8% before the probe and the scheduler's own overheads, against a
simulated +7.7%.

**It is a bet, and the table shows what it costs if the bet is wrong.**
Under the unlatched model the frozen probe wins by seven points, because
there re-forming really does redraw and dropping a pairing buys a new
coin. Each policy is better under the model it was designed for. The
evidence for the latch is strong -- 0 flips in 72 episodes across eight
processes, surviving fresh adapters, re-acquired streams and an
interposed different pair -- but it is two minutes of observation, and
the backoff exists so that a policy which bet wrong is only wrong until
it re-probes.

Neither number is a claim about hardware throughput. They are what the
simulator returns given a cost model, which is the only thing a
simulator can say.

### 2026-08-08 (night) -- mismatched tenants: partitioning pays, and pays twice

The case the scheduler was designed for and the only one with no per-step
measurement. SDXL at 768 against CogVideoX-2b at 480x720x9f, 16+16
disjoint masks, five co-run episodes per process, four processes, solo
remeasured after the episodes:

| episode | SDXL externality | SDXL vs rotation | CogVideoX externality | CogVideoX vs rotation |
| --- | --- | --- | --- | --- |
| 1 | 6.29 | -77.3% | 1.046 | +22.9% |
| 2 | 6.15 | -76.8% | 1.051 | +22.4% |
| 3-5 | **1.01** | **+41.7%** | **1.051** | **+22.3%** |

Settled, over episodes 4-5 across all four processes: SDXL 1.010 and
CogVideoX 1.051. Both tenants run at very nearly their solo speed while
holding half the die each.

**Both gain, so partitioning Pareto-dominates rotation here.** The
comparison is per tenant against its own share: under rotation a tenant
with half the schedule runs alone on the whole die for half the wall
clock, so its rate is 0.5/solo32; partitioned it holds half the die
always, so its rate is 1/corun. In time T, partitioning gives SDXL 6.41T
steps against 4.56T and CogVideoX 1.198T against 0.98T. Neither tenant is
paying for the other's gain, which is what the self-paired case could
never manage.

**This is the opposite result from self-pairing, and the contrast is the
finding.** Identical tenants at 16+16 pay 1.176 in the fast state and
1.776 in the slow one, so partitioning wins 13.0% or loses 25.9%
depending on a draw. Mismatched tenants pay 1.01 and 1.05, with no draw
at all -- four of four processes traced the same curve to within 0.02.
Two tenants wanting the same units in the same way at the same instant is
the hard case; two wanting different things is the easy one, and the
scheduler exists for the second.

**The two-episode transient is not a detail.** SDXL runs at 6.29x for its
first two episodes -- 28 steps at ~970 ms against a 153 ms solo -- before
dropping to 1.01 and staying there for the rest of the process. It is
perfectly reproducible: every process, both episodes, 6.11 to 6.34.
Whatever settles, settles after two episodes of real co-run and then
holds across fresh executors.

That transient is a trap for exactly the action this project just built.
``probing_partitioning`` compares observed against 1.608x predicted, and
6.29 clears it by four times, so the probe drops this pairing on its
first step -- the pairing that would have paid +41.7% three episodes
later. The frozen policy re-forms every round, so it pays two episodes
and finds the good state. **The sticky variant would not**, if its
verdict were permanent.

So the backoff earns its place. It was written this morning as insurance
against a state that might change on a timescale longer than the two
minutes measured -- speculative, and justified only by not being ruled
out. The mismatched pairing is a measured case where giving up
permanently costs 41.7%, and where re-probing after 1, 2, 4 seconds finds
it. A design that had reasoned only from the self-paired latch would have
made the verdict permanent and been wrong.

**Not established.** What settles. The obvious guesses -- allocator
warm-up, some driver-side placement decision, CogVideoX's first episodes
disturbing something SDXL then recovers from -- are guesses, and this
project has already written four wrong explanations for a co-run effect.
What is measured is that it takes two episodes, it is the same in four
processes, and it holds afterwards.

### Post-freeze change 11 -- pass the round to the pairing state

``trace_sim.simulate`` now passes ``at=now`` to
``PairingStates.factor_for``. One structural hash moves; no behavioural
digest and no table does, because the argument is ignored unless
``settle_after`` is set.

It exists so the simulator can represent the mismatched pairing's
transient: two rounds of real co-run at the slow figure, then the fast
one, which is what four processes measured to within 0.02. Counted in
distinct rounds rather than calls, since a round asks once per member and
"settles after two rounds" is not "settles after four calls".

Without it the simulator cannot express the case that decides whether a
sticky verdict may be permanent -- a pairing that is genuinely bad when
first probed and genuinely good three rounds later. Asserting that the
backoff handles it, which the previous entry did, is reasoning; this is
what makes it testable.

#### Measured, not reasoned: what each policy does in each regime

Twelve seeds, utilisation against a whole die of 1.0. The left column is
the self-paired die -- a state drawn per mask pair and latched. The right
is the mismatched one -- two rounds slow, then fast for good.

| policy | latched bistable | settles after 2 |
| --- | --- | --- |
| exclusive, whole die | 1.0000 | 1.0000 |
| blind pairing | 0.9664 | **1.2369** |
| probing_partitioning (frozen) | 1.0367 | 1.2268 |
| sticky, backoff 1 s to 60 s | **1.0767** | 1.2025 |
| sticky, verdict permanent | 1.0813 | 0.9982 |

Three of the four claims in the previous entry hold. A permanent verdict
does forfeit the settled pairing, and by more than "forfeit" suggests:
0.9982, worse than never partitioning at all. The frozen policy's
re-forming does find it. The backoff does rescue the sticky variant,
1.2025 against 0.9982.

**The fourth was wrong, and in a direction worth keeping.** Blind pairing
is the best policy in the settling regime -- 1.2369, ahead of every
probing variant. Of course it is: probing buys the ability to refuse a
bad pairing, and where every pairing becomes good there is nothing to
refuse and the probe is pure overhead. The probe earns its keep in the
other column, where blind pairing scores 0.9664 and loses to doing
nothing.

So no policy here dominates, and the die shows **both** regimes: identical
tenants latch into a drawn state, mismatched ones settle into a good one.
Any single-regime tuning is tuned against half the hardware.

The backoff's trade is now a measurement rather than an argument. It
costs 0.4% in the latched regime -- 1.0767 against a permanent verdict's
1.0813 -- and it is worth 20% in the settling one. That is the shape of
trade a design should take, and this morning it was justified only by
"nothing rules it out".

### 2026-08-08 -- the serial fallback, and the threshold it had to be measured against

plan.md's week 9-10 clause: fall back to a conservative serial action
when the profile is missing or drift exceeds 15%, recording the reason.
Implemented in ``Runtime`` rather than in the policy, because the policy
is frozen and because this is a safety envelope -- it can only ever
narrow a grant to one request holding the whole die, never widen one.

**Two thresholds, deliberately different.** The policy's probe fires at
1.608x the *solo* prediction and asks "is this pairing in the slow
state". The envelope fires at 1.15x the *paired* expectation and asks "is
the cost model wrong about this pairing at all". The gap between them is
where the envelope does its work.

Getting that second comparison right was not optional, and the first
attempt got it wrong in the way the policy's own comment warns about. A
co-run legitimately costs more than the solo prediction, so comparing
observed against the raw prediction reported a 23.7% drift for a runtime
behaving exactly as the cost model says -- the envelope fired every round
and the fallback would have been permanent. The test that caught it is
the one asserting a faithful pairing is never held, which existed only
because "the envelope must not fire on a runtime that is behaving" seemed
worth stating.

**The refusal expires, and that is the clause met rather than softened.**
SDXL against CogVideoX-2b runs at 6.29x predicted for two rounds -- 529%
drift, five times any threshold anyone would pick -- and then at 1.01x
for the rest of the process, where it beats rotation by 41.7%, in four
processes out of four. A permanent refusal there simulates at 0.9982
against a whole die's 1.0000: worse than never partitioning. So the hold
backs off, doubling 1 s to 60 s, keyed by the pair of granted widths
because that is what the die keys its co-run state on.

The profile-missing branch is the same mechanism. ``externality`` raises
rather than inventing a factor for a pair it has not measured -- it did
exactly that during today's split campaign, which is how that campaign
was found to be asking for 12+20 -- and the runtime now turns that
refusal into a serial round with a reason attached, instead of charging
the co-run at 1.0 and recording an invention.

946 tests pass; disabling the hold fails five of the six new ones.

### 2026-08-08 -- the mismatched result across splits, and what the transient is

Four more splits, four episodes each, one process per split, SDXL against
CogVideoX-2b. Settled figures are episodes 3-4; the sign is what matters
and both tenants carry it.

| split (sdxl+cog) | transient, first 2 eps | settled SDXL | settled CogVideoX |
| --- | --- | --- | --- |
| 4+28 | 1.89, 1.82 | **+67.9%** | +1.7% |
| 8+24 | 3.11, 3.06 | **+61.3%** | +7.4% |
| 16+16 | 6.29, 6.15 | **+41.7%** | +22.3% |
| 24+8 | 13.41, 13.26 | +16.6% (one episode) | +28.6% |
| 28+4 | 26.32, 26.16 | +2.6% | **+30.6%** |

Every split, once settled, pays both tenants. The split decides how the
gain is divided, not whether there is one: the narrower SDXL's quota, the
more it gains and the less CogVideoX does, monotonically across all five.

**The transient has a shape, and the shape says what it is.** During the
first two episodes each side's step costs about the *sum* of the two solo
steps:

    split    solo_a + solo_b      measured transient
    4+28     526 + 550 = 1076     994
    8+24     272 + 595 =  867     844
    16+16    153 + 793 =  946     970
    24+8     127 + 1567 = 1694    1704
    28+4     123 + 3097 = 3220    3250

Five splits, agreement within 3-9%, over a range where the sum varies by
3x. That is what full serialisation looks like: for two episodes the die
runs one masked stream's kernels then the other's, and each side's CUDA
events span the whole alternation. It also explains why CogVideoX barely
notices -- its own step dominates the sum -- and why the effect looked
like "SDXL is crushed" rather than "nothing overlaps".

**Stated as a prediction rather than a story, because this project has
written four wrong explanations for a co-run effect.** If the transient
is serialisation, then a pairing of two *equal* step costs should show a
transient of about 2.0x on both sides, and the self-paired 16+16 case
should open at 2.0x rather than the 1.33x surcharge it actually shows
over its plateau. It does not, so the account is incomplete: whatever
happens for two episodes is full serialisation between different models
and something weaker within one. That is a discriminating measurement
someone can run, not a conclusion.

**One outlier, unexplained and kept.** 24+8 episode 4 read 4699.8 ms for
SDXL -- externality 37.0 -- after episode 3 read 127.3 ms at 1.002. One
process, four episodes, so there is no repeat to say whether it is a
third state, a stray, or something on the box. It is in the payload and
in this table's caveat rather than dropped, and 24+8 is the split to
repeat first.

### 2026-08-08 -- 24+8 repeated: the outlier is quantised, not noise

Three processes, six episodes each, the split whose single run produced a
37x reading.

Settled (episodes 3-6, the one outlier excluded): SDXL at 24 units reads
0.998 and CogVideoX at 8 reads 1.007 -- both essentially free, both
gaining, +17.2% and +29.1%. That confirms the single-process result and
completes the picture: every split measured pays both tenants once
settled.

**The outlier recurred, and it is a discrete state rather than a stray.**

    original run, episode 4   4699.8 ms   externality 37.0
    repeat r1,   episode 5    4701.2 ms   externality 37.3

Two occurrences 0.03% apart. And 4701 ms is not an arbitrary number: the
peer's solo step is 1587 ms, and 4701 / 1587 = 2.96. SDXL's step spans
almost exactly **three** of CogVideoX's. Both times the very next episode
returned to 127 ms.

So this is not noise and not degradation; it is a round in which one
tenant is serviced once per three of its peer's steps. Two of fourteen
settled episodes, which is a rate with a wide interval and not one any
design should lean on -- what matters is the shape: quantised, severe,
and self-clearing in one episode.

**It is also the case the serial fallback was built for**, and it is
worth noticing that it was built before this was measured. A 3:1 starve
is a drift of 2900%, far over the envelope's 15%, so the round after it
goes serial with a reason recorded; and because the hold backs off rather
than being permanent, the pairing -- which pays +17% and +29% in the
other eleven episodes -- is retried rather than written off. A permanent
refusal would have cost both tenants their gain on the strength of one
round in seven.

What produces a 3:1 quantisation is unmeasured, and this project has
written four wrong explanations for a co-run effect. It is recorded as a
shape and a rate.

### 2026-08-08 -- the per-step pair table, and why it is not wired in yet

``MEASURED_STEP_PAIR_EXTERNALITY``, keyed by (model, units, peer_model,
peer_units) and giving that side's factor. Eleven entries: five
mismatched pairings from both sides, and the self-paired 16+16.

Keyed by both models because a mismatched pairing does not penalise its
tenants equally -- at 24+8 SDXL reads 0.998 and CogVideoX 1.007, at 16+16
they read 1.010 and 1.051 -- and per side because averaging them would
hide who pays.

**Three things it cannot hold, all recorded beside it rather than left
for a reader to discover.**

The transient. A mismatched pairing runs serialised for two episodes at
1.89x to 26.3x depending on the split, which is the largest effect in the
data. A table keyed on widths has nowhere to put "and for the first two
rounds it is 26x", so ``MEASURED_MISMATCHED_SETTLE_EPISODES`` sits next
to it and a test asserts the settled factor does not describe it.

The draw. The self-paired entry carries the fast state, which the die
takes six times in eight; the slow one is 51% worse and no lookup can say
which this process got. The entry exists so a lookup does not fail, not
because a number is the right answer.

The width-only fallback. ``step_pair_externality`` returns None for a
pairing it has not measured rather than reaching for
``MEASURED_EXTERNALITY``. That table is call-level and describes a
different quantity; substituting it is exactly the granularity error this
project has now made twice.

**Not wired into ``externality()``.** That function is on the path the
behavioural lock covers, and repointing it changes what every mixed-model
trace costs. The change is worth making, and worth making on its own,
with the lock re-examined and the simulator's mixed-model traces looked
at -- not as a side effect of recording measurements. Until then the
table is data with provenance and eight tests, and the frozen path is
untouched.

954 tests pass; freeze verifies.

### 2026-08-08 -- the profiler could not answer the profiler clause

plan.md's week 9-10 acceptance includes a timeline confirming the
expected overlap. rocprofv3 is the instrument on this card, and it failed
twice over in one run.

**It changed what it observed.** The traced episodes read 10.51, 1.00,
1.01, 5.92 for SDXL where the untraced harness gives 6.3, 6.2, 1.01, 1.01
in four processes out of four. The run's own validity check -- transient
above 3x for two episodes, below 1.5 after -- reported False. That check
existed because a profiler manufacturing the finding under test is the
obvious way this measurement goes wrong, and it is the reason the trace
was not read and interpreted anyway.

**Then it crashed without writing one.** SIGSEGV in
``hsa_signal_wait_relaxed`` at teardown; no ``kernel_trace.csv`` was
produced. So there was nothing to read even had it been valid.

Both are kept in the raw runs. The JSON was written before the crash, so
the externalities above are on disk.

**The clause does not need it.** Overlap is a question about when each
side's kernels were on the device, and both sides already bracket every
step with CUDA events on their own stream. The only thing missing was a
common origin: record one event before the episode, and afterwards every
event's offset from it puts both sides' intervals on one device timeline,
where busy and both-live are arithmetic. ``run_amd_overlap_events.py``
does that -- one synchronise per episode and it is after the episode, so
nothing is perturbed inside the measured region, and it reads the same
events the externality is computed from rather than a parallel account of
them.

It carries the same validity check the profiler failed, and the same
prediction, unchanged: the first two episodes overlap near zero and the
later ones do not. If both overlap, the sum-of-solo-steps fit is right
about the arithmetic and wrong about the cause.

### 2026-08-08 -- the instrument every per-step co-run number came from is unreliable

The overlap harness measures a step by bracketing it with two events on
that side's stream. The adapters measure it with their own start/end
events, recorded *inside* the same region on the same stream, and read
one step late. So the adapter's interval is contained in the harness's,
and its reading can never legitimately exceed it.

Running both in one process, over the same steps:

| episode | SDXL span | SDXL adapter |
| --- | --- | --- |
| 1 | 1.022 | **10.888** |
| 2 | 1.022 | **6.234** |
| 3 | 1.022 | 1.022 |
| 4 | 1.020 | 1.023 |

The containment is violated by a factor of ten. Whatever the adapter is
reporting in episodes 1 and 2, it is not the duration of the step it is
attributed to. The deferred read only updates when the previous step's
events have retired; when it does not update, the previous value stands
and is counted again, so a single slow reading can be re-counted until it
owns the median.

**Every per-step co-run number in this project comes from that reading.**
The bistability and its latch, the two-episode transient, the
sum-of-solo-steps fit across five splits, the per-split table, the 3:1
quantised outlier -- all of them are medians of
``adapter.last_step_seconds``.

What the span instrument says instead, provisionally, over three
processes and five episodes each:

    mismatched 16+16   SDXL 1.02, CogVideoX 1.05, every episode, no transient
    self-paired 16+16  1.23 to 1.35, every episode, no bimodality,
                       overlap fraction 0.90 to 0.92

Both are single-instrument numbers in their turn and neither is adopted
here. The span brackets more than the kernels -- it includes whatever
host time passes between the two records -- so it is an upper bound, and
an upper bound of 1.02 does mean the mismatched pairing is very nearly
free. But three processes is three processes.

**What has to happen before any of today's per-step conclusions stand
again.** The disagreement has to be explained rather than arbitrated: the
adapter did measure ~946 ms at some point, which is suspiciously exactly
solo_a + solo_b, so the slow steps may be real and rare while the
adapter's staleness makes them look continuous. Medians cannot tell those
apart, which is why the harness now keeps both instruments' full per-step
series instead of their medians.

**Recorded plainly: this is the ninth time today's pattern has repeated.**
A property of the measurement presented itself as a property of the
thing measured, and it did so with a beautiful quantitative fit --
solo_a + solo_b, five splits, within 3-9%, over a 3x range. The fit was
real and it was fitting an artifact. Nothing about a good fit makes an
instrument trustworthy.

The conclusions suspended by this, pending re-measurement: the co-run
bistability and its per-mask-pair latch; the two-episode transient and
the serialisation account of it; ``MEASURED_STEP_PAIR_EXTERNALITY`` and
``MEASURED_STEP_PAIRING_FAST``/``_SLOW``; the +41.7% and +13.0%/-25.9%
figures. The code built around them -- the sticky policy, the latched
model, the serial fallback -- is not thereby wrong, but its calibration
is unfounded until the numbers are re-established.

Not suspended: everything measured by byte comparison or by counting
(Gate A's masks, the ASLE bit-exactness, residency, the leak, the churn,
Jain), and the solo quota curves, which were measured with fresh adapters
whose first step always drained.

### 2026-08-09 -- the two instruments separated: what was artifact and what was not

Keeping both instruments' full per-step series instead of their medians
answers it in one run.

**The adapter's reading is a single value repeated.** Over eight
SDXL-side episodes it has `distinct values = 1` every time -- nine
appends, one number. The CogVideoX side, whose steps run ~830 ms, has 8
or 9 distinct values out of 9. The deferred read succeeds when the
previous step's events have had time to retire and fails when they have
not, so a side with short steps reports whichever reading it happened to
be holding, for the whole episode:

    episode 1   adapter 963 ms     episode 3   adapter 156 ms
    episode 2   adapter 949 ms     episode 4   adapter 1666 ms

That is the "two-episode transient" and the "latch". Not a state of the
die -- a state of the variable.

**But the span series shows real structure, and it is inside every
episode rather than across the first two:**

    sdxl span series, episodes 1-4, identical in shape
      1684  1683  888  156  156  156  156  156  156

Three slow steps then six at solo speed, in all four episodes. So the
slow steps are real; what was wrong was believing they stopped after two
episodes, which is what a stale variable would show and a repeating
within-episode phase would not.

**The self-paired bistability appears to survive, with different
numbers.** Two span measurements of the same configuration:

    three processes, no full-die solo   span ~195 ms   1.25 x solo
    one process, with full-die solo     span ~300 ms   1.93 x solo

The ratio between them is 1.54. The ratio between the states the adapter
reported, 1.776 and 1.176, is 1.51. Two instruments disagreeing on the
absolute value and agreeing on the ratio is what a real two-state system
plus a biased reading looks like -- but this is one process against
three, with another variable (the full-die solo) changing at the same
time, and that is exactly the confound that made the seed-and-position
campaign worthless this morning. Eight processes are running with the
span instrument and the configuration fixed.

**So the retraction narrows.** What was artifact: the two-episode
transient, the per-episode latch, the sum-of-solo-steps fit, and the
absolute values in ``MEASURED_STEP_PAIR_EXTERNALITY`` and
``MEASURED_STEP_PAIRING_FAST``/``_SLOW``. What survives so far: a real
slow phase at the start of every co-run episode, and -- pending the eight
processes -- a two-state co-run penalty whose states differ by about 1.5x.

The suspension stays until the span measurement stands on its own count
of processes. The point of yesterday's retraction was not that the
numbers were too high or too low; it was that they came from an
instrument that had not been checked, and checking it is what this is.

### 2026-08-09 -- the bistability re-established on a checked instrument

Eight processes, four episodes each, self-paired SDXL 16+16, measured by
device span rather than by the adapter's deferred read.

| | processes | side-episodes | mean | range | overlap |
| --- | --- | --- | --- | --- | --- |
| fast | 7 of 8 | 56 | **1.274** | 1.238-1.328 | 0.91-0.96 |
| slow | 1 of 8 | 8 | **1.871** | 1.843-1.920 | 0.994 |

Ratio 1.468. Latched per process, as the adapter campaign said: p6 is
slow in all four of its episodes and the other seven are fast in all of
theirs. No process straddles.

**So the shape of yesterday's finding survives and its numbers do not.**

| | adapter reading | span |
| --- | --- | --- |
| fast state | 1.176 | 1.274 |
| slow state | 1.776 | 1.871 |
| ratio | 1.51 | 1.47 |
| fast draws | 6 of 8 | 7 of 8 |

Recomputed against rotation, with solo 16u at 157.6 ms and 32u at 108.9:

    fast   paired 200.8 ms vs rotating 217.8 ms   partitioning  +8.5%
    slow   paired 294.9 ms vs rotating 217.8 ms   partitioning -26.1%

Against yesterday's +13.0% and -25.9%. The claim is the same claim and it
is 4.5 points smaller on the side that mattered.

An aside worth keeping: the slow state has the *higher* overlap fraction,
0.994 against 0.91-0.96. Whatever the two states are, the slow one is not
"they stopped running concurrently".

**What is now established, and by what.**

  * The self-paired co-run penalty is bistable and latches per process:
    1.274 or 1.871, seven of eight fast, no process straddling. Span
    instrument, eight processes.
  * The mismatched pairing is very nearly free -- 1.02 and 1.05 -- with
    no cross-episode transient. Span instrument.
  * There is a real slow phase at the start of every co-run episode:
    1684, 1683, 888 ms then 156 ms, the same shape in all four episodes
    of a run. It repeats per episode; it does not decay across them.

**What remains retracted.** The two-episode transient, the
sum-of-solo-steps account of it across five splits, the per-split settled
table, the 3:1 quantised outlier, and the absolute values in
``MEASURED_STEP_PAIR_EXTERNALITY`` and the two pairing constants. Those
were medians of a variable that held one number for a whole episode.

**One caveat on the solo baselines, stated rather than assumed.** They
come from the same adapter reading. They survive it for a specific
reason: ``run_solo`` changes width from the previous run, so the adapter
drains on its first step and latches a correct value for that width, and
the resulting numbers agree with Gate B's quota curves, which were
measured with a fresh adapter per point. That is an argument, and it
would be better as a measurement; the span instrument can produce one.

### 2026-08-09 -- solo measured the same way, and the verdict closes

The last dependency on the unreliable reading was the solo baseline every
externality is divided by. It came from ``adapter.last_step_seconds``,
and the argument for trusting it -- ``run_solo`` changes width, so the
adapter drains on its first step -- was an argument. ``solo_span``
measures it with the same events as the co-run.

Six processes, solo and co-run both by device span:

| | processes | ext | solo 16u | solo 32u | paired | rotating | partitioning |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fast | 5 of 6 | 1.299 | 154.6 ms | 112.0 ms | 200.9 ms | 224.1 ms | **+11.5%** |
| slow | 1 of 6 | 1.945 | 155.4 ms | 112.6 ms | 302.4 ms | 225.2 ms | **-25.5%** |

Across both span campaigns, twelve of fourteen processes draw fast.

The correction from measuring solo the same way is real but small: solo
at 32 units reads 112.0 ms by span against 108.9 by the adapter, 2.8%
high, which moves the rotation baseline the verdict is against. That is
the direction that makes partitioning look better, and it is worth saying
so explicitly rather than pocketing it: +11.5% here against +8.5%
computed with the old baseline and the same span externality.

``MEASURED_STEP_PAIRING_FAST`` and ``_SLOW`` are un-suspended and moved
to 1.299 and 1.945, with the rate at 12 of 14. The two 16+16 mismatched
entries in ``MEASURED_STEP_PAIR_EXTERNALITY`` are updated to their span
values, 1.022 and 1.054. **The other eight entries stay suspended**:
only 16+16 has been re-measured, and the four other splits are still
medians of the variable that held one number per episode.

**Where this leaves the claim.** Self-paired partitioning at 768 pays
+11.5% when the die grants the fast state and costs 25.5% when it does
not; the fast state is drawn twelve times in fourteen, latched per
process, and readable from an episode. Mismatched partitioning is very
nearly free on both sides. A scheduler that measures rides that; one
calibrated to a constant takes the draw. That is the same claim as
yesterday evening, arrived at through an instrument that has now been
checked against another, with the numbers it actually produces.

954 tests pass; freeze verifies.

### 2026-08-09 -- single SKU, by the user's decision

The blocking question was plan.md's main matrix: 4x RTX 4090 plus a
second SKU, with the NVIDIA line paused and plan.md stating explicitly
that the R9700 does not substitute for the second-SKU requirement. The
options put to the user were to migrate the matrix to AMD and rewrite the
cross-SKU clause, to restore NVIDIA only for the reduced 12-cell matrix,
or to accept a single SKU and state it in threat-to-validity. **The user
chose the single SKU and to continue on the AMD line.**

Applied to plan.md, not paraphrased into it. The second-SKU Phase 0 gate,
the "at least 9 of 12 cells agree in direction" acceptance criterion, the
week 11-12 smoke test, the week 13-14 reduced matrix, and the
4090-versus-second-SKU coverage item are all struck, each marked with the
date and the reason rather than deleted.

**What is given up, written the way it goes in the paper.** The
mechanism claim -- CU/SM mask partitioning plus a dual ledger -- will
have been verified on exactly one mask implementation, AMD's
``hipExtStreamCreateWithCUMask`` on gfx1201. Nothing in the results rules
out that they depend on that implementation's queueing and arbitration.
That is not a formality here: yesterday's measurements found two co-run
states drawn per process **on a single card**, so "does another vendor's
scheduler behave like this" is a live question, not a box to tick. The
threat-to-validity entry says that, in those words. No acceptance clause
replaces it, because cross-SKU agreement is a piece of evidence and more
cells on the same card is not a substitute for it.

**Two consequences that are engineering rather than wording, and both are
now blocking items before week 13.**

The schedule assumed four cards running the matrix in parallel. One card
serially is about four times the wall clock, and the two-week window very
likely does not hold the cell count as written. A measured single-cell
duration is required before week 13 starts, and the stated tie-break is
to cut cells rather than seeds -- seeds carry the confidence interval,
cells carry coverage, and they are not interchangeable.

The paired-comparison rule was "compare arms on the same card", against a
measured 3.49% inter-card bias. With one card that rule is vacuous and
the real confound moves to time: junction temperature rose 43 to 72.8 C
in three and a half minutes on 2026-08-08 and solo step time rose 5% with
it. So arms must be interleaved and paired in time, not run one arm to
completion and then the other. That is a stricter requirement than the
one it replaces, and it is written into the week 13-14 implementation
notes.

Memory budgets are restated rather than renumbered: 24/20/16 GB was the
4090's regime and the R9700 has 34.2 GB. The requirement stays "three
budgets spanning weights-resident, weights-barely-resident and
must-evict", with values to be justified.

### 2026-08-09 -- the first real cell, and a metric pinned at its ceiling

One cell of the main matrix on the card: SDXL urgent against a
CogVideoX-2b video stream, Poisson bursts at offered load 0.6, burst 4,
deadline slack 1.5, seed 0. Isolated service measured in-process: urgent
0.88 s per request with a 110 ms step p99, video 15.4 s with 517 ms.
Horizon sized to 40 urgent requests, 118 s, 42 requests.

**Wall clock, which is what the single-SKU decision needed:** 17 s model
load, 34 s isolated measurement, 107 s cell run, 159 s total. A primary
subset of 8 baselines x 2 loads x 2 bursts x 5 seeds is 160 cells, so
about 7 hours serially on the one card -- the two-week window holds it.
The full grid as written, 4 loads x 3 bursts x 3 slacks x 8 baselines x 5
seeds, is 1440 cells and about 64 hours, which it does not comfortably
hold alongside everything else in those two weeks.

**Three faults the run found, each recorded in the commit and fixed.**
Completions were read from the runtime's executors, but ``tick`` retires
a finished request itself; the cell reported 0 of 40 urgent done while
the ledger showed 1360 quota-seconds charged to that tenant. Idle rounds
spun the loop 4.9 million times in 106 seconds and made the decision p99
read 0.1 us by measuring empty ticks. And the deadline multiplied the
isolated *step* p99 rather than the request's isolated latency -- 0.110 s
against 0.87 s -- so every deadline was unmeetable and the miss rate was
exactly 1.0.

**Then the fixed cell said something about the experiment rather than
about the code.** Five policies on the identical cell:

    exclusive_fcfs           miss 0.950   video 0.563 steps/s
    static_even              miss 0.950   video 0.556
    deadline_aware           miss 0.950   video 0.559
    slo_aware_partitioning   miss 0.925   video 0.557
    probing_partitioning     miss 0.925   video 0.562

One request in forty separates the best from the worst, and the video
goodput spread is 1.3%. **The metric does not discriminate at this
parameterisation, and the reason is structural rather than statistical:**
a burst of four requests each needing 0.88 s alone carries 3.5 s of work,
against a deadline of 1.5 x 0.88 = 1.32 s. Three of every four urgent
requests miss on the whole die with no contention at all. No scheduler
can move a number that the arrival process has already decided.

plan.md's primary claim asks for a 20% relative reduction in urgent SLO
miss rate. That is unreachable from 0.94 regardless of the scheduler, so
the frozen deadline set {1.25x, 1.5x, 2.0x} -- chosen before any of this
was measured -- puts every cell of the primary claim at its ceiling.

**What is being done about it is a measurement, not a choice.** A sweep
of deadline slack at fixed burst, with two policies chosen to differ as
much as any pair in the set, finds the region where the miss rate
separates them. That region is what a pre-registration can honestly
freeze. Per-request latencies are now kept in every cell payload, so for
the baselines that do not read deadlines any slack can be evaluated
afterwards without re-running the card.

### 2026-08-09 -- with a burst-based deadline the cell discriminates

Same cell, same seed, deadline slack 1.5 against the burst's isolated
latency rather than one request's:

| policy | urgent miss | video goodput |
| --- | --- | --- |
| exclusive_fcfs (FCFS) | 0.750 | 0.568 |
| static_even (static SM partition) | 0.675 | 0.557 |
| deadline_aware (EDF) | 0.675 | 0.557 |
| slo_aware_partitioning | 0.550 | 0.557 |
| probing_partitioning | **0.525** | 0.557 |

Against the same cell with a per-request deadline, where all five sat
between 0.925 and 0.950.

**Which comparison is quoted matters more than the numbers, so all three
are here.** plan.md's primary claim is against the *strongest non-oracle
baseline*:

    vs FCFS                                    0.750 -> 0.525   -30.0%
    vs strongest baseline (static SM / EDF)    0.675 -> 0.525   -22.2%
    vs this method's own ablation, slo_aware   0.550 -> 0.525    -4.5%

The middle line is the one plan.md asks for, and it clears the 20% bar
with 2.2 points to spare. The first is the flattering one and is not the
claim. The third is worth keeping in view: most of the distance from FCFS
comes from partitioning at all, and the probe adds 4.5% on top of it --
which is the honest decomposition of where the gain lives.

Video goodput moves 0.568 to 0.557, down 1.9%, inside plan.md's 5%
allowance. So the first Pareto branch of the primary claim -- 20%
relative miss reduction with under 5% goodput loss -- is met.

**On one cell, one seed.** That is not the claim; it is evidence that the
experiment can produce the claim, which is what was in doubt this morning
when every policy sat at the ceiling. Five seeds and the frozen load and
burst set are what turns it into a result, and the wall clock for that is
measured: 159 s a cell, about 7 hours for the 160-cell primary subset.

### 2026-08-09 -- checking the null before believing the first group

The first complete group -- nine policies, load 0.6, burst 4, seed 0 --
came out ordered almost exactly as the policies were run:

    exclusive_fcfs 0.700, static_even 0.575, deadline_aware 0.425,
    step_matched 0.375, measured_pairs 0.450, slo_aware 0.375,
    probing 0.350, sticky 0.375, oracle 0.075

Position and quality coincided, and the order happened to run the
policies I expected to win last. That is the shape of a confound, so it
was checked rather than reported and rather than discarded.

**The check.** Isolated service and the deadline derived from it, by
position: 882 ms and 5.29 s at position 1, then 900-905 ms and 5.40-5.43
s for positions 2 through 9. So the first arm does face a 2.4% tighter
deadline -- a real bias -- but positions 2 to 9 face the same one to
within 0.6%, and their miss rates are not monotone in position: position
5 reads 0.450 against position 4's 0.375, and position 8 reads 0.375
against position 7's 0.350. The spread is policy, not position.

**Two faults fixed anyway, because both were mine and both are the same
kind.**

Isolated service is now measured once per group and shared by every arm.
The deadline comes from it, so measuring per arm hands whichever policy
runs first a colder card and a tighter window. Arms being compared must
face the same deadline or they are not the same experiment.

Policy order now rotates by group. A fixed order across twenty groups
confounds position with policy completely -- and this is precisely the
defect that cost a day when a repeat campaign varied seed with position
and no result in it could separate the two. Rotating makes the residual
average out and, more usefully, makes it measurable.

The two groups already run are kept as ``matrix_20260809_v1`` rather
than deleted. They are valid measurements of a design with a known bias,
and the bias is documented here.

### 2026-08-09 -- the frozen primary subset, and what it does not show

180 cells: 9 policies x 2 loads x 2 bursts x 5 seeds, 100% complete, 7344
urgent requests. Deadline 1.5x the burst's isolated latency, isolated
service measured once per group and shared by every arm, policy order
rotated per group.

| policy | kind | miss | video | urgent p99 |
| --- | --- | --- | --- | --- |
| oracle_shortest_remaining | oracle | 0.1911 | 0.5669 | 12.84 s |
| slo_aware_partitioning | method | 0.4559 | 0.5692 | 25.39 |
| probing_partitioning | method | 0.4568 | 0.5694 | 25.39 |
| step_matched_pairing | baseline | 0.4568 | 0.5693 | 25.44 |
| sticky_probing_partitioning | method | 0.4568 | 0.5695 | 25.40 |
| deadline_aware | baseline | 0.4768 | 0.5645 | 26.72 |
| measured_pairs_only | baseline | 0.4768 | 0.5645 | 26.75 |
| static_even | baseline | 0.4789 | 0.5645 | 26.74 |
| exclusive_fcfs | baseline | 0.4849 | 0.5696 | 26.43 |

**plan.md's first Pareto branch is not met.** Paired-seed bootstrap, 20
pairs, against each baseline:

    step_matched_pairing   +0.0000  [ 0.0000, 0.0000]    0.00%
    deadline_aware         -0.0200  [-0.0357,-0.0061]   -4.19%
    measured_pairs_only    -0.0200  [-0.0357,-0.0061]   -4.19%
    static_even            -0.0221  [-0.0401,-0.0064]   -4.61%
    exclusive_fcfs         -0.0281  [-0.0849,+0.0129]   -5.80%

The bar is 20%. The strongest baseline is beaten by nothing at all, and
the FCFS interval crosses zero. Video goodput moves +0.02%, so the second
half of the branch is satisfied and the first is not.

**The probe never fired.** ``probing_partitioning`` and
``step_matched_pairing`` agree to four decimals in every one of the
twenty configurations, at every seed -- a difference of exactly zero.
That is not a bug: ``Runtime._state_for`` does supply
``observed_step_seconds`` and ``observed_at_units``, and the probe
declines to fire because the pairings stayed under its threshold. Solo at
16 units predicts 0.1575 s, the threshold is 1.608x that at 0.253 s, and
a fast-state co-run measures about 0.20 s. Every pairing in the campaign
was in the fast state.

So in this workload the dual-ledger probe contributes nothing measurable,
and what separates the pairing family from the rest is step-matched
pairing itself. That is worth saying plainly: the mechanism this project
is named for did not do any work here.

**The serial fallback did, and it worked for the other side.** The drift
envelope fired about 10% of rounds for ``static_even``,
``deadline_aware`` and ``measured_pairs_only`` and never for the pairing
family, which looked like my own runtime manufacturing the comparison. It
is the opposite. With the envelope disabled, over three seeds at load 0.6
burst 4:

    static_even          0.5169 -> 0.6447   (+0.128)
    deadline_aware       0.5030 -> 0.6447   (+0.142)
    measured_pairs_only  0.5030 -> 0.6447   (+0.142)
    step_matched_pairing 0.4419 -> 0.4447   (+0.003)
    probing              0.4419 -> 0.4586   (+0.017)

The envelope rescues the baselines by a large margin and leaves the
pairing family alone, because the baselines form pairings whose cost the
model predicts badly. So the 4.5% reported above is what remains **after
this project's own safety mechanism has helped its opponents**; without
it the gap would be about 31%. Running it for every arm is the right
choice -- it is part of the runtime, not part of the policy -- and the
consequence belongs in the paper rather than in a footnote.

**What this changes about the claim.** As it stands the primary claim
fails at the frozen parameterisation. Three honest directions, none of
them "pick a different opponent":

  * the effect may live at parameterisations not in the frozen subset --
    the slack sweep already showed separation growing with slack, and
    load 1.05 and burst 2 are unmeasured here;
  * the probe needs a workload where pairings actually enter the slow
    state, which the latch measurements say happens in roughly one
    process in seven -- twenty groups drew fast in all twenty, which at
    that rate has probability 0.05;
  * the contribution may genuinely be the fallback and the step-matched
    pairing rather than the probe, in which case the paper should say so
    and the probe becomes an ablation that costs nothing and earns
    nothing here.

None of those is decided by data yet, and the frozen subset is what it
is. It is recorded as a negative result at this parameterisation.

### 2026-08-09 -- putting the probe somewhere it can act, and two corrections

The 180-cell campaign never fired the probe, and the reason is
structural. The matrix workload pairs SDXL against CogVideoX-2b; that
mismatched pairing measured 1.02 and 1.05 with no draw at all across four
processes, while the bistability the probe exists to catch was only ever
measured in **same-model** co-location -- self-paired 16+16 at 1.274 or
1.871, latched per process. A mismatched workload cannot exercise the
mechanism, so running more of it answers nothing. The long tenant is now
SDXL too.

Every process also measures, before any policy runs, which state it drew
-- by device span, on the pairing the runtime will actually form. It
costs seconds because one step identifies the state, and it turns "the
probe never fired" into "the probe never fired, and here is the state it
would have fired on".

**Correction one, found by running it.** Swapping the model and keeping
the step count made the video request 3.3 s instead of 15.4, and both
arms went to a miss rate of exactly 0.0. Offered load was still 0.6 by
construction, but what makes this workload hard is head-of-line blocking,
and that is set by a request's service time rather than its share of the
load. 140 SDXL steps is about 15.9 s, matching the CogVideoX request the
mismatched matrix used, so only the co-location changes.

**Correction two, and it is the important one.** With the workload hard
again, probing and step_matched separated: 0.400 to 0.300 at seed 0,
0.386 to 0.341 at seed 1 -- both processes in the fast state. It would
have been easy to report that as the probe working.

It is not evidence for the probe. ``probing_partitioning`` is
``slo_aware_partitioning`` plus the probe, and ``slo_aware`` is
``step_matched_pairing`` plus the deadline actions, so a gain from the
deadline actions is indistinguishable from a gain from the probe unless
the middle arm is present. It now is. And the arithmetic says the probe
should still be silent here: a fast-state co-run at 16 units measures
about 0.20 s against a 0.253 s threshold.

So the likely reading is that the deadline actions produce the gain and
the probe again does nothing -- which is a result, provided the
decomposition is measured rather than argued. Thirty seeds are running,
three arms each.

### 2026-08-10 -- the probe works, the deadline actions do not, and I predicted the opposite

Same-model co-location, 30 seeds, three arms so the gain can be
attributed: pairing only, pairing plus the deadline actions, and both
plus the probe. Paired-seed bootstrap over 30 pairs.

| segment | absolute | 95% CI | relative | excludes zero |
| --- | --- | --- | --- | --- |
| deadline actions | +0.0010 | [-0.0011, +0.0037] | +0.47% | no |
| **probe** | **-0.0195** | **[-0.0299, -0.0104]** | **-8.74%** | **yes** |
| both | -0.0185 | [-0.0285, -0.0096] | -8.32% | yes |
| video goodput | +0.0016 | | +0.06% | |

**This is the opposite of what I wrote down before running it.** The
prediction was that the probe would be silent because a fast-state co-run
at 16 units measures about 0.20 s against its 0.253 s threshold, and that
the deadline actions would turn out to be doing the work. The deadline
actions do nothing at all -- +0.47% with an interval spanning zero -- and
the probe carries the entire effect. Writing the prediction down first is
what makes that a result rather than a story; the middle arm is what made
it attributable at all.

**The mechanism, from the ledger rather than from reasoning.** Serial
fallbacks over the 30 seeds: 4880 for pairing alone, 4749 with the
deadline actions, **1902 with the probe**. The probe cuts the runtime's
serial fallbacks by 61%. It drops a degrading pairing itself, before the
drift envelope has to bail the round out with the whole die -- a cheaper
correction applied earlier. That is a different account of what the probe
is for than "it catches the slow state", and it is the one the data
supports here.

Per seed the probe is negative in 13 of 30 and exactly zero in 17. It
never makes a seed worse.

**Two things this does not establish, stated because they are easy to
elide.**

It is not plan.md's primary workload. The matrix pairs SDXL with
CogVideoX-2b; this pairs SDXL with SDXL, chosen precisely because the
mismatched pairing measured 1.02 and 1.05 and left the mechanism nothing
to act on. The honest sentence is "the probe is worth 8.7% under
same-model co-location", not "the matrix claim holds".

**All 30 processes drew the fast state**, 1.276 to 1.301. None drew slow.
The span campaigns saw 2 of 8 and 1 of 6, so 0 of 30 at that rate has
probability about 0.011. Either this process configuration suppresses the
slow state or the two measurements are not of the same thing. **The
probe's value in the slow state -- the case it was designed for --
remains unmeasured**, and that is an open question rather than a detail.

And 8.74%, interval roughly -13.4% to -4.7%, is still under plan.md's
20% bar.

### 2026-08-10 -- the slow state is one process in four, and the matrix never saw it

Thirty processes with the span instrument, self-paired SDXL 16+16, two
episodes each:

    fast  23 of 30   1.297   (1.287-1.303)
    slow   7 of 30   1.949   (1.923-1.962)

No overlap between the clusters, and both are tight. Pooled with the two
earlier span campaigns (2 of 8, 1 of 6): **10 of 44, 22.7%**, Clopper-
Pearson 95% [11.5%, 37.8%].

**So the matrix runner's state probe is broken, not the slow state
rare.** It reported fast in 30 of 30 processes; at a true rate of 22.7%
that has probability 0.0005.

**What this costs.** Yesterday's same-model result -- the probe worth
-8.74% -- was reported as being in the fast state on the strength of that
probe. The 8.74% itself is unaffected, since it is a paired difference
between two arms in the same process and does not depend on the label.
What is wrong is the attribution: those 30 processes very likely
contained about 7 slow ones, so the number is a mixture over both states
and not a fast-state result. The claim has to be restated as "over 30
processes drawn from the natural state distribution" until the split is
measured.

**A lead I recorded hours ago and did not follow.** The same-model span
measurement gave about 195 ms (1.25) in three processes that ran no
full-die solo, and about 300 ms (1.93) in one that did. I wrote that down
as a possible confound and moved on. The matrix's state probe never
creates a 32-unit stream at all -- it uses only ``for_quota(16)`` and
``disjoint_pair(16, 16)`` -- while this harness warms 32 in every
process. That is a concrete difference between the arrangement that never
draws slow and the one that draws slow 23% of the time.

Stated before running: **if masking the full die is what admits the slow
state, then processes that never create a 32-unit stream draw fast in
nearly all cases, and processes that do draw slow at about 23%.** Twelve
processes per arm, same seeds, the two arms alternating so any drift is
shared.

If it holds it is the first thing found that predicts the draw, after
four earlier explanations were proposed and retracted. If it does not,
the matrix probe's failure has some other cause and this is one more
refuted guess -- which is why the criterion is written down first.

### 2026-08-10 -- the full-die guess is not supported, and the state probe is being fixed first

Twelve processes per arm, same seeds, arms alternating:

    full-die mask created    1 of 12 slow  (8.3%)
    never created            0 of 12 slow  (0%)

The criterion written beforehand was 23% against 0%. Fisher's exact on
(1,11) against (0,12) gives p about 1.0, so **the guess is not
supported**. The one informative piece is thin: if the "never created"
arm had the same 23% rate, 0 of 12 has probability 0.045 -- but the
"created" arm returning only 1 of 12 undercuts even that.

**Not extending N to rescue it.** Adding processes after seeing an
underpowered result and re-running the same test is optional stopping. If
this is pursued it needs a confirmatory N fixed in advance and reported
separately from this pilot. It is a fifth candidate explanation for the
draw joining four already retracted, and it is parked, not adopted.

**The blocking problem is the state probe, and that is worth fixing on
its own.** The matrix runner measured the state before any warm-up and
with a 6-step episode, while the span harness that draws slow 23% of the
time warms 16 and 32 first and uses 14 steps with 4 dropped. Two
differences, and the second is disqualifying regardless of what triggers
the draw: the policies grant 32 units to a single runnable request --
``exclusive_fcfs`` always does -- so a probe that has never masked the
full die is not sampling the process the cells run in.

The probe now warms both widths first and uses the longer episode, capped
at the adapter's own schedule -- the urgent adapter holds 8 timesteps and
stepping past them indexed off the end of the tensor, which is what the
first fixed version did.

**It is being validated before it is used.** Twenty short processes that
run the probe and nothing else: if it reports slow at about 23% it is
measuring the same thing as the span harness, and the 30-seed same-model
campaign can be re-run and split by state. If it reports 0 of 20 again it
is still broken, and four hours of re-run would produce another set of
labels nobody can trust. Its absolute values differ from the reference
distribution -- 1.137 against 1.297 -- because the two tenants carry 8
and 140 timesteps rather than an identical 14, so the validation is about
whether the two clusters separate, not whether the numbers match.

### 2026-08-10 -- the state probe, validated, and what was wrong with it

The first fixed probe still reported fast in 20 of 20, but not flatly:
eighteen processes read 1.134-1.139 and two read 1.462 and 1.487. Two
clusters, both under the 1.57 threshold. The states were there and the
probe was halving them.

**The arithmetic said why, and it checks out exactly.** The probe paired
an 8-timestep urgent adapter against a 140-step video one, so the urgent
side finished first and the video side's remaining steps ran alone,
pulling its ratio toward 1.0. The reported value is the mean of the two
sides:

    fast   (1.30 + 1.0) / 2 = 1.15    measured 1.136
    slow   (1.95 + 1.0) / 2 = 1.48    measured 1.462, 1.487

Both predictions land. The probe was averaging each state with an
unmeasured solo run.

**With both sides running the same number of steps, it reproduces the
reference distribution.** Twenty processes:

    fast  13 of 20   1.292-1.296     reference 1.287-1.303
    slow   7 of 20   1.901-1.956     reference 1.923-1.962

7 of 20 against the span campaigns' 10 of 44 gives Fisher p about 0.25,
so the rates are compatible; pooled, **17 of 64 = 26.6%**.

That is a validated instrument rather than an assumed one, and the
validation was worth its 50 minutes: the previous version would have
labelled a four-hour campaign entirely "fast" for the second time.

The 36-seed same-model campaign is running with it, three arms, so the
probe's effect can finally be split by the state the process drew --
about 9 to 12 slow processes expected.

### 2026-08-10 -- the probe's effect, split by the state the process drew

36 seeds, three arms, with the validated state probe. 33 processes drew
fast, 3 slow.

| segment | fast (n=33) | slow (n=3) |
| --- | --- | --- |
| deadline actions | +0.62%, CI spans zero | -1.72%, CI spans zero |
| **probe** | **-9.75%, [-0.0292, -0.0112]** | -8.77%, CI spans zero |
| both | -9.19%, excludes zero | -10.34%, CI spans zero |
| serial fallbacks | 5498 -> 2158 (-61%) | 340 -> 130 (-62%) |
| video goodput | +0.04% | -0.01% |

**The fast-state result is now solid**: the probe is worth -9.75% on 33
paired processes with an interval excluding zero, and the deadline
actions are null. The slow state has only three processes, so its
interval spans zero and nothing is established there.

**The interesting part is that the two states agree.** The probe's point
estimate is -9.75% in the fast state and -8.77% in the slow one, and it
cuts serial fallbacks by 61% and 62% respectively. If the probe were a
slow-state detector its effect would be concentrated in the slow state
and near zero in the fast one. It is not. What it does, in both states
alike, is drop a degrading pairing before the drift envelope has to bail
the round out with the whole die.

That is a different mechanism from the one the design was argued from,
and it is the one the data supports. The probe should be described as an
early, cheap correction that pre-empts an expensive one -- not as a
detector for a bistable hardware state.

**An anomaly, recorded and not explained.** This campaign drew slow in 3
of 36. The probe validation, using the same probe in the same runner,
drew 7 of 20; the span campaigns drew 10 of 44.

    this campaign      3/36  =  8.3%   [1.8%, 22.5%]
    probe validation   7/20  = 35.0%   [15.4%, 59.2%]
    span campaigns    10/44  = 22.7%   [11.5%, 37.8%]
    all pooled        20/100 = 20.0%   [12.7%, 29.2%]

Fisher against the validation gives p = 0.025, against the other pooled
runs p = 0.037. So the rate is not stable between campaigns at a level
that chance covers comfortably. The validation runs differ from these
only in that their cells were empty -- the probe runs before any cell
either way -- so nothing in the obvious list explains it. It is recorded
as an open inconsistency in the draw rate, which matters because "one
process in five" is the number any claim about the slow state would rest
on.

### 2026-08-10 -- the draw rate is unstable, no predictor, and the runs had no clock

The slow-only campaign is drawing 0 of its first 14, where the probe
validation drew 7 of 20 with the same runner and probe a few hours
earlier. Across everything so far:

    span campaigns     10/44   22.7%
    probe validation    7/20   35.0%
    same-model v2       3/36    8.3%
    slow-only (partial) 0/14    0.0%

Fisher between the validation and the same-model campaign gives p =
0.025. The rate is not stable at a level chance covers comfortably, and
**no predictor of the draw has survived**: working set, power cap,
uncontrollable bistability, launch stagger, and now the full-die mask --
five proposed, five retracted or unsupported.

**A test I could not run, twice.** "Do the slow draws cluster in time" is
the obvious next question and the payloads carry no wall clock; a file's
mtime is when it was copied here. The one campaign with epoch markers in
its log puts its three slow draws at minutes 26.1, 111.3 and 125.2 of
245, with a fast draw between the last two -- no clustering visible, and
three draws cannot test for it either way.

``started_unix`` and ``started_iso`` are now in every cell and every
recorded draw. Asking the same question twice and finding the data
absent both times is what put them there, and it is the cheapest thing
in this entire log.

**What this means for the claim.** The fast-state result stands on its
own: the probe is worth -9.75% over 33 paired processes with an interval
excluding zero, and it works by cutting serial fallbacks 61%, not by
detecting a state. The slow state remains under-sampled, and a rate that
moves between campaigns means even "one process in five" is not a number
to build on yet. The slow-only campaign was pre-registered at 60
processes and will run to 60 regardless of what it draws -- changing it
now because the early draws are unfavourable is the same optional
stopping I refused three days running.

### 2026-08-10 -- both states measured: the probe works, and not for the reason it was built

The slow-only campaign ran its pre-registered 60 processes and drew 7
slow. With the 3 from the earlier campaign that is 10 slow-state
processes with full three-arm cells, against 33 fast.

| segment | slow (n=10) | fast (n=33) |
| --- | --- | --- |
| deadline actions | -0.57%, spans zero | +0.62%, spans zero |
| **probe** | **-10.87% [-0.0635, -0.0083]** | **-9.75% [-0.0292, -0.0112]** |
| both | -11.37%, excludes zero | -9.19%, excludes zero |
| serial fallbacks | 1341 -> 448 (-67%) | 5498 -> 2158 (-61%) |
| video goodput | +0.03% | +0.04% |

**The probe is established in both states, at the same magnitude, by the
same mechanism.** -10.87% and -9.75%; 67% and 61% fewer serial
fallbacks. The deadline actions are null in both.

That is a direct refutation of the argument the probe was designed from.
It was built to detect a bistable co-run state and stop pairing when the
die drew badly; if that were what it does, its effect would concentrate
in the slow state and vanish in the fast one. It does not. What it does,
identically in both, is drop a degrading pairing itself before the drift
envelope has to bail the round out with the whole die -- a cheap early
correction standing in for an expensive late one.

**The draw rate, pooled over 160 processes: 27, 16.9%, [11.4%, 23.6%].**
The between-campaign spread that looked alarming at 3/36 against 7/20 is
still there -- 8.3%, 11.7%, 22.7%, 35.0% -- and no predictor of the draw
has survived five attempts. But the rate no longer matters to the claim,
because the claim no longer depends on which state a process draws.

**What is and is not claimed.**

Established: under same-model co-location at 768, the probe reduces
urgent SLO miss rate by about 10% relative, in both co-run states, at no
cost to video goodput, over 43 paired processes. Its mechanism is a 61-67%
reduction in serial fallbacks.

Not established, and not to be elided: this is not plan.md's primary
workload. The matrix pairs SDXL with CogVideoX-2b, which measured 1.02 and
1.05 with no draw at all, and there the probe never fires and contributes
exactly nothing across 180 cells. The honest scope is same-model
co-location. And 10% is still under plan.md's 20% bar.

### 2026-08-10 -- the mismatched pairing re-measured, and the transient buried

Five splits, five processes each, three episodes, SDXL against
CogVideoX-2b, solo and co-run both by device span. Fifteen measurements
per entry, every range within 0.005.

| split | SDXL ext | CogVideoX ext | SDXL vs rotation | CogVideoX vs rotation |
| --- | --- | --- | --- | --- |
| 4+28 | 1.025 | 1.063 | **+71.4%** | +1.4% |
| 8+24 | 1.029 | 1.052 | **+65.3%** | +8.5% |
| 16+16 | 1.016 | 1.047 | +42.2% | +22.8% |
| 24+8 | 1.004 | 1.006 | +17.7% | +29.5% |
| 28+4 | 1.002 | 1.010 | +3.6% | **+30.9%** |

**Both tenants gain at every split**, and the figures land within a few
points of the retracted ones (+67.9/+1.7 through +2.6/+30.6). So the
settled values had been right; the eight suspended entries are
un-suspended and updated.

**The transient is definitively an artifact.** Per episode, all five
splits, the span instrument is flat to three decimals -- 4+28 reads
1.025, 1.025, 1.024; 28+4 reads 1.001, 1.001, 1.003. There is no
two-episode phase, and there never was one on the die.

That closes the most instructive error in this log. The transient came
with a quantitative account -- each side costing solo_a + solo_b for two
episodes -- that fitted five splits within 3-9% over a 3x range of the
sum. The fit was real and it was fitting a variable that held one stale
number per episode. A good fit to an artifact is still a fit, and the
only thing that caught it was running two instruments over the same
steps in the same process and finding one reported ten times the other
inside an interval that contained it.

**Debt cleared.** The suspended values are gone from the codebase; what
remains is measured on an instrument that has been checked against
another. That matters more than the numbers: a suspended entry sitting
in a table is worse than no entry, because the next reader sees a
complete table and only the comment says otherwise.

995 tests pass; freeze verifies.

### 2026-08-11 -- pre-registered coverage: overload and burst 2

The frozen primary subset covered load 0.6 and 0.85 at bursts 4 and 8.
plan.md's grid also has load 0.3 and 1.05 and burst 2, and the overload
point is where a scheduler should differ most. These are the cells that
close the grid: load 1.05 at bursts 2, 4 and 8, and loads 0.6 and 0.85 at
burst 2. Nine policies, five seeds, 25 groups, 225 cells, deadline 1.5x
the burst's isolated latency, policy order rotated per group, isolated
service shared within a group.

**Registered before running, because the temptation here is obvious.**
The primary claim failed on the frozen subset: probing beat the strongest
baseline by 0.00% with an interval of exactly zero, against a 20% bar.
These cells do not revisit that. If an effect appears at load 1.05 it is
a finding **about overload**, to be reported as one, and it does not
convert the frozen-subset result into a pass. Choosing the reported
parameterisation after seeing which one flatters is the failure this
whole log has been about; the frozen subset stays the primary claim
whatever these show.

What is expected: nothing in particular. The mismatched pairing measures
1.02 to 1.06 at every split, so the probe has almost nothing to act on in
this workload regardless of load, and the 180-cell result is likely to
repeat. If it does, that is worth having, because "the effect is absent
across the whole grid" is a stronger negative than "absent at four
points".

### 2026-08-11 -- the grid closed: the probe's value depends on whether tenants contend

225 coverage cells added to the 180 frozen ones: load 1.05 at bursts 2, 4
and 8, and loads 0.6 and 0.85 at burst 2. Nine policies, five seeds, 405
cells in total, all of plan.md's grid except load 0.3.

Paired-seed bootstrap, probing against each baseline, over the whole grid
(45 paired configurations):

    exclusive_fcfs         -0.0314  [-0.0710, +0.0002]   -6.46%
    deadline_aware         -0.0152  [-0.0253, -0.0059]   -3.23%
    measured_pairs_only    -0.0152  [-0.0249, -0.0064]   -3.23%
    static_even            -0.0125  [-0.0232, -0.0030]   -2.69%
    step_matched_pairing   **+0.0025  [+0.0000, +0.0056]   +0.56%**

**Against the strongest baseline the probe is worse, not equal.** On the
coverage cells alone it is +1.01% worse with an interval of [0.0000,
+0.0100]. That is small, and it is not noise: the interval excludes zero
on the wrong side.

It also makes sense. In the mismatched workload every pairing measures
1.02 to 1.06 -- nearly free -- so the probe has nothing to catch, but it
can still fire spuriously and drop a pairing that was fine. No benefit
available, a small cost paid.

**Put beside the same-model result, this is the finding.**

    mismatched co-location (SDXL x CogVideoX)   probe +0.56%, slightly harmful
    same-model co-location (SDXL x SDXL)       probe -9.75% fast, -10.87% slow

The probe's value depends entirely on whether the co-located tenants
contend for the same resources. Mismatched tenants do not -- that is why
partitioning them is nearly free and Pareto-dominates rotation at every
split -- and a mechanism for managing contention has nothing to manage.
Same-model tenants do, and there it is worth about 10%.

That is a sharper claim than the one the project set out to make, and it
is falsifiable in a way "the dual ledger raises utilisation" was not.

**plan.md's primary claim fails on the full grid.** Against the strongest
non-oracle baseline the method is 0.56% worse, where the bar is a 20%
improvement. The pairing family does beat the non-pairing baselines --
FCFS by 6.5%, though that interval touches zero, and static partition and
EDF by about 3% with intervals that exclude it -- so **step-matched
pairing** is what carries whatever the method has on this workload, and
at overload (1.05, burst 4) it beats FCFS by 17%.

Registered before these cells ran, and honoured: the overload result is
reported as a fact about overload, and does not convert the
frozen-subset failure into a pass.
