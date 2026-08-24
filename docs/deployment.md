# Deployment
Deploy the Next.js UI/API to Vercel from the GitHub repository root. `vercel.json` pins the framework, `npm install`, and `npm run build`.

Set production environment variables in the Vercel dashboard: `DATABASE_URL`, `OPENAI_API_KEY`, `YOUTUBE_API_KEY`, `CLOUDINARY_URL`, `MOCK_AI_MODE`, and optionally `REDIS_URL`.

Use `MOCK_AI_MODE=true` for a credential-light hosted demo. For real processing configure OpenAI/YouTube/MongoDB/Redis and deploy `Dockerfile.worker` to a long-running container environment. Do not run the persistent BullMQ consumer inside a normal Vercel request function.
