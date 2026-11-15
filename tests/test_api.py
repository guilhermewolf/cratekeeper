from fastapi.testclient import TestClient

from cratekeeper.api import create_app
from cratekeeper.scanner import Scanner
from tests.conftest import make_silence_mp3, tag_as_mik, tag_basic


def make_client(config) -> TestClient:
    app = create_app(config, background=False)
    return TestClient(app, base_url="http://127.0.0.1")


def test_stats_and_tracks_flow(config, library):
    a = library / "mik.mp3"
    b = library / "plain.mp3"
    make_silence_mp3(a)
    make_silence_mp3(b)
    tag_as_mik(a, key="3A", energy=9)
    tag_basic(b, "Some Artist", "Some Song")

    client = make_client(config)
    state = client.app.state.cratekeeper
    Scanner(config, state.db).scan()

    stats = client.get("/api/stats").json()
    assert stats["total"] == 2
    assert stats["mik"]["confirmed"] == 1
    assert stats["mik"]["none"] == 1

    # filter: only MIK-analyzed
    data = client.get("/api/tracks", params={"mik": "confirmed"}).json()
    assert data["total"] == 1
    assert data["tracks"][0]["filename"] == "mik.mp3"
    assert data["tracks"][0]["key"] == "3A"

    # filter: not in rekordbox (no rekordbox on CI — everything is out)
    data = client.get("/api/tracks", params={"rekordbox": "out"}).json()
    assert data["total"] == 2

    # search
    data = client.get("/api/tracks", params={"search": "Some Artist"}).json()
    assert data["total"] == 1
    assert data["tracks"][0]["artist"] == "Some Artist"


def test_track_detail_and_404(config, library):
    f = library / "one.mp3"
    make_silence_mp3(f)
    client = make_client(config)
    Scanner(config, client.app.state.cratekeeper.db).scan()

    track = client.get("/api/tracks/1").json()
    assert track["filename"] == "one.mp3"
    assert client.get("/api/tracks/9999").status_code == 404


def test_detail_includes_duplicates(config, library):
    import shutil

    a = library / "one.mp3"
    make_silence_mp3(a)
    tag_basic(a, "Artist", "Song")
    shutil.copy(a, library / "one copy.mp3")

    client = make_client(config)
    Scanner(config, client.app.state.cratekeeper.db).scan()

    # ids follow directory walk order, which differs per filesystem
    rows = client.get("/api/tracks").json()["tracks"]
    one = next(r for r in rows if r["filename"] == "one.mp3")
    track = client.get(f"/api/tracks/{one['id']}").json()
    assert len(track["exact_duplicates"]) == 1
    assert track["exact_duplicates"][0]["path"].endswith("one copy.mp3")
    assert track["rekordbox"] is None


def test_rekordbox_xml_export(config, library):
    f = library / "song.mp3"
    make_silence_mp3(f)
    tag_basic(f, "An Artist", "A <Song> & Co")

    client = make_client(config)
    Scanner(config, client.app.state.cratekeeper.db).scan()

    res = client.get("/api/export/rekordbox.xml")
    assert res.status_code == 200
    from xml.etree import ElementTree as ET

    root = ET.fromstring(res.content)
    tracks = root.findall(".//COLLECTION/TRACK")
    assert len(tracks) == 1
    assert tracks[0].get("Artist") == "An Artist"
    assert tracks[0].get("Location").startswith("file://localhost/")
    assert root.find(".//PLAYLISTS//NODE[@Type='1']/TRACK") is not None


def test_invalid_filter_rejected(config):
    client = make_client(config)
    assert client.get("/api/tracks", params={"mik": "bogus"}).status_code == 422


def test_index_served(config):
    client = make_client(config)
    res = client.get("/")
    assert res.status_code == 200
    assert "cratekeeper" in res.text


def test_new_files_flow(config, library):
    first = library / "old.mp3"
    make_silence_mp3(first)
    client = make_client(config)
    state = client.app.state.cratekeeper
    scanner = Scanner(config, state.db)
    scanner.scan()

    # the first scan is the baseline: nothing is new yet
    assert client.get("/api/new").json()["count"] == 0
    assert client.get("/api/tracks", params={"added": "new"}).json()["total"] == 0

    fresh = library / "fresh.mp3"
    make_silence_mp3(fresh)
    tag_basic(fresh, "New Artist", "New Song")
    scanner.scan()

    new = client.get("/api/stats").json()["new"]
    assert new["count"] == 1 and new["unanalyzed"] == 1 and new["not_in_rekordbox"] == 1
    rows = client.get("/api/tracks", params={"added": "new"}).json()["tracks"]
    assert [r["filename"] for r in rows] == ["fresh.mp3"]

    # XML scoped to the new-files window holds only the new track
    from xml.etree import ElementTree as ET

    window = {"since": new["since"], "until": new["until"]}
    root = ET.fromstring(client.get("/api/export/rekordbox.xml", params=window).content)
    assert [t.get("Artist") for t in root.findall(".//COLLECTION/TRACK")] == ["New Artist"]
    assert client.get("/api/export/rekordbox.xml", params={"since": 1}).status_code == 422

    # acknowledging clears the list; a stale (older) ack never moves it back
    assert client.post("/api/new/ack", json={"until": new["until"]}).json()["count"] == 0
    client.post("/api/new/ack", json={"until": 0})
    assert client.get("/api/new").json()["count"] == 0


def test_xml_strips_control_chars_and_bpm_key_sort_numerically(config, library):
    from mutagen.id3 import ID3, TBPM, TKEY

    for name, bpm, key in (("a.mp3", "128", "10A"), ("b.mp3", "95", "1A"), ("c.mp3", "174", "2B")):
        f = library / name
        make_silence_mp3(f)
        tag_basic(f, "Art\x01ist", name)
        id3 = ID3(f)
        id3.add(TBPM(encoding=3, text=[bpm]))
        id3.add(TKEY(encoding=3, text=[key]))
        id3.save(f)

    client = make_client(config)
    Scanner(config, client.app.state.cratekeeper.db).scan()

    from xml.etree import ElementTree as ET

    root = ET.fromstring(client.get("/api/export/rekordbox.xml").content)  # parses
    assert {t.get("Artist") for t in root.findall(".//COLLECTION/TRACK")} == {"Artist"}
    assert {t.get("AverageBpm") for t in root.findall(".//COLLECTION/TRACK")} == {
        "95.00", "128.00", "174.00"}

    by = lambda col: [t[col] for t in client.get(  # noqa: E731
        "/api/tracks", params={"sort": col}).json()["tracks"]]
    assert by("bpm") == ["95", "128", "174"]
    assert by("key") == ["1A", "2B", "10A"]
    assert client.get("/api/stats").json()["needs_attention"] == 3


def test_selection_actions(config, library, monkeypatch):
    from xml.etree import ElementTree as ET

    from cratekeeper import actions

    for name in ("a.mp3", "b.mp3", "c.mp3"):
        make_silence_mp3(library / name)
        tag_basic(library / name, "Artist", name)
    tag_as_mik(library / "a.mp3")
    client = make_client(config)
    Scanner(config, client.app.state.cratekeeper.db).scan()
    ids = {r["filename"]: r["id"] for r in client.get("/api/tracks").json()["tracks"]}

    sent = []
    monkeypatch.setattr(actions, "send_to_mixed_in_key",
                        lambda paths: sent.extend(paths) or {"ok": True, "sent": len(paths)})
    pick = [ids["a.mp3"], ids["c.mp3"]]
    assert client.post("/api/actions/mik/selection", json={"ids": pick}).json()["sent"] == 2
    assert sorted(p.rsplit("/", 1)[-1] for p in sent) == ["a.mp3", "c.mp3"]  # analyzed too
    assert client.post("/api/actions/mik/selection", json={"ids": []}).status_code == 422

    res = client.get("/api/export/rekordbox.xml", params={"ids": ",".join(map(str, pick))})
    root = ET.fromstring(res.content)
    assert len(root.findall(".//COLLECTION/TRACK")) == 2
    assert root.find(".//NODE[@Type='1']").get("Name") == "cratekeeper — selection"
    assert client.get("/api/export/rekordbox.xml", params={"ids": "1;DROP"}).status_code == 422


def test_missing_view_forget_and_relocate(config, library):
    a = library / "keep.mp3"
    gone = library / "gone.mp3"
    make_silence_mp3(a)
    make_silence_mp3(gone)
    tag_basic(gone, "Gone", "Song")
    client = make_client(config)
    db = client.app.state.cratekeeper.db
    Scanner(config, db).scan()
    gone.unlink()
    Scanner(config, db).scan()

    # relocate: rekordbox knows keep.mp3 at an old location that no longer exists
    db.conn.execute(
        """INSERT INTO rekordbox_tracks (rb_id, folder_path, filename, file_size, file_exists)
           VALUES ('9', '/old/keep.mp3', 'keep.mp3', ?, 0)""", (a.stat().st_size,))
    db.conn.commit()
    client.app.state.cratekeeper.rekordbox._link_tracks()
    rows = client.get("/api/tracks", params={"rekordbox": "relocate"}).json()["tracks"]
    assert [r["filename"] for r in rows] == ["keep.mp3"]
    assert client.get("/api/stats").json()["rekordbox"]["relocate"] == 1

    missing = client.get("/api/tracks", params={"issue": "missing"}).json()
    assert [r["filename"] for r in missing["tracks"]] == ["gone.mp3"]
    assert client.post("/api/missing/forget").json() == {"forgotten": 1}
    assert client.get("/api/tracks", params={"issue": "missing"}).json()["total"] == 0
    assert client.get("/api/tracks").json()["total"] == 1


def test_database_adds_columns_to_old_schema(tmp_path):
    import sqlite3

    from cratekeeper.db import Database

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE rekordbox_tracks (rb_id TEXT PRIMARY KEY, folder_path TEXT,"
                " filename TEXT, file_size INTEGER, title TEXT, artist TEXT, analyzed INTEGER,"
                " key TEXT, bpm REAL, matched_track_id INTEGER)")
    old.commit()
    old.close()
    cols = {r["name"] for r in Database(path).conn.execute("PRAGMA table_info(rekordbox_tracks)")}
    assert "file_exists" in cols


def test_key_and_bpm_mismatch_filters(config, library):
    from mutagen.id3 import ID3, TBPM

    for name, bpm in (("same.mp3", "128"), ("diff.mp3", "124")):
        f = library / name
        make_silence_mp3(f)
        tag_as_mik(f, key="8A", energy=5)
        id3 = ID3(f)
        id3.add(TBPM(encoding=3, text=[bpm]))
        id3.save(f)
    client = make_client(config)
    db = client.app.state.cratekeeper.db
    Scanner(config, db).scan()
    # rekordbox: "Am" == 8A, "Cm" == 5A; 128.0 == 128, 126.0 != 124
    db.conn.execute("UPDATE tracks SET rb_id='1', rb_key='Am', rb_bpm=128.0 WHERE filename='same.mp3'")
    db.conn.execute("UPDATE tracks SET rb_id='2', rb_key='Cm', rb_bpm=126.0 WHERE filename='diff.mp3'")
    db.conn.commit()

    for kind in ("key", "bpm"):
        rows = client.get("/api/tracks", params={"mismatch": kind}).json()["tracks"]
        assert [r["filename"] for r in rows] == ["diff.mp3"]
        assert rows[0][f"{kind}_mismatch"] == 1
    rb = client.get("/api/stats").json()["rekordbox"]
    assert (rb["key_mismatch"], rb["bpm_mismatch"]) == (1, 1)
