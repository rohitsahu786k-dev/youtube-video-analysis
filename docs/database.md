# Durable storage

The runtime uses SQLite at STORAGE_PATH/intelligence.sqlite3 with WAL and parameterized SQL. Collections are videos, ingestions, playlists, trades, knowledge, proposals, strategies, datasets, backtests and usage. All records use explicit ids; evidence replacement for a video is transactional and retry-safe. No empty query returns demonstration records.

Frame paths and hashes link stored observations to files under STORAGE_PATH/evidence. Back up that directory together with SQLite. Restoring only the database loses original images. Temporary downloads/audio are removed after processing; retained frames consume disk space. The legacy DATABASE_URL/MongoDB configuration is not used.
