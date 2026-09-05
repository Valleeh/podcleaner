"""S2.2 -- the headline test. Real processes, real SIGKILL, real recovery.

Nothing here is mocked. ``worker_main.py`` is spawned with ``subprocess.Popen``,
allowed to do genuine work against a genuine SQLite file, and then killed with
``SIGKILL`` (which cannot be caught, blocked or handled: no atexit hook, no
``finally``, no signal handler runs). A fresh worker is then started and the
episode must still reach ``published``.

"Reaches published" is deliberately *not* the only assertion. A pipeline that
marked steps done before doing them would also reach ``published`` -- with a
hole in the middle. So each case also asserts that every stage artifact exists
and that the final artifact matches a value the test computes independently of
the workers (:func:`pipeline.expected_published`). That is what makes this test
able to catch the commit-before-work mutation instead of rubber-stamping it.
"""

from __future__ import annotations

import json
import signal
import time
from pathlib import Path

import pytest

from rebuild_support import read_result, spawn_worker, wait_for_file
from pipeline import STAGE_SUFFIX, expected_published
from podcleaner.core.db import Database
from podcleaner.core.states import WORKING_STATES

#: The states in which a worker is actually doing work and can therefore be
#: killed mid-job. ``published`` and ``failed`` are terminal -- they are covered
#: separately by :func:`test_restart_after_publication_is_a_no_op`.
CRASHABLE = [s.value for s in WORKING_STATES]

LEASE = 2.0          # short, so the restarted worker does not wait long
MAX_ATTEMPTS = 10    # generous: a crash must not exhaust the retry budget
KILL_TIMEOUT = 30.0  # how long we allow the victim to reach its crash point
DRAIN_TIMEOUT = 45.0  # ~50x what a healthy run needs; bounds a hung mutant


def _artifacts(work: Path, guid: str) -> dict[str, bool]:
    return {
        kind: (work / f"{guid}.{kind}").exists()
        for kind in ("fetched", "analyzed", "cut", "published")
    }


def _setup(tmp_path: Path, guids: list[str]) -> tuple[Path, Path, Path]:
    db_path = tmp_path / "pipeline.db"
    work = tmp_path / "work"
    markers = tmp_path / "markers"
    work.mkdir()
    markers.mkdir()
    db = Database(db_path)
    for guid in guids:
        db.add_episode(guid, max_attempts=MAX_ATTEMPTS, title=f"title {guid}")
    db.close()
    return db_path, work, markers


def _kill_at(python_exe, *, db_path, work, markers, crash_at, guid) -> None:
    """Run a worker until it is inside ``crash_at``'s handler, then SIGKILL it."""
    proc = spawn_worker(
        python_exe,
        db_path=db_path,
        work_dir=work,
        marker_dir=markers,
        worker_id=f"victim-{crash_at}",
        crash_at=crash_at,
        lease=LEASE,
        max_attempts=MAX_ATTEMPTS,
        deadline_seconds=DRAIN_TIMEOUT,
    )
    try:
        wait_for_file(markers / f"{crash_at}.marker", timeout=KILL_TIMEOUT)

        # The victim is parked inside the handler. Before killing it, record
        # what the database believes -- this is the commit-before-work tripwire.
        db = Database(db_path, create=False)
        row = db.get_episode(guid=guid)
        state_at_crash = row["state"]
        claimed = row["claimed_at"]
        ledger = len(db.transitions(row["id"]))
        db.close()

        assert state_at_crash == crash_at, (
            f"worker is executing the {crash_at!r} handler but the database "
            f"already says {state_at_crash!r}: the state was committed before "
            f"the work was done"
        )
        assert claimed is not None, "worker is working without holding a lease"

        assert not (work / f"{guid}.{STAGE_SUFFIX[crash_at]}").exists(), (
            "the stage artifact exists before the work finished"
        )

        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)
        assert proc.returncode == -signal.SIGKILL, (
            f"expected death by SIGKILL, got returncode {proc.returncode}"
        )
        return ledger
    finally:
        if proc.poll() is None:  # pragma: no cover - only on an assert above
            proc.kill()
            proc.wait(timeout=30)


def _drain(python_exe, *, db_path, work, tmp_path, name="restart", workers=1):
    procs = []
    results = []
    for i in range(workers):
        result_json = tmp_path / f"{name}-{i}.json"
        results.append(result_json)
        procs.append(
            spawn_worker(
                python_exe,
                db_path=db_path,
                work_dir=work,
                worker_id=f"{name}-{i}",
                lease=LEASE,
                max_attempts=MAX_ATTEMPTS,
                deadline_seconds=DRAIN_TIMEOUT,
                result_json=result_json,
            )
        )
    out = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=DRAIN_TIMEOUT + 30)
        assert proc.returncode == 0, (
            f"restarted worker exited {proc.returncode}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
    for result_json in results:
        summary = read_result(result_json)
        assert summary["timed_out"] is False, summary
        assert summary["lock_errors"] == [], summary
        assert summary["other_errors"] == [], summary
        out.append(summary)
    return out


# --------------------------------------------------------------------- S2.2


@pytest.mark.parametrize("crash_at", CRASHABLE)
def test_sigkill_mid_work_still_reaches_published(python_exe, tmp_path, crash_at):
    """S2.2: kill a real worker inside each state's handler; recovery must finish."""
    guid = f"ep-{crash_at}"
    db_path, work, markers = _setup(tmp_path, [guid])

    ledger_before = _kill_at(
        python_exe, db_path=db_path, work=work, markers=markers,
        crash_at=crash_at, guid=guid,
    )

    db = Database(db_path, create=False)
    row = db.get_episode(guid=guid)
    assert row["state"] == crash_at, "the dead worker advanced the state anyway"
    assert row["lease_token"] is not None, "a dead worker's lease vanished by magic"
    stages_done = CRASHABLE.index(crash_at)
    assert ledger_before == stages_done
    db.close()

    _drain(python_exe, db_path=db_path, work=work, tmp_path=tmp_path)

    db = Database(db_path, create=False)
    row = db.get_episode(guid=guid)
    assert row["state"] == "published", dict(row)
    assert row["claimed_at"] is None and row["lease_token"] is None
    # Exactly one reclaim happened: the crashed stage.
    assert row["attempts"] == 1, dict(row)

    # The state machine really walked every step, once each.
    assert [(t["from_state"], t["to_state"]) for t in db.transitions(row["id"])] == [
        ("discovered", "fetched"),
        ("fetched", "analyzed"),
        ("analyzed", "cut"),
        ("cut", "published"),
    ]
    # The crashed stage was executed twice (at-least-once side effects); every
    # other stage exactly once.
    executions = [e["state"] for e in db.work_executions(row["id"])]
    assert executions.count(crash_at) == 2, executions
    for other in CRASHABLE:
        if other != crash_at:
            assert executions.count(other) == 1, executions
    db.close()

    # And the work products are all really there and really correct.
    assert _artifacts(work, guid) == dict.fromkeys(
        ("fetched", "analyzed", "cut", "published"), True
    )
    published = json.loads((work / f"{guid}.published").read_bytes())
    assert published == expected_published(guid)


def test_sigkill_at_every_state_in_one_episode(python_exe, tmp_path):
    """Four consecutive real crashes on one episode; it must still finish."""
    guid = "ep-serial-crash"
    db_path, work, markers = _setup(tmp_path, [guid])

    for crash_at in CRASHABLE:
        _kill_at(
            python_exe, db_path=db_path, work=work, markers=markers,
            crash_at=crash_at, guid=guid,
        )
        (markers / f"{crash_at}.marker").unlink()
        # Let the lease expire so the next process gets past the crashed stage.
        time.sleep(LEASE + 0.2)

    _drain(python_exe, db_path=db_path, work=work, tmp_path=tmp_path)

    db = Database(db_path, create=False)
    row = db.get_episode(guid=guid)
    assert row["state"] == "published", dict(row)
    assert row["attempts"] == len(CRASHABLE)
    assert len(db.transitions(row["id"])) == 4
    db.close()
    assert json.loads((work / f"{guid}.published").read_bytes()) == expected_published(guid)


def test_crash_recovery_with_a_backlog_and_several_workers(python_exe, tmp_path):
    """A crash in the middle of a batch does not strand the other episodes."""
    guids = [f"ep-batch-{i:02d}" for i in range(12)]
    db_path, work, markers = _setup(tmp_path, guids)

    proc = spawn_worker(
        python_exe, db_path=db_path, work_dir=work, marker_dir=markers,
        worker_id="victim", crash_at="analyzed", lease=LEASE,
        max_attempts=MAX_ATTEMPTS, deadline_seconds=DRAIN_TIMEOUT,
    )
    wait_for_file(markers / "analyzed.marker", timeout=KILL_TIMEOUT)
    victim_guid = (markers / "analyzed.marker").read_text().split()[1]
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)
    assert proc.returncode == -signal.SIGKILL

    _drain(python_exe, db_path=db_path, work=work, tmp_path=tmp_path, workers=3)

    db = Database(db_path, create=False)
    assert db.count_by_state() == {"published": len(guids)}
    assert db.overlapping_leases() == []
    for guid in guids:
        row = db.get_episode(guid=guid)
        assert len(db.transitions(row["id"])) == 4
        assert json.loads((work / f"{guid}.published").read_bytes()) == \
            expected_published(guid)
    assert db.get_episode(guid=victim_guid)["attempts"] == 1
    db.close()


@pytest.mark.parametrize("terminal", ["published", "failed"])
def test_restart_after_publication_is_a_no_op(python_exe, tmp_path, terminal):
    """Terminal states are not crashable: a restarted worker leaves them alone."""
    guid = f"ep-terminal-{terminal}"
    db_path, work, _ = _setup(tmp_path, [guid])
    db = Database(db_path, create=False)
    db.force_state(db.get_episode(guid=guid)["id"], terminal)
    db.close()

    _drain(python_exe, db_path=db_path, work=work, tmp_path=tmp_path)

    db = Database(db_path, create=False)
    row = db.get_episode(guid=guid)
    assert row["state"] == terminal
    assert db.transitions(row["id"]) == []
    assert db.work_executions(row["id"]) == []
    db.close()


def test_the_victim_really_dies_by_signal(python_exe, tmp_path):
    """Negative control on the test harness: prove the kill is a real SIGKILL.

    If this ever passed with a graceful shutdown the whole S2.2 story would be
    theatre, so it is asserted rather than assumed.
    """
    guid = "ep-signal-check"
    db_path, work, markers = _setup(tmp_path, [guid])
    proc = spawn_worker(
        python_exe, db_path=db_path, work_dir=work, marker_dir=markers,
        worker_id="victim", crash_at="discovered", lease=LEASE,
        max_attempts=MAX_ATTEMPTS, deadline_seconds=DRAIN_TIMEOUT,
    )
    wait_for_file(markers / "discovered.marker", timeout=KILL_TIMEOUT)
    proc.send_signal(signal.SIGKILL)
    stdout, stderr = proc.communicate(timeout=30)
    assert proc.returncode == -signal.SIGKILL
    assert stdout == "" and stderr == "", (stdout, stderr)  # no shutdown hooks ran
    assert not (work / f"{guid}.fetched").exists()
