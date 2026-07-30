# BurstServe research prototype

This workspace is implementing the staged roadmap in [`plan.md`](plan.md) for
deadline-aware virtual-service scheduling of bursty diffusion workloads.

The current implementation is in Phase 0. The original ASLE source is imported
under `vendor/asle` and identified by `vendor/ASLE_SOURCE.json`; new work belongs
under `src/burstserve`.

## Phase-0 checks

Run the standard-library test suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Verify that the imported baseline still matches its source archive:

```bash
PYTHONPATH=src python3 -m burstserve.vendor_import \
  --repo-root "$PWD" \
  --verify-only
```

Capture the active runtime and hardware:

```bash
PYTHONPATH=src \
/data/zhuoxu/miniconda3/envs/aris-vllm/bin/python \
  -m burstserve.environment \
  --repo-root "$PWD" \
  --model-root /data/zhuoxu/models \
  --output experiments/environment/phase0_4090_20260730.json
```

Run the deterministic tiny ASLE control cell on an idle physical GPU:

```bash
PYTHONPATH=src \
/data/zhuoxu/miniconda3/envs/aris-vllm/bin/python \
  -m burstserve.asle_runner \
  --repo-root "$PWD" \
  --physical-gpu 1 \
  --arm stepswap
```

Use `--arm offload_tiled` for the matching ASLE/MOSAIC smoke after the control
passes. The runner rejects a GPU with more than 1 GiB already allocated, gives
every semantic cell a deterministic run ID, never overwrites a prior run, and
stores its manifest, events, command, stdout, vendor logs, summary, and outcome
under `experiments/runs/`.

Capture a real final latent for repeatability or cross-mode comparison:

```bash
PYTHONPATH=src \
/data/zhuoxu/miniconda3/envs/aris-vllm/bin/python \
  -m burstserve.correctness_runner run \
  --repo-root "$PWD" \
  --physical-gpu 1 \
  --mode stock \
  --trial 0
```

Repeat with `--trial 1`, then compare the two returned run directories:

```bash
PYTHONPATH=src python3 -m burstserve.correctness_runner compare \
  experiments/runs/<first-run-id> \
  experiments/runs/<second-run-id> \
  --output experiments/aggregates/stock_repeat.json
```

Same-mode repeats require exact latent SHA equality. Cross-mode comparisons
report both hashes and float64 difference statistics but deliberately do not
turn those numbers into an unregistered correctness threshold.

Aggregate all complete, failed, timed-out, incomplete, and invalid runs:

```bash
PYTHONPATH=src python3 -m burstserve.results \
  --run-root experiments/runs \
  --output experiments/aggregates/phase0_runs.json
```

All model execution is offline. The first smoke uses the provisional
`aris-vllm` environment (Python 3.11, Torch 2.11/CUDA 13.0, Diffusers 0.38);
its exact package inventory is embedded in the environment snapshot and every
run manifest.
