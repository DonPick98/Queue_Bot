from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Europe/Rome"
DEFAULT_POSTING_WINDOWS = "all"


@dataclass(frozen=True)
class TimeWindow:
    start_minute: int
    end_minute: int

    @property
    def label(self) -> str:
        return f"{_format_minute(self.start_minute)}-{_format_minute(self.end_minute)}"


def validate_timezone(name: str) -> str:
    value = name.strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Timezone non valido. Esempio: Europe/Rome") from exc
    return value


def get_zone(name: str | None) -> ZoneInfo:
    return ZoneInfo(validate_timezone(name or DEFAULT_TIMEZONE))


def _parse_hhmm(raw: str) -> int:
    match = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?", raw.strip())
    if not match:
        raise ValueError("Orario non valido. Esempio: 10:00-23:30")
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    if hour > 24 or minute > 59 or (hour == 24 and minute != 0):
        raise ValueError("Orario non valido. Usa valori tra 00:00 e 24:00.")
    return hour * 60 + minute


def _format_minute(value: int) -> str:
    if value >= 24 * 60:
        return "24:00"
    hour, minute = divmod(value, 60)
    return f"{hour:02d}:{minute:02d}"


def parse_posting_windows(raw: str | None) -> list[TimeWindow]:
    value = (raw or "").strip().lower()
    if value in {"", "all", "always", "24h", "off"}:
        return []

    windows: list[TimeWindow] = []
    for chunk in value.replace(";", ",").split(","):
        part = chunk.strip()
        if not part:
            continue
        if "-" not in part:
            raise ValueError("Usa formato 10:00-23:30 oppure all.")
        start_raw, end_raw = part.split("-", 1)
        start = _parse_hhmm(start_raw)
        end = _parse_hhmm(end_raw)
        if start == end:
            raise ValueError("La finestra deve avere inizio e fine diversi.")
        windows.append(TimeWindow(start, end))
    return windows


def format_posting_windows(raw: str | None) -> str:
    windows = parse_posting_windows(raw)
    if not windows:
        return "all day"
    return ", ".join(window.label for window in windows)


def _minute_of_day(value: datetime, timezone_name: str) -> int:
    local = value.astimezone(get_zone(timezone_name))
    return local.hour * 60 + local.minute


def is_within_posting_windows(value: datetime, timezone_name: str, windows_raw: str | None) -> bool:
    windows = parse_posting_windows(windows_raw)
    if not windows:
        return True

    minute = _minute_of_day(value, timezone_name)
    for window in windows:
        if window.start_minute < window.end_minute:
            if window.start_minute <= minute < window.end_minute:
                return True
        elif minute >= window.start_minute or minute < window.end_minute:
            return True
    return False


def next_allowed_datetime(value: datetime, timezone_name: str, windows_raw: str | None) -> datetime:
    windows = parse_posting_windows(windows_raw)
    if not windows or is_within_posting_windows(value, timezone_name, windows_raw):
        return value.astimezone(UTC)

    zone = get_zone(timezone_name)
    local = value.astimezone(zone)
    candidates: list[datetime] = []
    for day_offset in range(8):
        day = local.date() + timedelta(days=day_offset)
        for window in windows:
            start_hour, start_minute = divmod(window.start_minute, 60)
            start_local = datetime.combine(
                day,
                time(hour=start_hour % 24, minute=start_minute),
                tzinfo=zone,
            )
            if window.start_minute >= 24 * 60:
                start_local += timedelta(days=1)
            if start_local >= local:
                candidates.append(start_local)

    if not candidates:
        return value.astimezone(UTC)
    return min(candidates).astimezone(UTC)


def next_publish_after_interval(
    now: datetime,
    interval_minutes: int,
    timezone_name: str,
    windows_raw: str | None,
) -> datetime:
    candidate = now.astimezone(UTC) + timedelta(minutes=max(1, interval_minutes))
    return next_allowed_datetime(candidate, timezone_name, windows_raw)


def next_aligned_publish_after(
    value: datetime,
    interval_minutes: int,
    timezone_name: str,
    windows_raw: str | None,
) -> datetime:
    interval = max(1, interval_minutes)
    zone = get_zone(timezone_name)
    local = value.astimezone(zone)
    midnight = datetime.combine(local.date(), time(0, 0), tzinfo=zone)
    elapsed_minutes = (local - midnight).total_seconds() / 60
    slots_elapsed = int(elapsed_minutes // interval) + 1
    candidate = midnight + timedelta(minutes=slots_elapsed * interval)
    candidate_utc = candidate.astimezone(UTC)
    allowed = next_allowed_datetime(candidate_utc, timezone_name, windows_raw)
    if allowed == candidate_utc:
        return allowed
    return next_aligned_publish_after(allowed, interval, timezone_name, windows_raw)
