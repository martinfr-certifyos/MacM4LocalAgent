"""Shared pytest fixtures.

The whole suite isolates each test by pointing every module's `DB_PATH` at a
fresh temporary SQLite file. We don't touch the real cost/cost.db.
"""

from __future__ import annotations

import importlib
import pathlib
import sys
from typing import Iterator

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tmp_db(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[pathlib.Path]:
    """Redirect cost/router/bench DB to a tmp file. Yields the path."""
    db = tmp_path / "cost.db"

    import cost.ingest as ingest
    import router.route_by_size as rrs

    monkeypatch.setattr(ingest, "DB_PATH", db, raising=True)
    monkeypatch.setattr(rrs,    "DB_PATH", db, raising=True)

    # Reload helpers that capture DB_PATH at import time? Our impl reads it
    # dynamically inside functions, so reload is unnecessary, but be safe:
    importlib.reload(ingest)
    monkeypatch.setattr(ingest, "DB_PATH", db, raising=True)

    # Bench DB is also pinned so bench tests can share this fixture.
    try:
        import bench.db as bdb
        monkeypatch.setattr(bdb, "DB_PATH", db, raising=True)
    except ImportError:
        pass

    yield db


@pytest.fixture
def repo_root() -> pathlib.Path:
    return REPO_ROOT
