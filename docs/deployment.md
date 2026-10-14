# Running locally or in Docker

Install Python requirements, FFmpeg and Node.js 22+ (or Deno 2.3+), configure GEMINI_API_KEY in .env, then run python run.py. The launcher uses the project directory, binds to localhost by default and disables automatic reload during analysis. HOST and PORT can override binding. Open /api/health and /api/health/gemini to inspect readiness.

Docker includes FFmpeg and Node.js 22, excludes local secrets/media/development environments, binds to 0.0.0.0 and persists storage through docker-compose.yml. Configure .env on the host; do not bake it into the image. Run one web process because the local durable queue assumes one active worker. ENABLE_WORKER=false supports a separate read-only QA instance.

Back up storage/intelligence.sqlite3 together with storage/evidence. SQLite holds videos, playlists, ingestion state, knowledge, rule proposals, datasets and reports; source images are separate files. Source downloads/audio are temporary.

This launcher is intended for personal local use. Public deployment still needs authentication, TLS and an access-control boundary; no multi-user isolation or distributed worker queue is implemented. Market feed requests are read-only. No live order API exists.
