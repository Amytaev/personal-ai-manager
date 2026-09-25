from __future__ import annotations

import pytest

from storage.database import Database


@pytest.fixture
def tmp_db(tmp_path) -> Database:
    return Database(tmp_path / "test_agent.db")
