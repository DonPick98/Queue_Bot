from pathlib import Path
from types import SimpleNamespace
import unittest
from uuid import uuid4

from telegram.error import TelegramError

from telegram_channel_scheduler_bot.storage import FAILED, PUBLISHED, QUEUED, Store
from telegram_channel_scheduler_bot.telegram_app import publish_many, publish_next


class FakeBot:
    def __init__(self) -> None:
        self.photos: list[str] = []
        self.videos: list[str] = []
        self.sent_media: list[tuple[str, str]] = []
        self.notification_attempts: list[tuple[str, bool | None]] = []
        self.failures_remaining: dict[str, int] = {}

    async def send_photo(self, **kwargs):
        photo = kwargs["photo"]
        self.photos.append(photo)
        self.sent_media.append(("photo", photo))
        self.notification_attempts.append((photo, kwargs.get("disable_notification")))
        if self.failures_remaining.get(photo, 0) > 0:
            self.failures_remaining[photo] -= 1
            raise TelegramError("temporary send failure")
        if photo == "bad-file":
            raise TelegramError("file is unavailable")
        return SimpleNamespace(message_id=777)

    async def send_video(self, **kwargs):
        video = kwargs["video"]
        self.videos.append(video)
        self.sent_media.append(("video", video))
        self.notification_attempts.append((video, kwargs.get("disable_notification")))
        return SimpleNamespace(message_id=888)


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
        store.set_setting("interval_minutes", "120")
        store.set_setting("posts_per_run", "1")
        store.set_setting("batch_mode", "fixed")
        store.set_setting("timezone", "Europe/Rome")
        store.set_setting("posting_windows", "all")
        store.set_setting("audible_posts_per_day", "3")
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

    async def test_publish_next_respects_available_after_publish_count(self):
        store = self.make_store()
        delayed = store.add_media(
            "photo",
            "delayed-file",
            "delayed-unique",
            None,
            123,
            priority=100,
            available_after_publish_count=2,
        )
        ready = store.add_media("photo", "ready-file", "ready-unique", None, 123)
        app = SimpleNamespace(bot=FakeBot(), bot_data={"store": store})

        first_outcome = await publish_next(app)

        self.assertEqual(first_outcome.status, "published")
        self.assertEqual(first_outcome.media_item.id, ready.media_item.id)
        self.assertEqual(app.bot.photos, ["ready-file"])

        store.mark_published("external-unique", "video", source="bot")
        second_outcome = await publish_next(app)

        self.assertEqual(second_outcome.status, "published")
        self.assertEqual(second_outcome.media_item.id, delayed.media_item.id)
        self.assertEqual(app.bot.photos, ["ready-file", "delayed-file"])

    async def test_publish_many_does_not_burst_videos_to_repay_photo_history(self):
        store = self.make_store()
        store.set_setting("photo_ratio", "2")
        store.set_setting("video_ratio", "1")
        for index in range(8):
            store.mark_published(f"published-photo-{index}", "photo", source="bot")
        for index in range(3):
            store.add_media("video", f"video-{index}", f"queued-video-{index}", None, 123)
        for index in range(4):
            store.add_media("photo", f"photo-{index}", f"queued-photo-{index}", None, 123)

        app = SimpleNamespace(bot=FakeBot(), bot_data={"store": store})

        outcomes = await publish_many(app, count=4, manual=True)

        self.assertTrue(all(outcome.status == "published" for outcome in outcomes))
        self.assertEqual(
            app.bot.sent_media,
            [
                ("video", "video-0"),
                ("photo", "photo-0"),
                ("photo", "photo-1"),
                ("video", "video-1"),
            ],
        )

    async def test_twelve_automated_posts_are_individual_with_three_even_notifications(self):
        store = self.make_store()
        for index in range(12):
            store.add_media("photo", f"photo-{index + 1}", f"unique-{index + 1}", None, 123)
        app = SimpleNamespace(bot=FakeBot(), bot_data={"store": store})

        with self.assertLogs("telegram_channel_scheduler_bot.telegram_app", level="INFO") as logs:
            outcomes = await publish_many(app, count=12, manual=False)

        self.assertTrue(all(outcome.status == "published" for outcome in outcomes))
        self.assertEqual(len(app.bot.photos), 12)
        self.assertEqual(
            [silent for _, silent in app.bot.notification_attempts],
            [False, True, True, True, False, True, True, True, False, True, True, True],
        )
        self.assertEqual(sum("notification=normal" in entry for entry in logs.output), 3)
        self.assertEqual(sum("notification=silent" in entry for entry in logs.output), 9)

    async def test_notification_choice_is_preserved_when_send_retries(self):
        store = self.make_store()
        flaky = store.add_media("photo", "flaky-file", "flaky-unique", None, 123)
        store.add_media("photo", "good-file", "good-unique", None, 123)
        bot = FakeBot()
        bot.failures_remaining["flaky-file"] = 1
        app = SimpleNamespace(bot=bot, bot_data={"store": store})

        first = await publish_next(app)
        second = await publish_next(app)

        self.assertEqual(first.status, "published")
        self.assertEqual(second.status, "published")
        self.assertEqual(
            bot.notification_attempts,
            [("flaky-file", False), ("good-file", True), ("flaky-file", False)],
        )
        retried = store.find_media_by_id(flaky.media_item.id)
        self.assertFalse(retried.notification_silent)
        self.assertEqual(retried.notification_position, 1)

    async def test_manual_paid_channel_post_keeps_existing_notification_behaviour(self):
        store = self.make_store()
        store.add_media("video", "manual-video", "manual-unique", None, 123)
        app = SimpleNamespace(bot=FakeBot(), bot_data={"store": store})

        outcome = await publish_next(app, manual=True)

        self.assertEqual(outcome.status, "published")
        self.assertEqual(app.bot.notification_attempts, [("manual-video", None)])


if __name__ == "__main__":
    unittest.main()
