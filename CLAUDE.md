# BurstServe / alse — what is established, what is not, and where the evidence is

Read this before proposing a claim, adding an arm, or reading a number
off a table. It is a map, not a summary: every line points at the file
that holds the evidence, and the wording of a claim here is the wording
the evidence supports.

## The one rule this project keeps relearning

**Choose the comparator before the measurement, and by rule.** Five
claims have been withdrawn; three of them because the criterion was
chosen after seeing the number. The current mechanism is
`matrix_results.BASELINE_POLICIES` / `METHOD_POLICIES` — a declared
split, not one inferred from names — plus a dated pre-registration per
experiment in `docs/prereg-*.md`.

It is still not enough. On 2026-08-26 the comparator set turned out to
be missing its floor for the whole life of the project: `exclusive_fcfs`
was read as "no partitioning at all" everywhere, and it is whole-die
**time-slicing between tenants** (`registry.rotate()` runs every round).
Strict priority — the obvious production policy — had never been run.
When adding an arm, read what the arm does rather than what it is called.

## Where things stand

### Established, intervals excluding zero

| | claim | evidence |
| --- | --- | --- |
| 1.5 | Partitioning two mismatched tenants costs 1.00–1.06 per side and **both** gain against rotation (4+28: +71.4% / +1.4%) | `docs/claims-and-evidence.md` |
| 1.6b | Masked partitioning beats two **unmasked** full-die streams: urgent miss +0.0333 [+0.0079, +0.0592] against it | `experiments/runs/unmasked_base`, 36 cells |
| 1.8 | Decision p99 16.6 µs; zero weight bytes after residency; an hour of load without leak | soak evidence |
| 1.9 | Run-time choice beats **whole-die time-slicing** and the best fixed split, at no video cost: −0.0687, **cluster bootstrap over 15 seeds [−0.1521, −0.0044]**; vs `fixed_split_8` [−0.2040, −0.0276]; video a wash | Experiment A3, `experiments/runs/expA3`, 120 cells |
| 1.10 | The partitioning gain has an **optimum width on CDNA2 and none on RDNA4** — aggregate solo throughput peaks at four ways on gfx90a and falls below the whole die at eight; the Amdahl form the cost model assumes is **refuted** there | `experiments/probes/gfx90a/`, 2026-09-03 |
| 1.11 | The co-run penalty **counts peers, not busy die**: the same slice with N−1 peers filling the same units costs +14.5% at four ways and +54.4% at eight over the pairwise entry. A pairwise table cannot express it, and every intra-tenant arithmetic here used one | `experiments/probes/gfx90a/nway/`, 2026-09-04 |

1.9's effect is **asymmetric in magnitude, not frequency**: 13 wins, 8
losses, 9 exact ties, sign test p = 0.383 — but the worst loss is +0.046
while five wins exceed 10 points and two exceed 60. Nearly free when
wrong, occasionally decisive when right. Report the shape, not the mean.

**The independent unit is the seed, not the cell.** `build_trace` seeds
its generator with `spec.seed` alone, so one seed at load 0.6 and at load
1.05 is *the same arrival sequence rescaled in time* — verified, the
inter-burst gap ratio is constant to 1e-9. Every paired comparison in
this project therefore has half the independent units it appears to.
1.9's interval above is the cluster bootstrap over 15 seeds; the naive
cell bootstrap gives [-0.1361, -0.0161] and is too tight. Experiment A's
verdict 3 did **not** survive this correction — see 2.3.

### Not established

* **2.1** plan.md's primary Pareto claim fails on the 405-cell grid: the
  METHOD is 0.56% *worse* than the strongest baseline where the bar is a
  20% improvement. Its hedge — that the grid is three-quarters no-op —
  was closed by Experiment A rather than cashed: on the backlog workload
  it nominated, the answer is the same.
* **The adaptive policies have two actions, not five.**
  `step_matched_pairing` returns `{first: 16, second: 16}` or
  `{one: 32}`, and `deadline_aware` the same pair. The action space is
  `{16+16, 32+0}`. Meanwhile 1.5 measures **five** splits, all of which
  Pareto-dominate rotation, with the most asymmetric giving the largest
  urgent gain (4+28: +71.4%). **The policy never issues the grant the
  hardware evidence says is best.** Nothing measured so far tests dynamic
  quota selection; it tests switching between an even split and
  exclusive.
* **2.2 / 2.3** The probe and SLO-aware layer add nothing over
  step-matched pairing. Across five evaluations they are
  **indistinguishable**: 26 of A3's 30 configurations are exactly
  identical and four cells decide the sign. No confirmed benefit, no
  confirmed harm. Do not write "the method beats" or "the method loses";
  both have been wrong here.

### Withdrawn 2026-08-27: the scheduling claim, by the floor that was missing

**Strict priority beats the scheduler.** `exclusive_priority` — whole die
to the deadline-carrying tenant whenever it has work, no partitioning
ever — beats `step_matched_pairing` on urgent miss by **+134%** under
arrivals and **+84.5%** under backlog, intervals excluding zero under the
seed cluster bootstrap. Better in 15 of 20 cells, tied in 3. Under
arrivals it **dominates**: identical video goodput, identical Jain. The
fairness defence was named in the pre-registration before the run and is
not available. See 3.6.

**Both named defects were then tested and neither explains it (3.7).**
Experiment 2x2, 240 cells, barrier `{on, off}` x action space `{2, 6}`,
opponent `exclusive_priority` in every group, pre-registered verdict 3.
Every cell loses to priority by +83% to +434%, intervals excluding zero;
the barrier factor moves nothing (all four paired differences cross zero,
none above 0.016); and six actions are *far worse* than two.

The cleanest argument does not need the barrier fix to work at all: in
backlog `step_matched_pairing` runs **1.000 steps per round** — it never
pairs on this workload, so it is already barrier-free — and it still
loses by 83-107%.

Why six actions lose: `deadline_quota` gives urgent **more** die-seconds
than priority (45.0% vs 40.8%) and completes the same requests, at miss
0.99 against 0.12. **Die-seconds are not the currency a deadline is paid
in.** Choosing the smallest quota that "makes it" on isolated predicted
costs under-provisions systematically once co-run is real.

**So on this workload the decision that matters is which tenant gets the
die, not how it is divided.** In backlog neither adaptive policy
partitions at all; they differ from priority only in picking by
accumulated deficit rather than by deadline.

**Corrected the same day: the 2x2's pipelining cap was set from the wrong
ratio.** It was 8, justified from 16+16's 4.6x. The asymmetric splits
reach 12.6x (24+8) and 25.8x (28+4). With the barrier on, **no split can
meet the 5.34 s burst deadline at all** -- 17.7 s to 99.7 s for a 32-step
burst. With it off and a cap of 12, `24+8` finishes in **4.7 s, inside
the deadline**; at the cap of 8 it takes 6.3 s and misses. The
`step_matched_pairing` result is unaffected (it never pairs), but the
six-action barrier-off cell was not properly tested. And
`deadline_quota`'s rule is backwards under pipelining: a *larger* urgent
quota buys more steps per round, so the burst finishes fastest at 24+8,
not 4+28.

### The second SKU, 2026-09-03/04

The single-SKU decision was reversed by the user on 2026-09-03. The
second SKU is **DiamondHill, 8x MI250X (gfx90a, CDNA2, 104 CU per GCD)**,
reached as `ssh pc@10.120.16.9`; it is not NVIDIA, so Gate B's NVIDIA
text stays withdrawn. Two cost tables were measured there and **no
scheduling cell has ever run on it**.

**The quota curve refutes the model's functional form.** `fit_quota_latency`
returns a *negative* serial term for both models, and the refutation does
not need the fitter: under `latency = serial + parallel/q` the
unit-seconds `q x t(q)` must rise with `q`, and on gfx90a they fall from
13 to 26 units. Leave-one-out MAPE 24.9% and 28.1% against 5.6% and 4.6%
on gfx1201, **where Gate B's bar is 10%** and the noise here is 0.11%.
`serial_fraction` is recorded as 0.0 and that is **not a fit**; the
measured quota set is closed under complement so the fit stays
unreachable.

**The co-run penalty travels; the scaling does not.** At the same die
fractions the two architectures' externality tables agree to within 0.039
absolute and 3.6% relative:

    fraction   1/8     1/4     1/2     3/4     7/8
    gfx90a   1.3559  1.2770  1.2176  1.1016  1.0317
    gfx1201  1.3383  1.3070  1.2367  1.1259  1.0706

So whatever produces the penalty is **not** what makes gfx90a's
efficiency peak at a quarter of the die. That says which table needs
re-measuring per SKU and which does not.

`MEASURED_*` and `externality()` both take a `device` now, and a device
with no table **raises rather than falling back**.

### Running now

* **expC run 2** on X570 (`docs/prereg-intra-tenant.md`), 160 cells.
  Run 1 was stopped at group 28 of 40 and preserved as
  `experiments/runs/expC_run1_overwritten/`: its four arms each ran in
  their own process, `run_amd_matrix_cell` substituted `POLICY` into
  `--out` only when given *more* than one policy, so every arm of a group
  wrote to the same path and the last overwrote the other three. Both
  guards looked at the wrong name. Fixed in the library
  (`matrix_results.output_path`) and in the campaign, pinned by a test.
* **A 300 s-window replication of the eight-way point** on gfx90a. The
  sweep itself is done (1.11): the eight-way cell is its weakest, with 5
  to 7 samples per slice inside the overlap window and sd 0.12, and the
  replication is kept **beside** it rather than replacing it.
* **Queued behind expC: the same sweep on gfx1201.** That is the number
  `prereg-intra-tenant.md`'s prediction actually turns on, and it is
  unmeasured. Nothing is synced into X570's tree while run 2 is in
  flight, or run 2's attestation would describe a tree that no longer
  exists.

**What 1.11 does to the one open path.** 3.8 left intra-tenant
concurrency as the only way to shorten a serial burst. On gfx90a, whole
die, burst of four: serial 3.83 s, two ways **3.55 s**, four ways
3.70 s. (Eight ways is unreachable at a burst of four -- the policy takes
`critical[:concurrency]` -- and at a burst of eight it costs **14.30 s**
against 7.66 s for not splitting.) The optimum is **two, not four**, and
the gain over serial is **7.3%** where the pairwise stand-in promised
18.5%. The stand-in erred in the direction that flattered the mechanism,
and by more the further it was extrapolated.

### The structural result, 2026-09-03

**No spatial split can meet this workload's burst deadline, at any
pipelining cap.** The registry offers one request per tenant per round,
so a burst of four is serial whatever the split, and an 8-step request
cannot use a budget larger than 8. Best partitioned burst **6.30 s**
(`24+8`), exclusive **3.70 s**, deadline **5.34 s**. See 3.8.

**It is a program now, and it holds on both architectures
(2026-09-04).** A hand-computed table cannot notice a cost table changing
under it, and `trace_sim` has just gained a device dimension, so the
arithmetic is `scripts/burst_feasibility.py` with every published row
pinned to 10 ms by a test -- including "raising the cap cannot help",
which is cap 16 and cap 256 agreeing to nine places. With each device's
own measured curves **and** its own measured co-run penalty:

    gfx1201  best partitioned 8.23 s vs deadline 5.54 s   +48.4%
    gfx90a   best partitioned 7.42 s vs deadline 5.75 s   +29.2%

The *fraction* travels -- three quarters of the die is the best split on
both -- and the margin does not. Before the penalty was measured, gfx90a
was over by only 1.2%, and the bar for it to join gfx1201
unconditionally was stated in advance as a penalty above 1.012. It is
1.2770.

That reframes 3.6 and 3.7: they are not "the scheduler was bad". Under
this runtime spatial partitioning cannot meet this SLO by construction,
and every campaign that measured it losing was measuring that. Making
partitioning viable for deadline-bound bursts needs **intra-tenant
concurrency**, which this runtime does not do and no experiment here has
tested.

**Three times now the same error has been made, twice by us and once by
me in this file's own derivation:** computing a deadline in a currency
that is not time-to-completion. Die-seconds (3.7), unit-seconds (the
boundary derivation), and per-request feasibility against a per-burst
deadline (`pipelined_quota`, 3.8). When a policy or a derivation reasons
about a deadline, check what unit the deadline is actually on.

## Gates

* **Gate A** — **accepted**, and it is Gate A-AMD. All clauses passed
  2026-08-02 (`plan.md` decision `gate-a-amd-passed`); the single-SKU
  decision of 2026-08-09 made the AMD clauses the only Gate A. 192 masked
  cells, all 32 mask bits mapped, 10,000 action switches with readback,
  latent byte-identical to an unswitched reference — see 1.1. The
  NVIDIA-worded Gate A is historical record and does not count as
  incomplete.
  *(Corrected 2026-08-27: this line previously read "`missing`. No masked
  kernel has run", copied from the stale 2026-07-30 alignment row, which
  was about the NVIDIA wording. It was false in both halves.)*
* **Gate B / Gate B-AMD** — **not accepted**. Both models sit at 9 of 10
  with `accepted: false`, failing the same clause,
  `cold_model_predicts_transfer_and_framework_separately`. SDXL: transfer
  4.08% and total 7.36% pass, framework **88.5%** fails. CogVideoX-2b:
  transfer **83.9%**, total **67.3%**, framework never calibrated. The
  single-SKU decision was extended to Gate B on 2026-08-26, narrowly:
  `G={1,4,8,16}`, pinned/pageable, NUMA/PCIe direction and the
  compute/HBM/PCIe probe co-runners are vendor-neutral and stay
  `missing`.
* **Gate C** — `exact`, **simulation only**. No GPU ran in it.
* Stage alignment: `docs/alignment-weeks-3-4.md` (hashed, referenced from
  plan.md). exact 9 / approximate 4 / missing 3 / not_applicable 2.

The plan's calendar and the work have diverged in both directions: Gate C
is done early, hardware runtime experiments from weeks 9–12 have been
running for weeks, and Gate B has not closed. A stage label is not a claim
about what was gated when.

## Where evidence lives

    scripts/analyse_campaign.py      every campaign number, from the raw cells
    scripts/burst_feasibility.py     3.8's table, as a program
    docs/claims-and-evidence.md      every claim, its interval, its falsifier
    docs/gate-c-decision-log.md      the running record, including corrections
    docs/prereg-*.md                 one per experiment, dated, before the run
    docs/alignment-weeks-3-4.md      requirement-to-evidence table
    docs/attestations/*.sha256       which code produced which campaign
    experiments/runs/                raw cells (gitignored, never deleted)
    experiments/runs/expA_analysis/  the analyses, regenerable by script

Campaigns: `expA` (envelope on), `expA2` (envelope off), `expA3`
(priority-free arrivals replication, 15 disjoint seeds), `expP` (the
priority baseline). `load03` is paused at 85/135 and `urgent30` at 0/45 —
coverage backfill, deliberately deprioritised.

## How work is run here

* Hardware campaigns run on **X570** (`gfx1201`, R9700, 32 maskable
  units), reached as `ssh X570` from dd. Its tree is a **partial working
  copy** — git HEAD is old and newer files were rsynced in, so
  `git status` is meaningless there. `docs/attestations/*.sha256` pins
  the code instead; capture one before every campaign.
* `src/burstserve/runtime.py` on X570 is `139b944^` — **before** the
  `max_steps_per_round` flag — deliberately, so all campaigns are
  commensurable. The round-barrier fix is default-off and unmeasured on
  hardware.
* Recheck the card immediately before any run. **Verify a stop from a
  separate connection**: a `pkill` confirmed by the shell that issued it
  once reported success while the runner was still at 100%.
* Never delete a raw run, including a failed one.
* **Analyse with `scripts/analyse_campaign.py`, not by hand.** The seed
  cluster bootstrap, the win/loss/tie counts, the sign test, the
  fast-state split and the grant counters were computed ad hoc, once per
  campaign, outside version control until 2026-09-04. Committing them
  regenerated 1.9's three intervals, 3.6's +134.0% / +84.5% and its
  "15 of 20, tied in 3", all eight rows of 3.7 and all four of 3.8 --
  and found three defects doing it: a cluster ordering that moved a
  published bound, a configuration key that could not separate arrivals
  from backlog and silently halved any pooled comparison, and a key that
  *over*-separated and would have paired nothing in expC. A published
  number that cannot be regenerated is a number on trust.
* **`grant_shapes` written before 2026-09-04 is sorted by width, not by
  tenant.** expB shows `24+8` in shapes and `8` in the urgent-quota
  histogram and both are right. The histogram is the authority for any
  question about which tenant got what.
* dd's gh account has **READ** on `HUSRCF/alse`; X570's has ADMIN.
  Pushes go via X570 (bundle → bare clone → push), never by moving a
  token.
