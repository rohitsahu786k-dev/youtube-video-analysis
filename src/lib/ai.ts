import OpenAI from "openai";
import fs from "node:fs/promises";
import { env, cost } from "./config";
import { prompts } from "@/ai/prompts";
import {
  tradeSchema,
  type TradeExtraction,
  type TranscriptSegment,
  type CandidateEvent,
} from "./schemas";
import { detectEvents } from "./events";

const client = new OpenAI({ apiKey: env.OPENAI_API_KEY || "mock" });
type ResponseRequestInput = string | OpenAI.Responses.ResponseInput;
type JsonSchema = Record<string, unknown>;

async function json<T>(
  model: string,
  instructions: string,
  input: ResponseRequestInput,
  name: string,
  schema: JsonSchema,
) {
  if (!env.OPENAI_API_KEY) throw new Error("OPENAI_API_KEY required");
  const response = await client.responses.create({
    model,
    instructions,
    input,
    text: { format: { type: "json_schema", name, strict: true, schema } },
  });
  const inputTokens = response.usage?.input_tokens ?? 0;
  const outputTokens = response.usage?.output_tokens ?? 0;
  const cachedTokens = response.usage?.input_tokens_details?.cached_tokens ?? 0;
  return {
    data: JSON.parse(response.output_text) as T,
    usage: {
      input: inputTokens,
      output: outputTokens,
      cached: cachedTokens,
      cost: cost(model, inputTokens, outputTokens, cachedTokens),
    },
    id: response.id,
  };
}

export async function semanticEvents(segments: TranscriptSegment[]): Promise<CandidateEvent[]> {
  if (env.MOCK_AI_MODE || !env.OPENAI_API_KEY) return [];
  const schema: JsonSchema = {
    type: "object",
    additionalProperties: false,
    required: ["events"],
    properties: {
      events: {
        type: "array",
        items: {
          type: "object",
          additionalProperties: false,
          required: ["timestamp", "eventType", "transcriptEvidence", "confidence", "detectionSource"],
          properties: {
            timestamp: { type: "number" },
            eventType: {
              type: "string",
              enum: [
                "TRADE_ENTRY",
                "TRADE_EXIT",
                "STOP_LOSS",
                "TAKE_PROFIT",
                "PARTIAL_EXIT",
                "BREAKEVEN",
                "SETUP_FORMATION",
                "MARKET_ANALYSIS",
                "BIAS_CHANGE",
                "TRADE_MANAGEMENT",
              ],
            },
            transcriptEvidence: { type: "string" },
            confidence: { type: "number" },
            detectionSource: { type: "string", enum: ["semantic"] },
          },
        },
      },
    },
  };
  return (
    await json<{ events: CandidateEvent[] }>(
      env.OPENAI_ANALYSIS_MODEL,
      prompts.candidate,
      JSON.stringify(segments),
      "candidate_events",
      schema,
    )
  ).data.events;
}

export async function transcribe(audioPath: string) {
  if (!env.OPENAI_API_KEY) throw new Error("OPENAI_API_KEY required");
  const response = await client.audio.transcriptions.create({
    file: (await import("node:fs")).createReadStream(audioPath),
    model: env.OPENAI_TRANSCRIBE_MODEL,
    response_format: "verbose_json",
    timestamp_granularities: ["segment"],
  });
  const result = response as unknown as {
    text?: string;
    language?: string;
    duration?: number;
    segments?: Array<{ start?: number; end?: number; text?: string }>;
  };
  const directSegments = (result.segments ?? []).filter((segment) => segment.text?.trim());
  const fallbackSegments = directSegments.length
    ? directSegments
    : chunkTranscript(result.text ?? "", result.duration);
  if (!fallbackSegments.length) throw new Error("Transcription returned no text or timestamp segments");
  return {
    text: result.text ?? "",
    language: result.language ?? null,
    segments: fallbackSegments.map((segment) => ({
      start: segment.start ?? 0,
      end: segment.end ?? segment.start ?? 0,
      text: segment.text ?? "",
      language: result.language ?? null,
      confidence: null,
    })),
  };
}

function chunkTranscript(text: string, duration?: number) {
  const words = text.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const chunkSize = 80;
  const chunks: Array<{ start: number; end: number; text: string }> = [];
  const estimatedDuration = duration && duration > 0 ? duration : Math.max(30, Math.ceil(words.length / 150) * 60);
  const secondsPerWord = estimatedDuration / words.length;
  for (let i = 0; i < words.length; i += chunkSize) {
    const part = words.slice(i, i + chunkSize);
    chunks.push({
      start: Math.round(i * secondsPerWord * 10) / 10,
      end: Math.round(Math.min(words.length, i + part.length) * secondsPerWord * 10) / 10,
      text: part.join(" "),
    });
  }
  return chunks;
}

export async function analyzeFrames(paths: string[], context: string) {
  if (env.MOCK_AI_MODE || !env.OPENAI_API_KEY) {
    return {
      symbol: "EURUSD",
      timeframe: "5M",
      visiblePrice: 1.16842,
      chartElements: ["prior session high", "bearish rejection"],
      indicators: [],
      drawings: ["liquidity line", "short position tool"],
      visualDescription: "Price sweeps the marked prior high and rejects below it.",
      confidence: 0.92,
      evidenceLevel: "DIRECT",
    };
  }

  const content: OpenAI.Responses.ResponseInputContent[] = [
    { type: "input_text", text: `Context: ${context}` },
  ];
  for (const path of paths) {
    const bytes = await fs.readFile(path);
    content.push({
      type: "input_image",
      image_url: `data:image/webp;base64,${bytes.toString("base64")}`,
      detail: "low",
    });
  }

  const schema: JsonSchema = {
    type: "object",
    additionalProperties: false,
    required: [
      "symbol",
      "timeframe",
      "visiblePrice",
      "chartElements",
      "indicators",
      "drawings",
      "visualDescription",
      "confidence",
      "evidenceLevel",
    ],
    properties: {
      symbol: { type: ["string", "null"] },
      timeframe: { type: ["string", "null"] },
      visiblePrice: { type: ["number", "null"] },
      chartElements: { type: "array", items: { type: "string" } },
      indicators: { type: "array", items: { type: "string" } },
      drawings: { type: "array", items: { type: "string" } },
      visualDescription: { type: "string" },
      confidence: { type: "number" },
      evidenceLevel: {
        type: "string",
        enum: ["DIRECT", "STRONG_INFERENCE", "WEAK_INFERENCE", "UNKNOWN"],
      },
    },
  };

  return (
    await json<Record<string, unknown>>(
      env.OPENAI_ANALYSIS_MODEL,
      prompts.visual,
      [{ role: "user", content }],
      "visual_analysis",
      schema,
    )
  ).data;
}

function evidenceSchema(): JsonSchema {
  return {
    type: "object",
    additionalProperties: false,
    required: ["type", "videoId", "timestamp", "text", "frameId", "description", "level", "confidence"],
    properties: {
      type: { type: "string", enum: ["transcript", "visual", "inference"] },
      videoId: { type: "string" },
      timestamp: { type: "number" },
      text: { type: ["string", "null"] },
      frameId: { type: ["string", "null"] },
      description: { type: "string" },
      level: {
        type: "string",
        enum: ["DIRECT", "STRONG_INFERENCE", "WEAK_INFERENCE", "UNKNOWN"],
      },
      confidence: { type: "number" },
    },
  };
}

function fieldSchema(valueSchema: JsonSchema): JsonSchema {
  return {
    type: "object",
    additionalProperties: false,
    required: ["value", "confidence", "evidence"],
    properties: {
      value: valueSchema,
      confidence: { type: "number" },
      evidence: { type: "array", items: evidenceSchema() },
    },
  };
}

const tradeExtractionJsonSchema: JsonSchema = {
  type: "object",
  additionalProperties: false,
  required: [
    "actuallyTaken",
    "symbol",
    "direction",
    "entryPrice",
    "stopLoss",
    "takeProfit",
    "exitPrice",
    "entryTimestamp",
    "exitTimestamp",
    "setup",
    "entryReason",
    "exitReason",
    "timeframe",
    "indicators",
    "marketCondition",
    "tradeManagement",
    "outcome",
    "spokenProfitClaim",
    "profitVerificationStatus",
    "overallConfidence",
  ],
  properties: {
    actuallyTaken: fieldSchema({ type: ["boolean", "null"] }),
    symbol: fieldSchema({ type: ["string", "null"] }),
    direction: fieldSchema({ type: ["string", "null"], enum: ["LONG", "SHORT", null] }),
    entryPrice: fieldSchema({ type: ["number", "null"] }),
    stopLoss: fieldSchema({ type: ["number", "null"] }),
    takeProfit: fieldSchema({ type: ["number", "null"] }),
    exitPrice: fieldSchema({ type: ["number", "null"] }),
    entryTimestamp: fieldSchema({ type: ["number", "null"] }),
    exitTimestamp: fieldSchema({ type: ["number", "null"] }),
    setup: fieldSchema({ type: ["string", "null"] }),
    entryReason: fieldSchema({ type: ["string", "null"] }),
    exitReason: fieldSchema({ type: ["string", "null"] }),
    timeframe: fieldSchema({ type: ["string", "null"] }),
    indicators: fieldSchema({ anyOf: [{ type: "array", items: { type: "string" } }, { type: "null" }] }),
    marketCondition: fieldSchema({ type: ["string", "null"] }),
    tradeManagement: fieldSchema({ anyOf: [{ type: "array", items: { type: "string" } }, { type: "null" }] }),
    outcome: fieldSchema({ type: ["string", "null"] }),
    spokenProfitClaim: fieldSchema({ type: ["string", "null"] }),
    profitVerificationStatus: {
      type: "string",
      enum: ["UNVERIFIED", "VISUALLY_SUPPORTED", "CALCULATED", "UNKNOWN"],
    },
    overallConfidence: { type: "number" },
  },
};

function f<T>(value: T | null, confidence = 0.9, evidence: unknown[] = []) {
  return { value, confidence, evidence };
}

export async function extractTrade(
  videoId: string,
  transcriptEvidence: unknown,
  visualEvidence: unknown,
): Promise<TradeExtraction> {
  if (env.MOCK_AI_MODE || !env.OPENAI_API_KEY) {
    return tradeSchema.parse({
      actuallyTaken: f(true, 0.98, [
        {
          type: "transcript",
          videoId,
          timestamp: 754.4,
          text: "I'm entering short here after that rejection.",
          frameId: null,
          description: "Explicit entry",
          level: "DIRECT",
          confidence: 0.99,
        },
      ]),
      symbol: f("EURUSD", 0.94, []),
      direction: f("SHORT", 0.99, []),
      entryPrice: f(1.16842, 0.89, []),
      stopLoss: f(1.16918, 0.86, []),
      takeProfit: f(1.16688, 0.84, []),
      exitPrice: f(1.16692, 0.81, []),
      entryTimestamp: f(754.4, 0.99, []),
      exitTimestamp: f(1298, 0.88, []),
      setup: f("Liquidity Sweep + Rejection", 0.91, []),
      entryReason: f("Prior high swept, bearish rejection, then short executed.", 0.91, []),
      exitReason: f("Target region reached", 0.82, []),
      timeframe: f("5M / 15M", 0.86, []),
      indicators: f([], 0.9, []),
      marketCondition: f("London reversal after buy-side sweep", 0.84, []),
      tradeManagement: f(["Move stop to breakeven after displacement"], 0.93, []),
      outcome: f("WIN", 0.88, []),
      spokenProfitClaim: f(null, 1, []),
      profitVerificationStatus: "CALCULATED",
      overallConfidence: 0.92,
    });
  }

  const result = await json<TradeExtraction>(
    env.OPENAI_REASONING_MODEL,
    prompts.trade,
    JSON.stringify({ videoId, transcriptEvidence, visualEvidence }),
    "trade_extraction",
    tradeExtractionJsonSchema,
  );
  return tradeSchema.parse(result.data);
}

export type TradeValidation = {
  status: "VALID" | "INVALID" | "UNCERTAIN";
  reason: string;
  confidence: number;
};

export async function validateTrade(
  extractedTrade: TradeExtraction,
  transcriptEvidence: unknown,
  visualEvidence: unknown,
): Promise<TradeValidation> {
  if (env.MOCK_AI_MODE || !env.OPENAI_API_KEY) {
    return {
      status: extractedTrade.actuallyTaken.value ? "VALID" : "INVALID",
      reason: extractedTrade.actuallyTaken.value
        ? "Mock evidence contains an explicit entry statement and matching visual position evidence."
        : "No supported trade execution was observed.",
      confidence: extractedTrade.overallConfidence,
    };
  }

  const schema: JsonSchema = {
    type: "object",
    additionalProperties: false,
    required: ["status", "reason", "confidence"],
    properties: {
      status: { type: "string", enum: ["VALID", "INVALID", "UNCERTAIN"] },
      reason: { type: "string" },
      confidence: { type: "number" },
    },
  };
  return (
    await json<TradeValidation>(
      env.OPENAI_REASONING_MODEL,
      prompts.validation,
      JSON.stringify({ extractedTrade, transcriptEvidence, visualEvidence }),
      "trade_validation",
      schema,
    )
  ).data;
}

export async function answer(question: string) {
  const normalized = question.toLowerCase();
  if (env.MOCK_AI_MODE || !env.OPENAI_API_KEY) {
    if (normalized.includes("short")) {
      return "The strongest observed short model is a buy-side liquidity sweep followed by rejection and a lower-timeframe structure shift or continuation trigger.";
    }
    if (normalized.includes("breakeven") || normalized.includes("break even")) {
      return "Observed videos suggest the trader moves stop to breakeven after clear displacement rather than immediately after entry.";
    }
    return "The strongest matching evidence is separated into spoken claims, visual observations and AI inference. Unknown values remain unknown.";
  }
  return (
    await client.responses.create({
      model: env.OPENAI_FINAL_MODEL,
      instructions: prompts.rag,
      input: question,
    })
  ).output_text;
}

export async function detectAllEvents(segments: TranscriptSegment[]) {
  const local = detectEvents(segments, env.MAX_CANDIDATE_EVENTS_PER_VIDEO);
  const semantic = await semanticEvents(segments);
  const merged = new Map<string, CandidateEvent>();
  for (const event of [...local, ...semantic]) {
    const key = `${event.eventType}-${Math.floor(event.timestamp / 3)}`;
    if (!merged.has(key) || merged.get(key)!.confidence < event.confidence) merged.set(key, event);
  }
  return [...merged.values()]
    .sort((a, b) => a.timestamp - b.timestamp)
    .slice(0, env.MAX_CANDIDATE_EVENTS_PER_VIDEO);
}
