"""Fixtures for the rebuild test suite.

Everything here is offline and every database lives under ``tmp_path``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from rebuild_support import FakeClock  # noqa: E402


@pytest.fixture(scope="session")
def python_exe() -> str:
    """Interpreter for spawned worker processes: the same one running pytest."""
    return sys.executable


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "podcleaner.db"


@pytest.fixture
def db(db_path: Path):
    from podcleaner.core.db import Database

    database = Database(db_path)
    yield database
    database.close()
