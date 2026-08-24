export function generateFrameTimestamps(
  eventTimestamp: number,
  durationSeconds: number,
  offsets: number[],
  maxFrames: number,
): number[] {
  const values = offsets
    .map((offset) => eventTimestamp + offset)
    .filter((value) => value >= 0 && value <= durationSeconds)
    .map((value) => Math.round(value * 1000) / 1000);

  return [...new Set(values)].slice(0, maxFrames);
}
