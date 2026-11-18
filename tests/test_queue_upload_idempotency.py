from email.message import Message
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telegram_channel_scheduler_bot.health import HealthHandler, _multipart_body
from telegram_channel_scheduler_bot.storage import Store


class QueueUploadIdempotencyTests(unittest.TestCase):
    key = "berry-live:stable-upload-key"
    receipt = {"result": {"message_id": 11, "photo": [{"file_id": "file", "file_unique_id": "unique"}]}}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "bot.sqlite3")
        self.store.initialize()
        environment = patch.dict("os.environ", {"QUEUE_API_STAGING_CHAT_ID": "42", "QUEUE_API_DELETE_STAGING": "false"})
        environment.start()
        self.addCleanup(environment.stop)

    def upload(self, fingerprint="stable"):
        handler = HealthHandler.__new__(HealthHandler)
        handler.store = self.store
        handler.bot_token = "fixture"
        media, boundary = _multipart_body({"content_fingerprint": fingerprint}, "media", "fixture.jpg", b"fixture")
        handler.headers = Message()
        handler.headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        handler.headers["Content-Length"] = str(len(media))
        handler.headers["Idempotency-Key"] = self.key
        handler.rfile = BytesIO(media)
        return handler._queue_upload("photo")

    def test_lost_http_response_reuses_confirmed_result(self):
        with patch("telegram_channel_scheduler_bot.health._telegram_api", return_value=self.receipt) as send:
            first = self.upload()
            self.store = Store(self.store.path)
            self.assertEqual(first, self.upload())
            self.assertEqual(self.upload("different")[0], 409)
            send.assert_called_once()
        self.assertEqual(first[0], 200)

    def test_retry_after_queue_write_interruption_uses_telegram_receipt(self):
        with patch("telegram_channel_scheduler_bot.health._telegram_api", return_value=self.receipt) as send:
            with patch.object(self.store, "add_media", side_effect=RuntimeError("interrupted")):
                with self.assertRaises(RuntimeError):
                    self.upload()
            self.assertEqual(self.upload()[0], 200)
            send.assert_called_once()

    def test_existing_media_from_before_upgrade_is_reused_without_staging(self):
        saved = self.store.add_media("photo", "file", "old", None, None, content_fingerprint="stable")
        with patch("telegram_channel_scheduler_bot.health._telegram_api") as send:
            status, result = self.upload()
            self.assertEqual(status, 200)
            self.assertEqual(result["media_id"], saved.media_item.id)
            send.assert_not_called()

    def test_unknown_telegram_outcome_does_not_resend(self):
        with patch("telegram_channel_scheduler_bot.health._telegram_api", side_effect=TimeoutError()) as send:
            self.assertEqual(self.upload()[0], 502)
            self.assertEqual(self.upload()[0], 503)
            with patch("telegram_channel_scheduler_bot.health.time.time", return_value=9999999999):
                self.assertEqual(self.upload()[1]["error"], "staging_outcome_unknown")
            send.assert_called_once()
