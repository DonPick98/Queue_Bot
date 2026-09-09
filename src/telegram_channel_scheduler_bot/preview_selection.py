from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Iterable

from .storage import MediaItem


PREVIEW_MOSAIC_LIMIT = 12
PREVIEW_MOSAIC_CANDIDATE_LIMIT = 48


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
        used_sources = {source_key(item) for item in published_today}
        last_source = source_key(last_published) if last_published else None
        last_tags = set(last_published.derived_tags) if last_published else set()
        last_visual_hash = last_published.visual_hash if last_published else None

        allowed = (item for item in candidates if source_key(item) not in used_sources)

        def score(item: MediaItem) -> tuple[int, int, int, int, int, int, int]:
            tags = set(item.derived_tags)
            aspect, resolution = technical_quality(item)
            return (
                1 if source_key(item) != last_source else 0,
                1 if not last_tags or not tags or not (tags & last_tags) else 0,
                1 if not last_visual_hash or item.visual_hash != last_visual_hash else 0,
                -item.preview_failed_attempts,
                aspect,
                resolution,
                -item.id,
            )

        return max(allowed, key=score, default=None)
