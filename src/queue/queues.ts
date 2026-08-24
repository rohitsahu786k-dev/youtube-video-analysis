export const QUEUES = {
  videoMetadata: "video_metadata",
  audioExtraction: "audio_extraction",
  transcription: "transcription",
  candidateDetection: "candidate_detection",
  frameExtraction: "frame_extraction",
  visualAnalysis: "visual_analysis",
  tradeExtraction: "trade_extraction",
  tradeValidation: "trade_validation",
  strategyAggregation: "strategy_aggregation",
  embedding: "embedding",
  knowledgeSynthesis: "knowledge_synthesis",
} as const;

export type QueueName = (typeof QUEUES)[keyof typeof QUEUES];

export const DEFAULT_JOB_OPTIONS = {
  attempts: 4,
  backoff: { type: "exponential" as const, delay: 5_000 },
  removeOnComplete: { age: 60 * 60 * 24, count: 2_000 },
  removeOnFail: { age: 60 * 60 * 24 * 7, count: 5_000 },
};
