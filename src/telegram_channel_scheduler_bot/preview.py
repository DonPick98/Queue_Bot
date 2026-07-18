from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from io import BytesIO
import asyncio
import hashlib
import logging
import math
import re
from typing import Iterable
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .storage import MediaItem, PreviewConversionEvent, Store


LOGGER = logging.getLogger(__name__)
PREVIEW_JOB_NAME = "mouth_preview_dispatcher"
PREVIEW_JOB_INTERVAL_SECONDS = 300
PREVIEW_MAX_POSTS_PER_DAY = 2
PREVIEW_UPGRADE_FREQUENCY = 6
PREVIEW_MOSAIC_LIMIT = 12
PREVIEW_WATERMARK_MAX_DIMENSION = 2560

PREVIEW_WELCOME_COPY_VERSION = "en-v1"


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_preview_times(raw: str) -> tuple[time, ...]:
    values: list[time] = []
    for chunk in raw.replace(";", ",").split(","):
        value = chunk.strip()
        if not value:
            continue
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
        if not match:
            raise ValueError(f"Orario Preview non valido: {value!r}")
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValueError(f"Orario Preview non valido: {value!r}")
        candidate = time(hour, minute)
        if candidate not in values:
            values.append(candidate)
    if not values:
        raise ValueError("PREVIEW_POSTING_TIMES deve contenere almeno un orario HH:MM")
    return tuple(sorted(values))


def local_day_bounds(value: datetime, timezone_name: str) -> tuple[datetime, datetime, str]:
    zone = ZoneInfo(timezone_name)
    local = value.astimezone(zone)
    start_local = datetime.combine(local.date(), time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC), local.date().isoformat()


def due_preview_slots(value: datetime, timezone_name: str, posting_times: str) -> int:
    local_time = value.astimezone(ZoneInfo(timezone_name)).time().replace(tzinfo=None)
    return sum(slot <= local_time for slot in parse_preview_times(posting_times))


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
        [[InlineKeyboardButton("Unlock the full MouthAesthetics feed →", url=url)]]
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
    return (
        f"You've seen {event.preview_count} previews. During the same period, Berry Premium "
        f"published {event.premium_count} posts.\n\n"
        "Mouth Preview is images only. Premium includes the complete feed: all photos and videos, "
        "without the 48-hour delay.\n\n"
        "Try the full feed for <b>$1</b> ↓"
    )


def recap_text(event: PreviewConversionEvent) -> str:
    return (
        "<b>This week in Berry Premium</b>\n\n"
        f"Premium published {event.premium_count} photos and videos. "
        f"Preview received {event.preview_count} images.\n\n"
        "Videos are not published in Mouth Preview. Get the complete feed immediately "
        "for <b>$1</b>."
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
    opacity: int = 82,
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
    font_size = max(18, min(52, round(min(width, height) * 0.034)))
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


async def prepare_preview_photo(
    application,
    store: Store,
    item: MediaItem,
    force_watermark: bool = False,
):
    if not force_watermark and not store.get_bool_setting("preview_watermark_enabled", True):
        return item.file_id
    telegram_file = await application.bot.get_file(item.file_id)
    content = bytes(await telegram_file.download_as_bytearray())
    return await asyncio.to_thread(
        build_watermarked_photo,
        content,
        store.get_setting("preview_watermark_text", "@MouthPreview") or "@MouthPreview",
        store.get_int_setting("preview_watermark_opacity", 82),
    )


def build_mosaic(images: Iterable[bytes], tile_size: int = 360) -> BytesIO:
    prepared: list[Image.Image] = []
    for content in images:
        try:
            with Image.open(BytesIO(content)) as source:
                tile = ImageOps.fit(source.convert("RGB"), (tile_size, tile_size), method=Image.Resampling.LANCZOS)
                prepared.append(ImageEnhance.Brightness(tile).enhance(0.82))
        except Exception:
            LOGGER.warning("Skipping unreadable Preview recap thumbnail", exc_info=True)
    if not prepared:
        raise ValueError("Nessuna miniatura valida per il recap Preview")

    columns = 3
    rows = math.ceil(len(prepared) / columns)
    canvas = Image.new("RGB", (columns * tile_size, rows * tile_size), "#120d12")
    for index, tile in enumerate(prepared):
        canvas.paste(tile, ((index % columns) * tile_size, (index // columns) * tile_size))
    output = BytesIO()
    output.name = "berry-premium-weekly-preview.jpg"
    canvas.save(output, format="JPEG", quality=88, optimize=True)
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
                sent = await application.bot.send_photo(
                    chat_id=channel_id,
                    photo=photo,
                    disable_notification=policy.silent,
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
            PreviewConversionScheduler(self.store).ensure_upgrade_event(published_at)
        return sent_count


@dataclass
class PreviewConversionScheduler:
    store: Store

    def ensure_upgrade_event(self, now_iso: str) -> PreviewConversionEvent | None:
        total = self.store.preview_count()
        if total < PREVIEW_UPGRADE_FREQUENCY or total % PREVIEW_UPGRADE_FREQUENCY:
            return None
        timestamps = self.store.latest_preview_timestamps(PREVIEW_UPGRADE_FREQUENCY)
        if len(timestamps) < PREVIEW_UPGRADE_FREQUENCY:
            return None
        start_at = timestamps[0]
        return self.store.create_preview_conversion_event(
            "upgrade",
            f"upgrade:{total}",
            now_iso,
            start_at,
            now_iso,
            self.store.premium_count_between(start_at, now_iso),
            PREVIEW_UPGRADE_FREQUENCY,
            (),
            self.store.get_setting("preview_memberpass_link_version", "v1") or "v1",
        )

    async def send_pending(self, application, now: datetime | None = None) -> int:
        channel_id = self.store.get_setting("preview_channel_id")
        if not channel_id:
            return 0
        now = now or utcnow()
        sent_count = 0
        for event in self.store.pending_preview_conversion_events(now.isoformat()):
            try:
                if event.kind == "weekly_recap":
                    message = await WeeklyPreviewRecap(self.store).send_event(application, event)
                else:
                    message = await application.bot.send_message(
                        chat_id=channel_id,
                        text=upgrade_text(event),
                        parse_mode="HTML",
                        reply_markup=upgrade_keyboard(
                            self.store.get_setting(
                                "preview_memberpass_url",
                                "https://my.memberpass.net/306354e7c4",
                            )
                            or "https://my.memberpass.net/306354e7c4"
                        ),
                        disable_notification=True,
                    )
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
            self.store.published_photo_ids_between(start_at, end_at, PREVIEW_MOSAIC_LIMIT),
            self.store.get_setting("preview_memberpass_link_version", "v1") or "v1",
        )

    async def send_event(self, application, event: PreviewConversionEvent):
        contents: list[bytes] = []
        for media_id in event.media_item_ids:
            item = self.store.find_media_by_id(media_id)
            if item is None:
                continue
            telegram_file = await application.bot.get_file(item.file_id)
            contents.append(bytes(await telegram_file.download_as_bytearray()))
        mosaic = build_mosaic(contents)
        return await application.bot.send_photo(
            chat_id=self.store.get_setting("preview_channel_id"),
            photo=mosaic,
            caption=recap_text(event),
            parse_mode="HTML",
            reply_markup=upgrade_keyboard(
                self.store.get_setting(
                    "preview_memberpass_url",
                    "https://my.memberpass.net/306354e7c4",
                )
                or "https://my.memberpass.net/306354e7c4"
            ),
            disable_notification=True,
        )

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
    link_version = store.get_setting("preview_memberpass_link_version", "v1") or "v1"
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
            store.get_setting("preview_memberpass_url", "https://my.memberpass.net/306354e7c4")
            or "https://my.memberpass.net/306354e7c4"
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


async def preview_dispatcher_job(context) -> None:
    store: Store = context.application.bot_data["store"]
    if not store.get_setting("preview_channel_id"):
        return
    try:
        await ensure_preview_welcome(context.application, store)
        await PreviewPublisher(store).publish_due(context.application)
        WeeklyPreviewRecap(store).ensure_event()
        await PreviewConversionScheduler(store).send_pending(context.application)
    except Exception:
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
