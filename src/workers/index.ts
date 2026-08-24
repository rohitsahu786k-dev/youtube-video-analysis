import { Worker, type Job } from "bullmq";
import IORedis from "ioredis";
import type { ProcessingStatus } from "@prisma/client";
import { env } from "@/lib/config";
import { prisma } from "@/lib/db";
import { videoMeta } from "@/lib/youtube";
import { yt, frameTimes } from "@/lib/utils";
import { MediaProcessor } from "@/lib/media";
import { transcribe, detectAllEvents, analyzeFrames, extractTrade, validateTrade } from "@/lib/ai";
import { enqueue } from "@/lib/queue";
import { tradeSchema } from "@/lib/schemas";
import { indexKnowledgeChunk } from "@/lib/knowledge";
import { aggregateStrategyPatterns, detectContradictions, synthesizePlaybooks } from "@/lib/strategy";
import { assertRunnable } from "@/lib/processing";
import { storageProvider } from "@/lib/storage";

if (!env.REDIS_URL || !env.DATABASE_URL) throw new Error("Worker requires REDIS_URL and DATABASE_URL");

const connection = new IORedis(env.REDIS_URL, { maxRetriesPerRequest: null });
const media = new MediaProcessor();
const storage = storageProvider();
type D = { videoId: string; youtubeVideoId: string; sourcePath?: string; audioPath?: string };
const workers: Worker[] = [];

async function status(id: string, value: ProcessingStatus) {
  await prisma.video.update({ where: { id }, data: { processingStatus: value } });
}

function add(name: string, concurrency: number, fn: (job: Job<D>) => Promise<void>) {
  workers.push(new Worker<D>(name, async (job) => {
    if (job.data.videoId !== "manual" && !(await assertRunnable(job.data.videoId))) return;
    await fn(job);
  }, { connection, concurrency }));
}

function riskReward(direction: string | null, entry: number | null, stop: number | null, target: number | null) {
  if (!direction || entry === null || stop === null || target === null) return null;
  const risk = direction === "LONG" ? entry - stop : stop - entry;
  const reward = direction === "LONG" ? target - entry : entry - target;
  return risk > 0 && reward >= 0 ? reward / risk : null;
}

add("video_metadata", env.VIDEO_PROCESS_CONCURRENCY, async (job) => {
  const metadata = await videoMeta(job.data.youtubeVideoId);
  await prisma.video.update({
    where: { id: job.data.videoId },
    data: {
      title: metadata.title,
      description: metadata.description,
      channelName: metadata.channel,
      publishedAt: metadata.publishedAt ? new Date(metadata.publishedAt) : null,
      durationSeconds: metadata.duration,
      thumbnailUrl: metadata.thumbnail,
      processingStatus: "DOWNLOADING",
    },
  });
  await enqueue("audio_extraction", job.data, `${job.data.videoId}-audio`);
});

add("audio_extraction", env.VIDEO_PROCESS_CONCURRENCY, async (job) => {
  await status(job.data.videoId, "DOWNLOADING");
  const sourcePath = await media.download(yt(job.data.youtubeVideoId), job.data.videoId);
  await status(job.data.videoId, "EXTRACTING_AUDIO");
  const audioPath = await media.audio(sourcePath, job.data.videoId);
  await enqueue("transcription", { ...job.data, sourcePath, audioPath }, `${job.data.videoId}-transcribe`);
});

add("transcription", env.AI_ANALYSIS_CONCURRENCY, async (job) => {
  if (!job.data.audioPath) throw new Error("audio missing");
  await status(job.data.videoId, "TRANSCRIBING");
  const result = await transcribe(job.data.audioPath);
  await prisma.transcriptSegment.deleteMany({ where: { videoId: job.data.videoId } });
  for (const segment of result.segments) {
    await prisma.transcriptSegment.create({
      data: {
        videoId: job.data.videoId,
        startSeconds: segment.start,
        endSeconds: segment.end,
        text: segment.text,
        language: segment.language,
        confidence: segment.confidence,
      },
    });
  }
  await enqueue("candidate_detection", job.data, `${job.data.videoId}-events`);
});

add("candidate_detection", env.AI_ANALYSIS_CONCURRENCY, async (job) => {
  await status(job.data.videoId, "DETECTING_EVENTS");
  const rows = await prisma.transcriptSegment.findMany({ where: { videoId: job.data.videoId }, orderBy: { startSeconds: "asc" } });
  const events = await detectAllEvents(rows.map((segment) => ({
    start: segment.startSeconds,
    end: segment.endSeconds,
    text: segment.text,
    language: segment.language,
    confidence: segment.confidence,
  })));
  await prisma.videoEvent.deleteMany({ where: { videoId: job.data.videoId } });
  for (const event of events) {
    await prisma.videoEvent.create({
      data: {
        videoId: job.data.videoId,
        timestampSeconds: event.timestamp,
        eventType: event.eventType,
        confidence: event.confidence,
        source: "TRANSCRIPT",
        evidenceLevel: event.confidence > 0.84 ? "DIRECT" : "STRONG_INFERENCE",
        description: event.transcriptEvidence,
        transcriptText: event.transcriptEvidence,
      },
    });
  }
  await enqueue("frame_extraction", job.data, `${job.data.videoId}-frames`);
});

add("frame_extraction", env.VIDEO_PROCESS_CONCURRENCY, async (job) => {
  if (!job.data.sourcePath) throw new Error("source missing");
  await status(job.data.videoId, "EXTRACTING_FRAMES");
  const video = await prisma.video.findUniqueOrThrow({ where: { id: job.data.videoId } });
  const events = await prisma.videoEvent.findMany({
    where: { videoId: job.data.videoId },
    orderBy: { confidence: "desc" },
    take: env.MAX_CANDIDATE_EVENTS_PER_VIDEO,
  });
  await prisma.frame.deleteMany({ where: { videoId: job.data.videoId } });
  const seen = new Set<string>();
  for (const event of events) {
    for (const timestamp of frameTimes(event.timestampSeconds, video.durationSeconds ?? event.timestampSeconds + 30, env.MAX_FRAMES_PER_EVENT)) {
      const framePath = await media.frame(job.data.sourcePath, job.data.videoId, timestamp);
      const perceptualHash = await media.hash(framePath);
      if (seen.has(perceptualHash)) continue;
      seen.add(perceptualHash);
      const key = `videos/${job.data.videoId}/frames/${String(timestamp).replace(".", "_")}.webp`;
      await storage.put(framePath, key);
      await prisma.frame.create({
        data: {
          videoId: job.data.videoId,
          timestampSeconds: timestamp,
          filePath: framePath,
          storageUrl: storage.url(key),
          frameHash: await media.sha(framePath),
          perceptualHash,
          isEvidence: true,
        },
      });
    }
  }
  await enqueue("visual_analysis", job.data, `${job.data.videoId}-visual`);
});

add("visual_analysis", env.FRAME_ANALYSIS_CONCURRENCY, async (job) => {
  await status(job.data.videoId, "ANALYZING_VISUALS");
  const events = await prisma.videoEvent.findMany({ where: { videoId: job.data.videoId } });
  const frames = await prisma.frame.findMany({ where: { videoId: job.data.videoId } });
  await prisma.frameAnalysis.deleteMany({ where: { frame: { videoId: job.data.videoId } } });
  for (const event of events) {
    const nearby = frames.filter((frame) => frame.filePath && Math.abs(frame.timestampSeconds - event.timestampSeconds) <= 30).slice(0, env.MAX_FRAMES_PER_EVENT);
    if (!nearby.length) continue;
    const result = await analyzeFrames(nearby.map((frame) => frame.filePath!), event.description);
    const target = nearby[0];
    await prisma.frameAnalysis.create({
      data: {
        frameId: target.id,
        symbol: typeof result.symbol === "string" ? result.symbol : null,
        timeframe: typeof result.timeframe === "string" ? result.timeframe : null,
        visiblePrice: typeof result.visiblePrice === "number" ? result.visiblePrice : null,
        chartElements: Array.isArray(result.chartElements) ? result.chartElements : [],
        indicators: Array.isArray(result.indicators) ? result.indicators : [],
        drawings: Array.isArray(result.drawings) ? result.drawings : [],
        visualDescription: String(result.visualDescription ?? ""),
        confidence: Number(result.confidence ?? 0),
        evidenceLevel: result.evidenceLevel === "DIRECT" ? "DIRECT" : "STRONG_INFERENCE",
        rawJson: JSON.parse(JSON.stringify(result)),
      },
    });
  }
  await enqueue("trade_extraction", job.data, `${job.data.videoId}-trade`);
});

add("trade_extraction", env.AI_ANALYSIS_CONCURRENCY, async (job) => {
  await status(job.data.videoId, "EXTRACTING_TRADES");
  const entries = await prisma.videoEvent.findMany({ where: { videoId: job.data.videoId, eventType: "TRADE_ENTRY" } });
  const segments = await prisma.transcriptSegment.findMany({ where: { videoId: job.data.videoId } });
  const frames = await prisma.frame.findMany({ where: { videoId: job.data.videoId }, include: { analyses: true } });
  await prisma.trade.deleteMany({ where: { videoId: job.data.videoId } });
  let tradeNumber = 0;
  for (const entryEvent of entries) {
    const transcriptEvidence = segments.filter((segment) => Math.abs(segment.startSeconds - entryEvent.timestampSeconds) < 180);
    const visualEvidence = frames.filter((frame) => Math.abs(frame.timestampSeconds - entryEvent.timestampSeconds) < 90);
    const extracted = await extractTrade(job.data.videoId, transcriptEvidence, visualEvidence);
    if (extracted.actuallyTaken.value !== true) continue;
    tradeNumber += 1;
    const rr = riskReward(extracted.direction.value, extracted.entryPrice.value, extracted.stopLoss.value, extracted.takeProfit.value);
    const trade = await prisma.trade.create({
      data: {
        videoId: job.data.videoId,
        tradeNumber,
        symbol: extracted.symbol.value,
        direction: extracted.direction.value,
        entryPrice: extracted.entryPrice.value,
        stopLoss: extracted.stopLoss.value,
        takeProfit: extracted.takeProfit.value,
        exitPrice: extracted.exitPrice.value,
        entryTimestamp: extracted.entryTimestamp.value,
        exitTimestamp: extracted.exitTimestamp.value,
        result: extracted.outcome.value,
        riskReward: rr,
        setupName: extracted.setup.value,
        entryReason: extracted.entryReason.value,
        exitReason: extracted.exitReason.value,
        marketCondition: extracted.marketCondition.value,
        timeframes: extracted.timeframe.value ? [extracted.timeframe.value] : [],
        indicators: extracted.indicators.value ?? [],
        management: extracted.tradeManagement.value ?? [],
        confidence: extracted.overallConfidence,
        validationStatus: "PENDING",
        spokenProfitClaim: extracted.spokenProfitClaim.value,
        profitVerification: extracted.profitVerificationStatus,
      },
    });
    await prisma.tradeEvent.create({
      data: {
        tradeId: trade.id,
        eventType: "ENTRY",
        timestampSeconds: extracted.entryTimestamp.value ?? entryEvent.timestampSeconds,
        description: extracted.entryReason.value ?? "Trade entry observed",
        sourceType: extracted.actuallyTaken.evidence.some((item) => item.type === "transcript") ? "TRANSCRIPT" : "AI_INFERENCE",
        sourceReference: `video:${job.data.videoId}`,
        evidenceLevel: extracted.actuallyTaken.confidence >= 0.9 ? "DIRECT" : "STRONG_INFERENCE",
        confidence: extracted.actuallyTaken.confidence,
        evidencePayload: JSON.parse(JSON.stringify(extracted.actuallyTaken.evidence)),
      },
    });
    if (extracted.stopLoss.value !== null) {
      await prisma.tradeEvent.create({ data: { tradeId: trade.id, eventType: "SL_PLACED", timestampSeconds: extracted.entryTimestamp.value ?? entryEvent.timestampSeconds, description: `Observed stop loss ${extracted.stopLoss.value}`, sourceType: "AI_INFERENCE", evidenceLevel: extracted.stopLoss.confidence >= 0.9 ? "DIRECT" : "STRONG_INFERENCE", confidence: extracted.stopLoss.confidence, evidencePayload: JSON.parse(JSON.stringify(extracted.stopLoss.evidence)) } });
    }
    if (extracted.takeProfit.value !== null) {
      await prisma.tradeEvent.create({ data: { tradeId: trade.id, eventType: "TP_PLACED", timestampSeconds: extracted.entryTimestamp.value ?? entryEvent.timestampSeconds, description: `Observed take profit ${extracted.takeProfit.value}`, sourceType: "AI_INFERENCE", evidenceLevel: extracted.takeProfit.confidence >= 0.9 ? "DIRECT" : "STRONG_INFERENCE", confidence: extracted.takeProfit.confidence, evidencePayload: JSON.parse(JSON.stringify(extracted.takeProfit.evidence)) } });
    }
  }
  await enqueue("trade_validation", job.data, `${job.data.videoId}-validate`);
});

add("trade_validation", env.AI_ANALYSIS_CONCURRENCY, async (job) => {
  await status(job.data.videoId, "VALIDATING");
  const trades = await prisma.trade.findMany({ where: { videoId: job.data.videoId } });
  const segments = await prisma.transcriptSegment.findMany({ where: { videoId: job.data.videoId } });
  const frames = await prisma.frame.findMany({ where: { videoId: job.data.videoId }, include: { analyses: true } });
  for (const trade of trades) {
    const extracted = tradeSchema.parse({
      actuallyTaken: { value: true, confidence: trade.confidence, evidence: [] },
      symbol: { value: trade.symbol, confidence: trade.confidence, evidence: [] },
      direction: { value: trade.direction, confidence: trade.confidence, evidence: [] },
      entryPrice: { value: trade.entryPrice === null ? null : Number(trade.entryPrice), confidence: trade.confidence, evidence: [] },
      stopLoss: { value: trade.stopLoss === null ? null : Number(trade.stopLoss), confidence: trade.confidence, evidence: [] },
      takeProfit: { value: trade.takeProfit === null ? null : Number(trade.takeProfit), confidence: trade.confidence, evidence: [] },
      exitPrice: { value: trade.exitPrice === null ? null : Number(trade.exitPrice), confidence: trade.confidence, evidence: [] },
      entryTimestamp: { value: trade.entryTimestamp, confidence: trade.confidence, evidence: [] },
      exitTimestamp: { value: trade.exitTimestamp, confidence: trade.confidence, evidence: [] },
      setup: { value: trade.setupName, confidence: trade.confidence, evidence: [] },
      entryReason: { value: trade.entryReason, confidence: trade.confidence, evidence: [] },
      exitReason: { value: trade.exitReason, confidence: trade.confidence, evidence: [] },
      timeframe: { value: Array.isArray(trade.timeframes) ? trade.timeframes.map(String).join(" / ") : null, confidence: trade.confidence, evidence: [] },
      indicators: { value: Array.isArray(trade.indicators) ? trade.indicators.map(String) : [], confidence: trade.confidence, evidence: [] },
      marketCondition: { value: trade.marketCondition, confidence: trade.confidence, evidence: [] },
      tradeManagement: { value: Array.isArray(trade.management) ? trade.management.map(String) : [], confidence: trade.confidence, evidence: [] },
      outcome: { value: trade.result, confidence: trade.confidence, evidence: [] },
      spokenProfitClaim: { value: trade.spokenProfitClaim, confidence: trade.confidence, evidence: [] },
      profitVerificationStatus: trade.profitVerification,
      overallConfidence: trade.confidence,
    });
    const validation = await validateTrade(extracted, segments, frames);
    await prisma.trade.update({ where: { id: trade.id }, data: { validationStatus: validation.status, validationReason: validation.reason, confidence: Math.min(trade.confidence, validation.confidence) } });
  }
  await status(job.data.videoId, "INDEXING");
  await enqueue("embedding", job.data, `${job.data.videoId}-index`);
});

add("embedding", env.AI_ANALYSIS_CONCURRENCY, async (job) => {
  const segments = await prisma.transcriptSegment.findMany({ where: { videoId: job.data.videoId } });
  await prisma.knowledgeChunk.deleteMany({ where: { videoId: job.data.videoId } });
  for (const segment of segments) {
    const chunk = await prisma.knowledgeChunk.create({
      data: {
        videoId: job.data.videoId,
        sourceType: "TRANSCRIPT",
        sourceId: segment.id,
        title: `Transcript @ ${segment.startSeconds.toFixed(1)}s`,
        content: segment.text,
        metadata: { start: segment.startSeconds, end: segment.endSeconds },
      },
    });
    await indexKnowledgeChunk(chunk.id, chunk.content, job.data.videoId);
  }
  const trades = await prisma.trade.findMany({ where: { videoId: job.data.videoId }, include: { events: true } });
  for (const trade of trades) {
    const content = JSON.stringify({ symbol: trade.symbol, direction: trade.direction, setup: trade.setupName, entryReason: trade.entryReason, exitReason: trade.exitReason, result: trade.result, events: trade.events });
    const chunk = await prisma.knowledgeChunk.create({ data: { videoId: job.data.videoId, sourceType: "TRADE", sourceId: trade.id, title: `Trade #${trade.tradeNumber} ${trade.symbol ?? "unknown"}`, content } });
    await indexKnowledgeChunk(chunk.id, chunk.content, job.data.videoId);
  }
  await enqueue("strategy_aggregation", job.data, `${job.data.videoId}-aggregate`);
});

add("strategy_aggregation", 1, async (job) => {
  await aggregateStrategyPatterns();
  await enqueue("knowledge_synthesis", job.data, `${job.data.videoId}-knowledge`);
});

add("knowledge_synthesis", 1, async (job) => {
  await synthesizePlaybooks();
  await detectContradictions();
  if (job.data.videoId !== "manual") {
    await status(job.data.videoId, "COMPLETED");
    await media.cleanup(job.data.videoId);
  }
});

process.on("SIGTERM", async () => {
  await Promise.all(workers.map((worker) => worker.close()));
  await connection.quit();
  process.exit(0);
});

console.log(`Started ${workers.length} pipeline workers`);
