from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Iterable, Iterator

from .balancer import PHOTO, VIDEO
from .config import AppConfig


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
    priority: int = 0
    video_width: int | None = None
    video_height: int | None = None
    video_duration: int | None = None


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
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    content_fingerprint TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
                    video_width INTEGER,
                    video_height INTEGER,
                    video_duration INTEGER
                );

                CREATE TABLE IF NOT EXISTS published_media (
                    file_unique_id TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
                    published_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    channel_message_id INTEGER,
                    content_fingerprint TEXT
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

                CREATE INDEX IF NOT EXISTS idx_media_items_status_type
                    ON media_items(status, media_type, added_at);

                CREATE INDEX IF NOT EXISTS idx_publish_log_recent
                    ON publish_log(id, media_type);
                """
            )
            self._ensure_column(connection, "media_items", "content_fingerprint TEXT")
            self._ensure_column(connection, "media_items", "priority INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "media_items", "video_width INTEGER")
            self._ensure_column(connection, "media_items", "video_height INTEGER")
            self._ensure_column(connection, "media_items", "video_duration INTEGER")
            self._ensure_column(connection, "published_media", "content_fingerprint TEXT")
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
            "posting_windows": config.default_posting_windows,
            "auto_backup_enabled": "true" if config.default_auto_backup_enabled else "false",
            "auto_backup_interval_minutes": str(config.default_auto_backup_interval_minutes),
            "backup_alert_active": "false",
            "backup_after_publish_enabled": "true" if config.default_backup_after_publish_enabled else "false",
            "backup_after_publish_send_telegram": "true"
            if config.default_backup_after_publish_send_telegram
            else "false",
            "backup_after_publish_path": config.default_backup_after_publish_path,
        }
        if config.channel_id:
            defaults["channel_id"] = config.channel_id

        with self.connect() as connection:
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (key, value),
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
        priority: int = 0,
        video_width: int | None = None,
        video_height: int | None = None,
        video_duration: int | None = None,
    ) -> AddMediaResult:
        content_fingerprint = normalize_fingerprint(content_fingerprint)
        priority = max(0, int(priority or 0))
        video_width = normalize_positive_int(video_width)
        video_height = normalize_positive_int(video_height)
        video_duration = normalize_positive_int(video_duration)
        if self.is_published(file_unique_id, content_fingerprint):
            return AddMediaResult(status="already_published")

        now = utcnow_iso()
        with self.connect() as connection:
            existing = self._find_existing_media(connection, file_unique_id, content_fingerprint)
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
                        status, content_fingerprint, priority, video_width, video_height, video_duration
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        priority,
                        video_width,
                        video_height,
                        video_duration,
                    ),
                )
            except sqlite3.IntegrityError:
                row = self._find_existing_media(connection, file_unique_id, content_fingerprint)
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
        file_unique_id: str,
        content_fingerprint: str | None = None,
    ) -> sqlite3.Row | None:
        if content_fingerprint:
            return connection.execute(
                """
                SELECT *
                FROM media_items
                WHERE file_unique_id = ? OR content_fingerprint = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (file_unique_id, content_fingerprint),
            ).fetchone()
        return connection.execute(
            "SELECT * FROM media_items WHERE file_unique_id = ? LIMIT 1",
            (file_unique_id,),
        ).fetchone()

    def is_published(self, file_unique_id: str, content_fingerprint: str | None = None) -> bool:
        content_fingerprint = normalize_fingerprint(content_fingerprint)
        with self.connect() as connection:
            if content_fingerprint:
                row = connection.execute(
                    """
                    SELECT 1
                    FROM published_media
                    WHERE file_unique_id = ? OR content_fingerprint = ?
                    LIMIT 1
                    """,
                    (file_unique_id, content_fingerprint),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT 1 FROM published_media WHERE file_unique_id = ?",
                    (file_unique_id,),
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
        now = utcnow_iso()
        with self.connect() as connection:
            content_fingerprint = None
            if media_item_id is not None:
                item_row = connection.execute(
                    "SELECT content_fingerprint FROM media_items WHERE id = ?",
                    (media_item_id,),
                ).fetchone()
                if item_row:
                    content_fingerprint = normalize_fingerprint(item_row["content_fingerprint"])
            existing = connection.execute(
                """
                SELECT source
                FROM published_media
                WHERE file_unique_id = ?
                   OR (? IS NOT NULL AND content_fingerprint = ?)
                LIMIT 1
                """,
                (file_unique_id, content_fingerprint, content_fingerprint),
            ).fetchone()
            is_new = existing is None

            connection.execute(
                """
                INSERT INTO published_media(
                    file_unique_id, media_type, published_at, source, channel_message_id,
                    content_fingerprint
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_unique_id) DO UPDATE SET
                    media_type = excluded.media_type,
                    channel_message_id = COALESCE(excluded.channel_message_id, published_media.channel_message_id),
                    content_fingerprint = COALESCE(excluded.content_fingerprint, published_media.content_fingerprint)
                """,
                (file_unique_id, media_type, now, source, channel_message_id, content_fingerprint),
            )
            connection.execute(
                """
                UPDATE media_items
                SET status = ?, published_at = ?, channel_message_id = COALESCE(?, channel_message_id),
                    error = NULL
                WHERE file_unique_id = ?
                """,
                (PUBLISHED, now, channel_message_id, file_unique_id),
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

    def get_queued_item(self, media_type: str | None = None, order: str = "chronological") -> MediaItem | None:
        if order == "random":
            order_by = "priority DESC, RANDOM()"
        else:
            order_by = "priority DESC, added_at ASC, id ASC"

        if media_type:
            sql = f"""
                SELECT * FROM media_items
                WHERE status = ? AND media_type = ?
                ORDER BY {order_by}
                LIMIT 1
            """
            params: tuple[object, ...] = (QUEUED, media_type)
        else:
            sql = f"""
                SELECT * FROM media_items
                WHERE status = ?
                ORDER BY {order_by}
                LIMIT 1
            """
            params = (QUEUED,)

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
            priority=int(row["priority"]) if "priority" in row.keys() else 0,
            video_width=optional_int(row, "video_width"),
            video_height=optional_int(row, "video_height"),
            video_duration=optional_int(row, "video_duration"),
        )


def normalize_fingerprint(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def normalize_positive_int(value: int | str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def optional_int(row: sqlite3.Row, key: str) -> int | None:
    if key not in row.keys() or row[key] is None:
        return None
    return normalize_positive_int(row[key])
