import json
import re

import pytest
from fastapi.testclient import TestClient

from pyapp.main import app

client = TestClient(app)


@pytest.mark.parametrize("symbol,expected", [
    ("EURUSD", "FX:EURUSD"),
    ("eur/usd", "FX:EURUSD"),
    ("NASDAQ:AAPL", "NASDAQ:AAPL"),
    ("BINANCE:BTCUSDT", "BINANCE:BTCUSDT"),
])
def test_chart_config(symbol, expected):
    response = client.get("/charts", params={"symbol": symbol, "interval": "240", "theme": "dark"})
    assert response.status_code == 200
    match = re.search(r'<script id="tradingview-config" type="application/json">(.*?)</script>', response.text)
    config = json.loads(match.group(1))
    assert config["symbol"] == expected
    assert config["interval"] == "240"
    assert config["theme"] == "dark"
    assert config["timezone"] == "Asia/Kolkata"
    assert "TRADINGVIEW_PASSWORD" not in response.text


@pytest.mark.parametrize("params", [
    {"symbol": "</script><script>alert(1)</script>"},
    {"symbol": ""},
    {"symbol": "A" * 65},
    {"interval": "invalid"},
    {"theme": "invalid"},
])
def test_invalid_chart_options(params):
    assert client.get("/charts", params=params).status_code == 422
