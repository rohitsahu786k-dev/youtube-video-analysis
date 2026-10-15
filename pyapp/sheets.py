"""Google Sheets export with OAuth. Rows are keyed by Trade ID so repeated exports update instead of duplicating.

Credentials: a Google Cloud OAuth *web* client (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET) with redirect URI {APP_URL}/api/google/callback.
Refresh tokens are stored encrypted (Fernet). Only the `spreadsheets` scope is requested.
"""
import re
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from pyapp.config import settings
from pyapp.db import db
from pyapp.engine.journal import JOURNAL_COLUMNS
from pyapp.exporters import describe_spec, safe, summary_pairs
from pyapp.vault import app_secret, decrypt, encrypt

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS = ("Trade Journal", "Performance Summary", "Strategy Rules", "Backtest Configuration", "Validation & Exceptions")


class SheetsError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def redirect_uri() -> str:
    return settings.APP_URL.rstrip("/") + "/api/google/callback"


def _state_signer():
    return URLSafeTimedSerializer(app_secret(), salt="google-oauth")


def authorization_url(user_id: str) -> str:
    if not configured():
        raise SheetsError("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to enable Google Sheets.")
    params = {"client_id": settings.GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code", "scope": SCOPE, "access_type": "offline", "prompt": "consent",
              "state": _state_signer().dumps({"u": user_id})}
    return AUTH_URL + "?" + urlencode(params)


def verify_state(state: str):
    try:
        return _state_signer().loads(state, max_age=900)["u"]
    except BadSignature as exc:
        raise SheetsError("The authorization request expired or was tampered with; start again.") from exc


async def exchange_code(code: str, client: httpx.AsyncClient | None = None) -> dict:
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        response = await client.post(TOKEN_URL, data={"code": code, "client_id": settings.GOOGLE_CLIENT_ID, "client_secret": settings.GOOGLE_CLIENT_SECRET,
                                                      "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})
    finally:
        if own:
            await client.aclose()
    if response.status_code != 200 or "refresh_token" not in response.json():
        raise SheetsError("Google did not return a refresh token; remove the app's access in your Google account and reconnect.")
    token = response.json()
    await db.save("integrations", {"id": "google", "refreshToken": encrypt(token["refresh_token"]), "scope": token.get("scope"), "connected": True})
    return {"connected": True}


async def access_token(client: httpx.AsyncClient | None = None) -> str:
    record = await db.get("integrations", "google")
    if not record or not record.get("refreshToken"):
        raise SheetsError("Google Sheets is not connected.")
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        response = await client.post(TOKEN_URL, data={"client_id": settings.GOOGLE_CLIENT_ID, "client_secret": settings.GOOGLE_CLIENT_SECRET,
                                                      "refresh_token": decrypt(record["refreshToken"]), "grant_type": "refresh_token"})
    finally:
        if own:
            await client.aclose()
    if response.status_code != 200:
        raise SheetsError("Google rejected the stored authorization; reconnect Google Sheets.")
    return response.json()["access_token"]


def spreadsheet_id(text: str) -> str:
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", text) or re.fullmatch(r"([A-Za-z0-9_-]{20,})", text.strip())
    if not match:
        raise SheetsError("Paste a Google Sheets URL or spreadsheet ID.")
    return match.group(1)


def _values(rows):
    return [[("" if v is None else safe(v) if isinstance(v, str) else v) for v in row] for row in rows]


def build_sheet_values(report: dict, rows: list[dict], version: dict | None) -> dict:
    journal = [[label for _, label in JOURNAL_COLUMNS]] + [[r.get(key) for key, _ in JOURNAL_COLUMNS] for r in rows]
    summary = [["Metric", "Value"]] + [[k, v] for k, v in summary_pairs(report)]
    summary += [[], ["Journal rows", len(rows)], ["Journal net P&L", round(sum(r["netPnl"] for r in rows), 2)], ["Engine final balance - initial capital", round(report["finalBalance"] - report["initialCapital"], 2)]]
    rules = [["Item", "Value"], ["Strategy version", report.get("strategyVersion")], ["Specification hash", report["strategyHash"]], ["Status", (version or {}).get("status", "")],
             ["Approved by", (version or {}).get("approvedBy") or ""], ["Fidelity note", report.get("fidelityNote") or "No approved assumptions; every material rule is source-confirmed."]]
    rules += [[a, b] for a, b in describe_spec(report["specSnapshot"])]
    rules += [["Approved assumption: " + a.get("component", ""), a.get("text", "")] for a in report.get("approvedAssumptions", [])]
    cfg = report["configSnapshot"]
    config = [["Setting", "Value"], ["Backtest ID", report["id"]], ["Run date (UTC)", report["runDate"]], ["Engine", f"{report['engine']} {report['engineVersion']}"], ["Symbol", report["symbol"]],
              ["Timeframe", report["timeframe"]], ["First bar", report["dataRange"]["firstBar"]], ["Last bar", report["dataRange"]["lastBar"]], ["Initial capital", cfg["initial_capital"]],
              ["Spread", cfg["costs"]["spread"]], ["Slippage", cfg["costs"]["slippage"]], ["Commission", f"{cfg['costs']['commission_model']} {cfg['costs']['commission_value']}"],
              ["Same-bar policy", cfg["same_bar_policy"]], ["Dataset SHA-256", report["datasetSha256"]], ["Result hash", report["resultHash"]]] + [["Assumption", t] for t in report["assumptions"]]
    validation = [["Check", "Status", "Detail"]] + [[c["name"], c["status"], c.get("detail", "")] for c in report["validation"]["checks"]]
    validation += [["Run status", report["status"], ""], ["Data gaps", report["dataQuality"]["unexpectedGapCount"], report["dataGapDisclosure"]], ["Limitations", "", report["limitations"]]]
    return {"Trade Journal": journal, "Performance Summary": summary, "Strategy Rules": rules, "Backtest Configuration": config, "Validation & Exceptions": validation}


class SheetsClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self.token, self.client, self.own = token, client or httpx.AsyncClient(timeout=60), client is None

    async def close(self):
        if self.own:
            await self.client.aclose()

    async def call(self, method, url, **kw):
        response = await self.client.request(method, url, headers={"Authorization": f"Bearer {self.token}"}, **kw)
        if response.status_code == 404:
            raise SheetsError("Spreadsheet not found or not accessible with the connected Google account.")
        if response.status_code >= 400:
            raise SheetsError(f"Google Sheets error {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}

    async def create(self, title):
        body = {"properties": {"title": title}, "sheets": [{"properties": {"title": name}} for name in SHEETS]}
        data = await self.call("POST", API, json=body)
        return data["spreadsheetId"], {s["properties"]["title"]: s["properties"]["sheetId"] for s in data["sheets"]}

    async def meta(self, sid):
        data = await self.call("GET", f"{API}/{sid}", params={"fields": "sheets.properties"})
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in data.get("sheets", [])}

    async def ensure_sheets(self, sid, existing):
        missing = [name for name in SHEETS if name not in existing]
        if missing:
            data = await self.call("POST", f"{API}/{sid}:batchUpdate", json={"requests": [{"addSheet": {"properties": {"title": name}}} for name in missing]})
            for reply in data.get("replies", []):
                props = reply["addSheet"]["properties"]
                existing[props["title"]] = props["sheetId"]
        return existing

    async def read_ids(self, sid):
        data = await self.call("GET", f"{API}/{sid}/values/{quote('Trade Journal')}!A:A")
        return [row[0] if row else "" for row in data.get("values", [])]

    async def write(self, sid, ranges):
        await self.call("POST", f"{API}/{sid}/values:batchUpdate", json={"valueInputOption": "RAW", "data": [{"range": r, "values": _values(v)} for r, v in ranges]})

    async def clear(self, sid, names):
        await self.call("POST", f"{API}/{sid}/values:batchClear", json={"ranges": [f"{quote(n)}" for n in names]})

    async def format(self, sid, ids, journal_rows, columns):
        requests = []
        for name, sheet_id in ids.items():
            requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1}, "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                             "backgroundColor": {"red": 0.12, "green": 0.16, "blue": 0.23}}}, "fields": "userEnteredFormat(textFormat,backgroundColor)"}})
            requests.append({"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}})
        journal_id = ids["Trade Journal"]
        requests.append({"setBasicFilter": {"filter": {"range": {"sheetId": journal_id, "startRowIndex": 0, "endRowIndex": max(2, journal_rows), "startColumnIndex": 0, "endColumnIndex": columns}}}})
        requests.append({"autoResizeDimensions": {"dimensions": {"sheetId": journal_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": min(columns, 30)}}})
        await self.call("POST", f"{API}/{sid}:batchUpdate", json={"requests": requests})


def quote(name):
    return "'" + name.replace("'", "''") + "'"


def col_letter(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


async def export_run(report: dict, rows: list[dict], version: dict | None, *, sheet: str | None, title: str | None, client: httpx.AsyncClient | None = None, token: str | None = None) -> dict:
    """Create or update a spreadsheet. Journal rows are upserted by Trade ID; summary sheets are rewritten."""
    token = token or await access_token(client)
    api = SheetsClient(token, client)
    try:
        values = build_sheet_values(report, rows, version)
        if sheet:
            sid = spreadsheet_id(sheet)
            ids = await api.ensure_sheets(sid, await api.meta(sid))
            created = False
        else:
            sid, ids = await api.create(title or f"{report['strategyName']} - {report['symbol']} {report['timeframe']} journal")
            created = True
        existing = await api.read_ids(sid)
        position = {value: n + 1 for n, value in enumerate(existing) if value and n > 0}
        columns = len(JOURNAL_COLUMNS)
        end = col_letter(columns)
        updates, appended = [], 0
        if not existing:
            updates.append((f"{quote('Trade Journal')}!A1:{end}1", [values["Trade Journal"][0]]))
        next_row = max(len(existing), 1) + 1
        refreshed = 0
        for row in values["Trade Journal"][1:]:
            trade_id = row[0]
            if trade_id in position:
                n = position[trade_id]
                refreshed += 1
            else:
                n = next_row
                next_row += 1
                appended += 1
            updates.append((f"{quote('Trade Journal')}!A{n}:{end}{n}", [row]))
        await api.write(sid, updates)
        others = [n for n in SHEETS if n != "Trade Journal"]
        await api.clear(sid, others)
        await api.write(sid, [(f"{quote(n)}!A1", values[n]) for n in others])
        await api.format(sid, ids, next_row - 1, columns)
        return {"spreadsheetId": sid, "url": f"https://docs.google.com/spreadsheets/d/{sid}/edit", "created": created, "rowsAppended": appended, "rowsUpdated": refreshed, "totalTrades": len(rows)}
    finally:
        await api.close()
