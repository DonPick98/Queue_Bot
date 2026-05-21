from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from telegram_channel_scheduler_bot.state_archive import restore_state_backup  # noqa: E402


def default_database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "./data/bot.sqlite3")).expanduser()


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a Queue Bot state backup.")
    parser.add_argument("backup", type=Path)
    parser.add_argument("--database", type=Path, default=default_database_path())
    parser.add_argument("--yes", action="store_true", help="Restore without confirmation.")
    args = parser.parse_args()

    database_path = args.database.expanduser().resolve()
    if database_path.exists() and not args.yes:
        answer = input(f"Replace existing database {database_path}? Type YES: ")
        if answer != "YES":
            raise SystemExit("Restore cancelled.")

    result = restore_state_backup(args.backup, database_path)
    if result.safety_copy_path:
        print(f"Existing database copied to: {result.safety_copy_path}")
    print(f"Restored database to: {result.database_path}")


if __name__ == "__main__":
    main()
