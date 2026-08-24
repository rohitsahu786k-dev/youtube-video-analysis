# Troubleshooting
Use `/api/health`, `/api/health/database`, `/api/health/redis` and `/api/health/openai`. Invalid/private/deleted YouTube items fail visibly. BullMQ retries transient AI/media errors with exponential backoff. Lower AI concurrency for 429s. Structured extraction preserves unavailable fields as null.
