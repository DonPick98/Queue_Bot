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
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS published_media (
                    file_unique_id TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
                    published_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    channel_message_id INTEGER
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
    ) -> AddMediaResult:
        if self.is_published(file_unique_id):
            return AddMediaResult(status="already_published")

        now = utcnow_iso()
        with self.connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO media_items(
                        media_type, file_id, file_unique_id, caption_html, added_by, added_at, status
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (media_type, file_id, file_unique_id, caption_html, added_by, now, QUEUED),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT * FROM media_items WHERE file_unique_id = ?",
                    (file_unique_id,),
                ).fetchone()
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

    def is_published(self, file_unique_id: str) -> bool:
        with self.connect() as connection:
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
            existing = connection.execute(
                "SELECT source FROM published_media WHERE file_unique_id = ?",
                (file_unique_id,),
            ).fetchone()
            is_new = existing is None

            connection.execute(
                """
                INSERT INTO published_media(
                    file_unique_id, media_type, published_at, source, channel_message_id
                )
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(file_unique_id) DO UPDATE SET
                    media_type = excluded.media_type,
                    channel_message_id = COALESCE(excluded.channel_message_id, published_media.channel_message_id)
                """,
                (file_unique_id, media_type, now, source, channel_message_id),
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
            order_by = "RANDOM()"
        else:
            order_by = "added_at ASC, id ASC"

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
                ORDER BY added_at ASC, id ASC
                LIMIT ?
                """,
                (QUEUED, limit),
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
        )
