import { NextResponse } from "next/server";
import { z } from "zod";
import { env } from "@/lib/config";
import { enqueue } from "@/lib/queue";
import { cancelVideoJobs, markPaused, resumeVideo } from "@/lib/processing";
import { rateLimit } from "@/lib/utils";

const schema = z.object({
  action: z.enum(["start", "retry", "reprocess", "pause", "resume", "cancel", "visual", "aggregate"]),
  videoId: z.string().optional(),
  youtubeVideoId: z.string().optional(),
});

export async function POST(req: Request) {
  if (!rateLimit(`p-${req.headers.get("x-forwarded-for") ?? "local"}`)) {
    return NextResponse.json({ error: "Rate limit exceeded" }, { status: 429 });
  }
  try {
    const body = schema.parse(await req.json());
    if (env.MOCK_AI_MODE || !env.REDIS_URL || !env.DATABASE_URL) {
      return NextResponse.json(
        {
          message: `${body.action} accepted · demo pipeline state updated`,
          status: body.action === "cancel" ? "CANCELLED" : body.action === "pause" ? "PAUSED" : "QUEUED",
        },
        { status: 202 },
      );
    }

    if (["start", "retry", "reprocess"].includes(body.action)) {
      if (!body.videoId || !body.youtubeVideoId) throw new Error("videoId and youtubeVideoId required");
      await enqueue("video_metadata", { videoId: body.videoId, youtubeVideoId: body.youtubeVideoId }, `${body.videoId}-metadata-${Date.now()}`);
    } else if (body.action === "pause") {
      if (!body.videoId) throw new Error("videoId required");
      await markPaused(body.videoId);
    } else if (body.action === "resume") {
      if (!body.videoId || !body.youtubeVideoId) throw new Error("videoId and youtubeVideoId required");
      await resumeVideo(body.videoId, body.youtubeVideoId);
    } else if (body.action === "cancel") {
      if (!body.videoId) throw new Error("videoId required");
      await cancelVideoJobs(body.videoId);
    } else if (body.action === "visual") {
      if (!body.videoId) throw new Error("videoId required");
      await enqueue("visual_analysis", { videoId: body.videoId, youtubeVideoId: body.youtubeVideoId ?? "" }, `${body.videoId}-visual-${Date.now()}`);
    } else if (body.action === "aggregate") {
      await enqueue("strategy_aggregation", { videoId: body.videoId ?? "manual" }, `aggregate-${Date.now()}`);
    }
    return NextResponse.json({ message: `${body.action} accepted` }, { status: 202 });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "Invalid request" }, { status: 400 });
  }
}
