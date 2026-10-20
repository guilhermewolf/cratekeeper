"""Command-line interface: cratekeeper <command>."""

from __future__ import annotations

import argparse
import logging
import plistlib
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import load_config, write_config

LAUNCHD_LABEL = "com.cratekeeper.server"


def _setup_logging(config) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.log_dir / "cratekeeper.log"),
        ],
    )


def cmd_init(args) -> int:
    library = Path(args.library).expanduser().resolve()
    if not library.is_dir():
        print(f"error: {library} is not a directory", file=sys.stderr)
        return 1
    path = write_config(library)
    print(f"config written to {path}")
    print(f"library: {library}")
    print("next: run `cratekeeper scan` to index it, then `cratekeeper serve`")
    return 0


def cmd_scan(args) -> int:
    from .db import Database
    from .rekordbox import RekordboxSync
    from .scanner import Scanner

    config = load_config()
    _setup_logging(config)
    if not config.library_path.is_dir():
        print(f"error: library path {config.library_path} does not exist.", file=sys.stderr)
        print("run: cratekeeper init <path-to-your-music>", file=sys.stderr)
        return 1
    db = Database(config.db_path)
    scanner = Scanner(config, db)
    print(f"scanning {config.library_path} ...")
    progress = scanner.scan()
    print(
        f"done: {progress.total} files "
        f"(+{progress.added} new, ~{progress.updated} updated, "
        f"-{progress.removed} missing, {progress.errors} errors)"
    )
    print("syncing rekordbox ...")
    result = RekordboxSync(db).sync(force=True)
    if result.get("ok"):
        print(f"rekordbox: {result.get('collection', '?')} entries, {result.get('matched', '?')} matched")
    else:
        print(f"rekordbox sync failed: {result.get('error')}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .api import create_app

    config = load_config()
    _setup_logging(config)
    app = create_app(config)
    print(f"cratekeeper {__version__} — http://{config.host}:{config.port}")
    if not config.configured:
        print("no library configured yet — pick one in the dashboard, or run: cratekeeper init <path>")
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning")
    return 0


def cmd_app(args) -> int:
    """Desktop window (macOS). Same server, native WKWebView around it."""
    from .app import main as app_main

    return app_main()


def cmd_status(args) -> int:
    import json
    import urllib.request

    config = load_config()
    url = f"http://{config.host}:{config.port}/api/stats"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            stats = json.load(resp)
    except OSError:
        print(f"server not reachable at {url}")
        print("start it with: cratekeeper serve   (or cratekeeper install-service)")
        return 1
    print(json.dumps(stats, indent=2))
    return 0


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def cmd_install_service(args) -> int:
    config = load_config()
    if getattr(sys, "frozen", False):
        # Inside cratekeeper.app: the bundle's executable doubles as the CLI.
        cratekeeper_bin = Path(sys.executable).resolve()
    else:
        cratekeeper_bin = Path(sys.argv[0]).resolve()
        if cratekeeper_bin.name != "cratekeeper":
            # invoked via python -m; fall back to the console script next to python
            candidate = Path(sys.executable).parent / "cratekeeper"
            if candidate.exists():
                cratekeeper_bin = candidate
            else:
                print("could not locate the cratekeeper executable", file=sys.stderr)
                return 1
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(cratekeeper_bin), "serve"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(config.log_dir / "launchd.out.log"),
        "StandardErrorPath": str(config.log_dir / "launchd.err.log"),
    }
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    result = subprocess.run(["launchctl", "load", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"launchctl load failed: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"service installed and started ({path})")
    print(f"dashboard: http://{config.host}:{config.port}")
    print("it will start automatically at login and restart if it crashes")
    return 0


def cmd_uninstall_service(args) -> int:
    path = _plist_path()
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink()
        print("service stopped and removed")
    else:
        print("service is not installed")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cratekeeper",
        description="Music library organizer: indexing, duplicates, Mixed In Key & rekordbox status.",
    )
    parser.add_argument("--version", action="version", version=f"cratekeeper {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="set the music library path")
    p.add_argument("library", help="path to your music folder")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("scan", help="index the library once and exit")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("serve", help="run the web dashboard + background watcher")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("app", help="open the desktop window (macOS)")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("status", help="print stats from the running server")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("install-service", help="run 24/7 via launchd (macOS)")
    p.set_defaults(func=cmd_install_service)

    p = sub.add_parser("uninstall-service", help="stop and remove the launchd service")
    p.set_defaults(func=cmd_uninstall_service)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
