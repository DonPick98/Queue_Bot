from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from PIL import Image
from telegram.error import TelegramError

from telegram_channel_scheduler_bot.preview import (
    PreviewConversionScheduler,
    PreviewEligibilityService,
    PreviewPublisher,
    PreviewSelector,
    WeeklyPreviewRecap,
    build_watermarked_photo,
    prepare_preview_photo,
    preview_welcome_version,
    preview_dispatcher_job,
    set_preview_welcome,
    sync_preview_memberpass_links,
    build_mosaic,
    due_preview_slots,
    ensure_preview_welcome,
    local_day_bounds,
    recap_text,
    select_mosaic_candidates,
    upgrade_text,
    welcome_text,
)
from telegram_channel_scheduler_bot.storage import Store


class FakeTelegramFile:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def download_as_bytearray(self):
        return bytearray(self.content)


class FakePreviewBot:
    def __init__(self) -> None:
        self.photos: list[dict[str, object]] = []
        self.failures_remaining: dict[str, int] = {}
        self.unavailable_file_ids: set[str] = set()
        self.last_file_id = ""
        self.downloaded_file_ids: list[str] = []
        source = BytesIO()
        Image.new("RGB", (1200, 1600), "#7d657d").save(source, format="JPEG")
        self.photo_content = source.getvalue()

    async def get_file(self, file_id: str):
        if file_id in self.unavailable_file_ids:
            raise TelegramError("file is no longer available")
        self.last_file_id = file_id
        self.downloaded_file_ids.append(file_id)
        return FakeTelegramFile(self.photo_content)

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)
        file_id = self.last_file_id or str(kwargs["photo"])
        if self.failures_remaining.get(file_id, 0):
            self.failures_remaining[file_id] -= 1
            raise TelegramError("temporary preview failure")
        return SimpleNamespace(message_id=1000 + len(self.photos))


class FakeWelcomeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.pins: list[dict[str, object]] = []
        self.deletions: list[dict[str, object]] = []
        self.edits: list[dict[str, object]] = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=2001)

    async def pin_chat_message(self, **kwargs):
        self.pins.append(kwargs)

    async def delete_message(self, **kwargs):
        self.deletions.append(kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        self.edits.append(kwargs)


class FakeRecoverBot(FakePreviewBot):
    def __init__(self) -> None:
        super().__init__()
        self.forwarded: list[dict[str, object]] = []
        self.deleted: list[dict[str, object]] = []

    async def get_file(self, file_id: str):
        if file_id == "file-1":
            raise TelegramError("Wrong file_id or the file is temporarily unavailable")
        return await super().get_file(file_id)

    async def forward_message(self, **kwargs):
        self.forwarded.append(kwargs)
        return SimpleNamespace(
            message_id=811,
            photo=[SimpleNamespace(file_id="fresh-file-1")],
        )

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)



class FakeVideoRecoverBot(FakePreviewBot):
    def __init__(self) -> None:
        super().__init__()
        self.forwarded: list[dict[str, object]] = []
        self.deleted: list[dict[str, object]] = []

    async def forward_message(self, **kwargs):
        self.forwarded.append(kwargs)
        return SimpleNamespace(
            message_id=912,
            video=SimpleNamespace(
                thumbnail=SimpleNamespace(file_id="fresh-video-thumbnail")
            ),
        )

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)

class PreviewTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> Store:
        root = Path(__file__).resolve().parents[1] / ".tmp"
        root.mkdir(exist_ok=True)
        path = root / f"preview-{uuid4().hex}.sqlite3"
        store = Store(path)
        store.initialize()
        store.set_setting("preview_channel_id", "@mouthpreview")
        store.set_setting("preview_posts_per_day", "2")
        store.set_setting("preview_posting_times", "10:00,20:00")
        store.set_setting("preview_attribution", "@MouthPreview · Full daily feed ↓")
        store.set_setting("timezone", "Europe/Rome")
        store.set_setting("preview_memberpass_link_version", "v1")
        self.addAsyncCleanup(self.cleanup_database, path)
        return store

    async def cleanup_database(self, path: Path) -> None:
        for candidate in (path, path.with_suffix(".sqlite3-journal")):
            candidate.unlink(missing_ok=True)

    def add_published(
        self,
        store: Store,
        index: int,
        *,
        media_type: str = "photo",
        source_id: str | None = None,
        caption_html: str | None = None,
    ):
        result = store.add_media(
            media_type,
            f"file-{index}",
            f"unique-{index}",
            caption_html,
            1,
            source_id=source_id or f"source-{index}",
            source_label=source_id or f"Source {index}",
            derived_tags=[f"tag-{index}"],
            media_width=1200,
            media_height=1600,
        )
        store.mark_published(
            result.media_item.file_unique_id,
            media_type,
            source="bot",
            media_item_id=result.media_item.id,
        )
        with store.connect() as connection:
            connection.execute(
                "UPDATE media_items SET preview_eligible_at = ? WHERE id = ?",
                ("2026-07-01T00:00:00+00:00", result.media_item.id),
            )
        return result.media_item

    def test_preview_eligibility_is_exactly_forty_eight_hours_after_premium(self):
        premium_at = datetime(2026, 7, 10, 12, 30, tzinfo=UTC)

        eligible_at = PreviewEligibilityService.eligible_at(premium_at)

        self.assertEqual(eligible_at, datetime(2026, 7, 12, 12, 30, tzinfo=UTC))

    def test_public_channel_copy_is_english(self):
        event = SimpleNamespace(preview_count=14, premium_count=84)

        self.assertIn("You'll get two hand-picked images", welcome_text())
        self.assertIn("does not publish videos", welcome_text())
        copy = recap_text(event)
        self.assertEqual(upgrade_text(event), copy)
        self.assertIn("You've seen 14 previews this week", copy)
        self.assertIn("Mouth Aesthethics published 84 posts (videos too!)", copy)
        self.assertIn("first month for <b>$1</b>", copy)
        self.assertIn("then <b>$3/month</b>", copy)
        self.assertNotIn("6 previews", copy)

    def test_due_slots_follow_project_timezone(self):
        self.assertEqual(due_preview_slots(datetime(2026, 7, 13, 7, 59, tzinfo=UTC), "Europe/Rome", "10:00,20:00"), 0)
        self.assertEqual(due_preview_slots(datetime(2026, 7, 13, 8, 0, tzinfo=UTC), "Europe/Rome", "10:00,20:00"), 1)
        self.assertEqual(due_preview_slots(datetime(2026, 7, 13, 18, 0, tzinfo=UTC), "Europe/Rome", "10:00,20:00"), 2)

    async def test_english_welcome_replaces_previous_pinned_copy(self):
        store = self.make_store()
        store.set_setting("preview_welcome_message_id", "144")
        store.set_setting("preview_welcome_link_version", "v1")
        bot = FakeWelcomeBot()

        message = await ensure_preview_welcome(SimpleNamespace(bot=bot), store)

        self.assertEqual(message.message_id, 2001)
        self.assertIn("Welcome to Mouth Preview", bot.messages[0]["text"])
        self.assertNotIn("Riceverai", bot.messages[0]["text"])
        self.assertTrue(bot.messages[0]["disable_notification"])
        self.assertEqual(bot.pins[0]["message_id"], 2001)
        self.assertTrue(bot.pins[0]["disable_notification"])
        self.assertEqual(bot.deletions[0]["message_id"], 144)
        self.assertEqual(store.get_setting("preview_welcome_message_id"), "2001")
        stored_version = store.get_setting("preview_welcome_version")
        self.assertEqual(stored_version, preview_welcome_version(store))
        self.assertTrue(stored_version.startswith("en-v1:v1:default:"))

    async def test_memberpass_link_sync_updates_pinned_and_all_sent_recaps(self):
        store = self.make_store()
        store.set_setting("preview_memberpass_url", "https://my.subscriby.net/306354e7c4")
        store.set_setting("preview_memberpass_link_version", "v2")
        store.set_setting("preview_welcome_message_id", "144")
        store.set_setting("preview_welcome_version", "en-v1:v1:default:old")
        first = store.create_preview_conversion_event(
            "weekly_recap",
            "weekly:2026-W30",
            "2026-07-26T19:00:00+00:00",
            "2026-07-20T00:00:00+00:00",
            "2026-07-26T19:00:00+00:00",
            84,
            14,
            (),
            "v1",
        )
        second = store.create_preview_conversion_event(
            "weekly_recap",
            "weekly:2026-W31",
            "2026-08-02T19:00:00+00:00",
            "2026-07-27T00:00:00+00:00",
            "2026-08-02T19:00:00+00:00",
            80,
            14,
            (),
            "v1",
        )
        store.mark_preview_conversion_sent(first.id, 4001)
        store.mark_preview_conversion_sent(second.id, 4002)
        bot = FakeWelcomeBot()

        synced = await sync_preview_memberpass_links(SimpleNamespace(bot=bot), store)

        self.assertTrue(synced)
        self.assertEqual(
            [edit["message_id"] for edit in bot.edits],
            [144, 4001, 4002],
        )
        for edit in bot.edits:
            self.assertEqual(
                edit["reply_markup"].inline_keyboard[0][0].url,
                "https://my.subscriby.net/306354e7c4",
            )
        self.assertEqual(store.get_setting("preview_memberpass_synced_version"), "v2")
        self.assertTrue(store.get_setting("preview_welcome_version").startswith("en-v1:v2:"))
        self.assertEqual(store.preview_recap_events_needing_link_sync("v2"), [])

    async def test_preview_publishes_two_individual_photos_and_never_video(self):
        store = self.make_store()
        self.add_published(store, 1)
        self.add_published(store, 2)
        self.add_published(store, 3)
        self.add_published(store, 4, media_type="video")
        bot = FakePreviewBot()
        app = SimpleNamespace(bot=bot)

        count = await PreviewPublisher(store).publish_due(
            app,
            datetime(2026, 7, 13, 19, 0, tzinfo=UTC),
        )

        self.assertEqual(count, 2)
        self.assertEqual(len(bot.photos), 2)
        self.assertEqual(
            [call["disable_notification"] for call in bot.photos],
            [False, True],
        )
        self.assertNotIn("file-4", bot.downloaded_file_ids)
        self.assertTrue(all("caption" not in call for call in bot.photos))
        self.assertTrue(all(isinstance(call["photo"], BytesIO) for call in bot.photos))

    async def test_preview_credits_only_reddit_creator_username(self):
        store = self.make_store()
        self.add_published(
            store,
            1,
            source_id="reddit:r-example",
            caption_html="Caption visible only on the premium channel\nu/Creator_Name-7",
        )
        self.add_published(
            store,
            2,
            source_id="pinterest:example",
            caption_html="u/not-a-reddit-credit",
        )
        bot = FakePreviewBot()

        await PreviewPublisher(store).publish_due(
            SimpleNamespace(bot=bot),
            datetime(2026, 7, 13, 19, 0, tzinfo=UTC),
        )

        captions = {call.get("caption") for call in bot.photos}
        self.assertEqual(captions, {"u/Creator_Name-7", None})

    async def test_preview_notification_choice_survives_retry(self):
        store = self.make_store()
        item = self.add_published(store, 1)
        bot = FakePreviewBot()
        bot.failures_remaining[item.file_id] = 1
        app = SimpleNamespace(bot=bot)
        now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)

        first = await PreviewPublisher(store).publish_due(app, now)
        second = await PreviewPublisher(store).publish_due(app, now)

        self.assertEqual((first, second), (0, 1))
        self.assertEqual(
            [call["disable_notification"] for call in bot.photos],
            [False, False],
        )
        retried = store.find_media_by_id(item.id)
        self.assertFalse(retried.preview_notification_silent)
        self.assertEqual(retried.preview_variant, "full_photo")
        self.assertEqual(retried.preview_memberpass_link_version, "v1")

    async def test_broken_photo_does_not_block_other_due_preview_posts(self):
        store = self.make_store()
        broken = self.add_published(store, 1)
        second = self.add_published(store, 2)
        third = self.add_published(store, 3)
        bot = FakePreviewBot()
        bot.unavailable_file_ids.add(broken.file_id)
        app = SimpleNamespace(bot=bot)

        count = await PreviewPublisher(store).publish_due(
            app,
            datetime(2026, 7, 13, 19, 0, tzinfo=UTC),
        )

        self.assertEqual(count, 2)
        self.assertEqual(
            {item.id for item in (second, third)},
            {
                item.id
                for item in store.preview_history_between(
                    "2026-07-13T00:00:00+00:00",
                    "2026-07-14T23:59:59+00:00",
                )
            },
        )
        self.assertEqual(store.find_media_by_id(broken.id).preview_failed_attempts, 1)

    def test_selector_never_reuses_a_source_on_same_day(self):
        store = self.make_store()
        first = self.add_published(store, 1, source_id="same")
        second = self.add_published(store, 2, source_id="same")
        other = self.add_published(store, 3, source_id="other")
        selector = PreviewSelector()

        selected = selector.choose(
            [store.find_media_by_id(second.id), store.find_media_by_id(other.id)],
            [store.find_media_by_id(first.id)],
            store.find_media_by_id(first.id),
        )

        self.assertEqual(selected.id, other.id)

    def test_sixth_preview_does_not_create_a_conversion_event(self):
        store = self.make_store()
        items = [self.add_published(store, index) for index in range(1, 7)]
        for index, item in enumerate(items, start=1):
            store.mark_preview_published(
                item.id,
                2000 + index,
                f"2026-07-{index:02d}T12:00:00+00:00",
            )

        event = PreviewConversionScheduler(store).ensure_upgrade_event(
            "2026-07-07T00:00:00+00:00"
        )

        self.assertIsNone(event)
        self.assertEqual(
            store.pending_preview_conversion_events("2026-07-07T00:00:00+00:00"),
            [],
        )

    def test_mosaic_teases_without_clearly_revealing_nine_to_twelve_thumbnails(self):
        images: list[bytes] = []
        for index in range(12):
            source = BytesIO()
            Image.new("RGB", (640, 480), (200 - index * 3, 150, 100)).save(
                source,
                format="JPEG",
            )
            images.append(source.getvalue())

        for count in range(9, 13):
            with self.subTest(count=count):
                mosaic = build_mosaic(images[:count], tile_size=100, video_indices=(1,))
                with Image.open(mosaic) as rendered:
                    expected_rows = 3 if count == 9 else 4
                    self.assertEqual(rendered.size, (300, expected_rows * 100))
                    red, green, blue = rendered.getpixel((50, 50))
                    self.assertGreater(red, 100)
                    self.assertLess(red, 140)
                    self.assertGreater(green, 70)
                    self.assertLess(green, 105)
                    self.assertGreater(blue, 40)
                    self.assertLess(blue, 80)

    def test_mosaic_selection_spans_the_week_and_includes_video(self):
        store = self.make_store()
        items = [
            self.add_published(
                store,
                index,
                media_type="video" if index % 5 == 0 else "photo",
            )
            for index in range(1, 26)
        ]

        selected = select_mosaic_candidates(
            [store.find_media_by_id(item.id) for item in items],
            display_limit=12,
            candidate_limit=20,
        )

        self.assertEqual(len(selected), 20)
        primary = selected[:12]
        self.assertEqual(sum(item.media_type == "video" for item in primary), 3)
        self.assertEqual(primary[0].id, items[0].id)
        self.assertEqual(primary[-1].id, items[-1].id)


    def test_watermark_is_subtle_bottom_left_and_limits_working_size(self):
        source = BytesIO()
        Image.new("RGB", (3000, 2000), (125, 125, 125)).save(source, format="JPEG")

        output = build_watermarked_photo(
            source.getvalue(),
            text="@MouthPreview",
            opacity=82,
            max_dimension=1000,
        )

        with Image.open(output) as rendered:
            self.assertEqual(rendered.size, (1000, 667))
            extrema = rendered.convert("L").getextrema()
            self.assertLess(extrema[0], 110)
            self.assertGreater(extrema[1], 145)
            self.assertAlmostEqual(rendered.getpixel((950, 40))[0], 125, delta=8)


    def test_watermark_stays_bottom_left_across_aspect_ratios(self):
        for size in ((800, 1200), (1200, 800), (900, 900), (180, 1200)):
            with self.subTest(size=size):
                source = BytesIO()
                Image.new("RGB", size, (125, 125, 125)).save(source, format="JPEG")

                output = build_watermarked_photo(source.getvalue(), max_dimension=1600)
                with Image.open(output) as rendered:
                    width, height = rendered.size
                    grayscale = rendered.convert("L")
                    lower_left = grayscale.crop((0, int(height * 0.55), int(width * 0.72), height))
                    self.assertLess(lower_left.getextrema()[0], 110)
                    self.assertGreater(lower_left.getextrema()[1], 145)
                    self.assertAlmostEqual(grayscale.getpixel((width - 20, 20)), 125, delta=8)

    def test_watermark_scale_is_relative_to_image_resolution(self):
        heights: list[int] = []
        for size in ((800, 1200), (1600, 2400)):
            source = BytesIO()
            Image.new("RGB", size, (125, 125, 125)).save(source, format="PNG")
            output = build_watermarked_photo(
                source.getvalue(),
                opacity=128,
                scale_percent=10,
                max_dimension=3000,
            )
            with Image.open(output) as rendered:
                grayscale = rendered.convert("L")
                mask = grayscale.point(lambda value: 255 if value < 105 or value > 145 else 0)
                bounds = mask.getbbox()
                self.assertIsNotNone(bounds)
                heights.append(bounds[3] - bounds[1])
        self.assertGreater(heights[1] / heights[0], 1.8)
        self.assertLess(heights[1] / heights[0], 2.2)

    async def test_invalid_file_id_is_recovered_from_original_premium_message(self):
        store = self.make_store()
        store.set_setting("channel_id", "@premium")
        item = self.add_published(store, 1)
        with store.connect() as connection:
            connection.execute(
                "UPDATE media_items SET channel_message_id = 501 WHERE id = ?",
                (item.id,),
            )
        bot = FakeRecoverBot()

        rendered = await prepare_preview_photo(
            SimpleNamespace(bot=bot),
            store,
            store.find_media_by_id(item.id),
            force_watermark=True,
            staging_chat_id=99,
        )

        self.assertIsInstance(rendered, BytesIO)
        self.assertEqual(store.find_media_by_id(item.id).file_id, "fresh-file-1")
        self.assertEqual(bot.forwarded[0]["message_id"], 501)
        self.assertEqual(bot.deleted[0]["message_id"], 811)

    async def test_manual_preview_button_path_publishes_one_eligible_photo(self):
        store = self.make_store()
        item = self.add_published(store, 1)
        bot = FakePreviewBot()

        published = await PreviewPublisher(store).publish_one_now(
            SimpleNamespace(bot=bot),
            datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
            staging_chat_id=99,
        )

        self.assertEqual(published.id, item.id)
        self.assertEqual(len(bot.photos), 1)
        stored = store.find_media_by_id(item.id)
        self.assertIsNotNone(stored.preview_published_at)
        self.assertEqual(stored.preview_publish_source, "manual")
        self.assertFalse(bot.photos[0]["disable_notification"])

    async def test_manual_preview_is_extra_and_does_not_consume_scheduled_slots(self):
        store = self.make_store()
        self.add_published(store, 1)
        self.add_published(store, 2)
        self.add_published(store, 3)
        bot = FakePreviewBot()
        app = SimpleNamespace(bot=bot)
        now = datetime(2026, 7, 13, 19, 0, tzinfo=UTC)

        manual_item = await PreviewPublisher(store).publish_one_now(app, now)
        scheduled_count = await PreviewPublisher(store).publish_due(app, now)
        start, end, _ = local_day_bounds(now, "Europe/Rome")

        self.assertIsNotNone(manual_item)
        self.assertEqual(scheduled_count, 2)
        self.assertEqual(store.preview_count_between(start.isoformat(), end.isoformat()), 3)
        self.assertEqual(store.scheduled_preview_count_between(start.isoformat(), end.isoformat()), 2)

    async def test_welcome_maintenance_failure_does_not_block_preview_publishing(self):
        store = self.make_store()
        application = SimpleNamespace(bot_data={"store": store}, bot=FakePreviewBot())
        context = SimpleNamespace(application=application)

        with (
            patch(
                "telegram_channel_scheduler_bot.preview.ensure_preview_welcome",
                new=AsyncMock(side_effect=TelegramError("pin unavailable")),
            ),
            patch.object(PreviewPublisher, "publish_due", new=AsyncMock(return_value=1)) as publish_due,
            patch.object(WeeklyPreviewRecap, "ensure_event", return_value=None),
            patch.object(
                PreviewConversionScheduler,
                "send_pending",
                new=AsyncMock(return_value=0),
            ),
        ):
            await preview_dispatcher_job(context)

        publish_due.assert_awaited_once_with(application)
        self.assertEqual(store.get_setting("preview_last_status"), "published")
        self.assertEqual(store.get_setting("preview_last_error"), "")
        self.assertIn("pin unavailable", store.get_setting("preview_last_warning"))
    async def test_custom_welcome_is_plain_text_with_memberpass_button(self):
        store = self.make_store()
        set_preview_welcome(store, "custom", "Custom <b>plain</b> welcome")
        bot = FakeWelcomeBot()

        await ensure_preview_welcome(SimpleNamespace(bot=bot), store, force=True)

        self.assertEqual(bot.messages[0]["text"], "Custom <b>plain</b> welcome")
        self.assertIsNone(bot.messages[0]["parse_mode"])
        self.assertIsNotNone(bot.messages[0]["reply_markup"])
        self.assertIn(":custom:", store.get_setting("preview_welcome_version"))

    async def test_forced_weekly_recap_uses_real_counts_and_prevents_duplicate(self):
        store = self.make_store()
        items = [self.add_published(store, index) for index in range(1, 13)]
        now = datetime.now(UTC)
        store.mark_preview_published(
            items[0].id,
            3001,
            now.isoformat(timespec="seconds"),
        )
        bot = FakePreviewBot()
        app = SimpleNamespace(bot=bot)

        message, event = await WeeklyPreviewRecap(store).force_send(app, now=now)
        repeated_message, repeated_event = await WeeklyPreviewRecap(store).force_send(app, now=now)

        self.assertIsNotNone(message)
        self.assertEqual(event.premium_count, 12)
        self.assertEqual(event.preview_count, 1)
        self.assertIn("You've seen 1 preview this week", bot.photos[0]["caption"])
        self.assertIn("Mouth Aesthethics published 12 posts", bot.photos[0]["caption"])
        self.assertTrue(bot.photos[0]["disable_notification"])
        self.assertIsNone(repeated_message)
        self.assertEqual(repeated_event.event_key, event.event_key)
        self.assertEqual(len(bot.photos), 1)

    async def test_recap_test_does_not_consume_the_weekly_event(self):
        store = self.make_store()
        items = [self.add_published(store, index) for index in range(1, 10)]
        now = datetime.now(UTC)
        store.mark_preview_published(items[0].id, 3001, now.isoformat(timespec="seconds"))
        bot = FakePreviewBot()
        recap = WeeklyPreviewRecap(store)

        _, test_event = await recap.send_test(SimpleNamespace(bot=bot), now=now)

        self.assertEqual(test_event.status, "test")
        self.assertEqual(store.pending_preview_conversion_events(now.isoformat()), [])
        weekly_event = recap.ensure_event(now=now, force=True)
        self.assertEqual(weekly_event.status, "pending")
        self.assertTrue(weekly_event.event_key.startswith("weekly:"))

    async def test_recap_skips_broken_telegram_files_and_still_builds_nine_tiles(self):
        store = self.make_store()
        items = [self.add_published(store, index) for index in range(1, 13)]
        bot = FakePreviewBot()
        bot.unavailable_file_ids.update(item.file_id for item in items[:3])

        await WeeklyPreviewRecap(store).send_test(
            SimpleNamespace(bot=bot),
            now=datetime.now(UTC),
        )

        self.assertEqual(len(bot.photos), 1)
        with Image.open(bot.photos[0]["photo"]) as mosaic:
            self.assertEqual(mosaic.size, (1080, 1080))


    async def test_recap_recovers_and_persists_a_premium_video_thumbnail(self):
        store = self.make_store()
        store.set_setting("channel_id", "@premium")
        _photos = [self.add_published(store, index) for index in range(1, 9)]
        video = self.add_published(store, 9, media_type="video")
        with store.connect() as connection:
            connection.execute(
                "UPDATE media_items SET channel_message_id = ? WHERE id = ?",
                (9009, video.id),
            )
        bot = FakeVideoRecoverBot()

        await WeeklyPreviewRecap(store).send_test(
            SimpleNamespace(bot=bot),
            now=datetime.now(UTC),
            staging_chat_id=99,
        )

        refreshed = store.find_media_by_id(video.id)
        self.assertEqual(
            refreshed.preview_thumbnail_file_id,
            "fresh-video-thumbnail",
        )
        self.assertEqual(len(bot.forwarded), 1)
        self.assertEqual(bot.forwarded[0]["message_id"], 9009)
        self.assertEqual(len(bot.deleted), 1)
        self.assertEqual(len(bot.photos), 1)


if __name__ == "__main__":
    unittest.main()
