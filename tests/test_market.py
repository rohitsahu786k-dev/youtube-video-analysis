import asyncio
import httpx
import pytest
from pyapp.config import settings
import pyapp.market as market

def test_oanda_history_filters_incomplete_candles_and_works_without_quotes(monkeypatch):
    client_class = httpx.AsyncClient
    calls = []
    def handle(request):
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.url.params["granularity"] == "M1"
        assert request.url.params["price"] == "M"
        return httpx.Response(200, json={"candles":[
            {"time":"2026-10-01T00:00:00Z","complete":True,"mid":{"o":"10","h":"11","l":"9","c":"10"}},
            {"time":"2026-10-01T00:01:00Z","complete":False,"mid":{"o":"10","h":"11","l":"9","c":"10"}}]})
    monkeypatch.setattr(settings,"OANDA_API_TOKEN","test-token")
    monkeypatch.setattr(settings,"OANDA_ACCOUNT_ID","test-account")
    monkeypatch.setattr(market.httpx,"AsyncClient",lambda **kwargs: client_class(transport=httpx.MockTransport(handle),**kwargs))
    result=asyncio.run(market.oanda_snapshot("EURUSD","M1",100,include_quote=False))
    assert len(result["candles"])==1
    assert result["provider"]=="OANDA"
    assert not any("pricing" in call for call in calls)

def test_oanda_closed_market_cannot_produce_live_quotes(monkeypatch):
    client_class=httpx.AsyncClient
    def handle(request):
        return httpx.Response(200,json={"prices":[{"status":"non-tradeable"}]} if "pricing" in request.url.path else {"candles":[]})
    monkeypatch.setattr(settings,"OANDA_API_TOKEN","test-token")
    monkeypatch.setattr(settings,"OANDA_ACCOUNT_ID","test-account")
    monkeypatch.setattr(market.httpx,"AsyncClient",lambda **kwargs: client_class(transport=httpx.MockTransport(handle),**kwargs))
    with pytest.raises(ValueError,match="Market is closed"):
        asyncio.run(market.oanda_snapshot("EURUSD","M1"))
