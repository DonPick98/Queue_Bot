import unittest
from datetime import UTC, datetime, timedelta

from telegram_channel_scheduler_bot.posting_plan import (
    auto_posts_per_run,
    cycles_in_hours,
    estimate_queue_coverage,
    initial_next_publish_at,
    seconds_until_next_publish,
)


class PostingPlanTests(unittest.TestCase):
    def test_auto_batch_thresholds(self):
        self.assertEqual(auto_posts_per_run(20), 1)
        self.assertEqual(auto_posts_per_run(21), 2)
        self.assertEqual(auto_posts_per_run(40), 2)
        self.assertEqual(auto_posts_per_run(41), 3)

    def test_cycles_in_24_hours_rounds_up(self):
        self.assertEqual(cycles_in_hours(60), 24)
        self.assertEqual(cycles_in_hours(120), 12)
        self.assertEqual(cycles_in_hours(90), 16)

    def test_fixed_batch_coverage(self):
        coverage = estimate_queue_coverage(
            queued_total=23,
            interval_minutes=60,
            batch_mode="fixed",
            posts_per_run=1,
        )

        self.assertFalse(coverage.covers)
        self.assertEqual(coverage.required_posts, 24)
        self.assertEqual(coverage.missing_posts, 1)

    def test_fixed_multi_post_batch_coverage(self):
        coverage = estimate_queue_coverage(
            queued_total=35,
            interval_minutes=120,
            batch_mode="fixed",
            posts_per_run=3,
        )

        self.assertFalse(coverage.covers)
        self.assertEqual(coverage.required_posts, 36)
        self.assertEqual(coverage.missing_posts, 1)

    def test_auto_batch_coverage_simulates_progressive_consumption(self):
        coverage = estimate_queue_coverage(
            queued_total=28,
            interval_minutes=60,
            batch_mode="auto",
            posts_per_run=1,
        )

        self.assertTrue(coverage.covers)
        self.assertEqual(coverage.required_posts, 28)
        self.assertEqual(coverage.missing_posts, 0)

    def test_initial_next_publish_uses_last_publish_when_available(self):
        now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
        last_published = datetime(2026, 5, 20, 10, 30, tzinfo=UTC)

        self.assertEqual(
            initial_next_publish_at(now, 120, last_published),
            datetime(2026, 5, 20, 12, 30, tzinfo=UTC),
        )

    def test_initial_next_publish_starts_from_now_without_history(self):
        now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)

        self.assertEqual(
            initial_next_publish_at(now, 60, None),
            datetime(2026, 5, 20, 13, 0, tzinfo=UTC),
        )

    def test_seconds_until_next_publish_waits_if_future(self):
        now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
        next_publish = now + timedelta(minutes=30)

        self.assertEqual(seconds_until_next_publish(next_publish, now), 1800)

    def test_seconds_until_next_publish_is_asap_if_missed(self):
        now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
        next_publish = now - timedelta(minutes=30)

        self.assertEqual(seconds_until_next_publish(next_publish, now), 5)


if __name__ == "__main__":
    unittest.main()
