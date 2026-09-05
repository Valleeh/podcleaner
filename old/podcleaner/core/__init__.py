"""Durable core: SQLite-backed episode state machine, leases and workers.

This package is the replacement for the MQTT broker + in-memory dicts +
per-container ``processed_files.json`` sets used by ``podcleaner.services``.
It is purely additive; nothing here imports from ``podcleaner.services``.
"""

from .states import (
    State,
    IllegalTransition,
    LEGAL_TRANSITIONS,
    NEXT_STATE,
    TERMINAL_STATES,
    WORKING_STATES,
    ALL_STATES,
    is_legal,
    validate_transition,
    next_state,
)
from .db import Database, connect, migrate, DuplicateGuid
from .queue import WorkQueue, Lease, LeaseLost

__all__ = [
    "State",
    "IllegalTransition",
    "LEGAL_TRANSITIONS",
    "NEXT_STATE",
    "TERMINAL_STATES",
    "WORKING_STATES",
    "ALL_STATES",
    "is_legal",
    "validate_transition",
    "next_state",
    "Database",
    "connect",
    "migrate",
    "DuplicateGuid",
    "WorkQueue",
    "Lease",
    "LeaseLost",
]
