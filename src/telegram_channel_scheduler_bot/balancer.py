from __future__ import annotations

from typing import Mapping, Sequence


PHOTO = "photo"
VIDEO = "video"
MEDIA_TYPES = (PHOTO, VIDEO)


def choose_media_type(
    queued_counts: Mapping[str, int],
    recent_published_types: Sequence[str],
    photo_ratio: int,
    video_ratio: int,
) -> str | None:
    """Choose the next media type without repaying old missing-media debt."""

    weights = {
        PHOTO: max(1, int(photo_ratio)),
        VIDEO: max(1, int(video_ratio)),
    }
    candidates = [media_type for media_type in MEDIA_TYPES if queued_counts.get(media_type, 0) > 0]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    latest_type = next(
        (media_type for media_type in recent_published_types if media_type in MEDIA_TYPES),
        None,
    )
    if latest_type in candidates:
        current_run = 0
        for media_type in recent_published_types:
            if media_type != latest_type:
                break
            current_run += 1

        if current_run < weights[latest_type]:
            return latest_type

        other_type = VIDEO if latest_type == PHOTO else PHOTO
        if other_type in candidates:
            return other_type

    return max(candidates, key=lambda media_type: (weights[media_type], queued_counts.get(media_type, 0)))
