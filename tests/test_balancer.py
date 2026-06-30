import unittest

from telegram_channel_scheduler_bot.balancer import PHOTO, VIDEO, choose_media_type


class ChooseMediaTypeTests(unittest.TestCase):
    def test_returns_none_when_queue_is_empty(self):
        self.assertIsNone(choose_media_type({PHOTO: 0, VIDEO: 0}, [], 1, 1))

    def test_returns_only_available_type(self):
        self.assertEqual(choose_media_type({PHOTO: 3, VIDEO: 0}, [], 1, 1), PHOTO)
        self.assertEqual(choose_media_type({PHOTO: 0, VIDEO: 3}, [], 1, 1), VIDEO)

    def test_equal_ratio_alternates_after_video(self):
        choice = choose_media_type(
            {PHOTO: 5, VIDEO: 5},
            [VIDEO],
            1,
            1,
        )
        self.assertEqual(choice, PHOTO)

    def test_starts_with_larger_ratio_when_there_is_no_history(self):
        choice = choose_media_type(
            {PHOTO: 5, VIDEO: 5},
            [],
            2,
            1,
        )
        self.assertEqual(choice, PHOTO)

    def test_after_video_respects_configured_photo_quota(self):
        choice = choose_media_type(
            {PHOTO: 5, VIDEO: 5},
            [PHOTO, VIDEO, PHOTO, PHOTO, PHOTO],
            2,
            1,
        )
        self.assertEqual(choice, PHOTO)

    def test_chooses_video_after_photo_quota(self):
        choice = choose_media_type(
            {PHOTO: 5, VIDEO: 5},
            [PHOTO, PHOTO, VIDEO],
            2,
            1,
        )
        self.assertEqual(choice, VIDEO)

    def test_does_not_repay_all_missing_videos_after_first_video(self):
        choice = choose_media_type(
            {PHOTO: 20, VIDEO: 5},
            [VIDEO, *([PHOTO] * 20)],
            4,
            1,
        )
        self.assertEqual(choice, PHOTO)


if __name__ == "__main__":
    unittest.main()
