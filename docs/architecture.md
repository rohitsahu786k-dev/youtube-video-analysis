# Architecture

## System shape
A modular monolith is used for the personal-research MVP: one Next.js application for UI/API plus one or more independently started BullMQ worker processes. PostgreSQL is the source of truth, Redis is queue infrastructure, and selected evidence media is stored behind a storage-provider interface.

```text
Browser
  -> Next.js UI
  -> Server APIs
       -> PostgreSQL / pgvector
       -> BullMQ -> Redis
                    -> Workers
                       -> YouTube metadata/media acquisition
                       -> FFmpeg
                       -> TranscriptionProvider
                       -> CandidateDetector
                       -> FrameExtractor
                       -> AI analysis services
                       -> Evidence/Trade repositories
                       -> EmbeddingService
                       -> Pattern/Playbook synthesis
```

## Boundaries

### Web process
Responsible for validation, persistence-oriented commands, read APIs, dashboards, filters, search/chat orchestration, and queue control. It must not perform long video processing in request handlers.

### Worker process
Consumes stage-specific queues. Each stage receives stable entity IDs rather than large binary payloads. Jobs use deterministic job IDs where possible so retries do not duplicate work.

### PostgreSQL
Stores metadata, timestamped transcripts, events, selected frame metadata, visual observations, trades, evidence, patterns, knowledge chunks, analysis runs, and AI usage.

### Redis/BullMQ
Queues:
- `video_metadata`
- `audio_extraction`
- `transcription`
- `candidate_detection`
- `frame_extraction`
- `visual_analysis`
- `trade_extraction`
- `trade_validation`
- `strategy_aggregation`
- `embedding`
- `knowledge_synthesis`

## Evidence model
Facts are never flattened into one opaque AI summary. Evidence records retain:
- source type: transcript, visual, calculated, or inference
- evidence level: DIRECT, STRONG_INFERENCE, WEAK_INFERENCE, UNKNOWN
- video and timestamp
- source record reference
- excerpt/description
- AI confidence score where applicable

Spoken claims and visual observations remain separate. A synthesized inference may reference both but cannot silently upgrade either into verified fact.

## AI architecture
AI calls go through services, not UI components or route handlers directly. Prompt templates live under `src/ai/prompts` and have explicit versions. Model names come from environment configuration.

Pipeline model roles:
- analysis model: high-volume candidate/visual/extraction work
- reasoning model: validation and difficult synthesis
- final model: playbook/RAG synthesis where justified
- transcription model: audio transcription
- embedding model: semantic retrieval

Structured AI output is validated with Zod. Invalid output is repaired/retried through a constrained path and raw output is retained for debugging where appropriate.

## Media strategy
Full source media and dense frame sequences are temporary. The processing workspace is cleaned after downstream persistence. Only thumbnails and selected evidence frames are retained long term.

## Cost controls
- transcript-first candidate narrowing
- configurable event limits
- sparse event-centered frames
- duplicate/perceptual frame filtering
- configurable image resolution/detail
- bounded worker concurrency
- standard vs offline batch processing abstraction
- AI usage ledger by model, operation, and video

## Security
Secrets remain server-only. YouTube URLs are parsed against known hosts/ID formats. Shell execution uses argument arrays rather than interpolated commands. User-supplied shell fragments are never executed. API input is validated. Production deployment should place rate limiting in front of mutation/chat endpoints.

## Scaling path
The modular monolith intentionally avoids premature microservices. Worker processes can be scaled independently from the web process. Storage can move from local disk to S3-compatible storage without changing domain logic. PostgreSQL/pgvector remains suitable for the intended personal research workload.
