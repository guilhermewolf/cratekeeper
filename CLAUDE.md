# cratekeeper — agent notes

24/7 music library organizer for DJs: indexes a music folder, finds duplicates,
and reports Mixed In Key + rekordbox status per track. Python 3.10+, FastAPI,
SQLite, no frontend build step.

## Commands

```bash
uv venv && uv pip install -e ".[dev]"   # setup
pytest                                   # tests (no music files or rekordbox needed)
ruff check .                             # lint
.venv/bin/cratekeeper scan                    # one-shot index of the configured library
.venv/bin/cratekeeper serve                   # dashboard at http://127.0.0.1:8321
```

Config lives at `~/.config/cratekeeper/config.toml` (`cratekeeper init <path>` writes it);
data at `~/.local/share/cratekeeper/cratekeeper.db`. Tests must never touch either — use
tmp_path fixtures.

## Architecture

```
cratekeeper/config.py     TOML config, defaults, audio extension list
cratekeeper/db.py         SQLite schema + per-thread connections (WAL mode)
cratekeeper/scanner.py    library walk, tag extraction (mutagen), 2-tier hashing,
                     duplicate grouping (exact + normalized artist/title)
cratekeeper/mik.py        Mixed In Key detection — pure function over mutagen tags
cratekeeper/rekordbox.py  reads rekordbox master.db via pyrekordbox (READ-ONLY),
                     mirrors collection, links tracks by path then name+size
cratekeeper/watcher.py    watchdog observer, debounced incremental updates
cratekeeper/actions.py    outbound actions: `open -a` handoff to Mixed In Key,
                     rekordbox XML export (the only rekordbox "write" path)
cratekeeper/api.py        FastAPI app: JSON API + serves cratekeeper/web/ static frontend
cratekeeper/cli.py        argparse CLI: init/scan/serve/status/install-service
cratekeeper/web/          vanilla HTML/CSS/JS dashboard (no build step — keep it that way)
```

Data flow: scanner/watcher write `tracks`; rekordbox sync rewrites
`rekordbox_tracks` and stamps `rb_*` columns onto `tracks`; the API only reads.
`mik_status` is one of `confirmed` / `likely` / `none` — see `mik.py` docstring
for what the MIK signature looks like per container format.

## Invariants

- **Never write to the rekordbox database.** pyrekordbox can write; we don't.
  All rekordbox access stays read-only.
- **Never modify files in the user's music library.** Tag reads only.
- `mik.detect()` and `scanner.read_tags()` must never raise — corrupt audio
  files are normal; record `tag_error` and move on.
- The frontend stays dependency-free vanilla JS served by FastAPI. No npm.
- SQLite connections are per-thread (`db.Database.conn`); don't share a
  connection across threads.
- Duplicate groups are fully recomputed after each scan (`compute_duplicates`),
  so group ids are not stable across scans — never persist them elsewhere.

## Gotchas

- MIK's FLAC signature is *base64-encoded JSON* in the `key`/`energy` vorbis
  comments; the ID3 one is raw JSON in `GEOB:Key`/`GEOB:Energy` frames.
  A Camelot `TKEY` + `TXXX:EnergyLevel` without the signature is only `likely`.
- rekordbox stores BPM ×100 (`BPM=12800` → 128.0).
- pyrekordbox needs the rekordbox db key; it auto-downloads it on first use.
  On CI/machines without rekordbox, `RekordboxSync.sync()` returns
  `{"ok": False}` gracefully — code must tolerate that.
- Syncthing drops partial files then renames; the watcher debounces 5 s and
  handles moved events, so don't "fix" that by scanning on every event.
