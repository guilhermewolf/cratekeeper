"""FastAPI application: JSON API + static frontend."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Config, load_config
from .db import Database
from .rekordbox import RekordboxSync
from .scanner import Scanner
from .watcher import LibraryWatcher

log = logging.getLogger("liborg.api")

SORTABLE = {
    "filename", "artist", "title", "album", "genre", "bpm", "key", "energy",
    "size", "mtime", "duration", "mik_status", "ext", "first_seen",
}


class AppState:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.db_path)
        self.scanner = Scanner(config, self.db)
        self.rekordbox = RekordboxSync(self.db)
        self.watcher = LibraryWatcher(config, self.db)
        self._scan_lock = threading.Lock()

    def scan_async(self) -> bool:
        """Kick off a scan unless one is already running."""
        if not self._scan_lock.acquire(blocking=False):
            return False

        def run():
            try:
                self.scanner.scan()
                self.rekordbox.sync(force=True)
            except Exception:
                log.exception("background scan failed")
                self.scanner.progress.state = "idle"
            finally:
                self._scan_lock.release()

        threading.Thread(target=run, name="liborg-scan", daemon=True).start()
        return True

    def start_background(self) -> None:
        """Watcher + periodic full rescan + rekordbox polling."""
        self.watcher.start()

        def loop():
            last_full = 0.0
            while True:
                now = time.time()
                if now - last_full >= self.config.rescan_interval:
                    last_full = now
                    self.scan_async()
                try:
                    self.rekordbox.sync()
                except Exception:
                    log.exception("rekordbox poll failed")
                time.sleep(self.config.rekordbox_poll_interval)

        threading.Thread(target=loop, name="liborg-periodic", daemon=True).start()


def create_app(config: Config | None = None, background: bool = True) -> FastAPI:
    from contextlib import asynccontextmanager

    config = config or load_config()
    state = AppState(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if background:
            state.scan_async()
            state.start_background()
        yield

    app = FastAPI(title="liborg", version=__version__, lifespan=lifespan)
    app.state.liborg = state

    # ---- stats -----------------------------------------------------------

    @app.get("/api/stats")
    def stats():
        conn = state.db.conn
        def one(sql: str) -> int:
            return conn.execute(sql).fetchone()[0]
        total = one("SELECT COUNT(*) FROM tracks WHERE missing = 0")
        return {
            "version": __version__,
            "library_path": str(config.library_path),
            "total": total,
            "missing": one("SELECT COUNT(*) FROM tracks WHERE missing = 1"),
            "mik": {
                "confirmed": one("SELECT COUNT(*) FROM tracks WHERE missing=0 AND mik_status='confirmed'"),
                "likely": one("SELECT COUNT(*) FROM tracks WHERE missing=0 AND mik_status='likely'"),
                "none": one("SELECT COUNT(*) FROM tracks WHERE missing=0 AND mik_status='none'"),
            },
            "rekordbox": {
                "available": state.rekordbox.available,
                "error": state.rekordbox.error,
                "collection": one("SELECT COUNT(*) FROM rekordbox_tracks"),
                "in_rekordbox": one("SELECT COUNT(*) FROM tracks WHERE missing=0 AND rb_id IS NOT NULL"),
                "not_in_rekordbox": one("SELECT COUNT(*) FROM tracks WHERE missing=0 AND rb_id IS NULL"),
                "not_analyzed": one(
                    "SELECT COUNT(*) FROM tracks WHERE missing=0 AND rb_id IS NOT NULL AND rb_analyzed=0"),
                "only_in_rekordbox": one(
                    "SELECT COUNT(*) FROM rekordbox_tracks WHERE matched_track_id IS NULL"),
            },
            "duplicates": {
                "exact_groups": one("SELECT COUNT(DISTINCT dup_group) FROM tracks WHERE dup_group IS NOT NULL"),
                "exact_files": one("SELECT COUNT(*) FROM tracks WHERE dup_group IS NOT NULL"),
                "meta_groups": one(
                    "SELECT COUNT(DISTINCT meta_dup_group) FROM tracks WHERE meta_dup_group IS NOT NULL"),
                "meta_files": one("SELECT COUNT(*) FROM tracks WHERE meta_dup_group IS NOT NULL"),
                "wasted_bytes": conn.execute(
                    """SELECT COALESCE(SUM(size),0) - COALESCE(SUM(m),0) FROM (
                         SELECT SUM(size) AS size, MAX(size) AS m FROM tracks
                         WHERE dup_group IS NOT NULL GROUP BY dup_group)"""
                ).fetchone()[0],
            },
            "tag_errors": one("SELECT COUNT(*) FROM tracks WHERE missing=0 AND tag_error IS NOT NULL"),
            "last_scan": state.db.get_meta("last_scan"),
            "last_rekordbox_sync": state.db.get_meta("last_rekordbox_sync"),
            "scan": state.scanner.progress.as_dict(),
        }

    # ---- tracks ------------------------------------------------------------

    @app.get("/api/tracks")
    def tracks(
        search: str = "",
        mik: str = Query("", pattern="^(|confirmed|likely|none|analyzed)$"),
        rekordbox: str = Query("", pattern="^(|in|out|unanalyzed)$"),
        dupes: str = Query("", pattern="^(|exact|meta)$"),
        errors: bool = False,
        sort: str = "artist",
        order: str = Query("asc", pattern="^(asc|desc)$"),
        page: int = Query(1, ge=1),
        per_page: int = Query(100, ge=1, le=500),
    ):
        where = ["missing = 0"]
        params: list = []
        if search:
            where.append("(filename LIKE ? OR artist LIKE ? OR title LIKE ? OR album LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like, like]
        if mik == "analyzed":
            where.append("mik_status IN ('confirmed', 'likely')")
        elif mik:
            where.append("mik_status = ?")
            params.append(mik)
        if rekordbox == "in":
            where.append("rb_id IS NOT NULL")
        elif rekordbox == "out":
            where.append("rb_id IS NULL")
        elif rekordbox == "unanalyzed":
            where.append("rb_id IS NOT NULL AND rb_analyzed = 0")
        if dupes == "exact":
            where.append("dup_group IS NOT NULL")
        elif dupes == "meta":
            where.append("meta_dup_group IS NOT NULL")
        if errors:
            where.append("tag_error IS NOT NULL")

        sort_col = sort if sort in SORTABLE else "artist"
        direction = "DESC" if order == "desc" else "ASC"
        clause = " AND ".join(where)
        if dupes == "exact":
            order_by = f"dup_group {direction}, size DESC"
        elif dupes == "meta":
            order_by = f"meta_dup_group {direction}, size DESC"
        else:
            order_by = f"{sort_col} COLLATE NOCASE {direction} NULLS LAST"

        conn = state.db.conn
        total = conn.execute(f"SELECT COUNT(*) FROM tracks WHERE {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT id, path, filename, ext, size, artist, title, album, genre, bpm,
                duration, bitrate, key, energy, mik_status, rb_id, rb_match, rb_analyzed,
                rb_key, rb_bpm, tag_error, dup_group, meta_dup_group
                FROM tracks WHERE {clause} ORDER BY {order_by} LIMIT ? OFFSET ?""",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "tracks": [dict(r) for r in rows],
        }

    @app.get("/api/tracks/{track_id}")
    def track_detail(track_id: int):
        conn = state.db.conn
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if not row:
            raise HTTPException(404, "track not found")
        track = dict(row)

        def siblings(col: str) -> list[dict]:
            if track[col] is None:
                return []
            rows = conn.execute(
                f"""SELECT id, path, filename, ext, size, bitrate, duration, mik_status,
                    rb_id, rb_analyzed FROM tracks
                    WHERE {col} = ? AND id != ? AND missing = 0 ORDER BY size DESC""",
                (track[col], track_id),
            ).fetchall()
            return [dict(r) for r in rows]

        track["exact_duplicates"] = siblings("dup_group")
        track["same_song"] = siblings("meta_dup_group")
        if track["rb_id"]:
            rb = conn.execute(
                "SELECT * FROM rekordbox_tracks WHERE rb_id = ?", (track["rb_id"],)
            ).fetchone()
            track["rekordbox"] = dict(rb) if rb else None
        else:
            track["rekordbox"] = None
        return track

    # ---- duplicates --------------------------------------------------------

    @app.get("/api/duplicates")
    def duplicates(kind: str = Query("exact", pattern="^(exact|meta)$")):
        col = "dup_group" if kind == "exact" else "meta_dup_group"
        rows = state.db.conn.execute(
            f"""SELECT id, path, filename, ext, size, artist, title, bitrate, duration,
                mik_status, rb_id, {col} AS grp
                FROM tracks WHERE {col} IS NOT NULL AND missing = 0
                ORDER BY {col}, size DESC"""
        ).fetchall()
        groups: dict[int, list] = {}
        for r in rows:
            groups.setdefault(r["grp"], []).append(dict(r))
        return {"kind": kind, "groups": list(groups.values())}

    # ---- rekordbox-only tracks ----------------------------------------------

    @app.get("/api/rekordbox/orphans")
    def rekordbox_orphans():
        """Collection entries with no matching file in the library."""
        rows = state.db.conn.execute(
            """SELECT rb_id, folder_path, title, artist, analyzed FROM rekordbox_tracks
               WHERE matched_track_id IS NULL ORDER BY artist, title"""
        ).fetchall()
        return {"tracks": [dict(r) for r in rows]}

    # ---- actions -------------------------------------------------------------

    @app.post("/api/scan")
    def trigger_scan():
        started = state.scan_async()
        return {"started": started, "progress": state.scanner.progress.as_dict()}

    @app.get("/api/scan/status")
    def scan_status():
        return state.scanner.progress.as_dict()

    @app.post("/api/rekordbox/sync")
    def trigger_rb_sync():
        return state.rekordbox.sync(force=True)

    @app.post("/api/actions/mik")
    def send_unanalyzed_to_mik(limit: int = Query(100, ge=1, le=2000)):
        """Open the not-yet-analyzed tracks in Mixed In Key (macOS)."""
        from . import actions

        rows = state.db.conn.execute(
            """SELECT path FROM tracks WHERE missing = 0 AND mik_status = 'none'
               ORDER BY artist, title LIMIT ?""",
            (limit,),
        ).fetchall()
        if not rows:
            return {"ok": True, "sent": 0}
        return actions.send_to_mixed_in_key([r["path"] for r in rows])

    @app.get("/api/export/rekordbox.xml")
    def export_rekordbox_xml():
        """rekordbox XML of every track missing from the collection.
        Import it via rekordbox Preferences > Advanced > Database > rekordbox xml."""
        from fastapi.responses import Response

        from . import actions

        xml = actions.rekordbox_xml(state.db)
        return Response(
            content=xml,
            media_type="application/xml",
            headers={"Content-Disposition": 'attachment; filename="liborg-rekordbox.xml"'},
        )

    # ---- frontend --------------------------------------------------------------

    web_dir = Path(__file__).parent / "web"

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(web_dir / "index.html")

    app.mount("/static", StaticFiles(directory=web_dir), name="static")
    return app
