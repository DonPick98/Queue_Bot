from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .queue_order import parse_queue_order
from .scheduling import DEFAULT_POSTING_WINDOWS, DEFAULT_TIMEZONE, parse_posting_windows, validate_timezone


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    load_dotenv()


def _parse_admin_ids(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return ()

    admin_ids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        value = chunk.strip()
        if not value:
            continue
        try:
            admin_ids.append(int(value))
        except ValueError as exc:
            raise ValueError(f"ADMIN_USER_IDS contiene un ID non numerico: {value!r}") from exc
    return tuple(dict.fromkeys(admin_ids))


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} deve essere un numero intero.") from exc
    if value < 1:
        raise ValueError(f"{name} deve essere almeno 1.")
    return value


def _batch_mode(name: str, default: str = "fixed") -> str:
    raw = os.getenv(name, default).strip().lower()
    if raw not in {"fixed", "auto"}:
        raise ValueError(f"{name} deve essere 'fixed' oppure 'auto'.")
    return raw


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _queue_order(name: str, default: str = "random") -> str:
    raw = os.getenv(name, default)
    return parse_queue_order(raw)


def _posting_windows(name: str, default: str = DEFAULT_POSTING_WINDOWS) -> str:
    raw = os.getenv(name, default).strip() or default
    parse_posting_windows(raw)
    return raw


def _schedule_mode(name: str, default: str = "anchored") -> str:
    raw = os.getenv(name, default).strip().lower() or default
    if raw not in {"anchored", "interval"}:
        raise ValueError(f"{name} deve essere 'anchored' oppure 'interval'.")
    return raw


@dataclass(frozen=True)
class AppConfig:
    bot_token: str
    database_path: Path
    channel_id: str | None
    admin_user_ids: tuple[int, ...]
    default_interval_minutes: int
    default_batch_mode: str
    default_posts_per_run: int
    default_photo_ratio: int
    default_video_ratio: int
    balance_window: int
    default_queue_order: str
    default_timezone: str
    default_posting_windows: str
    default_schedule_mode: str
    default_auto_backup_enabled: bool
    default_auto_backup_interval_minutes: int
    default_backup_after_publish_enabled: bool
    default_backup_after_publish_send_telegram: bool
    default_backup_after_publish_path: str
    backup_auto_restore_enabled: bool
    backup_auto_restore_if_empty: bool
    backup_before_shutdown_enabled: bool
    backup_telegram_auto_download_enabled: bool

    @classmethod
    def from_env(cls) -> "AppConfig":
        _load_dotenv_if_available()

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        database_path = Path(os.getenv("DATABASE_PATH", "./data/bot.sqlite3")).expanduser()
        channel_id = os.getenv("TELEGRAM_CHANNEL_ID", "").strip() or None

        return cls(
            bot_token=token,
            database_path=database_path,
            channel_id=channel_id,
            admin_user_ids=_parse_admin_ids(os.getenv("ADMIN_USER_IDS")),
            default_interval_minutes=_positive_int("DEFAULT_POST_INTERVAL_MINUTES", 60),
            default_batch_mode=_batch_mode("DEFAULT_BATCH_MODE", "fixed"),
            default_posts_per_run=_positive_int("DEFAULT_POSTS_PER_RUN", 1),
            default_photo_ratio=_positive_int("DEFAULT_PHOTO_RATIO", 1),
            default_video_ratio=_positive_int("DEFAULT_VIDEO_RATIO", 1),
            balance_window=_positive_int("BALANCE_WINDOW", 20),
            default_queue_order=_queue_order("DEFAULT_QUEUE_ORDER", "random"),
            default_timezone=validate_timezone(os.getenv("DEFAULT_TIMEZONE", DEFAULT_TIMEZONE)),
            default_posting_windows=_posting_windows("DEFAULT_POSTING_WINDOWS", DEFAULT_POSTING_WINDOWS),
            default_schedule_mode=_schedule_mode("DEFAULT_SCHEDULE_MODE", "anchored"),
            default_auto_backup_enabled=_bool("AUTO_BACKUP_ENABLED", False),
            default_auto_backup_interval_minutes=_positive_int("AUTO_BACKUP_INTERVAL_MINUTES", 24 * 60),
            default_backup_after_publish_enabled=_bool("BACKUP_AFTER_PUBLISH_ENABLED", True),
            default_backup_after_publish_send_telegram=_bool("BACKUP_AFTER_PUBLISH_SEND_TELEGRAM", False),
            default_backup_after_publish_path=os.getenv(
                "BACKUP_AFTER_PUBLISH_PATH",
                "./state_backups/latest-state.zip",
            ).strip()
            or "./state_backups/latest-state.zip",
            backup_auto_restore_enabled=_bool("BACKUP_AUTO_RESTORE_ENABLED", True),
            backup_auto_restore_if_empty=_bool("BACKUP_AUTO_RESTORE_IF_EMPTY", True),
            backup_before_shutdown_enabled=_bool("BACKUP_BEFORE_SHUTDOWN_ENABLED", True),
            backup_telegram_auto_download_enabled=_bool("BACKUP_TELEGRAM_AUTO_DOWNLOAD_ENABLED", True),
        )
