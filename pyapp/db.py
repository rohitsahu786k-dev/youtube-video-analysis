"""Durable SQLite documents. Empty stores never fall back to demo records."""
import asyncio
import json
import sqlite3
from pathlib import Path
from pyapp.config import settings

class Database:
    def __init__(self, path=None):
        self.path = Path(path or Path(settings.STORAGE_PATH) / "intelligence.sqlite3")
        self.use_memory = False

    def _connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS documents (collection TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(collection, id))")
        return connection

    def _read(self, collection):
        connection = self._connection()
        try:
            return [json.loads(row[0]) for row in connection.execute("SELECT body FROM documents WHERE collection=? ORDER BY rowid DESC", (collection,))]
        finally:
            connection.close()

    def _write(self, collection, document):
        connection = self._connection()
        try:
            with connection:
                connection.execute("INSERT INTO documents VALUES (?, ?, ?) ON CONFLICT(collection,id) DO UPDATE SET body=excluded.body", (collection, document["id"], json.dumps(document, allow_nan=False)))
        finally:
            connection.close()

    async def connect(self):
        await self.list("videos")

    async def disconnect(self):
        pass

    async def list(self, collection):
        return await asyncio.to_thread(self._read, collection)

    async def save(self, collection, document):
        await asyncio.to_thread(self._write, collection, document)

    async def get(self, collection, identifier):
        return next((item for item in await self.list(collection) if item["id"] == identifier), None)

    def _replace_evidence(self, video_id, trades, knowledge):
        connection = self._connection()
        try:
            with connection:
                for collection, items in (("trades", trades), ("knowledge", knowledge)):
                    connection.execute("DELETE FROM documents WHERE collection=? AND json_extract(body, '$.videoId')=?", (collection, video_id))
                    for item in items:
                        connection.execute("INSERT INTO documents VALUES (?, ?, ?)", (collection, item["id"], json.dumps(item, allow_nan=False)))
        finally:
            connection.close()

    async def replace_evidence(self, video_id, trades, knowledge):
        await asyncio.to_thread(self._replace_evidence, video_id, trades, knowledge)

    async def get_videos(self):
        return await self.list("videos")

    async def get_video(self, identifier):
        return next((v for v in await self.get_videos() if identifier in (v["id"], v.get("youtubeId"))), None)

    async def save_video(self, document):
        await self.save("videos", document)

    async def get_trades(self, video_id=None):
        return [t for t in await self.list("trades") if video_id is None or t.get("videoId") == video_id]

    async def get_trade(self, identifier):
        return await self.get("trades", identifier)

    async def save_trade(self, document):
        await self.save("trades", document)

    async def get_playlists(self):
        playlists = await self.list("playlists")
        videos, trades = await self.get_videos(), await self.get_trades()
        for playlist in playlists:
            members = [v for v in videos if playlist["id"] in v.get("playlistIds", [])]
            ids = {v["id"] for v in members}
            completed = [t for t in trades if t.get("videoId") in ids and t.get("validation") == "VALID" and t.get("result") in ("WIN", "LOSS")]
            playlist.update(analyzedCount=sum(v["status"] == "COMPLETED" for v in members), winRate=round(100 * sum(t["result"] == "WIN" for t in completed) / len(completed), 1) if completed else None)
        return playlists

    async def get_jobs(self):
        return [{**v, "video": v["title"], "stage": v["status"], "duration": v.get("updatedAt", "Queued")} for v in await self.get_videos()]

db = Database()
