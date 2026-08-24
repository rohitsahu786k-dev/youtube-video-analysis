# Cost Optimization

Frame windows, interval, event count, image detail and concurrency are configurable. The default pipeline does not sample every second of video.

Text-model pricing is centralized in `src/config/model-pricing.ts` and can be replaced at runtime with `OPENAI_PRICING_JSON`; it is not distributed throughout business logic. Batch processing is available through `BatchAnalysisService` for offline workloads.

The dashboard labels cost as an estimate and breaks it down by operation.
