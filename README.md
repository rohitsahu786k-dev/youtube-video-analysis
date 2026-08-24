# Forex Trader Video Intelligence

Personal research platform for reconstructing the **observable** decision-making process in Forex education/live-trading videos. It combines timestamped speech evidence, selected chart-frame evidence and structured AI extraction without automatic trade execution.

## Features
- YouTube single-video and playlist ingestion
- BullMQ/Redis asynchronous pipeline
- secure yt-dlp + FFmpeg processing worker
- OpenAI timestamped transcription provider
- keyword + semantic candidate-event detection
- selective compressed evidence-frame extraction and duplicate suppression
- multimodal visual chart analysis
- structured trade extraction with nullable unknown fields
- second-pass trade validation
- spoken vs visual vs inference evidence separation
- strategy-pattern aggregation and contradiction surfaces
- trader playbook
- pgvector-ready knowledge chunks / embeddings
- RAG research chat with YouTube timestamp links
- cost estimation and operation breakdown
- retry/pause/resume/cancel/re-run controls
- deterministic `MOCK_AI_MODE`
- CI, worker Dockerfile and health endpoints

## Prerequisites
Node.js 22+, PostgreSQL with pgvector, Redis, FFmpeg and yt-dlp. Demo preview only needs Node.

## Setup
```bash
cp .env.example .env
npm install
npm run dev
```
Open `http://localhost:3000`.

For real processing set the required keys/URLs in `.env`, set `MOCK_AI_MODE=false`, initialize PostgreSQL/pgvector, and run:
```bash
npm run db:push
npm run worker
npm run dev
```

A development stack for PostgreSQL, Redis and the worker is available with `docker compose up --build`.

## Environment
See `.env.example`. Model IDs and processing limits are configurable. Defaults use `gpt-5.6-luna` for high-volume analysis, `gpt-5.6-terra` for reasoning/validation, `gpt-5.6-sol` for final synthesis, `gpt-transcribe` for transcription and `text-embedding-3-small` for embeddings.

## Verification
```bash
npm run typecheck
npm test
npm run lint
npm run build
```
GitHub Actions runs the same verification sequence.

## Architecture
See `docs/architecture.md`, `docs/ai-pipeline.md`, `docs/video-processing.md`, `docs/database.md`, `docs/cost-optimization.md`, `docs/deployment.md` and `docs/troubleshooting.md`.

## Research limitation
Statistics represent only observable/identified trades in analyzed videos. They are not audited trading-history statistics, and extracted patterns do not guarantee future profitability.
