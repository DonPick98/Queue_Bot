from datetime import UTC, datetime, timedelta
from io import BytesIO
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import unittest

from PIL import Image, ImageDraw

from telegram_channel_scheduler_bot.media_quality import inspect_photo, inspect_video, photo_distance
from telegram_channel_scheduler_bot.storage import Store


def encoded_photo(size, seed, quality=90):
    image = Image.new("RGB", (640, 480))
    draw = ImageDraw.Draw(image)
    palette = random.Random(seed)
    for y in range(0, 480, 40):
        for x in range(0, 640, 40):
            color = tuple(palette.randrange(256) for _ in range(3))
            draw.rectangle((x, y, x + 40, y + 40), fill=color)
    buffer = BytesIO()
    image.resize(size, Image.Resampling.LANCZOS).save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


class MediaQualityTests(unittest.TestCase):
    def test_photo_reencode_is_similar_but_different_pattern_is_not(self):
        original = inspect_photo(encoded_photo((640, 480), 17))
        smaller = inspect_photo(encoded_photo((320, 240), 17, quality=67))
        different = inspect_photo(encoded_photo((640, 480), 78))
        self.assertLessEqual(photo_distance(original.photo_signature, smaller.photo_signature), 6)
        self.assertGreater(photo_distance(original.photo_signature, different.photo_signature), 6)
        self.assertIn("Risoluzione bassa", smaller.warning)

    def test_possible_duplicates_are_advisory_and_old_signatures_expire(self):
        with tempfile.TemporaryDirectory() as workdir:
            store = Store(Path(workdir) / "bot.sqlite3")
            store.initialize()
            signature = inspect_photo(encoded_photo((640, 480), 17)).photo_signature
            photo = store.add_media(
                "photo", "first", "id-first", None, None, photo_signature=signature,
                media_width=640, media_height=480,
            ).media_item
            new = store.add_media(
                "photo", "second", "id-second", None, None, photo_signature=signature,
                media_width=320, media_height=240,
            ).media_item
            self.assertEqual(new.status, "queued")
            self.assertIn(f"Simile alla foto #{photo.id} in coda", new.quality_warning)
            self.assertIn(f"Risoluzione maggiore nella foto #{photo.id}", new.quality_warning)
            past = (datetime.now(UTC) - timedelta(days=91)).isoformat()
            store.mark_published("id-first", "photo", "bot", channel_message_id=41, published_at=past)
            store.mark_published("id-second", "photo", "bot", channel_message_id=42, published_at=past)
            third = store.add_media(
                "photo", "third", "id-third", None, None, photo_signature=signature,
            ).media_item
            self.assertIsNone(third.quality_warning)
            with store.connect() as connection:
                retained = connection.execute(
                    "SELECT id FROM media_items WHERE photo_signature IS NOT NULL"
                ).fetchall()
            self.assertEqual([row["id"] for row in retained], [third.id])

    @unittest.skipUnless(
        hasattr(os, "memfd_create") and shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "Video inspection requires Linux and FFmpeg",
    )
    def test_real_video_is_sampled_from_memory_and_garbage_is_rejected(self):
        clip = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=8",
             "-t", "2.5", "-c:v", "mpeg4", "-movflags", "frag_keyframe+empty_moov",
             "-f", "mp4", "pipe:1"],
            check=True, capture_output=True, timeout=10,
        ).stdout
        result = inspect_video(clip)
        self.assertFalse(result.error)
        self.assertIn("320×240", result.warning)
        self.assertTrue(inspect_video(b"not an mp4").error)
