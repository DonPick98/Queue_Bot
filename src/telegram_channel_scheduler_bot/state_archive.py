from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile


@dataclass(frozen=True)
class RestoreResult:
    database_path: Path
    safety_copy_path: Path | None


def backup_database(source: Path, destination_db: Path) -> None:
    destination_db.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination_db)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


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
        if work_db.exists():
            work_db.unlink()

    return archive_path


def create_rolling_state_backup(database_path: Path, archive_path: Path) -> Path:
    source = database_path.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Database not found: {source}")

    target = archive_path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    with tempfile.TemporaryDirectory(prefix="queue-bot-rolling-backup-") as temp_dir:
        temp_path = Path(temp_dir)
        work_db = temp_path / f"queue-bot-state-{timestamp}.sqlite3"
        temp_archive = temp_path / target.name
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

    return target


def validate_database(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'media_items'"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError("Backup does not look like a Queue Bot database.")


def restore_state_backup(backup_path: Path, database_path: Path) -> RestoreResult:
    archive_path = backup_path.expanduser().resolve()
    target = database_path.expanduser().resolve()
    if not archive_path.exists():
        raise FileNotFoundError(f"Backup not found: {archive_path}")

    with tempfile.TemporaryDirectory(prefix="queue-bot-restore-") as temp_dir:
        extract_dir = Path(temp_dir)
        with zipfile.ZipFile(archive_path) as archive:
            database_members = [name for name in archive.namelist() if name.endswith(".sqlite3")]
            if len(database_members) != 1:
                raise ValueError("Backup archive must contain exactly one .sqlite3 file.")
            archive.extract(database_members[0], path=extract_dir)

        restored_db = extract_dir / database_members[0]
        validate_database(restored_db)

        target.parent.mkdir(parents=True, exist_ok=True)
        safety_copy: Path | None = None
        if target.exists():
            timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            safety_copy = target.with_suffix(f".before-restore-{timestamp}.sqlite3")
            shutil.copy2(target, safety_copy)

        shutil.copy2(restored_db, target)
        return RestoreResult(database_path=target, safety_copy_path=safety_copy)
