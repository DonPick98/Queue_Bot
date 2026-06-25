from contextlib import suppress
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from telegram_channel_scheduler_bot.state_archive import (
    create_rolling_state_backup,
    create_state_backup,
    restore_latest_backup_if_needed,
    restore_state_backup,
    write_telegram_backup_reference,
    read_telegram_backup_reference,
)
from telegram_channel_scheduler_bot.storage import Store


class StateArchiveTests(unittest.TestCase):
    def make_paths(self) -> tuple[Path, str]:
        root = Path(__file__).resolve().parents[1] / ".tmp"
        root.mkdir(exist_ok=True)
        suffix = uuid4().hex
        self.addCleanup(self.cleanup_paths, root, suffix)
        return root, suffix

    @staticmethod
    def cleanup_paths(root: Path, suffix: str) -> None:
        for path in root.glob(f"*{suffix}*"):
            with suppress(OSError):
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)

    def test_create_and_restore_state_backup(self):
        root, suffix = self.make_paths()
        database = root / f"source-{suffix}.sqlite3"
        restored_database = root / f"restored-{suffix}.sqlite3"
        backups = root / f"backups-{suffix}"

        store = Store(database)
        store.initialize()
        store.add_media("photo", "file-id", "unique-id", None, 123)

        backup = create_state_backup(database, backups)
        self.assertTrue(backup.exists())

        result = restore_state_backup(backup, restored_database)
        restored_store = Store(result.database_path)

        self.assertEqual(restored_store.queued_counts_by_type()["photo"], 1)
        self.assertIsNone(result.safety_copy_path)

    def test_restore_creates_safety_copy(self):
        root, suffix = self.make_paths()
        source_database = root / f"source-{suffix}.sqlite3"
        target_database = root / f"target-{suffix}.sqlite3"
        backups = root / f"backups-{suffix}"

        source_store = Store(source_database)
        source_store.initialize()
        source_store.add_media("video", "file-id", "unique-video", None, 123)
        backup = create_state_backup(source_database, backups)

        target_store = Store(target_database)
        target_store.initialize()

        result = restore_state_backup(backup, target_database)

        self.assertIsNotNone(result.safety_copy_path)
        self.assertTrue(result.safety_copy_path.exists())
        self.assertEqual(Store(target_database).queued_counts_by_type()["video"], 1)

    def test_create_rolling_state_backup_overwrites_same_archive(self):
        root, suffix = self.make_paths()
        database = root / f"source-{suffix}.sqlite3"
        archive = root / f"latest-state-{suffix}.zip"

        store = Store(database)
        store.initialize()
        store.add_media("photo", "file-1", "unique-1", None, 123)

        first = create_rolling_state_backup(database, archive)
        first_mtime = first.stat().st_mtime_ns

        store.add_media("video", "file-2", "unique-2", None, 123)
        second = create_rolling_state_backup(database, archive)

        self.assertEqual(first, second)
        self.assertTrue(second.exists())
        self.assertGreater(second.stat().st_size, 0)
        self.assertGreaterEqual(second.stat().st_mtime_ns, first_mtime)

    def test_restore_latest_backup_if_needed_restores_missing_database(self):
        root, suffix = self.make_paths()
        source_database = root / f"source-{suffix}.sqlite3"
        target_database = root / f"target-{suffix}.sqlite3"
        archive = root / f"latest-state-{suffix}.zip"

        store = Store(source_database)
        store.initialize()
        store.add_media("photo", "file-1", "unique-1", None, 123)
        create_rolling_state_backup(source_database, archive)

        result = restore_latest_backup_if_needed(target_database, archive)

        self.assertIsNotNone(result)
        self.assertEqual(result.reason, "database_missing")
        self.assertEqual(Store(target_database).queued_counts_by_type()["photo"], 1)

    def test_restore_latest_backup_if_needed_restores_empty_database(self):
        root, suffix = self.make_paths()
        source_database = root / f"source-{suffix}.sqlite3"
        target_database = root / f"target-{suffix}.sqlite3"
        archive = root / f"latest-state-{suffix}.zip"

        source_store = Store(source_database)
        source_store.initialize()
        source_store.add_media("video", "file-1", "unique-1", None, 123)
        create_rolling_state_backup(source_database, archive)

        target_store = Store(target_database)
        target_store.initialize()
        result = restore_latest_backup_if_needed(target_database, archive)

        self.assertIsNotNone(result)
        self.assertEqual(result.reason, "database_empty")
        self.assertEqual(Store(target_database).queued_counts_by_type()["video"], 1)

    def test_restore_latest_backup_if_needed_restores_newer_backup(self):
        root, suffix = self.make_paths()
        source_database = root / f"source-{suffix}.sqlite3"
        target_database = root / f"target-{suffix}.sqlite3"
        archive = root / f"latest-state-{suffix}.zip"

        target_store = Store(target_database)
        target_store.initialize()
        target_store.add_media("photo", "old-file", "old-unique", None, 123)

        source_store = Store(source_database)
        source_store.initialize()
        source_store.add_media("video", "new-file", "new-unique", None, 123)
        create_rolling_state_backup(source_database, archive)

        result = restore_latest_backup_if_needed(
            target_database,
            archive,
            restore_if_backup_newer=True,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.reason, "backup_newer")
        counts = Store(target_database).queued_counts_by_type()
        self.assertEqual(counts["video"], 1)
        self.assertEqual(counts["photo"], 0)

    def test_telegram_backup_reference_roundtrip(self):
        root, suffix = self.make_paths()
        archive = root / f"latest-state-{suffix}.zip"
        archive.write_bytes(b"zip")

        write_telegram_backup_reference(
            archive,
            file_id="telegram-file-id",
            file_unique_id="unique",
            chat_id=123,
            message_id=456,
        )
        reference = read_telegram_backup_reference(archive)

        self.assertIsNotNone(reference)
        self.assertEqual(reference["file_id"], "telegram-file-id")


if __name__ == "__main__":
    unittest.main()
