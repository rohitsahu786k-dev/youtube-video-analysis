import { z } from "zod";

const boolString = z.enum(["true", "false"]).transform((value) => value === "true");
const positiveInt = (fallback: number) => z.coerce.number().int().positive().default(fallback);

const schema = z.object({
  APP_ENV: z.enum(["development", "test", "production"]).default("development"),
  NEXT_PUBLIC_APP_URL: z.string().url().default("http://localhost:3000"),
  OPENAI_API_KEY: z.string().optional(),
  OPENAI_ANALYSIS_MODEL: z.string().default("gpt-5.6-luna"),
  OPENAI_REASONING_MODEL: z.string().default("gpt-5.6-terra"),
  OPENAI_FINAL_MODEL: z.string().default("gpt-5.6-sol"),
  OPENAI_TRANSCRIBE_MODEL: z.string().optional(),
  OPENAI_EMBEDDING_MODEL: z.string().optional(),
  YOUTUBE_API_KEY: z.string().optional(),
  DATABASE_URL: z.string().min(1),
  REDIS_URL: z.string().min(1),
  STORAGE_PROVIDER: z.enum(["local", "s3", "supabase"]).default("local"),
  STORAGE_PATH: z.string().default("./storage"),
  TRANSCRIPTION_PROVIDER: z.enum(["openai", "local"]).default("openai"),
  AI_PROCESSING_MODE: z.enum(["standard", "batch"]).default("standard"),
  MOCK_AI_MODE: boolString.default(false),
  VIDEO_PROCESS_CONCURRENCY: positiveInt(2),
  AI_ANALYSIS_CONCURRENCY: positiveInt(5),
  FRAME_ANALYSIS_CONCURRENCY: positiveInt(3),
  FRAME_EVENT_BEFORE_SECONDS: positiveInt(15),
  FRAME_EVENT_AFTER_SECONDS: positiveInt(30),
  FRAME_INTERVAL_SECONDS: positiveInt(5),
  FRAME_OFFSETS_SECONDS: z.string().default("-15,-10,-5,0,5,10,20,30"),
  MAX_FRAMES_PER_EVENT: positiveInt(8),
  MAX_CANDIDATE_EVENTS_PER_VIDEO: positiveInt(100),
  TRADING_EVENT_KEYWORDS: z.string().default("entry,entered,buy,sell,long,short,stop loss,take profit,target,exit,breakeven,liquidity,sweep,breakout,retest,rejection,confirmation,setup,signal,trade,position,order"),
  OPENAI_COST_TRACKING: boolString.default(true),
  LOG_LEVEL: z.string().default("info"),
});

export type AppEnv = z.infer<typeof schema>;

let cached: AppEnv | undefined;
export function getEnv(): AppEnv {
  cached ??= schema.parse(process.env);
  return cached;
}

export function frameOffsets(): number[] {
  return getEnv().FRAME_OFFSETS_SECONDS.split(",")
    .map((value) => Number(value.trim()))
    .filter(Number.isFinite);
}

export function tradingKeywords(): string[] {
  return getEnv().TRADING_EVENT_KEYWORDS.split(",")
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean);
}
