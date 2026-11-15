"""Outbound actions: hand tracks to Mixed In Key, export rekordbox XML.

cratekeeper never writes to the rekordbox database or to audio files itself.
Instead it uses each app's own intake path:

- Mixed In Key analyzes anything opened with it (`open -a "Mixed In Key 10"`).
  MIK writes its results into the files' tags, which the filesystem watcher
  picks up — so tracks flip to 'confirmed' on their own after analysis.
- rekordbox imports collections from its XML bridge format. We generate an XML
  containing every library track it doesn't know yet; the user imports it from
  rekordbox (Preferences > Advanced > Database > rekordbox xml).
"""

from __future__ import annotations

import logging
import re
import subprocess
import unicodedata
import urllib.parse
from pathlib import Path
from xml.etree import ElementTree as ET

from . import __version__
from .db import Database

log = logging.getLogger("cratekeeper.actions")

OPEN_BATCH = 50  # files per `open` invocation; keeps argv comfortably small


def find_mixed_in_key_app() -> str | None:
    apps = sorted(Path("/Applications").glob("Mixed In Key*.app"), reverse=True)
    return apps[0].stem if apps else None


def send_to_mixed_in_key(paths: list[str], app_name: str | None = None) -> dict:
    app = app_name or find_mixed_in_key_app()
    if not app:
        return {"ok": False, "error": "Mixed In Key app not found in /Applications"}
    sent = 0
    for i in range(0, len(paths), OPEN_BATCH):
        batch = [p for p in paths[i : i + OPEN_BATCH] if Path(p).exists()]
        if not batch:
            continue
        result = subprocess.run(
            ["open", "-a", app, *batch], capture_output=True, text=True
        )
        if result.returncode != 0:
            return {"ok": False, "sent": sent, "error": result.stderr.strip()[:200]}
        sent += len(batch)
    log.info("sent %d files to %s", sent, app)
    return {"ok": True, "sent": sent, "app": app}


KIND_NAMES = {"aif": "AIFF", "m4a": "M4A"}
# Characters XML 1.0 forbids; ElementTree writes them anyway and rekordbox then
# rejects the whole file because of one badly tagged track.
XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _xml_safe(value: str) -> str:
    return XML_ILLEGAL_RE.sub("", value)


def _location(path: str) -> str:
    # NFC like rekordbox's own FolderPath, so the import links back by path
    return "file://localhost" + urllib.parse.quote(unicodedata.normalize("NFC", path))


def rekordbox_xml(
    db: Database,
    where: str = "missing = 0 AND rb_id IS NULL",
    params: list | tuple = (),
    playlist_name: str = "cratekeeper — missing from rekordbox",
) -> bytes:
    """Build a rekordbox XML (collection + one playlist) of the tracks matching
    `where` — by default every present track not in the collection. `where`
    is trusted SQL from api.py; values go through `params`."""
    rows = db.conn.execute(
        f"""SELECT id, path, filename, title, artist, album, genre, bpm, key, size, duration
            FROM tracks WHERE {where} ORDER BY artist, title""",
        list(params),
    ).fetchall()

    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="cratekeeper", Version=__version__, Company="cratekeeper")
    collection = ET.SubElement(root, "COLLECTION", Entries=str(len(rows)))
    for r in rows:
        ext = Path(r["path"]).suffix.lstrip(".").lower()
        attrs = {
            "TrackID": str(r["id"]),
            "Name": r["title"] or r["filename"],
            "Artist": r["artist"] or "",
            "Album": r["album"] or "",
            "Genre": r["genre"] or "",
            "Kind": KIND_NAMES.get(ext, (ext or "audio").upper()) + " File",
            "Size": str(r["size"]),
            "TotalTime": str(int(r["duration"] or 0)),
            "Location": _location(r["path"]),
        }
        try:
            attrs["AverageBpm"] = f"{float(r['bpm']):.2f}" if r["bpm"] else None
        except ValueError:
            attrs["AverageBpm"] = None
        attrs["Tonality"] = r["key"] or None
        ET.SubElement(collection, "TRACK", **{
            k: _xml_safe(v) for k, v in attrs.items() if v is not None})

    playlists = ET.SubElement(root, "PLAYLISTS")
    node_root = ET.SubElement(playlists, "NODE", Type="0", Name="ROOT", Count="1")
    playlist = ET.SubElement(
        node_root, "NODE", Name=playlist_name, Type="1",
        KeyType="0", Entries=str(len(rows)),
    )
    for r in rows:
        ET.SubElement(playlist, "TRACK", Key=str(r["id"]))

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
