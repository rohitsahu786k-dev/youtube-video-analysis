import sys
from pathlib import Path
import pytest
from pyapp import budget, limits, qa, retrieval
from pyapp.config import settings
from pyapp.db import Database, db as db_proxy, owner_var

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Every test gets its own SQLite file, dataset directory and an unauthenticated local workspace."""
    database = Database(tmp_path / "test.sqlite3")
    original = db_proxy._impl
    db_proxy.use(database)
    monkeypatch.setattr(settings, "MOCK_AI_MODE", True)
    monkeypatch.setattr(settings, "STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", None)
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "BACKTEST_PROCESSES", False)
    limits._hits.clear()
    limits._disk_cache.update(at=0.0, value=None)
    budget._day_cache.clear()
    qa._answers.clear()
    retrieval._vector_cache.clear()
    token = owner_var.set("")
    yield database
    owner_var.reset(token)
    db_proxy.use(original)
