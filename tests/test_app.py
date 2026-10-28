"""Desktop-app plumbing and the config API. No GUI is opened here."""

import json
import socket
import time

from fastapi.testclient import TestClient

from cratekeeper import __version__
from cratekeeper.api import create_app
from cratekeeper.config import Config, load_config, write_config
from tests.conftest import make_silence_mp3


def make_client(config) -> TestClient:
    return TestClient(create_app(config, background=False), base_url="http://127.0.0.1")


# ---- config file round-trip -------------------------------------------------


def test_write_config_preserves_other_keys(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    monkeypatch.setenv("CRATEKEEPER_CONFIG", str(cfg_file))
    cfg_file.write_text(
        f'library_path = "/old"\ndata_dir = "{tmp_path / "data"}"\nport = 9000\n'
        'extensions = [".mp3"]\ncustom = "x"\nnested = { a = 1, b = "two" }\n'
        'when = 2024-01-02T03:04:05Z\n'
    )
    lib = tmp_path / "Mus ic \"quoted\""
    lib.mkdir()

    write_config(lib)
    loaded = load_config()
    assert loaded.library_path == lib
    assert loaded.data_dir == tmp_path / "data"
    assert loaded.port == 9000
    assert loaded.extensions == {".mp3"}
    assert loaded.configured is True
    text = cfg_file.read_text()
    assert 'custom = "x"' in text
    # unknown non-scalar values survive a rewrite with their types intact
    write_config(lib)
    import tomllib
    raw = tomllib.loads(cfg_file.read_text())
    assert raw["nested"] == {"a": 1, "b": "two"}
    assert raw["when"].year == 2024


def test_unconfigured_without_library_path(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'data_dir = "{tmp_path / "data"}"\n')
    monkeypatch.setenv("CRATEKEEPER_CONFIG", str(cfg_file))
    cfg = load_config()
    assert cfg.configured is False
    assert cfg.data_dir == tmp_path / "data"


def test_rejects_foreign_host_and_origin(config):
    client = make_client(config)
    assert client.get("/api/health", headers={"Host": "evil.example:8321"}).status_code == 421
    assert client.get("/api/health", headers={"Host": "localhost:8321"}).status_code == 200
    r = client.put("/api/config", json={"library_path": "/x"},
                   headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/scan", headers={"Origin": "http://127.0.0.1:8321"})
    assert r.status_code == 200


# ---- /api/config + /api/health ----------------------------------------------


def test_health_and_config_endpoints(config):
    client = make_client(config)
    assert client.get("/api/health").json() == {"app": "cratekeeper", "version": __version__}
    data = client.get("/api/config").json()
    assert data["configured"] is True
    assert data["library_path"] == str(config.library_path)
    assert data["library_exists"] is True


def test_put_config_switches_library_and_scans(tmp_path, monkeypatch, library):
    cfg_file = tmp_path / "config.toml"
    monkeypatch.setenv("CRATEKEEPER_CONFIG", str(cfg_file))
    config = Config(library_path=tmp_path / "nowhere", data_dir=tmp_path / "data")
    config.data_dir.mkdir()
    config.log_dir.mkdir()
    client = make_client(config)
    assert client.get("/api/stats").json()["configured"] is False
    assert client.post("/api/scan").status_code == 409

    make_silence_mp3(library / "a.mp3")
    res = client.put("/api/config", json={"library_path": str(library)})
    assert res.status_code == 200
    assert res.json()["configured"] is True
    assert config.library_path == library.resolve()
    assert cfg_file.exists()

    # the scan kicked off by set_library finishes in the background
    state = client.app.state.cratekeeper
    deadline = time.time() + 30
    while True:
        # only touch the lock when the scan thread has let go of it
        if state._scan_lock.acquire(timeout=1):
            state._scan_lock.release()
            if client.get("/api/stats").json()["total"] == 1:
                break
        assert time.time() < deadline, "background scan did not finish"
    # watcher was started on the new path
    assert state._watching is True
    state.watcher.stop()


def test_put_config_rejects_non_directory(config, tmp_path):
    client = make_client(config)
    res = client.put("/api/config", json={"library_path": str(tmp_path / "nope")})
    assert res.status_code == 400
    assert client.get("/api/config").json()["library_path"] == str(config.library_path)


# ---- desktop helpers ----------------------------------------------------------


def test_port_helpers_and_server_detection():
    from cratekeeper import app as desktop

    port = desktop.find_free_port("127.0.0.1")
    assert desktop.port_is_free("127.0.0.1", port)
    assert desktop.is_cratekeeper_server("127.0.0.1", port) is False

    # a non-cratekeeper listener on the port
    sock = socket.socket()
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
    try:
        assert desktop.port_is_free("127.0.0.1", port) is False
        assert desktop.is_cratekeeper_server("127.0.0.1", port, timeout=0.5) is False
    finally:
        sock.close()


def test_server_thread_serves_and_stops(config):
    from cratekeeper import app as desktop

    port = desktop.find_free_port(config.host)
    server = desktop.ServerThread(config, port)
    server.start()
    try:
        assert desktop.wait_for_server(config.host, port, timeout=15)
        assert desktop.is_cratekeeper_server(config.host, port)
    finally:
        server.stop()
    assert desktop.port_is_free(config.host, port)


def test_desktop_api_reveal_and_open_guard(tmp_path):
    from cratekeeper.app import DesktopApi

    api = DesktopApi()
    assert api.is_desktop() is True
    assert api.version() == __version__
    assert api.reveal(str(tmp_path / "missing")) is False
    assert api.open_external("file:///etc/passwd") is False
    assert json.dumps({"ok": api.is_desktop()})  # serializable for the JS bridge


def test_bridge_refuses_calls_from_foreign_pages(tmp_path):
    from cratekeeper.app import DesktopApi

    class FakeWindow:
        def __init__(self, url):
            self.url = url

        def get_current_url(self):
            return self.url

    existing = tmp_path / "song.mp3"
    existing.write_bytes(b"x")
    api = DesktopApi()
    api.base_url = "http://127.0.0.1:8321"

    api.window = FakeWindow("http://evil.example/")
    assert api.reveal(str(existing)) is False
    assert api.open_external("https://example.com") is False
    assert api.choose_library() is None
    assert api.save_rekordbox_xml()["ok"] is False

    api.window = FakeWindow("http://127.0.0.1:8321/")
    assert api._trusted() is True
