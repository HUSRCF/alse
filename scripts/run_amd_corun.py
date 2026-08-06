"""Measure pairwise externality between two disjointly masked processes.

Gate B-AMD requires the co-run cell to be two processes with disjoint CU
masks rather than one process with two streams, so the whole arrangement --
including the synchronisation -- lives outside any single HIP context.

The measurement has one hazard worth stating: two processes that never
overlap in time produce exactly the numbers a well-behaved co-run produces,
because neither slowed the other down. So the pair is barrier-synchronised
after warmup, both sample for a fixed wall-clock duration rather than a
fixed sample count, and only samples that ran entirely inside the window
where *both* were sampling are counted. A pair whose overlap is too small
is reported as such and not as a zero-externality result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import threading
import sys
import tempfile
import time
from pathlib import Path

# Before the first burstserve import: see the note in run_amd_gate_b.py.
sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.amd_cu_runner import source_revision  # noqa: E402
from burstserve.provenance import canonical_json  # noqa: E402

GIT = Path(os.environ.get("BURSTSERVE_GIT", "/usr/bin/git"))
TABLE_SCHEMA_VERSION = "burstserve.amd-corun-table/v1"
MASKABLE_UNITS = 32


def disjoint_masks(units_a: int, units_b: int) -> tuple[str, str]:
    """Two contiguous runs that share no unit.

    Disjointness is the property Gate A established for these masks, and it
    is what makes the pair a partition rather than an oversubscription.
    """
    if units_a + units_b > MASKABLE_UNITS:
        raise ValueError(
            f"{units_a}+{units_b} exceeds the {MASKABLE_UNITS} maskable units"
        )
    if units_a < 1 or units_b < 1:
        raise ValueError("each side needs at least one unit")
    mask_a = (1 << units_a) - 1
    mask_b = ((1 << units_b) - 1) << units_a
    assert mask_a & mask_b == 0
    return hex(mask_a), hex(mask_b)


def cell_argv(args, *, model: str, batch: int, steps: int,
              height: int | None = None, width: int | None = None
              ) -> list[str]:
    """Build one side's command.

    Resolution is per side, not shared. Two tenants running the same model
    at different resolutions is the only way to get step-time ratios
    between 1.0 and 3.4 out of the two models measured so far, and the
    frozen 1.6 pairing tolerance is untested without them. Falls back to
    the shared --height/--width when a side does not override, so every
    existing invocation keeps its meaning.
    """
    return [
        sys.executable, str(REPO / "scripts/amd_profile_cell.py"),
        "--model", model,
        "--batch", str(batch),
        "--steps", str(steps),
        "--height", str(args.height if height is None else height),
        "--width", str(args.width if width is None else width),
        "--frames", str(args.frames),
        "--seed", str(args.seed),
        "--target-cv", str(args.target_cv),
    ]


def launch(argv: list[str], mask: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["ROC_GLOBAL_CU_MASK"] = mask
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def collect(proc: subprocess.Popen, label: str) -> dict:
    out, err = proc.communicate()
    body = [line for line in out.splitlines() if line.startswith("{")]
    if proc.returncode != 0 or not body:
        return {"status": "cell_failed", "label": label,
                "returncode": proc.returncode, "stderr_tail": err[-2000:]}
    record = json.loads(body[-1])
    record["label"] = label
    return record


def solo(args, side: dict, mask: str, label: str) -> dict:
    argv = cell_argv(args, model=side["model"], batch=side["batch"],
                     steps=side["steps"], height=side.get("height"),
                     width=side.get("width"))
    argv += ["--warmup", str(args.warmup), "--samples", str(args.samples),
             "--max-samples", str(args.max_samples)]
    return collect(launch(argv, mask), label)


def corun(args, side_a: dict, side_b: dict, mask_a: str, mask_b: str) -> tuple:
    barrier_dir = tempfile.mkdtemp(prefix="amd-corun-")
    try:
        procs = []
        for side, mask, name, peer in (
            (side_a, mask_a, "a", "b"),
            (side_b, mask_b, "b", "a"),
        ):
            argv = cell_argv(args, model=side["model"], batch=side["batch"],
                             steps=side["steps"], height=side.get("height"),
                             width=side.get("width"))
            argv += [
                "--warmup", str(args.warmup),
                "--barrier-dir", barrier_dir,
                "--barrier-name", name,
                "--barrier-peer", peer,
                "--barrier-timeout", str(args.barrier_timeout),
                "--co-run-seconds", str(args.co_run_seconds),
            ]
            procs.append((launch(argv, mask), name))
        return tuple(collect(proc, name) for proc, name in procs)
    finally:
        shutil.rmtree(barrier_dir, ignore_errors=True)


def memory_headroom(solo_a: dict, solo_b: dict, *, safety: float) -> dict:
    """Whether two solo cells of this size fit on the card at once.

    A co-run that dies of OOM produces no externality number, and worse,
    a co-run whose two sides thrash the allocator produces one that
    measures memory pressure while being reported as CU contention. So the
    check happens after the solo cells, whose peaks are already measured,
    and before the pair is launched.
    """
    peaks = [solo_a.get("peak_memory_reserved_bytes"),
             solo_b.get("peak_memory_reserved_bytes")]
    total = solo_a.get("total_memory_bytes") or solo_b.get("total_memory_bytes")
    if any(p is None for p in peaks) or not total:
        return {"known": False}
    required = sum(peaks)
    return {
        "known": True,
        "required_bytes": required,
        "total_bytes": total,
        "safety_fraction": safety,
        "budget_bytes": total * safety,
        "fits": required <= total * safety,
        "peaks": peaks,
    }


def restrict_to_overlap(record_a: dict, record_b: dict, minimum: float) -> dict:
    """Keep only samples that ran while both processes were sampling.

    A sample that started before the peer's first sample, or ended after its
    last, was not contended for its whole duration, and averaging it in would
    dilute the externality towards zero -- in the direction that makes the
    result look good.
    """
    start = max(record_a["window_start_wall"], record_b["window_start_wall"])
    end = min(record_a["window_end_wall"], record_b["window_end_wall"])
    overlap = max(0.0, end - start)
    spans = [
        r["window_end_wall"] - r["window_start_wall"]
        for r in (record_a, record_b)
    ]
    fraction = overlap / max(spans) if max(spans) > 0 else 0.0

    result = {
        "overlap_start_wall": start,
        "overlap_end_wall": end,
        "overlap_seconds": overlap,
        "overlap_fraction_of_longer_window": fraction,
        "sufficient_overlap": fraction >= minimum,
        "minimum_overlap_fraction": minimum,
    }
    for key, record in (("a", record_a), ("b", record_b)):
        windows = record["sample_windows"]
        totals = record.get("per_sample_step_totals") or []
        counts = record.get("per_sample_step_counts") or []
        inside: list[float] = []
        inside_steps: list[float] = []
        for index, window in enumerate(windows):
            if not (window["start_wall"] >= start
                    and window["end_wall"] <= end):
                continue
            inside.append(window["s"])
            # Per-step means for the same samples, by index. A call's
            # duration and its step times have to come from one sample:
            # pairing one sample's duration with another's steps would
            # read as noise rather than as the mistake it is.
            if index < len(totals) and index < len(counts) and counts[index]:
                inside_steps.append(totals[index] / counts[index])
        side = {"samples_total": len(windows),
                "samples_in_overlap": len(inside)}
        if len(inside) >= 2:
            ordered = sorted(inside)
            side.update({
                "p50_s": ordered[len(ordered) // 2],
                "p99_s": ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))],
                "mean_s": statistics.mean(inside),
                "cv": statistics.stdev(inside) / statistics.mean(inside),
            })
        if len(inside_steps) >= 2:
            ordered_steps = sorted(inside_steps)
            side["per_step_p50_s"] = ordered_steps[len(ordered_steps) // 2]
            side["per_step_samples"] = len(inside_steps)
        result[key] = side
    return result


def _rocm_scalar(flag: str, needle: str) -> float | None:
    """One number out of rocm-smi, or None if it is unavailable."""
    try:
        proc = subprocess.run(["rocm-smi", flag], capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in proc.stdout.splitlines():
        if needle.lower() not in line.lower():
            continue
        for token in line.replace("(", " ").replace(")", " ").split():
            try:
                return float(token.rstrip("CW%Mhz"))
            except ValueError:
                continue
    return None


def junction_celsius() -> float | None:
    return _rocm_scalar("--showtemp", "junction")


def cool_to(target_c: float, timeout_s: float) -> dict:
    """Idle until the die reaches ``target_c``, and say what happened.

    Co-run numbers are only comparable from a controlled thermal start.
    On 2026-08-06 the same 768 configuration measured +22.4% externality
    on a cold card and +80.1% after hours of continuous runs -- the
    difference being that two tenants reach the 300 W cap and one does
    not, so the hot run was measuring the power limit. Runs compared
    across thermal states measure the schedule of the experiments.
    """
    started = time.time()
    readings = []
    while True:
        temperature = junction_celsius()
        readings.append({"t": time.time() - started, "junction_c": temperature})
        if temperature is None:
            return {"status": "unavailable", "readings": readings,
                    "target_c": target_c}
        if temperature <= target_c:
            return {"status": "reached", "junction_c": temperature,
                    "seconds": time.time() - started, "target_c": target_c,
                    "readings": readings}
        if time.time() - started > timeout_s:
            return {"status": "timeout", "junction_c": temperature,
                    "seconds": time.time() - started, "target_c": target_c,
                    "readings": readings}
        time.sleep(10.0)


class ThermalTrace:
    """Samples temperature, clock and power for the length of a run.

    Recorded with every co-run because the penalty being measured is not
    separable from the power state it was measured in.
    """

    def __init__(self, interval_s: float = 10.0):
        self.interval_s = interval_s
        self.samples: list[dict] = []
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ThermalTrace":
        def loop() -> None:
            while not self._halt.wait(self.interval_s):
                self.samples.append({
                    "junction_c": junction_celsius(),
                    "sclk_mhz": _rocm_scalar("--showclocks", "sclk clock"),
                    "power_w": _rocm_scalar("--showpower", "Average"),
                })
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=30.0)

    def summary(self) -> dict:
        temps = [s["junction_c"] for s in self.samples if s["junction_c"]]
        powers = [s["power_w"] for s in self.samples if s["power_w"]]
        clocks = [s["sclk_mhz"] for s in self.samples if s["sclk_mhz"]]
        return {
            "samples": self.samples,
            "peak_junction_c": max(temps) if temps else None,
            "peak_power_w": max(powers) if powers else None,
            "min_sclk_mhz": min(clocks) if clocks else None,
            "max_sclk_mhz": max(clocks) if clocks else None,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-a", required=True)
    parser.add_argument("--model-b", required=True)
    parser.add_argument("--units-a", type=int, default=16)
    parser.add_argument("--units-b", type=int, default=16)
    parser.add_argument("--batch-a", type=int, default=1)
    parser.add_argument("--batch-b", type=int, default=1)
    parser.add_argument("--steps-a", type=int, default=20)
    parser.add_argument("--steps-b", type=int, default=20)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--width", type=int, default=1024)
    # Per-side overrides. Both sides are recorded in the emitted table, so
    # a pair measured at different resolutions cannot be mistaken later for
    # one measured at the shared default.
    parser.add_argument("--height-a", type=int)
    parser.add_argument("--width-a", type=int)
    parser.add_argument("--height-b", type=int)
    parser.add_argument("--width-b", type=int)
    parser.add_argument("--frames", type=int, default=49)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--max-samples", type=int, default=300)
    parser.add_argument("--target-cv", type=float, default=0.05)
    parser.add_argument("--co-run-seconds", type=float, default=180.0)
    parser.add_argument("--barrier-timeout", type=float, default=1800.0)
    parser.add_argument("--minimum-overlap-fraction", type=float, default=0.80)
    parser.add_argument("--memory-safety", type=float, default=0.90,
                        help="fraction of card memory the pair may use")
    parser.add_argument("--cool-to", type=float, default=None,
                        help="idle until junction temperature reaches this "
                             "many degrees C before measuring. Required for "
                             "any run compared against another: two tenants "
                             "reach the 300 W cap where one does not, so a "
                             "hot run measures the power limit rather than "
                             "the pairing")
    parser.add_argument("--cool-timeout", type=float, default=600.0)
    parser.add_argument("--force", action="store_true",
                        help="run the pair even when it does not fit")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    mask_a, mask_b = disjoint_masks(args.units_a, args.units_b)
    side_a = {"model": args.model_a, "batch": args.batch_a,
              "steps": args.steps_a,
              "height": args.height_a if args.height_a else args.height,
              "width": args.width_a if args.width_a else args.width}
    side_b = {"model": args.model_b, "batch": args.batch_b,
              "steps": args.steps_b,
              "height": args.height_b if args.height_b else args.height,
              "width": args.width_b if args.width_b else args.width}

    libsmctrl_pin = json.loads(
        (REPO / "vendor/LIBSMCTRL_SOURCE.json").read_text()
    )["source_commit"]
    attested = {}
    for relative in ("build/amd_cu_probe/cu_probe",
                     "build/amd_cu_probe/gate_a_probe"):
        path = REPO / relative
        if path.is_file():
            attested[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    revision = source_revision(
        REPO,
        expected_gitlinks={"vendor/libsmctrl": libsmctrl_pin},
        attested_build_files=attested,
        git=GIT,
    )
    print(f"source revision: {revision}", flush=True)
    print(f"masks: a={mask_a} ({args.units_a}u)  b={mask_b} ({args.units_b}u)",
          flush=True)

    started = time.time()
    print("solo a ...", flush=True)
    cooling = None
    if args.cool_to is not None:
        print(f"cooling to {args.cool_to:.0f} C ...", flush=True)
        cooling = cool_to(args.cool_to, args.cool_timeout)
        print(f"  {cooling['status']} at "
              f"{cooling.get('junction_c')} C after "
              f"{cooling.get('seconds', 0):.0f}s", flush=True)

    thermal = ThermalTrace()
    thermal.__enter__()
    solo_a = solo(args, side_a, mask_a, "a")
    print("solo b ...", flush=True)
    solo_b = solo(args, side_b, mask_b, "b")

    headroom = memory_headroom(solo_a, solo_b, safety=args.memory_safety)
    if headroom.get("known"):
        print(f"memory: {headroom['required_bytes']/1e9:.1f} GB needed vs "
              f"{headroom['budget_bytes']/1e9:.1f} GB budget "
              f"({headroom['total_bytes']/1e9:.1f} GB card) -> "
              f"fits={headroom['fits']}", flush=True)
    if headroom.get("known") and not headroom["fits"] and not args.force:
        # Reported as its own outcome. A co-run run anyway would either die
        # of OOM or measure allocator thrash and label it CU contention.
        report = {
            "schema_version": TABLE_SCHEMA_VERSION,
            "source_revision": revision,
            "status": "does_not_fit",
            "masks": {"a": mask_a, "b": mask_b,
                      "units_a": args.units_a, "units_b": args.units_b,
                      "disjoint": (int(mask_a, 0) & int(mask_b, 0)) == 0},
            "sides": {"a": side_a, "b": side_b},
            "solo": {"a": solo_a, "b": solo_b},
            "memory_headroom": headroom,
        }
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(canonical_json(report) + "\n")
        print("co-run skipped: the pair does not fit on the card. "
              "Lower the resolution or batch, or pass --force to measure "
              "the failure.")
        print(f"report: {out}")
        return 1

    print("co-run ...", flush=True)
    co_a, co_b = corun(args, side_a, side_b, mask_a, mask_b)
    thermal.__exit__(None, None, None)

    # Bind the tree again: the four cells are fresh subprocesses that re-read
    # the profile script from disk, so a tree edited mid-run would have been
    # executed under a revision the report no longer describes.
    revision_after = source_revision(
        REPO,
        expected_gitlinks={"vendor/libsmctrl": libsmctrl_pin},
        attested_build_files=attested,
        git=GIT,
    )
    if revision_after != revision:
        print(f"SOURCE MOVED DURING THE RUN: {revision} -> {revision_after}",
              flush=True)

    report = {
        "schema_version": TABLE_SCHEMA_VERSION,
        "source_revision": revision,
        "source_revision_after": revision_after,
        "source_revision_stable": revision_after == revision,
        "masks": {"a": mask_a, "b": mask_b,
                  "units_a": args.units_a, "units_b": args.units_b,
                  "disjoint": (int(mask_a, 0) & int(mask_b, 0)) == 0},
        "sides": {"a": side_a, "b": side_b},
        "solo": {"a": solo_a, "b": solo_b},
        "corun": {"a": co_a, "b": co_b},
        "memory_headroom": headroom,
        # The power state the penalty was measured in. Not metadata: the
        # same configuration measured +22.4% cold and +80.1% hot on
        # 2026-08-06, because two tenants reach the 300 W cap and one does
        # not. A co-run report without this cannot be compared to another.
        "thermal": {"cooling": cooling, **thermal.summary()},
        "wall_seconds": time.time() - started,
        "reduced_contract": "docs/amd-reduced-contract.md",
    }

    failed = [
        label for label, rec in (("solo_a", solo_a), ("solo_b", solo_b),
                                 ("corun_a", co_a), ("corun_b", co_b))
        if rec.get("status") != "ok"
    ]
    if failed:
        report["status"] = "incomplete"
        report["failed_cells"] = failed
        print(f"FAILED cells: {failed}", flush=True)
    else:
        overlap = restrict_to_overlap(co_a, co_b, args.minimum_overlap_fraction)
        report["overlap"] = overlap
        report["status"] = "ok"
        for key, solo_rec in (("a", solo_a), ("b", solo_b)):
            side = overlap[key]
            if "p50_s" in side:
                side["solo_p50_s"] = solo_rec["p50_s"]
                side["externality"] = side["p50_s"] / solo_rec["p50_s"] - 1.0
            # Both penalties are reported because they are different
            # quantities and the scheduler needs the second one. A call is
            # denoising steps plus a VAE decode; if two tenants do not
            # overlap during each other's decode, the call-level penalty is
            # diluted by a term the per-step penalty does not contain, and
            # using it understates what sharing costs per decision.
            solo_steps = None
            if solo_rec.get("per_step_count"):
                solo_steps = (solo_rec["per_step_total_s"]
                              / solo_rec["per_step_count"])
            if solo_steps and "per_step_p50_s" in side:
                side["solo_per_step_s"] = solo_steps
                side["per_step_externality"] = (
                    side["per_step_p50_s"] / solo_steps - 1.0
                )
        print(f"\noverlap {overlap['overlap_seconds']:.1f}s "
              f"({overlap['overlap_fraction_of_longer_window']*100:.1f}% of the "
              f"longer window)  sufficient={overlap['sufficient_overlap']}")
        for key in ("a", "b"):
            side = overlap[key]
            if "externality" in side:
                per_step_note = (
                    f"  per-step {side['per_step_externality']*100:+.1f}%"
                    if "per_step_externality" in side else ""
                )
                print(f"  {key}: solo p50 {side['solo_p50_s']:.3f}s -> "
                      f"co-run p50 {side['p50_s']:.3f}s  "
                      f"externality {side['externality']*100:+.1f}%"
                      f"{per_step_note}  "
                      f"n={side['samples_in_overlap']}/{side['samples_total']} "
                      f"cv={side['cv']*100:.2f}%")
            else:
                print(f"  {key}: too few samples inside the overlap "
                      f"({side['samples_in_overlap']}/{side['samples_total']})")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(report) + "\n")
    print(f"report: {out}")
    if not report["source_revision_stable"]:
        return 2
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
