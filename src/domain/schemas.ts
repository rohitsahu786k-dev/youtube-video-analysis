import { z } from "zod";

export const evidenceLevelSchema = z.enum(["DIRECT", "STRONG_INFERENCE", "WEAK_INFERENCE", "UNKNOWN"]);
export const sourceTypeSchema = z.enum(["TRANSCRIPT", "VISUAL", "AI_INFERENCE", "CALCULATED"]);
export const candidateEventTypeSchema = z.enum([
  "TRADE_ENTRY", "TRADE_EXIT", "STOP_LOSS", "TAKE_PROFIT", "PARTIAL_EXIT", "BREAKEVEN",
  "SETUP_FORMATION", "MARKET_ANALYSIS", "BIAS_CHANGE", "TRADE_MANAGEMENT",
]);

export const evidenceSchema = z.object({
  sourceType: sourceTypeSchema,
  evidenceLevel: evidenceLevelSchema,
  videoId: z.string(),
  timestampSeconds: z.number().nonnegative(),
  sourceReference: z.string().nullable().default(null),
  text: z.string().nullable().default(null),
  description: z.string().nullable().default(null),
});

const supportedField = <T extends z.ZodType>(valueSchema: T) => z.object({
  value: valueSchema.nullable(),
  confidence: z.number().min(0).max(1),
  evidence: z.array(evidenceSchema),
});

export const extractedTradeSchema = z.object({
  wasTradeTaken: supportedField(z.boolean()),
  symbol: supportedField(z.string()),
  direction: supportedField(z.enum(["LONG", "SHORT"])),
  entryPrice: supportedField(z.number()),
  stopLoss: supportedField(z.number()),
  takeProfit: supportedField(z.number()),
  exitPrice: supportedField(z.number()),
  entryTimestamp: supportedField(z.number().nonnegative()),
  exitTimestamp: supportedField(z.number().nonnegative()),
  setup: supportedField(z.string()),
  entryReason: supportedField(z.string()),
  exitReason: supportedField(z.string()),
  timeframes: supportedField(z.array(z.string())),
  indicators: supportedField(z.array(z.string())),
  marketConditions: supportedField(z.array(z.string())),
  tradeManagement: supportedField(z.array(z.string())),
  outcome: supportedField(z.enum(["WIN", "LOSS", "BREAKEVEN", "OPEN", "UNKNOWN"])),
  spokenClaims: z.array(z.object({ claim: z.string(), evidence: z.array(evidenceSchema) })),
  visualObservations: z.array(z.object({ observation: z.string(), evidence: z.array(evidenceSchema) })),
  inferences: z.array(z.object({ inference: z.string(), evidence: z.array(evidenceSchema) })),
  profitVerificationStatus: z.enum(["UNVERIFIED", "VISUALLY_SUPPORTED", "CALCULATED", "UNKNOWN"]),
});

export const tradeValidationSchema = z.object({
  status: z.enum(["valid", "invalid", "uncertain"]),
  reasons: z.array(z.string()),
  unsupportedFields: z.array(z.string()),
  confidence: z.number().min(0).max(1),
});

export type ExtractedTrade = z.infer<typeof extractedTradeSchema>;
