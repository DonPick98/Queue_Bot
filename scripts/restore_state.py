from __future__ import annotations

from datetime import UTC, datetime
import argparse
import os
from pathlib import Path
import shutil
import sqlite3
import zipfile


def default_database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "./data/bot.sqlite3")).expanduser()


def validate_database(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'media_items'"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError("Backup does not look like a Mouth Queue database.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a Mouth Queue state backup.")
    parser.add_argument("backup", type=Path)
    parser.add_argument("--database", type=Path, default=default_database_path())
    parser.add_argument("--yes", action="store_true", help="Restore without confirmation.")
    args = parser.parse_args()

    backup_path = args.backup.expanduser().resolve()
    database_path = args.database.expanduser().resolve()
    if not backup_path.exists():
        raise SystemExit(f"Backup not found: {backup_path}")

    extract_dir = database_path.parent / ".restore_tmp"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)

    try:
        with zipfile.ZipFile(backup_path) as archive:
            database_members = [name for name in archive.namelist() if name.endswith(".sqlite3")]
            if len(database_members) != 1:
                raise SystemExit("Backup archive must contain exactly one .sqlite3 file.")
            archive.extract(database_members[0], path=extract_dir)

        restored_db = extract_dir / database_members[0]
        validate_database(restored_db)

        if database_path.exists() and not args.yes:
            answer = input(f"Replace existing database {database_path}? Type YES: ")
            if answer != "YES":
                raise SystemExit("Restore cancelled.")

        database_path.parent.mkdir(parents=True, exist_ok=True)
        if database_path.exists():
            timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            safety_copy = database_path.with_suffix(f".before-restore-{timestamp}.sqlite3")
            shutil.copy2(database_path, safety_copy)
            print(f"Existing database copied to: {safety_copy}")

        shutil.copy2(restored_db, database_path)
        print(f"Restored database to: {database_path}")
    finally:
        if extract_dir.exists():
            shutil.rmtree(extract_dir)


if __name__ == "__main__":
    main()
