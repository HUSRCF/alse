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
