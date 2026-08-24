import crypto from "node:crypto";
import OpenAI from "openai";
import { prisma } from "./db";
import { cost, env } from "./config";
import { recordAiUsage } from "./usage";

export type KnowledgeHit = {
  id: string;
  title: string | null;
  content: string;
  sourceType: string;
  sourceId: string | null;
  metadata: unknown;
  distance: number;
  youtubeVideoId: string | null;
  videoTitle: string | null;
};

function openai() {
  if (!env.OPENAI_API_KEY) throw new Error("OPENAI_API_KEY required");
  return new OpenAI({ apiKey: env.OPENAI_API_KEY });
}

export async function embedText(text: string, operation = "embedding", videoId?: string | null) {
  if (env.MOCK_AI_MODE || !env.OPENAI_API_KEY) return [] as number[];
  const response = await openai().embeddings.create({
    model: env.OPENAI_EMBEDDING_MODEL,
    input: text,
    encoding_format: "float",
  });
  const vector = response.data[0]?.embedding ?? [];
  const inputTokens = response.usage?.prompt_tokens ?? response.usage?.total_tokens ?? 0;
  await recordAiUsage({
    videoId,
    operation,
    model: env.OPENAI_EMBEDDING_MODEL,
    inputTokens,
    outputTokens: 0,
    estimatedCost: cost(env.OPENAI_EMBEDDING_MODEL, inputTokens, 0),
  });
  return vector;
}

export async function indexKnowledgeChunk(chunkId: string, content: string, videoId?: string | null) {
  const vector = await embedText(content, "embedding", videoId);
  if (!vector.length || !env.DATABASE_URL) return false;
  const vectorText = `[${vector.join(",")}]`;
  const id = crypto.randomUUID();
  await prisma.$executeRawUnsafe(`DELETE FROM "Embedding" WHERE "knowledgeChunkId" = $1`, chunkId);
  await prisma.$executeRawUnsafe(
    `INSERT INTO "Embedding" ("id","knowledgeChunkId","model","dimensions","vector","createdAt") VALUES ($1,$2,$3,$4,$5::vector,NOW())`,
    id,
    chunkId,
    env.OPENAI_EMBEDDING_MODEL,
    vector.length,
    vectorText,
  );
  return true;
}

export async function semanticSearch(query: string, limit = 8): Promise<KnowledgeHit[]> {
  if (env.MOCK_AI_MODE || !env.OPENAI_API_KEY || !env.DATABASE_URL) return [];
  const vector = await embedText(query, "rag_query_embedding");
  if (!vector.length) return [];
  const vectorText = `[${vector.join(",")}]`;
  const safeLimit = Math.max(1, Math.min(25, Math.trunc(limit)));
  return prisma.$queryRawUnsafe<KnowledgeHit[]>(
    `SELECT k."id", k."title", k."content", k."sourceType", k."sourceId", k."metadata",
      (e."vector" <=> $1::vector)::float8 AS "distance",
      v."youtubeVideoId", v."title" AS "videoTitle"
     FROM "Embedding" e
     JOIN "KnowledgeChunk" k ON k."id" = e."knowledgeChunkId"
     LEFT JOIN "Video" v ON v."id" = k."videoId"
     ORDER BY e."vector" <=> $1::vector
     LIMIT $2`,
    vectorText,
    safeLimit,
  );
}
