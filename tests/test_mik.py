import base64
import json

import mutagen
import pytest

from cratekeeper import mik
from tests.conftest import make_silence_mp3, tag_as_mik


def test_none_tags():
    assert mik.detect(None).status == "none"


def test_id3_confirmed(library):
    f = library / "track.mp3"
    make_silence_mp3(f)
    tag_as_mik(f, key="4A", energy=6)
    result = mik.detect(mutagen.File(f).tags)
    assert result.status == "confirmed"
    assert result.key == "4A"
    assert result.energy == 6


def test_id3_untagged_is_none(library):
    f = library / "plain.mp3"
    make_silence_mp3(f)
    result = mik.detect(mutagen.File(f).tags)
    assert result.status == "none"


def test_id3_likely_without_signature(library):
    from mutagen.id3 import ID3, TKEY, TXXX

    f = library / "likely.mp3"
    make_silence_mp3(f)
    id3 = ID3(f)
    id3.add(TKEY(encoding=3, text=["7A"]))
    id3.add(TXXX(encoding=3, desc="EnergyLevel", text=["5"]))
    id3.save(f)
    result = mik.detect(mutagen.File(f).tags)
    assert result.status == "likely"
    assert result.key == "7A"
    assert result.energy == 5


def test_musical_key_without_energy_is_not_likely(library):
    from mutagen.id3 import ID3, TKEY

    f = library / "keyed.mp3"
    make_silence_mp3(f)
    id3 = ID3(f)
    id3.add(TKEY(encoding=3, text=["Am"]))  # not a Camelot key
    id3.save(f)
    assert mik.detect(mutagen.File(f).tags).status == "none"


def test_vorbis_base64_signature():
    """FLAC-style tags: base64 JSON payload in key/energy fields."""
    key_payload = base64.b64encode(
        json.dumps({"algorithm": 94, "key": "9A", "source": "mixedinkey"}).encode()
    ).decode()
    energy_payload = base64.b64encode(
        json.dumps({"algorithm": 13, "energyLevel": 8, "source": "mixedinkey"}).encode()
    ).decode()
    tags = {
        "key": [key_payload],
        "energy": [energy_payload],
        "initialkey": ["9A"],
        "energylevel": ["8"],
    }
    result = mik.detect(tags)
    assert result.status == "confirmed"
    assert result.key == "9A"
    assert result.energy == 8


def test_vorbis_plain_tags_are_likely():
    tags = {"initialkey": ["12B"], "energylevel": ["4"], "key": ["12B"]}
    result = mik.detect(tags)
    assert result.status == "likely"
    assert result.key == "12B"
    assert result.energy == 4


def test_garbage_payload_does_not_crash():
    tags = {"key": ["!!!not base64 or json!!!"], "energy": ["{broken"]}
    assert mik.detect(tags).status == "none"


@pytest.mark.parametrize("key,ok", [
    ("1A", True), ("12B", True), ("9m", True), ("13A", False),
    ("0A", False), ("Am", False), ("", False),
])
def test_camelot_regex(key, ok):
    assert bool(mik.CAMELOT_RE.match(key)) is ok
