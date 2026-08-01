"""Stream-offset probe driver.

Every attempt runs in a short-lived child process so that a blind struct write
which faults kills only that child. The driver itself never links CUDA.

This is NOT a formal evidence path. It writes nothing into experiments/runs and
does not touch the promotion lock.
"""

import json
import os
import subprocess
import sys

REPO = "/data/zhuoxu/alse"
LAUNCHER = f"{REPO}/build/smctrl_probe/smid_probe"

GPU0_UUID = "GPU-243d044f-1fa5-4efc-55ef-e456a11bde7e"


def attempt(mode, *, gpu_uuid=GPU0_UUID, bit=None, mask_off=None,
            blocks=256, iterations=256, timeout_s=60):
    """Run one probe attempt in an isolated child. Never raises on child death."""
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "CUDA_VISIBLE_DEVICES": gpu_uuid,
        # The launcher verifies this against its own getppid(); this driver
        # process is the direct parent of the launcher.
        "BURSTSERVE_PARENT_PID": str(os.getpid()),
    }
    if mask_off is not None:
        env["MASK_OFF"] = str(mask_off)

    argv = [LAUNCHER, "--mode", mode, "--device", "0",
            "--blocks", str(blocks), "--iterations", str(iterations)]
    if bit is not None:
        argv += ["--enabled-tpc", str(bit)]
    if mode != "baseline":
        argv.append("--allow-unsupported-driver")

    result = {"mode": mode, "bit": bit, "mask_off": mask_off,
              "argv": argv[1:], "outcome": None, "exit": None, "signal": None,
              "sms": None, "report": None, "stderr": None}
    try:
        completed = subprocess.run(
            argv, env=env, capture_output=True, timeout=timeout_s,
            cwd=REPO, check=False)
    except subprocess.TimeoutExpired:
        result["outcome"] = "timeout"
        return result

    code = completed.returncode
    result["stderr"] = completed.stderr.decode("utf-8", "replace").strip() or None
    if code < 0:
        result["outcome"] = "signal"
        result["signal"] = -code
        return result
    result["exit"] = code

    stdout = completed.stdout.decode("utf-8", "replace").strip()
    for line in stdout.splitlines():
        if line.startswith("{"):
            try:
                result["report"] = json.loads(line)
            except json.JSONDecodeError:
                pass
    report = result["report"]
    if report is not None and report.get("status") == "ok":
        histogram = report.get("observed_histogram") or {}
        result["sms"] = sorted(int(k) for k in histogram)
        result["outcome"] = "ok"
        result["tpc_count"] = report.get("tpc_count")
        result["sm_count"] = (report.get("device") or {}).get("sm_count")
    else:
        result["outcome"] = "rejected"
        if report is not None:
            result["error"] = report.get("error")
            result["status"] = report.get("status")
    return result


if __name__ == "__main__":
    print(json.dumps(attempt(*sys.argv[1:2], bit=(
        int(sys.argv[2]) if len(sys.argv) > 2 else None)), indent=2))
