# Deployment (VPS) — and why not Vercel

**Use a VPS (Docker + Caddy).** Vercel runs short-lived serverless functions with a read-only filesystem; this app needs a
long-running background worker (video download, FFmpeg, multi-minute AI analysis, backtests), local disk (SQLite, evidence frames,
market data) and Node/FFmpeg binaries. None of that fits Vercel. A 2 vCPU / 4 GB / 40 GB VPS is enough to start.

## 1. Server
```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
git clone <your repo> /opt/tvi && cd /opt/tvi
cp .env.example .env && nano .env
```
Required in `.env`: `GEMINI_API_KEY`, `SECRET_KEY` (32+ random chars), `ADMIN_PASSWORD` (10+ chars), `APP_URL=https://your.domain`,
`APP_ENV=production`. The app refuses to start in production without an admin password and a strong secret.

## 2. Run
```bash
export DOMAIN=your.domain        # DNS A record must point at the server first
docker compose up -d --build
docker compose logs -f web
```
Caddy terminates HTTPS automatically. Sign in at `https://your.domain` with `ADMIN_USERNAME` / `ADMIN_PASSWORD`; create more users
under *Integrations & Settings*. Each user's data is isolated.

Run exactly **one** `web` container (the worker and SQLite are single-process by design).

## 3. Optional integrations
* **OANDA** history: `OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID`, `OANDA_ENVIRONMENT`.
* **Google Sheets**: create a Web OAuth client, redirect URI `https://your.domain/api/google/callback`, set `GOOGLE_CLIENT_ID/SECRET`.
* **Cookies** for videos you are authorized to access: `YTDLP_COOKIES_PATH` (never commit it).

## 4. Backups & restore
`deploy/backup.sh` (cron daily) snapshots SQLite safely (`sqlite3 .backup`) and archives datasets + evidence; *Integrations → Download backup*
gives an on-demand ZIP. Restore: stop the stack, put `intelligence.sqlite3` and `datasets/` back into `storage/`, start.

## 5. Updates / monitoring
`git pull && docker compose up -d --build`. Health: `GET /api/health`. Logs: `docker compose logs web`. Disk: *Integrations → Operations*.
Interrupted videos and jobs resume automatically after a restart.

## 6. Security checklist
HTTPS only (Caddy) · strong `SECRET_KEY`/`ADMIN_PASSWORD` · signed HttpOnly SameSite cookies · login lockout · origin check on all writes ·
credentials encrypted at rest · firewall allowing only 22/80/443 · keep the OS patched. YouTube/Google/OANDA/TradingView terms and creator
rights remain your responsibility.

## 7. Cost, abuse and storage protection (built in)
* **AI budget** - every Gemini call is recorded with its token cost (*Integrations -> AI usage*). `AI_DAILY_BUDGET_USD` (default 3) pauses *new* video analysis and makes chat
  answer with free source passages once reached; `AI_DAILY_QUESTIONS` (default 80) caps model-written chat answers. Both reset at midnight `DISPLAY_TZ`. `0` disables a cap.
  Prices come from the built-in table; override with `GEMINI_PRICING_JSON` (`{"model": {"input": usd_per_1M, "output": usd_per_1M}}`).
* **Chat model** - `GEMINI_CHAT_MODEL` (default `gemini-3.1-flash-lite`) is used for query rewriting and answers; set it to `gemini-3.8-flash` for maximum answer quality.
  Answers are always validated against the retrieved evidence; repeated questions over unchanged evidence are served from memory at no cost.
* **Rate limits** - per user/IP in the app (login 10/min, chat 12/min, uploads 12/min, heavy jobs 20/min, everything else 300/min) and per IP in nginx (20 req/s, 1 upload/s).
* **Disk and inodes** - uploads and new downloads are refused (HTTP 507) and new analysis pauses when free disk < `MIN_FREE_DISK_MB` (3072) or free inodes < `MIN_FREE_INODE_PCT` (8).
  The media library is capped by `MEDIA_QUOTA_MB` (3000) per user and 1000 files; uploads are type-checked (extension and leading bytes), streamed to disk and de-duplicated.
* **Housekeeping** - every 6 h: stale temp/download files (> 24 h) and finished jobs older than `JOB_RETENTION_DAYS` are removed and the SQLite WAL is checkpointed.
  `deploy/backup.sh` verifies each backup with `PRAGMA integrity_check`. journald is capped at 300 MB; the service has memory and task limits.
