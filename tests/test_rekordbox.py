import time
import unicodedata

from cratekeeper.rekordbox import RekordboxSync


def _track(db, path: str, size: int) -> int:
    now = time.time()
    cur = db.conn.execute(
        """INSERT INTO tracks (path, filename, ext, size, mtime, first_seen, last_seen)
           VALUES (?, ?, '.mp3', ?, 0, ?, ?)""",
        (path, path.rsplit("/", 1)[-1], size, now, now),
    )
    return cur.lastrowid


def _rb(db, rb_id: str, path: str, size: int) -> None:
    db.conn.execute(
        """INSERT INTO rekordbox_tracks (rb_id, folder_path, filename, file_size, analyzed)
           VALUES (?, ?, ?, ?, 1)""",
        (rb_id, path, path.rsplit("/", 1)[-1], size),
    )


def test_link_tracks_nfc_moved_and_duplicate_copies(db):
    name = "Beyoncé - Song.mp3"
    nfd_path = unicodedata.normalize("NFD", f"/music/{name}")
    nfc_path = unicodedata.normalize("NFC", f"/music/{name}")

    on_disk = _track(db, nfd_path, 100)  # macOS NFD vs rekordbox NFC
    copy = _track(db, "/music/copies/" + unicodedata.normalize("NFD", name), 100)
    moved = _track(db, "/new/place/moved.mp3", 200)
    unknown = _track(db, "/music/unknown.mp3", 300)
    _rb(db, "1", nfc_path, 100)
    _rb(db, "2", "/old/place/moved.mp3", 200)
    _rb(db, "3", "/gone/orphan.mp3", 400)
    db.conn.commit()

    assert RekordboxSync(db)._link_tracks() == 3
    rows = {r["id"]: (r["rb_id"], r["rb_match"]) for r in
            db.conn.execute("SELECT id, rb_id, rb_match FROM tracks")}
    assert rows[on_disk] == ("1", "path")
    assert rows[copy] == ("1", "dup_of")
    assert rows[moved] == ("2", "name_size")
    assert rows[unknown] == (None, None)

    matched = dict(db.conn.execute("SELECT rb_id, matched_track_id FROM rekordbox_tracks"))
    assert matched == {"1": on_disk, "2": moved, "3": None}
