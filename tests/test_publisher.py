from pathlib import Path
from types import SimpleNamespace
import unittest
from uuid import uuid4

from telegram.error import TelegramError

from telegram_channel_scheduler_bot.storage import FAILED, PUBLISHED, QUEUED, Store
from telegram_channel_scheduler_bot.telegram_app import publish_next


class FakeBot:
    def __init__(self) -> None:
        self.photos: list[str] = []

    async def send_photo(self, **kwargs):
        photo = kwargs["photo"]
        self.photos.append(photo)
        if photo == "bad-file":
            raise TelegramError("file is unavailable")
        return SimpleNamespace(message_id=777)

    async def send_video(self, **kwargs):
        raise AssertionError("send_video should not be called in this test")


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> Store:
        temp_root = Path(__file__).resolve().parents[1] / ".tmp"
        temp_root.mkdir(exist_ok=True)
        database_path = temp_root / f"publisher-{uuid4().hex}.sqlite3"
        store = Store(database_path)
        store.initialize()
        self.addAsyncCleanup(self.cleanup_database, database_path)
        store.set_setting("channel_id", "@channel")
        store.set_setting("paused", "false")
        store.set_setting("queue_order", "chronological")
        store.set_setting("photo_ratio", "1")
        store.set_setting("video_ratio", "1")
        store.set_setting("balance_window", "20")
        return store

    async def cleanup_database(self, database_path: Path) -> None:
        for path in (database_path, database_path.with_suffix(".sqlite3-journal")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    async def test_publish_next_skips_failed_item_and_posts_next(self):
        store = self.make_store()
        first = store.add_media("photo", "bad-file", "bad-unique", None, 123)
        second = store.add_media("photo", "good-file", "good-unique", None, 123)
        app = SimpleNamespace(bot=FakeBot(), bot_data={"store": store})

        outcome = await publish_next(app)

        self.assertEqual(outcome.status, "published")
        self.assertEqual(outcome.media_item.id, second.media_item.id)
        self.assertEqual(app.bot.photos, ["bad-file", "good-file"])

        with store.connect() as connection:
            rows = {
                row["file_unique_id"]: dict(row)
                for row in connection.execute(
                    "SELECT file_unique_id, status, failed_attempts FROM media_items ORDER BY id"
                )
            }
        self.assertEqual(rows["bad-unique"]["status"], QUEUED)
        self.assertEqual(rows["bad-unique"]["failed_attempts"], 1)
        self.assertEqual(rows["good-unique"]["status"], PUBLISHED)

    async def test_publish_next_reports_failed_when_no_candidate_can_publish(self):
        store = self.make_store()
        first = store.add_media("photo", "bad-file", "bad-unique", None, 123)
        app = SimpleNamespace(bot=FakeBot(), bot_data={"store": store})

        outcome = await publish_next(app)

        self.assertEqual(outcome.status, "failed")
        self.assertIn(str(first.media_item.id), outcome.message)
        with store.connect() as connection:
            row = connection.execute(
                "SELECT status, failed_attempts FROM media_items WHERE id = ?",
                (first.media_item.id,),
            ).fetchone()
        self.assertEqual(row["status"], QUEUED)
        self.assertEqual(row["failed_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
