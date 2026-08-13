from contextlib import suppress
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
import urllib.request
from uuid import uuid4

from telegram_channel_scheduler_bot.channel_status import build_channel_status
from telegram_channel_scheduler_bot.health import HealthHandler
from telegram_channel_scheduler_bot.storage import Store


class ChannelStatusTests(unittest.TestCase):
    def make_store(self) -> Store:
        root = Path(__file__).resolve().parents[1] / ".tmp"
        root.mkdir(exist_ok=True)
        path = root / f"channel-status-{uuid4().hex}.sqlite3"
        self.addCleanup(self.unlink_if_possible, path)
        self.addCleanup(self.unlink_if_possible, path.with_suffix(".sqlite3-journal"))
        store = Store(path)
        store.initialize()
        store.set_setting("channel_id", "@premium")
        store.set_setting("preview_channel_id", "@mouthpreview")
        store.set_setting("timezone", "Europe/Rome")
        store.set_setting("posting_windows", "all")
        store.set_setting("interval_minutes", "60")
        store.set_setting("paused", "false")
        store.set_setting("preview_posts_per_day", "2")
        store.set_setting("preview_posting_times", "10:00,20:00")
        store.set_setting("preview_delay_hours", "48")
        return store

    @staticmethod
    def unlink_if_possible(path: Path) -> None:
        with suppress(OSError):
            path.unlink(missing_ok=True)

    def add_published_photo(self, store: Store, eligible_at: datetime) -> None:
        result = store.add_media("photo", "photo-file", "photo-unique", None, 1, source_id="reddit:one")
        item = result.media_item
        store.mark_published(item.file_unique_id, "photo", source="bot", media_item_id=item.id)
        with store.connect() as connection:
            connection.execute(
                "UPDATE media_items SET preview_eligible_at = ? WHERE id = ?",
                (eligible_at.isoformat(timespec="seconds"), item.id),
            )

    def test_reports_premium_and_due_preview_separately(self) -> None:
        store = self.make_store()
        now = datetime(2026, 8, 13, 18, 30, tzinfo=UTC)
        store.add_media("video", "queued-video", "queued-video-unique", None, 1)
        store.set_setting("next_publish_at", (now + timedelta(minutes=30)).isoformat())
        store.set_setting("preview_last_check_at", now.isoformat())
        self.add_published_photo(store, now - timedelta(hours=1))

        payload = build_channel_status(store, now=now)

        self.assertEqual(payload["premium"]["status"], "active")
        self.assertEqual(payload["premium"]["queued"]["video"], 1)
        self.assertEqual(payload["preview"]["status"], "due")
        self.assertEqual(payload["preview"]["published_today"], 0)
        self.assertEqual(payload["preview"]["due_slots"], 2)
        self.assertEqual(payload["preview"]["eligible_photos"], 1)
        self.assertEqual(payload["preview"]["next_post_at"], now.isoformat(timespec="seconds"))

    def test_preview_explains_48_hour_wait_and_next_eligible_time(self) -> None:
        store = self.make_store()
        now = datetime(2026, 8, 13, 18, 30, tzinfo=UTC)
        eligible_at = now + timedelta(hours=18)
        store.set_setting("next_publish_at", (now + timedelta(minutes=30)).isoformat())
        store.set_setting("preview_last_check_at", now.isoformat())
        self.add_published_photo(store, eligible_at)

        preview = build_channel_status(store, now=now)["preview"]

        self.assertEqual(preview["status"], "waiting")
        self.assertIn("48 ore", preview["reason"])
        self.assertEqual(preview["next_eligible_at"], eligible_at.isoformat(timespec="seconds"))
        self.assertEqual(preview["next_post_at"], eligible_at.isoformat(timespec="seconds"))

    def test_preview_detects_stale_dispatcher(self) -> None:
        store = self.make_store()
        now = datetime(2026, 8, 13, 18, 30, tzinfo=UTC)
        store.set_setting("next_publish_at", (now + timedelta(minutes=30)).isoformat())
        store.set_setting("preview_last_check_at", (now - timedelta(minutes=12)).isoformat())

        preview = build_channel_status(store, now=now)["preview"]

        self.assertEqual(preview["status"], "error")
        self.assertIn("11 minuti", preview["reason"])

    def test_unconfigured_channels_do_not_claim_a_next_post(self) -> None:
        store = self.make_store()
        now = datetime(2026, 8, 13, 18, 30, tzinfo=UTC)
        store.set_setting("channel_id", "")
        store.set_setting("preview_channel_id", "")

        payload = build_channel_status(store, now=now)

        self.assertEqual(payload["premium"]["status"], "not_configured")
        self.assertIsNone(payload["premium"]["next_post_at"])
        self.assertEqual(payload["preview"]["status"], "not_configured")
        self.assertIsNone(payload["preview"]["next_post_at"])

    def test_health_server_exposes_read_only_channel_status(self) -> None:
        store = self.make_store()
        HealthHandler.store = store
        server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)

        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/channels/status",
            timeout=2,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["premium"]["channel_id"], "@premium")
        self.assertEqual(payload["preview"]["channel_id"], "@mouthpreview")


if __name__ == "__main__":
    unittest.main()
