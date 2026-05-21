from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math


QUEUE_ALERT_HOURS = 24


@dataclass(frozen=True)
class QueueCoverage:
    queued_total: int
    required_posts: int
    missing_posts: int
    cycles: int
    covered_cycles: int
    interval_minutes: int
    batch_mode: str
    posts_per_run: int

    @property
    def covers(self) -> bool:
        return self.missing_posts == 0

    @property
    def covered_minutes(self) -> int:
        return min(QUEUE_ALERT_HOURS * 60, self.covered_cycles * self.interval_minutes)


def auto_posts_per_run(queued_total: int) -> int:
    if queued_total > 40:
        return 3
    if queued_total > 20:
        return 2
    return 1


def cycles_in_hours(interval_minutes: int, hours: int = QUEUE_ALERT_HOURS) -> int:
    return max(1, math.ceil((hours * 60) / max(1, interval_minutes)))


def initial_next_publish_at(
    now: datetime,
    interval_minutes: int,
    last_published_at: datetime | None = None,
) -> datetime:
    if last_published_at is not None:
        return last_published_at + timedelta(minutes=max(1, interval_minutes))
    return now + timedelta(minutes=max(1, interval_minutes))


def seconds_until_next_publish(
    next_publish_at: datetime,
    now: datetime,
    asap_seconds: int = 5,
) -> int:
    delay = math.ceil((next_publish_at - now).total_seconds())
    if delay <= 0:
        return max(1, asap_seconds)
    return delay


def estimate_queue_coverage(
    queued_total: int,
    interval_minutes: int,
    batch_mode: str,
    posts_per_run: int,
    hours: int = QUEUE_ALERT_HOURS,
) -> QueueCoverage:
    normalized_mode = batch_mode.strip().lower()
    cycles = cycles_in_hours(interval_minutes, hours)
    remaining = max(0, queued_total)
    required_posts = 0
    covered_cycles = 0

    for _ in range(cycles):
        batch_size = (
            auto_posts_per_run(remaining)
            if normalized_mode == "auto"
            else max(1, posts_per_run)
        )
        required_posts += batch_size
        if remaining >= batch_size:
            covered_cycles += 1
        remaining -= batch_size

    missing_posts = max(0, required_posts - max(0, queued_total))
    return QueueCoverage(
        queued_total=max(0, queued_total),
        required_posts=required_posts,
        missing_posts=missing_posts,
        cycles=cycles,
        covered_cycles=covered_cycles,
        interval_minutes=max(1, interval_minutes),
        batch_mode=normalized_mode,
        posts_per_run=max(1, posts_per_run),
    )
