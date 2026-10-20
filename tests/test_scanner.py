import shutil

from cratekeeper.scanner import Scanner, normalize_meta
from tests.conftest import make_silence_mp3, tag_as_mik, tag_basic


def test_scan_indexes_and_detects_mik(config, db, library):
    a = library / "analyzed.mp3"
    b = library / "sub" / "plain.mp3"
    make_silence_mp3(a)
    make_silence_mp3(b)
    tag_as_mik(a, key="8A", energy=7)

    scanner = Scanner(config, db)
    progress = scanner.scan()
    assert progress.total == 2
    assert progress.added == 2
    assert progress.errors == 0

    rows = {r["filename"]: dict(r) for r in db.conn.execute("SELECT * FROM tracks")}
    assert rows["analyzed.mp3"]["mik_status"] == "confirmed"
    assert rows["analyzed.mp3"]["key"] == "8A"
    assert rows["analyzed.mp3"]["energy"] == 7
    assert rows["plain.mp3"]["mik_status"] == "none"


def test_rescan_skips_unchanged_and_marks_missing(config, db, library):
    f = library / "track.mp3"
    make_silence_mp3(f)
    scanner = Scanner(config, db)
    scanner.scan()

    f.unlink()
    progress = scanner.scan()
    assert progress.removed == 1
    row = db.conn.execute("SELECT missing FROM tracks WHERE filename='track.mp3'").fetchone()
    assert row["missing"] == 1


def test_exact_duplicates_grouped(config, db, library):
    a = library / "one.mp3"
    make_silence_mp3(a)
    tag_basic(a, "Artist", "Song")
    shutil.copy(a, library / "copy of one.mp3")
    make_silence_mp3(library / "different.mp3")
    tag_basic(library / "different.mp3", "Other", "Thing")

    Scanner(config, db).scan()
    groups = db.conn.execute(
        "SELECT filename, dup_group FROM tracks ORDER BY filename"
    ).fetchall()
    by_name = {r["filename"]: r["dup_group"] for r in groups}
    assert by_name["one.mp3"] is not None
    assert by_name["one.mp3"] == by_name["copy of one.mp3"]
    assert by_name["different.mp3"] is None


def test_metadata_duplicates_grouped(config, db, library):
    a = library / "a.mp3"
    b = library / "b.mp3"
    make_silence_mp3(a)
    make_silence_mp3(b)
    tag_basic(a, "Daft Punk", "One More Time (Original Mix)")
    tag_basic(b, "daft punk", "One More Time")

    Scanner(config, db).scan()
    rows = db.conn.execute(
        "SELECT filename, meta_dup_group FROM tracks"
    ).fetchall()
    groups = {r["filename"]: r["meta_dup_group"] for r in rows}
    assert groups["a.mp3"] is not None
    assert groups["a.mp3"] == groups["b.mp3"]


def test_corrupt_file_recorded_not_crashed(config, db, library):
    bad = library / "corrupt.mp3"
    bad.write_bytes(b"this is not an mp3 at all" * 10)
    progress = Scanner(config, db).scan()
    assert progress.total == 1
    row = db.conn.execute("SELECT tag_error, mik_status FROM tracks").fetchone()
    assert row["mik_status"] == "none"


def test_normalize_meta():
    assert normalize_meta("Daft Punk", "Around the World (Original Mix)") == \
        normalize_meta("daft  punk", "Around The World")
    assert normalize_meta("A", "Song [LABEL001]") == normalize_meta("A", "Song")
    assert normalize_meta(None, "x") is None
    assert normalize_meta("x", "") is None
    # different remixes in parens must NOT collapse
    assert normalize_meta("A", "Song (Club Mix)") != normalize_meta("A", "Song (Dub Mix)")


def test_watcher_ignores_dot_dirs_like_scanner(config, library, tmp_path):
    from cratekeeper.watcher import LibraryWatcher, _Handler

    handler = _Handler(LibraryWatcher(config, None))
    assert handler._relevant(str(library / "a" / "song.mp3"))
    assert not handler._relevant(str(library / ".quarantine" / "song.mp3"))
    assert not handler._relevant(str(library / ".stfolder" / "song.mp3"))
    assert not handler._relevant(str(library / "a" / "notes.txt"))

    # a library that itself lives under a dot-dir is still watched
    config.library_path = tmp_path / ".hidden" / "music"
    assert handler._relevant(str(config.library_path / "song.mp3"))


def test_scan_keeps_rows_when_library_vanishes(config, library, db):
    from cratekeeper.scanner import Scanner

    make_silence_mp3(library / "keep.mp3")
    scanner = Scanner(config, db)
    scanner.scan()
    assert db.conn.execute("SELECT COUNT(*) FROM tracks WHERE missing = 0").fetchone()[0] == 1

    import shutil
    shutil.rmtree(library)  # drive unmounted
    progress = scanner.scan()
    assert progress.removed == 0
    assert db.conn.execute("SELECT COUNT(*) FROM tracks WHERE missing = 0").fetchone()[0] == 1
