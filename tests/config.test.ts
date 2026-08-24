import { describe,expect,it } from "vitest";
import { parseEnv } from "@/lib/config";

describe("config",()=>{
  it("uses defaults for blank Vercel environment values",()=>{
    const env=parseEnv({
      AI_PROCESSING_MODE:"",
      VIDEO_PROCESS_CONCURRENCY:"",
      AI_ANALYSIS_CONCURRENCY:"",
      FRAME_ANALYSIS_CONCURRENCY:"",
      FRAME_EVENT_BEFORE_SECONDS:"",
      FRAME_EVENT_AFTER_SECONDS:"",
      FRAME_INTERVAL_SECONDS:"",
      MAX_FRAMES_PER_EVENT:"",
      MAX_CANDIDATE_EVENTS_PER_VIDEO:""
    });
    expect(env.AI_PROCESSING_MODE).toBe("standard");
    expect(env.VIDEO_PROCESS_CONCURRENCY).toBe(2);
    expect(env.AI_ANALYSIS_CONCURRENCY).toBe(5);
    expect(env.MAX_FRAMES_PER_EVENT).toBe(8);
  });
});
