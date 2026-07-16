"""Filesystem watcher.

Watches the library for changes (Syncthing drops, manual edits, Mixed In Key
tag writes) and applies them incrementally. Events are debounced: a burst of
changes triggers one batch a few seconds after the last event, followed by a
duplicate-group recompute.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config
from .db import Database
from .scanner import Scanner

log = logging.getLogger("liborg.watcher")

DEBOUNCE_SECONDS = 5.0


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "LibraryWatcher"):
        self.watcher = watcher

    def _relevant(self, path: str) -> bool:
        p = Path(path)
        if p.suffix.lower() not in self.watcher.config.extensions:
            return False
        parts = set(p.parts)
        return not (parts & self.watcher.config.ignore_dirs)

    def on_created(self, event):
        if not event.is_directory and self._relevant(event.src_path):
            self.watcher.enqueue("upsert", event.src_path)

    def on_modified(self, event):
        if not event.is_directory and self._relevant(event.src_path):
            self.watcher.enqueue("upsert", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and self._relevant(event.src_path):
            self.watcher.enqueue("delete", event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        if self._relevant(event.src_path):
            self.watcher.enqueue("delete", event.src_path)
        if self._relevant(event.dest_path):
            self.watcher.enqueue("upsert", event.dest_path)


class LibraryWatcher:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._pending: dict[str, str] = {}  # path -> action (last wins)
        self._lock = threading.Lock()
        self._last_event = 0.0
        self._observer: Observer | None = None
        self._stop = threading.Event()

    def enqueue(self, action: str, path: str) -> None:
        with self._lock:
            self._pending[path] = action
            self._last_event = time.time()

    def start(self) -> None:
        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(self.config.library_path), recursive=True)
        self._observer.start()
        threading.Thread(target=self._drain_loop, name="liborg-watcher", daemon=True).start()
        log.info("watching %s", self.config.library_path)

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            self._observer.stop()

    def _drain_loop(self) -> None:
        scanner = Scanner(self.config, self.db)
        while not self._stop.wait(1.0):
            with self._lock:
                quiet = time.time() - self._last_event
                if not self._pending or quiet < DEBOUNCE_SECONDS:
                    continue
                batch = self._pending
                self._pending = {}
            log.info("processing %d filesystem events", len(batch))
            for path, action in batch.items():
                try:
                    if action == "delete":
                        scanner.remove_file(Path(path))
                    else:
                        scanner.scan_file(Path(path))
                except Exception:
                    log.exception("watcher failed on %s", path)
            self.db.conn.commit()
            try:
                scanner.compute_duplicates()
            except Exception:
                log.exception("duplicate recompute failed")
