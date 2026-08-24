# Troubleshooting

- Invalid YouTube URL: only youtube.com / youtu.be video and playlist forms are accepted.
- Private/deleted video: YouTube Data API returns no video item; the job fails visibly.
- FFmpeg/yt-dlp failure: inspect worker structured logs and retry the failed job.
- OpenAI 429/timeout: BullMQ retries with exponential backoff; lower AI concurrency if needed.
- Invalid AI JSON: Structured Outputs + Zod validation prevent unchecked payloads from entering trade records.
- Redis/PostgreSQL down: `/api/health/redis` and `/api/health/database` expose service state.
- Preview says demo fallback: attach corresponding service secrets and set `MOCK_AI_MODE=false` for real processing.
