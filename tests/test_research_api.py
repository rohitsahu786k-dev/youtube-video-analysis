import asyncio
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from pyapp.main import app
from pyapp.intelligence import build_patterns
from pyapp.backtest import rule_hash

def seed_pattern(database):
    async def run():
        for i in range(2):
            await database.save_trade(dict(id=f"trade-{i}", videoId=f"video-{i}", symbol="EURUSD", direction="LONG", setup="Sweep", timeframe="M1", validation="VALID",
                result="UNKNOWN", rr=2., confidence=.8, evidenceCutoff="2020-01-01T00:00:00+00:00"))
        return build_patterns(await database.get_trades())[0]
    return asyncio.run(run())

def csv_data():
    start = datetime(2026,1,1,tzinfo=timezone.utc)
    rows = ["time,open,high,low,close"]
    for i in range(120):
        rows.append(f"{(start+timedelta(minutes=i)).isoformat()},10,11,9,10")
    return "\n".join(rows).encode()

def test_full_research_api_flow(isolated_database):
    pattern = seed_pattern(isolated_database)
    client = TestClient(app)
    strategy = client.post("/api/strategies", json={"patternId":pattern["id"], "timeframe":"M1", "reviewed":True,
        "rule":{"kind":"sweep_rejection", "direction":"LONG", "lookback":2, "rewardRisk":2, "stopBuffer":0, "maxHoldingBars":5}})
    assert strategy.status_code == 201
    dataset = client.post("/api/datasets/csv", files={"file":("history.csv",csv_data(),"text/csv")}, data={"symbol":"EURUSD", "timeframe":"M1", "provider":"OANDA"})
    assert dataset.status_code == 201
    report = client.post("/api/backtests", json={"strategyId":strategy.json()["id"], "datasetId":dataset.json()["id"], "spread":.1, "slippage":.01})
    assert report.status_code == 201
    assert not report.json()["eligibleForLive"]
    assert report.json()["holdout"]["winRate"] is None
    assert client.get("/backtesting").status_code == 200
    assert client.get("/patterns").status_code == 200
    assert client.get("/playbook").status_code == 200
    assert client.get("/api/backtests/"+report.json()["id"]).status_code == 200
    assert client.get("/api/signals?symbol=EURUSD&timeframe=M1").json()["status"] == "WAIT"

def test_timeframe_mismatch_cannot_operationalize_pattern(isolated_database):
    pattern = seed_pattern(isolated_database)
    response = TestClient(app).post("/api/strategies", json={"patternId":pattern["id"], "timeframe":"H4", "reviewed":True,
        "rule":{"kind":"sweep_rejection", "direction":"LONG", "lookback":2, "rewardRisk":2, "stopBuffer":0, "maxHoldingBars":5}})
    assert response.status_code == 400

def test_changed_source_evidence_invalidates_live_strategy(isolated_database):
    pattern = seed_pattern(isolated_database)
    client=TestClient(app)
    strategy=client.post("/api/strategies",json={"patternId":pattern["id"], "timeframe":"M1", "reviewed":True,
        "rule":{"kind":"sweep_rejection", "direction":"LONG", "lookback":2, "rewardRisk":2, "stopBuffer":0, "maxHoldingBars":5}}).json()
    async def change():
        trade=await isolated_database.get_trade("trade-0")
        trade["validation"]="INVALID"
        await isolated_database.save_trade(trade)
    asyncio.run(change())
    result=client.get("/api/signals?symbol=EURUSD&timeframe=M1").json()
    assert result["status"] == "WAIT"
    assert "Source evidence changed" in result["reason"]

def test_chat_empty_store_returns_no_fabricated_sources():
    response=TestClient(app).post("/api/chat",json={"question":"What is the winning strategy?"})
    assert response.status_code == 200
    assert response.json()["sources"] == []
    assert "No matching" in response.json()["answer"]

def test_wrong_csv_and_no_provider_fail_explicitly():
    client=TestClient(app)
    assert client.post("/api/datasets/csv",files={"file":("bad.csv",b"wrong,columns\n1,2")},data={"symbol":"EURUSD","timeframe":"M1","provider":"Test"}).status_code==400
    assert client.get("/api/signals?symbol=BTCUSDT").status_code==400
    assert client.post("/api/backtests",json={"strategyId":"none","datasetId":"none","spread":.1,"slippage":0}).status_code==404
