import json
import shutil
import subprocess
from pathlib import Path

import pytest

from liborg.config import Config
from liborg.db import Database

FIXTURES = Path(__file__).parent / "fixtures"


def make_silence_mp3(dest: Path) -> None:
    """Create a tiny valid MP3 (silence) for tag-writing tests."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "silence.mp3", dest)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "music"
    lib.mkdir()
    return lib


@pytest.fixture
def config(tmp_path: Path, library: Path) -> Config:
    cfg = Config(library_path=library, data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def db(config: Config) -> Database:
    return Database(config.db_path)


def tag_as_mik(path: Path, key: str = "9A", energy: int = 7) -> None:
    """Write a Mixed In Key-style signature onto an mp3."""
    from mutagen.id3 import COMM, GEOB, ID3, TKEY, TXXX

    id3 = ID3(path)
    key_json = json.dumps({"algorithm": 94, "key": key, "source": "mixedinkey"})
    energy_json = json.dumps(
        {"algorithm": 13, "energyLevel": energy, "source": "mixedinkey"}
    )
    id3.add(GEOB(encoding=0, mime="application/json", desc="Key", data=key_json.encode()))
    id3.add(GEOB(encoding=0, mime="application/json", desc="Energy", data=energy_json.encode()))
    id3.add(TKEY(encoding=3, text=[key]))
    id3.add(TXXX(encoding=3, desc="EnergyLevel", text=[str(energy)]))
    id3.add(COMM(encoding=3, lang="eng", desc="", text=[f"{key} - Energy {energy}"]))
    id3.save(path)


def tag_basic(path: Path, artist: str, title: str, **extra) -> None:
    from mutagen.id3 import ID3, TIT2, TPE1

    try:
        id3 = ID3(path)
    except Exception:
        id3 = ID3()
    id3.add(TPE1(encoding=3, text=[artist]))
    id3.add(TIT2(encoding=3, text=[title]))
    id3.save(path)


def ensure_fixture_mp3() -> None:
    """Generate the silence.mp3 fixture once (requires ffmpeg or lame) —
    checked into the repo so CI doesn't need either."""
    out = FIXTURES / "silence.mp3"
    if out.exists():
        return
    FIXTURES.mkdir(exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1",
         "-q:a", "9", "-y", str(out)],
        check=True, capture_output=True,
    )


ensure_fixture_mp3()
