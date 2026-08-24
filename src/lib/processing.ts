import { prisma } from "./db";
import { q, queues } from "./queue";

export async function cancelVideoJobs(videoId: string) {
  let removed = 0;
  for (const name of queues) {
    const queue = q(name);
    const jobs = await queue.getJobs(["wait", "delayed", "prioritized", "paused"], 0, 1000, true);
    for (const job of jobs) {
      const id = String(job.id ?? "");
      const dataVideoId = typeof job.data?.videoId === "string" ? job.data.videoId : "";
      if (dataVideoId !== videoId && !id.startsWith(videoId)) continue;
      await job.remove();
      removed += 1;
    }
  }
  if (videoId) {
    await prisma.video.update({ where: { id: videoId }, data: { processingStatus: "CANCELLED" } });
  }
  return removed;
}

export async function markPaused(videoId: string) {
  await prisma.video.update({ where: { id: videoId }, data: { processingStatus: "PAUSED" } });
}

export async function resumeVideo(videoId: string, youtubeVideoId: string) {
  await prisma.video.update({ where: { id: videoId }, data: { processingStatus: "QUEUED", processingError: null } });
  await q("video_metadata").add("video_metadata", { videoId, youtubeVideoId }, { jobId: `${videoId}-resume-${Date.now()}` });
}

export async function assertRunnable(videoId: string) {
  const video = await prisma.video.findUnique({ where: { id: videoId }, select: { processingStatus: true } });
  if (!video) throw new Error("Video not found");
  if (video.processingStatus === "CANCELLED") return false;
  if (video.processingStatus === "PAUSED") return false;
  return true;
}
