# Cost Optimization
The system avoids blind per-second frame sampling. Candidate count, event window, frame count and concurrency are environment-driven. Model prices are centralized in `src/lib/config.ts` and can be overridden using `OPENAI_PRICING_JSON`. Cost shown in the UI is estimated, not billing truth.
