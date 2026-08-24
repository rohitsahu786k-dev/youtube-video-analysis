# Implementation Plan

## Goal
Build a personal, evidence-first Forex trader video intelligence platform. The system analyzes YouTube trading education/live-session videos and reconstructs observable decision making without inventing unavailable trade facts.

## Repository assessment
- Repository was empty at project start.
- No existing framework, package configuration, environment file, database configuration, components, or README existed.
- We can therefore establish a clean modular architecture without migration constraints.

## Engineering principles
1. Evidence before inference.
2. `null`/unknown rather than hallucination.
3. Spoken claims, visual observations, and AI inference remain distinct.
4. Long-running video work always runs in BullMQ workers.
5. AI models, limits, storage, transcription, embeddings, and pricing are configuration-driven.
6. Every pipeline stage is idempotent and retryable.
7. Temporary media is deleted; evidence media is retained.
8. AI outputs are schema-validated before persistence.
9. Bulk processing is bounded by configurable concurrency.
10. Mock mode supports deterministic development without AI spend.

## Delivery phases

### Phase 1 — Foundation
- Next.js + TypeScript strict mode
- Tailwind CSS
- Environment validation
- PostgreSQL/Prisma schema
- pgvector-ready knowledge schema
- Redis + BullMQ queue registry
- Structured logging
- Dark research-dashboard shell
- Health endpoints
- Core domain schemas and enums

### Phase 2 — YouTube ingestion
- URL parser/validator
- Playlist and video metadata provider
- Playlist pagination
- Add playlist/video APIs
- Persist discovered videos
- Queue metadata jobs

### Phase 3 — Video processing
- `VideoProcessor` interface/service
- Safe FFmpeg process wrapper
- audio extraction
- frame extraction
- optional scene detection hooks
- temporary workspace lifecycle
- storage abstraction

### Phase 4 — Transcription
- `TranscriptionProvider`
- OpenAI provider
- timestamped segments
- language/confidence where returned
- local-provider interface reserved

### Phase 5 — Candidate detection
- configurable lexical signals
- semantic event detection
- event deduplication
- candidate caps and confidence

### Phase 6 — Selective frames
- configurable event windows
- frame timestamp generation
- perceptual hash/deduplication
- evidence frame retention

### Phase 7 — Multimodal analysis
- centralized versioned prompt
- structured visual schema
- chart symbol/timeframe/price/structure observations
- explicit unknown handling

### Phase 8 — Trade extraction and validation
- evidence-first structured trade schema
- initial extraction pass
- independent validation pass
- valid/invalid/uncertain status
- profit verification status

### Phase 9 — Retrieval/indexing
- knowledge chunking
- configurable embeddings
- pgvector search
- filters by trader/video/trade metadata

### Phase 10 — Strategy aggregation
- recurring observed patterns
- minimum evidence thresholds
- descriptive statistics over identified trades only

### Phase 11 — Trader Playbook
- evidence-linked synthesis
- contradictions and unclear rules
- source coverage statistics

### Phase 12 — RAG chat
- retrieval + evidence assembly
- grounded answer schema
- timestamped YouTube source links
- explicit limitations

### Phase 13 — Cost dashboard
- AI usage ledger
- model/operation/video breakdown
- updateable pricing config
- cost/video and cost/trade

### Phase 14 — Tests and optimization
- unit/integration coverage from specification
- mock fixtures
- queue failure/retry tests
- lint/typecheck/build/test gates
- documentation hardening

## Initial MVP acceptance path
`playlist -> videos -> queue -> audio -> transcript -> candidates -> frames -> visual analysis -> trade extraction -> validation -> indexing -> patterns/playbook/chat`

## Testing rule
A feature is documented as implemented only after its code path exists. A feature is documented as tested only after the relevant command/test has actually executed successfully. External integrations requiring credentials will be explicitly marked unverified until exercised with real credentials.
