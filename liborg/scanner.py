"""Library scanner.

Walks the library, extracts tags with mutagen, detects Mixed In Key status,
and keeps the tracks table in sync with the filesystem. Unchanged files
(same size + mtime) are skipped, so rescans of a large library are fast.

Content hashing is two-tier: every file gets a cheap partial hash
(first 64 KiB + last 64 KiB + size); a full SHA-256 is computed only for
files whose (size, partial_hash) collides with another — the only ones that
can be exact duplicates.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from . import mik
from .config import Config
from .db import Database

log = logging.getLogger("liborg.scanner")

PARTIAL_CHUNK = 65536


@dataclass
class ScanProgress:
    state: str = "idle"  # idle | scanning | hashing | grouping
    total: int = 0
    done: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: int = 0
    started_at: float | None = None
    finished_at: float | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def partial_hash(path: Path, size: int) -> str:
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(PARTIAL_CHUNK))
        if size > PARTIAL_CHUNK * 2:
            fh.seek(-PARTIAL_CHUNK, os.SEEK_END)
            h.update(fh.read(PARTIAL_CHUNK))
    return h.hexdigest()


def full_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _first(tags, *names) -> str | None:
    if tags is None:
        return None
    for name in names:
        try:
            vals = tags.get(name)
        except (KeyError, ValueError, TypeError):
            continue
        if vals:
            text = str(vals[0]) if isinstance(vals, list) else str(vals)
            text = text.strip()
            if text:
                return text
    return None


def read_tags(path: Path) -> dict:
    """Extract the metadata we care about. Never raises."""
    out: dict = {
        "artist": None, "title": None, "album": None, "genre": None,
        "bpm": None, "duration": None, "bitrate": None,
        "key": None, "energy": None, "mik_status": "none", "tag_error": None,
    }
    try:
        audio = mutagen.File(path)
    except Exception as exc:  # corrupt file, bad header, ...
        out["tag_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return out
    if audio is None:
        out["tag_error"] = "unrecognized format"
        return out

    if audio.info is not None:
        out["duration"] = round(getattr(audio.info, "length", 0) or 0, 2) or None
        out["bitrate"] = getattr(audio.info, "bitrate", None) or None

    tags = audio.tags
    out["artist"] = _first(tags, "TPE1", "artist", "ARTIST", "\xa9ART")
    out["title"] = _first(tags, "TIT2", "title", "TITLE", "\xa9nam")
    out["album"] = _first(tags, "TALB", "album", "ALBUM", "\xa9alb")
    out["genre"] = _first(tags, "TCON", "genre", "GENRE", "\xa9gen")
    bpm = _first(tags, "TBPM", "bpm", "BPM", "tmpo")
    if bpm and re.match(r"^\d+(\.\d+)?$", bpm):
        out["bpm"] = bpm

    result = mik.detect(tags)
    out["mik_status"] = result.status
    out["key"] = result.key
    out["energy"] = result.energy
    return out


NOISE_RE = re.compile(
    r"\(original mix\)|\(extended mix\)|\[.*?\]|original mix|extended mix", re.IGNORECASE
)


def normalize_meta(artist: str | None, title: str | None) -> str | None:
    """Key used to group metadata duplicates (same song, different file)."""
    if not artist or not title:
        return None
    def norm(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        s = NOISE_RE.sub("", s.lower())
        return re.sub(r"[^a-z0-9]+", "", s)
    a, t = norm(artist), norm(title)
    if not a or not t:
        return None
    return f"{a}|{t}"


class Scanner:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.progress = ScanProgress()

    # ---- single file ----------------------------------------------------

    def scan_file(self, path: Path) -> str | None:
        """Index or refresh one file. Returns 'added'/'updated'/None."""
        try:
            st = path.stat()
        except OSError:
            return None
        conn = self.db.conn
        row = conn.execute(
            "SELECT id, size, mtime FROM tracks WHERE path = ?", (str(path),)
        ).fetchone()
        now = time.time()
        if row and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1:
            conn.execute(
                "UPDATE tracks SET last_seen = ?, missing = 0 WHERE id = ?", (now, row["id"])
            )
            return None

        tags = read_tags(path)
        try:
            phash = partial_hash(path, st.st_size)
        except OSError as exc:
            log.warning("hash failed for %s: %s", path, exc)
            phash = None

        values = dict(
            tags,
            path=str(path), filename=path.name, ext=path.suffix.lower(),
            size=st.st_size, mtime=st.st_mtime, partial_hash=phash,
            last_seen=now,
        )
        if row:
            conn.execute(
                """UPDATE tracks SET filename=:filename, ext=:ext, size=:size, mtime=:mtime,
                   partial_hash=:partial_hash, full_hash=NULL, artist=:artist, title=:title,
                   album=:album, genre=:genre, bpm=:bpm, duration=:duration, bitrate=:bitrate,
                   key=:key, energy=:energy, mik_status=:mik_status, tag_error=:tag_error,
                   last_seen=:last_seen, missing=0 WHERE path=:path""",
                values,
            )
            return "updated"
        conn.execute(
            """INSERT INTO tracks (path, filename, ext, size, mtime, partial_hash, artist,
               title, album, genre, bpm, duration, bitrate, key, energy, mik_status,
               tag_error, first_seen, last_seen)
               VALUES (:path, :filename, :ext, :size, :mtime, :partial_hash, :artist, :title,
               :album, :genre, :bpm, :duration, :bitrate, :key, :energy, :mik_status,
               :tag_error, :last_seen, :last_seen)""",
            values,
        )
        return "added"

    def remove_file(self, path: Path) -> None:
        with self.db.conn:
            self.db.conn.execute("UPDATE tracks SET missing = 1 WHERE path = ?", (str(path),))

    # ---- full scan -------------------------------------------------------

    def iter_library(self):
        exts = self.config.extensions
        ignore = self.config.ignore_dirs
        for dirpath, dirnames, filenames in os.walk(self.config.library_path):
            dirnames[:] = [d for d in dirnames if d not in ignore and not d.startswith(".")]
            for name in filenames:
                if os.path.splitext(name)[1].lower() in exts:
                    yield Path(dirpath) / name

    def scan(self) -> ScanProgress:
        p = self.progress = ScanProgress(state="scanning", started_at=time.time())
        files = list(self.iter_library())
        p.total = len(files)
        seen = set()
        conn = self.db.conn
        for i, path in enumerate(files):
            seen.add(str(path))
            try:
                result = self.scan_file(path)
                if result == "added":
                    p.added += 1
                elif result == "updated":
                    p.updated += 1
            except Exception:
                p.errors += 1
                log.exception("failed to scan %s", path)
            p.done = i + 1
            if p.done % 200 == 0:
                conn.commit()
        conn.commit()

        # Mark rows whose file disappeared (moves show up as add + missing).
        for row in conn.execute("SELECT id, path FROM tracks WHERE missing = 0"):
            if row["path"] not in seen:
                conn.execute("UPDATE tracks SET missing = 1 WHERE id = ?", (row["id"],))
                p.removed += 1
        conn.commit()

        self.compute_duplicates()
        p.state = "idle"
        p.finished_at = time.time()
        self.db.set_meta("last_scan", str(p.finished_at))
        log.info(
            "scan done: %d files, +%d ~%d -%d, %d errors",
            p.total, p.added, p.updated, p.removed, p.errors,
        )
        return p

    # ---- duplicates ------------------------------------------------------

    def compute_duplicates(self) -> None:
        self.progress.state = "hashing"
        conn = self.db.conn

        # Full-hash only the collision candidates.
        candidates = conn.execute(
            """SELECT id, path, full_hash FROM tracks WHERE missing = 0 AND partial_hash IN (
                 SELECT partial_hash FROM tracks WHERE missing = 0 AND partial_hash IS NOT NULL
                 GROUP BY partial_hash HAVING COUNT(*) > 1)"""
        ).fetchall()
        for row in candidates:
            if row["full_hash"]:
                continue
            try:
                fh = full_hash(Path(row["path"]))
            except OSError:
                continue
            conn.execute("UPDATE tracks SET full_hash = ? WHERE id = ?", (fh, row["id"]))
        conn.commit()

        self.progress.state = "grouping"
        conn.execute("UPDATE tracks SET dup_group = NULL, meta_dup_group = NULL")

        group = 0
        for row in conn.execute(
            """SELECT full_hash FROM tracks WHERE missing = 0 AND full_hash IS NOT NULL
               GROUP BY full_hash HAVING COUNT(*) > 1"""
        ).fetchall():
            group += 1
            conn.execute(
                "UPDATE tracks SET dup_group = ? WHERE full_hash = ? AND missing = 0",
                (group, row["full_hash"]),
            )

        # Metadata duplicates: same normalized artist+title, not already an
        # exact-content group of one file... include everything so different
        # formats/bitrates of the same song surface.
        buckets: dict[str, list[int]] = {}
        for row in conn.execute(
            "SELECT id, artist, title FROM tracks WHERE missing = 0"
        ).fetchall():
            key = normalize_meta(row["artist"], row["title"])
            if key:
                buckets.setdefault(key, []).append(row["id"])
        mgroup = 0
        for ids in buckets.values():
            if len(ids) > 1:
                mgroup += 1
                conn.executemany(
                    "UPDATE tracks SET meta_dup_group = ? WHERE id = ?",
                    [(mgroup, i) for i in ids],
                )
        conn.commit()
