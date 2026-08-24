# Deployment

## Web app
Deploy the repository to Vercel. Demo preview requires no paid service credentials and should set `MOCK_AI_MODE=true`.

## Real mode
Configure `OPENAI_API_KEY`, `YOUTUBE_API_KEY`, `DATABASE_URL`, `REDIS_URL`, set `MOCK_AI_MODE=false`, run PostgreSQL with pgvector, and deploy `Dockerfile.worker` to a long-running container environment with access to the same PostgreSQL/Redis.

The Vercel web app should not run the BullMQ worker process inside request functions.
