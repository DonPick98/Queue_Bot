import unittest
from datetime import UTC, datetime

from telegram_channel_scheduler_bot.scheduling import (
    format_posting_windows,
    is_within_posting_windows,
    next_allowed_datetime,
    parse_posting_windows,
    validate_timezone,
)


class SchedulingTests(unittest.TestCase):
    def test_validate_timezone_accepts_rome(self):
        self.assertEqual(validate_timezone("Europe/Rome"), "Europe/Rome")

    def test_parse_posting_windows_formats_single_window(self):
        windows = parse_posting_windows("10:00-23:30")

        self.assertEqual(len(windows), 1)
        self.assertEqual(format_posting_windows("10:00-23:30"), "10:00-23:30")

    def test_all_day_has_no_windows(self):
        self.assertEqual(parse_posting_windows("all"), [])

    def test_is_within_posting_windows_uses_timezone(self):
        # 08:30 UTC is 10:30 in Rome during summer time.
        value = datetime(2026, 5, 21, 8, 30, tzinfo=UTC)

        self.assertTrue(is_within_posting_windows(value, "Europe/Rome", "10:00-23:30"))

    def test_next_allowed_datetime_moves_to_window_start(self):
        # 06:30 UTC is 08:30 in Rome during summer time.
        value = datetime(2026, 5, 21, 6, 30, tzinfo=UTC)

        next_allowed = next_allowed_datetime(value, "Europe/Rome", "10:00-23:30")

        self.assertEqual(next_allowed, datetime(2026, 5, 21, 8, 0, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
