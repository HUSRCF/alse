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
