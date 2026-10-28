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
