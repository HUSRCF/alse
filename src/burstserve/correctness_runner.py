"""Run and compare Phase-0 ASLE correctness cells on the current hardware.

The runner deliberately treats ``vendor/asle/pc1_gate.py`` as an immutable
child program. It adds GPU preflight checks, offline execution, provenance,
crash-safe metadata, and canonical artifact names without importing Torch in
the controller process.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from typing import Any, Mapping, Sequence

from .asle_runner import (
    DEFAULT_MODEL_ROOT,
    DEFAULT_PYTHON,
    DEFAULT_VENDOR_ROOT,
    build_child_environment as build_asle_child_environment,
    ensure_gpu_available,
    query_gpu,
    source_revision,
)
from .environment import capture_environment
from .provenance import (
    EventRecord,
    RunManifest,
    append_jsonl_atomic,
    canonical_json,
    read_json,
    write_json_atomic,
)


DEFAULT_RUN_ROOT = Path("experiments/runs")
SUPPORTED_MODELS = ("cog2b", "cog5b")
SUPPORTED_MODES = ("stock", "tiled", "offload", "offload_tiled")
_LATENT_SHA256 = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)


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


def build_command(
    *,
    python: Path,
    vendor_root: Path,
    logdir: Path,
    config: Mapping[str, Any],
) -> list[str]:
    """Build the explicit immutable-PC1 invocation for one correctness cell."""

    return [
        str(python),
        "-u",
        str(vendor_root / "pc1_gate.py"),
        "--model",
        str(config["model"]),
        "--mode",
        str(config["mode"]),
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
        "--G",
        str(config["tiles"]),
        "--seed",
        str(config["seed"]),
        "--logdir",
        str(logdir),
    ]


def build_child_environment(
    *,
    physical_gpu: int,
    model_root: Path,
) -> dict[str, str]:
    """Return the inherited ASLE environment with offline mode made explicit."""

    environment = build_asle_child_environment(
        physical_gpu=physical_gpu,
        model_root=model_root,
        arm="stepswap",
    )
    environment.update(
        {
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    return environment


def evaluate_correctness(
    summary: Mapping[str, Any] | None,
    *,
    process_exit_code: int,
) -> tuple[dict[str, bool], bool]:
    """Evaluate the semantic Phase-0 correctness contract."""

    latent_sha256 = summary.get("latent_sha256") if summary else None
    try:
        video_done = int(summary.get("video_done") or 0) if summary else 0
    except (TypeError, ValueError):
        video_done = 0
    acceptance = {
        "runnable": bool(summary and summary.get("runnable") is True),
        "minimum_video_met": video_done >= 1,
        "latent_sha256_present": bool(
            isinstance(latent_sha256, str)
            and _LATENT_SHA256.fullmatch(latent_sha256)
        ),
    }
    return acceptance, process_exit_code == 0 and all(acceptance.values())


def _find_unique(path: Path, pattern: str) -> tuple[Path | None, list[str]]:
    candidates = sorted(path.glob(pattern))
    return (
        candidates[0] if len(candidates) == 1 else None,
        [candidate.name for candidate in candidates],
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_artifact_atomic(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _terminate_process_group(process: subprocess.Popen[str]) -> tuple[str, str | None]:
    """Terminate a timed-out process group and return its remaining output."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.communicate()


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
    """Execute one PC1 correctness cell and return its exit code and run path."""

    physical_gpu = int(config["physical_gpu"])
    gpu = query_gpu(physical_gpu)
    ensure_gpu_available(
        gpu,
        maximum_used_mib=maximum_used_mib,
        allow_busy=allow_busy_gpu,
    )

    pc1_script = vendor_root / "pc1_gate.py"
    if not pc1_script.is_file():
        raise FileNotFoundError(f"immutable PC1 harness not found: {pc1_script}")
    if not python.is_file():
        raise FileNotFoundError(f"Python interpreter not found: {python}")
    model_directory = model_root / {
        "cog2b": "CogVideoX-2b",
        "cog5b": "CogVideoX-5b",
    }[str(config["model"])]
    if not model_directory.is_dir():
        raise FileNotFoundError(f"local offline model not found: {model_directory}")

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
            "purpose": "phase0-current-hardware-correctness",
            "runner": "burstserve.correctness_runner/v1",
            "immutable_child": "vendor/asle/pc1_gate.py",
        },
    )

    run_directory = run_root / manifest.run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    vendor_logdir = run_directory / "vendor"
    vendor_logdir.mkdir()
    events_path = run_directory / "events.jsonl"
    write_json_atomic(run_directory / "manifest.json", manifest.to_dict())
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=0,
        event_type="run.preflight",
        payload={"gpu": gpu, "offline_model": str(model_directory)},
    )

    command = build_command(
        python=python,
        vendor_root=vendor_root,
        logdir=vendor_logdir,
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
    )
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=1,
        event_type="run.started",
        payload={"argv": command},
    )

    output = ""
    timed_out = False
    launch_error: str | None = None
    process_exit_code = 127
    try:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            env=child_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            output, _ = _terminate_process_group(process)
        process_exit_code = 124 if timed_out else int(process.returncode or 0)
    except OSError as exc:
        launch_error = f"{type(exc).__name__}: {exc}"
        output = f"runner launch error: {launch_error}\n"
    (run_directory / "stdout.log").write_text(output or "", encoding="utf-8")

    summary_path, summary_candidates = _find_unique(
        vendor_logdir, "summary_*.json"
    )
    summary: dict[str, Any] | None = None
    summary_error: str | None = None
    if summary_path is not None:
        try:
            value = read_json(summary_path)
            if not isinstance(value, dict):
                raise TypeError("summary must be a JSON object")
            summary = value
            write_json_atomic(run_directory / "summary.json", summary)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            summary_error = f"{type(exc).__name__}: {exc}"
    elif len(summary_candidates) > 1:
        summary_error = f"expected one summary, found {summary_candidates!r}"

    latent_source, latent_candidates = _find_unique(vendor_logdir, "latent_*.npy")
    latent_artifact: dict[str, Any] | None = None
    latent_error: str | None = None
    if latent_source is not None:
        try:
            latent_destination = run_directory / "latent.npy"
            _copy_artifact_atomic(latent_source, latent_destination)
            latent_artifact = {
                "path": "latent.npy",
                "source_path": str(latent_source.relative_to(run_directory)),
                "byte_size": latent_destination.stat().st_size,
                "file_sha256": _sha256_file(latent_destination),
                "tensor_sha256": (
                    summary.get("latent_sha256") if summary is not None else None
                ),
            }
        except OSError as exc:
            latent_error = f"{type(exc).__name__}: {exc}"
    elif len(latent_candidates) > 1:
        latent_error = f"expected one latent artifact, found {latent_candidates!r}"

    semantic_acceptance, semantic_accepted = evaluate_correctness(
        summary,
        process_exit_code=process_exit_code,
    )
    artifact_captured = latent_artifact is not None
    accepted = semantic_accepted and artifact_captured
    exit_code = process_exit_code if process_exit_code != 0 else (0 if accepted else 3)
    outcome = {
        "completed_at_utc": _utc_now(),
        "exit_code": exit_code,
        "process_exit_code": process_exit_code,
        "timed_out": timed_out,
        "launch_error": launch_error,
        "summary_found": summary is not None,
        "summary_candidates": summary_candidates,
        "summary_error": summary_error,
        "runnable": summary.get("runnable") if summary else None,
        "video_done": summary.get("video_done") if summary else None,
        "latent_sha256": summary.get("latent_sha256") if summary else None,
        "latent_candidates": latent_candidates,
        "latent_error": latent_error,
        "latent_artifact": latent_artifact,
        "semantic_acceptance": semantic_acceptance,
        "semantic_accepted": semantic_accepted,
        "artifact_captured": artifact_captured,
        "accepted": accepted,
        "smoke_accepted": accepted,
    }
    write_json_atomic(run_directory / "outcome.json", outcome)
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=2,
        event_type="run.completed" if accepted else "run.failed",
        payload=outcome,
    )
    return exit_code, run_directory


def _run_comparison_inputs(run_directory: Path) -> dict[str, Any]:
    manifest_value = read_json(run_directory / "manifest.json")
    summary_value = read_json(run_directory / "summary.json")
    if not isinstance(manifest_value, dict) or not isinstance(summary_value, dict):
        raise TypeError(f"manifest and summary must be JSON objects: {run_directory}")
    config = manifest_value.get("config")
    if not isinstance(config, dict):
        raise TypeError(f"manifest config must be a JSON object: {run_directory}")
    mode = config.get("mode")
    latent_sha256 = summary_value.get("latent_sha256")
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"invalid or missing mode in {run_directory}: {mode!r}")
    if not isinstance(latent_sha256, str) or not _LATENT_SHA256.fullmatch(
        latent_sha256
    ):
        raise ValueError(f"invalid or missing latent_sha256 in {run_directory}")
    return {
        "run_directory": str(run_directory.resolve()),
        "run_id": manifest_value.get("run_id"),
        "source_revision": manifest_value.get("source_revision"),
        "mode": mode,
        "comparison_signature": {
            key: config.get(key)
            for key in (
                "model",
                "budget_gb",
                "frames",
                "height",
                "width",
                "video_steps",
                "tiles",
                "seed",
            )
        },
        "latent_sha256": latent_sha256.lower(),
        "latent_path": run_directory / "latent.npy",
    }


def _compare_latent_arrays(left: Path, right: Path) -> dict[str, Any]:
    if not left.is_file() or not right.is_file():
        return {
            "available": False,
            "reason": "one or both canonical latent.npy artifacts are missing",
        }
    try:
        import numpy as np
    except ImportError as exc:
        return {
            "available": False,
            "reason": f"NumPy unavailable: {exc}",
        }

    try:
        left_array = np.load(left, allow_pickle=False)
        right_array = np.load(right, allow_pickle=False)
    except (OSError, ValueError) as exc:
        return {
            "available": False,
            "reason": f"latent load failed: {type(exc).__name__}: {exc}",
        }
    if left_array.shape != right_array.shape:
        return {
            "available": False,
            "reason": "latent shapes differ",
            "left_shape": list(left_array.shape),
            "right_shape": list(right_array.shape),
        }

    difference = np.abs(
        left_array.astype(np.float64, copy=False)
        - right_array.astype(np.float64, copy=False)
    )
    finite = bool(np.isfinite(difference).all())
    return {
        "available": True,
        "calculation_dtype": "float64",
        "left_shape": list(left_array.shape),
        "right_shape": list(right_array.shape),
        "element_count": int(difference.size),
        "finite": finite,
        "max_abs": float(difference.max()) if finite and difference.size else 0.0,
        "mean_abs": float(difference.mean()) if finite and difference.size else 0.0,
    }


def compare_runs(left_run: Path, right_run: Path) -> dict[str, Any]:
    """Compare two correctness runs under the registered comparison policy.

    Same-mode repeats receive a strict SHA verdict. Cross-mode comparisons are
    report-only, even when their SHA values happen to match.
    """

    left = _run_comparison_inputs(left_run.resolve())
    right = _run_comparison_inputs(right_run.resolve())
    same_mode = left["mode"] == right["mode"]
    comparable = (
        left["source_revision"] == right["source_revision"]
        and left["comparison_signature"] == right["comparison_signature"]
    )
    sha_equal = left["latent_sha256"] == right["latent_sha256"]
    common = {
        "schema_version": "burstserve.correctness-comparison/v1",
        "created_at_utc": _utc_now(),
        "left": {key: value for key, value in left.items() if key != "latent_path"},
        "right": {key: value for key, value in right.items() if key != "latent_path"},
        "same_mode": same_mode,
        "comparable": comparable,
        "sha256_equal": sha_equal,
    }
    if not comparable:
        return {
            **common,
            "comparison_kind": "incomparable",
            "policy": "same source and workload signature required",
            "verdict": "invalid",
        }
    if same_mode:
        return {
            **common,
            "comparison_kind": "same_mode_repeat",
            "policy": "exact_latent_sha256_required",
            "verdict": "pass" if sha_equal else "fail",
        }
    return {
        **common,
        "comparison_kind": "cross_mode",
        "policy": "report_only_no_correctness_verdict",
        "verdict": "report_only",
        "numeric_difference": _compare_latent_arrays(
            left["latent_path"], right["latent_path"]
        ),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    run = subparsers.add_parser(
        "run", help="run one immutable PC1 current-hardware correctness cell"
    )
    run.add_argument("--repo-root", type=Path, default=Path.cwd())
    run.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    run.add_argument("--vendor-root", type=Path, default=DEFAULT_VENDOR_ROOT)
    run.add_argument("--model-root", type=Path, default=DEFAULT_MODEL_ROOT)
    run.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    run.add_argument("--physical-gpu", type=int, required=True)
    run.add_argument("--model", choices=SUPPORTED_MODELS, default="cog2b")
    run.add_argument("--mode", choices=SUPPORTED_MODES, default="stock")
    run.add_argument("--frames", type=int, default=9)
    run.add_argument("--height", type=int, default=480)
    run.add_argument("--width", type=int, default=720)
    run.add_argument("--video-steps", type=int, default=1)
    run.add_argument("--tiles", type=int, default=8)
    run.add_argument("--seed", type=int, default=777)
    run.add_argument("--trial", type=int, default=0)
    run.add_argument("--budget-gb", type=float, default=0.0)
    run.add_argument("--timeout-s", type=float, default=1800.0)
    run.add_argument("--maximum-used-mib", type=int, default=1024)
    run.add_argument("--allow-busy-gpu", action="store_true")

    compare = subparsers.add_parser(
        "compare", help="compare two completed correctness run directories"
    )
    compare.add_argument("left_run", type=Path)
    compare.add_argument("right_run", type=Path)
    compare.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.subcommand == "compare":
        result = compare_runs(args.left_run, args.right_run)
        if args.output is not None:
            write_json_atomic(args.output, result)
        print(canonical_json(result))
        return 4 if result["verdict"] == "fail" else 0

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
        "schema_version": "burstserve.correctness-cell/v1",
        "kind": "latent_correctness",
        "arm": f"pc1_{args.mode}",
        "physical_gpu": args.physical_gpu,
        "trial": args.trial,
        "model": args.model,
        "mode": args.mode,
        "budget_gb": args.budget_gb,
        "frames": args.frames,
        "height": args.height,
        "width": args.width,
        "video_steps": args.video_steps,
        "tiles": args.tiles,
        "seed": args.seed,
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
