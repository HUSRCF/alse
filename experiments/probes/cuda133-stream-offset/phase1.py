"""Phase 1: bounded MASK_OFF sweep for the CUDA 13.3 stream-mask offset.

Each attempt runs in a short-lived child process, so a blind write that faults
kills only that child. An NVML Xid monitor is open across each attempt (not
merely after it), because a monitor registered after a fault would miss it.

Outcome classes:
  hit      exit 0 with exactly the oracle's SM set -> candidate
  no-op    exit 7, mask had no effect (128 SMs)   -> wrote somewhere harmless
  signal   child died                              -> wrote somewhere invalid
  timeout  child hung                              -> possible GPU trouble

A hit here is NOT a result. It is a candidate that Phase 2 must fail to
falsify across every bit and trial.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/data/zhuoxu/alse/src")
sys.path.insert(0, str(Path(__file__).parent))

import probe_driver as d  # noqa: E402
from burstserve.nvml_events import NvmlXidMonitor  # noqa: E402

HERE = Path(__file__).parent
GPU0 = "GPU-243d044f-1fa5-4efc-55ef-e456a11bde7e"
NVML = Path("/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.610.43.02")
NVML_SHA = "2dc828b3f5027f98e05c7607c1d8129d11bd28de4c2091c5cd7e32dbc21ec172"
NVML_VERSION = "13.610.43.02"

CU_12_2_MASK_OFF = 0x4E4
# libsmctrl's known x86_64 offsets span 0x49c..0x4fc for the 12.x line; sweep
# well past both ends rather than assuming 13.x stayed inside that band.
DELTA_START, DELTA_END, DELTA_STEP = -256, 512, 4
PROBE_BIT = 0
ORACLE = {PROBE_BIT: {2 * PROBE_BIT, 2 * PROBE_BIT + 1}}
MAX_CONSECUTIVE_TIMEOUTS = 3


def gpu_healthy() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["/usr/bin/nvidia-smi", "--id=0",
             "--query-gpu=memory.used,temperature.gpu", "--format=csv,noheader"],
            capture_output=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return False, "nvidia-smi timed out; GPU may be wedged"
    if result.returncode != 0:
        return False, f"nvidia-smi exit {result.returncode}"
    return True, result.stdout.decode().strip()


def attempt_with_monitor(delta: int) -> dict:
    """Run one offset with an Xid monitor open across the child's lifetime."""
    monitor = NvmlXidMonitor(
        GPU0, library_path=NVML,
        expected_library_sha256=NVML_SHA,
        expected_library_version=NVML_VERSION)
    with monitor as opened:
        result = d.attempt("stream", bit=PROBE_BIT, mask_off=delta,
                           blocks=256, iterations=256, timeout_s=45)
        events = opened.drain(timeout_ms=200, max_events=64,
                              maximum_total_ms=4000)
    result["xid_events"] = events
    return result


def classify(result: dict) -> str:
    if result["xid_events"]:
        return "xid"
    if result["outcome"] == "timeout":
        return "timeout"
    if result["outcome"] == "signal":
        return "signal"
    if result["outcome"] == "ok" and set(result["sms"] or []) == ORACLE[PROBE_BIT]:
        return "hit"
    if result["outcome"] == "ok":
        return "ok-wrong-sms"
    if result.get("status") == "mask_observation_error":
        return "no-op"
    return "rejected"


def main() -> int:
    healthy, detail = gpu_healthy()
    if not healthy:
        print(f"ABORT before start: {detail}")
        return 1
    print(f"GPU 0 pre-sweep: {detail}")

    records = []
    candidates = []
    counts: dict[str, int] = {}
    consecutive_timeouts = 0

    for delta in range(DELTA_START, DELTA_END + 1, DELTA_STEP):
        total = CU_12_2_MASK_OFF + delta
        if total < 0:
            continue
        result = attempt_with_monitor(delta)
        kind = classify(result)
        counts[kind] = counts.get(kind, 0) + 1
        records.append({
            "delta": delta, "total_offset": hex(total), "class": kind,
            "outcome": result["outcome"], "exit": result["exit"],
            "signal": result["signal"], "sms": result["sms"],
            "xid_events": result["xid_events"],
        })

        if kind == "xid":
            print(f"\nABORT: Xid raised at MASK_OFF={delta} ({hex(total)}): "
                  f"{result['xid_events']}")
            break
        if kind == "timeout":
            consecutive_timeouts += 1
            if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                print(f"\nABORT: {consecutive_timeouts} consecutive timeouts")
                break
        else:
            consecutive_timeouts = 0

        if kind in ("hit", "ok-wrong-sms"):
            candidates.append(delta)
            print(f"  MASK_OFF={delta:+5d} total={hex(total)} -> {kind.upper()} "
                  f"sms={result['sms']}")

        # A fault may leave the card unhealthy even without an Xid.
        if kind in ("signal", "timeout"):
            healthy, detail = gpu_healthy()
            if not healthy:
                print(f"\nABORT: GPU unhealthy after MASK_OFF={delta}: {detail}")
                break

    payload = {
        "sweep": {"start": DELTA_START, "end": DELTA_END, "step": DELTA_STEP,
                  "base": hex(CU_12_2_MASK_OFF), "probe_bit": PROBE_BIT},
        "counts": counts,
        "candidates": candidates,
        "records": records,
    }
    out = HERE / "phase1_sweep.json"
    out.write_text(json.dumps(payload, indent=2))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()

    healthy, detail = gpu_healthy()
    print(f"\nattempts: {len(records)}")
    for kind in sorted(counts):
        print(f"  {kind:14s} {counts[kind]}")
    print(f"candidates: {[f'{c:+d} ({hex(CU_12_2_MASK_OFF+c)})' for c in candidates]}")
    print(f"GPU 0 post-sweep: {'OK ' + detail if healthy else 'UNHEALTHY ' + detail}")
    print(f"raw: {out} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
