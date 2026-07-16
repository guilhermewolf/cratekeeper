"""Configuration loading.

Config lives at ~/.config/liborg/config.toml (or $LIBORG_CONFIG).
A missing file is fine — every field has a sensible default, and the
library path can be given on the command line the first time.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".ogg", ".opus"}

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "liborg" / "config.toml"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "liborg"


@dataclass
class Config:
    library_path: Path = Path.home() / "Music"
    data_dir: Path = DEFAULT_DATA_DIR
    host: str = "127.0.0.1"
    port: int = 8321
    # Full library rescan interval while serving (seconds). Watchdog handles
    # incremental changes in between; this catches anything it missed.
    rescan_interval: int = 6 * 3600
    # How often to re-read the rekordbox database (seconds). Cheap: skipped
    # unless the db file mtime changed.
    rekordbox_poll_interval: int = 300
    extensions: set[str] = field(default_factory=lambda: set(AUDIO_EXTENSIONS))
    # Directories (relative names) skipped during scans.
    ignore_dirs: set[str] = field(default_factory=lambda: {".stfolder", ".stversions", ".sync"})

    @property
    def db_path(self) -> Path:
        return self.data_dir / "liborg.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def config_path() -> Path:
    return Path(os.environ.get("LIBORG_CONFIG", DEFAULT_CONFIG_PATH))


def load_config() -> Config:
    cfg = Config()
    path = config_path()
    if path.exists():
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        if "library_path" in raw:
            cfg.library_path = Path(raw["library_path"]).expanduser()
        if "data_dir" in raw:
            cfg.data_dir = Path(raw["data_dir"]).expanduser()
        cfg.host = raw.get("host", cfg.host)
        cfg.port = int(raw.get("port", cfg.port))
        cfg.rescan_interval = int(raw.get("rescan_interval", cfg.rescan_interval))
        cfg.rekordbox_poll_interval = int(
            raw.get("rekordbox_poll_interval", cfg.rekordbox_poll_interval)
        )
        if "extensions" in raw:
            cfg.extensions = {e.lower() if e.startswith(".") else f".{e.lower()}"
                              for e in raw["extensions"]}
        if "ignore_dirs" in raw:
            cfg.ignore_dirs = set(raw["ignore_dirs"])
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def write_config(library_path: Path) -> Path:
    """Write a minimal config file recording the library path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'library_path = "{library_path}"\n')
    return path
