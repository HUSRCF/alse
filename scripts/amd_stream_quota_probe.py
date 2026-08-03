"""Can a PyTorch process change its CU quota without restarting?

Every AMD cell so far fixes its quota with ROC_GLOBAL_CU_MASK, which is
read once at process start. That is enough to profile a quota, but a
scheduler has to move a running tenant between quotas, and Gate B's
transition-prediction clause is about exactly that move.

hipExtStreamCreateWithCUMask makes a stream with its own mask, and
torch.cuda.ExternalStream can adopt a raw stream handle, so the two
together would give per-stream quotas inside one process. This probe
establishes whether that composition actually holds, and it does not take
the composition on faith:

  * each stream's mask is read back with hipExtStreamGetCUMask;
  * the work is timed per stream, because a mask that is installed but not
    enforced reads back perfectly while changing nothing;
  * the default stream is timed too, to check the mask is per-stream and
    has not leaked into the whole process.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import sys
import time

sys.dont_write_bytecode = True

hip = ctypes.CDLL("libamdhip64.so")
hip.hipExtStreamCreateWithCUMask.restype = ctypes.c_int
hip.hipExtStreamCreateWithCUMask.argtypes = [
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
]
hip.hipExtStreamGetCUMask.restype = ctypes.c_int
hip.hipExtStreamGetCUMask.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
]
hip.hipStreamDestroy.restype = ctypes.c_int
hip.hipStreamDestroy.argtypes = [ctypes.c_void_p]

WORDS = 4


def create_masked_stream(mask: int):
    handle = ctypes.c_void_p()
    words = (ctypes.c_uint32 * WORDS)(
        *[(mask >> (32 * i)) & 0xFFFFFFFF for i in range(WORDS)]
    )
    rc = hip.hipExtStreamCreateWithCUMask(ctypes.byref(handle), WORDS, words)
    if rc != 0:
        raise RuntimeError(f"hipExtStreamCreateWithCUMask failed: rc={rc}")
    return handle


def read_mask(handle_value: int) -> int | None:
    buffer = (ctypes.c_uint32 * WORDS)()
    rc = hip.hipExtStreamGetCUMask(
        ctypes.c_void_p(handle_value), WORDS, buffer
    )
    if rc != 0:
        return None
    value = 0
    for index, word in enumerate(buffer):
        value |= word << (32 * index)
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--units", default="4,8,16,32")
    parser.add_argument("--size", type=int, default=8192)
    parser.add_argument("--chain", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--samples", type=int, default=15)
    parser.add_argument("--switch-samples", type=int, default=60)
    args = parser.parse_args()

    import torch

    if not hasattr(torch.cuda, "ExternalStream"):
        print(json.dumps({"status": "no_external_stream",
                          "torch": torch.__version__}))
        return 2

    device = torch.device("cuda")
    torch.manual_seed(0)
    a = torch.randn(args.size, args.size, device=device, dtype=torch.float16)
    b = torch.randn(args.size, args.size, device=device, dtype=torch.float16)
    torch.cuda.synchronize()

    def work():
        x = a
        for _ in range(args.chain):
            x = torch.mm(x, b)
        return x

    def timed(stream=None, *, warmup, samples):
        context = torch.cuda.stream(stream) if stream is not None else None
        out = []
        for index in range(warmup + samples):
            torch.cuda.synchronize()
            started = time.perf_counter()
            if context is not None:
                with torch.cuda.stream(stream):
                    work()
            else:
                work()
            torch.cuda.synchronize()
            elapsed = time.perf_counter() - started
            if index >= warmup:
                out.append(elapsed)
        return out

    units = [int(u) for u in args.units.split(",") if u.strip()]
    report = {
        "status": "ok",
        "torch": torch.__version__,
        "device_name": torch.cuda.get_device_name(0),
        "matrix": args.size,
        "chain": args.chain,
        "flops_per_call": 2 * args.size ** 3 * args.chain,
    }

    baseline = timed(None, warmup=args.warmup, samples=args.samples)
    report["default_stream_p50_s"] = statistics.median(baseline)

    handles = {}
    arms = []
    for count in units:
        mask = (1 << count) - 1
        handle = create_masked_stream(mask)
        handles[count] = handle
        stream = torch.cuda.ExternalStream(handle.value)
        readback = read_mask(handle.value)
        samples = timed(stream, warmup=args.warmup, samples=args.samples)
        arms.append({
            "units": count,
            "requested_mask": hex(mask),
            "readback_mask": hex(readback) if readback is not None else None,
            "readback_matches_request": readback == mask,
            "readback_popcount": bin(readback).count("1") if readback else None,
            "p50_s": statistics.median(samples),
            "speedup_vs_default": statistics.median(baseline)
            / statistics.median(samples),
        })
        report.setdefault("arms", []).append(arms[-1])

    # The mask is per-stream only if the default stream is unchanged after
    # all of this. A mask that leaked to the process would look identical on
    # the masked streams and only differ here.
    after = timed(None, warmup=1, samples=args.samples)
    report["default_stream_p50_after_s"] = statistics.median(after)
    report["default_stream_unaffected"] = (
        abs(statistics.median(after) / statistics.median(baseline) - 1.0) < 0.10
    )

    # Transition: alternate between the smallest and the largest arm on every
    # call, so each sample pays whatever a quota change costs.
    if len(units) >= 2:
        low, high = min(units), max(units)
        stream_low = torch.cuda.ExternalStream(handles[low].value)
        stream_high = torch.cuda.ExternalStream(handles[high].value)
        alternating = []
        for index in range(args.warmup + args.switch_samples):
            stream = stream_low if index % 2 else stream_high
            torch.cuda.synchronize()
            started = time.perf_counter()
            with torch.cuda.stream(stream):
                work()
            torch.cuda.synchronize()
            elapsed = time.perf_counter() - started
            if index >= args.warmup:
                alternating.append({"units": low if index % 2 else high,
                                    "s": elapsed})
        for count in (low, high):
            values = [e["s"] for e in alternating if e["units"] == count]
            steady = next(
                arm["p50_s"] for arm in arms if arm["units"] == count
            )
            report.setdefault("alternating", {})[str(count)] = {
                "samples": len(values),
                "p50_s": statistics.median(values),
                "steady_p50_s": steady,
                "transition_overhead": statistics.median(values) / steady - 1.0,
            }

    for handle in handles.values():
        hip.hipStreamDestroy(handle)

    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
