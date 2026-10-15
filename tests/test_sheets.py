"""Google Sheets export against a faithful in-memory fake of the Sheets REST API (no network)."""
import asyncio
import json
import re
from urllib.parse import unquote

import httpx

from helpers import random_market
from pyapp import sheets
from pyapp.config import settings
from pyapp.db import db
from pyapp.engine.journal import journal_rows
from pyapp.engine.runner import execute_backtest, parse_spec
from pyapp.engine.spec import BacktestConfig

SPEC = {"name": "EMA cross", "timeframe": "M15", "series": {"ema": {"type": "ema", "period": 20}, "atr": {"type": "atr", "period": 14}},
        "entry": {"long": {"all": [{"left": "close", "op": ">", "right": "ema"}, {"left": {"s": "close", "shift": 1}, "op": "<=", "right": {"s": "ema", "shift": 1}}]}},
        "stop": {"type": "atr", "mult": 1.5, "series": "atr"}, "targets": [{"type": "rr", "value": 1.5}]}


class FakeSheets:
    def __init__(self):
        self.sheets = {}                 # title -> {row_number: [values]}
        self.requests = []

    def handler(self, request: httpx.Request):
        path, method = unquote(request.url.path), request.method
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if method == "POST" and path == "/v4/spreadsheets":
            for s in body["sheets"]:
                self.sheets[s["properties"]["title"]] = {}
            return httpx.Response(200, json={"spreadsheetId": "sid" + "x" * 20, "sheets": [{"properties": {"title": t, "sheetId": n}} for n, t in enumerate(self.sheets)]})
        if method == "GET" and path.endswith("/values/'Trade Journal'!A:A"):
            rows = self.sheets["Trade Journal"]
            return httpx.Response(200, json={"values": [[rows[n][0]] if n in rows else [] for n in range(1, max(rows, default=0) + 1)]})
        if method == "GET":
            return httpx.Response(200, json={"sheets": [{"properties": {"title": t, "sheetId": n}} for n, t in enumerate(self.sheets)]})
        if path.endswith("/values:batchUpdate"):
            for item in body["data"]:
                match = re.match(r"'(.+)'!([A-Z]+)(\d+)(?::[A-Z]+(\d+))?$", item["range"])
                title, first = match.group(1), int(match.group(3))
                for offset, row in enumerate(item["values"]):
                    self.sheets[title][first + offset] = row
            return httpx.Response(200, json={})
        if path.endswith("/values:batchClear"):
            for r in body["ranges"]:
                self.sheets[r.strip("'")] = {}
            return httpx.Response(200, json={})
        if path.endswith(":batchUpdate"):
            return httpx.Response(200, json={"replies": []})
        return httpx.Response(404)


def make_run():
    cs = random_market(1200, "M15", 7)
    cfg = BacktestConfig.model_validate({"dataset_id": "dataset-x", "symbol": "EURUSD", "costs": {"spread": 0.0001}, "causality_samples": 0})
    report = execute_backtest(SPEC, cfg, cs, None, {"strategyHash": "h", "versionLabel": "v1", "id": "run1234567"})
    trades = report.pop("trades")
    report.pop("events")
    for t in trades:
        t["id"] = f"run12345-T{t['seq']:04d}"
    rows = journal_rows(report, trades, parse_spec(SPEC), cfg, "v1")
    return report, rows


def test_export_creates_then_updates_without_duplicating_rows():
    report, rows = make_run()
    assert len(rows) > 5
    fake = FakeSheets()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    async def go():
        first = await sheets.export_run(report, rows, None, sheet=None, title="My journal", client=client, token="tok")
        second = await sheets.export_run(report, rows, None, sheet=first["spreadsheetId"], title=None, client=client, token="tok")
        longer = rows + [{**rows[0], "tradeId": "run12345-T9999", "netPnl": 1.23}]
        third = await sheets.export_run(report, longer, None, sheet=f"https://docs.google.com/spreadsheets/d/{first['spreadsheetId']}/edit", title=None, client=client, token="tok")
        await client.aclose()
        return first, second, third
    first, second, third = asyncio.run(go())
    assert first["created"] and first["rowsAppended"] == len(rows) and first["rowsUpdated"] == 0
    assert not second["created"] and second["rowsAppended"] == 0 and second["rowsUpdated"] == len(rows)
    assert third["rowsAppended"] == 1 and third["rowsUpdated"] == len(rows)
    journal = fake.sheets["Trade Journal"]
    ids = [row[0] for n, row in sorted(journal.items()) if n > 1]
    assert len(ids) == len(set(ids)) == len(rows) + 1                       # three exports, still one row per trade
    assert journal[1][0] == "Trade ID"
    for title in sheets.SHEETS:
        assert fake.sheets[title], title                                      # all five sheets are populated
    summary = {row[0]: row[1] for row in fake.sheets["Performance Summary"].values() if len(row) > 1}
    assert summary["Journal rows"] == len(rows) + 1 or summary["Journal rows"] == len(rows)


def test_spreadsheet_id_parsing_and_oauth_state():
    assert sheets.spreadsheet_id("https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit#gid=0") == "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    try:
        sheets.spreadsheet_id("not a sheet")
        raise AssertionError("should reject")
    except sheets.SheetsError:
        pass
    settings.GOOGLE_CLIENT_ID, settings.GOOGLE_CLIENT_SECRET, settings.SECRET_KEY = "cid", "secret", "k" * 40
    try:
        url = sheets.authorization_url("admin")
        assert "access_type=offline" in url and "spreadsheets" in url and "state=" in url
        state = re.search(r"state=([^&]+)", url).group(1)
        assert sheets.verify_state(unquote(state)) == "admin"
        try:
            sheets.verify_state("tampered")
            raise AssertionError("should reject")
        except sheets.SheetsError:
            pass
    finally:
        settings.GOOGLE_CLIENT_ID = settings.GOOGLE_CLIENT_SECRET = settings.SECRET_KEY = None


def test_refresh_token_is_stored_encrypted():
    settings.GOOGLE_CLIENT_ID, settings.GOOGLE_CLIENT_SECRET, settings.SECRET_KEY = "cid", "secret", "k" * 40
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"refresh_token": "1//super-secret", "access_token": "at", "scope": sheets.SCOPE}))
    try:
        async def go():
            async with httpx.AsyncClient(transport=transport) as client:
                await sheets.exchange_code("code", client)
            return await db.get("integrations", "google")
        record = asyncio.run(go())
    finally:
        settings.GOOGLE_CLIENT_ID = settings.GOOGLE_CLIENT_SECRET = settings.SECRET_KEY = None
    assert record["connected"] and "super-secret" not in json.dumps(record)
