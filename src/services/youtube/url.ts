const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;

export type YouTubeInput =
  | { type: "video"; id: string }
  | { type: "playlist"; id: string };

export function parseYouTubeUrl(input: string): YouTubeInput {
  let url: URL;
  try {
    url = new URL(input.trim());
  } catch {
    throw new Error("Invalid YouTube URL");
  }

  const host = url.hostname.replace(/^www\./, "").toLowerCase();
  if (!["youtube.com", "m.youtube.com", "youtu.be"].includes(host)) {
    throw new Error("Unsupported YouTube host");
  }

  const playlistId = url.searchParams.get("list");
  if (playlistId && playlistId.length >= 10) return { type: "playlist", id: playlistId };

  let videoId: string | null = null;
  if (host === "youtu.be") videoId = url.pathname.split("/").filter(Boolean)[0] ?? null;
  if (url.pathname === "/watch") videoId = url.searchParams.get("v");
  if (url.pathname.startsWith("/shorts/") || url.pathname.startsWith("/live/")) {
    videoId = url.pathname.split("/").filter(Boolean)[1] ?? null;
  }

  if (!videoId || !VIDEO_ID.test(videoId)) throw new Error("Could not determine a valid YouTube video ID");
  return { type: "video", id: videoId };
}

export function youtubeTimestampUrl(videoId: string, seconds: number): string {
  if (!VIDEO_ID.test(videoId)) throw new Error("Invalid YouTube video ID");
  return `https://www.youtube.com/watch?v=${videoId}&t=${Math.max(0, Math.floor(seconds))}s`;
}
