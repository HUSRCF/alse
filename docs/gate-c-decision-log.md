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
