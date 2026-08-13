from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo


PREVIEW_JOB_INTERVAL_SECONDS = 300
PREVIEW_MAX_POSTS_PER_DAY = 2


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
