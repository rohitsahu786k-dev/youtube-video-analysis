import re
from urllib.parse import urlparse, parse_qs

def canonical_url(url):
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.hostname not in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be") or parsed.username or parsed.password or parsed.port:
        raise ValueError("Enter an HTTPS YouTube video or playlist URL.")
    query = parse_qs(parsed.query)
    playlist = query.get("list", [None])[0]
    if playlist:
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", playlist):
            raise ValueError("Invalid YouTube playlist ID.")
        return f"https://www.youtube.com/playlist?list={playlist}", True
    video = parsed.path.strip("/") if parsed.hostname == "youtu.be" else query.get("v", [None])[0]
    if parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
        video = parsed.path.split("/")[2]
    if not video or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
        raise ValueError("Invalid YouTube video ID.")
    return f"https://www.youtube.com/watch?v={video}", False
