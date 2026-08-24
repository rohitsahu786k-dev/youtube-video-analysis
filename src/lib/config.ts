import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { z } from "zod";
function loadLocalEnv(){const file=path.resolve(process.cwd(),".env");if(!existsSync(file))return;for(const raw of readFileSync(file,"utf8").replace(/^\uFEFF/,"").split(/\r?\n/)){const line=raw.trim();if(!line||line.startsWith("#"))continue;const i=line.indexOf("=");if(i<1)continue;const key=line.slice(0,i).trim();let value=line.slice(i+1).trim();if((value.startsWith('"')&&value.endsWith('"'))||(value.startsWith("'")&&value.endsWith("'")))value=value.slice(1,-1);if(process.env[key]===undefined)process.env[key]=value;}}
loadLocalEnv();
const emptyToUndefined=(v:unknown)=>v===""?undefined:v;
const bool=z.preprocess(v=>emptyToUndefined(v)===undefined?undefined:v===true||v==="true",z.boolean());
const int=(n:number)=>z.preprocess(emptyToUndefined,z.coerce.number().int().positive().default(n).catch(n));
const text=(fallback:string)=>z.preprocess(emptyToUndefined,z.string().default(fallback).catch(fallback));
const optionalText=z.preprocess(emptyToUndefined,z.string().optional());
const processingMode=z.preprocess(emptyToUndefined,z.enum(["standard","batch"]).default("standard").catch("standard"));
const schema=z.object({
  MOCK_AI_MODE:bool.default(false),OPENAI_API_KEY:optionalText,OPENAI_ANALYSIS_MODEL:text("gpt-5.6-luna"),OPENAI_REASONING_MODEL:text("gpt-5.6-terra"),OPENAI_FINAL_MODEL:text("gpt-5.6-sol"),OPENAI_TRANSCRIBE_MODEL:text("gpt-transcribe"),OPENAI_EMBEDDING_MODEL:text("text-embedding-3-small"),YOUTUBE_API_KEY:optionalText,DATABASE_URL:optionalText,REDIS_URL:optionalText,CLOUDINARY_URL:optionalText,YTDLP_COOKIES_PATH:optionalText,STORAGE_PATH:text("./storage"),AI_PROCESSING_MODE:processingMode,VIDEO_PROCESS_CONCURRENCY:int(2),AI_ANALYSIS_CONCURRENCY:int(5),FRAME_ANALYSIS_CONCURRENCY:int(3),FRAME_EVENT_BEFORE_SECONDS:int(15),FRAME_EVENT_AFTER_SECONDS:int(30),FRAME_INTERVAL_SECONDS:int(5),MAX_FRAMES_PER_EVENT:int(8),MAX_CANDIDATE_EVENTS_PER_VIDEO:int(100),OPENAI_COST_TRACKING:bool.default(true),OPENAI_PRICING_JSON:optionalText,LOG_LEVEL:text("info")
});
export const parseEnv=(input:Record<string,string|undefined>)=>schema.parse(input);
export const env=parseEnv(process.env);
export const pricing:Record<string,{input:number;output:number;cached?:number}>={"gpt-5.6-sol":{input:4,output:20,cached:.4},"gpt-5.6-terra":{input:2,output:12,cached:.2},"gpt-5.6-luna":{input:.2,output:1.2,cached:.02},"text-embedding-3-small":{input:.02,output:0},...(env.OPENAI_PRICING_JSON?JSON.parse(env.OPENAI_PRICING_JSON):{})};
export function cost(model:string,input:number,output:number,cached=0){const p=pricing[model];if(!p)return 0;return ((input-cached)/1e6)*p.input+(cached/1e6)*(p.cached??p.input)+(output/1e6)*p.output;}
