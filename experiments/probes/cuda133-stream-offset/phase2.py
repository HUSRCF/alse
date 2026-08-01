"""Phase 2: try to falsify the Phase-1 stream-offset candidate.

A single bit landing on two SMs is not evidence: a wrong offset can write into
a field that happens to restrict. The candidate only survives if, across every
declared bit and trial, `stream` produces exactly the same TPC->SM mapping as
the two callback mechanisms, which do not depend on this offset at all.

That is why all three modes are run here rather than stream alone: the two
callback modes are the independent witnesses, and `validate_masked_tpc_matrix`
is what refuses to call the result a mapping unless they agree.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, "/data/zhuoxu/alse/src")
sys.path.insert(0, str(Path(__file__).parent))

import probe_driver as d  # noqa: E402
from burstserve.gate_a_results import validate_masked_tpc_matrix  # noqa: E402
from burstserve.nvml_events import NvmlXidMonitor  # noqa: E402

HERE = Path(__file__).parent
GPU0 = "GPU-243d044f-1fa5-4efc-55ef-e456a11bde7e"
NVML = Path("/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.610.43.02")
NVML_SHA = "2dc828b3f5027f98e05c7607c1d8129d11bd28de4c2091c5cd7e32dbc21ec172"
NVML_VERSION = "13.610.43.02"

CANDIDATE_DELTA = 280           # total offset 0x5fc
MODES = ["global", "next", "stream"]
BITS = [0, 31, 32, 63]
TRIALS = 3
BLOCKS, ITERATIONS, THREADS = 256, 256, 256
PHYSICAL_GPU = 0

raw, observations, failures = [], [], []
xids: list[dict] = []


def run(mode: str, bit: int) -> dict:
    mask_off = CANDIDATE_DELTA if mode == "stream" else None
    monitor = NvmlXidMonitor(
        GPU0, library_path=NVML, expected_library_sha256=NVML_SHA,
        expected_library_version=NVML_VERSION)
    with monitor as opened:
        result = d.attempt(mode, bit=bit, mask_off=mask_off,
                           blocks=BLOCKS, iterations=ITERATIONS, timeout_s=60)
        result["xid_events"] = opened.drain(
            timeout_ms=200, max_events=64, maximum_total_ms=4000)
    return result


baseline = d.attempt("baseline", blocks=BLOCKS, iterations=ITERATIONS)
raw.append(baseline)
if baseline["outcome"] != "ok":
    print("BASELINE FAILED:", json.dumps(baseline, indent=2))
    raise SystemExit(1)
baseline_sms = baseline["sms"]
baseline_uuid = baseline["report"]["device"]["uuid"]
print(f"baseline: {len(baseline_sms)} SMs on {baseline_uuid}")
print(f"candidate MASK_OFF={CANDIDATE_DELTA:+d} "
      f"(total {hex(0x4E4 + CANDIDATE_DELTA)})\n")

for mode in MODES:
    for bit in BITS:
        for trial in range(TRIALS):
            result = run(mode, bit)
            raw.append({"mode": mode, "bit": bit, "trial": trial, **result})
            tag = f"{mode:6s} bit={bit:2d} trial={trial}"
            if result["xid_events"]:
                xids.append({"mode": mode, "bit": bit, "trial": trial,
                             "events": result["xid_events"]})
                print(f"{tag} -> XID {result['xid_events']}")
                break
            if result["outcome"] != "ok":
                failures.append({"mode": mode, "bit": bit, "trial": trial,
                                 "outcome": result["outcome"],
                                 "status": result.get("status"),
                                 "signal": result["signal"]})
                print(f"{tag} -> {result['outcome']} "
                      f"{result.get('status') or ''} sig={result['signal']}")
                continue
            observations.append({
                "mode": mode, "tpc_bit": bit, "trial": trial,
                "physical_gpu": PHYSICAL_GPU,
                "gpu_uuid": result["report"]["device"]["uuid"],
                "blocks": BLOCKS,
                "observed_blocks": sum(
                    result["report"]["observed_histogram"].values()),
                "observed_sms": result["sms"],
            })
            print(f"{tag} -> SMs {result['sms']}")
        if xids:
            break
    if xids:
        break

(HERE / "phase2_raw.json").write_text(json.dumps(raw, indent=2, default=str))

verdict = validate_masked_tpc_matrix(
    observations,
    matrix={"modes": MODES, "tpc_bits": BITS, "trials_per_cell": TRIALS,
            "allowed_observed_sm_count": [2], "iterations": ITERATIONS,
            "blocks": BLOCKS, "threads_per_block": THREADS},
    hardware={"sm_count": 128, "expected_tpc_count": 64},
    baseline_observed_sm_count=len(baseline_sms),
    baseline_observed_sms=baseline_sms,
    baseline_gpu_uuid=baseline_uuid,
)
out = HERE / "phase2_verdict.json"
out.write_text(json.dumps(
    {"candidate_delta": CANDIDATE_DELTA,
     "candidate_total_offset": hex(0x4E4 + CANDIDATE_DELTA),
     "verdict": verdict, "failures": failures, "xids": xids}, indent=2))

print("\n=== validate_masked_tpc_matrix (3 modes x 4 bits x 3 trials) ===")
for name, ok in sorted(verdict["checks"].items()):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
print(f"  mapping: {verdict['tpc_sm_mapping']}")
for error in verdict["errors"]:
    print(f"  ERROR: {error}")
print(f"  ACCEPTED: {verdict['accepted']}")
print(f"  failed cells: {len(failures)}   Xids: {len(xids)}")
print(f"raw sha256={hashlib.sha256(out.read_bytes()).hexdigest()}")
