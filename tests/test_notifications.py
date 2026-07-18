import unittest

from telegram_channel_scheduler_bot.notifications import audible_post_positions, scheduled_posts_per_day


class NotificationPlanningTests(unittest.TestCase):
    def test_audible_positions_for_daily_quantities(self):
        cases = {
            1: (1,),
            2: (1, 2),
            3: (1, 2, 3),
            4: (1, 2, 3),
            12: (1, 5, 9),
            24: (1, 9, 17),
        }
        for daily_count, expected in cases.items():
            with self.subTest(daily_count=daily_count):
                self.assertEqual(audible_post_positions(daily_count, 3), expected)

    def test_audible_limit_can_be_reduced_or_disabled(self):
        self.assertEqual(audible_post_positions(12, 2), (1, 7))
        self.assertEqual(audible_post_positions(12, 0), ())

    def test_normal_schedule_plans_twelve_individual_posts(self):
        self.assertEqual(scheduled_posts_per_day(120, 1, "all"), 12)


if __name__ == "__main__":
    unittest.main()
