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
* dd's gh account has **READ** on `HUSRCF/alse`; X570's has ADMIN.
  Pushes go via X570 (bundle → bare clone → push), never by moving a
  token.
