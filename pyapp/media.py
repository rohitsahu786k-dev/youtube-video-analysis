import os
import asyncio
import shutil
import hashlib
import subprocess
from typing import Dict, Any, List
from PIL import Image
import imagehash
import yt_dlp
from pathlib import Path
from pyapp.config import settings

async def blocking_call(function, *args):
    # A cancelled coroutine must not delete/reuse files while its FFmpeg/download
    # thread is still writing them. Finish the active blocking step before cleanup.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise

# FFmpeg is located through PATH, FFMPEG_PATH or the WinGet install directory (see MediaProcessor.ffmpeg).
for p in [r"C:\Program Filesfmpegin"]:
    if os.path.exists(p) and p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")


def classify_download_error(exc):
    """Map a yt-dlp/ffmpeg failure to an explicit processing state (SRS 2.2)."""
    text = str(exc).lower()
    if any(k in text for k in ("sign in to confirm", "confirm your age", "age-restricted", "age restricted", "members-only", "members only", "join this channel",
                               "login required", "requires payment", "not a bot", "use --cookies", "cookies")):
        return "AWAITING_PERMISSION", "Access needs authorization (sign-in, age or membership restriction). Provide a cookies file for an account you are authorized to use, or upload the media/transcript."
    if any(k in text for k in ("private video", "video unavailable", "this video is not available", "has been removed", "account associated", "terminated", "deleted",
                               "blocked it in your country", "not available in your country", "copyright", "no longer available", "does not exist")):
        return "UNAVAILABLE", "The video is private, removed or blocked and cannot be accessed. Upload authorized media or a transcript to analyse it."
    if any(k in text for k in ("http error 429", "too many requests", "timed out", "timeout", "temporary failure", "connection reset", "http error 5", "network is unreachable", "unable to download")):
        return "TRANSIENT", str(exc)[:300]
    return "FAILED", str(exc)[:500]


class MediaProcessor:
    def __init__(self, storage_path: str = settings.STORAGE_PATH):
        self.storage_path = storage_path
        os.makedirs(storage_path, exist_ok=True)

    def js_runtimes(self):
        return {name: {"path": executable} for name in ("deno", "node") if (executable := shutil.which(name))}

    def dimensions(self, source):
        probe = self._probe()
        result = subprocess.run([str(probe), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", source], capture_output=True, text=True, check=True, timeout=30)
        import json
        return json.loads(result.stdout)["streams"][0]

    def _probe(self):
        ffmpeg = Path(self.ffmpeg())
        return str(ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix == ".exe" else "ffprobe"))

    def has_video_stream(self, source):
        result = subprocess.run([self._probe(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", source],
                                capture_output=True, text=True, timeout=60)
        return "video" in result.stdout

    def media_duration(self, source):
        result = subprocess.run([self._probe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", source], capture_output=True, text=True, timeout=60)
        return float(result.stdout.strip() or 0)

    def ffmpeg(self):
        if settings.FFMPEG_PATH:
            executable = settings.FFMPEG_PATH if Path(settings.FFMPEG_PATH).is_file() else shutil.which(settings.FFMPEG_PATH)
            if not executable:
                raise RuntimeError("FFMPEG_PATH does not resolve to an executable.")
            return executable
        executable = shutil.which("ffmpeg")
        if not executable:
            root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
            executable = next((str(p) for p in root.glob("*FFmpeg*/**/bin/ffmpeg.exe")), None)
        if not executable:
            raise RuntimeError("FFmpeg is not installed. Install it or set FFMPEG_PATH.")
        return executable

    def audio_chunks(self, source, video_id, duration):
        chunks = []
        out_dir = Path(self.storage_path) / "tmp" / video_id
        out_dir.mkdir(parents=True, exist_ok=True)
        for start in range(0, max(1, int(duration)), settings.AUDIO_CHUNK_SECONDS):
            path = out_dir / f"audio_{start}.mp3"
            subprocess.run([self.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
                            "-i", source, "-t", str(settings.AUDIO_CHUNK_SECONDS), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(path)], check=True, timeout=600)
            chunks.append({"path": str(path), "offset": start})
        return chunks

    def get_info(self, url: str, playlist: bool = False, limit: int | None = None) -> Dict[str, Any]:
        limit = limit or settings.MAX_PLAYLIST_VIDEOS
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist" if playlist else False,
            "noplaylist": not playlist,
            "playlistend": limit + 1,
            "socket_timeout": 30,
            "retries": 3,
            "cookiefile": settings.YTDLP_COOKIES_PATH,
            "js_runtimes": self.js_runtimes(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if "entries" in info:
                entries = [e for e in info.get("entries", []) if e]
                nested = [e for e in entries if e.get("_type") == "playlist" and not e.get("duration")]
                if nested and len(nested) == len(entries):
                    # A channel root lists tabs; walk the video-bearing ones.
                    flat = []
                    for tab in nested:
                        title = (tab.get("title") or "").lower()
                        if any(k in title for k in ("videos", "live", "streams")) or not title:
                            flat.extend(e for e in tab.get("entries", []) if e)
                    entries = flat
                truncated = len(entries) > limit
                entries = entries[:limit]
                first = entries[0] if entries else {}
                return {
                    "isPlaylist": True,
                    "playlistId": info.get("id"),
                    "title": info.get("title", "YouTube Playlist"),
                    "videoCount": len(entries),
                    "truncated": truncated,
                    "channel": info.get("uploader") or info.get("channel") or first.get("channel") or first.get("uploader"),
                    "channelId": info.get("channel_id") or info.get("uploader_id"),
                    "videos": [
                        {
                            "id": f"vid-{e.get('id')}",
                            "youtubeId": e.get("id"),
                            "title": e.get("title"),
                            "duration": e.get("duration") or 0,
                            "thumbnail": e.get("thumbnail"),
                            "availability": e.get("availability"),
                            "uploadDate": e.get("upload_date"),
                            "description": (e.get("description") or "")[:600],
                        }
                        for e in entries if e.get("id")
                    ]
                }
            return {
                "isPlaylist": False,
                "id": info.get("id"),
                "youtubeId": info.get("id"),
                "title": info.get("title", "YouTube Video"),
                "channel": info.get("uploader") or info.get("channel", "Unknown Channel"),
                "channelId": info.get("channel_id"),
                "duration": info.get("duration", 0),
                "thumbnail": info.get("thumbnail", f"https://i.ytimg.com/vi/{info.get('id')}/hqdefault.jpg"),
                "description": (info.get("description") or "")[:4000],
                "uploadDate": info.get("upload_date"),
                "liveStatus": info.get("live_status"),
                "availability": info.get("availability"),
            }

    def download_video(self, url: str, video_id: str) -> str:
        out_dir = os.path.join(self.storage_path, "tmp", video_id)
        os.makedirs(out_dir, exist_ok=True)
        out_file = os.path.join(out_dir, "source.mp4")
        if os.path.exists(out_file):
            return out_file

        ydl_opts = {
            "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(out_dir, "source.%(ext)s"),
            "noplaylist": True,
            "cookiefile": settings.YTDLP_COOKIES_PATH,
            "ffmpeg_location": os.path.dirname(self.ffmpeg()),
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "noprogress": True,
            "js_runtimes": self.js_runtimes(),
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        sources = list(Path(out_dir).glob("source.*"))
        return next((str(p) for p in sources if p.suffix in (".mp4", ".webm", ".mkv")), out_file)

    def extract_audio(self, video_path: str, video_id: str) -> str:
        out_dir = os.path.join(self.storage_path, "tmp", video_id)
        os.makedirs(out_dir, exist_ok=True)
        out_audio = os.path.join(out_dir, "audio.mp3")
        if os.path.exists(out_audio):
            return out_audio

        cmd = [
            self.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
            "-b:a", "64k", out_audio
        ]
        subprocess.run(cmd, check=True, timeout=600)
        return out_audio

    def extract_frame(self, video_path: str, video_id: str, timestamp_seconds: float) -> str:
        out_dir = os.path.join(self.storage_path, "evidence", video_id, "frames")
        os.makedirs(out_dir, exist_ok=True)
        out_frame = os.path.join(out_dir, f"frame_{timestamp_seconds:.3f}.webp")
        if os.path.exists(out_frame):
            return out_frame

        cmd = [
            self.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(timestamp_seconds), "-i", video_path,
            "-frames:v", "1", "-vf", "scale='min(1920,iw)':-2",
            "-c:v", "libwebp", "-quality", "92", out_frame
        ]
        subprocess.run(cmd, check=True, timeout=120)
        return out_frame

    def scene_changes(self, video_path: str, threshold: float = 0.30, limit: int = 4000) -> list:
        """Timestamps where the picture changes materially (chart redraws, new zones, slide changes)."""
        import re
        command = [self.ffmpeg(), "-hide_banner", "-nostats", "-i", video_path, "-an", "-vf", f"scale=320:-2,select=gt(scene\\,{threshold}),showinfo", "-f", "null", "-"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
        times = [float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+)", result.stderr)]
        return sorted(set(round(t, 2) for t in times))[:limit]

    def perceptual_hash(self, image_path: str) -> str:
        with Image.open(image_path) as img:
            return str(imagehash.phash(img))

    def sha256_hash(self, file_path: str) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()

    def cleanup(self, video_id: str):
        out_dir = os.path.join(self.storage_path, "tmp", video_id)
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)

media_processor = MediaProcessor()
