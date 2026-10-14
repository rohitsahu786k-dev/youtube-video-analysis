# AI inference

Google Gemini handles chunked transcription, full-video visual/audio event discovery, frame readings, multi-trade extraction, skeptical image validation, teaching-rule synthesis, draft-rule compilation, query keyword translation and evidence-grounded answers. Schemas are checked against the installed Gemini Developer API SDK; unconstrained dictionary schemas are avoided.

Calls are asynchronous, bounded by provider request timeouts, and transient 429/5xx responses receive limited retries. Missing credentials, unsupported schemas, unavailable models and empty structured responses fail explicitly. No AI function creates fake trades as a fallback. MOCK_AI_MODE disables real processing.

The system prompt treats video/transcript text as untrusted evidence and separates speech, visible facts and inference. Sources preserve video timestamps and original chart images. Exact prices require evidence; confidence alone cannot establish a price, outcome or reward/risk ratio. Low-confidence, conflicting or execution-unsupported candidates remain uncertain.

Local keyword retrieval selects stored evidence. Unmatched English/Hindi/Hinglish questions can use Gemini keyword translation before another local search. Invalid/uncertain trade chunks are excluded from ordinary answers. Responses cite timestamped sources and admit insufficient evidence. This is keyword-grounded retrieval, not an implemented vector database.

Recurring patterns generate drafts with explicit supported rule parameters only. The model is instructed not to invent lookbacks, targets, buffers or holding periods, or erase discretionary filters. User review is required before a mechanical interpretation can qualify for live paper analysis. No AI model predicts an unobserved market price or submits an order.
