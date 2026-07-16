"""SQLite storage.

One connection per thread (sqlite3 objects are not thread-safe); WAL mode so
the scanner thread and API reads don't block each other.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    partial_hash TEXT,
    full_hash TEXT,
    artist TEXT,
    title TEXT,
    album TEXT,
    genre TEXT,
    bpm TEXT,
    duration REAL,
    bitrate INTEGER,
    key TEXT,
    energy INTEGER,
    -- 'confirmed' = Mixed In Key signature found in tags,
    -- 'likely'    = key + energy tags present but no MIK signature,
    -- 'none'      = not analyzed.
    mik_status TEXT NOT NULL DEFAULT 'none',
    -- rekordbox link (refreshed on every rekordbox sync)
    rb_id TEXT,
    rb_match TEXT,               -- 'path' | 'name_size' | NULL
    rb_analyzed INTEGER,         -- rekordbox waveform/beatgrid analysis done
    rb_key TEXT,
    rb_bpm REAL,
    -- bookkeeping
    tag_error TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    missing INTEGER NOT NULL DEFAULT 0,
    -- duplicate groups (recomputed after every scan)
    dup_group INTEGER,           -- exact content duplicates
    meta_dup_group INTEGER       -- same artist+title, different file
);
CREATE INDEX IF NOT EXISTS idx_tracks_mik ON tracks(mik_status);
CREATE INDEX IF NOT EXISTS idx_tracks_rb ON tracks(rb_id);
CREATE INDEX IF NOT EXISTS idx_tracks_dup ON tracks(dup_group);
CREATE INDEX IF NOT EXISTS idx_tracks_metadup ON tracks(meta_dup_group);
CREATE INDEX IF NOT EXISTS idx_tracks_size ON tracks(size);

CREATE TABLE IF NOT EXISTS rekordbox_tracks (
    rb_id TEXT PRIMARY KEY,
    folder_path TEXT,
    filename TEXT,
    file_size INTEGER,
    title TEXT,
    artist TEXT,
    analyzed INTEGER,
    key TEXT,
    bpm REAL,
    matched_track_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rb_path ON rekordbox_tracks(folder_path);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        # Initialize schema once.
        with self.conn:
            self.conn.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, value),
            )
