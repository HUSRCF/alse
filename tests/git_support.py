"""Which git the tests use, and when they cannot run at all.

The provenance capture hardens its git invocations with flags introduced
in git 2.45 -- ``--no-lazy-fetch`` among them. On a host whose
``/usr/bin/git`` is older, every test that exercises the capture fails on
"unknown option", which reads as a defect in the capture rather than as an
unmet precondition. That is what happened on the AMD host, whose system
git is 2.43: 25 failures that said nothing about the code.

The production drivers already accept ``BURSTSERVE_GIT`` to point at a
newer binary. The tests honour the same variable, and skip with the reason
stated when no suitable git is available.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

GIT = Path(os.environ.get("BURSTSERVE_GIT", "/usr/bin/git"))

# --no-lazy-fetch, used by the capture, arrived in 2.45.
MINIMUM_GIT = (2, 45)


def git_version(binary: Path = GIT) -> tuple[int, ...] | None:
    try:
        completed = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = completed.stdout.strip().split()
    if len(parts) < 3:
        return None
    numbers: list[int] = []
    for piece in parts[2].split("."):
        if piece.isdigit():
            numbers.append(int(piece))
        else:
            break
    return tuple(numbers) or None


def require_supported_git(case) -> None:
    """Skip, with the reason, rather than fail on an unmet precondition."""
    version = git_version()
    if version is None:
        case.skipTest(f"cannot determine the version of {GIT}")
    if version < MINIMUM_GIT:
        case.skipTest(
            f"{GIT} is {'.'.join(map(str, version))}; these tests need "
            f"{'.'.join(map(str, MINIMUM_GIT))} or newer. Set BURSTSERVE_GIT "
            "to a suitable binary."
        )


SYSTEM_GIT = Path("/usr/bin/git")


def require_supported_system_git(case) -> None:
    """For tests that cannot be pointed at a different binary.

    Some capture paths reach capture_repository several frames down and
    bind DEFAULT_GIT as a default argument value, so neither BURSTSERVE_GIT
    nor patching the module attribute reaches them. Those tests genuinely
    need the system git to be new enough, and on a host where it is not,
    skipping with the version named is the honest outcome -- failing would
    report a defect in the capture that is not there.
    """
    version = git_version(SYSTEM_GIT)
    if version is None:
        case.skipTest(f"cannot determine the version of {SYSTEM_GIT}")
    if version < MINIMUM_GIT:
        case.skipTest(
            f"{SYSTEM_GIT} is {'.'.join(map(str, version))}; this test "
            f"reaches capture paths that use it directly and needs "
            f"{'.'.join(map(str, MINIMUM_GIT))} or newer"
        )
