"""Durable SQLite document store with per-user namespaces.

Each document lives in `collection`; the active user (a ContextVar set by the auth layer or the worker) is
folded into the collection name so one user can never read another's strategies, journals or chats.
Global collections (users, sessions) are never namespaced. Empty stores never fall back to demo records.
"""
import asyncio
import json
import sqlite3
import contextvars
from pathlib import Path
from pyapp.config import settings

owner_var: contextvars.ContextVar[str] = contextvars.ContextVar("owner", default="")
GLOBAL_COLLECTIONS = {"users", "auth_events"}
SEP = "::"


def scoped(collection: str, owner: str | None = None) -> str:
    if collection in GLOBAL_COLLECTIONS:
        return collection
    owner = owner_var.get() if owner is None else owner
    return f"{owner}{SEP}{collection}" if owner else collection


class Database:
    def __init__(self, path=None):
        self.path = Path(path or Path(settings.STORAGE_PATH) / "intelligence.sqlite3")
        self.use_memory = False
        self._ready = False

    def _connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-16000")             # 16 MB page cache per connection
        connection.execute("PRAGMA journal_size_limit=67108864")   # the WAL file shrinks back to 64 MB after a checkpoint
        if not self._ready:
            connection.execute("CREATE TABLE IF NOT EXISTS documents (collection TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(collection, id))")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_documents_video ON documents(collection, json_extract(body, '$.videoId'))")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_documents_parent ON documents(collection, json_extract(body, '$.parentId'))")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(collection, json_extract(body, '$.status'))")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_documents_day ON documents(collection, json_extract(body, '$.day'))")
            self._ready = True
        return connection

    # -- synchronous primitives (run in threads) ------------------------------------------------------
    def _read(self, collection, limit=None):
        connection = self._connection()
        try:
            sql = "SELECT body FROM documents WHERE collection=? ORDER BY rowid DESC"
            args = [collection]
            if limit:
                sql += " LIMIT ?"
                args.append(limit)
            return [json.loads(row[0]) for row in connection.execute(sql, args)]
        finally:
            connection.close()

    def _read_one(self, collection, identifier):
        connection = self._connection()
        try:
            row = connection.execute("SELECT body FROM documents WHERE collection=? AND id=?", (collection, identifier)).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            connection.close()

    def _write(self, collection, document):
        connection = self._connection()
        try:
            with connection:
                connection.execute("INSERT INTO documents VALUES (?, ?, ?) ON CONFLICT(collection,id) DO UPDATE SET body=excluded.body",
                                   (collection, document["id"], json.dumps(document, allow_nan=False)))
        finally:
            connection.close()

    def _delete(self, collection, identifier):
        connection = self._connection()
        try:
            with connection:
                return connection.execute("DELETE FROM documents WHERE collection=? AND id=?", (collection, identifier)).rowcount
        finally:
            connection.close()

    def _query(self, collection, where, limit):
        connection = self._connection()
        try:
            sql, args = "SELECT body FROM documents WHERE collection=?", [collection]
            for field, value in (where or {}).items():
                if not field.replace("_", "").isalnum():
                    raise ValueError("Invalid field name")
                sql += f" AND json_extract(body, '$.{field}') = ?"
                args.append(value)
            sql += " ORDER BY rowid DESC"
            if limit:
                sql += " LIMIT ?"
                args.append(limit)
            return [json.loads(row[0]) for row in connection.execute(sql, args)]
        finally:
            connection.close()

    def _count(self, collection):
        connection = self._connection()
        try:
            return connection.execute("SELECT COUNT(*) FROM documents WHERE collection=?", (collection,)).fetchone()[0]
        finally:
            connection.close()

    def _maintain(self, retention_days):
        """Housekeeping: drop finished jobs past retention, checkpoint/truncate the WAL, refresh query-planner stats. Returns a small report."""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        connection = self._connection()
        try:
            with connection:
                removed = connection.execute(
                    "DELETE FROM documents WHERE (collection='jobs' OR collection LIKE '%::jobs') AND json_extract(body, '$.status') IN ('COMPLETED','FAILED','CANCELLED') "
                    "AND COALESCE(json_extract(body, '$.finishedAt'), json_extract(body, '$.createdAt'), '') < ?", (cutoff,)).rowcount
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA optimize")
            return {"jobsRemoved": removed, "documents": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]}
        finally:
            connection.close()

    def _check(self, full=False):
        connection = self._connection()
        try:
            return [row[0] for row in connection.execute("PRAGMA integrity_check" if full else "PRAGMA quick_check")][:5]
        finally:
            connection.close()

    def _owners(self):
        connection = self._connection()
        try:
            names = [row[0] for row in connection.execute("SELECT DISTINCT collection FROM documents")]
        finally:
            connection.close()
        owners = {""}
        for name in names:
            if SEP in name:
                owners.add(name.split(SEP, 1)[0])
        return sorted(owners)

    def _replace_evidence(self, video_id, trades, knowledge):
        connection = self._connection()
        try:
            with connection:
                for collection, items in ((scoped("trades"), trades), (scoped("knowledge"), knowledge)):
                    connection.execute("DELETE FROM documents WHERE collection=? AND json_extract(body, '$.videoId')=?", (collection, video_id))
                    for item in items:
                        connection.execute("INSERT INTO documents VALUES (?, ?, ?)", (collection, item["id"], json.dumps(item, allow_nan=False)))
        finally:
            connection.close()

    def _replace_for_video(self, collection, video_id, items):
        connection = self._connection()
        try:
            with connection:
                connection.execute("DELETE FROM documents WHERE collection=? AND json_extract(body, '$.videoId')=?", (collection, video_id))
                for item in items:
                    connection.execute("INSERT INTO documents VALUES (?, ?, ?) ON CONFLICT(collection,id) DO UPDATE SET body=excluded.body",
                                       (collection, item["id"], json.dumps(item, allow_nan=False)))
        finally:
            connection.close()

    def _write_many(self, collection, documents):
        connection = self._connection()
        try:
            with connection:
                connection.executemany("INSERT INTO documents VALUES (?, ?, ?) ON CONFLICT(collection,id) DO UPDATE SET body=excluded.body",
                                       [(collection, d["id"], json.dumps(d, allow_nan=False)) for d in documents])
        finally:
            connection.close()

    # -- public async API ----------------------------------------------------------------------------
    async def connect(self):
        await asyncio.to_thread(self._count, "videos")

    async def disconnect(self):
        pass

    async def list(self, collection, limit=None):
        return await asyncio.to_thread(self._read, scoped(collection), limit)

    async def get(self, collection, identifier):
        return await asyncio.to_thread(self._read_one, scoped(collection), identifier)

    async def save(self, collection, document):
        await asyncio.to_thread(self._write, scoped(collection), document)

    async def save_many(self, collection, documents):
        if documents:
            await asyncio.to_thread(self._write_many, scoped(collection), documents)

    async def delete(self, collection, identifier):
        return await asyncio.to_thread(self._delete, scoped(collection), identifier)

    async def query(self, collection, where=None, limit=None):
        return await asyncio.to_thread(self._query, scoped(collection), where, limit)

    async def count(self, collection):
        return await asyncio.to_thread(self._count, scoped(collection))

    async def owners(self):
        return await asyncio.to_thread(self._owners)

    async def maintain(self, retention_days=30):
        return await asyncio.to_thread(self._maintain, retention_days)

    async def check(self, full=False):
        return await asyncio.to_thread(self._check, full)

    async def replace_evidence(self, video_id, trades, knowledge):
        await asyncio.to_thread(self._replace_evidence, video_id, trades, knowledge)

    async def replace_for_video(self, collection, video_id, items):
        await asyncio.to_thread(self._replace_for_video, scoped(collection), video_id, items)

    # -- domain helpers (kept for existing routes/tests) --------------------------------------------
    async def get_videos(self):
        return await self.list("videos")

    async def get_video(self, identifier):
        direct = await self.get("videos", identifier)
        if direct:
            return direct
        found = await self.query("videos", {"youtubeId": identifier}, 1)
        return found[0] if found else None

    async def save_video(self, document):
        await self.save("videos", document)

    async def get_trades(self, video_id=None):
        return await (self.query("trades", {"videoId": video_id}) if video_id else self.list("trades"))

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


class _Proxy:
    """Indirection so tests (and the app) can swap the backing database in one place."""
    def __init__(self, impl):
        object.__setattr__(self, "_impl", impl)

    def use(self, impl):
        object.__setattr__(self, "_impl", impl)

    def __getattr__(self, name):
        return getattr(self._impl, name)


db = _Proxy(Database())
