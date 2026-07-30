# Virtual-service currency toy bench

This dependency-free simulator probes accounting choices for burst-aware,
spatially partitioned diffusion scheduling. It is a semantic stress test, not a
performance model for a specific GPU.

Run:

```bash
python currency_bench.py --out results
python io_switch_bench.py --pcie-gen 4 --lanes 16 \
  --efficiency 0.70 --state-gb 20 --out results
python pcie_copy_bench.py --gpu 0 --size-mib 1024 --reps 8 \
  --out results/pcie_copy.json
```

It evaluates four candidate currencies:

- `wall`: observed wall-clock execution time;
- `sm_time`: allocated SM fraction multiplied by wall time;
- `fge_progress`: completed work expressed as predicted full-GPU-equivalent
  solo time;
- `dominant_time`: wall time multiplied by the largest of allocated SM,
  estimated HBM-bandwidth, and PCIe shares.

Experiments:

1. `quota_invariance.csv`: charge for one identical completed step under
   different SM quotas.
2. `contention_accounting.csv`: charge for identical completed work with and
   without residual shared-resource contention.
3. `boost_rotation.csv`: two permanently backlogged tenants share 25% base SMs
   each while a 50% boost rotates according to minimum virtual service.
4. `predictor_bias_sensitivity.csv`: sensitivity of progress fairness to a
   systematic profiling/model error.
5. `optimistic_edf.csv`: a full-GPU solo EDF feasibility check for the burst
   workload. The script rejects a workload that fails even this optimistic
   lower bound.
6. `burst_deadline.csv` and `burst_jobs.csv`: one long video plus four
   simultaneous urgent requests, comparing fairness-only selection with a
   least-slack guard.

The intended design interpretation is a two-ledger scheduler:

- use `fge_progress / weight` as the fairness ordering key;
- handle deadlines with predicted slack rather than modifying the progress
  currency;
- track SM/HBM/PCIe/memory occupancy separately for resource admission,
  pricing, and anti-gaming.

The distinction matters: FGE progress answers "how much useful diffusion work
did this tenant receive?", while SM/dominant-resource time answers "how much
scarce hardware did it consume?". No single scalar preserves both semantics
under nonlinear scaling and interference.

`io_switch_bench.py` separately models sequence-dependent residency changes.
It reports the one-way and evict-plus-load costs, the I/O fraction at different
rotation quanta, the minimum quantum needed to amortize a switch, and how much
state can fit inside a given SLO I/O budget.

`pcie_copy_bench.py` measures the transfer constants instead of assuming them.
Pin the process to the CPU/NUMA node local to the selected GPU when possible.
Its simultaneous test deliberately uses separate H2D and D2H streams, because
full-duplex PCIe signaling does not guarantee that a particular GPU/runtime
path sustains the sum of both one-direction rates.

Before transferring any numerical result to the real scheduler, replace the toy
speed curves with profiles measured for each `(model, step, tile G, SM quota,
corunner)` configuration.
