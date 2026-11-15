"""Update check, and self-update for the macOS desktop app.

- Every few hours (if `check_updates` is on) ask GitHub for the latest
  release of cratekeeper; the dashboard shows a banner when it is newer.
- In the desktop app only, `install()` (manually, or automatically when
  `auto_update` is on) downloads this machine's DMG from the release,
  checks it against the release's SHA256SUMS.txt, mounts it read-only,
  verifies the app's code signature — and, when the running app is
  Developer-ID signed, that the new one comes from the same Team ID — and
  stages it next to the running bundle.
- The running bundle is never touched while cratekeeper runs (a PyInstaller
  app imports lazily from its own bundle). A detached helper waits for the
  process to exit, then swaps the staged app in, and relaunches it only if
  the user asked for a restart. So a staged update applies on the next quit.

Trust: with Developer ID builds the Team ID check pins the publisher. With
ad-hoc builds (no signing secrets on CI) integrity rests on HTTPS to
github.com plus the checksum file from the same release.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

log = logging.getLogger("cratekeeper.updates")

REPO = "guilhermewolf/cratekeeper"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_INTERVAL = 6 * 3600
MAX_DOWNLOAD = 800 * 1024 * 1024
STAGE_DIR = ".cratekeeper-update"
# Hosts a release download may come from (GitHub redirects assets to its CDN).
ALLOWED_HOSTS = ("github.com", "api.github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# Set by app.main(): only the desktop window process may replace the bundle
# (a launchd-run `serve` from the same bundle would race launchd's restart).
IN_DESKTOP_APP = False

# Swap the staged app in once the process `$1` has exited; relaunch only when
# the restart marker exists. Restores the old bundle if the swap fails.
SWAP_SCRIPT = r"""
pid="$1"; app="$2"; stage="$3"
while kill -0 "$pid" 2>/dev/null; do sleep 1; done
new="$stage/cratekeeper.app"
if [ -d "$new" ]; then
  rm -rf "$app.old"
  if mv "$app" "$app.old"; then
    if mv "$new" "$app"; then rm -rf "$app.old"; else mv "$app.old" "$app"; fi
  fi
fi
relaunch=0; [ -f "$stage/restart" ] && relaunch=1
rm -rf "$stage"
[ "$relaunch" = 1 ] && open "$app"
exit 0
"""


class UpdateError(Exception):
    pass


def parse_version(s: str | None) -> tuple[int, int, int] | None:
    m = VERSION_RE.match((s or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def app_bundle() -> Path | None:
    """The running .app bundle, when this is the frozen macOS app."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    bundle = Path(sys.executable).resolve().parents[2]  # .app/Contents/MacOS/exe
    return bundle if bundle.suffix == ".app" else None


def dmg_name(version: str, arch: str | None = None) -> str:
    return f"cratekeeper-{version}-macos-{arch or platform.machine()}.dmg"


def _check_host(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise UpdateError(f"refusing to download from {parts.scheme}://{parts.hostname}")


def _open(url: str, timeout: float, accept: str = "*/*"):
    _check_host(url)
    req = urllib.request.Request(
        url, headers={"User-Agent": f"cratekeeper/{__version__}", "Accept": accept})
    resp = urllib.request.urlopen(req, timeout=timeout)
    _check_host(resp.geturl())  # after redirects
    return resp


def _get(url: str, timeout: float = 30, accept: str = "*/*", limit: int = 4 << 20) -> bytes:
    with _open(url, timeout, accept) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise UpdateError(f"response from {url} is too large")
    return data


def _download(url: str, dest: Path) -> str:
    """Stream to `dest`; returns the sha256 hex digest."""
    h = hashlib.sha256()
    total = 0
    with _open(url, 300) as resp, open(dest, "wb") as out:
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise UpdateError("download is larger than any cratekeeper release")
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest()


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise UpdateError(f"{cmd[0]} failed: {(result.stderr or result.stdout).strip()[:200]}")
    return result


def team_id(app: Path) -> str | None:
    """Developer ID Team ID of a signed app; None for ad-hoc/unsigned."""
    result = subprocess.run(["codesign", "-dv", "--verbose=2", str(app)],
                            capture_output=True, text=True, timeout=60)
    m = re.search(r"^TeamIdentifier=(\S+)$", result.stderr, re.MULTILINE)
    return m.group(1) if m and m.group(1) != "not" else None


def expected_sha256(sums: str, name: str) -> str | None:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    return None


@dataclass
class UpdateStatus:
    state: str = "idle"  # idle | downloading | ready | error
    latest: str | None = None
    available: bool = False
    url: str | None = None  # release page
    notes: str | None = None
    error: str | None = None
    checked_at: float | None = None
    staged_version: str | None = None


class Updater:
    def __init__(self, config):
        self.config = config
        self.status = UpdateStatus()
        self._assets: dict[str, str] = {}
        self._install_lock = threading.Lock()
        self._swapper_spawned = False

    @property
    def can_install(self) -> bool:
        return IN_DESKTOP_APP and app_bundle() is not None

    def as_dict(self) -> dict:
        return dict(
            self.status.__dict__,
            current=__version__,
            check_updates=self.config.check_updates,
            auto_update=self.config.auto_update,
            can_install=self.can_install,
        )

    # ---- check ---------------------------------------------------------------

    def check(self) -> dict:
        s = self.status
        try:
            data = json.loads(_get(LATEST_URL, accept="application/vnd.github+json"))
        except Exception as exc:
            s.error = f"update check failed: {exc}"[:300]
            log.info(s.error)
            return self.as_dict()
        tag = str(data.get("tag_name") or "")
        latest, current = parse_version(tag), parse_version(__version__)
        s.latest = tag.lstrip("v") or None
        s.available = bool(latest and current and latest > current)
        page = str(data.get("html_url") or "")
        s.url = page if page.startswith(f"https://github.com/{REPO}/") else None
        s.notes = str(data.get("body") or "")[:2000]
        self._assets = {
            str(a.get("name")): str(a.get("browser_download_url"))
            for a in data.get("assets") or [] if isinstance(a, dict)
        }
        s.checked_at = time.time()
        s.error = None
        return self.as_dict()

    def maybe_check(self) -> None:
        """Periodic hook: check when due; auto-install when enabled."""
        if not self.config.check_updates:
            return
        s = self.status
        if s.checked_at is None or time.time() - s.checked_at >= CHECK_INTERVAL:
            self.check()
        if (self.config.auto_update and s.available and self.can_install
                and s.staged_version != s.latest and s.state != "downloading"):
            self.install(background=False)

    # ---- install -------------------------------------------------------------

    def install(self, background: bool = True) -> dict:
        if not self.can_install:
            self.status.error = "self-update works in the desktop app only"
            return self.as_dict()
        if not self.status.available:
            return self.as_dict()
        if not self._install_lock.acquire(blocking=False):
            return self.as_dict()  # already running

        def run():
            s = self.status
            try:
                s.state, s.error = "downloading", None
                self._stage(s.latest)
                s.state, s.staged_version = "ready", s.latest
                log.info("update %s staged; applies when cratekeeper quits", s.latest)
            except Exception as exc:
                s.state, s.error = "error", f"update failed: {exc}"[:300]
                log.warning(s.error)
            finally:
                self._install_lock.release()

        if background:
            self.status.state = "downloading"
            threading.Thread(target=run, name="cratekeeper-update", daemon=True).start()
        else:
            run()
        return self.as_dict()

    def _stage(self, version: str) -> None:
        bundle = app_bundle()
        if bundle is None:
            raise UpdateError("not running from cratekeeper.app")
        name = dmg_name(version)
        dmg_url, sums_url = self._assets.get(name), self._assets.get("SHA256SUMS.txt")
        if not dmg_url or not sums_url:
            raise UpdateError(f"release has no {name} or SHA256SUMS.txt")
        stage = bundle.parent / STAGE_DIR
        with tempfile.TemporaryDirectory(prefix="cratekeeper-update-") as tmp_name:
            tmp = Path(tmp_name)
            want = expected_sha256(_get(sums_url).decode("utf-8", "replace"), name)
            if not want:
                raise UpdateError(f"SHA256SUMS.txt has no entry for {name}")
            if _download(dmg_url, tmp / name) != want:
                raise UpdateError("checksum mismatch — download discarded")
            mnt = tmp / "mnt"
            mnt.mkdir()
            _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
                  "-mountpoint", str(mnt), str(tmp / name)])
            try:
                new_app = mnt / "cratekeeper.app"
                if not new_app.is_dir():
                    raise UpdateError("DMG does not contain cratekeeper.app")
                _run(["codesign", "--verify", "--deep", "--strict", str(new_app)])
                pinned = team_id(bundle)
                if pinned and team_id(new_app) != pinned:
                    raise UpdateError("new app is signed by a different developer")
                if stage.exists():
                    shutil.rmtree(stage)
                stage.mkdir()
                _run(["ditto", str(new_app), str(stage / "cratekeeper.app")])
            finally:
                subprocess.run(["hdiutil", "detach", str(mnt), "-force"],
                               capture_output=True, timeout=60)
        self._spawn_swapper(bundle, stage)

    def _spawn_swapper(self, bundle: Path, stage: Path) -> None:
        if self._swapper_spawned:
            return
        subprocess.Popen(
            ["/bin/sh", "-c", SWAP_SCRIPT, "cratekeeper-update",
             str(os.getpid()), str(bundle), str(stage)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._swapper_spawned = True

    def request_restart(self) -> bool:
        """Ask the swapper to relaunch after the swap. The caller then quits."""
        bundle = app_bundle()
        if bundle is None or self.status.state != "ready":
            return False
        (bundle.parent / STAGE_DIR / "restart").touch()
        return True
