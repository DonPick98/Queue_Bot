from __future__ import annotations

from .scheduling import TimeWindow, parse_posting_windows


MAX_AUDIBLE_POSTS_PER_DAY = 3


def audible_post_positions(daily_post_count: int, audible_limit: int = MAX_AUDIBLE_POSTS_PER_DAY) -> tuple[int, ...]:
    total = max(0, int(daily_post_count))
    limit = min(MAX_AUDIBLE_POSTS_PER_DAY, max(0, int(audible_limit)), total)
    if not total or not limit:
        return ()
    return tuple(1 + (index * total) // limit for index in range(limit))


def scheduled_posts_per_day(
    interval_minutes: int,
    posts_per_run: int,
    posting_windows: str | None,
) -> int:
    interval = max(1, int(interval_minutes))
    batch_size = max(1, int(posts_per_run))
    windows = parse_posting_windows(posting_windows)
    slots = sum(
        1
        for minute in range(0, 24 * 60, interval)
        if not windows or any(_minute_in_window(minute, window) for window in windows)
    )
    return max(1, slots) * batch_size


def _minute_in_window(minute: int, window: TimeWindow) -> bool:
    if window.start_minute < window.end_minute:
        return window.start_minute <= minute < window.end_minute
    return minute >= window.start_minute or minute < window.end_minute
