import { tradingKeywords } from "@/config/env";

export type TranscriptSegment = { start: number; end: number; text: string };
export type CandidateEvent = {
  timestampSeconds: number;
  eventType: "TRADE_ENTRY" | "TRADE_EXIT" | "STOP_LOSS" | "TAKE_PROFIT" | "PARTIAL_EXIT" | "BREAKEVEN" | "SETUP_FORMATION" | "MARKET_ANALYSIS" | "BIAS_CHANGE" | "TRADE_MANAGEMENT";
  transcriptEvidence: string;
  confidence: number;
};

const rules: Array<[CandidateEvent["eventType"], RegExp]> = [
  ["TRADE_ENTRY", /\b(enter(?:ing|ed)?|buy(?:ing)?|sell(?:ing)?|long|short|position|order)\b/i],
  ["TRADE_EXIT", /\b(exit|close(?:d|ing)?|closed the trade)\b/i],
  ["STOP_LOSS", /\b(stop loss|\bsl\b|stopped out)\b/i],
  ["TAKE_PROFIT", /\b(take profit|\btp\b|target(?: hit)?)\b/i],
  ["PARTIAL_EXIT", /\b(partial|scale out|take some off)\b/i],
  ["BREAKEVEN", /\b(break\s?even|\bbe\b|risk free)\b/i],
  ["BIAS_CHANGE", /\b(bias (?:changed|change|flip)|turn(?:ing)? bullish|turn(?:ing)? bearish)\b/i],
  ["SETUP_FORMATION", /\b(liquidity|sweep|breakout|retest|rejection|confirmation|setup|signal)\b/i],
  ["TRADE_MANAGEMENT", /\b(move(?:d|ing)? (?:my )?stop|trail(?:ing)?|manage(?:ment|ing)?)\b/i],
];

export function detectLexicalCandidates(segments: TranscriptSegment[], maxEvents = 100): CandidateEvent[] {
  const configured = tradingKeywords();
  const candidates: CandidateEvent[] = [];

  for (const segment of segments) {
    const normalized = segment.text.toLowerCase();
    if (!configured.some((keyword) => normalized.includes(keyword))) continue;

    const matches = rules.filter(([, regex]) => regex.test(segment.text));
    if (matches.length === 0) {
      candidates.push({ timestampSeconds: segment.start, eventType: "MARKET_ANALYSIS", transcriptEvidence: segment.text, confidence: 0.45 });
    } else {
      for (const [eventType] of matches) {
        candidates.push({ timestampSeconds: segment.start, eventType, transcriptEvidence: segment.text, confidence: 0.65 });
      }
    }
    if (candidates.length >= maxEvents) break;
  }

  return dedupeCandidates(candidates).slice(0, maxEvents);
}

export function dedupeCandidates(events: CandidateEvent[], windowSeconds = 8): CandidateEvent[] {
  return events
    .sort((a, b) => a.timestampSeconds - b.timestampSeconds)
    .filter((event, index, all) => !all.slice(0, index).some((previous) =>
      previous.eventType === event.eventType && Math.abs(previous.timestampSeconds - event.timestampSeconds) <= windowSeconds,
    ));
}
