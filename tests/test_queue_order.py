import unittest

from telegram_channel_scheduler_bot.queue_order import parse_queue_order


class QueueOrderTests(unittest.TestCase):
    def test_parse_queue_order_random_aliases(self):
        self.assertEqual(parse_queue_order("random"), "random")
        self.assertEqual(parse_queue_order("casuale"), "random")

    def test_parse_queue_order_chronological_aliases(self):
        self.assertEqual(parse_queue_order("chronological"), "chronological")
        self.assertEqual(parse_queue_order("cronologico"), "chronological")

    def test_parse_queue_order_rejects_unknown_value(self):
        with self.assertRaises(ValueError):
            parse_queue_order("banana")


if __name__ == "__main__":
    unittest.main()
