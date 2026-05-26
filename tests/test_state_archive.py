from pathlib import Path
import tempfile
import unittest

from telegram_channel_scheduler_bot.state_archive import (
    create_rolling_state_backup,
    create_state_backup,
    restore_latest_backup_if_needed,
    restore_state_backup,
)
from telegram_channel_scheduler_bot.storage import Store


class StateArchiveTests(unittest.TestCase):
    def test_create_and_restore_state_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "source.sqlite3"
            restored_database = root / "restored.sqlite3"
            backups = root / "backups"

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
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_database = root / "source.sqlite3"
            target_database = root / "target.sqlite3"
            backups = root / "backups"

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
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "source.sqlite3"
            archive = root / "latest-state.zip"

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
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_database = root / "source.sqlite3"
            target_database = root / "target.sqlite3"
            archive = root / "latest-state.zip"

            store = Store(source_database)
            store.initialize()
            store.add_media("photo", "file-1", "unique-1", None, 123)
            create_rolling_state_backup(source_database, archive)

            result = restore_latest_backup_if_needed(target_database, archive)

            self.assertIsNotNone(result)
            self.assertEqual(result.reason, "database_missing")
            self.assertEqual(Store(target_database).queued_counts_by_type()["photo"], 1)

    def test_restore_latest_backup_if_needed_restores_empty_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_database = root / "source.sqlite3"
            target_database = root / "target.sqlite3"
            archive = root / "latest-state.zip"

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


if __name__ == "__main__":
    unittest.main()
