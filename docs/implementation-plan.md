# Implementation Plan

## Goal
Build a personal, evidence-first Forex trader video intelligence platform that turns YouTube trading videos into timestamped transcript evidence, selected chart-frame evidence, structured observed trades, recurring patterns, a playbook, and evidence-cited research answers.

## Architecture decisions
- Next.js App Router + TypeScript strict for UI and server APIs.
- PostgreSQL + Prisma; pgvector is the semantic-index target.
- Redis + BullMQ for long-running worker stages; web requests only enqueue work.
- Dedicated worker image includes FFmpeg and yt-dlp. Commands use argument arrays with `shell: false`.
- OpenAI model tiers are centralized in environment variables. High-volume tasks use Luna, extraction/validation uses Terra, final synthesis uses Sol by default.
- OpenAI Responses API is used for reasoning/vision/structured outputs. Transcription is provider-based.
- Demo mode is first-class and deterministic, so UI/UX and extraction contracts can be tested without API spend.
- Evidence types stay separated: transcript claim, visual observation, AI inference.

## Completed phases
1. Foundation: Next.js, strict TS, Tailwind/shadcn-style primitives, env validation, PostgreSQL schema, Redis/BullMQ interfaces.
2. YouTube: URL parsing, playlist pagination, metadata and duration parsing.
3. Video: secure yt-dlp/FFmpeg download/audio/frame pipeline, compression, hashing, cleanup.
4. Transcription: OpenAI provider abstraction and timestamped segments.
5. Candidate detection: local semantic-ish rules + OpenAI semantic pass.
6. Selective frames: configurable event windows/max count and duplicate suppression.
7. Multimodal visual analysis with Structured Outputs.
8. Trade extraction + skeptical validation pass.
9. Knowledge/embedding services and pgvector-compatible schema.
10. Pattern aggregation and contradiction-ready data model.
11. Trader playbook UI and evidence statistics.
12. RAG chat with source timestamp links.
13. Cost configuration/dashboard.
14. Tests, CI, Docker worker, docs and deployment configuration.

## Runtime modes
### Demo / preview
`MOCK_AI_MODE=true`. No paid API calls. Dashboard, ingestion acknowledgement, controls and RAG use deterministic evidence fixtures.

### Real analysis
Requires OpenAI, YouTube Data API, PostgreSQL/pgvector and Redis. Web app and worker share the database/Redis. The worker host must provide FFmpeg + yt-dlp and persistent/ephemeral processing disk.

## Non-goals
No broker execution, order placement, SaaS billing, multi-tenant organizations or autonomous trading.
