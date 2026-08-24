# Forex Trader Video Intelligence

Personal evidence-first research platform for analyzing Forex education/live-trading videos. It is designed to reconstruct observable decision making from timestamped speech and chart visuals while keeping direct observations, spoken claims, inference, and unknown information distinct.

> Research/education only. No broker execution or autonomous trading.

## Current status
Phase 1 foundation is being established. The repository started empty. Current code includes the project/tooling manifest, strict TypeScript configuration, environment schema/template, evidence-first PostgreSQL schema, BullMQ queue names, core extraction schemas, centralized trade extraction prompt, YouTube URL utilities, lexical candidate detection, selective frame timestamp logic, dashboard shell, health endpoint, and initial unit tests.

External integrations have **not** yet been credential-tested and must not be treated as production-verified.

## Prerequisites
- Node.js 20+
- PostgreSQL with pgvector available for vector retrieval
- Redis
- FFmpeg (required from video-processing phase onward)

## Local setup
1. Copy `.env.example` to `.env` and fill required server-side values.
2. Install dependencies: `npm install`
3. Generate Prisma client: `npm run db:generate`
4. Create/update database schema during development: `npm run db:push`
5. Run the app: `npm run dev`
6. In later queue-enabled phases, run workers separately: `npm run worker`

## Quality commands
- `npm run typecheck`
- `npm run lint`
- `npm test`
- `npm run build`

## Architecture
See `docs/architecture.md` and `docs/implementation-plan.md`.

## Configuration
Model names, processing limits, frame offsets, concurrency, AI mode, storage provider, transcription provider, and event keywords are environment-driven. Secrets must never be exposed to browser code or committed to Git.

## Important limitations right now
Playlist discovery, media downloading, FFmpeg processing, real transcription, multimodal analysis, trade validation, embeddings/RAG, playbook synthesis, batch API integration, worker implementations, and full UI pages are subsequent phases and are not claimed as implemented yet.
