from __future__ import annotations

from datetime import UTC, datetime
import argparse
import json
import os
from pathlib import Path
import sqlite3
import zipfile


def default_database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "./data/bot.sqlite3")).expanduser()


def backup_database(source: Path, destination_db: Path) -> None:
    destination_db.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination_db)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a portable Mouth Queue state backup.")
    parser.add_argument("--database", type=Path, default=default_database_path())
    parser.add_argument("--output-dir", type=Path, default=Path("./state_backups"))
    args = parser.parse_args()

    database_path = args.database.expanduser().resolve()
    if not database_path.exists():
        raise SystemExit(f"Database not found: {database_path}")

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_db = output_dir / f"mouth-queue-state-{timestamp}.sqlite3"
    archive_path = output_dir / f"mouth-queue-state-{timestamp}.zip"

    backup_database(database_path, work_db)
    manifest = {
        "created_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_database": str(database_path),
        "database_file": work_db.name,
        "app": "mouth-queue-bot",
    }

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(work_db, arcname=work_db.name)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))

    work_db.unlink()
    print(archive_path)


if __name__ == "__main__":
    main()
