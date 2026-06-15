from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import zipfile


@dataclass(frozen=True)
class RestoreResult:
    database_path: Path
    safety_copy_path: Path | None


@dataclass(frozen=True)
class AutoRestoreResult:
    restored: bool
    reason: str
    database_path: Path
    backup_path: Path
    safety_copy_path: Path | None = None


def backup_database(source: Path, destination_db: Path) -> None:
    destination_db.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination_db)
    try:
        apply_optional_journal_mode(destination_connection)
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def apply_optional_journal_mode(connection: sqlite3.Connection) -> None:
    journal_mode = os.getenv("BOT_SQLITE_JOURNAL_MODE", "").strip().upper()
    if journal_mode in {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}:
        connection.execute(f"PRAGMA journal_mode = {journal_mode}")


def create_state_backup(database_path: Path, output_dir: Path) -> Path:
    source = database_path.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Database not found: {source}")

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    destination_dir = output_dir.expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    work_db = destination_dir / f"queue-bot-state-{timestamp}.sqlite3"
    archive_path = destination_dir / f"queue-bot-state-{timestamp}.zip"

    backup_database(source, work_db)
    manifest = {
        "app": "queue-bot",
        "created_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "database_file": work_db.name,
        "source_database": str(source),
    }

    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(work_db, arcname=work_db.name)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    finally:
        with suppress(OSError):
            work_db.unlink()

    return archive_path


def create_rolling_state_backup(database_path: Path, archive_path: Path) -> Path:
    source = database_path.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Database not found: {source}")

    target = archive_path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    work_db = target.parent / f".{target.stem}-{timestamp}.sqlite3"
    temp_archive = target.parent / f".{target.stem}-{timestamp}.zip"
    try:
        backup_database(source, work_db)
        manifest = {
            "app": "queue-bot",
            "created_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "database_file": work_db.name,
            "source_database": str(source),
            "rolling_backup": True,
        }
        with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(work_db, arcname=work_db.name)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        shutil.copy2(temp_archive, target)
    finally:
        for path in (work_db, temp_archive):
            with suppress(OSError):
                path.unlink()

    return target


def database_has_schema(database_path: Path) -> bool:
    if not database_path.exists():
        return False

    connection = sqlite3.connect(database_path)
    try:
        apply_optional_journal_mode(connection)
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'media_items'"
        ).fetchone()
    finally:
        connection.close()
    return row is not None


def media_item_count(database_path: Path) -> int:
    if not database_has_schema(database_path):
        return 0

    connection = sqlite3.connect(database_path)
    try:
        apply_optional_journal_mode(connection)
        row = connection.execute("SELECT COUNT(*) FROM media_items").fetchone()
    finally:
        connection.close()
    return int(row[0] if row else 0)


def validate_database(database_path: Path) -> None:
    if not database_has_schema(database_path):
        raise ValueError("Backup does not look like a Queue Bot database.")


def _extract_backup_database(backup_path: Path, restored_db: Path) -> Path:
    with zipfile.ZipFile(backup_path) as archive:
        database_members = [name for name in archive.namelist() if name.endswith(".sqlite3")]
        if len(database_members) != 1:
            raise ValueError("Backup archive must contain exactly one .sqlite3 file.")
        restored_db.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(database_members[0]) as source, restored_db.open("wb") as destination:
            shutil.copyfileobj(source, destination)

    validate_database(restored_db)
    return restored_db


def restore_state_backup(backup_path: Path, database_path: Path) -> RestoreResult:
    archive_path = backup_path.expanduser().resolve()
    target = database_path.expanduser().resolve()
    if not archive_path.exists():
        raise FileNotFoundError(f"Backup not found: {archive_path}")

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target.parent.mkdir(parents=True, exist_ok=True)
    restored_db = target.parent / f".restore-{timestamp}.sqlite3"
    try:
        _extract_backup_database(archive_path, restored_db)
        safety_copy: Path | None = None
        if target.exists():
            safety_copy = target.with_suffix(f".before-restore-{timestamp}.sqlite3")
            shutil.copy2(target, safety_copy)

        shutil.copy2(restored_db, target)
        return RestoreResult(database_path=target, safety_copy_path=safety_copy)
    finally:
        with suppress(OSError):
            restored_db.unlink()


def restore_latest_backup_if_needed(
    database_path: Path,
    backup_path: Path,
    restore_if_empty: bool = True,
) -> AutoRestoreResult | None:
    target = database_path.expanduser().resolve()
    archive_path = backup_path.expanduser().resolve()
    if not archive_path.exists():
        return None

    reason = ""
    if not target.exists():
        reason = "database_missing"
    elif not database_has_schema(target):
        reason = "database_invalid"
    elif restore_if_empty and media_item_count(target) == 0:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup_db = archive_path.parent / f".autorestore-check-{timestamp}.sqlite3"
        try:
            _extract_backup_database(archive_path, backup_db)
            if media_item_count(backup_db) > 0:
                reason = "database_empty"
        finally:
            with suppress(OSError):
                backup_db.unlink()

    if not reason:
        return None

    result = restore_state_backup(archive_path, target)
    return AutoRestoreResult(
        restored=True,
        reason=reason,
        database_path=result.database_path,
        backup_path=archive_path,
        safety_copy_path=result.safety_copy_path,
    )
