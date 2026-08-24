# Forex Trader Video Intelligence

Personal evidence-first platform for analyzing Forex education/live-trading videos. It reconstructs observable decision-making from timestamped speech and selected chart frames while keeping spoken claims, visual observations, AI inference and unknown values separate.

## Included
YouTube video/playlist ingestion; BullMQ/Redis background pipeline; FFmpeg + yt-dlp media processing; OpenAI transcription, semantic candidate detection and multimodal analysis; structured trade extraction; validation stage; evidence records; recurring patterns; contradiction surfaces; observed trader playbook; MongoDB-backed knowledge chunks; timestamp-linked RAG chat; AI cost configuration; health endpoints; processing controls; deterministic demo mode; tests/CI; worker Docker image.

## Preview
`MOCK_AI_MODE=true` runs the complete UI without paid external services. Real processing requires OpenAI, YouTube Data API, MongoDB Atlas, Redis and a long-running worker with FFmpeg + yt-dlp.

## Vercel Hosting
Import this GitHub repository in Vercel and keep the root directory as `/`. Vercel should use `npm install` and `npm run build` from `vercel.json`.

Set required secrets in Vercel Project Settings, not in committed files: `DATABASE_URL`, `OPENAI_API_KEY`, `YOUTUBE_API_KEY`, `CLOUDINARY_URL`, and optionally `REDIS_URL`.

For a web-only hosted preview, set `MOCK_AI_MODE=true`. For real processing, keep the Next.js web/API on Vercel and run the BullMQ worker from `Dockerfile.worker` on a long-running host with FFmpeg and yt-dlp.

## Commands
```bash
cp .env.example .env
npm install
npm run dev
npm run worker       # real processing worker
npm run typecheck
npm test
npm run lint
npm run build
```

## Research limitation
Statistics are from observable/identified trades in analyzed videos only. They are not audited trading history and do not guarantee future profitability. The application never places broker orders.
