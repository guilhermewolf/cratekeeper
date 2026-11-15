import json
import subprocess

import pytest

from cratekeeper import __version__, updates
from cratekeeper.config import Config


def _release(tag: str, url: str = "https://github.com/guilhermewolf/cratekeeper/releases/tag/x"):
    return json.dumps({
        "tag_name": tag, "html_url": url, "body": "notes",
        "assets": [{"name": "SHA256SUMS.txt", "browser_download_url": "https://github.com/s"}],
    }).encode()


def test_version_and_checksum_helpers():
    assert updates.parse_version("v1.2.10") == (1, 2, 10) > updates.parse_version("1.2.9")
    assert updates.parse_version("v1.2") is None
    sums = "abc123  cratekeeper-1.0.0-macos-arm64.dmg\ndef456  other.zip\n"
    assert updates.expected_sha256(sums, "cratekeeper-1.0.0-macos-arm64.dmg") == "abc123"
    assert updates.expected_sha256(sums, "missing.dmg") is None
    assert updates.dmg_name("1.0.0", "x86_64") == "cratekeeper-1.0.0-macos-x86_64.dmg"


@pytest.mark.parametrize("url", [
    "http://github.com/x", "https://evil.example/x", "file:///etc/passwd",
])
def test_downloads_only_from_github_over_https(url):
    with pytest.raises(updates.UpdateError):
        updates._check_host(url)


def test_check_reports_newer_release(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "_get", lambda *a, **k: _release("v99.0.0"))
    u = updates.Updater(Config(data_dir=tmp_path))
    info = u.check()
    assert info["available"] is True and info["latest"] == "99.0.0"
    assert info["current"] == __version__ and info["can_install"] is False

    monkeypatch.setattr(updates, "_get", lambda *a, **k: _release(f"v{__version__}", "https://x.y/"))
    info = u.check()
    assert info["available"] is False and info["url"] is None  # foreign link dropped

    # outside the desktop app nothing is ever downloaded
    monkeypatch.setattr(updates, "_get", lambda *a, **k: _release("v99.0.0"))
    u.check()
    assert "desktop app only" in u.install()["error"]


def test_check_failure_is_reported_not_raised(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(updates, "_get", boom)
    info = updates.Updater(Config(data_dir=tmp_path)).check()
    assert "offline" in info["error"] and info["available"] is False


def test_swap_script_swaps_after_exit_without_relaunch(tmp_path):
    app = tmp_path / "cratekeeper.app"
    app.mkdir()
    (app / "version").write_text("old")
    stage = tmp_path / updates.STAGE_DIR
    (stage / "cratekeeper.app").mkdir(parents=True)
    (stage / "cratekeeper.app" / "version").write_text("new")
    gone = subprocess.Popen(["true"])
    gone.wait()  # an exited pid: the script proceeds immediately
    subprocess.run(["/bin/sh", "-c", updates.SWAP_SCRIPT, "t", str(gone.pid), str(app), str(stage)],
                   check=True, timeout=30)
    assert (app / "version").read_text() == "new"
    assert not stage.exists() and not (tmp_path / "cratekeeper.app.old").exists()


def test_update_settings_persist_without_touching_library(tmp_path, monkeypatch, config):
    from fastapi.testclient import TestClient

    from cratekeeper.api import create_app

    cfg_file = tmp_path / "config.toml"
    monkeypatch.setenv("CRATEKEEPER_CONFIG", str(cfg_file))
    client = TestClient(create_app(config, background=False), base_url="http://127.0.0.1")
    info = client.put("/api/update/settings", json={"check_updates": False}).json()
    assert info["check_updates"] is False and info["auto_update"] is False
    text = cfg_file.read_text()
    assert "check_updates = false" in text and "library_path" not in text
