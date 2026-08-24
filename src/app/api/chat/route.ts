import { NextResponse } from "next/server";
import { z } from "zod";
import { answer } from "@/lib/ai";
import { knowledge, videos } from "@/lib/demo";
import { semanticSearch } from "@/lib/knowledge";
import { env } from "@/lib/config";
import { rateLimit, yt } from "@/lib/utils";

const schema = z.object({ question: z.string().trim().min(2).max(2000) });

export async function POST(req: Request) {
  if (!rateLimit(`c-${req.headers.get("x-forwarded-for") ?? "local"}`)) {
    return NextResponse.json({ error: "Rate limit exceeded" }, { status: 429 });
  }
  try {
    const { question } = schema.parse(await req.json());
    if (!env.MOCK_AI_MODE && env.OPENAI_API_KEY && env.DATABASE_URL) {
      const hits = await semanticSearch(question, 8);
      const context = hits.map((hit, index) => ({
        index: index + 1,
        title: hit.title,
        sourceType: hit.sourceType,
        content: hit.content,
        metadata: hit.metadata,
        youtubeVideoId: hit.youtubeVideoId,
        videoTitle: hit.videoTitle,
      }));
      const text = await answer(JSON.stringify({ question, retrievedEvidence: context }));
      const sources = hits.map((hit) => {
        const metadata = typeof hit.metadata === "object" && hit.metadata !== null ? hit.metadata as Record<string, unknown> : {};
        const timestamp = typeof metadata.start === "number" ? metadata.start : 0;
        return {
          title: hit.videoTitle ?? hit.title ?? "Knowledge source",
          timestamp,
          url: hit.youtubeVideoId ? yt(hit.youtubeVideoId, timestamp) : null,
          type: hit.sourceType,
          distance: hit.distance,
        };
      });
      return NextResponse.json({
        answer: text,
        pattern: "Patterns are reported only from retrieved analyzed-video evidence and validated trade records.",
        evidence: hits.map((hit) => `${hit.sourceType}: ${hit.content}`),
        limitations: "Derived from analyzed videos only, not audited trading history. AI confidence is not a statistical probability.",
        sources,
      });
    }

    const text = await answer(question);
    const sources = knowledge.slice(0, 3).map((item) => {
      const video = videos.find((candidate) => candidate.title === item.video) ?? videos[0];
      return { title: item.video, timestamp: item.time, url: yt(video.youtubeId, item.time), type: item.type };
    });
    return NextResponse.json({
      answer: text,
      pattern: "Recurring claims are promoted to patterns only after multiple supported occurrences; unknown fields stay unknown.",
      evidence: knowledge.slice(0, 3).map((item) => `${item.type}: ${item.content}`),
      limitations: "Derived from analyzed videos only, not audited trading history. AI confidence is not a statistical probability.",
      sources,
    });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "Invalid request" }, { status: 400 });
  }
}
