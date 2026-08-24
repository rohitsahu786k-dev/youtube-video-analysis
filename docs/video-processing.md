# Video Processing
The worker validates YouTube URLs before invoking external tools. yt-dlp downloads only the selected video. FFmpeg extracts compressed mono audio and WebP evidence frames only around candidate events. Frames are bounded, compressed and deduplicated with perceptual/SHA hashes. Worker host requirements: Node 20+, FFmpeg and yt-dlp.
