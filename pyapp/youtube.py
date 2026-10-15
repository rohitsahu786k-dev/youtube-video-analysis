import re
from urllib.parse import urlparse, parse_qs

HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")
CHANNEL_TABS = ("videos", "streams", "shorts", "featured", "playlists", "community", "about", "releases")


def _validated(url):
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username or parsed.password or parsed.port:
        raise ValueError("Enter an HTTPS YouTube video, playlist or channel URL.")
    return parsed


def parse_source(url):
    """Classify a YouTube URL as video, playlist or channel and return its canonical form."""
    parsed = _validated(url)
    query = parse_qs(parsed.query)
    path = parsed.path.rstrip("/")
    playlist = query.get("list", [None])[0]
    if parsed.path.startswith("/playlist") or playlist:
        if not playlist or not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", playlist):
            raise ValueError("Invalid YouTube playlist ID.")
        return {"kind": "playlist", "url": f"https://www.youtube.com/playlist?list={playlist}", "id": playlist}
    handle = re.match(r"^/(@[A-Za-z0-9._-]{2,60})(?:/([a-z]+))?$", path)
    channel_id = re.match(r"^/channel/(UC[A-Za-z0-9_-]{22})(?:/([a-z]+))?$", path)
    legacy = re.match(r"^/(?:c|user)/([A-Za-z0-9._%-]{2,100})(?:/([a-z]+))?$", path)
    for match, prefix in ((handle, "/{}"), (channel_id, "/channel/{}"), (legacy, None)):
        if match:
            tab = match.group(2)
            if tab and tab not in CHANNEL_TABS:
                raise ValueError("Unsupported channel page.")
            base = f"/{match.group(1)}" if match is handle else f"/channel/{match.group(1)}" if match is channel_id else path.rsplit("/", 1)[0] if tab else path
            return {"kind": "channel", "url": f"https://www.youtube.com{base}", "id": match.group(1)}
    return {"kind": "video", **_video(parsed, query, path)}


def _video(parsed, query, path):
    video = path.strip("/") if parsed.hostname == "youtu.be" else query.get("v", [None])[0]
    if parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
        video = parsed.path.split("/")[2]
    if not video or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
        raise ValueError("Invalid YouTube video ID.")
    return {"url": f"https://www.youtube.com/watch?v={video}", "id": video}


def canonical_url(url):
    """Video/playlist canonicalisation kept for callers that only handle those two kinds."""
    parsed = _validated(url)
    query = parse_qs(parsed.query)
    playlist = query.get("list", [None])[0]
    if playlist:
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", playlist):
            raise ValueError("Invalid YouTube playlist ID.")
        return f"https://www.youtube.com/playlist?list={playlist}", True
    return _video(parsed, query, parsed.path.rstrip("/"))["url"], False
