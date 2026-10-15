"""Media library: authenticated per-user uploads (video, audio, images, documents) with type checks, size/quota/file-count limits and de-duplication.

Files are streamed to disk in 1 MB chunks (never held in memory), stored under an id-based path (the client file name is only a label), checked by
extension *and* leading bytes, counted against a per-user quota and refused when the server disk or inode headroom is low."""
import codecs
import hashlib
import mimetypes
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from pyapp import limits
from pyapp.config import settings
from pyapp.db import db, owner_var

router = APIRouter(prefix="/api/media", tags=["Media"])

KINDS = {".mp4": "video", ".mkv": "video", ".webm": "video", ".mov": "video", ".mp3": "audio", ".m4a": "audio", ".wav": "audio", ".ogg": "audio",
         ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
         ".pdf": "document", ".txt": "document", ".md": "document", ".srt": "document", ".vtt": "document", ".json": "document", ".csv": "document"}
TEXT_TYPES = {".txt", ".md", ".srt", ".vtt", ".json", ".csv"}
INLINE_KINDS = {"video", "audio", "image"}
MAX_FILES = 1000
CHUNK = 1024 * 1024


def _magic_ok(ext: str, head: bytes) -> bool:
    if ext in (".mp4", ".mov", ".m4a"):
        return head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip")
    if ext in (".mkv", ".webm"):
        return head[:4] == b"\x1a\x45\xdf\xa3"
    if ext == ".mp3":
        return head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if ext == ".wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == ".ogg":
        return head[:4] == b"OggS"
    if ext == ".png":
        return head[:8] == b"\x89PNG\r\n\x1a\n"
    if ext in (".jpg", ".jpeg"):
        return head[:3] == b"\xff\xd8\xff"
    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == ".gif":
        return head[:4] == b"GIF8"
    if ext == ".pdf":
        return head[:5] == b"%PDF-"
    if ext in TEXT_TYPES:
        if b"\x00" in head:
            return False
        try:
            codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
            return True
        except UnicodeDecodeError:
            return False
    return False


def _owner_key() -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", owner_var.get()) or "root"


def _root() -> Path:
    return Path(settings.STORAGE_PATH) / "media" / _owner_key()


def _label(name: str | None) -> str:
    base = Path(name or "file").name
    return re.sub(r"[^\w.\- ()]", "_", base, flags=re.UNICODE).strip(" .")[:120] or "file"


def _public(doc: dict) -> dict:
    return {k: doc.get(k) for k in ("id", "name", "ext", "kind", "size", "sha256", "note", "createdAt")}


async def _usage(docs=None) -> dict:
    docs = docs if docs is not None else await db.list("media")
    status = limits.storage_status()
    return {"usedBytes": sum(d["size"] for d in docs), "quotaBytes": settings.MEDIA_QUOTA_MB * 1024 * 1024, "files": len(docs), "maxFiles": MAX_FILES,
            "maxFileBytes": settings.MAX_UPLOAD_MB * 1024 * 1024, "diskFreeMb": status["freeMb"], "allowed": sorted(KINDS)}


@router.get("")
async def list_media():
    docs = await db.list("media")
    return {"items": [_public(d) for d in docs], "usage": await _usage(docs)}


@router.post("", status_code=201)
async def upload_media(request: Request, file: UploadFile = File(...), note: str = Form("", max_length=300)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in KINDS:
        raise HTTPException(400, f"File type {ext or '(none)'} is not allowed. Allowed: {', '.join(sorted(KINDS))}.")
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.MAX_UPLOAD_MB} MB")
    docs = await db.list("media")
    used = sum(d["size"] for d in docs)
    quota = settings.MEDIA_QUOTA_MB * 1024 * 1024
    if len(docs) >= MAX_FILES:
        raise HTTPException(409, f"Media library is full ({MAX_FILES} files). Delete some files first.")
    if used >= quota:
        raise HTTPException(409, f"Media quota of {settings.MEDIA_QUOTA_MB} MB is used. Delete some files first.")
    limits.ensure_storage(min(limit, quota - used))
    media_id = uuid4().hex
    folder = _root() / media_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"file{ext}"
    digest, size, first = hashlib.sha256(), 0, True
    try:
        with target.open("wb") as handle:
            while chunk := await file.read(CHUNK):
                if first:
                    first = False
                    if not _magic_ok(ext, chunk[:4096]):
                        raise HTTPException(400, f"The file content does not look like a valid {ext} file.")
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"File exceeds {settings.MAX_UPLOAD_MB} MB")
                if used + size > quota:
                    raise HTTPException(409, f"This upload would exceed your {settings.MEDIA_QUOTA_MB} MB media quota.")
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise HTTPException(400, "The file is empty.")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    sha = digest.hexdigest()
    twin = next((d for d in docs if d.get("sha256") == sha), None)
    if twin:                                     # the same bytes are already stored: keep one copy
        shutil.rmtree(folder, ignore_errors=True)
        return {**_public(twin), "duplicate": True}
    doc = {"id": media_id, "name": _label(file.filename), "ext": ext, "kind": KINDS[ext], "size": size, "sha256": sha, "note": note.strip(),
           "path": str(target.relative_to(Path(settings.STORAGE_PATH))), "createdAt": datetime.now(timezone.utc).isoformat()}
    await db.save("media", doc)
    return {**_public(doc), "duplicate": False}


async def _find(media_id: str) -> dict:
    doc = await db.get("media", media_id) if re.fullmatch(r"[0-9a-f]{32}", media_id) else None
    if not doc:
        raise HTTPException(404, "File not found")
    return doc


@router.get("/{media_id}/file")
async def download_media(media_id: str):
    doc = await _find(media_id)
    path = Path(settings.STORAGE_PATH) / doc["path"]
    if not path.is_file() or _root() not in path.resolve().parents:
        raise HTTPException(404, "File not found")
    media_type = mimetypes.guess_type(doc["name"])[0] or "application/octet-stream"
    inline = doc["kind"] in INLINE_KINDS or doc["ext"] == ".pdf"
    return FileResponse(path, media_type=media_type, filename=doc["name"], content_disposition_type="inline" if inline else "attachment")


@router.delete("/{media_id}")
async def delete_media(media_id: str):
    doc = await _find(media_id)
    folder = (Path(settings.STORAGE_PATH) / doc["path"]).parent
    if _root() in folder.resolve().parents:
        shutil.rmtree(folder, ignore_errors=True)
    await db.delete("media", media_id)
    return {"deleted": media_id}
