# Database

PostgreSQL is modeled through Prisma. Core relationships are:
Trader → Playlist/Video → Transcript Segment/Event/Frame → Trade → Trade Event → Pattern/Evidence.

`KnowledgeChunk.embedding` is a pgvector-compatible `vector(1536)` unsupported Prisma field. Create the `vector` extension before vector-index migrations. For production semantic search, create an HNSW or IVFFlat index appropriate to the embedding dimension/model and workload.

Important models: `Video`, `VideoProcessingJob`, `TranscriptSegment`, `VideoEvent`, `Frame`, `FrameAnalysis`, `Trade`, `TradeEvent`, `StrategyPattern`, `KnowledgeChunk`, `AnalysisRun`, `AiUsage`, `Contradiction`.
