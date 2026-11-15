"""rekordbox integration.

Reads the rekordbox 6/7 encrypted master.db via pyrekordbox (read-only) and
links its collection to indexed tracks. Matching is by absolute path first,
then by (filename, file size) for songs rekordbox imported from a different
location (e.g. before a library move).
"""

from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

from .db import Database

log = logging.getLogger("cratekeeper.rekordbox")

# A real file location: POSIX absolute or Windows drive path. rekordbox also
# lists bundled/streaming content with other FolderPath shapes.
FILE_PATH_RE = re.compile(r"^(/|[A-Za-z]:[\\/])")


def nfc(s: str | None) -> str:
    return unicodedata.normalize("NFC", s or "")


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
        from pyrekordbox import MasterDatabase

        rb = MasterDatabase()  # never committed: read-only
        rows = []
        try:
            for c in rb.get_content().filter_by(rb_local_deleted=0):
                folder = c.FolderPath or ""
                if not FILE_PATH_RE.match(folder):
                    continue
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
        """Link tracks to collection entries. Paths are compared NFC-normalized:
        rekordbox stores NFC while macOS filenames are often NFD on disk.

        1. path      — same file rekordbox knows.
        2. name_size — same filename + size, rekordbox entry not claimed by a
                       path match: the file moved since rekordbox imported it.
        3. dup_of    — same filename + size as an entry another file already
                       claims: a duplicate copy of a song rekordbox has.
        """
        conn = self.db.conn
        rb_rows = conn.execute(
            "SELECT rb_id, folder_path, filename, file_size, analyzed, key, bpm"
            " FROM rekordbox_tracks ORDER BY rb_id"
        ).fetchall()
        rb_info = {r["rb_id"]: r for r in rb_rows}
        by_path: dict[str, str] = {}
        by_name_size: dict[tuple[str, int], list[str]] = defaultdict(list)
        for r in rb_rows:
            by_path.setdefault(nfc(r["folder_path"]), r["rb_id"])
            by_name_size[(nfc(r["filename"]), r["file_size"])].append(r["rb_id"])

        tracks = conn.execute(
            "SELECT id, path, filename, size FROM tracks ORDER BY missing, id"
        ).fetchall()
        links: dict[int, tuple[str, str]] = {}  # track id -> (rb_id, match)
        claimed: dict[str, int] = {}  # rb_id -> track id
        for t in tracks:
            rb_id = by_path.get(nfc(t["path"]))
            if rb_id is not None and rb_id not in claimed:
                links[t["id"]] = (rb_id, "path")
                claimed[rb_id] = t["id"]
        for t in tracks:
            if t["id"] in links:
                continue
            candidates = by_name_size.get((nfc(t["filename"]), t["size"]))
            if not candidates:
                continue
            free = [c for c in candidates if c not in claimed]
            if free:
                links[t["id"]] = (free[0], "name_size")
                claimed[free[0]] = t["id"]
            else:
                links[t["id"]] = (candidates[0], "dup_of")

        with conn:
            conn.execute(
                "UPDATE tracks SET rb_id=NULL, rb_match=NULL, rb_analyzed=NULL,"
                " rb_key=NULL, rb_bpm=NULL"
            )
            conn.execute("UPDATE rekordbox_tracks SET matched_track_id = NULL")
            conn.executemany(
                "UPDATE tracks SET rb_id=?, rb_match=?, rb_analyzed=?, rb_key=?, rb_bpm=?"
                " WHERE id=?",
                [
                    (rb_id, match, rb_info[rb_id]["analyzed"], rb_info[rb_id]["key"],
                     rb_info[rb_id]["bpm"], track_id)
                    for track_id, (rb_id, match) in links.items()
                ],
            )
            conn.executemany(
                "UPDATE rekordbox_tracks SET matched_track_id=? WHERE rb_id=?",
                [(track_id, rb_id) for rb_id, track_id in claimed.items()],
            )
        return len(links)
