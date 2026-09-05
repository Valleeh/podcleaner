#!/usr/bin/env python
"""Standalone worker process used by the crash and concurrency tests.

This is a real program: it opens its own SQLite connection, claims real rows,
writes real files, and can be ``SIGKILL``ed at any instant. The tests spawn it
with ``subprocess.Popen``; nothing about the crash is simulated.

Usage::

    python worker_main.py --db DB --work-dir DIR [--crash-at STATE] ...

On a clean exit it writes a JSON summary to ``--result-json`` (if given).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from podcleaner.core.db import Database  # noqa: E402
from podcleaner.core.queue import RunResult, WorkQueue  # noqa: E402
from pipeline import Pipeline  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--marker-dir", default=None)
    ap.add_argument("--worker-id", default=None)
    ap.add_argument("--crash-at", default=None,
                    help="block forever in this state's handler so the parent can SIGKILL us")
    ap.add_argument("--lease", type=float, default=30.0)
    ap.add_argument("--max-attempts", type=int, default=5)
    ap.add_argument("--deadline-seconds", type=float, default=120.0)
    ap.add_argument("--work-ms", type=float, default=0.0)
    ap.add_argument("--start-barrier", default=None,
                    help="wait for this file to appear before starting (herd the workers)")
    ap.add_argument("--ready-file", default=None)
    ap.add_argument("--result-json", default=None)
    ap.add_argument("--poll", type=float, default=0.01)
    args = ap.parse_args(argv)

    worker_id = args.worker_id or f"w{os.getpid()}"
    db = Database(args.db)
    queue = WorkQueue(
        db,
        worker_id,
        lease_seconds=args.lease,
        max_attempts=args.max_attempts,
    )
    pipeline = Pipeline(
        args.work_dir, crash_at=args.crash_at, marker_dir=args.marker_dir
        , work_ms=args.work_ms,
    )
    handlers = pipeline.handlers()
    states = list(handlers)

    if args.ready_file:
        Path(args.ready_file).write_text(str(os.getpid()))
    if args.start_barrier:
        barrier = Path(args.start_barrier)
        while not barrier.exists():
            time.sleep(0.005)

    result = RunResult()
    lock_errors: list[str] = []
    other_errors: list[str] = []
    deadline = time.time() + args.deadline_seconds

    while time.time() < deadline:
        try:
            if queue.pending(states) == 0:
                break
            lease = queue.run_once(handlers, result)
        except sqlite3.OperationalError as exc:
            msg = f"{type(exc).__name__}: {exc}"
            (lock_errors if "lock" in str(exc).lower() else other_errors).append(msg)
            time.sleep(0.01)
            continue
        except Exception as exc:  # noqa: BLE001
            other_errors.append(f"{type(exc).__name__}: {exc}")
            break
        if lease is None:
            result.idle_polls += 1
            time.sleep(args.poll)
    else:
        result.timed_out = True

    summary = {
        "worker_id": worker_id,
        "pid": os.getpid(),
        **result.as_dict(),
        "lock_errors": lock_errors,
        "other_errors": other_errors,
        "journal_mode": db.journal_mode(),
    }
    db.close()
    if args.result_json:
        Path(args.result_json).write_text(json.dumps(summary, indent=2))
    else:
        print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
