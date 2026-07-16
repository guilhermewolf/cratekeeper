"""rekordbox integration.

Reads the rekordbox 6/7 encrypted master.db via pyrekordbox (read-only) and
links its collection to indexed tracks. Matching is by absolute path first,
then by (filename, file size) for songs rekordbox imported from a different
location (e.g. before a library move).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .db import Database

log = logging.getLogger("cratekeeper.rekordbox")


def _rb_db_path() -> Path | None:
    candidates = [
        Path.home() / "Library" / "Pioneer" / "rekordbox" / "master.db",
        Path.home() / "AppData" / "Roaming" / "Pioneer" / "rekordbox" / "master.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


class RekordboxSync:
    def __init__(self, db: Database):
        self.db = db
        self.last_mtime: float | None = None
        self.available: bool | None = None
        self.error: str | None = None

    def needs_sync(self) -> bool:
        path = _rb_db_path()
        if path is None:
            return False
        mtime = path.stat().st_mtime
        return self.last_mtime is None or mtime > self.last_mtime

    def sync(self, force: bool = False) -> dict:
        """Refresh the rekordbox mirror table and re-link tracks."""
        path = _rb_db_path()
        if path is None:
            self.available = False
            self.error = "rekordbox master.db not found"
            return {"ok": False, "error": self.error}
        if not force and not self.needs_sync():
            return {"ok": True, "skipped": True}

        try:
            rows = self._read_collection()
        except Exception as exc:
            self.available = False
            self.error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("rekordbox read failed")
            return {"ok": False, "error": self.error}

        self.available = True
        self.error = None
        self.last_mtime = path.stat().st_mtime

        conn = self.db.conn
        with conn:
            conn.execute("DELETE FROM rekordbox_tracks")
            conn.executemany(
                """INSERT OR REPLACE INTO rekordbox_tracks
                   (rb_id, folder_path, filename, file_size, title, artist, analyzed, key, bpm)
                   VALUES (:rb_id, :folder_path, :filename, :file_size, :title, :artist,
                           :analyzed, :key, :bpm)""",
                rows,
            )
        matched = self._link_tracks()
        self.db.set_meta("last_rekordbox_sync", str(time.time()))
        log.info("rekordbox sync: %d collection entries, %d matched", len(rows), matched)
        return {"ok": True, "collection": len(rows), "matched": matched}

    def _read_collection(self) -> list[dict]:
        from pyrekordbox import Rekordbox6Database

        rb = Rekordbox6Database()
        rows = []
        try:
            for c in rb.get_content():
                folder = c.FolderPath or ""
                key = None
                try:
                    if c.Key is not None:
                        key = c.Key.ScaleName
                except Exception:
                    pass
                artist = None
                try:
                    if c.Artist is not None:
                        artist = c.Artist.Name
                except Exception:
                    pass
                bpm = None
                if c.BPM:
                    bpm = c.BPM / 100.0
                rows.append({
                    "rb_id": str(c.ID),
                    "folder_path": folder,
                    "filename": os.path.basename(folder),
                    "file_size": c.FileSize,
                    "title": c.Title,
                    "artist": artist,
                    "analyzed": 1 if getattr(c, "Analysed", 0) else 0,
                    "key": key,
                    "bpm": bpm,
                })
        finally:
            try:
                rb.close()
            except Exception:
                pass
        return rows

    def _link_tracks(self) -> int:
        conn = self.db.conn
        with conn:
            conn.execute(
                "UPDATE tracks SET rb_id=NULL, rb_match=NULL, rb_analyzed=NULL,"
                " rb_key=NULL, rb_bpm=NULL"
            )
            conn.execute("UPDATE rekordbox_tracks SET matched_track_id = NULL")

            # 1. exact path match
            conn.execute(
                """UPDATE tracks SET
                     rb_id = (SELECT r.rb_id FROM rekordbox_tracks r WHERE r.folder_path = tracks.path),
                     rb_match = 'path'
                   WHERE EXISTS (SELECT 1 FROM rekordbox_tracks r WHERE r.folder_path = tracks.path)"""
            )
            # 2. filename + size match for the rest
            conn.execute(
                """UPDATE tracks SET
                     rb_id = (SELECT r.rb_id FROM rekordbox_tracks r
                              WHERE r.filename = tracks.filename AND r.file_size = tracks.size
                              LIMIT 1),
                     rb_match = 'name_size'
                   WHERE rb_id IS NULL AND EXISTS (
                     SELECT 1 FROM rekordbox_tracks r
                     WHERE r.filename = tracks.filename AND r.file_size = tracks.size)"""
            )
            # copy analysis info onto matched tracks
            conn.execute(
                """UPDATE tracks SET
                     rb_analyzed = (SELECT r.analyzed FROM rekordbox_tracks r WHERE r.rb_id = tracks.rb_id),
                     rb_key = (SELECT r.key FROM rekordbox_tracks r WHERE r.rb_id = tracks.rb_id),
                     rb_bpm = (SELECT r.bpm FROM rekordbox_tracks r WHERE r.rb_id = tracks.rb_id)
                   WHERE rb_id IS NOT NULL"""
            )
            conn.execute(
                """UPDATE rekordbox_tracks SET matched_track_id =
                     (SELECT t.id FROM tracks t WHERE t.rb_id = rekordbox_tracks.rb_id LIMIT 1)"""
            )
        row = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM tracks WHERE rb_id IS NOT NULL"
        ).fetchone()
        return row["n"]
