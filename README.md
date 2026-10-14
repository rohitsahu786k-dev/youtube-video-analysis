# Trader Video Intelligence

A local FastAPI research platform that turns YouTube trading videos and playlists into timestamped transcripts, captured chart evidence, a searchable knowledge base, recurring setup patterns, explicit strategy drafts, and causal OHLC backtests. TradingView charts display market context; a separate read-only OANDA feed evaluates paper setups.

## Run

Python 3.10+, FFmpeg and Node.js 22+ (or Deno 2.3+) are required for full YouTube format access. Python 3.12 is recommended. The yt-dlp default dependencies include its JavaScript challenge solver; supported runtimes are detected automatically.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Configure GEMINI_API_KEY in .env; keep MOCK_AI_MODE=false.
.\.venv\Scripts\python.exe run.py
```

Open http://localhost:8000. Run one web process: its restart-safe local worker handles the configured video concurrency. Automatic reload is disabled so editing a file does not interrupt analysis. Docker binds to 0.0.0.0; the ordinary launcher binds to localhost. HOST and PORT override the binding.

## Workflow

1. Add a YouTube playlist or video. Playlist discovery expands individual videos, persists membership, avoids duplicate video records, and queues each video independently. Failed discovery or analysis can be retried from Processing.
2. Videos download at up to 1080p. Ten-minute audio chunks produce timestamped transcripts. A full visual/audio scan at one frame per second also detects silent events. Each event gets a before/after frame window; retained images have timestamps and SHA-256 checksums.
3. Gemini extracts multiple trades and rechecks their original frames. Execution needs a visible actual position/fill, not merely a position drawing. Unreadable or unsupported prices stay null, unknown outcomes stay unknown, and R:R is calculated only from consistent supported prices. AI validation is not human verification. Execution modes distinguish live brokers, paper accounts, historical replay and unknown environments; replay wins are not live profits.
4. Knowledge stores spoken evidence, visual readings, teaching rules, trade evidence and timestamped source links. Chat retrieves relevant stored chunks; it never answers from seeded demo records. Patterns distinguish observed executions from taught concepts; unknown outcomes do not contribute to a win-rate denominator. A recurring pattern requires at least two source videos.
5. Recurring patterns automatically produce rule drafts. Unsupported conditions and missing parameters stay explicit. Review a mechanical interpretation in Backtesting before saving it. Supported engines currently cover a prior-range sweep/rejection and a range breakout, with a signal-candle stop, fixed reward/risk target and maximum holding period. Multi-timeframe IFVG, order-block, session and other discretionary filters are not reproduced by these engines.
6. Import historical mid-price OHLC CSV, or fetch up to 5,000 completed OANDA candles. Timezone-aware timestamps, ordering, timeframe and OHLC bounds are validated. CSV columns are `time,open,high,low,close`; example timestamp: `2026-10-01T10:00:00Z`.
7. Backtests use fixed reviewed rules, a chronological 70/30 split, next-bar execution, one position at a time, spread and slippage in absolute price units, stop-first ambiguous bars, and gap-aware stop fills. Holdout candles must be later than publication of every source video. Unfinished end-of-data trades are excluded from closed-trade metrics. Reports include expectancy, net R, drawdown, ambiguity and sample sizes.
8. The Charts page checks eligible rules against fresh OANDA quotes and completed candles. It shows WAIT for missing evidence, insufficient holdout results, stale data, changed source evidence, provider mismatch, wide spreads, expired setups or no matching trigger. Qualifying setups are paper research suggestions with entry quote, stop and target. No broker orders are submitted.

## Configuration

- `GEMINI_API_KEY` is required. An OpenAI key cannot authenticate to Gemini. Models are configured separately for analysis, transcription, reasoning and answers; `/api/health/gemini` checks model access. This metadata check does not establish generation quota.
- `FFMPEG_PATH` optionally selects the FFmpeg executable. Windows WinGet installations are detected automatically.
- `STORAGE_PATH` defaults to `./storage`. SQLite at `storage/intelligence.sqlite3` persists videos, playlists, evidence, jobs, strategies and reports. Back up this database and `storage/evidence` together. The old MongoDB URL is not used by this local implementation.
- `YTDLP_COOKIES_PATH` optionally points to a local YouTube cookie file for videos you are authorized to access. Do not commit it.
- `MAX_PLAYLIST_VIDEOS` defaults to 500; oversized playlists fail explicitly rather than silently truncating.
- `MAX_CANDIDATE_EVENTS_PER_VIDEO` caps detailed event analysis. Coverage warnings identify partial analysis. One-frame-per-second discovery can miss brief actions; blurred or edited footage cannot establish every historical fill.
- `VIDEO_PROCESS_CONCURRENCY` defaults to 2. Gemini quota and video duration affect processing time and cost. Usage records report provider token counts; billing comes from Google AI Studio, not invented cost estimates.
- `OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID` and `OANDA_ENVIRONMENT=practice|live` enable read-only candle/quote requests. Credentials stay server-side. Historical imports work while the market is closed; live suggestions require tradeable current quotes. TradingView does not provide calculation data through its widget, and feed prices may differ.
- `MIN_HOLDOUT_TRADES` defaults to 30. Live eligibility also requires positive independent holdout expectancy, reviewed rules and a dataset fetched directly from OANDA. CSV provider labels cannot bypass this check. Backtest data older than 30 days is rejected for live analysis.
- `DEFAULT_CHART_SYMBOL` defaults to `FX:EURUSD`. TradingView credentials are not required or stored. Sign in on TradingView itself to use personal layouts. Chart toolbar symbol changes do not change the external analysis panel; use this platform's symbol/timeframe controls to align them.
- `MOCK_AI_MODE=true` disables real processing. Production pages start empty; demo results are never mixed into research. `ENABLE_WORKER=false` supports read-only browser QA; ordinary operation should keep the worker enabled.

## Validation

Build a deployable source ZIP with `python tools/build.py`. The artifact is written to `dist/trader-video-intelligence.zip`; it includes the application, dependency list, documentation and Docker configuration, while excluding credentials, databases, captured media and development environments. Install its dependencies before running. GitHub CI tests the application and uploads this ZIP.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Tests isolate storage and external inference. They cover durable ingestion, idempotent playlist expansion, multi-event trade updates, supported Gemini schemas, truthful empty states, price/risk validation, causal bar replay, transaction rollback, source changes and live abstention. Browser QA additionally verifies desktop/mobile pages and the actual TradingView iframe. Real video inference is a separate integration check; passing tests does not demonstrate predictive accuracy.

## Limits

This is a research tool, not an audited trading account or a guarantee of profitable predictions. AI readings can be wrong. Video examples can be cherry-picked, edited, hypothetical or simulated. Backtesting an explicit interpretation is not the same as recreating every discretionary decision by an expert. OHLC data cannot reveal intrabar order or historical liquidity, and repeated testing can overfit. Keep suggestions in paper trading while comparing captured evidence and forward results. Public deployment requires an authentication/access-control layer; the default launcher is local.
