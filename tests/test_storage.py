from contextlib import suppress
from pathlib import Path
import unittest
from uuid import uuid4

from telegram_channel_scheduler_bot.config import AppConfig
from telegram_channel_scheduler_bot.storage import PUBLISHED, QUEUED, Store


class StoreTests(unittest.TestCase):
    def make_store(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp"
        temp_root.mkdir(exist_ok=True)
        database_path = temp_root / f"test-{uuid4().hex}.sqlite3"
        self.addCleanup(self.unlink_if_possible, database_path)
        self.addCleanup(self.unlink_if_possible, database_path.with_suffix(".sqlite3-journal"))
        store = Store(database_path)
        store.initialize()
        store.bootstrap(
            AppConfig(
                bot_token="token",
                database_path=database_path,
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
                default_schedule_mode="anchored",
                default_auto_backup_enabled=False,
                default_auto_backup_interval_minutes=24 * 60,
                default_backup_after_publish_enabled=True,
                default_backup_after_publish_send_telegram=False,
                default_backup_after_publish_path="./state_backups/latest-state.zip",
                backup_auto_restore_enabled=True,
                backup_auto_restore_if_empty=True,
                backup_before_shutdown_enabled=True,
                backup_telegram_auto_download_enabled=True,
            )
        )
        return store

    @staticmethod
    def unlink_if_possible(path: Path) -> None:
        with suppress(OSError):
            path.unlink(missing_ok=True)

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

    def test_add_media_rejects_same_content_hash(self):
        store = self.make_store()
        first = store.add_media("video", "file-id-1", "unique-id-1", None, 123, content_hash="abc123")
        second = store.add_media("video", "file-id-2", "unique-id-2", None, 123, content_hash="abc123")

        self.assertEqual(first.status, "queued")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(second.media_item.id, first.media_item.id)

    def test_add_media_rejects_same_photo_visual_hash(self):
        store = self.make_store()
        first = store.add_media("photo", "file-id-1", "unique-id-1", None, 123, visual_hash="ff00aa")
        second = store.add_media("photo", "file-id-2", "unique-id-2", None, 123, visual_hash="ff00aa")

        self.assertEqual(first.status, "queued")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(second.media_item.id, first.media_item.id)

    def test_published_content_fingerprint_is_not_queued_again(self):
        store = self.make_store()
        first = store.add_media("video", "file-id-1", "unique-id-1", None, 123, "reddit-video:abc123")
        store.mark_published("unique-id-1", "video", source="bot", media_item_id=first.media_item.id)
        result = store.add_media("video", "file-id-2", "unique-id-2", None, 123, "reddit-video:abc123")

        self.assertEqual(result.status, "already_published")

    def test_published_content_hash_is_not_queued_again(self):
        store = self.make_store()
        first = store.add_media("video", "file-id-1", "unique-id-1", None, 123, content_hash="abc123")
        store.mark_published("unique-id-1", "video", source="bot", media_item_id=first.media_item.id)
        result = store.add_media("video", "file-id-2", "unique-id-2", None, 123, content_hash="abc123")

        self.assertEqual(result.status, "already_published")

    def test_add_media_stores_video_metadata(self):
        store = self.make_store()
        result = store.add_media(
            "video",
            "file-video",
            "unique-video",
            None,
            123,
            video_width=1920,
            video_height=1080,
            video_duration=12,
        )

        self.assertEqual(result.media_item.video_width, 1920)
        self.assertEqual(result.media_item.video_height, 1080)
        self.assertEqual(result.media_item.video_duration, 12)

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

    def test_recent_published_media_types_returns_newest_first(self):
        store = self.make_store()
        store.mark_published("unique-1", "photo", source="bot", channel_message_id=1)
        store.mark_published("unique-2", "photo", source="bot", channel_message_id=2)
        store.mark_published("unique-3", "video", source="bot", channel_message_id=3)

        self.assertEqual(store.recent_published_media_types(3), ["video", "photo", "photo"])

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

    def test_get_queued_item_skips_until_publish_count_reaches_gate(self):
        store = self.make_store()
        store.add_media(
            "photo",
            "delayed-file",
            "delayed-unique",
            None,
            123,
            priority=100,
            available_after_publish_count=2,
        )
        store.add_media("photo", "ready-file", "ready-unique", None, 123)

        first_photo = store.get_queued_item("photo", order="chronological")

        self.assertEqual(first_photo.file_unique_id, "ready-unique")

        store.mark_published("published-1", "photo", source="bot")
        store.mark_published("published-2", "video", source="bot")
        delayed_photo = store.get_queued_item("photo", order="chronological")

        self.assertEqual(delayed_photo.file_unique_id, "delayed-unique")

    def test_get_queued_item_random_respects_requested_type(self):
        store = self.make_store()
        store.add_media("photo", "file-photo-1", "unique-photo-1", None, 123)
        store.add_media("video", "file-video-1", "unique-video-1", None, 123)

        item = store.get_queued_item("video", order="random")

        self.assertEqual(item.media_type, "video")

    def test_get_queued_item_can_exclude_failed_candidate(self):
        store = self.make_store()
        first = store.add_media("photo", "file-photo-1", "unique-photo-1", None, 123)
        store.add_media("photo", "file-photo-2", "unique-photo-2", None, 123)

        item = store.get_queued_item("photo", order="chronological", exclude_ids=[first.media_item.id])

        self.assertEqual(item.file_unique_id, "unique-photo-2")


if __name__ == "__main__":
    unittest.main()
