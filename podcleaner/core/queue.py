"""Lease / claim / commit -- the durable work queue.

The protocol, in one paragraph
------------------------------

A worker **claims** the oldest eligible row in a single atomic ``UPDATE`` that
stamps ``claimed_at``, a lease duration and a random **fencing token**. It then
does the work. Finally it **commits**, which -- in one transaction -- checks the
row still carries *its* token and *its* state, appends a ``transitions`` row and
advances ``episodes.state``. If the worker dies at any point before the commit,
nothing was advanced: the lease simply expires and another worker reclaims the
row, bumping ``attempts``. At ``max_attempts`` the row goes to ``failed`` instead
of being retried forever.

Two ordering rules make the whole thing work, and both are load-bearing:

1. **Work first, commit second.** :meth:`WorkQueue.run_once` calls the handler
   and *then* commits. Committing first would mean a crash mid-work leaves the
   database claiming a step was done that was not -- the episode marches on to
   ``published`` with a missing artifact. That is the bug this module exists to
   make impossible.
2. **The commit is a compare-and-swap.** It matches on the fencing token *and*
   the expected state, so a worker whose lease was stolen (or which came back
   from the dead) cannot apply a stale write.

Semantics, stated honestly: state advancement is **exactly once** (the CAS
guarantees it). The side effect performed by the handler is **at-least-once** --
if a worker is killed after writing a file but before committing, the file is
written again on retry. Handlers must therefore be idempotent, which is why the
sample pipeline writes artifacts to deterministic paths.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping, Optional, Sequence

from .db import Database
from .states import (
    State,
    TERMINAL_STATES,
    WORKING_STATES,
    coerce,
    next_state as happy_path_next,
    validate_transition,
)

__all__ = ["WorkQueue", "Lease", "LeaseLost", "HandlerError", "RunResult"]


class LeaseLost(Exception):
    """The row no longer carries this worker's fencing token (or state).

    Raised by :meth:`WorkQueue.commit`. It means: somebody else owns this work
    now; throw away whatever you computed and do not write it anywhere.
    """


class HandlerError(Exception):
    """Wraps an exception raised by a job handler."""


@dataclass(frozen=True)
class Lease:
    """A time-boxed exclusive claim on one episode in one state."""

    episode_id: int
    guid: str
    state: State
    token: str
    owner: str
    claimed_at: float
    expires_at: float
    attempts: int
    audio_url: Optional[str] = None
    title: Optional[str] = None
    payload: Optional[str] = None

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at


@dataclass
class RunResult:
    """What one :meth:`WorkQueue.run_until_idle` pass did."""

    committed: int = 0
    handler_errors: int = 0
    leases_lost: int = 0
    idle_polls: int = 0
    timed_out: bool = False

    def as_dict(self) -> dict:
        return {
            "committed": self.committed,
            "handler_errors": self.handler_errors,
            "leases_lost": self.leases_lost,
            "idle_polls": self.idle_polls,
            "timed_out": self.timed_out,
        }


Handler = Callable[[Lease], None]


class WorkQueue:
    """Lease-based work queue over :class:`~podcleaner.core.db.Database`."""

    def __init__(
        self,
        db: Database,
        worker_id: Optional[str] = None,
        *,
        lease_seconds: float = 30.0,
        max_attempts: Optional[int] = None,
        now_fn: Callable[[], float] = time.time,
    ):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be > 0")
        self.db = db
        self.worker_id = worker_id or f"worker-{os.getpid()}-{secrets.token_hex(3)}"
        self.lease_seconds = float(lease_seconds)
        self.max_attempts = max_attempts
        self._now = now_fn

    # ------------------------------------------------------------------ claim

    def claim(self, states: Optional[Sequence[object]] = None) -> Optional[Lease]:
        """Atomically take the next eligible row, or return ``None``.

        Eligible means: in one of ``states`` (default: every working state) and
        either unclaimed or holding an expired lease. The selection and the
        stamping happen in a single ``UPDATE ... WHERE id = (SELECT ...)``, so
        two workers cannot both observe the same row as free.
        """
        wanted = self._state_values(states)
        token = secrets.token_hex(16)
        placeholders = ",".join("?" for _ in wanted)

        with self.db.write_tx() as conn:
            # Read the clock *inside* the transaction. Sampling it before
            # BEGIN IMMEDIATE would stamp claimed_at with a time from before we
            # actually held the write lock, which silently shortens our own
            # lease and lets a peer reclaim a row we are still working on.
            now = self._now()
            self._reap(conn, now, wanted)

            cur = conn.execute(
                f"""
                UPDATE episodes
                   SET claimed_at    = ?,
                       lease_seconds = ?,
                       lease_token   = ?,
                       owner         = ?,
                       attempts      = attempts
                                     + (CASE WHEN claimed_at IS NULL THEN 0 ELSE 1 END),
                       updated_at    = ?
                 WHERE id = (
                       SELECT id
                         FROM episodes
                        WHERE state IN ({placeholders})
                        ORDER BY updated_at ASC, id ASC
                        LIMIT 1
                 )
                RETURNING id, guid, state, attempts, claimed_at, lease_seconds,
                          lease_token, owner, audio_url, title, payload
                """,
                (now, self.lease_seconds, token, self.worker_id, now, *wanted),
            )
            row = cur.fetchone()
            cur.fetchall()  # drain, so the statement is fully stepped
            if row is None:
                return None

            expires_at = now + self.lease_seconds
            conn.execute(
                """
                INSERT INTO leases
                    (episode_id, lease_token, worker, pid, state,
                     claimed_at, expires_at, released_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (row["id"], token, self.worker_id, os.getpid(),
                 row["state"], now, expires_at),
            )

        return Lease(
            episode_id=int(row["id"]),
            guid=row["guid"],
            state=coerce(row["state"]),
            token=token,
            owner=self.worker_id,
            claimed_at=now,
            expires_at=expires_at,
            attempts=int(row["attempts"]),
            audio_url=row["audio_url"],
            title=row["title"],
            payload=row["payload"],
        )

    def _reap(self, conn: sqlite3.Connection, now: float, wanted: Sequence[str]) -> int:
        """Move rows that have burned through ``max_attempts`` to ``failed``.

        Runs inside the caller's write transaction, immediately before the claim,
        so a poisoned row is retired rather than handed out again.
        """
        placeholders = ",".join("?" for _ in wanted)
        rows = conn.execute(
            f"""
            SELECT id, guid, state, attempts, max_attempts, owner
              FROM episodes
             WHERE state IN ({placeholders})
               AND claimed_at IS NOT NULL
               AND claimed_at + COALESCE(lease_seconds, 0) <= ?
               AND attempts + 1 >= CASE WHEN ? IS NULL THEN max_attempts ELSE ? END
            """,
            (*wanted, now, self.max_attempts, self.max_attempts),
        ).fetchall()
        for row in rows:
            limit = self.max_attempts if self.max_attempts is not None else row["max_attempts"]
            reason = (
                f"lease expired after {row['attempts'] + 1} attempt(s); "
                f"max_attempts={limit}"
            )
            validate_transition(row["state"], State.FAILED)
            conn.execute(
                """
                UPDATE episodes
                   SET state = 'failed',
                       attempts = attempts + 1,
                       last_error = ?,
                       claimed_at = NULL, lease_token = NULL,
                       lease_seconds = NULL, owner = NULL,
                       updated_at = ?
                 WHERE id = ?
                """,
                (reason, now, row["id"]),
            )
            conn.execute(
                """
                INSERT INTO transitions
                    (episode_id, guid, from_state, to_state, worker, lease_token,
                     note, created_at)
                VALUES (?, ?, ?, 'failed', ?, NULL, ?, ?)
                """,
                (row["id"], row["guid"], row["state"], self.worker_id, reason, now),
            )
        return len(rows)

    def reap(self, states: Optional[Sequence[object]] = None) -> int:
        """Public one-shot reap, for a janitor process or a test."""
        wanted = self._state_values(states)
        with self.db.write_tx() as conn:
            return self._reap(conn, self._now(), wanted)

    # ----------------------------------------------------------------- commit

    def commit(
        self,
        lease: Lease,
        *,
        to_state: object = None,
        note: Optional[str] = None,
    ) -> State:
        """Advance the episode, atomically, if we still hold the lease.

        One transaction does all of: verify the fencing token, verify the state
        has not moved, validate the transition, append the ``transitions`` row,
        write the new state, and release the lease. Either all of that lands or
        none of it does.
        """
        with self.db.write_tx() as conn:
            now = self._now()   # inside the lock; see claim()
            row = conn.execute(
                "SELECT id, guid, state, lease_token FROM episodes WHERE id = ?",
                (lease.episode_id,),
            ).fetchone()
            if row is None:
                raise LeaseLost(f"episode {lease.episode_id} disappeared")
            if row["lease_token"] != lease.token:
                raise LeaseLost(
                    f"episode {lease.episode_id}: lease token no longer ours "
                    f"(row holds {row['lease_token']!r})"
                )
            if coerce(row["state"]) is not lease.state:
                raise LeaseLost(
                    f"episode {lease.episode_id}: state moved from "
                    f"{lease.state.value!r} to {row['state']!r} under us"
                )

            target = happy_path_next(lease.state) if to_state is None else to_state
            src, dst = validate_transition(row["state"], target)

            conn.execute(
                """
                INSERT INTO transitions
                    (episode_id, guid, from_state, to_state, worker, lease_token,
                     note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (lease.episode_id, row["guid"], src.value, dst.value,
                 self.worker_id, lease.token, note, now),
            )
            conn.execute(
                """
                UPDATE episodes
                   SET state = ?, updated_at = ?, last_error = NULL,
                       claimed_at = NULL, lease_token = NULL,
                       lease_seconds = NULL, owner = NULL
                 WHERE id = ? AND lease_token = ?
                """,
                (dst.value, now, lease.episode_id, lease.token),
            )
            conn.execute(
                "UPDATE leases SET released_at = ? WHERE lease_token = ?",
                (now, lease.token),
            )
        return dst

    # ------------------------------------------------------- give up / release

    def abandon(self, lease: Lease, error: str) -> State:
        """Release a lease after a handler failure, bumping ``attempts``.

        Returns the state the row is now in: unchanged (retryable) or
        ``failed`` once ``max_attempts`` is reached.
        """
        with self.db.write_tx() as conn:
            now = self._now()   # inside the lock; see claim()
            row = conn.execute(
                "SELECT id, guid, state, attempts, max_attempts, lease_token "
                "FROM episodes WHERE id = ?",
                (lease.episode_id,),
            ).fetchone()
            if row is None or row["lease_token"] != lease.token:
                # Someone else already owns it; not ours to fail.
                raise LeaseLost(f"episode {lease.episode_id}: cannot abandon, lease gone")
            attempts = int(row["attempts"]) + 1
            limit = (
                self.max_attempts
                if self.max_attempts is not None
                else int(row["max_attempts"])
            )
            current = coerce(row["state"])
            if attempts >= limit:
                validate_transition(current, State.FAILED)
                conn.execute(
                    """
                    UPDATE episodes
                       SET state = 'failed', attempts = ?, last_error = ?,
                           claimed_at = NULL, lease_token = NULL,
                           lease_seconds = NULL, owner = NULL, updated_at = ?
                     WHERE id = ? AND lease_token = ?
                    """,
                    (attempts, error, now, lease.episode_id, lease.token),
                )
                conn.execute(
                    """
                    INSERT INTO transitions
                        (episode_id, guid, from_state, to_state, worker,
                         lease_token, note, created_at)
                    VALUES (?, ?, ?, 'failed', ?, ?, ?, ?)
                    """,
                    (lease.episode_id, row["guid"], current.value, self.worker_id,
                     lease.token, error, now),
                )
                new_state = State.FAILED
            else:
                conn.execute(
                    """
                    UPDATE episodes
                       SET attempts = ?, last_error = ?,
                           claimed_at = NULL, lease_token = NULL,
                           lease_seconds = NULL, owner = NULL, updated_at = ?
                     WHERE id = ? AND lease_token = ?
                    """,
                    (attempts, error, now, lease.episode_id, lease.token),
                )
                new_state = current
            conn.execute(
                "UPDATE leases SET released_at = ? WHERE lease_token = ?",
                (now, lease.token),
            )
        return new_state

    def extend(self, lease: Lease, seconds: Optional[float] = None) -> Lease:
        """Renew a live lease (heartbeat). Raises :class:`LeaseLost` if stolen."""
        seconds = self.lease_seconds if seconds is None else float(seconds)
        with self.db.write_tx() as conn:
            now = self._now()   # inside the lock; see claim()
            cur = conn.execute(
                """
                UPDATE episodes
                   SET claimed_at = ?, lease_seconds = ?, updated_at = ?
                 WHERE id = ? AND lease_token = ?
                """,
                (now, seconds, now, lease.episode_id, lease.token),
            )
            if cur.rowcount != 1:
                raise LeaseLost(f"episode {lease.episode_id}: lease no longer ours")
            conn.execute(
                "UPDATE leases SET expires_at = ? WHERE lease_token = ?",
                (now + seconds, lease.token),
            )
        return replace(lease, claimed_at=now, expires_at=now + seconds)

    # ------------------------------------------------------------- the loop

    def record_execution(self, lease: Lease) -> None:
        """Append the "a worker really started this unit of work" audit row.

        Deliberately *not* guarded by a token check: this table records what
        actually happened, so if a broken claim protocol lets two workers run
        the same job it shows up here as two rows.
        """
        with self.db.write_tx() as conn:
            conn.execute(
                """
                INSERT INTO work_executions
                    (episode_id, guid, state, worker, lease_token, pid, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (lease.episode_id, lease.guid, lease.state.value, self.worker_id,
                 lease.token, os.getpid(), self._now()),
            )

    def run_once(
        self,
        handlers: Mapping[object, Handler],
        result: Optional[RunResult] = None,
    ) -> Optional[Lease]:
        """Claim one job, run its handler, commit. Returns the lease, or ``None``.

        Infrastructure errors (``sqlite3`` problems) propagate. Handler errors do
        not: they release the lease via :meth:`abandon` so the job is retried.
        """
        result = result if result is not None else RunResult()
        by_state = {coerce(k).value: v for k, v in handlers.items()}
        lease = self.claim(list(by_state))
        if lease is None:
            return None

        handler = by_state[lease.state.value]
        self.record_execution(lease)

        # ------------------------------------------------------------------
        # ORDER IS LOAD-BEARING. The work happens, and only then is the new
        # state committed. Swapping these two statements reintroduces the
        # classic "marked done before it was done" bug: a crash between them
        # would leave the pipeline believing a step completed that never ran.
        # ------------------------------------------------------------------
        try:
            handler(lease)                       # 1. do the work
        except Exception as exc:                 # noqa: BLE001 - handlers are user code
            result.handler_errors += 1
            try:
                self.abandon(lease, f"{type(exc).__name__}: {exc}")
            except LeaseLost:
                result.leases_lost += 1
            return lease

        try:
            self.commit(lease)                   # 2. record that it happened
        except LeaseLost:
            result.leases_lost += 1
            return lease

        result.committed += 1
        return lease

    def pending(self, states: Optional[Sequence[object]] = None) -> int:
        """Rows still sitting in one of ``states``, leased or not."""
        wanted = self._state_values(states)
        placeholders = ",".join("?" for _ in wanted)
        row = self.db.conn.execute(
            f"SELECT COUNT(*) AS n FROM episodes WHERE state IN ({placeholders})",
            tuple(wanted),
        ).fetchone()
        return int(row["n"])

    def run_until_idle(
        self,
        handlers: Mapping[object, Handler],
        *,
        deadline: Optional[float] = None,
        poll: float = 0.02,
        max_jobs: Optional[int] = None,
    ) -> RunResult:
        """Drain the queue.

        Keeps going while any row remains in a handled state -- including rows
        currently leased by *other* (possibly dead) workers, which is what lets a
        restarted worker pick up after a crash once the lease expires.
        """
        result = RunResult()
        states = [coerce(k).value for k in handlers]
        jobs = 0
        while True:
            if deadline is not None and self._now() >= deadline:
                result.timed_out = self.pending(states) > 0
                return result
            if self.pending(states) == 0:
                return result
            lease = self.run_once(handlers, result)
            if lease is None:
                result.idle_polls += 1
                time.sleep(poll)
                continue
            jobs += 1
            if max_jobs is not None and jobs >= max_jobs:
                return result

    # ---------------------------------------------------------------- helpers

    def _state_values(self, states: Optional[Sequence[object]]) -> list[str]:
        if states is None:
            chosen: Iterable[object] = WORKING_STATES
        else:
            chosen = states
        values = []
        for s in chosen:
            st = coerce(s)
            if st in TERMINAL_STATES:
                raise ValueError(f"{st.value} is terminal; nothing to claim there")
            values.append(st.value)
        if not values:
            raise ValueError("no states to claim from")
        return values
