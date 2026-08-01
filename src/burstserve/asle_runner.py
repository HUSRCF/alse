"""Run one provenance-complete ASLE baseline cell safely."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any, Mapping, Sequence

from .environment import capture_environment
from .provenance import (
    EventRecord,
    RunManifest,
    append_jsonl_atomic,
    write_json_atomic,
)


DEFAULT_PYTHON = Path(
    "/data/zhuoxu/miniconda3/envs/burstserve-phase0/bin/python"
)
DEFAULT_MODEL_ROOT = Path("/data/zhuoxu/models")
DEFAULT_VENDOR_ROOT = Path("vendor/asle")
DEFAULT_RUN_ROOT = Path("experiments/runs")
_SUPPORTED_ARMS = ("stepswap", "offload_tiled")


def query_gpu(index: int) -> dict[str, Any]:
    """Return one physical GPU's state from nvidia-smi."""

    result = subprocess.run(
        [
            "nvidia-smi",
            f"--id={index}",
            "--query-gpu=index,name,uuid,pci.bus_id,memory.total,memory.used,"
            "utilization.gpu,driver_version",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {result.stderr.strip()}")
    rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1:
        raise RuntimeError(f"expected one GPU row, got {rows!r}")
    fields = [field.strip() for field in rows[0].split(",")]
    if len(fields) != 8:
        raise RuntimeError(f"unexpected nvidia-smi row: {rows[0]!r}")
    return {
        "index": int(fields[0]),
        "name": fields[1],
        "uuid": fields[2],
        "pci_bus_id": fields[3],
        "memory_total_mib": int(fields[4]),
        "memory_used_mib": int(fields[5]),
        "utilization_gpu_percent": int(fields[6]),
        "driver_version": fields[7],
    }


def ensure_gpu_available(
    gpu: Mapping[str, Any],
    *,
    maximum_used_mib: int,
    allow_busy: bool,
) -> None:
    used = int(gpu["memory_used_mib"])
    if used > maximum_used_mib and not allow_busy:
        raise RuntimeError(
            f"GPU {gpu['index']} is busy: {used} MiB used exceeds "
            f"{maximum_used_mib} MiB; choose another GPU or pass --allow-busy-gpu"
        )


def _git_output(repo_root: Path, arguments: Sequence[str]) -> str | None:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def source_revision(repo_root: Path, source_metadata: Mapping[str, Any]) -> str:
    """Describe both the immutable ASLE source and BurstServe implementation."""

    archive_sha = str(source_metadata["archive_sha256"])
    head = _git_output(repo_root, ["rev-parse", "HEAD"]) or "uncommitted"
    dirty = _git_output(repo_root, ["diff", "--binary", "HEAD"])
    staged = _git_output(repo_root, ["diff", "--binary", "--cached", "HEAD"])
    dirty_tag = ""
    dirty_payload = "\n".join(part for part in (dirty, staged) if part)
    if dirty_payload:
        digest = hashlib.sha256(dirty_payload.encode("utf-8")).hexdigest()[:16]
        dirty_tag = f"+dirty-{digest}"
    return f"asle-{archive_sha};burstserve-{head}{dirty_tag}"


def build_command(
    *,
    python: Path,
    vendor_root: Path,
    logdir: Path,
    config: Mapping[str, Any],
) -> list[str]:
    return [
        str(python),
        "-u",
        str(vendor_root / "r1_driver.py"),
        "--arm",
        str(config["arm"]),
        "--rollback_bound",
        "0",
        "--mode",
        "denoiser",
        "--arrival",
        str(config["arrival"]),
        "--seed",
        str(config["seed"]),
        "--horizon",
        str(config["horizon_s"]),
        "--lam",
        str(config["arrival_rate"]),
        "--long_model",
        str(config["long_model"]),
        "--urgent_model",
        str(config["urgent_model"]),
        "--budget_gb",
        str(config["budget_gb"]),
        "--frames",
        str(config["frames"]),
        "--height",
        str(config["height"]),
        "--width",
        str(config["width"]),
        "--vsteps",
        str(config["video_steps"]),
        "--usteps",
        str(config["urgent_steps"]),
        "--G",
        str(config["tiles"]),
        "--mem_sample_every",
        "1",
        "--logdir",
        str(logdir),
    ]


def build_child_environment(
    *,
    physical_gpu: int,
    model_root: Path,
    arm: str,
) -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "CUDA_VISIBLE_DEVICES": str(physical_gpu),
            "HF_HUB_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "OMP_NUM_THREADS": "8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "STEPSWAP_MODELS": str(model_root),
            "STEPSWAP_ALLOC_CONF": "expandable_segments:True",
        }
    )
    if arm == "offload_tiled":
        environment["R1_DEBUG_ARM"] = "arm_offload_tiled"
    else:
        environment.pop("R1_DEBUG_ARM", None)
    return environment


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _record_event(
    path: Path,
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    append_jsonl_atomic(
        path,
        EventRecord.create(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
        ),
    )


def _find_summary(logdir: Path) -> Path | None:
    canonical = logdir / "summary.json"
    if canonical.is_file():
        return canonical
    summaries = sorted(logdir.glob("summary_*.json"))
    return summaries[-1] if summaries else None


def evaluate_smoke(
    summary: Mapping[str, Any] | None,
    *,
    process_exit_code: int,
) -> tuple[dict[str, bool], bool]:
    """Evaluate the semantic Phase-0 smoke contract."""

    acceptance = {
        "runnable": bool(summary and summary.get("runnable") is True),
        "minimum_urgent_met": bool(
            summary and int(summary.get("n_urgent") or 0) >= 1
        ),
        "minimum_video_met": bool(summary and int(summary.get("n_video") or 0) >= 1),
    }
    return acceptance, process_exit_code == 0 and all(acceptance.values())


def execute(
    *,
    repo_root: Path,
    python: Path,
    vendor_root: Path,
    model_root: Path,
    run_root: Path,
    config: Mapping[str, Any],
    timeout_s: float,
    maximum_used_mib: int,
    allow_busy_gpu: bool,
) -> tuple[int, Path]:
    """Execute one cell and return ``(exit_code, run_directory)``."""

    physical_gpu = int(config["physical_gpu"])
    gpu = query_gpu(physical_gpu)
    ensure_gpu_available(
        gpu,
        maximum_used_mib=maximum_used_mib,
        allow_busy=allow_busy_gpu,
    )

    source_path = repo_root / "vendor" / "ASLE_SOURCE.json"
    with source_path.open("r", encoding="utf-8") as source:
        source_metadata = json.load(source)
    revision = source_revision(repo_root, source_metadata)
    environment = capture_environment(repo_root=repo_root, model_root=model_root)
    environment["selected_gpu_preflight"] = gpu
    manifest = RunManifest.create(
        config=config,
        seed=int(config["seed"]),
        source_revision=revision,
        environment=environment,
        metadata={
            "purpose": "phase0-asle-baseline",
            "runner": "burstserve.asle_runner/v1",
        },
    )

    run_directory = run_root / manifest.run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    logdir = run_directory / "vendor"
    logdir.mkdir()
    events_path = run_directory / "events.jsonl"
    write_json_atomic(run_directory / "manifest.json", manifest.to_dict())
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=0,
        event_type="run.preflight",
        payload={"gpu": gpu},
    )

    command = build_command(
        python=python,
        vendor_root=vendor_root,
        logdir=logdir,
        config=config,
    )
    write_json_atomic(
        run_directory / "command.json",
        {
            "argv": command,
            "cwd": str(repo_root),
            "started_at_utc": _utc_now(),
        },
    )
    child_environment = build_child_environment(
        physical_gpu=physical_gpu,
        model_root=model_root,
        arm=str(config["arm"]),
    )
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=1,
        event_type="run.started",
        payload={"argv": command},
    )

    process = subprocess.Popen(
        command,
        cwd=repo_root,
        env=child_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
    (run_directory / "stdout.log").write_text(output or "", encoding="utf-8")

    summary_path = _find_summary(logdir)
    summary: dict[str, Any] | None = None
    if summary_path is not None:
        with summary_path.open("r", encoding="utf-8") as source:
            summary = json.load(source)
        write_json_atomic(run_directory / "summary.json", summary)

    process_exit_code = 124 if timed_out else int(process.returncode or 0)
    acceptance, smoke_accepted = evaluate_smoke(
        summary,
        process_exit_code=process_exit_code,
    )
    exit_code = process_exit_code if process_exit_code != 0 else (0 if smoke_accepted else 3)
    outcome = {
        "completed_at_utc": _utc_now(),
        "exit_code": exit_code,
        "process_exit_code": process_exit_code,
        "timed_out": timed_out,
        "summary_found": summary is not None,
        "runnable": summary.get("runnable") if summary else None,
        "n_urgent": summary.get("n_urgent") if summary else None,
        "n_video": summary.get("n_video") if summary else None,
        "smoke_acceptance": acceptance,
        "smoke_accepted": smoke_accepted,
    }
    write_json_atomic(run_directory / "outcome.json", outcome)
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=2,
        event_type="run.completed" if exit_code == 0 else "run.failed",
        payload=outcome,
    )
    return exit_code, run_directory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--vendor-root", type=Path, default=DEFAULT_VENDOR_ROOT)
    parser.add_argument("--model-root", type=Path, default=DEFAULT_MODEL_ROOT)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--physical-gpu", type=int, required=True)
    parser.add_argument("--arm", choices=_SUPPORTED_ARMS, default="stepswap")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--trial",
        type=int,
        default=0,
        help=(
            "repetition index within one cell; the run ID is content "
            "addressed, so repeating a cell needs an explicit trial"
        ),
    )
    parser.add_argument("--horizon-s", type=float, default=10.0)
    parser.add_argument("--arrival-rate", type=float, default=0.1)
    parser.add_argument("--frames", type=int, default=9)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--video-steps", type=int, default=1)
    parser.add_argument("--urgent-steps", type=int, default=1)
    parser.add_argument("--tiles", type=int, default=8)
    parser.add_argument("--budget-gb", type=float, default=0.0)
    parser.add_argument("--timeout-s", type=float, default=1800.0)
    parser.add_argument("--maximum-used-mib", type=int, default=1024)
    parser.add_argument("--allow-busy-gpu", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    vendor_root = (
        args.vendor_root
        if args.vendor_root.is_absolute()
        else repo_root / args.vendor_root
    )
    run_root = (
        args.run_root if args.run_root.is_absolute() else repo_root / args.run_root
    )
    config = {
        "schema_version": "burstserve.asle-cell/v1",
        "physical_gpu": args.physical_gpu,
        "arm": args.arm,
        "arrival": "poisson",
        "seed": args.seed,
        "trial": args.trial,
        "horizon_s": args.horizon_s,
        "arrival_rate": args.arrival_rate,
        "long_model": "cog2b",
        "urgent_model": "sdxl",
        "budget_gb": args.budget_gb,
        "frames": args.frames,
        "height": args.height,
        "width": args.width,
        "video_steps": args.video_steps,
        "urgent_steps": args.urgent_steps,
        "tiles": args.tiles,
    }
    code, run_directory = execute(
        repo_root=repo_root,
        python=args.python.resolve(),
        vendor_root=vendor_root.resolve(),
        model_root=args.model_root.resolve(),
        run_root=run_root.resolve(),
        config=config,
        timeout_s=args.timeout_s,
        maximum_used_mib=args.maximum_used_mib,
        allow_busy_gpu=args.allow_busy_gpu,
    )
    print(run_directory)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
