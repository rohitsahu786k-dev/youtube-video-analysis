# Runtime architecture

FastAPI serves Jinja pages and JSON APIs. One lifespan-managed worker expands persisted YouTube ingestion jobs and processes a bounded number of videos concurrently. SQLite document collections provide durable state and atomic per-video replacement of trades/knowledge. Restarted jobs recover to QUEUED; failures remain visible until explicit retry.

Pipeline: metadata -> download -> chunked transcription -> visual/audio event discovery -> timestamped original frames -> multi-trade extraction -> skeptical image validation -> knowledge/lessons -> recurring patterns -> rule drafts. Source frames remain under storage/evidence after temporary media cleanup.

The same deterministic setup evaluator drives OHLC replay and live paper analysis. Backtests use chronological, post-evidence holdouts. Live checks require reviewed unchanged source evidence, an eligible matching-provider report, current completed candles and fresh tradeable quotes. TradingView renders chart context independently; no widget data is scraped and no order endpoint exists.

SQLite and the worker are designed for a single local web process. Use ENABLE_WORKER=false for a separate read-only QA server. Multi-host queues, public authentication, tick replay and arbitrary discretionary strategy translation are not implemented.
