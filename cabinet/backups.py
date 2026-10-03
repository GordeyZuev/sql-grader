"""Online, integrity-checked backups and explicit restore helpers for SQLite."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cabinet.config import app_path
from cabinet.infrastructure import sqlite_store


def create_backup(directory: Path | None = None, retention_days: int = 7) -> Path:
    source_path = sqlite_store.DB_PATH
    destination_dir = directory or app_path(os.getenv("CABINET_BACKUP_DIR"), "var/backups")
    destination_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(destination_dir, 0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final_path = destination_dir / f"cabinet-{stamp}.sqlite3"
    temporary_path = final_path.with_suffix(".sqlite3.tmp")
    with (
        sqlite3.connect(source_path, timeout=30) as source,
        sqlite3.connect(temporary_path) as target,
    ):
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            temporary_path.unlink(missing_ok=True)
            raise RuntimeError(f"backup integrity check failed: {result}")
    os.replace(temporary_path, final_path)
    if os.name == "posix":
        os.chmod(final_path, 0o600)
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    for candidate in destination_dir.glob("cabinet-*.sqlite3"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
        if modified < cutoff:
            candidate.unlink()
    return final_path


def restore_backup(backup_path: Path, destination: Path | None = None) -> Path:
    """Copy a checked backup into a temporary database, then atomically replace target."""
    destination_path = destination or sqlite_store.DB_PATH
    if not backup_path.is_file():
        raise FileNotFoundError(backup_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_suffix(destination_path.suffix + ".restore.tmp")
    with sqlite3.connect(f"file:{backup_path.resolve()}?mode=ro", uri=True) as source:
        result = source.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"backup integrity check failed: {result}")
        with sqlite3.connect(temporary_path) as target:
            if os.name == "posix":
                os.chmod(temporary_path, 0o600)
            source.backup(target)
            target_result = target.execute("PRAGMA integrity_check").fetchone()[0]
            if target_result != "ok":
                temporary_path.unlink(missing_ok=True)
                raise RuntimeError(f"restored database integrity check failed: {target_result}")
    if os.name == "posix":
        os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, destination_path)
    return destination_path


def backup_loop(interval_seconds: int = 86_400, retry_seconds: int = 300) -> None:
    while True:
        delay = interval_seconds
        try:
            if sqlite_store.DB_PATH.is_file():
                path = create_backup()
                print(f"cabinet backup created: {path}", flush=True)
            else:
                delay = retry_seconds
        except Exception as exc:
            print(f"cabinet backup failed: {exc}", flush=True)
            delay = retry_seconds
        time.sleep(delay)
