import asyncio
from unittest.mock import AsyncMock
from pyapp.intelligence import build_patterns
import pyapp.proposals as proposals

def lesson(identifier, video):
    return {"id":identifier,"videoId":video,"video":"Test","time":10,"type":"Spoken","title":"Sweep","content":"Wait for sweep rejection",
            "synthesized":True,"strategyTag":"liquidity-sweep-rejection","symbol":"EURUSD","timeframe":"M5","direction":"LONG", "evidenceCutoff":"2020-01-01T00:00:00+00:00"}

def test_teaching_patterns_have_no_invented_trade_stats():
    pattern=build_patterns([], [lesson("a","one"),lesson("b","two")])[0]
    assert pattern["status"]=="RECURRING"
    assert pattern["kind"]=="TEACHING"
    assert pattern["winRate"] is None
    assert pattern["tradeIds"]==[]
    assert build_patterns([], [lesson("a","one"),lesson("b","one")])[0]["status"]=="CANDIDATE"

def test_rule_drafting_is_automatic_idempotent_and_unapproved(isolated_database, monkeypatch):
    draft={"kind":"sweep_rejection","direction":"LONG","timeframe":"M5","lookback":None,"rewardRisk":None,"stopBuffer":None,"maxHoldingBars":None,"reason":"Numeric rule parameters were not specified.","sourceQuotes":[]}
    compiler=AsyncMock(return_value=draft)
    monkeypatch.setattr(proposals,"propose_rule",compiler)
    async def run():
        for item in [lesson("a","one"),lesson("b","two")]: await isolated_database.save("knowledge",item)
        await proposals.discover_proposals()
        await proposals.discover_proposals()
        records=await isolated_database.list("proposals")
        assert len(records)==1
        assert not records[0]["reviewed"]
        assert records[0]["lookback"] is None
        assert compiler.await_count==1
    asyncio.run(run())
