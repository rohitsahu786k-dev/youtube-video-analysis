# Video Processing

The worker validates YouTube URLs before running external tools. `yt-dlp` downloads a single validated video and FFmpeg extracts mono 16 kHz compressed audio. Frames are extracted only around candidate timestamps, resized to at most 1280px width and stored as compressed WebP.

Duplicate suppression uses SHA-256 plus a simple 8×8 perceptual hash. Temporary source media is removed when the pipeline completes.

Requirements on the worker host: Node 22+, FFmpeg and yt-dlp.
