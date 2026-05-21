from __future__ import annotations

from typing import Mapping


PHOTO = "photo"
VIDEO = "video"
MEDIA_TYPES = (PHOTO, VIDEO)


def choose_media_type(
    queued_counts: Mapping[str, int],
    recent_published_counts: Mapping[str, int],
    photo_ratio: int,
    video_ratio: int,
    last_published_type: str | None = None,
) -> str | None:
    """Choose the next media type using weighted fairness over recent posts."""

    weights = {
        PHOTO: max(1, int(photo_ratio)),
        VIDEO: max(1, int(video_ratio)),
    }
    candidates = [media_type for media_type in MEDIA_TYPES if queued_counts.get(media_type, 0) > 0]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    scores = {
        media_type: recent_published_counts.get(media_type, 0) / weights[media_type]
        for media_type in candidates
    }
    best_score = min(scores.values())
    tied = [media_type for media_type, score in scores.items() if score == best_score]

    if len(tied) == 1:
        return tied[0]

    if last_published_type in tied:
        alternatives = [media_type for media_type in tied if media_type != last_published_type]
        if alternatives:
            tied = alternatives

    return max(tied, key=lambda media_type: (weights[media_type], queued_counts.get(media_type, 0)))
