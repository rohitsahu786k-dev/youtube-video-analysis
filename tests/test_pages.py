"""Every workspace page renders (empty and populated), JavaScript parses, and key honesty statements are present."""
import re
import subprocess
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pyapp.main import app

PAGES = ["/", "/sources", "/videos", "/library", "/qa", "/editor", "/backtest", "/runs", "/compare", "/integrations", "/login",
         "/charts", "/playbook", "/patterns", "/trades", "/knowledge", "/processing", "/backtesting", "/settings", "/chat"]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_empty(path):
    response = TestClient(app).get(path)
    assert response.status_code == 200, path
    assert "Traceback" not in response.text and "{{" not in response.text


def test_journal_and_analytics_guide_new_users_then_redirect_to_the_latest_run():
    client = TestClient(app)
    assert "No backtest has been run yet" in client.get("/journal").text
    assert "No backtest has been run yet" in client.get("/analytics").text


def test_sidebar_has_all_ten_sections_plus_integrations():
    html = TestClient(app).get("/").text
    for label in ("Source Manager", "Video Library", "Strategy Library", "AI Trading Q", "Editor &amp; Approval",
                  "Backtesting Workspace", "Trade Journal", "Analytics", "Comparison", "Integrations"):
        assert label in html, label


def test_populated_pages_render(tmp_path):
    import asyncio
    from helpers import random_market
    from test_lab_api import SPEC, csv_bytes, run_all_jobs
    client = TestClient(app)
    data = client.post("/api/market-data/csv", files={"file": ("d.csv", csv_bytes(), "text/csv")}, data={"symbol": "EURUSD", "timeframe": "M15", "provider": "Vendor"}).json()
    strategy = client.post("/api/library/custom", json={"name": "Page test"}).json()
    version = client.post("/api/versions", json={"strategyId": strategy["id"], "spec": SPEC}).json()
    client.post(f"/api/versions/{version['id']}/approve", json={"acknowledged": []})
    job = client.post("/api/runs", json={"versionId": version["id"], "config": {"dataset_id": data["id"], "symbol": "EURUSD", "costs": {"spread": 0.0001}, "causality_samples": 2}}).json()
    run_all_jobs()
    run_id = client.get(f"/api/jobs/{job['id']}").json()["result"]["runId"]
    for path in (f"/runs/{run_id}", f"/library/{strategy['id']}", f"/editor?strategy={strategy['id']}&version={version['id']}", f"/backtest?version={version['id']}", "/runs", "/dashboard"):
        r = client.get(path)
        assert r.status_code == 200 and "Traceback" not in r.text, path
    assert client.get("/journal", follow_redirects=False).headers["location"] == f"/runs/{run_id}#journal"
    assert client.get("/analytics", follow_redirects=False).headers["location"] == f"/runs/{run_id}#overview"
    backtest_page = client.get("/backtest").text
    assert "Page test" in backtest_page and "EURUSD" in backtest_page


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_all_javascript_parses_and_inline_page_scripts_compile(tmp_path):
    for path in Path("pyapp/static/js").glob("*.js"):
        assert subprocess.run(["node", "--check", str(path)], capture_output=True).returncode == 0, path
    client = TestClient(app)
    for page in ("/sources", "/library", "/qa", "/editor", "/backtest", "/runs", "/compare", "/integrations", "/videos"):
        html = client.get(page).text
        for n, script in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
            if "{%" in script or "{{" in script:
                continue
            file = tmp_path / f"{page.strip('/').replace('/', '_') or 'home'}_{n}.js"
            file.write_text(script, encoding="utf-8")
            result = subprocess.run(["node", "--check", str(file)], capture_output=True, text=True)
            assert result.returncode == 0, f"{page} script {n}: {result.stderr[:300]}"
