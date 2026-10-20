# cratekeeper — agent notes

24/7 music library organizer for DJs: indexes a music folder, finds duplicates,
and reports Mixed In Key + rekordbox status per track. Python 3.10+, FastAPI,
SQLite, no frontend build step. Ships as a native macOS app (pywebview window
around the same server, frozen with PyInstaller) and as a CLI/web server.

## Commands

```bash
uv venv && uv pip install -e ".[dev]"   # setup
pytest                                   # tests (no music files or rekordbox needed)
ruff check .                             # lint
.venv/bin/cratekeeper scan                    # one-shot index of the configured library
.venv/bin/cratekeeper serve                   # dashboard at http://127.0.0.1:8321
.venv/bin/cratekeeper app                     # same thing in a native window (needs [app] extra)
scripts/build_macos.sh                        # dist/cratekeeper-<ver>-macos-<arch>.dmg (needs [app,build])
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
cratekeeper/app.py        desktop mode: uvicorn in a thread + pywebview (WKWebView) window,
                     JS bridge (folder/save dialogs, Reveal in Finder), hide-on-close,
                     attaches to an already-running server instead of starting a second
cratekeeper/cli.py        argparse CLI: init/scan/serve/app/status/install-service
cratekeeper/web/          vanilla HTML/CSS/JS dashboard (no build step — keep it that way)
packaging/           PyInstaller spec + entry point + entitlements + icon source
scripts/build_macos.sh  icns → pyinstaller → codesign → smoke tests → dmg (+ notarize if secrets)
.github/workflows/build.yml  tests, then builds on macos-26-intel (x86_64) + macos-26 (arm64);
                     v* tags publish a GitHub release with both DMGs
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
- `Config.configured` is False until a config file names a library; the server
  then starts no scan/watcher and the dashboard shows the folder picker.
  Changing the folder goes through `AppState.set_library` (persists, restarts
  the watcher, scans) — don't mutate `config.library_path` anywhere else.
- Desktop/web parity: every feature must work in a plain browser too. The JS
  bridge (`window.pywebview.api`) is only a nicer path for dialogs/Finder;
  `desktop()` in app.js returns null in a browser and the HTML falls back
  (`.desktop-only` / `.browser-only`).
- `packaging/entry.py` is the frozen entry: no args → window, args → CLI.
  Anything imported lazily must be listed in `packaging/cratekeeper.spec`
  hiddenimports or the frozen app will miss it — the build script's smoke
  tests (CLI, headless serve + scan, window) exist to catch exactly that.

## Gotchas

- MIK's FLAC signature is *base64-encoded JSON* in the `key`/`energy` vorbis
  comments; the ID3 one is raw JSON in `GEOB:Key`/`GEOB:Energy` frames.
  A Camelot `TKEY` + `TXXX:EnergyLevel` without the signature is only `likely`.
- rekordbox stores BPM ×100 (`BPM=12800` → 128.0).
- pyrekordbox derives the rekordbox db key from the local rekordbox install
  (no network). On CI/machines without rekordbox, `RekordboxSync.sync()`
  returns `{"ok": False}` gracefully — code must tolerate that.
- pywebview's Cocoa backend quits when the last window closes and has no
  Dock-reopen handler; `app.py` subclasses its `AppDelegate` to hide on close,
  re-show on Dock click and let ⌘Q through. `CRATEKEEPER_SMOKE=1` opens the
  window, checks the page rendered and exits — used by the build script.
- Build for each arch on that arch (no universal2: sqlcipher3/pyobjc wheels
  are per-arch). Local builds only produce the host's architecture.
- Syncthing drops partial files then renames; the watcher debounces 5 s and
  handles moved events, so don't "fix" that by scanning on every event.
