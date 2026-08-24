# Deployment
Deploy the Next.js UI/API to Vercel with `MOCK_AI_MODE=true` for a credential-free preview. For real processing configure OpenAI/YouTube/MongoDB/Redis and deploy `Dockerfile.worker` to a long-running container environment. Do not run the persistent BullMQ consumer inside a normal Vercel request function.
