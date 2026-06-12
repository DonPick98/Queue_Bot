from __future__ import annotations

import cgi
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import threading
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from .balancer import PHOTO, VIDEO
from .storage import Store


LOGGER = logging.getLogger(__name__)
PUBLIC_URL_SETTING = "last_public_base_url"


def _clean_forwarded_value(value: str | None) -> str:
    return (value or "").split(",", 1)[0].strip()


def _is_private_host(host: str) -> bool:
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].strip().lower()
    return (
        not host
        or host == "localhost"
        or host.startswith("127.")
        or host.startswith("10.")
        or host.startswith("192.168.")
        or host.endswith(".local")
    )


def normalize_public_base_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    return value.rstrip("/")


def infer_public_base_url(headers: Any) -> str | None:
    forwarded_host = _clean_forwarded_value(headers.get("X-Forwarded-Host"))
    host = forwarded_host or _clean_forwarded_value(headers.get("Host"))
    if _is_private_host(host):
        return None

    forwarded_proto = _clean_forwarded_value(headers.get("X-Forwarded-Proto"))
    proto = forwarded_proto if forwarded_proto in {"http", "https"} else "https"
    return normalize_public_base_url(f"{proto}://{host}")


def remember_public_base_url(store: Store | None, headers: Any) -> None:
    if store is None:
        return

    base_url = infer_public_base_url(headers)
    if not base_url:
        return

    try:
        store.set_setting(PUBLIC_URL_SETTING, base_url)
    except Exception:
        LOGGER.exception("Failed to remember public base URL")


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _multipart_body(fields: dict[str, str], file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----MouthQueueBoundary7MA4YWxkTrZu0gW"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            b"Content-Type: application/octet-stream\r\n\r\n",
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(chunks), boundary


def _telegram_api(token: str, method: str, payload: bytes, boundary: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{urllib.parse.quote(token, safe=':')}/{method}",
        data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API error {exc.code}: {details}") from exc
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def _telegram_api_fields(token: str, method: str, fields: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{urllib.parse.quote(token, safe=':')}/{method}",
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API error {exc.code}: {details}") from exc
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def _delete_staging_message(token: str, chat_id: str, message_id: int) -> None:
    try:
        _telegram_api_fields(token, "deleteMessage", {"chat_id": chat_id, "message_id": str(message_id)})
    except Exception:
        LOGGER.exception("Failed to delete staging message %s in chat %s", message_id, chat_id)


def _first_admin_chat_id(store: Store) -> str | None:
    admin_ids = sorted(store.get_admin_ids())
    if not admin_ids:
        return None
    return str(admin_ids[0])


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "MouthQueueHealth/1.0"
    store: Store | None = None
    bot_token: str | None = None

    def do_GET(self) -> None:
        remember_public_base_url(self.store, self.headers)
        if self.path not in {"/", "/healthz"}:
            self.send_response(404)
            self.end_headers()
            return

        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        remember_public_base_url(self.store, self.headers)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in {"/api/queue/photo", "/api/queue/video"}:
            _json_response(self, 404, {"ok": False, "error": "not_found"})
            return

        if self.store is None or self.bot_token is None:
            _json_response(self, 503, {"ok": False, "error": "queue_api_not_ready"})
            return

        expected_token = os.getenv("QUEUE_API_TOKEN", "").strip()
        if not expected_token:
            _json_response(self, 403, {"ok": False, "error": "queue_api_disabled"})
            return

        query = urllib.parse.parse_qs(parsed.query)
        supplied_token = ""
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            supplied_token = auth_header.removeprefix("Bearer ").strip()
        elif "token" in query:
            supplied_token = query["token"][0]

        if supplied_token != expected_token:
            _json_response(self, 401, {"ok": False, "error": "unauthorized"})
            return

        max_mb = int(os.getenv("QUEUE_API_MAX_MB", "50"))
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length > max_mb * 1024 * 1024:
            _json_response(self, 413, {"ok": False, "error": f"file_too_large_max_{max_mb}mb"})
            return

        media_type = PHOTO if parsed.path.endswith("/photo") else VIDEO
        result = self._queue_upload(media_type)
        _json_response(self, result[0], result[1])

    def _queue_upload(self, media_type: str) -> tuple[int, dict[str, Any]]:
        assert self.store is not None
        assert self.bot_token is not None

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            },
        )
        upload = form["media"] if "media" in form else None
        if upload is None or not getattr(upload, "filename", None):
            return 400, {"ok": False, "error": "missing_media_file_field"}

        content = upload.file.read()
        if not content:
            return 400, {"ok": False, "error": "empty_media"}

        staging_chat_id = os.getenv("QUEUE_API_STAGING_CHAT_ID", "").strip() or _first_admin_chat_id(self.store)
        if not staging_chat_id:
            return 400, {"ok": False, "error": "missing_staging_chat_id_or_admin"}

        caption = ""
        if "caption_html" in form:
            caption = str(form.getfirst("caption_html") or "")
        elif "caption" in form:
            caption = str(form.getfirst("caption") or "")
        content_fingerprint = str(form.getfirst("content_fingerprint") or "").strip() or None
        try:
            priority = max(0, int(str(form.getfirst("priority") or "0").strip() or "0"))
        except ValueError:
            priority = 0

        fields = {"chat_id": staging_chat_id}
        if caption:
            fields["caption"] = caption
            if "caption_html" in form:
                fields["parse_mode"] = "HTML"

        file_field = "photo" if media_type == PHOTO else "video"
        method = "sendPhoto" if media_type == PHOTO else "sendVideo"
        body, boundary = _multipart_body(fields, file_field, upload.filename, content)

        try:
            telegram_response = _telegram_api(self.bot_token, method, body, boundary)
        except Exception as exc:
            LOGGER.exception("Queue API upload failed")
            return 502, {"ok": False, "error": str(exc)}

        message = telegram_response["result"]
        if media_type == PHOTO:
            telegram_media = message["photo"][-1]
            video_width = None
            video_height = None
            video_duration = None
        else:
            telegram_media = message["video"]
            video_width = telegram_media.get("width")
            video_height = telegram_media.get("height")
            video_duration = telegram_media.get("duration")

        add_result = self.store.add_media(
            media_type=media_type,
            file_id=telegram_media["file_id"],
            file_unique_id=telegram_media["file_unique_id"],
            caption_html=caption or None,
            added_by=None,
            content_fingerprint=content_fingerprint,
            priority=priority,
            video_width=video_width,
            video_height=video_height,
            video_duration=video_duration,
        )

        if os.getenv("QUEUE_API_DELETE_STAGING", "false").strip().lower() in {"1", "true", "yes", "on"}:
            _delete_staging_message(self.bot_token, staging_chat_id, message["message_id"])

        return 200, {
            "ok": True,
            "queue_status": add_result.status,
            "media_id": add_result.media_item.id if add_result.media_item else None,
            "media_type": media_type,
            "staging_chat_id": staging_chat_id,
            "staging_message_id": message["message_id"],
        }

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.debug("Health endpoint: " + format, *args)


def start_http_server_from_env(store: Store | None = None, bot_token: str | None = None) -> ThreadingHTTPServer | None:
    raw_port = os.getenv("PORT") or os.getenv("HEALTH_PORT")
    if not raw_port:
        return None

    try:
        port = int(raw_port)
    except ValueError:
        LOGGER.warning("Ignoring invalid PORT/HEALTH_PORT value: %r", raw_port)
        return None

    if port < 1:
        return None

    host = os.getenv("HEALTH_HOST", "0.0.0.0")
    HealthHandler.store = store
    HealthHandler.bot_token = bot_token
    server = ThreadingHTTPServer((host, port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    LOGGER.info("Health endpoint listening on %s:%s", host, port)
    return server


start_health_server_from_env = start_http_server_from_env
