#!/usr/bin/env python3
"""Analytical PCIe switch-cost bench for residency-aware GPU rotation."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List


PCIE_GTS = {
    1: 2.5,
    2: 5.0,
    3: 8.0,
    4: 16.0,
    5: 32.0,
}


def payload_bandwidth_gbps(generation: int, lanes: int) -> float:
    """Theoretical one-direction PCIe payload bandwidth in decimal GB/s."""
    if generation not in PCIE_GTS:
        raise ValueError(f"unsupported PCIe generation: {generation}")
    encoding_efficiency = 0.8 if generation <= 2 else 128.0 / 130.0
    return PCIE_GTS[generation] * encoding_efficiency * lanes / 8.0


def write_csv(path: Path, rows: Iterable[Dict[str, object]]) -> None:
    rows = list(rows)
    if not rows:
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def quantum_rows(
    quanta_s: Iterable[float],
    one_way_s: float,
    serialized_swap_s: float,
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for quantum_s in quanta_s:
        for mode, switch_s in (
            ("one_way_or_full_duplex_lower_bound", one_way_s),
            ("serialized_evict_plus_load", serialized_swap_s),
        ):
            cycle_s = quantum_s + switch_s
            rows.append(
                {
                    "quantum_ms": round(quantum_s * 1e3, 3),
                    "mode": mode,
                    "switch_ms": round(switch_s * 1e3, 3),
                    "io_fraction": round(switch_s / cycle_s, 6),
                    "useful_compute_fraction": round(quantum_s / cycle_s, 6),
                }
            )
    return rows


def amortization_rows(
    overhead_targets: Iterable[float],
    one_way_s: float,
    serialized_swap_s: float,
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for target in overhead_targets:
        for mode, switch_s in (
            ("one_way_or_full_duplex_lower_bound", one_way_s),
            ("serialized_evict_plus_load", serialized_swap_s),
        ):
            # switch / (quantum + switch) <= target
            min_quantum_s = switch_s * (1.0 - target) / target
            rows.append(
                {
                    "max_io_fraction": target,
                    "mode": mode,
                    "min_quantum_ms": round(min_quantum_s * 1e3, 3),
                }
            )
    return rows


def slo_rows(
    budgets_s: Iterable[float],
    effective_gbps: float,
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for budget_s in budgets_s:
        total_transfer_gb = effective_gbps * budget_s
        rows.append(
            {
                "io_budget_ms": round(budget_s * 1e3, 3),
                "max_one_way_transfer_gb": round(total_transfer_gb, 4),
                "max_symmetric_state_gb_if_serialized_swap": round(
                    total_transfer_gb / 2.0, 4
                ),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pcie-gen", type=int, default=4)
    parser.add_argument("--lanes", type=int, default=16)
    parser.add_argument("--efficiency", type=float, default=0.70)
    parser.add_argument(
        "--state-gb",
        type=float,
        default=20.0,
        help="GB transferred in each direction; GB is decimal, not GiB",
    )
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()

    if not 0.0 < args.efficiency <= 1.0:
        raise ValueError("efficiency must be in (0, 1]")
    if args.state_gb <= 0.0:
        raise ValueError("state-gb must be positive")

    args.out.mkdir(parents=True, exist_ok=True)
    theoretical_gbps = payload_bandwidth_gbps(args.pcie_gen, args.lanes)
    effective_gbps = theoretical_gbps * args.efficiency
    one_way_s = args.state_gb / effective_gbps
    one_way_gib_s = (args.state_gb * (1024.0**3) / 1e9) / effective_gbps
    serialized_swap_s = 2.0 * one_way_s
    full_duplex_swap_lower_bound_s = one_way_s

    quanta = (0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 5.0, 10.0, 20.0, 40.0)
    overhead_targets = (0.5, 0.2, 0.1, 0.05)
    slo_budgets = (0.02, 0.05, 0.1, 0.2, 0.5, 1.0)

    quantum = quantum_rows(quanta, one_way_s, serialized_swap_s)
    amortization = amortization_rows(
        overhead_targets,
        one_way_s,
        serialized_swap_s,
    )
    slo = slo_rows(slo_budgets, effective_gbps)
    summary = {
        "assumptions": {
            "pcie_generation": args.pcie_gen,
            "lanes": args.lanes,
            "payload_efficiency": args.efficiency,
            "state_size_decimal_gb_per_direction": args.state_gb,
            "full_duplex_overlap_assumes_independent_H2D_and_D2H_capacity": True,
        },
        "bandwidth": {
            "theoretical_payload_gbps_per_direction": round(
                theoretical_gbps, 4
            ),
            "effective_gbps_per_direction": round(effective_gbps, 4),
        },
        "switch_cost": {
            "one_way_decimal_gb_ms": round(one_way_s * 1e3, 3),
            "one_way_if_input_means_gib_ms": round(one_way_gib_s * 1e3, 3),
            "evict_and_load_serialized_ms": round(serialized_swap_s * 1e3, 3),
            "evict_and_load_ideal_full_duplex_lower_bound_ms": round(
                full_duplex_swap_lower_bound_s * 1e3, 3
            ),
        },
        "interpretation": {
            "per_step_rotation": (
                "Infeasible when every rotation transfers the full state; "
                "the I/O setup time dominates millisecond-scale denoising quanta."
            ),
            "scheduler": (
                "Treat transition I/O as a sequence-dependent setup cost and "
                "rotate finely only inside the currently resident set."
            ),
        },
    }

    write_csv(args.out / "io_quantum_overhead.csv", quantum)
    write_csv(args.out / "io_amortization.csv", amortization)
    write_csv(args.out / "io_slo_budget.csv", slo)
    with (args.out / "io_switch_summary.json").open("w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
