"""Process-spawning and clock helpers for the rebuild tests.

Kept in its own module (rather than in ``conftest.py``) so test modules can
``from rebuild_support import ...`` without depending on which ``conftest``
happens to be first on ``sys.path``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WORKER_SCRIPT = Path(__file__).resolve().parent / "worker_main.py"


class FakeClock:
    """A clock the test drives by hand.

    Used wherever a criterion is about lease *expiry* rather than about real
    concurrency, so those tests are exact instead of merely probably-long-enough.
    """

    def __init__(self, start: float = 1_700_000_000.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t


def wait_for_file(path: Path, timeout: float = 60.0, poll: float = 0.01) -> None:
    """Block until ``path`` exists, or fail the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(poll)
    raise AssertionError(f"timed out after {timeout}s waiting for {path}")


def spawn_worker(
    python_exe: str,
    *,
    db_path: Path,
    work_dir: Path,
    marker_dir: Path | None = None,
    worker_id: str | None = None,
    crash_at: str | None = None,
    lease: float = 30.0,
    max_attempts: int = 5,
    deadline_seconds: float = 120.0,
    work_ms: float = 0.0,
    start_barrier: Path | None = None,
    ready_file: Path | None = None,
    result_json: Path | None = None,
) -> subprocess.Popen:
    """Launch ``worker_main.py`` as a genuine separate OS process."""
    cmd = [
        python_exe,
        str(WORKER_SCRIPT),
        "--db", str(db_path),
        "--work-dir", str(work_dir),
        "--lease", str(lease),
        "--max-attempts", str(max_attempts),
        "--deadline-seconds", str(deadline_seconds),
        "--work-ms", str(work_ms),
    ]
    if marker_dir is not None:
        cmd += ["--marker-dir", str(marker_dir)]
    if worker_id is not None:
        cmd += ["--worker-id", worker_id]
    if crash_at is not None:
        cmd += ["--crash-at", crash_at]
    if start_barrier is not None:
        cmd += ["--start-barrier", str(start_barrier)]
    if ready_file is not None:
        cmd += ["--ready-file", str(ready_file)]
    if result_json is not None:
        cmd += ["--result-json", str(result_json)]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(WORKER_SCRIPT.parent), env.get("PYTHONPATH", "")]
    )
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )


def read_result(path: Path) -> dict:
    return json.loads(path.read_text())
