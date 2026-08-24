import { describe, expect, it } from "vitest";
import { generateFrameTimestamps } from "../src/services/video/frame-timestamps";

describe("frame timestamps", () => {
  it("clips timestamps to the video and max frame count", () => {
    expect(generateFrameTimestamps(10, 100, [-15, -10, -5, 0, 5, 10, 20, 30], 4)).toEqual([0, 5, 10, 15]);
  });
});
