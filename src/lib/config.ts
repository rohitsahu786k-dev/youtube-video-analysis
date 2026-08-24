import { z } from "zod";
const bool=z.preprocess(v=>v===undefined?undefined:v===true||v==="true",z.boolean());
const int=(n:number)=>z.coerce.number().int().positive().default(n);
const schema=z.object({
  MOCK_AI_MODE:bool.default(true),OPENAI_API_KEY:z.string().optional(),OPENAI_ANALYSIS_MODEL:z.string().default("gpt-5.6-luna"),OPENAI_REASONING_MODEL:z.string().default("gpt-5.6-terra"),OPENAI_FINAL_MODEL:z.string().default("gpt-5.6-sol"),OPENAI_TRANSCRIBE_MODEL:z.string().default("gpt-transcribe"),OPENAI_EMBEDDING_MODEL:z.string().default("text-embedding-3-small"),YOUTUBE_API_KEY:z.string().optional(),DATABASE_URL:z.string().optional(),REDIS_URL:z.string().optional(),CLOUDINARY_URL:z.string().optional(),STORAGE_PATH:z.string().default("./storage"),AI_PROCESSING_MODE:z.enum(["standard","batch"]).default("standard"),VIDEO_PROCESS_CONCURRENCY:int(2),AI_ANALYSIS_CONCURRENCY:int(5),FRAME_ANALYSIS_CONCURRENCY:int(3),FRAME_EVENT_BEFORE_SECONDS:int(15),FRAME_EVENT_AFTER_SECONDS:int(30),FRAME_INTERVAL_SECONDS:int(5),MAX_FRAMES_PER_EVENT:int(8),MAX_CANDIDATE_EVENTS_PER_VIDEO:int(100),OPENAI_COST_TRACKING:bool.default(true),OPENAI_PRICING_JSON:z.string().optional(),LOG_LEVEL:z.string().default("info")
});
export const env=schema.parse(process.env);
export const pricing:Record<string,{input:number;output:number;cached?:number}>={"gpt-5.6-sol":{input:4,output:20,cached:.4},"gpt-5.6-terra":{input:2,output:12,cached:.2},"gpt-5.6-luna":{input:.2,output:1.2,cached:.02},"text-embedding-3-small":{input:.02,output:0},...(env.OPENAI_PRICING_JSON?JSON.parse(env.OPENAI_PRICING_JSON):{})};
export function cost(model:string,input:number,output:number,cached=0){const p=pricing[model];if(!p)return 0;return ((input-cached)/1e6)*p.input+(cached/1e6)*(p.cached??p.input)+(output/1e6)*p.output;}
