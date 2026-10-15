# Trader Video Intelligence

Source-grounded trading strategy research: **YouTube source → speech + chart analysis → evidence-cited strategy manuals → grounded Q&A →
approved executable versions → causal, lookahead-audited backtests → trade-level journal, analytics, Excel / Google Sheets / Pine exports.**
Built to the "AI Trading Strategy Intelligence Platform" specification (see `docs/srs-compliance.md` for a requirement-by-requirement status).

## Run locally
Python 3.11+, FFmpeg and Node.js 22+ (YouTube format access) are required for video analysis.
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env        # set GEMINI_API_KEY, keep MOCK_AI_MODE=false
.\.venv\Scripts\python.exe run.py  # http://localhost:8000
```
Local use needs no login. **Before hosting publicly** set `ADMIN_PASSWORD` and `SECRET_KEY` — see `docs/deployment.md` (VPS + HTTPS; Vercel is not suitable).

## Workspace
| Section | What it does |
|---|---|
| YouTube Source Manager | Video / playlist / whole channel; discovery, eligibility classification, explicit states (discovered, queued, analyzing, completed, failed, unavailable, awaiting permission, skipped), honest coverage statement, sync without duplicates, authorized transcript/media upload |
| Video Analysis Library | Time-aligned transcript, captured chart frames (sha-256), per-video extracted rules, trades |
| Strategy Library | Strategies keyed by the teacher's terminology, ranked; full A–Z manual where **every item cites verified quotes + timestamps**; conflicts shown, never resolved silently; missing rules reported |
| AI Trading Q&A | Multi-turn, strategy-scoped, cross-video; source answer vs separately labelled general note; "Show Source Evidence" opens the video at the moment |
| Strategy Editor & Approval | JSON rules with validation, declared assumptions, approve → freeze versioning, change log, readiness (fully / partially / not reliably backtestable) |
| Backtesting Workspace | Data import (CSV, OANDA, Binance), costs, sessions, same-bar policy, optimisation + walk-forward (separately labelled variants) |
| Trade Journal / Analytics | Every trade a row with ~50 fields, plain-language explanation from event records, SVG chart evidence, 30+ metrics, segmentation, charts |
| Strategy Comparison | Side by side runs, equity overlay, session/instrument breakdown |
| Integrations & Settings | Google Sheets OAuth, TradingView workflow, users, backup |

## Accuracy design (what "accurate" means here)
* **No invented rules.** The model must quote the transcript verbatim; the server verifies the quote exists (exact/fuzzy). Unverified rules never enter a manual; uncited manual items are dropped.
* **Causal engine.** Decisions at bar *i* use bars ≤ *i*; swings/structure are known only after confirmation; higher-timeframe values only after that bar closes. Every run proves it: series rebuilt on truncated data, sampled trades re-simulated on truncated data, a guarded replay that raises on any future read, and a reproducibility re-run.
* **Conservative fills.** Next-bar execution, bid/ask spread, slippage, gap-aware stops, stop-first when a stop and target share a candle (or lower-timeframe resolution), limit orders must trade through.
* **Independent validation** blocks publication (`INVALID`): P&L, risk, quantities, costs, sessions, chronology and balance reconciliation are recomputed.
* **Honest labelling.** Backtests with approved assumptions are marked *not a faithful instructor-method test*; later-taught rules are *retrospective*.

Limits that remain: AI readings of video can be wrong (hence evidence review), candle data cannot reveal intrabar order, results never predict the future, and TradingView cannot be remote-controlled (Pine is generated for you to test; a trade-list import compares the two engines).

## Tests & QA
```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q          # engine, lookahead, benchmarks, API, security, UI parse
.\.venv\Scripts\python.exe tools/qa_browser.py   # real-Chrome end-to-end run (screenshots in dist/qa)
python tools/build.py                            # deployable ZIP
```
Acceptance tests 5–9 of the specification (lookahead, ambiguous SL/TP, numerical benchmark, journal reconciliation, reproducibility) are automated in `tests/test_engine_core.py`; test 10 (TradingView consistency) in `tests/test_engine_pine.py`.

## Configuration
See `.env.example`. Key settings: `GEMINI_API_KEY`, `SECRET_KEY`, `ADMIN_PASSWORD`, `OANDA_*`, `GOOGLE_CLIENT_*`, `MAX_CHANNEL_VIDEOS`, `INCLUDE_SHORTS`, `DISPLAY_TZ`, `BACKTEST_PROCESSES`.
Storage: `storage/intelligence.sqlite3` (+ `datasets/`, `evidence/`) — back up all three. Strategy rule format: the in-app editor guide and `GET /api/spec/templates`; API docs at `/api/docs` outside production.
