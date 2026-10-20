"""Configuration loading.

Config lives at ~/.config/cratekeeper/config.toml (or $CRATEKEEPER_CONFIG).
A missing file is fine — every field has a sensible default, and the
library path can be given on the command line the first time.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".ogg", ".opus"}

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "cratekeeper" / "config.toml"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "cratekeeper"

# Keys that `write_config` knows how to serialize back out, in file order.
_WRITABLE_KEYS = (
    "library_path", "data_dir", "host", "port", "rescan_interval",
    "rekordbox_poll_interval", "extensions", "ignore_dirs",
)


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
    # True once a config file with a library_path exists. Until then the
    # server does not auto-scan anything and the dashboard asks for a folder.
    configured: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cratekeeper.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def config_path() -> Path:
    return Path(os.environ.get("CRATEKEEPER_CONFIG", DEFAULT_CONFIG_PATH))


def _read_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load_config() -> Config:
    cfg = Config()
    path = config_path()
    raw = _read_raw(path)
    if "library_path" in raw:
        cfg.library_path = Path(raw["library_path"]).expanduser()
        cfg.configured = True
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


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set, frozenset)):
        return "[" + ", ".join(_toml_value(v) for v in sorted(value, key=str)) + "]"
    if isinstance(value, dict):  # inline table
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
    # TOML basic strings share JSON's escaping rules.
    return json.dumps(str(value), ensure_ascii=False)


def write_config(library_path: Path, **overrides) -> Path:
    """Record the library path (and any other known keys), preserving the
    other settings already in the file."""
    path = config_path()
    raw = _read_raw(path)
    raw["library_path"] = str(library_path)
    for key, value in overrides.items():
        if key not in _WRITABLE_KEYS:
            raise ValueError(f"unknown config key: {key}")
        raw[key] = value
    lines = ["# cratekeeper configuration", ""]
    for key in _WRITABLE_KEYS:
        if key in raw:
            lines.append(f"{key} = {_toml_value(raw[key])}")
    for key, value in raw.items():  # keep anything we don't know about
        if key not in _WRITABLE_KEYS:
            lines.append(f"{key} = {_toml_value(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path
