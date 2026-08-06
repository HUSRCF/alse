"""A single matmul, repeated: a load that reaches the card's power cap.

Deliberately not a chain. Feeding each result into the next matmul spills
intermediates to memory and turns the kernel bandwidth-bound, which caps
it near 54 TFLOPS and 150 W on this card, against 122 TFLOPS and 300 W
here. Efficiency measured on the chain describes the memory system, not
the compute units, and that mistake produced a published-then-withdrawn
claim about the efficiency optimum (2026-08-05).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.dont_write_bytecode = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=4096)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--warmup", type=int, default=5)
    args = parser.parse_args()

    import torch

    n = args.size
    device = torch.device("cuda")
    torch.manual_seed(0)
    a = torch.randn(n, n, device=device, dtype=torch.float16)
    b = torch.randn(n, n, device=device, dtype=torch.float16)
    torch.cuda.synchronize()
    for _ in range(args.warmup):
        torch.mm(a, b)
    torch.cuda.synchronize()

    count = 0
    started = time.perf_counter()
    while time.perf_counter() - started < args.seconds:
        torch.mm(a, b)
        count += 1
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started

    print(json.dumps({
        "status": "ok",
        "size": n,
        "iterations": count,
        "elapsed_s": elapsed,
        "tflops": count * 2 * n ** 3 / elapsed / 1e12,
        "requested_mask": os.environ.get("ROC_GLOBAL_CU_MASK"),
        "torch": torch.__version__,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
