import { describe, expect, it } from "vitest";
import { parseYouTubeUrl, youtubeTimestampUrl } from "../src/services/youtube/url";

describe("YouTube URL parser", () => {
  it("parses a watch URL", () => expect(parseYouTubeUrl("https://www.youtube.com/watch?v=dQw4w9WgXcQ")).toEqual({ type: "video", id: "dQw4w9WgXcQ" }));
  it("parses a playlist URL", () => expect(parseYouTubeUrl("https://www.youtube.com/playlist?list=PL1234567890abcdef")).toEqual({ type: "playlist", id: "PL1234567890abcdef" }));
  it("rejects foreign hosts", () => expect(() => parseYouTubeUrl("https://example.com/watch?v=dQw4w9WgXcQ")).toThrow());
  it("builds timestamp links", () => expect(youtubeTimestampUrl("dQw4w9WgXcQ", 754.8)).toContain("t=754s"));
});
