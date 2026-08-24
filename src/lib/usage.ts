import type { Prisma } from "@prisma/client";
import { prisma } from "./db";
import { env } from "./config";

export const PROMPT_VERSION = "2026-08-24.v1";

type UsageInput = {
  videoId?: string | null;
  operation: string;
  model: string;
  inputTokens?: number;
  outputTokens?: number;
  cachedTokens?: number;
  estimatedCost?: number;
};

export async function recordAiUsage(input: UsageInput) {
  if (!env.OPENAI_COST_TRACKING || !env.DATABASE_URL) return;
  try {
    await prisma.aiUsage.create({
      data: {
        videoId: input.videoId ?? null,
        operation: input.operation,
        model: input.model,
        inputTokens: input.inputTokens ?? 0,
        outputTokens: input.outputTokens ?? 0,
        cachedTokens: input.cachedTokens ?? 0,
        estimatedCost: input.estimatedCost ?? 0,
      },
    });
  } catch (error) {
    console.warn("AI usage persistence failed", error);
  }
}

export async function startAnalysisRun(input: {
  videoId?: string | null;
  operation: string;
  model?: string | null;
}) {
  if (!env.DATABASE_URL) return null;
  try {
    return await prisma.analysisRun.create({
      data: {
        videoId: input.videoId ?? null,
        operation: input.operation,
        promptVersion: PROMPT_VERSION,
        model: input.model ?? null,
        status: "RUNNING",
      },
    });
  } catch (error) {
    console.warn("Analysis run persistence failed", error);
    return null;
  }
}

export async function finishAnalysisRun(
  id: string | null | undefined,
  status: "COMPLETED" | "FAILED",
  rawOutput?: Prisma.InputJsonValue,
  error?: string,
) {
  if (!id || !env.DATABASE_URL) return;
  try {
    await prisma.analysisRun.update({
      where: { id },
      data: {
        status,
        completedAt: new Date(),
        rawOutput,
        error,
      },
    });
  } catch (persistError) {
    console.warn("Analysis run finalization failed", persistError);
  }
}
