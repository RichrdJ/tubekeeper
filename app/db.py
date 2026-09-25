import os
import sqlite3
import threading
from contextlib import contextmanager

DATA_DIR = os.environ.get("DATA_DIR", "/config")
DB_PATH = os.path.join(DATA_DIR, "tubekeeper.db")
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    url              TEXT NOT NULL,
    channel_id       TEXT,
    kind             TEXT NOT NULL DEFAULT 'video',   -- video | audio
    quality          TEXT NOT NULL DEFAULT '1080',    -- best | 2160 | 1440 | 1080 | 720 | 480 | 360
    audio_format     TEXT NOT NULL DEFAULT 'm4a',
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    only_after       TEXT,                            -- YYYYMMDD
    keep_last        INTEGER,                         -- keep only the N newest downloads
    sub_langs        TEXT NOT NULL DEFAULT '',
    backfill         INTEGER NOT NULL DEFAULT 0,      -- download existing videos on first index
    enabled          INTEGER NOT NULL DEFAULT 1,
    redownload_missing INTEGER NOT NULL DEFAULT 0,    -- re-queue files deleted by hand
    layout           TEXT NOT NULL DEFAULT 'flat',    -- flat | series (Plex/Jellyfin TV show)
    lang             TEXT NOT NULL DEFAULT '',        -- title language; '' = detect automatically
    lang_detected    TEXT,
    last_checked     TEXT,
    last_error       TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    video_id      TEXT NOT NULL,
    title         TEXT,
    url           TEXT NOT NULL,
    upload_date   TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | downloading | done | error | skipped | deleted
    filepath      TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    downloaded_at TEXT,
    UNIQUE (source_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init():
    os.makedirs(DATA_DIR, exist_ok=True)
    with tx() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        columns = {r["name"] for r in c.execute("PRAGMA table_info(sources)")}
        if "lang" not in columns:
            c.execute("ALTER TABLE sources ADD COLUMN lang TEXT NOT NULL DEFAULT ''")
        if "channel_id" not in columns:
            c.execute("ALTER TABLE sources ADD COLUMN channel_id TEXT")
        if "layout" not in columns:
            c.execute("ALTER TABLE sources ADD COLUMN layout TEXT NOT NULL DEFAULT 'flat'")
        if "redownload_missing" not in columns:
            c.execute("ALTER TABLE sources ADD COLUMN redownload_missing INTEGER NOT NULL DEFAULT 0")
        if "lang_detected" not in columns:
            c.execute("ALTER TABLE sources ADD COLUMN lang_detected TEXT")
        # Downloads interrupted by a restart go back into the queue
        c.execute("UPDATE media SET status = 'pending' WHERE status = 'downloading'")


def query(sql, args=()):
    with tx() as c:
        return c.execute(sql, args).fetchall()


def one(sql, args=()):
    with tx() as c:
        return c.execute(sql, args).fetchone()


def execute(sql, args=()):
    with tx() as c:
        return c.execute(sql, args)
