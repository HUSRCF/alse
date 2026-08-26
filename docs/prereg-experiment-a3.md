# Pre-registration: does run-time choice beat not partitioning at all?

Written 2026-08-26, after Experiment A completed and before any cell of
this one was run. A's cells are not reanalysed here and A's reported
result does not change: this is an independent replication on disjoint
seeds, not a continuation. Adding seeds to a campaign whose interval
crossed zero, and stopping when it stops crossing, is optional stopping;
that is why the seeds here are new, the count is fixed below, and the
verdict is whatever lands at that count.

## Why this question and not the one A asked

A's comparators were the five fixed splits, and its floor was
`exclusive_fcfs` -- no partitioning at all, the whole die to one tenant
at a time. The floor is not a floor. Under arrivals at load 0.6 it beat
every fixed split in both campaigns. So A's one verdict-3 result was
measured against a comparator set that excludes the arm which actually
performs best in that regime, and against that arm the same win does not
exclude zero:

    A1 arrivals   step_matched - exclusive_fcfs   -13.6%  [-0.2086, +0.0173]
    A2 arrivals   step_matched - exclusive_fcfs   -15.9%  [-0.2182, +0.0050]

This is the question a reviewer asks next and the one that decides
whether the paper can say run-time choice is worth anything under the
grid's own arrival pattern.

The backlog regime is not run here. There the same comparison is
-56.9% and -58.7% with intervals far from zero, at a cost of 25% of
video goodput. That is a trade-off to state, not a question to settle.

## What A's ten differences actually look like

The mean is not the phenomenon. A2's ten paired differences, sorted:

    -0.5833  -0.2500  -0.1500  0.0000  0.0000  0.0000  0.0000
    +0.0227  +0.0227  +0.0250

Three configurations carry a win larger than 5 points; four are exactly
zero; three are slightly against us. A sign test over the six non-tied
pairs is 3 against 3, p = 1.0. The mean is negative because one
configuration is -0.5833.

That configuration is load 1.05, seed 2, and it gives **-0.5833 in A1 and
-0.5833 in A2** -- the same number to four decimals with the drift
envelope on and off. Load 0.6 seed 2 gives -0.2500 in both. The effect
is a deterministic property of those traces, not thermal noise, and that
is what makes it worth measuring more of rather than averaging away.

So this experiment has two questions, and the second is not subordinate:

1. Does the paired interval exclude zero at a pre-declared n?
2. Is the win concentrated in a minority of traces, and what do they
   have in common?

## Design

Regime: **arrivals only** (the grid's own trace, unchanged).
Envelope: **off**, `--drift-tolerance 1000000`. A2's configuration and
this project's best known one; claim 3.3 established the envelope is a
net cost.
Workload: unchanged from A -- urgent SDXL 8 steps, video CogVideoX-2b
30 steps, deadline base `burst`, slack 1.5, 40 urgent requests nominal,
burst 4.

    load   0.6, 1.05
    seeds  5 6 7 8 9 10 11 12 13 14 15 16 17 18 19   (15 new, disjoint from A)

**n = 30 paired configurations.** Fixed here. The campaign is not
extended if the interval crosses zero and not truncated if it does not.

Four arms, all in one process per group so they share the drawn co-run
state and the thermal history:

    exclusive_fcfs           the comparator this experiment is about
    step_matched_pairing     M, by A's rule in all four of its evaluations
    slo_aware_partitioning   0.4097 against 0.4130 in A2; at ten pairs the
                             choice of M between these two was not
                             resolved, and if it flips here that is
                             reported rather than hidden by the rule
    fixed_split_8            static-deploy in every one of A's four
                             evaluations; carried so this campaign
                             independently replicates A's own comparison

`deadline_aware`, the five other splits and the oracle are dropped. A
measured them; none is the comparator here, and each costs about
40 minutes of card time per group.

## Metrics and direction

Primary: urgent deadline miss rate, lower is better. An unfinished
request counts as a miss and the denominator is admitted urgent
requests, as in A.
Secondary: video goodput in steps per second of cell run.
Paired by (load, seed); 95% CI by bootstrap over the paired differences,
10000 resamples, seed 0.

## Verdicts, fixed in advance

On `step_matched_pairing` minus `exclusive_fcfs`, miss rate, 30 pairs:

1. **Run-time choice beats no partitioning under arrivals.** The
   interval excludes zero on the negative side *and* video goodput is no
   worse than -5% relative. Only this supports the sentence "the
   scheduler is worth something on the workload commensurable with the
   405-cell grid".
2. **Not established.** The interval crosses zero. Then the arrivals
   regime is reported as unsettled at 30 pairs, the point estimate and
   the distribution are both given, and the paper's positive claim rests
   on the backlog regime alone -- where it costs 25% of video goodput.
3. **No partitioning is better.** The interval excludes zero on the
   positive side. Reported as a negative result alongside the five
   already withdrawn.

Video goodput is checked in all three, on the same -5% point-estimate
rule A used, and the rule is not relaxed if it is what fails.

## The distribution, reported whatever the interval does

Declared here so that a concentrated win is described as concentrated
rather than as an average:

* the count of configurations with a win larger than 5 points, and with
  a loss larger than 5 points;
* the number of exact ties;
* a sign test over the non-tied pairs.

If fewer than a third of configurations carry the win, the result is
stated as "the win is carried by a minority of traces" together with the
interval, not instead of it.

## Predictors, declared before the run so this is not fishing

Four candidate properties of a trace, and only these four. Pearson r of
each against the paired difference, all four reported whatever they
show:

    urgent request count      r = +0.704 at A2's n = 10
    video request count       r = -0.490
    horizon in seconds        r = -0.103
    coexistence fraction      r = -0.012

None of these is claimed. The first is the only one that looks like
anything and it is driven by the single seed-2 configuration, which has
the fewest urgent requests of any seed. The coexistence fraction --
this project's own explanation for why the 405-cell grid compresses the
policies -- is the one that would have been most convenient, and at ten
pairs it explains nothing.

**No predictor outside this set will be reported as a finding.** If
something else in the traces turns out to separate the winners, it gets
its own pre-registration and its own seeds.

## Code identity

A3 runs on the same X570 tree that ran A2, deliberately: the round-barrier
fix (`max_steps_per_round`, branch `round-barrier`) is **not** in it, so
A3's cells are commensurable with A2's and the fix is measured
separately. `src/burstserve/runtime.py` there is `139b944^`.

That tree's git HEAD is `5415ed1` and the files added since were rsynced
rather than checked out, so git reports them untracked and "clean tree"
means nothing on that host. `docs/attestations/x570-tree-expA.sha256`
pins all 119 Python files by sha256 instead, captured after A1 and A2
finished and before A3 was launched. Only the two new files this
experiment needs -- its campaign script and its analyser -- are copied
over, and neither is imported by the runtime.

## What would make this experiment invalid

* Any safety failure in any round; the runner aborts the cell already.
* Fewer than 15 seeds landing at a given load; the pairing is per
  configuration and an unpaired cell cannot enter the bootstrap.
* `step_matched_pairing` and `exclusive_fcfs` producing identical miss
  rates in more than 24 of the 30 configurations. Then the comparison
  rests on fewer than six points and no interval computed over it means
  anything -- which is the defect claim 2.2b describes and 2.2 was
  withdrawn for.
* The drawn co-run state is reported per group. All twenty of A's groups
  drew `fast`; if that repeats, the claim is scoped to the fast state and
  says so, as A's is.

## Why 30 and not more

A planning estimate that treats A2's ten differences as the truth gives
about 84% power at 20 pairs and 96% at 30. That estimate is optimistic
-- it assumes one trace in ten reliably gives -0.58 -- so it is a budget
rationale rather than a promise. Thirty groups is about 3.7 hours of
card time at A2's measured 7.3 minutes per four-arm group. The verdict
is whatever lands at 30.

## Note, 2026-08-26, written after the campaign and before the analysis

A3's design describes `exclusive_fcfs` as "no partitioning at all", the
wording inherited from Experiment A's pre-registration. That wording is
wrong about what the arm is, and the error was found by reading the code
on the day A3 completed, not by the result.

`TenantRegistry.ready` returns one candidate per tenant in `self._order`
and `Runtime.tick` ends with `registry.rotate()`, so the request
`exclusive_fcfs` hands the whole die to alternates between tenants every
round. It is **whole-die time-slicing between tenants**. It is neither
first-come-first-served nor priority.

Nothing about the campaign or the verdicts changes: the arm that ran is
the arm that was pre-registered, and the criterion, the direction and the
three verdicts are untouched. What changes is the English sentence the
verdict licenses. A3's verdict 1 means "run-time choice beats whole-die
time-slicing", not "beats not partitioning at all". Strict priority is
pre-registered separately in `docs/prereg-priority-baseline.md` and has
not been run.
