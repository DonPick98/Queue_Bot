from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterable, Iterator

from .balancer import PHOTO, VIDEO
from .config import (
    AppConfig,
    LEGACY_PREVIEW_MEMBERPASS_URLS,
    PREVIEW_MEMBERPASS_LINK_VERSION,
    PREVIEW_MEMBERPASS_URL,
)
from .notifications import MAX_AUDIBLE_POSTS_PER_DAY, audible_post_positions


QUEUED = "queued"
PUBLISHED = "published"
DUPLICATE = "duplicate"
FAILED = "failed"
REMOVED = "removed"


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class MediaItem:
    id: int
    media_type: str
    file_id: str
    file_unique_id: str
    caption_html: str | None
    added_by: int | None
    added_at: str
    status: str
    failed_attempts: int
    error: str | None
    content_fingerprint: str | None = None
    content_hash: str | None = None
    visual_hash: str | None = None
    priority: int = 0
    available_after_publish_count: int = 0
    video_width: int | None = None
    video_height: int | None = None
    video_duration: int | None = None
    notification_silent: bool | None = None
    notification_local_date: str | None = None
    notification_position: int | None = None
    source_id: str | None = None
    source_label: str | None = None
    derived_tags: tuple[str, ...] = ()
    media_width: int | None = None
    media_height: int | None = None
    channel_message_id: int | None = None
    preview_thumbnail_file_id: str | None = None
    preview_eligible_at: str | None = None
    preview_published_at: str | None = None
    preview_publish_source: str | None = None
    preview_message_id: int | None = None
    preview_variant: str | None = None
    preview_memberpass_link_version: str | None = None
    preview_notification_silent: bool | None = None
    preview_notification_local_date: str | None = None
    preview_notification_position: int | None = None
    preview_failed_attempts: int = 0
    preview_error: str | None = None


@dataclass(frozen=True)
class NotificationPolicy:
    silent: bool
    local_date: str
    position: int
    planned_posts: int


@dataclass(frozen=True)
class PreviewConversionEvent:
    id: int
    kind: str
    event_key: str
    status: str
    eligible_at: str
    start_at: str
    end_at: str
    premium_count: int
    preview_count: int
    media_item_ids: tuple[int, ...]
    memberpass_link_version: str
    message_id: int | None
    attempts: int
    error: str | None


@dataclass(frozen=True)
class AddMediaResult:
    status: str
    media_item: MediaItem | None = None
    existing_status: str | None = None


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        journal_mode = os.getenv("BOT_SQLITE_JOURNAL_MODE", "").strip().upper()
        if journal_mode in {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}:
            connection.execute(f"PRAGMA journal_mode = {journal_mode}")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_type TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL UNIQUE,
                    caption_html TEXT,
                    added_by INTEGER,
                    added_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'published', 'duplicate', 'failed', 'removed')
                    ),
                    published_at TEXT,
                    channel_message_id INTEGER,
                    preview_thumbnail_file_id TEXT,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    content_fingerprint TEXT,
                    content_hash TEXT,
                    visual_hash TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
                    available_after_publish_count INTEGER NOT NULL DEFAULT 0,
                    video_width INTEGER,
                    video_height INTEGER,
                    video_duration INTEGER,
                    notification_silent INTEGER CHECK (notification_silent IN (0, 1)),
                    notification_local_date TEXT,
                    notification_position INTEGER
                );

                CREATE TABLE IF NOT EXISTS published_media (
                    file_unique_id TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
                    published_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    channel_message_id INTEGER,
                    content_fingerprint TEXT,
                    content_hash TEXT,
                    visual_hash TEXT
                );

                CREATE TABLE IF NOT EXISTS publish_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_item_id INTEGER,
                    file_unique_id TEXT NOT NULL,
                    media_type TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
                    published_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    channel_message_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS notification_day_plans (
                    local_date TEXT PRIMARY KEY,
                    planned_posts INTEGER NOT NULL,
                    audible_limit INTEGER NOT NULL,
                    next_position INTEGER NOT NULL DEFAULT 0,
                    audible_reserved INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS preview_day_plans (
                    local_date TEXT PRIMARY KEY,
                    next_position INTEGER NOT NULL DEFAULT 0,
                    audible_reserved INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS preview_conversion_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK (kind IN ('upgrade', 'weekly_recap')),
                    event_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'sent')) DEFAULT 'pending',
                    eligible_at TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    premium_count INTEGER NOT NULL,
                    preview_count INTEGER NOT NULL,
                    media_item_ids_json TEXT NOT NULL DEFAULT '[]',
                    memberpass_link_version TEXT NOT NULL,
                    message_id INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_media_items_status_type
                    ON media_items(status, media_type, added_at);

                CREATE INDEX IF NOT EXISTS idx_publish_log_recent
                    ON publish_log(id, media_type);
                """
            )
            self._ensure_column(connection, "media_items", "content_fingerprint TEXT")
            self._ensure_column(connection, "media_items", "content_hash TEXT")
            self._ensure_column(connection, "media_items", "visual_hash TEXT")
            self._ensure_column(connection, "media_items", "priority INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "media_items", "available_after_publish_count INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "media_items", "video_width INTEGER")
            self._ensure_column(connection, "media_items", "video_height INTEGER")
            self._ensure_column(connection, "media_items", "video_duration INTEGER")
            self._ensure_column(connection, "media_items", "notification_silent INTEGER")
            self._ensure_column(connection, "media_items", "notification_local_date TEXT")
            self._ensure_column(connection, "media_items", "notification_position INTEGER")
            self._ensure_column(connection, "media_items", "source_id TEXT")
            self._ensure_column(connection, "media_items", "source_label TEXT")
            self._ensure_column(connection, "media_items", "derived_tags_json TEXT")
            self._ensure_column(connection, "media_items", "media_width INTEGER")
            self._ensure_column(connection, "media_items", "media_height INTEGER")
            self._ensure_column(connection, "media_items", "preview_thumbnail_file_id TEXT")
            self._ensure_column(connection, "media_items", "preview_eligible_at TEXT")
            self._ensure_column(connection, "media_items", "preview_published_at TEXT")
            self._ensure_column(connection, "media_items", "preview_publish_source TEXT")
            self._ensure_column(connection, "media_items", "preview_message_id INTEGER")
            self._ensure_column(connection, "media_items", "preview_variant TEXT")
            self._ensure_column(connection, "media_items", "preview_memberpass_link_version TEXT")
            self._ensure_column(connection, "media_items", "preview_notification_silent INTEGER")
            self._ensure_column(connection, "media_items", "preview_notification_local_date TEXT")
            self._ensure_column(connection, "media_items", "preview_notification_position INTEGER")
            self._ensure_column(connection, "media_items", "preview_failed_attempts INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "media_items", "preview_error TEXT")
            self._ensure_column(connection, "published_media", "content_fingerprint TEXT")
            self._ensure_column(connection, "published_media", "content_hash TEXT")
            self._ensure_column(connection, "published_media", "visual_hash TEXT")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_media_items_content_fingerprint
                    ON media_items(content_fingerprint)
                    WHERE content_fingerprint IS NOT NULL AND content_fingerprint != ''
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_published_media_content_fingerprint
                    ON published_media(content_fingerprint)
                    WHERE content_fingerprint IS NOT NULL AND content_fingerprint != ''
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_media_items_content_hash
                    ON media_items(content_hash)
                    WHERE content_hash IS NOT NULL AND content_hash != ''
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_media_items_visual_hash
                    ON media_items(media_type, visual_hash)
                    WHERE visual_hash IS NOT NULL AND visual_hash != ''
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_published_media_content_hash
                    ON published_media(content_hash)
                    WHERE content_hash IS NOT NULL AND content_hash != ''
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_published_media_visual_hash
                    ON published_media(media_type, visual_hash)
                    WHERE visual_hash IS NOT NULL AND visual_hash != ''
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_media_items_preview_eligible
                    ON media_items(preview_eligible_at, preview_published_at, published_at)
                    WHERE status = 'published' AND media_type = 'photo'
                """
            )

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
        column = definition.split()[0]
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def bootstrap(self, config: AppConfig) -> None:
        defaults = {
            "interval_minutes": str(config.default_interval_minutes),
            "batch_mode": config.default_batch_mode,
            "posts_per_run": str(config.default_posts_per_run),
            "photo_ratio": str(config.default_photo_ratio),
            "video_ratio": str(config.default_video_ratio),
            "balance_window": str(config.balance_window),
            "paused": "false",
            "last_published_type": "",
            "queue_alert_active": "false",
            "queue_order": config.default_queue_order,
            "timezone": config.default_timezone,
            "audible_posts_per_day": str(config.default_audible_posts_per_day),
            "posting_windows": config.default_posting_windows,
            "schedule_mode": config.default_schedule_mode,
            "auto_backup_enabled": "true" if config.default_auto_backup_enabled else "false",
            "auto_backup_interval_minutes": str(config.default_auto_backup_interval_minutes),
            "backup_alert_active": "false",
            "backup_after_publish_enabled": "true" if config.default_backup_after_publish_enabled else "false",
            "backup_after_publish_send_telegram": "true"
            if config.default_backup_after_publish_send_telegram
            else "false",
            "backup_after_publish_path": config.default_backup_after_publish_path,
            "preview_delay_hours": str(config.preview_delay_hours),
            "preview_posts_per_day": str(config.preview_posts_per_day),
            "preview_posting_times": config.preview_posting_times,
            "preview_memberpass_url": config.preview_memberpass_url,
            "preview_memberpass_link_version": config.preview_memberpass_link_version,
            "preview_attribution": config.preview_attribution,
            "preview_watermark_enabled": "true" if config.preview_watermark_enabled else "false",
            "preview_watermark_text": config.preview_watermark_text,
            "preview_watermark_opacity": str(config.preview_watermark_opacity),
            "preview_watermark_scale_percent": str(config.preview_watermark_scale_percent),
            "preview_recap_weekday": str(config.preview_recap_weekday),
            "preview_recap_time": config.preview_recap_time,
            "preview_welcome_mode": "default",
            "preview_welcome_custom_text": "",
        }
        if config.channel_id:
            defaults["channel_id"] = config.channel_id
        if config.preview_channel_id:
            defaults["preview_channel_id"] = config.preview_channel_id

        with self.connect() as connection:
            existing_schedule_mode = connection.execute(
                "SELECT value FROM settings WHERE key = 'schedule_mode'"
            ).fetchone()
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (key, value),
                )
            memberpass_url = connection.execute(
                "SELECT value FROM settings WHERE key = 'preview_memberpass_url'"
            ).fetchone()
            memberpass_link_version = connection.execute(
                "SELECT value FROM settings WHERE key = 'preview_memberpass_link_version'"
            ).fetchone()
            stored_memberpass_url = memberpass_url["value"] if memberpass_url else ""
            stored_link_version = (
                memberpass_link_version["value"] if memberpass_link_version else ""
            )
            if (
                stored_memberpass_url in LEGACY_PREVIEW_MEMBERPASS_URLS
                or (
                    stored_memberpass_url == PREVIEW_MEMBERPASS_URL
                    and stored_link_version != PREVIEW_MEMBERPASS_LINK_VERSION
                )
            ):
                connection.execute(
                    "UPDATE settings SET value = ? WHERE key = 'preview_memberpass_url'",
                    (PREVIEW_MEMBERPASS_URL,),
                )
                connection.execute(
                    "UPDATE settings SET value = ? WHERE key = 'preview_memberpass_link_version'",
                    (PREVIEW_MEMBERPASS_LINK_VERSION,),
                )
                connection.execute(
                    "DELETE FROM settings WHERE key = 'preview_memberpass_synced_version'"
                )
            watermark_style_version = connection.execute(
                "SELECT value FROM settings WHERE key = 'preview_watermark_style_version'"
            ).fetchone()
            if watermark_style_version is None:
                connection.execute(
                    "UPDATE settings SET value = ? WHERE key = 'preview_watermark_opacity'",
                    (str(config.preview_watermark_opacity),),
                )
                connection.execute(
                    "UPDATE settings SET value = ? WHERE key = 'preview_watermark_scale_percent'",
                    (str(config.preview_watermark_scale_percent),),
                )
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES('preview_watermark_style_version', 'v2')"
                )
            if existing_schedule_mode is None:
                connection.execute(
                    """
                    INSERT INTO settings(key, value) VALUES('interval_minutes', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(config.default_interval_minutes),),
                )
                connection.execute(
                    """
                    INSERT INTO settings(key, value) VALUES('schedule_mode', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (config.default_schedule_mode,),
                )
                connection.execute("DELETE FROM settings WHERE key = 'next_publish_at'")
            connection.execute(
                """
                UPDATE media_items
                SET preview_eligible_at = strftime(
                    '%Y-%m-%dT%H:%M:%S+00:00', published_at,
                    '+' || COALESCE((SELECT value FROM settings WHERE key = 'preview_delay_hours'), '48') || ' hours'
                )
                WHERE status = 'published' AND media_type = 'photo'
                  AND published_at IS NOT NULL AND preview_eligible_at IS NULL
                """
            )

        if config.admin_user_ids:
            self.ensure_admin_ids(config.admin_user_ids)

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def update_media_file_id(self, media_item_id: int, file_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE media_items SET file_id = ? WHERE id = ?",
                (file_id, media_item_id),
            )

    def update_media_preview_thumbnail_file_id(self, media_item_id: int, file_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE media_items SET preview_thumbnail_file_id = ? WHERE id = ?",
                (file_id, media_item_id),
            )

    def get_int_setting(self, key: str, default: int) -> int:
        raw = self.get_setting(key)
        if raw is None or raw == "":
            return default
        return int(raw)

    def get_bool_setting(self, key: str, default: bool = False) -> bool:
        raw = self.get_setting(key)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def get_admin_ids(self) -> set[int]:
        raw = self.get_setting("admin_user_ids", "") or ""
        admin_ids: set[int] = set()
        for chunk in raw.split(","):
            value = chunk.strip()
            if value:
                admin_ids.add(int(value))
        return admin_ids

    def set_admin_ids(self, admin_ids: Iterable[int]) -> None:
        self.set_setting("admin_user_ids", ",".join(str(admin_id) for admin_id in sorted(set(admin_ids))))

    def ensure_admin_ids(self, admin_ids: Iterable[int]) -> None:
        merged = self.get_admin_ids()
        merged.update(admin_ids)
        self.set_setting("admin_user_ids", ",".join(str(admin_id) for admin_id in sorted(merged)))

    def add_admin_id(self, admin_id: int) -> None:
        self.ensure_admin_ids([admin_id])

    def add_media(
        self,
        media_type: str,
        file_id: str,
        file_unique_id: str,
        caption_html: str | None,
        added_by: int | None,
        content_fingerprint: str | None = None,
        content_hash: str | None = None,
        visual_hash: str | None = None,
        priority: int = 0,
        available_after_publish_count: int = 0,
        video_width: int | None = None,
        video_height: int | None = None,
        video_duration: int | None = None,
        source_id: str | int | None = None,
        source_label: str | None = None,
        derived_tags: Iterable[str] | str | None = None,
        media_width: int | None = None,
        media_height: int | None = None,
    ) -> AddMediaResult:
        content_fingerprint = normalize_fingerprint(content_fingerprint)
        content_hash = normalize_hash(content_hash)
        visual_hash = normalize_hash(visual_hash)
        priority = max(0, int(priority or 0))
        available_after_publish_count = max(0, int(available_after_publish_count or 0))
        video_width = normalize_positive_int(video_width)
        video_height = normalize_positive_int(video_height)
        video_duration = normalize_positive_int(video_duration)
        source_id = normalize_optional_text(source_id)
        source_label = normalize_optional_text(source_label)
        normalized_tags = normalize_tags(derived_tags)
        media_width = normalize_positive_int(media_width) or video_width
        media_height = normalize_positive_int(media_height) or video_height
        if self.is_published(media_type, file_unique_id, content_fingerprint, content_hash, visual_hash):
            return AddMediaResult(status="already_published")

        now = utcnow_iso()
        with self.connect() as connection:
            existing = self._find_existing_media(
                connection,
                media_type,
                file_unique_id,
                content_fingerprint,
                content_hash,
                visual_hash,
            )
            if existing:
                item = self._row_to_media_item(existing)
                return AddMediaResult(
                    status="duplicate",
                    media_item=item,
                    existing_status=item.status,
                )
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO media_items(
                        media_type, file_id, file_unique_id, caption_html, added_by, added_at,
                        status, content_fingerprint, content_hash, visual_hash, priority,
                        available_after_publish_count, video_width, video_height, video_duration,
                        source_id, source_label, derived_tags_json, media_width, media_height
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        media_type,
                        file_id,
                        file_unique_id,
                        caption_html,
                        added_by,
                        now,
                        QUEUED,
                        content_fingerprint,
                        content_hash,
                        visual_hash,
                        priority,
                        available_after_publish_count,
                        video_width,
                        video_height,
                        video_duration,
                        source_id,
                        source_label,
                        json.dumps(normalized_tags, ensure_ascii=False),
                        media_width,
                        media_height,
                    ),
                )
            except sqlite3.IntegrityError:
                row = self._find_existing_media(
                    connection,
                    media_type,
                    file_unique_id,
                    content_fingerprint,
                    content_hash,
                    visual_hash,
                )
                item = self._row_to_media_item(row) if row else None
                return AddMediaResult(
                    status="duplicate",
                    media_item=item,
                    existing_status=item.status if item else None,
                )

            row = connection.execute(
                "SELECT * FROM media_items WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return AddMediaResult(status="queued", media_item=self._row_to_media_item(row))

    def _find_existing_media(
        self,
        connection: sqlite3.Connection,
        media_type: str,
        file_unique_id: str,
        content_fingerprint: str | None = None,
        content_hash: str | None = None,
        visual_hash: str | None = None,
    ) -> sqlite3.Row | None:
        clauses = ["file_unique_id = ?"]
        params: list[object] = [file_unique_id]
        if content_fingerprint:
            clauses.append("content_fingerprint = ?")
            params.append(content_fingerprint)
        if content_hash:
            clauses.append("content_hash = ?")
            params.append(content_hash)
        if visual_hash:
            clauses.append("(media_type = ? AND visual_hash = ?)")
            params.extend([media_type, visual_hash])
        return connection.execute(
            f"""
            SELECT *
            FROM media_items
            WHERE {" OR ".join(clauses)}
            ORDER BY id ASC
            LIMIT 1
            """,
            params,
        ).fetchone()

    def is_published(
        self,
        media_type: str,
        file_unique_id: str,
        content_fingerprint: str | None = None,
        content_hash: str | None = None,
        visual_hash: str | None = None,
    ) -> bool:
        content_fingerprint = normalize_fingerprint(content_fingerprint)
        content_hash = normalize_hash(content_hash)
        visual_hash = normalize_hash(visual_hash)
        clauses = ["file_unique_id = ?"]
        params: list[object] = [file_unique_id]
        if content_fingerprint:
            clauses.append("content_fingerprint = ?")
            params.append(content_fingerprint)
        if content_hash:
            clauses.append("content_hash = ?")
            params.append(content_hash)
        if visual_hash:
            clauses.append("(media_type = ? AND visual_hash = ?)")
            params.extend([media_type, visual_hash])
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT 1
                FROM published_media
                WHERE {" OR ".join(clauses)}
                LIMIT 1
                """,
                params,
            ).fetchone()
        return row is not None

    def mark_published(
        self,
        file_unique_id: str,
        media_type: str,
        source: str,
        channel_message_id: int | None = None,
        media_item_id: int | None = None,
    ) -> bool:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat(timespec="seconds")
        preview_eligible_at = (now_dt + timedelta(hours=self.get_int_setting("preview_delay_hours", 48))).isoformat(timespec="seconds")
        with self.connect() as connection:
            content_fingerprint = None
            content_hash = None
            visual_hash = None
            if media_item_id is not None:
                item_row = connection.execute(
                    "SELECT content_fingerprint, content_hash, visual_hash FROM media_items WHERE id = ?",
                    (media_item_id,),
                ).fetchone()
                if item_row:
                    content_fingerprint = normalize_fingerprint(item_row["content_fingerprint"])
                    content_hash = normalize_hash(item_row["content_hash"])
                    visual_hash = normalize_hash(item_row["visual_hash"])
            existing = connection.execute(
                """
                SELECT source
                FROM published_media
                WHERE file_unique_id = ?
                   OR (? IS NOT NULL AND content_fingerprint = ?)
                   OR (? IS NOT NULL AND content_hash = ?)
                   OR (? IS NOT NULL AND media_type = ? AND visual_hash = ?)
                LIMIT 1
                """,
                (
                    file_unique_id,
                    content_fingerprint,
                    content_fingerprint,
                    content_hash,
                    content_hash,
                    visual_hash,
                    media_type,
                    visual_hash,
                ),
            ).fetchone()
            is_new = existing is None

            connection.execute(
                """
                INSERT INTO published_media(
                    file_unique_id, media_type, published_at, source, channel_message_id,
                    content_fingerprint, content_hash, visual_hash
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_unique_id) DO UPDATE SET
                    media_type = excluded.media_type,
                    channel_message_id = COALESCE(excluded.channel_message_id, published_media.channel_message_id),
                    content_fingerprint = COALESCE(excluded.content_fingerprint, published_media.content_fingerprint),
                    content_hash = COALESCE(excluded.content_hash, published_media.content_hash),
                    visual_hash = COALESCE(excluded.visual_hash, published_media.visual_hash)
                """,
                (
                    file_unique_id,
                    media_type,
                    now,
                    source,
                    channel_message_id,
                    content_fingerprint,
                    content_hash,
                    visual_hash,
                ),
            )
            connection.execute(
                """
                UPDATE media_items
                SET status = ?, published_at = ?, channel_message_id = COALESCE(?, channel_message_id),
                    error = NULL,
                    preview_eligible_at = CASE
                        WHEN media_type = 'photo' THEN COALESCE(preview_eligible_at, ?)
                        ELSE NULL
                    END
                WHERE file_unique_id = ?
                """,
                (
                    PUBLISHED,
                    now,
                    channel_message_id,
                    preview_eligible_at,
                    file_unique_id,
                ),
            )
            if is_new:
                connection.execute(
                    """
                    INSERT INTO publish_log(
                        media_item_id, file_unique_id, media_type, published_at, source, channel_message_id
                    )
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (media_item_id, file_unique_id, media_type, now, source, channel_message_id),
                )
        return is_new

    def mark_failed(self, media_item_id: int, error: str, max_attempts: int = 3) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT failed_attempts FROM media_items WHERE id = ?",
                (media_item_id,),
            ).fetchone()
            attempts = (row["failed_attempts"] if row else 0) + 1
            status = FAILED if attempts >= max_attempts else QUEUED
            connection.execute(
                """
                UPDATE media_items
                SET failed_attempts = ?, status = ?, error = ?
                WHERE id = ?
                """,
                (attempts, status, error[:1000], media_item_id),
            )

    def get_or_assign_notification_policy(
        self,
        media_item_id: int,
        local_date: str,
        planned_posts: int,
        audible_limit: int,
    ) -> NotificationPolicy:
        planned = max(1, int(planned_posts))
        limit = min(MAX_AUDIBLE_POSTS_PER_DAY, max(0, int(audible_limit)), planned)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                """
                SELECT notification_silent, notification_local_date, notification_position
                FROM media_items
                WHERE id = ?
                """,
                (media_item_id,),
            ).fetchone()
            if item is None:
                raise ValueError(f"Media item {media_item_id} non trovato")
            if item["notification_silent"] is not None:
                plan = connection.execute(
                    "SELECT planned_posts FROM notification_day_plans WHERE local_date = ?",
                    (item["notification_local_date"],),
                ).fetchone()
                return NotificationPolicy(
                    silent=bool(item["notification_silent"]),
                    local_date=str(item["notification_local_date"]),
                    position=int(item["notification_position"]),
                    planned_posts=int(plan["planned_posts"]) if plan else planned,
                )

            connection.execute(
                """
                INSERT OR IGNORE INTO notification_day_plans(
                    local_date, planned_posts, audible_limit, next_position, audible_reserved
                )
                VALUES(?, ?, ?, 0, 0)
                """,
                (local_date, planned, limit),
            )
            plan = connection.execute(
                """
                SELECT planned_posts, audible_limit, next_position, audible_reserved
                FROM notification_day_plans
                WHERE local_date = ?
                """,
                (local_date,),
            ).fetchone()
            position = int(plan["next_position"]) + 1
            audible = (
                position in audible_post_positions(int(plan["planned_posts"]), int(plan["audible_limit"]))
                and int(plan["audible_reserved"]) < int(plan["audible_limit"])
            )
            connection.execute(
                """
                UPDATE notification_day_plans
                SET next_position = ?, audible_reserved = audible_reserved + ?
                WHERE local_date = ?
                """,
                (position, 1 if audible else 0, local_date),
            )
            connection.execute(
                """
                UPDATE media_items
                SET notification_silent = ?, notification_local_date = ?, notification_position = ?
                WHERE id = ?
                """,
                (0 if audible else 1, local_date, position, media_item_id),
            )
            return NotificationPolicy(
                silent=not audible,
                local_date=local_date,
                position=position,
                planned_posts=int(plan["planned_posts"]),
            )

    def list_preview_candidates(self, eligible_at: str, limit: int = 100) -> list[MediaItem]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM media_items
                WHERE status = ?
                  AND media_type = ?
                  AND preview_eligible_at IS NOT NULL
                  AND preview_eligible_at <= ?
                  AND preview_published_at IS NULL
                ORDER BY preview_eligible_at ASC, published_at ASC, id ASC
                LIMIT ?
                """,
                (PUBLISHED, PHOTO, eligible_at, max(1, int(limit))),
            ).fetchall()
        return [self._row_to_media_item(row) for row in rows]

    def earliest_pending_preview_eligible_at(self, after: str | None = None) -> str | None:
        query = """
            SELECT MIN(preview_eligible_at) AS eligible_at
            FROM media_items
            WHERE status = ?
              AND media_type = ?
              AND preview_eligible_at IS NOT NULL
              AND preview_published_at IS NULL
        """
        parameters: list[object] = [PUBLISHED, PHOTO]
        if after is not None:
            query += " AND preview_eligible_at > ?"
            parameters.append(after)
        with self.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return str(row["eligible_at"]) if row and row["eligible_at"] else None

    def preview_history_between(self, start_at: str, end_at: str) -> list[MediaItem]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM media_items
                WHERE preview_published_at >= ? AND preview_published_at < ?
                ORDER BY preview_published_at ASC, id ASC
                """,
                (start_at, end_at),
            ).fetchall()
        return [self._row_to_media_item(row) for row in rows]

    def latest_preview_item(self) -> MediaItem | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM media_items
                WHERE preview_published_at IS NOT NULL
                ORDER BY preview_published_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._row_to_media_item(row) if row else None

    def get_or_assign_preview_notification_policy(
        self,
        media_item_id: int,
        local_date: str,
    ) -> NotificationPolicy:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                """
                SELECT preview_notification_silent, preview_notification_local_date,
                       preview_notification_position
                FROM media_items
                WHERE id = ?
                """,
                (media_item_id,),
            ).fetchone()
            if item is None:
                raise ValueError(f"Media item {media_item_id} non trovato")

            connection.execute(
                """
                INSERT OR IGNORE INTO preview_day_plans(local_date, next_position, audible_reserved)
                VALUES(?, 0, 0)
                """,
                (local_date,),
            )
            if item["preview_notification_silent"] is not None:
                silent = bool(item["preview_notification_silent"])
                if not silent:
                    connection.execute(
                        """
                        UPDATE preview_day_plans
                        SET audible_reserved = 1
                        WHERE local_date = ?
                        """,
                        (local_date,),
                    )
                return NotificationPolicy(
                    silent=silent,
                    local_date=str(item["preview_notification_local_date"]),
                    position=int(item["preview_notification_position"]),
                    planned_posts=2,
                )

            plan = connection.execute(
                "SELECT next_position, audible_reserved FROM preview_day_plans WHERE local_date = ?",
                (local_date,),
            ).fetchone()
            position = int(plan["next_position"]) + 1
            audible = int(plan["audible_reserved"]) == 0
            connection.execute(
                """
                UPDATE preview_day_plans
                SET next_position = ?, audible_reserved = audible_reserved + ?
                WHERE local_date = ?
                """,
                (position, 1 if audible else 0, local_date),
            )
            connection.execute(
                """
                UPDATE media_items
                SET preview_notification_silent = ?, preview_notification_local_date = ?,
                    preview_notification_position = ?
                WHERE id = ?
                """,
                (0 if audible else 1, local_date, position, media_item_id),
            )
            return NotificationPolicy(
                silent=not audible,
                local_date=local_date,
                position=position,
                planned_posts=2,
            )

    def mark_preview_published(
        self,
        media_item_id: int,
        message_id: int,
        published_at: str | None = None,
        variant: str = "full_photo",
        memberpass_link_version: str | None = None,
        publish_source: str = "scheduled",
    ) -> None:
        link_version = memberpass_link_version or self.get_setting(
            "preview_memberpass_link_version",
            PREVIEW_MEMBERPASS_LINK_VERSION,
        ) or PREVIEW_MEMBERPASS_LINK_VERSION
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE media_items
                SET preview_published_at = ?, preview_message_id = ?, preview_variant = ?,
                    preview_memberpass_link_version = ?, preview_publish_source = ?,
                    preview_error = NULL
                WHERE id = ? AND preview_published_at IS NULL
                """,
                (
                    published_at or utcnow_iso(),
                    message_id,
                    variant,
                    link_version,
                    publish_source,
                    media_item_id,
                ),
            )

    def mark_preview_failed(self, media_item_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE media_items
                SET preview_failed_attempts = preview_failed_attempts + 1, preview_error = ?
                WHERE id = ?
                """,
                (error[:1000], media_item_id),
            )

    def premium_count_between(self, start_at: str, end_at: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM publish_log WHERE published_at >= ? AND published_at < ?",
                (start_at, end_at),
            ).fetchone()
        return int(row["count"] or 0)

    def preview_count_between(self, start_at: str, end_at: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM media_items
                WHERE preview_published_at >= ? AND preview_published_at < ?
                """,
                (start_at, end_at),
            ).fetchone()
        return int(row["count"] or 0)

    def scheduled_preview_count_between(self, start_at: str, end_at: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM media_items
                WHERE preview_published_at >= ? AND preview_published_at < ?
                  AND COALESCE(preview_publish_source, 'scheduled') = 'scheduled'
                """,
                (start_at, end_at),
            ).fetchone()
        return int(row["count"] or 0)

    def preview_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM media_items WHERE preview_published_at IS NOT NULL"
            ).fetchone()
        return int(row["count"] or 0)

    def latest_preview_timestamps(self, limit: int) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT preview_published_at FROM media_items
                WHERE preview_published_at IS NOT NULL
                ORDER BY preview_published_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [str(row["preview_published_at"]) for row in reversed(rows)]

    def published_photo_ids_between(self, start_at: str, end_at: str, limit: int = 12) -> tuple[int, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM media_items
                WHERE status = ? AND media_type = ? AND published_at >= ? AND published_at < ?
                ORDER BY published_at DESC, id DESC
                LIMIT ?
                """,
                (PUBLISHED, PHOTO, start_at, end_at, max(1, int(limit))),
            ).fetchall()
        return tuple(int(row["id"]) for row in reversed(rows))

    def published_mosaic_candidates_between(
        self,
        start_at: str,
        end_at: str,
        limit: int = 256,
    ) -> list[MediaItem]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM media_items
                WHERE status = ?
                  AND media_type IN (?, ?)
                  AND published_at >= ?
                  AND published_at < ?
                ORDER BY published_at ASC, id ASC
                LIMIT ?
                """,
                (PUBLISHED, PHOTO, VIDEO, start_at, end_at, max(1, int(limit))),
            ).fetchall()
        return [self._row_to_media_item(row) for row in rows]

    def create_preview_conversion_event(
        self,
        kind: str,
        event_key: str,
        eligible_at: str,
        start_at: str,
        end_at: str,
        premium_count: int,
        preview_count: int,
        media_item_ids: Iterable[int],
        memberpass_link_version: str,
    ) -> PreviewConversionEvent:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO preview_conversion_events(
                    kind, event_key, eligible_at, start_at, end_at, premium_count,
                    preview_count, media_item_ids_json, memberpass_link_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    event_key,
                    eligible_at,
                    start_at,
                    end_at,
                    max(0, int(premium_count)),
                    max(0, int(preview_count)),
                    json.dumps([int(value) for value in media_item_ids]),
                    memberpass_link_version,
                ),
            )
            row = connection.execute(
                "SELECT * FROM preview_conversion_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
        return self._row_to_preview_event(row)

    def pending_preview_conversion_events(self, eligible_at: str) -> list[PreviewConversionEvent]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM preview_conversion_events
                WHERE status = 'pending' AND eligible_at <= ?
                ORDER BY eligible_at ASC, id ASC
                """,
                (eligible_at,),
            ).fetchall()
        return [self._row_to_preview_event(row) for row in rows]

    def mark_preview_conversion_sent(self, event_id: int, message_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE preview_conversion_events
                SET status = 'sent', message_id = ?, error = NULL
                WHERE id = ?
                """,
                (message_id, event_id),
            )

    def preview_recap_events_needing_link_sync(
        self,
        memberpass_link_version: str,
    ) -> list[PreviewConversionEvent]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM preview_conversion_events
                WHERE kind = 'weekly_recap'
                  AND status = 'sent'
                  AND message_id IS NOT NULL
                  AND memberpass_link_version != ?
                ORDER BY id ASC
                """,
                (memberpass_link_version,),
            ).fetchall()
        return [self._row_to_preview_event(row) for row in rows]

    def mark_preview_conversion_link_synced(
        self,
        event_id: int,
        memberpass_link_version: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE preview_conversion_events
                SET memberpass_link_version = ?
                WHERE id = ?
                """,
                (memberpass_link_version, event_id),
            )

    def dismiss_preview_conversion_event(self, event_id: int, reason: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE preview_conversion_events
                SET status = 'sent', message_id = NULL, error = ?
                WHERE id = ? AND status = 'pending'
                """,
                (reason[:1000], event_id),
            )

    def mark_preview_conversion_failed(self, event_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE preview_conversion_events
                SET attempts = attempts + 1, error = ?
                WHERE id = ?
                """,
                (error[:1000], event_id),
            )

    @staticmethod
    def _row_to_preview_event(row: sqlite3.Row) -> PreviewConversionEvent:
        try:
            media_ids = tuple(int(value) for value in json.loads(row["media_item_ids_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            media_ids = ()
        return PreviewConversionEvent(
            id=int(row["id"]),
            kind=str(row["kind"]),
            event_key=str(row["event_key"]),
            status=str(row["status"]),
            eligible_at=str(row["eligible_at"]),
            start_at=str(row["start_at"]),
            end_at=str(row["end_at"]),
            premium_count=int(row["premium_count"]),
            preview_count=int(row["preview_count"]),
            media_item_ids=media_ids,
            memberpass_link_version=str(row["memberpass_link_version"]),
            message_id=optional_int(row, "message_id"),
            attempts=int(row["attempts"]),
            error=row["error"],
        )

    def mark_removed(self, media_item_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE media_items SET status = ? WHERE id = ? AND status = ?",
                (REMOVED, media_item_id, QUEUED),
            )
        return cursor.rowcount > 0

    def queued_counts_by_type(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT media_type, COUNT(*) AS count
                FROM media_items
                WHERE status = ?
                GROUP BY media_type
                """,
                (QUEUED,),
            ).fetchall()
        counts = {PHOTO: 0, VIDEO: 0}
        counts.update({row["media_type"]: row["count"] for row in rows})
        return counts

    def failed_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM media_items WHERE status = ?",
                (FAILED,),
            ).fetchone()
        return int(row["count"])

    def recent_published_counts_by_type(self, limit: int) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT media_type, COUNT(*) AS count
                FROM (
                    SELECT media_type
                    FROM publish_log
                    ORDER BY id DESC
                    LIMIT ?
                )
                GROUP BY media_type
                """,
                (limit,),
            ).fetchall()
        counts = {PHOTO: 0, VIDEO: 0}
        counts.update({row["media_type"]: row["count"] for row in rows})
        return counts

    def recent_published_media_types(self, limit: int) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT media_type
                FROM publish_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
        return [row["media_type"] for row in rows]

    def latest_published_at(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT published_at
                FROM publish_log
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return row["published_at"] if row else None

    def published_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM publish_log").fetchone()
        return int(row["count"] or 0)

    def get_queued_item(
        self,
        media_type: str | None = None,
        order: str = "chronological",
        exclude_ids: Iterable[int] | None = None,
    ) -> MediaItem | None:
        if order == "random":
            order_by = "priority DESC, RANDOM()"
        else:
            order_by = "priority DESC, added_at ASC, id ASC"

        excluded = [int(media_id) for media_id in (exclude_ids or [])]
        excluded_clause = ""
        excluded_params: list[object] = []
        if excluded:
            placeholders = ", ".join("?" for _ in excluded)
            excluded_clause = f" AND id NOT IN ({placeholders})"
            excluded_params.extend(excluded)

        if media_type:
            sql = f"""
                SELECT * FROM media_items
                WHERE status = ?
                  AND media_type = ?
                  AND available_after_publish_count <= ?{excluded_clause}
                ORDER BY {order_by}
                LIMIT 1
            """
            params: tuple[object, ...] = (QUEUED, media_type, self.published_count(), *excluded_params)
        else:
            sql = f"""
                SELECT * FROM media_items
                WHERE status = ?
                  AND available_after_publish_count <= ?{excluded_clause}
                ORDER BY {order_by}
                LIMIT 1
            """
            params = (QUEUED, self.published_count(), *excluded_params)

        with self.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return self._row_to_media_item(row) if row else None

    def get_oldest_queued(self, media_type: str | None = None) -> MediaItem | None:
        return self.get_queued_item(media_type=media_type, order="chronological")

    def list_queued(self, limit: int = 10) -> list[MediaItem]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM media_items
                WHERE status = ?
                ORDER BY priority DESC, added_at ASC, id ASC
                LIMIT ?
                """,
                (QUEUED, limit),
            ).fetchall()
        return [self._row_to_media_item(row) for row in rows]

    def list_published_photos(self, limit: int = 3, exclude_ids: Iterable[int] = ()) -> list[MediaItem]:
        excluded = tuple(dict.fromkeys(int(media_id) for media_id in exclude_ids))
        params: list[object] = [PUBLISHED, PHOTO]
        exclude_clause = ""
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            exclude_clause = f"AND id NOT IN ({placeholders})"
            params.extend(excluded)
        params.append(limit)

        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM media_items
                WHERE status = ?
                  AND media_type = ?
                  {exclude_clause}
                ORDER BY published_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_media_item(row) for row in rows]

    def find_media_by_id(self, media_item_id: int) -> MediaItem | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM media_items WHERE id = ?",
                (media_item_id,),
            ).fetchone()
        return self._row_to_media_item(row) if row else None

    @staticmethod
    def _row_to_media_item(row: sqlite3.Row) -> MediaItem:
        return MediaItem(
            id=row["id"],
            media_type=row["media_type"],
            file_id=row["file_id"],
            file_unique_id=row["file_unique_id"],
            caption_html=row["caption_html"],
            added_by=row["added_by"],
            added_at=row["added_at"],
            status=row["status"],
            failed_attempts=row["failed_attempts"],
            error=row["error"],
            content_fingerprint=row["content_fingerprint"] if "content_fingerprint" in row.keys() else None,
            content_hash=row["content_hash"] if "content_hash" in row.keys() else None,
            visual_hash=row["visual_hash"] if "visual_hash" in row.keys() else None,
            priority=int(row["priority"]) if "priority" in row.keys() else 0,
            available_after_publish_count=(
                int(row["available_after_publish_count"])
                if "available_after_publish_count" in row.keys()
                else 0
            ),
            video_width=optional_int(row, "video_width"),
            video_height=optional_int(row, "video_height"),
            video_duration=optional_int(row, "video_duration"),
            notification_silent=(
                bool(row["notification_silent"])
                if "notification_silent" in row.keys() and row["notification_silent"] is not None
                else None
            ),
            notification_local_date=(
                row["notification_local_date"] if "notification_local_date" in row.keys() else None
            ),
            notification_position=optional_int(row, "notification_position"),
            source_id=row["source_id"] if "source_id" in row.keys() else None,
            source_label=row["source_label"] if "source_label" in row.keys() else None,
            derived_tags=parse_tags(row["derived_tags_json"] if "derived_tags_json" in row.keys() else None),
            media_width=optional_int(row, "media_width"),
            media_height=optional_int(row, "media_height"),
            channel_message_id=optional_int(row, "channel_message_id"),
            preview_thumbnail_file_id=(
                row["preview_thumbnail_file_id"]
                if "preview_thumbnail_file_id" in row.keys()
                else None
            ),
            preview_eligible_at=(row["preview_eligible_at"] if "preview_eligible_at" in row.keys() else None),
            preview_published_at=(row["preview_published_at"] if "preview_published_at" in row.keys() else None),
            preview_publish_source=(
                row["preview_publish_source"]
                if "preview_publish_source" in row.keys()
                else None
            ),
            preview_message_id=optional_int(row, "preview_message_id"),
            preview_variant=(row["preview_variant"] if "preview_variant" in row.keys() else None),
            preview_memberpass_link_version=(
                row["preview_memberpass_link_version"]
                if "preview_memberpass_link_version" in row.keys()
                else None
            ),
            preview_notification_silent=(
                bool(row["preview_notification_silent"])
                if "preview_notification_silent" in row.keys()
                and row["preview_notification_silent"] is not None
                else None
            ),
            preview_notification_local_date=(
                row["preview_notification_local_date"]
                if "preview_notification_local_date" in row.keys()
                else None
            ),
            preview_notification_position=optional_int(row, "preview_notification_position"),
            preview_failed_attempts=(
                int(row["preview_failed_attempts"] or 0)
                if "preview_failed_attempts" in row.keys()
                else 0
            ),
            preview_error=row["preview_error"] if "preview_error" in row.keys() else None,
        )


def normalize_fingerprint(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def normalize_hash(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    return normalized


def normalize_positive_int(value: int | str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def normalize_optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized[:500] or None


def normalize_tags(value: Iterable[str] | str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_values: Iterable[object]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = value.replace(";", ",").split(",")
        raw_values = decoded if isinstance(decoded, list) else [decoded]
    else:
        raw_values = value
    tags: list[str] = []
    for raw in raw_values:
        tag = str(raw).strip().lower()
        if tag and tag not in tags:
            tags.append(tag[:100])
    return tuple(tags[:20])


def parse_tags(value: str | None) -> tuple[str, ...]:
    return normalize_tags(value)


def optional_int(row: sqlite3.Row, key: str) -> int | None:
    if key not in row.keys() or row[key] is None:
        return None
    return normalize_positive_int(row[key])
