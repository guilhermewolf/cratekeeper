"""Desktop app: a native macOS window around the local dashboard.

The web dashboard stays exactly what it is — a FastAPI server plus vanilla
JS. This module hosts it in a WKWebView window (via pywebview) so the whole
thing ships as a double-clickable `cratekeeper.app`:

- The server runs in a background thread on the configured host/port. If
  something is already listening there and identifies itself as cratekeeper
  (e.g. the launchd service), the window simply attaches to it.
- Closing the window hides it; the watcher keeps running and a click on the
  Dock icon brings the window back. Quit (⌘Q) stops everything.
- A small JS bridge (`window.pywebview.api`) gives the page native folder /
  save dialogs and "Reveal in Finder".
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__
from .config import Config, load_config

log = logging.getLogger("cratekeeper.app")

APP_TITLE = "cratekeeper"
SMOKE_ENV = "CRATEKEEPER_SMOKE"  # set to 1: open, verify the page loaded, quit


# ---- server plumbing ---------------------------------------------------------


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def is_cratekeeper_server(host: str, port: int, timeout: float = 1.5) -> bool:
    """True if a cratekeeper server answers on host:port."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=timeout) as resp:
            data = json.load(resp)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("app") == "cratekeeper"


def wait_for_server(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_cratekeeper_server(host, port, timeout=1.0):
            return True
        time.sleep(0.2)
    return False


class ServerThread:
    """uvicorn in a daemon thread; stop() asks it to exit."""

    def __init__(self, config: Config, port: int):
        import uvicorn

        from .api import create_app

        self.config = config
        self.port = port
        app = create_app(config)
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=config.host, port=port, log_level="warning")
        )
        self._thread = threading.Thread(
            target=self._server.run, name="cratekeeper-server", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


# ---- JS bridge ---------------------------------------------------------------


class DesktopApi:
    """Methods callable from the page as `window.pywebview.api.<name>()`."""

    def __init__(self):
        self.window = None  # set once the window exists
        self.base_url = ""

    def _trusted(self) -> bool:
        """pywebview exposes this object to whatever page the window shows;
        only honour calls while it is showing our own server."""
        try:
            url = self.window.get_current_url() or ""
        except Exception:
            return False
        return url == self.base_url or url.startswith(self.base_url + "/")

    def is_desktop(self) -> bool:
        return True

    def version(self) -> str:
        return __version__

    def choose_library(self) -> str | None:
        """Native folder picker; returns the chosen path (or None)."""
        if not self._trusted():
            return None
        import webview

        result = self.window.create_file_dialog(
            webview.FileDialog.FOLDER, directory=str(Path.home())
        )
        if not result:
            return None
        return str(result[0]) if isinstance(result, (list, tuple)) else str(result)

    def reveal(self, path: str) -> bool:
        """Show a file in Finder."""
        if not self._trusted() or not path or not Path(path).exists():
            return False
        subprocess.Popen(["open", "-R", path])
        return True

    def open_external(self, url: str) -> bool:
        if not self._trusted() or not url.startswith(("http://", "https://")):
            return False
        subprocess.Popen(["open", url])
        return True

    def save_rekordbox_xml(self) -> dict:
        """Fetch the rekordbox XML from the server and save it where the
        user picks (native save dialog), then reveal it."""
        if not self._trusted():
            return {"ok": False, "error": "untrusted page"}
        import webview

        target = self.window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=str(Path.home() / "Downloads"),
            save_filename="cratekeeper-rekordbox.xml",
        )
        if not target:
            return {"ok": False, "cancelled": True}
        target_path = Path(target[0] if isinstance(target, (list, tuple)) else target)
        try:
            with urllib.request.urlopen(
                f"{self.base_url}/api/export/rekordbox.xml", timeout=60
            ) as resp:
                target_path.write_bytes(resp.read())
        except OSError as exc:
            return {"ok": False, "error": str(exc)[:200]}
        subprocess.Popen(["open", "-R", str(target_path)])
        return {"ok": True, "path": str(target_path)}


# ---- window lifecycle --------------------------------------------------------


class _Lifecycle:
    def __init__(self):
        self.quitting = False


def _install_cocoa_hooks(lifecycle: _Lifecycle) -> None:
    """Teach pywebview's Cocoa backend two macOS niceties: a Dock-icon click
    re-shows the hidden window, and ⌘Q is allowed through even though a
    plain window close is turned into a hide."""
    try:
        import Foundation
        from webview.platforms.cocoa import BrowserView
    except Exception:  # pragma: no cover - non-mac or missing pyobjc
        log.debug("cocoa hooks unavailable", exc_info=True)
        return

    base = BrowserView.AppDelegate

    class CratekeeperAppDelegate(base):
        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible):
            if not has_visible:
                import webview

                for w in webview.windows:
                    try:
                        w.show()
                    except Exception:
                        log.debug("show failed", exc_info=True)
                app.activateIgnoringOtherApps_(True)
            return Foundation.YES

        def applicationShouldTerminate_(self, app):
            lifecycle.quitting = True
            return Foundation.YES

    BrowserView.AppDelegate = CratekeeperAppDelegate


def _setup_logging(config: Config) -> None:
    handlers: list[logging.Handler] = [logging.FileHandler(config.log_dir / "cratekeeper.log")]
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def main(argv: list[str] | None = None) -> int:
    import webview

    config = load_config()
    _setup_logging(config)
    smoke = os.environ.get(SMOKE_ENV) == "1"
    log.info("cratekeeper %s desktop starting (frozen=%s)", __version__, getattr(sys, "frozen", False))

    # 1. Server: attach to a running one, else start our own.
    server: ServerThread | None = None
    port = config.port
    if is_cratekeeper_server(config.host, config.port):
        log.info("attaching to cratekeeper already running on port %d", config.port)
    else:
        if not port_is_free(config.host, port):
            port = find_free_port(config.host)
            log.warning("port %d busy (not cratekeeper); using %d", config.port, port)
        server = ServerThread(config, port)
        server.start()
        if not wait_for_server(config.host, port):
            log.error("server did not come up on port %d", port)
            return 2
    base_url = f"http://{config.host}:{port}"

    # 2. Window.
    lifecycle = _Lifecycle()
    _install_cocoa_hooks(lifecycle)
    api = DesktopApi()
    api.base_url = base_url
    window = webview.create_window(
        APP_TITLE, base_url, js_api=api,
        width=1280, height=840, min_size=(820, 560),
        text_select=True,
    )
    api.window = window

    def on_closing():
        # Red button / ⌘W: hide instead of quit so the watcher keeps running.
        if lifecycle.quitting or smoke:
            return True
        window.hide()
        return False

    window.events.closing += on_closing

    exit_code = 0

    def after_start():
        # Runs in a worker thread once the GUI loop is up.
        if not smoke:
            return
        nonlocal exit_code

        def bail():
            time.sleep(60)
            log.error("smoke: timed out")
            os._exit(3)

        threading.Thread(target=bail, daemon=True).start()
        if not window.events.loaded.wait(30):
            log.error("smoke: page never loaded")
            exit_code = 4
        else:
            time.sleep(1.5)
            title = window.evaluate_js("document.title") or ""
            tiles = window.evaluate_js("document.querySelectorAll('.tile').length") or 0
            log.info("smoke: title=%r tiles=%s", title, tiles)
            if "cratekeeper" not in title or not tiles:
                exit_code = 5
        lifecycle.quitting = True
        window.destroy()

    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.start(after_start, debug=os.environ.get("CRATEKEEPER_DEBUG") == "1")

    # 3. GUI loop ended (⌘Q or smoke): shut the server down.
    if server is not None:
        server.stop()
    log.info("cratekeeper desktop exiting (%d)", exit_code)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
