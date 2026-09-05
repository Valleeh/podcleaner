"""Lease / claim / commit unit behaviour.

Covers S2.4 (lease expiry), S2.5 (attempts and give-up), the WAL half of S2.6,
the single-process half of S2.7 (a stolen lease cannot commit), and the ordering
invariant that the crash test exercises for real: **the work happens before the
state is committed**.

Time-dependent criteria use an injected clock rather than ``sleep``, so they are
exact rather than "probably long enough".
"""

from __future__ import annotations

import sqlite3

import pytest

from podcleaner.core.db import ConcurrentModification, Database, DuplicateGuid
from podcleaner.core.queue import Lease, LeaseLost, RunResult, WorkQueue
from podcleaner.core.states import IllegalTransition, State


@pytest.fixture
def q(db, clock):
    return WorkQueue(db, "w1", lease_seconds=10.0, max_attempts=3, now_fn=clock)


def noop(lease: Lease) -> None:
    return None


# ---------------------------------------------------------------- schema / db


def test_wal_is_enabled(db):
    """S2.6 (part): WAL, not the default rollback journal."""
    assert db.journal_mode() == "wal"


def test_pragmas_are_set(db):
    conn = db.conn
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000


def test_migrate_is_idempotent(db_path):
    from podcleaner.core.db import Database as DB

    a = DB(db_path)
    a.add_episode("g1")
    a.close()
    b = DB(db_path)  # runs migrate() again over a populated file
    assert [r["guid"] for r in b.list_episodes()] == ["g1"]
    b.close()


def test_guid_uniqueness(db):
    db.add_episode("dup")
    with pytest.raises(DuplicateGuid):
        db.add_episode("dup")
    same = db.add_episode("dup", if_exists="ignore")
    assert same == db.get_episode(guid="dup")["id"]
    assert len(db.list_episodes()) == 1


def test_guid_uniqueness_is_enforced_by_the_schema(db):
    db.add_episode("dup2")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO episodes (guid, state, created_at, updated_at) "
            "VALUES ('dup2', 'discovered', 0, 0)"
        )


def test_state_check_constraint_rejects_junk(db):
    db.add_episode("g")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE episodes SET state = 'nonsense' WHERE guid = 'g'")


# ------------------------------------------------------------- claim / commit


def test_claim_on_empty_queue_returns_none(q):
    assert q.claim() is None


def test_claim_stamps_the_row_and_commit_advances_it(db, q, clock):
    ep = db.add_episode("g")
    lease = q.claim()
    assert lease is not None
    assert lease.episode_id == ep
    assert lease.state is State.DISCOVERED
    assert lease.attempts == 0

    row = db.get_episode(ep)
    assert row["state"] == "discovered"        # not advanced by the claim
    assert row["claimed_at"] == clock.t
    assert row["lease_token"] == lease.token
    assert row["owner"] == "w1"

    assert q.commit(lease) is State.FETCHED
    row = db.get_episode(ep)
    assert row["state"] == "fetched"
    assert row["claimed_at"] is None and row["lease_token"] is None


def test_commit_writes_state_and_ledger_in_one_transaction(db, q):
    ep = db.add_episode("g")
    lease = q.claim()
    q.commit(lease, note="hello")
    ledger = db.transitions(ep)
    assert len(ledger) == 1
    assert (ledger[0]["from_state"], ledger[0]["to_state"]) == ("discovered", "fetched")
    assert ledger[0]["worker"] == "w1"
    assert ledger[0]["lease_token"] == lease.token
    assert ledger[0]["note"] == "hello"


def test_commit_rejects_an_illegal_target_and_changes_nothing(db, q):
    ep = db.add_episode("g")
    lease = q.claim()
    with pytest.raises(IllegalTransition):
        q.commit(lease, to_state=State.PUBLISHED)
    row = db.get_episode(ep)
    assert row["state"] == "discovered"
    assert row["lease_token"] == lease.token   # still ours; nothing was released
    assert db.transitions(ep) == []


def test_commit_twice_raises_lease_lost(db, q):
    db.add_episode("g")
    lease = q.claim()
    q.commit(lease)
    with pytest.raises(LeaseLost):
        q.commit(lease)
    assert len(db.transitions()) == 1


def test_claim_walks_the_whole_pipeline(db, q):
    ep = db.add_episode("g")
    seen = []
    for _ in range(4):
        lease = q.claim()
        seen.append(lease.state.value)
        q.commit(lease)
    assert seen == ["discovered", "fetched", "analyzed", "cut"]
    assert db.get_episode(ep)["state"] == "published"
    assert q.claim() is None                   # published is not claimable


def test_claim_can_be_restricted_to_one_state(db, q):
    a = db.add_episode("a")
    db.add_episode("b")
    db.transition(a, State.FETCHED)
    lease = q.claim([State.FETCHED])
    assert lease.episode_id == a
    assert q.claim([State.FETCHED]) is None


def test_claiming_a_terminal_state_is_a_programming_error(q):
    with pytest.raises(ValueError):
        q.claim([State.PUBLISHED])
    with pytest.raises(ValueError):
        q.claim([])


# --------------------------------------------------------- S2.4 lease expiry


def test_live_lease_blocks_every_other_worker(db, q, clock):
    """S2.4 first half: while a lease is live, other workers claim nothing."""
    db.add_episode("g")
    held = q.claim()
    assert held is not None

    others = [
        WorkQueue(db, f"w{i}", lease_seconds=10.0, max_attempts=3, now_fn=clock)
        for i in range(2, 6)
    ]
    claims = 0
    for _ in range(5):
        clock.advance(1.0)           # still inside the 10 s lease
        for other in others:
            if other.claim() is not None:
                claims += 1
    assert claims == 0

    # ...and the holder can still commit, because nobody took it.
    assert q.commit(held) is State.FETCHED


def test_exactly_one_worker_claims_after_the_lease_expires(db, q, clock):
    """S2.4 second half: after expiry exactly one competitor gets the row."""
    db.add_episode("g")
    q.claim()
    others = [
        WorkQueue(db, f"w{i}", lease_seconds=10.0, max_attempts=9, now_fn=clock)
        for i in range(2, 6)
    ]

    clock.advance(9.999)
    assert [o.claim() for o in others] == [None, None, None, None]

    clock.advance(0.002)             # now past claimed_at + lease_seconds
    winners = [o.worker_id for o in others if o.claim() is not None]
    assert len(winners) == 1, winners

    # The single winner holds it exclusively, again.
    assert [o.claim() for o in others] == [None, None, None, None]


def test_lease_boundary_is_inclusive_and_exact(db, clock):
    db.add_episode("g")
    a = WorkQueue(db, "a", lease_seconds=5.0, now_fn=clock)
    b = WorkQueue(db, "b", lease_seconds=5.0, now_fn=clock)
    a.claim()
    clock.advance(4.999999)
    assert b.claim() is None
    clock.advance(0.000001)
    assert b.claim() is not None


def test_extend_renews_a_live_lease(db, clock):
    db.add_episode("g")
    a = WorkQueue(db, "a", lease_seconds=5.0, now_fn=clock)
    b = WorkQueue(db, "b", lease_seconds=5.0, now_fn=clock)
    lease = a.claim()
    clock.advance(4.0)
    lease = a.extend(lease)
    clock.advance(4.0)               # would have expired without the renewal
    assert b.claim() is None
    assert a.commit(lease) is State.FETCHED


def test_extend_after_theft_raises(db, clock):
    db.add_episode("g")
    a = WorkQueue(db, "a", lease_seconds=5.0, now_fn=clock)
    b = WorkQueue(db, "b", lease_seconds=5.0, now_fn=clock)
    lease = a.claim()
    clock.advance(6.0)
    b.claim()
    with pytest.raises(LeaseLost):
        a.extend(lease)


# ------------------------------------------------- S2.7 (single-process half)


def test_a_stolen_lease_cannot_commit(db, clock):
    """The zombie worker wakes up late and must not be able to write."""
    db.add_episode("g")
    a = WorkQueue(db, "a", lease_seconds=5.0, max_attempts=9, now_fn=clock)
    b = WorkQueue(db, "b", lease_seconds=5.0, max_attempts=9, now_fn=clock)

    stale = a.claim()
    clock.advance(6.0)
    fresh = b.claim()
    assert fresh.episode_id == stale.episode_id
    assert fresh.token != stale.token

    with pytest.raises(LeaseLost):
        a.commit(stale)
    assert db.transitions() == []

    assert b.commit(fresh) is State.FETCHED
    assert len(db.transitions()) == 1


def test_no_overlapping_leases_in_the_sequential_case(db, clock):
    db.add_episode("g")
    a = WorkQueue(db, "a", lease_seconds=5.0, max_attempts=9, now_fn=clock)
    b = WorkQueue(db, "b", lease_seconds=5.0, max_attempts=9, now_fn=clock)
    a.claim()
    clock.advance(6.0)
    b.claim()
    assert db.overlapping_leases() == []


def test_the_overlap_detector_can_actually_detect_overlap(db):
    """Negative control on the S2.7 oracle itself: plant an overlap, see it."""
    ep = db.add_episode("g")
    db.conn.execute(
        "INSERT INTO leases (episode_id, lease_token, worker, state, claimed_at,"
        " expires_at) VALUES (?, 't1', 'a', 'discovered', 100, 110)", (ep,))
    db.conn.execute(
        "INSERT INTO leases (episode_id, lease_token, worker, state, claimed_at,"
        " expires_at) VALUES (?, 't2', 'b', 'discovered', 105, 115)", (ep,))
    assert len(db.overlapping_leases()) == 1


# --------------------------------------------------- S2.5 attempts / give-up


def test_attempts_increment_on_reclaim_then_the_row_fails(db, clock):
    """S2.5: reclaim bumps attempts; at max_attempts the row is retired."""
    ep = db.add_episode("g", max_attempts=99)   # queue's limit wins
    workers = [
        WorkQueue(db, f"w{i}", lease_seconds=5.0, max_attempts=3, now_fn=clock)
        for i in range(3)
    ]

    lease = workers[0].claim()
    assert (lease.attempts, db.get_episode(ep)["attempts"]) == (0, 0)

    clock.advance(6.0)
    lease = workers[1].claim()
    assert (lease.attempts, db.get_episode(ep)["attempts"]) == (1, 1)

    clock.advance(6.0)
    lease = workers[2].claim()
    assert (lease.attempts, db.get_episode(ep)["attempts"]) == (2, 2)

    clock.advance(6.0)
    assert workers[0].claim() is None           # reaped instead of handed out
    row = db.get_episode(ep)
    assert row["state"] == "failed"
    assert row["attempts"] == 3
    assert "max_attempts=3" in row["last_error"]
    assert row["claimed_at"] is None

    # Not retried forever.
    for _ in range(5):
        clock.advance(100.0)
        assert workers[0].claim() is None
    assert db.get_episode(ep)["state"] == "failed"
    assert [t["to_state"] for t in db.transitions(ep)] == ["failed"]


def test_per_row_max_attempts_is_honoured_when_the_queue_sets_none(db, clock):
    ep = db.add_episode("g", max_attempts=2)
    w = WorkQueue(db, "w", lease_seconds=5.0, max_attempts=None, now_fn=clock)
    w.claim()
    clock.advance(6.0)
    assert w.claim() is not None                 # attempts -> 1
    clock.advance(6.0)
    assert w.claim() is None
    assert db.get_episode(ep)["state"] == "failed"


def test_abandon_bumps_attempts_and_retires_at_the_limit(db, q, clock):
    ep = db.add_episode("g")
    for expected in (1, 2):
        lease = q.claim()
        assert q.abandon(lease, "boom") is State.DISCOVERED
        row = db.get_episode(ep)
        assert row["attempts"] == expected
        assert row["last_error"] == "boom"
        assert row["state"] == "discovered"      # retryable immediately
    lease = q.claim()
    assert q.abandon(lease, "boom") is State.FAILED
    assert db.get_episode(ep)["state"] == "failed"
    assert q.claim() is None


def test_abandon_without_the_lease_raises(db, clock):
    db.add_episode("g")
    a = WorkQueue(db, "a", lease_seconds=5.0, max_attempts=9, now_fn=clock)
    b = WorkQueue(db, "b", lease_seconds=5.0, max_attempts=9, now_fn=clock)
    stale = a.claim()
    clock.advance(6.0)
    b.claim()
    with pytest.raises(LeaseLost):
        a.abandon(stale, "boom")


def test_a_successful_commit_clears_last_error(db, q):
    ep = db.add_episode("g")
    q.abandon(q.claim(), "transient")
    assert db.get_episode(ep)["last_error"] == "transient"
    q.commit(q.claim())
    assert db.get_episode(ep)["last_error"] is None


# ------------------------------------------ the ordering invariant (mut. 2b)


def test_run_once_does_the_work_before_committing_the_state(db, q):
    """Directly asserts the ordering the crash test proves under real SIGKILL.

    Inside the handler the row must still be in the *pre*-work state. If
    ``run_once`` committed first, this observation would read 'fetched' and the
    assertion below would fail.
    """
    ep = db.add_episode("g")
    observed = {}

    def handler(lease):
        row = db.get_episode(ep)
        observed["state"] = row["state"]
        observed["lease_token"] = row["lease_token"]
        observed["ledger_rows"] = len(db.transitions(ep))

    q.run_once({State.DISCOVERED: handler})

    assert observed["state"] == "discovered", (
        "run_once committed the next state before running the handler"
    )
    assert observed["ledger_rows"] == 0, "ledger row written before the work"
    assert observed["lease_token"] is not None, "lease released before the work"
    # ...and afterwards it really did advance.
    assert db.get_episode(ep)["state"] == "fetched"
    assert len(db.transitions(ep)) == 1


def test_run_once_records_one_execution_then_one_transition(db, q):
    ep = db.add_episode("g")
    q.run_once({State.DISCOVERED: noop})
    assert [r["state"] for r in db.work_executions(ep)] == ["discovered"]
    assert [r["to_state"] for r in db.transitions(ep)] == ["fetched"]


def test_run_once_on_handler_failure_releases_and_does_not_advance(db, q):
    ep = db.add_episode("g")
    result = RunResult()

    def boom(lease):
        raise RuntimeError("kaboom")

    q.run_once({State.DISCOVERED: boom}, result)
    row = db.get_episode(ep)
    assert row["state"] == "discovered"
    assert row["attempts"] == 1
    assert "kaboom" in row["last_error"]
    assert row["claimed_at"] is None
    assert db.transitions(ep) == []
    assert result == RunResult(committed=0, handler_errors=1)


def test_run_once_returns_none_when_there_is_nothing_to_do(q):
    assert q.run_once({State.DISCOVERED: noop}) is None


def test_run_until_idle_drains(db, q):
    ids = [db.add_episode(f"g{i}") for i in range(5)]
    handlers = {s: noop for s in (State.DISCOVERED, State.FETCHED,
                                  State.ANALYZED, State.CUT)}
    result = q.run_until_idle(handlers, deadline=None, poll=0.0)
    assert result.committed == 20
    assert result.timed_out is False
    assert db.count_by_state() == {"published": 5}
    for ep in ids:
        assert len(db.transitions(ep)) == 4
        assert len(db.work_executions(ep)) == 4


def test_run_until_idle_gives_up_at_the_deadline(db, clock):
    db.add_episode("g")
    w = WorkQueue(db, "w", lease_seconds=60.0, now_fn=clock)
    w.claim()                                    # park a live lease on the row
    other = WorkQueue(db, "other", lease_seconds=60.0, now_fn=clock)
    result = other.run_until_idle(
        {State.DISCOVERED: noop}, deadline=clock.t - 1, poll=0.0
    )
    assert result.timed_out is True
    assert result.committed == 0


def test_concurrent_modification_is_detected(db):
    ep = db.add_episode("g")
    with pytest.raises(ConcurrentModification):
        db.transition(ep, State.ANALYZED, expected_state=State.FETCHED)
    assert db.get_episode(ep)["state"] == "discovered"


def test_pending_counts_leased_rows_too(db, q):
    db.add_episode("g")
    q.claim()
    assert q.pending() == 1
