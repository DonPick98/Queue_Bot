from pathlib import Path
import tempfile
import unittest

from telegram_channel_scheduler_bot.config import AppConfig
from telegram_channel_scheduler_bot.storage import PUBLISHED, QUEUED, Store


class StoreTests(unittest.TestCase):
    def make_store(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = Store(Path(tempdir.name) / "bot.sqlite3")
        store.initialize()
        store.bootstrap(
            AppConfig(
                bot_token="token",
                database_path=Path(tempdir.name) / "bot.sqlite3",
                channel_id="@channel",
                admin_user_ids=(123,),
                default_interval_minutes=60,
                default_batch_mode="fixed",
                default_posts_per_run=1,
                default_photo_ratio=1,
                default_video_ratio=1,
                balance_window=20,
                default_queue_order="random",
                default_timezone="Europe/Rome",
                default_posting_windows="all",
                default_auto_backup_enabled=False,
                default_auto_backup_interval_minutes=24 * 60,
                default_backup_after_publish_enabled=True,
                default_backup_after_publish_send_telegram=False,
                default_backup_after_publish_path="./state_backups/latest-state.zip",
                backup_auto_restore_enabled=True,
                backup_auto_restore_if_empty=True,
                backup_before_shutdown_enabled=True,
            )
        )
        return store

    def test_add_media_rejects_duplicate_queue_item(self):
        store = self.make_store()
        first = store.add_media("photo", "file-id", "unique-id", None, 123)
        second = store.add_media("photo", "file-id", "unique-id", None, 123)

        self.assertEqual(first.status, "queued")
        self.assertEqual(first.media_item.status, QUEUED)
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(second.existing_status, QUEUED)

    def test_published_media_is_not_queued_again(self):
        store = self.make_store()
        store.mark_published("unique-id", "video", source="channel", channel_message_id=10)
        result = store.add_media("video", "file-id", "unique-id", None, 123)

        self.assertEqual(result.status, "already_published")

    def test_add_media_rejects_same_content_fingerprint(self):
        store = self.make_store()
        first = store.add_media("photo", "file-id-1", "unique-id-1", None, 123, "reddit:abc123")
        second = store.add_media("photo", "file-id-2", "unique-id-2", None, 123, "reddit:abc123")

        self.assertEqual(first.status, "queued")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(second.media_item.id, first.media_item.id)

    def test_published_content_fingerprint_is_not_queued_again(self):
        store = self.make_store()
        first = store.add_media("video", "file-id-1", "unique-id-1", None, 123, "reddit-video:abc123")
        store.mark_published("unique-id-1", "video", source="bot", media_item_id=first.media_item.id)
        result = store.add_media("video", "file-id-2", "unique-id-2", None, 123, "reddit-video:abc123")

        self.assertEqual(result.status, "already_published")

    def test_mark_published_updates_queued_item(self):
        store = self.make_store()
        result = store.add_media("photo", "file-id", "unique-id", None, 123)
        store.mark_published("unique-id", "photo", source="bot", media_item_id=result.media_item.id)
        item = store.find_media_by_id(result.media_item.id)

        self.assertEqual(item.status, PUBLISHED)

    def test_latest_published_at_returns_most_recent_log_entry(self):
        store = self.make_store()
        self.assertIsNone(store.latest_published_at())

        store.mark_published("unique-1", "photo", source="bot", channel_message_id=1)
        first = store.latest_published_at()
        store.mark_published("unique-2", "video", source="bot", channel_message_id=2)
        second = store.latest_published_at()

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertGreaterEqual(second, first)

    def test_list_published_photos_returns_only_published_photos(self):
        store = self.make_store()
        first = store.add_media("photo", "file-photo-1", "unique-photo-1", None, 123)
        second = store.add_media("photo", "file-photo-2", "unique-photo-2", None, 123)
        store.add_media("photo", "file-photo-queued", "unique-photo-queued", None, 123)
        video = store.add_media("video", "file-video-1", "unique-video-1", None, 123)
        store.mark_published("unique-photo-1", "photo", source="bot", media_item_id=first.media_item.id)
        store.mark_published("unique-photo-2", "photo", source="bot", media_item_id=second.media_item.id)
        store.mark_published("unique-video-1", "video", source="bot", media_item_id=video.media_item.id)

        items = store.list_published_photos(limit=3)

        self.assertEqual([item.file_unique_id for item in items], ["unique-photo-2", "unique-photo-1"])

    def test_list_published_photos_can_exclude_previous_exports(self):
        store = self.make_store()
        first = store.add_media("photo", "file-photo-1", "unique-photo-1", None, 123)
        second = store.add_media("photo", "file-photo-2", "unique-photo-2", None, 123)
        store.mark_published("unique-photo-1", "photo", source="bot", media_item_id=first.media_item.id)
        store.mark_published("unique-photo-2", "photo", source="bot", media_item_id=second.media_item.id)

        items = store.list_published_photos(limit=3, exclude_ids=[second.media_item.id])

        self.assertEqual([item.file_unique_id for item in items], ["unique-photo-1"])

    def test_get_queued_item_chronological_returns_oldest_matching_type(self):
        store = self.make_store()
        store.add_media("photo", "file-photo-1", "unique-photo-1", None, 123)
        store.add_media("video", "file-video-1", "unique-video-1", None, 123)
        store.add_media("photo", "file-photo-2", "unique-photo-2", None, 123)

        first_photo = store.get_queued_item("photo", order="chronological")

        self.assertEqual(first_photo.file_unique_id, "unique-photo-1")

    def test_get_queued_item_prefers_priority_within_type(self):
        store = self.make_store()
        store.add_media("photo", "file-photo-1", "unique-photo-1", None, 123)
        store.add_media("photo", "file-photo-2", "unique-photo-2", None, 123, priority=100)

        first_photo = store.get_queued_item("photo", order="chronological")

        self.assertEqual(first_photo.file_unique_id, "unique-photo-2")
        self.assertEqual(first_photo.priority, 100)

    def test_get_queued_item_random_respects_requested_type(self):
        store = self.make_store()
        store.add_media("photo", "file-photo-1", "unique-photo-1", None, 123)
        store.add_media("video", "file-video-1", "unique-video-1", None, 123)

        item = store.get_queued_item("video", order="random")

        self.assertEqual(item.media_type, "video")


if __name__ == "__main__":
    unittest.main()
