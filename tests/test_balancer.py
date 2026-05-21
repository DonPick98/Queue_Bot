import unittest

from telegram_channel_scheduler_bot.balancer import PHOTO, VIDEO, choose_media_type


class ChooseMediaTypeTests(unittest.TestCase):
    def test_returns_none_when_queue_is_empty(self):
        self.assertIsNone(choose_media_type({PHOTO: 0, VIDEO: 0}, {}, 1, 1))

    def test_returns_only_available_type(self):
        self.assertEqual(choose_media_type({PHOTO: 3, VIDEO: 0}, {}, 1, 1), PHOTO)
        self.assertEqual(choose_media_type({PHOTO: 0, VIDEO: 3}, {}, 1, 1), VIDEO)

    def test_balances_equal_ratio_against_recent_posts(self):
        choice = choose_media_type(
            {PHOTO: 5, VIDEO: 5},
            {PHOTO: 8, VIDEO: 2},
            1,
            1,
        )
        self.assertEqual(choice, VIDEO)

    def test_respects_weighted_ratio(self):
        choice = choose_media_type(
            {PHOTO: 5, VIDEO: 5},
            {PHOTO: 1, VIDEO: 1},
            2,
            1,
        )
        self.assertEqual(choice, PHOTO)

    def test_tie_avoids_repeating_last_type(self):
        choice = choose_media_type(
            {PHOTO: 5, VIDEO: 5},
            {PHOTO: 1, VIDEO: 1},
            1,
            1,
            last_published_type=PHOTO,
        )
        self.assertEqual(choice, VIDEO)


if __name__ == "__main__":
    unittest.main()
