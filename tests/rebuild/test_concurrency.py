"""S2.3, S2.6, S2.7 -- real concurrency, not simulated.

Eight genuine OS processes, released together by a barrier file, hammering one
SQLite file. Nothing is threaded, patched or stubbed: if the claim protocol were
wrong, two of these processes would really do the same job at the same time.

What each criterion is checked against:

* **S2.3 exactly-once** -- ``work_executions`` records one row every time a
  worker *starts* a unit of work, before it runs and with no lease check. So a
  duplicate claim shows up as a second row even though the compare-and-swap in
  ``commit`` would still keep the *state* advancing only once. Both counts are
  asserted, because only the first one can see a broken lease.
* **S2.6 no lock errors / no lost updates** -- every worker reports the
  ``sqlite3.OperationalError``s it saw; the union must be empty, WAL must be on,
  and the ledger must contain exactly the 200 expected transitions.
* **S2.7 no double lease** -- every lease ever granted is recorded with its
  validity window; no two windows for one episode may overlap.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from rebuild_support import read_result, spawn_worker, wait_for_file
from pipeline import expected_published
from podcleaner.core.db import Database

N_EPISODES = 50
N_WORKERS = 8
LEASE = 30.0            # far longer than any job: no expiry during the run
DEADLINE = 120.0        # ~600x a healthy run; bounds a hung mutant
STAGES = ("discovered", "fetched", "analyzed", "cut")


def _max_concurrent_leases(leases) -> int:
    """Peak number of simultaneously-held leases, anywhere in the database.

    Evidence that the workers really did overlap in time -- without it, a
    suite could "prove" exclusivity simply by never running two workers at once.
    """
    events = []
    for row in leases:
        end = row["expires_at"]
        if row["released_at"] is not None:
            end = min(end, row["released_at"])
        if end > row["claimed_at"]:
            events.append((row["claimed_at"], 1))
            events.append((end, -1))
    events.sort()
    peak = live = 0
    for _t, delta in events:
        live += delta
        peak = max(peak, live)
    return peak


def _run_pool(python_exe, tmp_path, *, guids, workers, work_ms):
    db_path = tmp_path / "pool.db"
    work = tmp_path / "work"
    work.mkdir()
    barrier = tmp_path / "GO"

    db = Database(db_path)
    for guid in guids:
        db.add_episode(guid)
    db.close()

    procs = []
    results = []
    for i in range(workers):
        result_json = tmp_path / f"w{i}.json"
        results.append(result_json)
        procs.append(
            spawn_worker(
                python_exe,
                db_path=db_path,
                work_dir=work,
                worker_id=f"w{i}",
                lease=LEASE,
                max_attempts=3,
                deadline_seconds=DEADLINE,
                work_ms=work_ms,
                start_barrier=barrier,
                ready_file=tmp_path / f"ready{i}",
                result_json=result_json,
            )
        )
    # Wait for every process to be up, then release them all at once.
    for i in range(workers):
        ready = tmp_path / f"ready{i}"
        wait_for_file(ready, timeout=60.0)
    barrier.write_text("go")

    for i, proc in enumerate(procs):
        stdout, stderr = proc.communicate(timeout=DEADLINE + 60)
        assert proc.returncode == 0, (
            f"worker {i} exited {proc.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )
    summaries = [read_result(p) for p in results]
    return db_path, work, summaries


# --------------------------------------------------------- S2.3 / S2.6 / S2.7


@pytest.fixture(scope="module")
def pool_run(python_exe, tmp_path_factory):
    """One 8-worker / 50-episode run, shared by the criteria that read it."""
    tmp_path = tmp_path_factory.mktemp("pool")
    guids = [f"ep-{i:03d}" for i in range(N_EPISODES)]
    db_path, work, summaries = _run_pool(
        python_exe, tmp_path, guids=guids, workers=N_WORKERS, work_ms=2.0
    )
    db = Database(db_path, create=False)
    yield {
        "db": db,
        "work": work,
        "guids": guids,
        "summaries": summaries,
    }
    db.close()


def test_the_pool_really_ran_concurrently(pool_run):
    """Guard on the experiment itself: without overlap the rest proves nothing."""
    db = pool_run["db"]
    peak = _max_concurrent_leases(db.leases())
    assert peak >= 2, (
        f"peak concurrent leases was {peak}; the workers never overlapped, so "
        f"the exclusivity assertions below are vacuous"
    )
    workers_that_did_work = {t["worker"] for t in db.transitions()}
    assert len(workers_that_did_work) >= 2, workers_that_did_work


def test_every_episode_reaches_published(pool_run):
    db = pool_run["db"]
    assert db.count_by_state() == {"published": N_EPISODES}


def test_side_effect_recorded_exactly_once_per_episode_and_stage(pool_run):
    """S2.3: count == 1 for all 50 episodes, at every stage."""
    db = pool_run["db"]
    executions = Counter(
        (row["guid"], row["state"]) for row in db.work_executions()
    )
    assert len(executions) == N_EPISODES * len(STAGES)
    duplicated = {k: v for k, v in executions.items() if v != 1}
    assert duplicated == {}, f"work performed more than once: {duplicated}"
    for guid in pool_run["guids"]:
        for stage in STAGES:
            assert executions[(guid, stage)] == 1


def test_state_advanced_exactly_once_per_episode_and_stage(pool_run):
    """S2.3, ledger side: the compare-and-swap admitted each step once."""
    db = pool_run["db"]
    ledger = Counter((row["guid"], row["to_state"]) for row in db.transitions())
    assert sum(ledger.values()) == N_EPISODES * len(STAGES)
    assert {k: v for k, v in ledger.items() if v != 1} == {}
    for guid in pool_run["guids"]:
        row = db.get_episode(guid=guid)
        assert [
            (t["from_state"], t["to_state"]) for t in db.transitions(row["id"])
        ] == [
            ("discovered", "fetched"),
            ("fetched", "analyzed"),
            ("analyzed", "cut"),
            ("cut", "published"),
        ]


def test_the_real_output_is_correct_for_every_episode(pool_run):
    """The exactly-once claim is about real files, not just about rows."""
    work: Path = pool_run["work"]
    for guid in pool_run["guids"]:
        published = json.loads((work / f"{guid}.published").read_bytes())
        assert published == expected_published(guid), guid


def test_no_database_is_locked_and_wal_is_on(pool_run):
    """S2.6."""
    lock_errors = [e for s in pool_run["summaries"] for e in s["lock_errors"]]
    other_errors = [e for s in pool_run["summaries"] for e in s["other_errors"]]
    assert lock_errors == []
    assert other_errors == []
    assert {s["journal_mode"] for s in pool_run["summaries"]} == {"wal"}
    assert pool_run["db"].journal_mode() == "wal"


def test_no_lost_updates(pool_run):
    """S2.6: nothing was retried, nothing was dropped, nothing was overwritten."""
    db = pool_run["db"]
    for guid in pool_run["guids"]:
        row = db.get_episode(guid=guid)
        assert row["attempts"] == 0, dict(row)
        assert row["claimed_at"] is None and row["lease_token"] is None
        assert row["last_error"] is None
    committed = sum(s["committed"] for s in pool_run["summaries"])
    assert committed == N_EPISODES * len(STAGES)
    assert sum(s["leases_lost"] for s in pool_run["summaries"]) == 0
    assert sum(s["handler_errors"] for s in pool_run["summaries"]) == 0


def test_no_two_workers_ever_held_the_same_lease(pool_run):
    """S2.7, over the 8-worker run."""
    db = pool_run["db"]
    overlaps = db.overlapping_leases()
    assert overlaps == [], [
        (dict(a), dict(b)) for a, b in overlaps
    ]
    assert len(db.leases()) == N_EPISODES * len(STAGES)


# --------------------------------------------- S2.7, maximum-contention case


def test_no_double_lease_with_more_workers_than_episodes(python_exe, tmp_path):
    """S2.7 negative control: 8 workers fighting over 5 rows, slow handlers."""
    guids = [f"hot-{i}" for i in range(5)]
    db_path, work, summaries = _run_pool(
        python_exe, tmp_path, guids=guids, workers=8, work_ms=25.0
    )
    db = Database(db_path, create=False)
    try:
        assert db.count_by_state() == {"published": len(guids)}
        assert db.overlapping_leases() == []
        executions = Counter(
            (row["guid"], row["state"]) for row in db.work_executions()
        )
        assert {k: v for k, v in executions.items() if v != 1} == {}
        assert len(executions) == len(guids) * len(STAGES)
        assert _max_concurrent_leases(db.leases()) >= 2
        assert [e for s in summaries for e in s["lock_errors"]] == []
        for guid in guids:
            assert json.loads((work / f"{guid}.published").read_bytes()) == \
                expected_published(guid)
    finally:
        db.close()
