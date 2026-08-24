# Database
Prisma models traders, playlists, videos, processing jobs, timestamped transcript segments, video events, frames, frame analyses, trades, trade events, strategy patterns, knowledge chunks, AI usage and contradictions. PostgreSQL with pgvector is the production target. Enable the `vector` extension before creating a vector index for `KnowledgeChunk.embedding`.
