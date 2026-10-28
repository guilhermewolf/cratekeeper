"""Mixed In Key detection.

Mixed In Key leaves a definitive signature in the files it analyzes:

- ID3 (mp3 / aiff / wav): GEOB frames named "Key" and "Energy" whose body is
  JSON like {"algorithm": 94, "key": "9A", "source": "mixedinkey"}.
- Vorbis comments (flac / ogg): "key" and "energy" fields holding the same
  JSON, base64-encoded.
- MP4 (m4a): freeform "----:com.mixedinkey.mixedinkey:..." atoms.

When the signature is present we report 'confirmed'. Some files have a
Camelot key + EnergyLevel tag written by other tools (or had the GEOB frames
stripped by a tag editor) — those are 'likely'. Everything else is 'none'.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass

CAMELOT_RE = re.compile(r"^(?:[1-9]|1[0-2])[ABdm]$", re.IGNORECASE)
ENERGY_COMMENT_RE = re.compile(r"\bEnergy\s*(\d{1,2})\b", re.IGNORECASE)


@dataclass
class MikResult:
    status: str  # 'confirmed' | 'likely' | 'none'
    key: str | None = None
    energy: int | None = None


def _parse_mik_json(raw: bytes | str) -> dict | None:
    """Parse a MIK JSON payload, tolerating base64 wrapping and GEOB headers."""
    if isinstance(raw, bytes):
        # GEOB frame data may have trailing NULs.
        raw = raw.strip(b"\x00")
        # Find the JSON object inside the payload.
        start = raw.find(b"{")
        if start >= 0:
            try:
                return json.loads(raw[start:].decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
        raw = raw.decode("ascii", "replace")
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    # Vorbis variant: base64-encoded JSON.
    try:
        decoded = base64.b64decode(raw, validate=False)
        start = decoded.find(b"{")
        if start >= 0:
            return json.loads(decoded[start:].decode("utf-8", "replace"))
    except Exception:
        pass
    return None


def _is_mik_payload(obj: dict | None) -> bool:
    return bool(obj) and str(obj.get("source", "")).lower() == "mixedinkey"


def detect(tags) -> MikResult:  # a flat cascade of formats
    """Detect MIK analysis from a mutagen tag object (may be None)."""
    if tags is None:
        return MikResult("none")

    confirmed = False
    key: str | None = None
    energy: int | None = None

    # --- ID3 (mp3, aiff, wav with ID3 chunk) ---
    getall = getattr(tags, "getall", None)
    if getall is not None:
        for name in ("GEOB:Key", "GEOB:Energy"):
            for frame in getall(name):
                obj = _parse_mik_json(bytes(frame.data))
                if _is_mik_payload(obj):
                    confirmed = True
                    if "key" in obj:
                        key = str(obj["key"])
                    if "energyLevel" in obj:
                        try:
                            energy = int(obj["energyLevel"])
                        except (TypeError, ValueError):
                            pass
        for frame in getall("TKEY"):
            if key is None and frame.text:
                key = str(frame.text[0]) or None
        for frame in getall("TXXX:EnergyLevel"):
            if energy is None and frame.text:
                try:
                    energy = int(str(frame.text[0]))
                except ValueError:
                    pass
        if energy is None:
            for frame in getall("COMM"):
                m = ENERGY_COMMENT_RE.search(str(frame))
                if m:
                    energy = int(m.group(1))
                    break

    # --- Vorbis comments (flac, ogg) / plain dict-style tags ---
    elif hasattr(tags, "get"):
        for field in ("key", "energy"):
            for value in tags.get(field, []):
                obj = _parse_mik_json(str(value))
                if _is_mik_payload(obj):
                    confirmed = True
                    if "key" in obj:
                        key = str(obj["key"])
                    if "energyLevel" in obj:
                        try:
                            energy = int(obj["energyLevel"])
                        except (TypeError, ValueError):
                            pass
        # MP4 freeform atoms: MIK namespaces its atoms.
        for tag_name in list(tags.keys()):
            if "com.mixedinkey" in str(tag_name).lower():
                confirmed = True
        if key is None:
            for field in ("initialkey", "INITIALKEY", "TKEY"):
                vals = tags.get(field)
                if vals:
                    key = str(vals[0]) or None
                    break
        if energy is None:
            for field in ("energylevel", "ENERGYLEVEL"):
                vals = tags.get(field)
                if vals:
                    try:
                        energy = int(str(vals[0]))
                    except ValueError:
                        pass
                    break
            else:
                vals = tags.get("comment") or tags.get("COMMENT") or []
                for v in vals:
                    m = ENERGY_COMMENT_RE.search(str(v))
                    if m:
                        energy = int(m.group(1))
                        break

    if confirmed:
        return MikResult("confirmed", key, energy)
    # Camelot key + energy level without the MIK signature: analyzed, but the
    # signature frames were stripped or written by an older MIK version.
    if key and CAMELOT_RE.match(key.strip()) and energy is not None:
        return MikResult("likely", key, energy)
    return MikResult("none", key, energy)
