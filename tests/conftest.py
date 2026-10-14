import pytest
from pyapp.config import settings
from pyapp.db import Database
import pyapp.ai as ai
import pyapp.main as main
import pyapp.pipeline as pipeline
import pyapp.worker as worker
import pyapp.routes.api as api
import pyapp.routes.web as web
import pyapp.routes.research as research
import pyapp.proposals as proposals

@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    database = Database(tmp_path / "test.sqlite3")
    for module in (ai, main, pipeline, worker, api, web, research, proposals):
        monkeypatch.setattr(module, "db", database)
    monkeypatch.setattr(settings, "MOCK_AI_MODE", True)
    return database
