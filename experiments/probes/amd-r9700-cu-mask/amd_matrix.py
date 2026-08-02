"""AMD masked matrix: two independent mechanisms through the same validator
the NVIDIA side uses, unmodified."""
import json, os, subprocess, sys
sys.path.insert(0, os.path.expanduser("~/Code/alse/src"))
from burstserve.gate_a_results import validate_masked_tpc_matrix

MODES = ["global_cu_mask", "stream_cu_mask"]   # canonical order: alphabetical
BITS = [0, 15, 16, 31]
TRIALS = 3
BLOCKS, ITERATIONS, THREADS = 256, 256, 256
GPU_UUID = "GPU-55d91e3d9b7d11e6"
UNITS = 32  # measured: bits 0..31 mask, bits >=32 are ignored


def run(mode, bit):
    env = dict(os.environ)
    env.pop("ROC_GLOBAL_CU_MASK", None)
    if mode == "global_cu_mask":
        env["ROC_GLOBAL_CU_MASK"] = hex(1 << bit)
    argv = ["./cu_probe", "--mode",
            "cu_mask" if mode == "stream_cu_mask" else mode,
            "--enabled-cu", str(bit), "--blocks", str(BLOCKS),
            "--iterations", str(ITERATIONS),
            "--maskable-units", str(UNITS)]
    r = subprocess.run(argv, env=env, capture_output=True, text=True)
    line = [l for l in r.stdout.splitlines() if l.startswith("{")][-1]
    return r.returncode, json.loads(line)


env = dict(os.environ); env.pop("ROC_GLOBAL_CU_MASK", None)
b = subprocess.run(["./cu_probe", "--mode", "baseline", "--blocks", str(BLOCKS),
                    "--iterations", str(ITERATIONS)], env=env,
                   capture_output=True, text=True)
base = json.loads([l for l in b.stdout.splitlines() if l.startswith("{")][-1])
baseline_ids = sorted(int(k) for k in base["observed_histogram"])
print("baseline: %d units on %s" % (len(baseline_ids), base["device"]["name"]))
# HW_ID1 is an opaque hardware encoding, not a dense index. Normalise through
# the baseline's own sorted id set so identities stay comparable and land in
# the 0..N-1 range every downstream check assumes. The mapping is recorded.
DENSE = {raw: i for i, raw in enumerate(baseline_ids)}
print("dense index map: %d raw ids -> 0..%d" % (len(DENSE), len(DENSE) - 1))

observations, rejected = [], []
for mode in MODES:
    for bit in BITS:
        for trial in range(TRIALS):
            code, d = run(mode, bit)
            tag = "%-15s bit=%2d trial=%d" % (mode, bit, trial)
            if code != 0 or d.get("status") != "ok":
                rejected.append((mode, bit, trial, d.get("error")))
                print("  REJECTED", tag, d.get("error")); continue
            if mode == "stream_cu_mask" and not d["readback_matches_request"]:
                rejected.append((mode, bit, trial, "readback disagreed"))
                print("  REJECTED", tag, "readback disagreed"); continue
            raw_ids = sorted(int(k) for k in d["observed_histogram"])
            if any(r not in DENSE for r in raw_ids):
                rejected.append((mode, bit, trial, "id absent from baseline"))
                print("  REJECTED", tag, "id absent from the baseline set"); continue
            ids = sorted(DENSE[r] for r in raw_ids)
            observations.append({
                "mode": mode, "tpc_bit": bit, "trial": trial,
                "physical_gpu": 0, "gpu_uuid": GPU_UUID,
                "blocks": BLOCKS, "observed_blocks": sum(d["observed_histogram"].values()),
                "observed_sms": ids})
            print("  %s -> %s" % (tag, ids))

v = validate_masked_tpc_matrix(
    observations,
    matrix={"modes": MODES, "tpc_bits": BITS, "trials_per_cell": TRIALS,
            "allowed_observed_sm_count": [1], "iterations": ITERATIONS,
            "blocks": BLOCKS, "threads_per_block": THREADS},
    hardware={"sm_count": len(baseline_ids),
              "expected_tpc_count": len(baseline_ids)},
    baseline_observed_sm_count=len(baseline_ids),
    # Dense, like the observations: the baseline defines the index space.
    baseline_observed_sms=list(range(len(baseline_ids))),
    baseline_gpu_uuid=GPU_UUID)

print("\n=== validate_masked_tpc_matrix (unmodified, from the NVIDIA line) ===")
for n, ok in sorted(v["checks"].items()):
    print("  %s  %s" % ("PASS" if ok else "FAIL", n))
print("  mapping:", v["tpc_sm_mapping"])
for e in v["errors"]:
    print("  ERROR:", e)
print("  cells accepted: %d  rejected: %d" % (len(observations), len(rejected)))
print("  ACCEPTED:", v["accepted"])
json.dump({"verdict": v, "rejected": rejected,
           "baseline_units": len(baseline_ids),
           "dense_index_map": {str(k): v2 for k, v2 in DENSE.items()}},
          open("amd_matrix_verdict.json", "w"), indent=2)
