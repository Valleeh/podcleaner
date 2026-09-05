"""SQLite schema, connection handling and episode row helpers.

Design notes
------------

* **WAL.** ``journal_mode=WAL`` lets many readers run alongside one writer, which
  is what makes an 8-worker pool viable in a single file (contract S2.6).
* **``synchronous=FULL``.** Slower than ``NORMAL`` but it is the honest setting
  for a durability claim: a committed transaction is on disk before we return.
* **``BEGIN IMMEDIATE`` for every write.** A deferred transaction that starts as
  a reader and later upgrades to a writer can get ``SQLITE_BUSY`` *without* the
  busy handler being consulted, which is the usual source of spurious
  "database is locked" errors under WAL. Taking the write lock up front, plus a
  generous ``busy_timeout``, removes that failure mode.
* **No ORM, no connection pool.** One connection per thread per
  :class:`Database`; workers are separate OS processes.

Nothing in this module imports ``podcleaner.services``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional

from .states import (
    State,
    coerce,
    validate_transition,
)

__all__ = [
    "Database",
    "DuplicateGuid",
    "EpisodeNotFound",
    "SCHEMA_VERSION",
    "ConcurrentModification",
    "connect",
    "migrate",
]

SCHEMA_VERSION = 1

#: Generous on purpose. Under WAL with short write transactions the real wait is
#: milliseconds; the large timeout exists so that a slow machine produces a slow
#: test rather than a flaky "database is locked".
DEFAULT_BUSY_TIMEOUT_MS = 30_000


class DuplicateGuid(Exception):
    """Raised by :meth:`Database.add_episode` when the GUID already exists."""


class EpisodeNotFound(Exception):
    """Raised when an episode id/guid does not resolve to a row."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per podcast episode. This table *is* the pipeline: there is no
-- in-memory queue and no per-container JSON set of processed files.
CREATE TABLE IF NOT EXISTS episodes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guid           TEXT    NOT NULL UNIQUE,      -- feed GUID; the idempotency key
    feed_url       TEXT,
    audio_url      TEXT,
    title          TEXT,

    state          TEXT    NOT NULL DEFAULT 'discovered',

    -- Lease bookkeeping. claimed_at IS NULL  <=> nobody holds the row.
    claimed_at     REAL,                          -- epoch seconds of the claim
    lease_seconds  REAL,                          -- lease granted at claim time
    lease_token    TEXT,                          -- fencing token for this claim
    owner          TEXT,                          -- worker id, for humans

    attempts       INTEGER NOT NULL DEFAULT 0,    -- abandoned/failed tries
    max_attempts   INTEGER NOT NULL DEFAULT 5,
    last_error     TEXT,

    payload        TEXT,                          -- opaque JSON scratch space
    created_at     REAL    NOT NULL,
    updated_at     REAL    NOT NULL,

    CHECK (state IN ('discovered','fetched','analyzed','cut','published','failed')),
    CHECK (attempts >= 0)
);

-- The claim query's index: eligible rows are (state, claimed_at) shaped.
CREATE INDEX IF NOT EXISTS idx_episodes_claimable
    ON episodes (state, claimed_at, updated_at, id);

-- Append-only audit of every state change that was actually committed.
-- One row here == one committed unit of pipeline progress. This is the
-- exactly-once ledger the contract's S2.3 counts.
CREATE TABLE IF NOT EXISTS transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    guid        TEXT    NOT NULL,
    from_state  TEXT    NOT NULL,
    to_state    TEXT    NOT NULL,
    worker      TEXT,
    lease_token TEXT,
    note        TEXT,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_episode
    ON transitions (episode_id, to_state);

-- Append-only audit of every time a worker *started* doing the work for a
-- state. Under correct operation this is 1:1 with `transitions`; a crash makes
-- it at-least-once, and duplicate concurrent claims would make it >1 with no
-- crash, which is exactly what S2.3 / S2.7 look for.
CREATE TABLE IF NOT EXISTS work_executions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    guid        TEXT    NOT NULL,
    state       TEXT    NOT NULL,
    worker      TEXT    NOT NULL,
    lease_token TEXT    NOT NULL,
    pid         INTEGER,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_executions_episode
    ON work_executions (episode_id, state);

-- Append-only audit of every lease ever granted, with its validity window.
-- Two leases on the same episode whose [claimed_at, effective_end) windows
-- overlap is the definition of a double-lease bug (S2.7).
CREATE TABLE IF NOT EXISTS leases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    lease_token TEXT    NOT NULL UNIQUE,
    worker      TEXT    NOT NULL,
    pid         INTEGER,
    state       TEXT    NOT NULL,
    claimed_at  REAL    NOT NULL,
    expires_at  REAL    NOT NULL,
    released_at REAL
);
CREATE INDEX IF NOT EXISTS idx_leases_episode ON leases (episode_id, claimed_at);
"""


def connect(
    path: "str | os.PathLike[str]",
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    timeout: float = 30.0,
    synchronous: str = "FULL",
) -> sqlite3.Connection:
    """Open a connection with the pragmas this design depends on.

    ``isolation_level=None`` turns off Python's implicit transaction management
    so that transaction boundaries are explicit and always ``BEGIN IMMEDIATE``.
    """
    conn = sqlite3.connect(
        os.fspath(path),
        timeout=timeout,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":  # pragma: no cover - only on exotic filesystems
        conn.close()
        raise RuntimeError(f"could not enable WAL (journal_mode={mode!r})")
    conn.execute(f"PRAGMA synchronous = {synchronous}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Idempotent schema creation. Safe to call from every process, always."""
    # NB: executescript() implicitly commits before it runs, so the transaction
    # has to live inside the script text rather than around the call.
    conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class Database:
    """A SQLite database plus the small set of row operations the core needs.

    A :class:`Database` owns one connection *per thread*; separate worker
    processes each construct their own.
    """

    def __init__(
        self,
        path: "str | os.PathLike[str]",
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        timeout: float = 30.0,
        synchronous: str = "FULL",
        create: bool = True,
        now_fn=time.time,
    ):
        self.path = os.fspath(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._timeout = timeout
        self._synchronous = synchronous
        self._now = now_fn
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        if create:
            migrate(self.conn)

    # ---------------------------------------------------------------- plumbing

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(
                self.path,
                busy_timeout_ms=self._busy_timeout_ms,
                timeout=self._timeout,
                synchronous=self._synchronous,
            )
            self._local.conn = conn
            with self._conn_lock:
                self._all_conns.append(conn)
        return conn

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """An explicit ``BEGIN IMMEDIATE`` .. ``COMMIT`` block.

        The write lock is taken at ``BEGIN``, so the busy handler (and therefore
        ``busy_timeout``) applies and lock upgrades can never deadlock.
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - connection already dead
                pass
            raise
        else:
            conn.execute("COMMIT")

    @contextmanager
    def read_tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        conn.execute("BEGIN DEFERRED")
        try:
            yield conn
        finally:
            conn.execute("COMMIT")

    def journal_mode(self) -> str:
        return str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def close(self) -> None:
        with self._conn_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
        self._local = threading.local()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------- episode I/O

    def add_episode(
        self,
        guid: str,
        *,
        feed_url: Optional[str] = None,
        audio_url: Optional[str] = None,
        title: Optional[str] = None,
        state: object = State.DISCOVERED,
        max_attempts: int = 5,
        payload: Optional[str] = None,
        if_exists: str = "raise",
    ) -> int:
        """Insert an episode and return its id.

        ``if_exists`` is ``"raise"`` (default, -> :class:`DuplicateGuid`) or
        ``"ignore"`` (return the existing row's id). GUID uniqueness is enforced
        by the schema, so two schedulers racing on the same feed cannot create
        two rows for one episode.
        """
        if if_exists not in ("raise", "ignore"):
            raise ValueError(f"if_exists must be 'raise' or 'ignore', got {if_exists!r}")
        st = coerce(state)
        try:
            with self.write_tx() as conn:
                now = self._now()
                cur = conn.execute(
                    """
                    INSERT INTO episodes
                        (guid, feed_url, audio_url, title, state,
                         max_attempts, payload, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (guid, feed_url, audio_url, title, st.value,
                     max_attempts, payload, now, now),
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "guid" not in str(exc).lower():
                raise
            if if_exists == "ignore":
                row = self.get_episode(guid=guid)
                return int(row["id"])
            raise DuplicateGuid(f"episode guid already present: {guid!r}") from exc

    def add_episodes(self, guids: Iterable[str], **kwargs: Any) -> list[int]:
        return [self.add_episode(g, **kwargs) for g in guids]

    def get_episode(
        self, episode_id: Optional[int] = None, *, guid: Optional[str] = None
    ) -> sqlite3.Row:
        if (episode_id is None) == (guid is None):
            raise ValueError("pass exactly one of episode_id or guid")
        if episode_id is not None:
            row = self.conn.execute(
                "SELECT * FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
            key: Any = episode_id
        else:
            row = self.conn.execute(
                "SELECT * FROM episodes WHERE guid = ?", (guid,)
            ).fetchone()
            key = guid
        if row is None:
            raise EpisodeNotFound(f"no episode for {key!r}")
        return row

    def list_episodes(self, state: object = None) -> list[sqlite3.Row]:
        if state is None:
            return list(self.conn.execute("SELECT * FROM episodes ORDER BY id"))
        return list(
            self.conn.execute(
                "SELECT * FROM episodes WHERE state = ? ORDER BY id",
                (coerce(state).value,),
            )
        )

    def count_by_state(self) -> dict[str, int]:
        return {
            r["state"]: r["n"]
            for r in self.conn.execute(
                "SELECT state, COUNT(*) AS n FROM episodes GROUP BY state"
            )
        }

    # --------------------------------------------------------------- mutations

    def transition(
        self,
        episode_id: int,
        to_state: object,
        *,
        expected_state: object = None,
        worker: Optional[str] = None,
        note: Optional[str] = None,
        clear_lease: bool = True,
    ) -> State:
        """Validated state change, outside of the lease protocol.

        Validation happens *before* any write and the whole thing runs in one
        transaction, so an :class:`~podcleaner.core.states.IllegalTransition`
        leaves the row byte-for-byte unmodified (contract S2.1).
        """
        with self.write_tx() as conn:
            row = conn.execute(
                "SELECT id, guid, state FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
            if row is None:
                raise EpisodeNotFound(f"no episode with id {episode_id!r}")
            current = coerce(row["state"])
            if expected_state is not None and current is not coerce(expected_state):
                raise ConcurrentModification(
                    f"episode {episode_id} is in {current.value!r}, "
                    f"expected {coerce(expected_state).value!r}"
                )
            # Raises before we touch anything.
            src, dst = validate_transition(current, to_state)
            now = self._now()  # inside the write lock
            if clear_lease:
                conn.execute(
                    """
                    UPDATE episodes
                       SET state = ?, updated_at = ?,
                           claimed_at = NULL, lease_token = NULL,
                           lease_seconds = NULL, owner = NULL
                     WHERE id = ?
                    """,
                    (dst.value, now, episode_id),
                )
            else:
                conn.execute(
                    "UPDATE episodes SET state = ?, updated_at = ? WHERE id = ?",
                    (dst.value, now, episode_id),
                )
            conn.execute(
                """
                INSERT INTO transitions
                    (episode_id, guid, from_state, to_state, worker, lease_token,
                     note, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (episode_id, row["guid"], src.value, dst.value, worker, note, now),
            )
            return dst

    def force_state(self, episode_id: int, state: object) -> None:
        """Set a state with **no** validation.

        Test/repair scaffolding only: it exists so the S2.1 test can place a row
        in every state without going through the very code path it is testing.
        Never call this from pipeline code.
        """
        st = coerce(state)
        with self.write_tx() as conn:
            conn.execute(
                "UPDATE episodes SET state = ?, updated_at = ? WHERE id = ?",
                (st.value, self._now(), episode_id),
            )

    # ------------------------------------------------------------------ audits

    def transitions(self, episode_id: Optional[int] = None) -> list[sqlite3.Row]:
        if episode_id is None:
            return list(self.conn.execute("SELECT * FROM transitions ORDER BY id"))
        return list(
            self.conn.execute(
                "SELECT * FROM transitions WHERE episode_id = ? ORDER BY id",
                (episode_id,),
            )
        )

    def work_executions(self, episode_id: Optional[int] = None) -> list[sqlite3.Row]:
        if episode_id is None:
            return list(self.conn.execute("SELECT * FROM work_executions ORDER BY id"))
        return list(
            self.conn.execute(
                "SELECT * FROM work_executions WHERE episode_id = ? ORDER BY id",
                (episode_id,),
            )
        )

    def leases(self, episode_id: Optional[int] = None) -> list[sqlite3.Row]:
        if episode_id is None:
            return list(self.conn.execute("SELECT * FROM leases ORDER BY id"))
        return list(
            self.conn.execute(
                "SELECT * FROM leases WHERE episode_id = ? ORDER BY claimed_at, id",
                (episode_id,),
            )
        )

    def overlapping_leases(
        self, limit: int = 50
    ) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
        """Pairs of leases on the same episode whose validity windows overlap.

        A lease is valid from ``claimed_at`` until it is released, or until it
        expires, whichever comes first. Under a correct claim protocol this list
        is always empty -- that is the S2.7 negative control.

        ``limit`` caps how many offending pairs are collected. The answer to
        "is the claim protocol broken?" is already given by the first pair, and
        a badly broken protocol can produce quadratically many of them, which
        should not be allowed to exhaust memory while a test is failing.
        """
        by_episode: dict[int, list[sqlite3.Row]] = {}
        for row in self.leases():
            by_episode.setdefault(row["episode_id"], []).append(row)
        bad: list[tuple[sqlite3.Row, sqlite3.Row]] = []
        for rows in by_episode.values():
            windows = []
            for r in rows:
                end = r["expires_at"]
                if r["released_at"] is not None:
                    end = min(end, r["released_at"])
                windows.append((r["claimed_at"], end, r))
            windows.sort(key=lambda w: w[0])
            for i in range(len(windows) - 1):
                start_i, end_i, row_i = windows[i]
                for j in range(i + 1, len(windows)):
                    start_j, _end_j, row_j = windows[j]
                    if start_j >= end_i:
                        break
                    bad.append((row_i, row_j))
                    if len(bad) >= limit:
                        return bad
        return bad


class ConcurrentModification(Exception):
    """Raised when a compare-and-swap on ``episodes.state`` loses the race."""
