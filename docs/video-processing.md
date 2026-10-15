# Video processing and evidence

Transcript chunks, spoken event detection and completed ten-minute video scan chunks are checkpointed so retries can reuse saved results. A request interrupted before its result is saved may still need to be repeated.

One video uses multiple paid calls: audio transcription, video scanning, event frame analysis, trade extraction/validation and strategy extraction. The daily estimated-spend budget is checked before each generation request, including retries within an active video. Responses are accounted for even if their structured output is unusable. This is not a provider billing cap: an in-flight request (or concurrent requests) can exceed the remaining budget, and estimates depend on configured model prices. Reaching the budget can leave a video partially processed; review its status before retrying.

HTTPS YouTube video and playlist URLs are canonicalized. Playlist discovery runs separately from video analysis, persists membership, queues each accessible entry, and does not duplicate existing videos. Oversized playlists are rejected explicitly. Private/unavailable videos show failures, not successful demo results.

Video downloads preserve chart readability up to 1080p. Audio is split into ten-minute chunks; relative segment timestamps are translated into absolute video timestamps. Gemini also scans the visual/audio timeline in clipped intervals at one frame per second to find silent executions and teaching events.

Each event gets a configurable before/after frame window. Individual images retain fractional-second filenames, timestamps, SHA-256 hashes and source links. Extraction and validation examine original frames, not just an aggregate text summary. A position drawing is not proof of a fill. Multiple entries and subsequent exits update distinct evidence records; unsupported numbers remain unknown. R:R comes from supported entry/stop/target geometry.

Stored transcripts, visual observations, taught rules and actual trade candidates populate the knowledge base. Trade statistics exclude invalid/uncertain executions and unknown outcomes. Teaching concepts can recur without proving that an actual live trade was executed. Rule proposals retain missing or unsupported parameters for review.

A scan can miss actions between sampled frames, and edited/blurred videos cannot establish an exact audit trail. Detailed event caps are visible in coverage metadata. AI validation remains fallible. Processing progress and errors persist; interrupted jobs reuse completed checkpoints, while unfinished requests and stages may incur additional inference cost. Temporary downloads/audio are cleaned up after processing, while chart evidence remains on disk.
