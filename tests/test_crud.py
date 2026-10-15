"""Create / read / update / delete coverage for every user-managed object."""
import asyncio

from fastapi.testclient import TestClient

from pyapp.db import db
from pyapp.main import app
from test_lab_api import SPEC, csv_bytes, run_all_jobs


def test_strategy_version_run_lifecycle_crud():
    c = TestClient(app)
    s = c.post("/api/library/custom", json={"name": "Mine"}).json()
    assert c.patch(f"/api/library/strategies/{s['id']}", json={"name": "Renamed", "notes": "hello", "starred": True}).json()["name"] == "Renamed"
    assert c.get(f"/api/library/strategies/{s['id']}").json()["notes"] == "hello"
    assert [x["name"] for x in c.get("/api/library/strategies?q=renam").json()] == ["Renamed"]
    assert c.get("/api/library/strategies?q=zzz").json() == []
    v = c.post("/api/versions", json={"strategyId": s["id"], "spec": SPEC}).json()
    assert c.delete(f"/api/versions/{v['id']}").status_code == 200 and c.get(f"/api/versions/{v['id']}").status_code == 404
    v = c.post("/api/versions", json={"strategyId": s["id"], "spec": SPEC}).json()
    c.post(f"/api/versions/{v['id']}/approve", json={"acknowledged": []})
    assert c.delete(f"/api/versions/{v['id']}").status_code == 409            # approved versions are kept
    assert c.delete(f"/api/library/strategies/{s['id']}").status_code == 409
    d = c.post("/api/market-data/csv", files={"file": ("a.csv", csv_bytes(), "text/csv")}, data={"symbol": "EURUSD", "timeframe": "M15", "provider": "vendor"}).json()
    job = c.post("/api/runs", json={"versionId": v["id"], "config": {"dataset_id": d["id"], "symbol": "EURUSD", "costs": {"spread": 0.0001}, "causality_samples": 0}}).json()
    run_all_jobs()
    run_id = c.get(f"/api/jobs/{job['id']}").json()["result"]["runId"]
    assert c.patch(f"/api/runs/{run_id}", json={"label": "Baseline", "note": "n"}).json()["label"] == "Baseline"
    assert c.get("/api/runs").json()[0]["label"] == "Baseline"
    assert c.delete(f"/api/market-data/{d['id']}").status_code == 409         # referenced by a run
    assert c.delete(f"/api/runs/{run_id}").status_code == 200 and c.get(f"/api/runs/{run_id}").status_code == 404
    assert c.get(f"/api/runs/{run_id}/trades").status_code == 404
    assert c.delete(f"/api/market-data/{d['id']}").status_code == 200


def test_draft_only_strategy_can_be_deleted_with_its_data():
    c = TestClient(app)
    s = c.post("/api/library/custom", json={"name": "Temp"}).json()
    c.post("/api/versions", json={"strategyId": s["id"], "spec": SPEC})
    conv = c.post("/api/qa/conversations", json={"strategyId": s["id"]}).json()
    assert c.delete(f"/api/library/strategies/{s['id']}").status_code == 200
    assert c.get(f"/api/library/strategies/{s['id']}").status_code == 404
    assert c.get(f"/api/qa/conversations/{conv['id']}").status_code == 404
    assert c.get(f"/api/versions?strategyId={s['id']}").json() == []


def test_video_delete_removes_derived_evidence_and_conversations_crud():
    c = TestClient(app)
    async def seed():
        await db.save_video({"id": "video-zzzzzzzzzzz", "youtubeId": "zzzzzzzzzzz", "title": "T", "status": "COMPLETED"})
        await db.save("atoms", {"id": "a1", "videoId": "video-zzzzzzzzzzz"})
        await db.save("knowledge", {"id": "k1", "videoId": "video-zzzzzzzzzzz", "type": "Spoken", "content": "x"})
        await db.save_video({"id": "video-busybusyb11", "youtubeId": "busybusyb11", "title": "B", "status": "TRANSCRIBING"})
    asyncio.run(seed())
    assert c.delete("/api/videos/video-busybusyb11").status_code == 409
    assert c.delete("/api/videos/video-zzzzzzzzzzz").status_code == 200
    assert asyncio.run(db.list("atoms")) == [] and asyncio.run(db.list("knowledge")) == []
    assert c.delete("/api/videos/video-zzzzzzzzzzz").status_code == 404
    conv = c.post("/api/qa/conversations", json={}).json()
    assert c.get("/api/qa/conversations").json()[0]["id"] == conv["id"]
    assert c.delete(f"/api/qa/conversations/{conv['id']}").status_code == 200 and c.get("/api/qa/conversations").json() == []
