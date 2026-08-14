from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .balancer import PHOTO, VIDEO
from .preview_schedule import (
    PREVIEW_JOB_INTERVAL_SECONDS,
    PREVIEW_MAX_POSTS_PER_DAY,
    due_preview_slots,
    local_day_bounds,
    parse_preview_times,
)
from .scheduling import (
    DEFAULT_TIMEZONE,
    next_aligned_publish_after,
    next_allowed_datetime,
)
from .storage import Store


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def _local_iso(value: datetime | None, zone: ZoneInfo) -> str | None:
    return value.astimezone(zone).isoformat(timespec="minutes") if value else None


def _timezone(store: Store) -> tuple[str, ZoneInfo]:
    name = store.get_setting("timezone", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return DEFAULT_TIMEZONE, ZoneInfo(DEFAULT_TIMEZONE)


def _next_preview_opportunity(
    now: datetime,
    eligible_at: datetime,
    zone: ZoneInfo,
    posting_times: tuple[time, ...],
    scheduled_today: int,
    daily_limit: int,
) -> datetime:
    candidate = max(now, eligible_at).astimezone(zone)
    today = now.astimezone(zone).date()
    for day_offset in range(8):
        day = candidate.date() + timedelta(days=day_offset)
        existing = scheduled_today if day == today else 0
        if existing >= daily_limit:
            continue
        earliest = candidate if day_offset == 0 else datetime.combine(day, time.min, tzinfo=zone)
        due_at_earliest = sum(slot <= earliest.time().replace(tzinfo=None) for slot in posting_times)
        if due_at_earliest > existing:
            return earliest.astimezone(UTC)
        for position, slot in enumerate(posting_times, start=1):
            if position <= existing:
                continue
            slot_at = datetime.combine(day, slot, tzinfo=zone)
            if slot_at >= earliest:
                return slot_at.astimezone(UTC)
    return candidate.astimezone(UTC)


def _next_preview_day_slot(now: datetime, zone: ZoneInfo, posting_times: tuple[time, ...]) -> datetime:
    tomorrow = now.astimezone(zone).date() + timedelta(days=1)
    return datetime.combine(tomorrow, posting_times[0], tzinfo=zone).astimezone(UTC)


def _source_key(item) -> str:
    if item.source_id:
        return f"id:{item.source_id.strip().lower()}"
    if item.source_label:
        return f"label:{item.source_label.strip().lower()}"
    if item.content_fingerprint:
        pieces = item.content_fingerprint.split(":", 2)
        if pieces:
            return f"type:{pieces[0]}:{item.id}"
    return f"item:{item.id}"


def _selectable_candidate(eligible, history):
    used_sources = {_source_key(item) for item in history}
    allowed = [item for item in eligible if _source_key(item) not in used_sources]
    return min(allowed, key=lambda item: (item.preview_failed_attempts, item.id), default=None)


def _premium_status(store: Store, now: datetime, timezone_name: str, zone: ZoneInfo) -> dict[str, Any]:
    channel_id = store.get_setting("channel_id")
    queued = store.queued_counts_by_type()
    queued_total = int(queued.get(PHOTO, 0)) + int(queued.get(VIDEO, 0))
    start, end, _ = local_day_bounds(now, timezone_name)
    next_post_at = _parse_datetime(store.get_setting("next_publish_at"))
    if next_post_at is None:
        next_post_at = next_aligned_publish_after(
            now,
            store.get_int_setting("interval_minutes", 60),
            timezone_name,
            store.get_setting("posting_windows", "all") or "all",
        )
    else:
        next_post_at = next_allowed_datetime(
            next_post_at,
            timezone_name,
            store.get_setting("posting_windows", "all") or "all",
        )

    paused = store.get_bool_setting("paused")
    last_run_status = store.get_setting("premium_last_status") or ""
    last_error = store.get_setting("premium_last_error") or None
    if not channel_id:
        status, reason = "not_configured", "Canale principale non configurato"
        next_post_at = None
    elif last_run_status == "error" and last_error:
        status, reason = "error", last_error
    elif paused:
        status, reason = "paused", "Pubblicazione automatica in pausa"
    elif queued_total == 0:
        status, reason = "empty", "Coda Premium vuota"
    elif next_post_at < now - timedelta(minutes=5):
        status, reason = "late", "Orario superato: lo scheduler non ha ancora completato il ciclo"
    else:
        status, reason = "active", "Programmazione regolare"

    last_post_at = _parse_datetime(store.latest_published_at())
    return {
        "label": "Mouth Aesthetics",
        "channel_id": channel_id,
        "configured": bool(channel_id),
        "status": status,
        "reason": reason[:500],
        "next_post_at": _iso(next_post_at),
        "next_post_local": _local_iso(next_post_at, zone),
        "last_post_at": _iso(last_post_at),
        "last_post_local": _local_iso(last_post_at, zone),
        "last_check_at": store.get_setting("premium_last_check_at"),
        "published_today": store.premium_count_between(start.isoformat(), end.isoformat()),
        "queued": {
            "photo": int(queued.get(PHOTO, 0)),
            "video": int(queued.get(VIDEO, 0)),
            "total": queued_total,
        },
        "failed": store.failed_count(),
        "paused": paused,
    }


def _preview_status(store: Store, now: datetime, timezone_name: str, zone: ZoneInfo) -> dict[str, Any]:
    channel_id = store.get_setting("preview_channel_id")
    start, end, _ = local_day_bounds(now, timezone_name)
    history = store.preview_history_between(start.isoformat(), end.isoformat())
    published_today = len(history)
    scheduled_today = store.scheduled_preview_count_between(start.isoformat(), end.isoformat())
    manual_today = max(0, published_today - scheduled_today)
    daily_limit = min(
        PREVIEW_MAX_POSTS_PER_DAY,
        max(1, store.get_int_setting("preview_posts_per_day", PREVIEW_MAX_POSTS_PER_DAY)),
    )
    posting_times_raw = store.get_setting("preview_posting_times", "10:00,20:00") or "10:00,20:00"
    try:
        posting_times = parse_preview_times(posting_times_raw)
    except ValueError:
        posting_times_raw = "10:00,20:00"
        posting_times = parse_preview_times(posting_times_raw)
    due_slots = min(daily_limit, due_preview_slots(now, timezone_name, posting_times_raw))
    missing_due = max(0, due_slots - scheduled_today)
    now_iso = now.isoformat(timespec="seconds")
    eligible = store.list_preview_candidates(now_iso)
    selected = _selectable_candidate(eligible, history)
    earliest_pending = _parse_datetime(store.earliest_pending_preview_eligible_at())
    earliest_future = _parse_datetime(store.earliest_pending_preview_eligible_at(after=now_iso))
    last_check_at = _parse_datetime(store.get_setting("preview_last_check_at"))
    last_dispatcher_error = store.get_setting("preview_last_error") or None

    next_post_at: datetime | None = None
    if earliest_pending is not None:
        if selected is None and eligible:
            next_post_at = _next_preview_day_slot(now, zone, posting_times)
        else:
            eligible_reference = now if selected is not None else earliest_future or earliest_pending
            next_post_at = _next_preview_opportunity(
                now,
                eligible_reference,
                zone,
                posting_times,
                scheduled_today,
                daily_limit,
            )

    if not channel_id:
        status, reason = "not_configured", "Canale MouthPreview non configurato"
        next_post_at = None
    elif last_dispatcher_error:
        status, reason = "error", last_dispatcher_error
    elif last_check_at and now - last_check_at > timedelta(seconds=PREVIEW_JOB_INTERVAL_SECONDS * 2 + 60):
        status, reason = "error", "Il controllo Preview non viene eseguito da oltre 11 minuti"
    elif missing_due and selected is not None and selected.preview_error:
        status, reason = "error", selected.preview_error
    elif missing_due and selected is not None:
        status, reason = "due", "Post dovuto e foto idonea disponibile"
    elif missing_due and eligible:
        status, reason = "waiting", "Foto idonee già usate oggi; attesa una fonte diversa"
    elif missing_due and earliest_future:
        delay = store.get_int_setting("preview_delay_hours", 48)
        status, reason = "waiting", f"Nessuna foto ancora idonea dopo il ritardo di {delay} ore"
    elif missing_due:
        status, reason = "empty", "Nessuna foto Premium in attesa per Preview"
    elif scheduled_today >= daily_limit:
        status, reason = "complete", "Quota giornaliera completata"
    elif earliest_pending is None:
        status, reason = "empty", "Nessuna foto Premium in attesa per Preview"
    else:
        status, reason = "scheduled", "In attesa del prossimo orario Preview"

    last_item = store.latest_preview_item()
    last_post_at = _parse_datetime(last_item.preview_published_at if last_item else None)
    return {
        "label": "MouthPreview",
        "channel_id": channel_id,
        "configured": bool(channel_id),
        "status": status,
        "reason": reason[:500],
        "next_post_at": _iso(next_post_at),
        "next_post_local": _local_iso(next_post_at, zone),
        "last_post_at": _iso(last_post_at),
        "last_post_local": _local_iso(last_post_at, zone),
        "last_check_at": _iso(last_check_at),
        "last_publish_attempt_at": store.get_setting("preview_last_publish_attempt_at"),
        "warning": store.get_setting("preview_last_warning") or None,
        "published_today": published_today,
        "scheduled_today": scheduled_today,
        "manual_today": manual_today,
        "daily_limit": daily_limit,
        "due_slots": due_slots,
        "missing_due": missing_due,
        "eligible_photos": len(eligible),
        "next_eligible_at": _iso(earliest_future),
    }


def build_channel_status(store: Store, now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    timezone_name, zone = _timezone(store)
    return {
        "ok": True,
        "generated_at": current.isoformat(timespec="seconds"),
        "timezone": timezone_name,
        "premium": _premium_status(store, current, timezone_name, zone),
        "preview": _preview_status(store, current, timezone_name, zone),
    }
