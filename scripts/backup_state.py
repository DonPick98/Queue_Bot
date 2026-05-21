from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from telegram_channel_scheduler_bot.state_archive import create_state_backup  # noqa: E402


def default_database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "./data/bot.sqlite3")).expanduser()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a portable Queue Bot state backup.")
    parser.add_argument("--database", type=Path, default=default_database_path())
    parser.add_argument("--output-dir", type=Path, default=Path("./state_backups"))
    args = parser.parse_args()

    archive_path = create_state_backup(args.database, args.output_dir)
    print(archive_path)


if __name__ == "__main__":
    main()
