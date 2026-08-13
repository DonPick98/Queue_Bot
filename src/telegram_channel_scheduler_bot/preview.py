from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from io import BytesIO
import asyncio
import hashlib
import logging
import re
from typing import Iterable
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .config import PREVIEW_MEMBERPASS_LINK_VERSION, PREVIEW_MEMBERPASS_URL
from .preview_schedule import (
    PREVIEW_JOB_INTERVAL_SECONDS,
    PREVIEW_MAX_POSTS_PER_DAY,
    due_preview_slots,
    local_day_bounds,
    parse_preview_times,
)
from .storage import MediaItem, PreviewConversionEvent, Store


LOGGER = logging.getLogger(__name__)
PREVIEW_JOB_NAME = "mouth_preview_dispatcher"
PREVIEW_MOSAIC_MIN = 9
PREVIEW_MOSAIC_LIMIT = 12
PREVIEW_MOSAIC_CANDIDATE_LIMIT = 48
PREVIEW_MOSAIC_DOWNLOAD_BATCH = 4
PREVIEW_WATERMARK_MAX_DIMENSION = 2560
PREMIUM_CHANNEL_NAME = "Mouth Aesthethics"

PREVIEW_WELCOME_COPY_VERSION = "en-v1"
REDDIT_CREATOR_CREDIT_RE = re.compile(r"u/[A-Za-z0-9_-]{3,20}")


def utcnow() -> datetime:
    return datetime.now(UTC)


def source_key(item: MediaItem) -> str:
    if item.source_id:
        return f"id:{item.source_id.strip().lower()}"
    if item.source_label:
        return f"label:{item.source_label.strip().lower()}"
    if item.content_fingerprint:
        pieces = item.content_fingerprint.split(":", 2)
        if pieces:
            return f"type:{pieces[0]}:{item.id}"
    return f"item:{item.id}"


def technical_quality(item: MediaItem) -> tuple[int, int]:
    width = item.media_width or 0
    height = item.media_height or 0
    pixels = width * height
    if not width or not height:
        return 0, 0
    ratio = max(width / height, height / width)
    aspect_score = 0 if ratio > 3.0 else 1 if ratio > 2.2 else 2
    resolution_score = 2 if pixels >= 1_000_000 else 1 if pixels >= 400_000 else 0
    return aspect_score, resolution_score


def _evenly_spaced(items: list[MediaItem], count: int) -> list[MediaItem]:
    items = list(items)
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return items
    if count == 1:
        return [items[len(items) // 2]]

    return [
        items[round(index * (len(items) - 1) / (count - 1))]
        for index in range(count)
    ]


def select_mosaic_candidates(
    candidates: Iterable[MediaItem],
    display_limit: int = PREVIEW_MOSAIC_LIMIT,
    candidate_limit: int = PREVIEW_MOSAIC_CANDIDATE_LIMIT,
) -> tuple[MediaItem, ...]:
    ordered = list(candidates)
    if not ordered:
        return ()

    display_limit = max(1, int(display_limit))
    target = min(display_limit, len(ordered))
    videos = [item for item in ordered if item.media_type == "video"]
    photos = [item for item in ordered if item.media_type == "photo"]
    video_target = min(3, len(videos), target)
    photo_target = min(target - video_target, len(photos))
    if photo_target < target - video_target:
        video_target = min(len(videos), target - photo_target)

    primary = _evenly_spaced(photos, photo_target) + _evenly_spaced(videos, video_target)
    primary_ids = {item.id for item in primary}
    positions = {item.id: index for index, item in enumerate(ordered)}
    primary.sort(key=lambda item: positions[item.id])

    remaining = [item for item in ordered if item.id not in primary_ids]
    fallback_count = max(0, min(len(remaining), int(candidate_limit) - len(primary)))
    fallback = _evenly_spaced(remaining, fallback_count)
    return tuple(primary + fallback)


def preview_creator_credit(item: MediaItem) -> str | None:
    source_id = str(item.source_id or "").strip().lower()
    caption = str(item.caption_html or "").strip()
    creator_credit = caption.rsplit("\n", 1)[-1].strip()
    if source_id.startswith("reddit:") and REDDIT_CREATOR_CREDIT_RE.fullmatch(creator_credit):
        return creator_credit
    return None


class PreviewEligibilityService:
    @staticmethod
    def eligible_at(premium_published_at: datetime, delay_hours: int = 48) -> datetime:
        return premium_published_at.astimezone(UTC) + timedelta(hours=max(1, delay_hours))


class PreviewSelector:
    def choose(
        self,
        candidates: Iterable[MediaItem],
        published_today: Iterable[MediaItem],
        last_published: MediaItem | None,
    ) -> MediaItem | None:
        candidates = tuple(candidates)
        used_sources = {source_key(item) for item in published_today}
        last_source = source_key(last_published) if last_published else None
        last_tags = set(last_published.derived_tags) if last_published else set()
        last_visual_hash = last_published.visual_hash if last_published else None

        allowed = [item for item in candidates if source_key(item) not in used_sources]
        if not allowed:
            return None

        def score(item: MediaItem) -> tuple[int, int, int, int, int, int]:
            tags = set(item.derived_tags)
            aspect, resolution = technical_quality(item)
            return (
                1 if source_key(item) != last_source else 0,
                1 if not last_tags or not tags or not (tags & last_tags) else 0,
                1 if not last_visual_hash or item.visual_hash != last_visual_hash else 0,
                aspect,
                resolution,
                -item.id,
            )

        return max(allowed, key=score)


def upgrade_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"Try {PREMIUM_CHANNEL_NAME}", url=url)]]
    )


def welcome_text() -> str:
    return (
        "🍓 <b>Welcome to Mouth Preview</b>\n\n"
        "You'll get two hand-picked images every day, 48 hours after they appear in Premium.\n\n"
        "Mouth Preview does not publish videos. Berry Premium gets the complete feed immediately: "
        "12+ photos and videos every day.\n\n"
        "First month: <b>$1</b>\nThen: <b>$3/month</b> · cancel anytime"
    )


def upgrade_text(event: PreviewConversionEvent) -> str:
    return recap_text(event)


def recap_text(event: PreviewConversionEvent) -> str:
    preview_label = "preview" if event.preview_count == 1 else "previews"
    post_label = "post" if event.premium_count == 1 else "posts"
    return (
        f"You've seen {event.preview_count} {preview_label} this week. During the same period, "
        f"{PREMIUM_CHANNEL_NAME} published {event.premium_count} {post_label} (videos too!).\n\n"
        "Try your first month for <b>$1</b>, then <b>$3/month</b> \u2193"
    )


def _watermark_font(size: int):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def build_watermarked_photo(
    content: bytes,
    text: str = "@MouthPreview",
    opacity: int = 64,
    scale_percent: int = 10,
    max_dimension: int = PREVIEW_WATERMARK_MAX_DIMENSION,
) -> BytesIO:
    max_dimension = max(720, int(max_dimension))
    with Image.open(BytesIO(content)) as source:
        source.draft("RGB", (max_dimension, max_dimension))
        image = ImageOps.exif_transpose(source)
        if max(image.size) > max_dimension:
            image.thumbnail(
                (max_dimension, max_dimension),
                Image.Resampling.LANCZOS,
                reducing_gap=3.0,
            )
        image = image.convert("RGBA")

    width, height = image.size
    scale_percent = max(3, min(18, int(scale_percent)))
    font_size = max(8, round(min(width, height) * scale_percent / 100))
    padding = max(8, round(min(width, height) * 0.025))
    font = _watermark_font(font_size)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    label = text.strip() or "@MouthPreview"
    bounds = draw.textbbox((0, 0), label, font=font)
    while bounds[2] - bounds[0] > width - (2 * padding) and font_size > 8:
        font_size = max(8, font_size - 2)
        font = _watermark_font(font_size)
        bounds = draw.textbbox((0, 0), label, font=font)
    alpha = max(24, min(160, int(opacity)))
    shadow = max(20, min(110, alpha))
    shadow_offset = max(1, round(font_size * 0.06))
    x = padding - bounds[0]
    y = max(padding - bounds[1], height - padding - shadow_offset - bounds[3])
    draw.text(
        (x + shadow_offset, y + shadow_offset),
        label,
        font=font,
        fill=(0, 0, 0, shadow),
    )
    draw.text((x, y), label, font=font, fill=(255, 255, 255, alpha))
    rendered = Image.alpha_composite(image, overlay).convert("RGB")
    output = BytesIO()
    output.name = "mouth-preview.jpg"
    rendered.save(output, format="JPEG", quality=90)
    output.seek(0)
    return output


async def _download_preview_source(
    application,
    store: Store,
    item: MediaItem,
    staging_chat_id: int | str | None = None,
) -> tuple[bytes, str]:
    file_id = item.file_id
    forwarded_message = None
    try:
        telegram_file = await application.bot.get_file(file_id)
        content = bytes(await telegram_file.download_as_bytearray())
    except TelegramError:
        if not item.channel_message_id:
            raise
        if staging_chat_id is None:
            admin_ids = sorted(store.get_admin_ids())
            staging_chat_id = admin_ids[0] if admin_ids else None
        premium_channel_id = store.get_setting("channel_id")
        if staging_chat_id is None or not premium_channel_id:
            raise
        forwarded_message = await application.bot.forward_message(
            chat_id=staging_chat_id,
            from_chat_id=premium_channel_id,
            message_id=item.channel_message_id,
            disable_notification=True,
        )
        try:
            if not forwarded_message.photo:
                raise ValueError("Il messaggio Premium recuperato non contiene una foto.")
            file_id = forwarded_message.photo[-1].file_id
            store.update_media_file_id(item.id, file_id)
            telegram_file = await application.bot.get_file(file_id)
            content = bytes(await telegram_file.download_as_bytearray())
        finally:
            try:
                await application.bot.delete_message(
                    chat_id=staging_chat_id,
                    message_id=forwarded_message.message_id,
                )
            except TelegramError:
                LOGGER.warning("Could not delete temporary Preview recovery message", exc_info=True)
    return content, file_id

async def _download_mosaic_source(
    application,
    store: Store,
    item: MediaItem,
    staging_chat_id: int | str | None = None,
) -> bytes:
    if item.media_type == "photo":
        content, _ = await _download_preview_source(
            application,
            store,
            item,
            staging_chat_id=staging_chat_id,
        )
        return content

    last_error: TelegramError | None = None
    thumbnail_file_id = item.preview_thumbnail_file_id
    if thumbnail_file_id:
        try:
            telegram_file = await application.bot.get_file(thumbnail_file_id)
            return bytes(await telegram_file.download_as_bytearray())
        except TelegramError as exc:
            last_error = exc

    if not item.channel_message_id:
        if last_error is not None:
            raise last_error
        raise ValueError(f"Il video Premium #{item.id} non ha una thumbnail recuperabile.")

    if staging_chat_id is None:
        admin_ids = sorted(store.get_admin_ids())
        staging_chat_id = admin_ids[0] if admin_ids else None
    premium_channel_id = store.get_setting("channel_id")
    if staging_chat_id is None or not premium_channel_id:
        raise ValueError("Manca una chat admin per recuperare la thumbnail video Premium.")

    forwarded_message = await application.bot.forward_message(
        chat_id=staging_chat_id,
        from_chat_id=premium_channel_id,
        message_id=item.channel_message_id,
        disable_notification=True,
    )
    try:
        video = getattr(forwarded_message, "video", None)
        thumbnail = getattr(video, "thumbnail", None)
        if thumbnail is None:
            raise ValueError("Il video Premium recuperato non contiene una thumbnail.")
        thumbnail_file_id = thumbnail.file_id
        store.update_media_preview_thumbnail_file_id(item.id, thumbnail_file_id)
        telegram_file = await application.bot.get_file(thumbnail_file_id)
        return bytes(await telegram_file.download_as_bytearray())
    finally:
        try:
            await application.bot.delete_message(
                chat_id=staging_chat_id,
                message_id=forwarded_message.message_id,
            )
        except TelegramError:
            LOGGER.warning("Could not delete temporary video-thumbnail message", exc_info=True)



async def prepare_preview_photo(
    application,
    store: Store,
    item: MediaItem,
    force_watermark: bool = False,
    staging_chat_id: int | str | None = None,
):
    content, file_id = await _download_preview_source(
        application,
        store,
        item,
        staging_chat_id=staging_chat_id,
    )

    if not force_watermark and not store.get_bool_setting("preview_watermark_enabled", True):
        return file_id
    return await asyncio.to_thread(
        build_watermarked_photo,
        content,
        store.get_setting("preview_watermark_text", "@MouthPreview") or "@MouthPreview",
        store.get_int_setting("preview_watermark_opacity", 64),
        store.get_int_setting("preview_watermark_scale_percent", 10),
    )


def _add_video_badge(image: Image.Image) -> Image.Image:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    diameter = max(36, round(min(image.size) * 0.16))
    margin = max(12, round(min(image.size) * 0.05))
    left = image.width - margin - diameter
    top = margin
    draw.ellipse(
        (left, top, left + diameter, top + diameter),
        fill=(10, 8, 12, 150),
        outline=(255, 255, 255, 190),
        width=max(2, diameter // 24),
    )
    inset = diameter * 0.29
    draw.polygon(
        (
            (left + inset, top + inset * 0.72),
            (left + inset, top + diameter - inset * 0.72),
            (left + diameter - inset * 0.68, top + diameter / 2),
        ),
        fill=(255, 255, 255, 225),
    )
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def build_mosaic(
    images: Iterable[bytes],
    tile_size: int = 360,
    video_indices: Iterable[int] = (),
) -> BytesIO:
    video_indices = set(video_indices)
    prepared: list[tuple[Image.Image, bool]] = []
    for index, content in enumerate(images):
        try:
            with Image.open(BytesIO(content)) as source:
                image = ImageOps.exif_transpose(source).convert("RGB").copy()
            prepared.append((image, index in video_indices))
        except Exception:
            LOGGER.warning("Skipping unreadable Preview recap thumbnail", exc_info=True)
    if not prepared:
        raise ValueError("Nessuna miniatura valida per il recap Preview")

    columns = 3
    if len(prepared) == 10:
        row_counts = (3, 3, 2, 2)
    else:
        full_rows, remainder = divmod(len(prepared), columns)
        row_counts = (columns,) * full_rows + ((remainder,) if remainder else ())
    rows = len(row_counts)
    canvas_width = columns * tile_size
    canvas = Image.new("RGB", (canvas_width, rows * tile_size), "#120d12")
    gap = max(2, tile_size // 90)

    offset = 0
    for row, row_count in enumerate(row_counts):
        row_items = prepared[offset : offset + row_count]
        offset += row_count
        cell_width = canvas_width // len(row_items)
        for column, (source, is_video) in enumerate(row_items):
            left = column * cell_width
            right = canvas_width if column == len(row_items) - 1 else (column + 1) * cell_width
            target = (max(1, right - left - gap * 2), max(1, tile_size - gap * 2))
            tile = ImageOps.fit(source, target, method=Image.Resampling.LANCZOS)
            tile = tile.filter(ImageFilter.GaussianBlur(radius=max(3.0, tile_size / 48)))
            tile = ImageEnhance.Brightness(tile).enhance(0.58)
            if is_video:
                tile = _add_video_badge(tile)
            canvas.paste(tile, (left + gap, row * tile_size + gap))

    output = BytesIO()
    output.name = "berry-premium-weekly-preview.jpg"
    canvas.save(output, format="JPEG", quality=92, optimize=True)
    output.seek(0)
    return output


@dataclass
class PreviewPublisher:
    store: Store
    selector: PreviewSelector = field(default_factory=PreviewSelector)

    async def publish_due(self, application, now: datetime | None = None) -> int:
        channel_id = self.store.get_setting("preview_channel_id")
        if not channel_id:
            return 0
        now = now or utcnow()
        timezone_name = self.store.get_setting("timezone", "Europe/Rome") or "Europe/Rome"
        start, end, local_date = local_day_bounds(now, timezone_name)
        history = self.store.preview_history_between(start.isoformat(), end.isoformat())
        configured_limit = self.store.get_int_setting("preview_posts_per_day", 2)
        daily_limit = min(PREVIEW_MAX_POSTS_PER_DAY, max(1, configured_limit))
        due = min(
            daily_limit,
            due_preview_slots(
                now,
                timezone_name,
                self.store.get_setting("preview_posting_times", "10:00,20:00") or "10:00,20:00",
            ),
        )
        missing = max(0, due - len(history))
        sent_count = 0
        for _ in range(missing):
            candidates = self.store.list_preview_candidates(now.isoformat())
            item = self.selector.choose(candidates, history, self.store.latest_preview_item())
            if item is None:
                LOGGER.info("No diverse eligible photo available for Mouth Preview")
                break
            policy = self.store.get_or_assign_preview_notification_policy(item.id, local_date)
            try:
                photo = await prepare_preview_photo(application, self.store, item)
                creator_credit = preview_creator_credit(item)
                sent = await application.bot.send_photo(
                    chat_id=channel_id,
                    photo=photo,
                    disable_notification=policy.silent,
                    **({"caption": creator_credit} if creator_credit else {}),
                )
            except (TelegramError, OSError, ValueError) as exc:
                self.store.mark_preview_failed(item.id, str(exc))
                LOGGER.warning("Mouth Preview photo %s failed", item.id, exc_info=True)
                break
            published_at = utcnow().isoformat(timespec="seconds")
            self.store.mark_preview_published(item.id, sent.message_id, published_at)
            history.append(self.store.find_media_by_id(item.id) or item)
            sent_count += 1
            LOGGER.info(
                "Published Mouth Preview photo %s, notification=%s, local_date=%s, position=%s",
                item.id,
                "silent" if policy.silent else "normal",
                local_date,
                policy.position,
            )
        return sent_count

    async def publish_one_now(
        self,
        application,
        now: datetime | None = None,
        staging_chat_id: int | str | None = None,
    ) -> MediaItem | None:
        channel_id = self.store.get_setting("preview_channel_id")
        if not channel_id:
            raise ValueError("Configura prima il canale Mouth Preview.")
        now = now or utcnow()
        timezone_name = self.store.get_setting("timezone", "Europe/Rome") or "Europe/Rome"
        start, end, local_date = local_day_bounds(now, timezone_name)
        history = self.store.preview_history_between(start.isoformat(), end.isoformat())
        item = self.selector.choose(
            self.store.list_preview_candidates(now.isoformat()),
            history,
            self.store.latest_preview_item(),
        )
        if item is None:
            return None
        policy = self.store.get_or_assign_preview_notification_policy(item.id, local_date)
        try:
            photo = await prepare_preview_photo(
                application,
                self.store,
                item,
                staging_chat_id=staging_chat_id,
            )
            creator_credit = preview_creator_credit(item)
            sent = await application.bot.send_photo(
                chat_id=channel_id,
                photo=photo,
                disable_notification=policy.silent,
                **({"caption": creator_credit} if creator_credit else {}),
            )
        except (TelegramError, OSError, ValueError) as exc:
            self.store.mark_preview_failed(item.id, str(exc))
            raise
        published_at = utcnow().isoformat(timespec="seconds")
        self.store.mark_preview_published(item.id, sent.message_id, published_at)
        LOGGER.info("Manually published Mouth Preview photo %s", item.id)
        return self.store.find_media_by_id(item.id) or item


@dataclass
class PreviewConversionScheduler:
    store: Store

    def ensure_upgrade_event(self, now_iso: str) -> PreviewConversionEvent | None:
        LOGGER.debug("Skipping legacy six-preview conversion event at %s", now_iso)
        return None

    async def send_pending(self, application, now: datetime | None = None) -> int:
        channel_id = self.store.get_setting("preview_channel_id")
        if not channel_id:
            return 0
        now = now or utcnow()
        sent_count = 0
        for event in self.store.pending_preview_conversion_events(now.isoformat()):
            if event.kind != "weekly_recap":
                self.store.dismiss_preview_conversion_event(
                    event.id,
                    "Disabled: conversion posts are weekly only.",
                )
                LOGGER.info("Dismissed legacy Mouth Preview conversion %s", event.event_key)
                continue
            try:
                message = await WeeklyPreviewRecap(self.store).send_event(application, event)
            except (TelegramError, ValueError) as exc:
                self.store.mark_preview_conversion_failed(event.id, str(exc))
                LOGGER.warning("Mouth Preview conversion %s failed", event.event_key, exc_info=True)
                continue
            self.store.mark_preview_conversion_sent(event.id, message.message_id)
            sent_count += 1
            LOGGER.info("Published Mouth Preview conversion %s silently", event.event_key)
        return sent_count


@dataclass
class WeeklyPreviewRecap:
    store: Store

    def _mosaic_candidate_ids(self, start_at: str, end_at: str) -> tuple[int, ...]:
        candidates = self.store.published_mosaic_candidates_between(
            start_at,
            end_at,
            PREVIEW_MOSAIC_CANDIDATE_LIMIT * 4,
        )
        return tuple(item.id for item in select_mosaic_candidates(candidates))

    def ensure_event(self, now: datetime | None = None, force: bool = False) -> PreviewConversionEvent | None:
        now = now or utcnow()
        timezone_name = self.store.get_setting("timezone", "Europe/Rome") or "Europe/Rome"
        zone = ZoneInfo(timezone_name)
        local = now.astimezone(zone)
        weekday = self.store.get_int_setting("preview_recap_weekday", 6)
        recap_at = parse_preview_times(self.store.get_setting("preview_recap_time", "21:00") or "21:00")[0]
        if not force and (local.weekday() != weekday or local.time().replace(tzinfo=None) < recap_at):
            return None
        week_start_local = datetime.combine(local.date() - timedelta(days=6), time.min, tzinfo=zone)
        start_at = week_start_local.astimezone(UTC).isoformat()
        end_at = now.isoformat()
        iso_year, iso_week, _ = local.isocalendar()
        return self.store.create_preview_conversion_event(
            "weekly_recap",
            f"weekly:{iso_year}-W{iso_week:02d}",
            end_at,
            start_at,
            end_at,
            self.store.premium_count_between(start_at, end_at),
            self.store.preview_count_between(start_at, end_at),
            self._mosaic_candidate_ids(start_at, end_at),
            self.store.get_setting(
                "preview_memberpass_link_version",
                PREVIEW_MEMBERPASS_LINK_VERSION,
            )
            or PREVIEW_MEMBERPASS_LINK_VERSION,
        )

    def build_test_event(self, now: datetime | None = None) -> PreviewConversionEvent:
        now = now or utcnow()
        timezone_name = self.store.get_setting("timezone", "Europe/Rome") or "Europe/Rome"
        zone = ZoneInfo(timezone_name)
        local = now.astimezone(zone)
        week_start_local = datetime.combine(local.date() - timedelta(days=6), time.min, tzinfo=zone)
        start_at = week_start_local.astimezone(UTC).isoformat()
        end_at = now.isoformat()
        return PreviewConversionEvent(
            id=0,
            kind="weekly_recap",
            event_key=f"test:{now.isoformat()}",
            status="test",
            eligible_at=end_at,
            start_at=start_at,
            end_at=end_at,
            premium_count=self.store.premium_count_between(start_at, end_at),
            preview_count=self.store.preview_count_between(start_at, end_at),
            media_item_ids=self._mosaic_candidate_ids(start_at, end_at),
            memberpass_link_version=(
                self.store.get_setting(
                    "preview_memberpass_link_version",
                    PREVIEW_MEMBERPASS_LINK_VERSION,
                )
                or PREVIEW_MEMBERPASS_LINK_VERSION
            ),
            message_id=None,
            attempts=0,
            error=None,
        )

    async def send_event(
        self,
        application,
        event: PreviewConversionEvent,
        staging_chat_id: int | str | None = None,
    ):
        channel_id = self.store.get_setting("preview_channel_id")
        if not channel_id:
            raise ValueError("Configura prima il canale Mouth Preview.")

        candidate_items = [
            item
            for media_id in event.media_item_ids
            if (item := self.store.find_media_by_id(media_id)) is not None
        ]
        contents: list[bytes] = []
        media_types: list[str] = []

        async def load(item: MediaItem) -> tuple[bytes | None, Exception | None]:
            try:
                content = await _download_mosaic_source(
                    application,
                    self.store,
                    item,
                    staging_chat_id=staging_chat_id,
                )
                with Image.open(BytesIO(content)) as thumbnail:
                    thumbnail.verify()
                return content, None
            except (TelegramError, OSError, ValueError) as exc:
                return None, exc

        for offset in range(0, len(candidate_items), PREVIEW_MOSAIC_DOWNLOAD_BATCH):
            batch = candidate_items[offset : offset + PREVIEW_MOSAIC_DOWNLOAD_BATCH]
            results = await asyncio.gather(*(load(item) for item in batch))
            for item, (content, error) in zip(batch, results):
                if error is not None:
                    LOGGER.warning(
                        "Skipping unavailable weekly recap media %s: %s",
                        item.id,
                        error,
                    )
                    continue
                if content is not None:
                    contents.append(content)
                    media_types.append(item.media_type)
                if len(contents) >= PREVIEW_MOSAIC_LIMIT:
                    break
            if len(contents) >= PREVIEW_MOSAIC_LIMIT:
                break

        if len(contents) < PREVIEW_MOSAIC_MIN:
            raise ValueError(
                "Recap non creato: servono almeno "
                f"{PREVIEW_MOSAIC_MIN} thumbnail Premium recuperabili; "
                f"trovate {len(contents)} su {len(candidate_items)}."
            )

        video_indices = [
            index for index, media_type in enumerate(media_types) if media_type == "video"
        ]
        mosaic = await asyncio.to_thread(build_mosaic, contents, 360, video_indices)
        return await application.bot.send_photo(
            chat_id=channel_id,
            photo=mosaic,
            caption=recap_text(event),
            parse_mode="HTML",
            reply_markup=upgrade_keyboard(
                self.store.get_setting(
                    "preview_memberpass_url",
                    PREVIEW_MEMBERPASS_URL,
                )
                or PREVIEW_MEMBERPASS_URL
            ),
            disable_notification=True,
        )

    async def send_test(
        self,
        application,
        now: datetime | None = None,
        staging_chat_id: int | str | None = None,
    ):
        event = self.build_test_event(now)
        message = await self.send_event(application, event, staging_chat_id=staging_chat_id)
        LOGGER.info("Published Mouth Preview weekly recap test without creating a weekly event")
        return message, event

    async def force_send(self, application, now: datetime | None = None):
        event = self.ensure_event(now=now, force=True)
        if event is None:
            return None, None
        if event.status == "sent":
            return None, event
        message = await self.send_event(application, event)
        self.store.mark_preview_conversion_sent(event.id, message.message_id)
        LOGGER.info("Published forced Mouth Preview weekly recap %s", event.event_key)
        return message, event



def preview_welcome_payload(store: Store) -> tuple[str, str | None]:
    mode = store.get_setting("preview_welcome_mode", "default") or "default"
    custom_text = store.get_setting("preview_welcome_custom_text", "") or ""
    if mode == "custom" and custom_text.strip():
        return custom_text.strip(), None
    return welcome_text(), "HTML"


def preview_welcome_version(store: Store) -> str:
    text, _ = preview_welcome_payload(store)
    mode = store.get_setting("preview_welcome_mode", "default") or "default"
    link_version = (
        store.get_setting(
            "preview_memberpass_link_version",
            PREVIEW_MEMBERPASS_LINK_VERSION,
        )
        or PREVIEW_MEMBERPASS_LINK_VERSION
    )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{PREVIEW_WELCOME_COPY_VERSION}:{link_version}:{mode}:{digest}"


def set_preview_welcome(store: Store, mode: str, custom_text: str = "") -> None:
    store.set_setting("preview_welcome_mode", mode)
    store.set_setting("preview_welcome_custom_text", custom_text.strip() if mode == "custom" else "")


async def ensure_preview_welcome(application, store: Store, force: bool = False):
    channel_id = store.get_setting("preview_channel_id")
    if not channel_id:
        return None
    text, parse_mode = preview_welcome_payload(store)
    welcome_version = preview_welcome_version(store)
    previous_message_id = store.get_setting("preview_welcome_message_id")
    if not force and store.get_setting("preview_welcome_message_id") and (
        store.get_setting("preview_welcome_version") == welcome_version
    ):
        return None
    message = await application.bot.send_message(
        chat_id=channel_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=upgrade_keyboard(
            store.get_setting("preview_memberpass_url", PREVIEW_MEMBERPASS_URL)
            or PREVIEW_MEMBERPASS_URL
        ),
        disable_notification=True,
    )
    await application.bot.pin_chat_message(
        chat_id=channel_id,
        message_id=message.message_id,
        disable_notification=True,
    )
    store.set_setting("preview_welcome_message_id", str(message.message_id))
    store.set_setting("preview_welcome_version", welcome_version)
    if previous_message_id and previous_message_id != str(message.message_id):
        try:
            await application.bot.delete_message(
                chat_id=channel_id,
                message_id=int(previous_message_id),
            )
        except (TelegramError, ValueError):
            LOGGER.warning(
                "Could not remove the previous Mouth Preview welcome message",
                exc_info=True,
            )
    LOGGER.info("Published and pinned Mouth Preview welcome message")
    return message


def _message_reply_markup_already_current(error: TelegramError) -> bool:
    return "message is not modified" in str(error).lower()


async def sync_preview_memberpass_links(application, store: Store) -> bool:
    channel_id = store.get_setting("preview_channel_id")
    if not channel_id:
        return False

    memberpass_url = (
        store.get_setting("preview_memberpass_url", PREVIEW_MEMBERPASS_URL)
        or PREVIEW_MEMBERPASS_URL
    )
    link_version = (
        store.get_setting(
            "preview_memberpass_link_version",
            PREVIEW_MEMBERPASS_LINK_VERSION,
        )
        or PREVIEW_MEMBERPASS_LINK_VERSION
    )
    if store.get_setting("preview_memberpass_synced_version") == link_version:
        return True

    reply_markup = upgrade_keyboard(memberpass_url)
    sync_succeeded = True
    welcome_message_id = store.get_setting("preview_welcome_message_id")
    if welcome_message_id:
        try:
            await application.bot.edit_message_reply_markup(
                chat_id=channel_id,
                message_id=int(welcome_message_id),
                reply_markup=reply_markup,
            )
        except (TelegramError, ValueError) as exc:
            if not isinstance(exc, TelegramError) or not _message_reply_markup_already_current(exc):
                sync_succeeded = False
                LOGGER.warning(
                    "Could not update the Mouth Preview welcome MemberPass link",
                    exc_info=True,
                )
        if sync_succeeded:
            store.set_setting("preview_welcome_version", preview_welcome_version(store))
    else:
        try:
            await ensure_preview_welcome(application, store, force=True)
        except TelegramError:
            sync_succeeded = False
            LOGGER.warning(
                "Could not publish the Mouth Preview welcome with the current MemberPass link",
                exc_info=True,
            )

    for event in store.preview_recap_events_needing_link_sync(link_version):
        try:
            await application.bot.edit_message_reply_markup(
                chat_id=channel_id,
                message_id=event.message_id,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            if not _message_reply_markup_already_current(exc):
                sync_succeeded = False
                LOGGER.warning(
                    "Could not update the MemberPass link for Preview recap %s",
                    event.event_key,
                    exc_info=True,
                )
                continue
        store.mark_preview_conversion_link_synced(event.id, link_version)

    if sync_succeeded:
        store.set_setting("preview_memberpass_synced_version", link_version)
        LOGGER.info("Mouth Preview MemberPass links are synced to %s", link_version)
    return sync_succeeded


async def preview_dispatcher_job(context) -> None:
    store: Store = context.application.bot_data["store"]
    checked_at = utcnow().isoformat(timespec="seconds")
    store.set_setting("preview_last_check_at", checked_at)
    if not store.get_setting("preview_channel_id"):
        store.set_setting("preview_last_status", "not_configured")
        store.set_setting("preview_last_error", "")
        return
    try:
        await ensure_preview_welcome(context.application, store)
        sent_count = await PreviewPublisher(store).publish_due(context.application)
        WeeklyPreviewRecap(store).ensure_event()
        await PreviewConversionScheduler(store).send_pending(context.application)
        store.set_setting("preview_last_status", "published" if sent_count else "checked")
        store.set_setting("preview_last_error", "")
    except Exception as exc:
        store.set_setting("preview_last_status", "error")
        store.set_setting("preview_last_error", str(exc)[:1000])
        LOGGER.exception("Mouth Preview dispatcher failed")


def schedule_preview(application, store: Store) -> None:
    if application.job_queue is None:
        return
    for job in application.job_queue.get_jobs_by_name(PREVIEW_JOB_NAME):
        job.schedule_removal()
    application.job_queue.run_repeating(
        preview_dispatcher_job,
        interval=timedelta(seconds=PREVIEW_JOB_INTERVAL_SECONDS),
        first=timedelta(seconds=15),
        name=PREVIEW_JOB_NAME,
    )
