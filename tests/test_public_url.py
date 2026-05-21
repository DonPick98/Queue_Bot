import unittest
from email.message import Message

from telegram_channel_scheduler_bot.health import infer_public_base_url, normalize_public_base_url


class PublicUrlTests(unittest.TestCase):
    def test_infers_forwarded_public_url(self):
        headers = Message()
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Forwarded-Host"] = "queue-bot.example.com"
        headers["Host"] = "127.0.0.1:8080"

        self.assertEqual(infer_public_base_url(headers), "https://queue-bot.example.com")

    def test_ignores_private_host(self):
        headers = Message()
        headers["Host"] = "127.0.0.1:8080"

        self.assertIsNone(infer_public_base_url(headers))

    def test_normalize_adds_https_and_removes_trailing_slash(self):
        self.assertEqual(
            normalize_public_base_url("queue-bot.example.com/"),
            "https://queue-bot.example.com",
        )


if __name__ == "__main__":
    unittest.main()
