# Architecture

## Data flow
YouTube source → metadata queue → worker download → FFmpeg audio → timestamped transcription → candidate events → selective evidence frames → multimodal observations → trade extraction → validation → embeddings → pattern aggregation → playbook/RAG.

## Evidence contract
Every extracted trade field has:
- nullable value
- AI confidence score
- zero or more evidence records
- evidence source (`transcript`, `visual`, `inference`)
- evidence level (`DIRECT`, `STRONG_INFERENCE`, `WEAK_INFERENCE`, `UNKNOWN`)

Null is preferred to fabrication. Spoken profit claims remain unverified unless visual/calculated support exists.

## Queues
`video_metadata`, `audio_extraction`, `transcription`, `candidate_detection`, `frame_extraction`, `visual_analysis`, `trade_extraction`, `trade_validation`, `strategy_aggregation`, `embedding`, `knowledge_synthesis`.

Workers use retries with exponential backoff and custom idempotent job IDs. Per-stage concurrency is environment-driven.

## Web/runtime boundary
Next.js route handlers validate input, rate-limit sensitive endpoints and enqueue work. They do not run 2-hour processing jobs. BullMQ workers execute external binaries and paid AI calls.

## Storage
Temporary source video/audio/frames live below `STORAGE_PATH/tmp/<videoId>`. Only selected evidence frames are intended for durable storage. `StorageProvider` currently includes a local implementation and keeps cloud implementations replaceable.

## Deployment
The UI/API is Vercel-compatible. The BullMQ worker is deployed from `Dockerfile.worker` to a long-running container host because persistent BullMQ consumers and FFmpeg jobs should not run inside normal request/response functions.
