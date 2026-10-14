# Troubleshooting

Check /api/health for configured services and /api/health/gemini for actual model access. Missing Gemini keys, disabled real processing, missing FFmpeg or YouTube access failures are explicit errors; they do not create fabricated successful analyses. Processing shows persisted discovery and video failures with retry buttons.

If downloads require sign-in, set YTDLP_COOKIES_PATH to a local cookie export for authorized access and update yt-dlp with its default dependencies. Install Node.js 22+ or Deno 2.3+ so YouTube challenge solving can expose high-resolution formats. Keep cookie files private. For missing FFmpeg, install it or set FFMPEG_PATH to the executable. Gemini quotas can cause retries and long analysis times; reduce VIDEO_PROCESS_CONCURRENCY.

No trades can be the correct result for a teaching video or chart replay without visible actual fills. Inspect transcript, detected events and frame evidence. Unknown outcomes stay unknown. Capped event coverage is reported on the video page. An unsupported strategy draft needs review and cannot magically become an exact implementation of discretionary rules.

Live WAIT messages explain missing reviewed strategies, insufficient independent holdout results, changed evidence, missing OANDA settings, stale quotes, closed markets or invalid setups. CSV backtests cannot activate live suggestions. Never enter TradingView passwords into the app; personal layouts are opened directly on TradingView.

Run one worker-enabled server. Automatic code reload is disabled during video processing. SQLITE data persists across restarts; record identifiers prevent duplicate playlist members.
