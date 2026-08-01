"""Phase 0: establish a TPC->SM oracle on CUDA 13.3 using global/next only.

No blind struct writes are performed here. If this phase fails, the whole
stream-offset effort stops.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, "/data/zhuoxu/alse/src")
sys.path.insert(0, str(Path(__file__).parent))

import probe_driver as d  # noqa: E402
from burstserve.gate_a_results import validate_masked_tpc_matrix  # noqa: E402

HERE = Path(__file__).parent
MODES = ["global", "next"]
BITS = [0, 31, 32, 63]
TRIALS = 3
BLOCKS = 256
ITERATIONS = 256
THREADS = 256
PHYSICAL_GPU = 0

raw = []
observations = []
failures = []

baseline = d.attempt("baseline", blocks=BLOCKS, iterations=ITERATIONS)
raw.append(baseline)
if baseline["outcome"] != "ok":
    print("BASELINE FAILED:", json.dumps(baseline, indent=2))
    sys.exit(1)
baseline_sm_count = len(baseline["sms"])
print(f"baseline: {baseline_sm_count} unique SMs, "
      f"uuid {baseline['report']['device']['uuid']}")

for mode in MODES:
    for bit in BITS:
        for trial in range(TRIALS):
            result = d.attempt(mode, bit=bit, blocks=BLOCKS,
                               iterations=ITERATIONS)
            raw.append(result)
            tag = f"{mode:6s} bit={bit:2d} trial={trial}"
            if result["outcome"] != "ok":
                failures.append(result)
                print(f"{tag} -> {result['outcome']} "
                      f"{result.get('status')} {result.get('error')}")
                continue
            histogram = result["report"]["observed_histogram"]
            observed_blocks = sum(histogram.values())
            observations.append({
                "mode": mode,
                "tpc_bit": bit,
                "trial": trial,
                "physical_gpu": PHYSICAL_GPU,
                "gpu_uuid": result["report"]["device"]["uuid"],
                "blocks": BLOCKS,
                "observed_blocks": observed_blocks,
                "observed_sms": result["sms"],
            })
            print(f"{tag} -> SMs {result['sms']}")

(HERE / "phase0_raw.json").write_text(json.dumps(raw, indent=2))

verdict = validate_masked_tpc_matrix(
    observations,
    matrix={
        "modes": MODES,
        "tpc_bits": BITS,
        "trials_per_cell": TRIALS,
        "allowed_observed_sm_count": [2],
        "iterations": ITERATIONS,
        "blocks": BLOCKS,
        "threads_per_block": THREADS,
    },
    hardware={"sm_count": 128, "expected_tpc_count": 64},
    baseline_observed_sm_count=baseline_sm_count,
)
(HERE / "phase0_verdict.json").write_text(json.dumps(verdict, indent=2))

print("\n=== validate_masked_tpc_matrix ===")
for name, ok in sorted(verdict["checks"].items()):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
print(f"  mapping: {verdict['tpc_sm_mapping']}")
for error in verdict["errors"]:
    print(f"  ERROR: {error}")
print(f"  ACCEPTED: {verdict['accepted']}")
print(f"  failed attempts: {len(failures)}")
