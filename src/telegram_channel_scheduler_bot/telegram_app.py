from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import logging
import math
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .balancer import PHOTO, VIDEO, choose_media_type
from .config import AppConfig
from .health import PUBLIC_URL_SETTING, normalize_public_base_url, start_http_server_from_env
from .posting_plan import (
    QUEUE_ALERT_HOURS,
    QueueCoverage,
    auto_posts_per_run,
    estimate_queue_coverage,
    initial_next_publish_at,
    seconds_until_next_publish,
)
from .queue_order import parse_queue_order
from .scheduling import (
    DEFAULT_TIMEZONE,
    format_posting_windows,
    is_within_posting_windows,
    next_allowed_datetime,
    next_publish_after_interval,
    parse_posting_windows,
    validate_timezone,
)
from .state_archive import (
    create_rolling_state_backup,
    create_state_backup,
    restore_latest_backup_if_needed,
    restore_state_backup,
)
from .storage import AddMediaResult, MediaItem, Store


LOGGER = logging.getLogger(__name__)
PUBLISH_JOB_NAME = "publisher"
BACKUP_JOB_NAME = "auto_backup"
MAX_POSTS_PER_RUN = 20
PUBLIC_URL_ENV_KEYS = (
    "PUBLIC_BASE_URL",
    "APP_URL",
    "APP_DOMAIN",
    "SERVICE_URL",
    "TRANGER_PUBLIC_URL",
    "TRANGER_APP_URL",
    "RENDER_EXTERNAL_URL",
    "RAILWAY_PUBLIC_DOMAIN",
)


@dataclass(frozen=True)
class ExtractedMedia:
    media_type: str
    file_id: str
    file_unique_id: str
    caption_html: str | None


@dataclass(frozen=True)
class PublishOutcome:
    status: str
    media_item: MediaItem | None = None
    message: str = ""


def extract_media(message: Message) -> ExtractedMedia | None:
    if message.photo:
        photo = message.photo[-1]
        return ExtractedMedia(
            media_type=PHOTO,
            file_id=photo.file_id,
            file_unique_id=photo.file_unique_id,
            caption_html=message.caption_html if message.caption else None,
        )
    if message.video:
        video = message.video
        return ExtractedMedia(
            media_type=VIDEO,
            file_id=video.file_id,
            file_unique_id=video.file_unique_id,
            caption_html=message.caption_html if message.caption else None,
        )
    return None


def get_store(application_or_context: Application | ContextTypes.DEFAULT_TYPE) -> Store:
    application = (
        application_or_context.application
        if hasattr(application_or_context, "application")
        else application_or_context
    )
    return application.bot_data["store"]


def format_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours, remaining = divmod(minutes, 60)
    if remaining == 0:
        return f"{hours} h"
    return f"{hours} h {remaining} min"


def utcnow() -> datetime:
    return datetime.now(UTC)


def datetime_to_setting(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_datetime_setting(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def get_timezone_name(store: Store) -> str:
    raw = store.get_setting("timezone", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
    try:
        return validate_timezone(raw)
    except ValueError:
        return DEFAULT_TIMEZONE


def get_posting_windows(store: Store) -> str:
    return store.get_setting("posting_windows", "all") or "all"


def format_datetime_for_user(value: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    return value.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M")


def parse_duration_minutes(raw: str) -> int:
    value = raw.strip().lower().replace(",", ".")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(m|min|h|ore?|d|giorni?)?", value)
    if not match:
        raise ValueError("Formato non valido. Esempi: 30, 45m, 2h, 1d.")
    amount = float(match.group(1))
    unit = match.group(2) or "m"
    multiplier = 1
    if unit in {"h", "ora", "ore"}:
        multiplier = 60
    elif unit in {"d", "giorno", "giorni"}:
        multiplier = 60 * 24
    minutes = int(amount * multiplier)
    if minutes < 1:
        raise ValueError("L'intervallo minimo e 1 minuto.")
    return minutes


def parse_ratio(args: list[str]) -> tuple[int, int]:
    if len(args) == 1 and ":" in args[0]:
        chunks = args[0].split(":", 1)
    elif len(args) == 2:
        chunks = args
    else:
        raise ValueError("Usa /set_ratio 1 1 oppure /set_ratio 2:1.")

    try:
        photo_ratio = int(chunks[0])
        video_ratio = int(chunks[1])
    except ValueError as exc:
        raise ValueError("Il rapporto deve contenere numeri interi.") from exc

    if photo_ratio < 1 or video_ratio < 1:
        raise ValueError("Entrambi i valori devono essere almeno 1.")
    return photo_ratio, video_ratio


def parse_post_count(raw: str, max_count: int = MAX_POSTS_PER_RUN) -> int:
    try:
        count = int(raw)
    except ValueError as exc:
        raise ValueError("Il numero di post deve essere un intero.") from exc
    if count < 1:
        raise ValueError("Il numero di post deve essere almeno 1.")
    if count > max_count:
        raise ValueError(f"Per sicurezza il massimo e {max_count} post per ciclo.")
    return count


def resolve_posts_per_run(store: Store, queued_counts: dict[str, int] | None = None) -> int:
    mode = (store.get_setting("batch_mode", "fixed") or "fixed").strip().lower()
    if mode == "auto":
        counts = queued_counts if queued_counts is not None else store.queued_counts_by_type()
        return auto_posts_per_run(sum(counts.values()))
    return store.get_int_setting("posts_per_run", 1)


def describe_batch_setting(store: Store, queued_counts: dict[str, int]) -> str:
    mode = (store.get_setting("batch_mode", "fixed") or "fixed").strip().lower()
    if mode == "auto":
        effective = resolve_posts_per_run(store, queued_counts)
        return f"auto, adesso {effective} (>20 = 2, >40 = 3)"
    return str(store.get_int_setting("posts_per_run", 1))


def get_queue_coverage(store: Store, queued_counts: dict[str, int] | None = None) -> QueueCoverage:
    counts = queued_counts if queued_counts is not None else store.queued_counts_by_type()
    return estimate_queue_coverage(
        queued_total=sum(counts.values()),
        interval_minutes=store.get_int_setting("interval_minutes", 60),
        batch_mode=store.get_setting("batch_mode", "fixed") or "fixed",
        posts_per_run=store.get_int_setting("posts_per_run", 1),
    )


def format_coverage_status(coverage: QueueCoverage) -> str:
    if coverage.covers:
        return f"ok ({coverage.queued_total}/{coverage.required_posts} media)"
    covered = format_duration(coverage.covered_minutes)
    return (
        f"insufficiente: mancano {coverage.missing_posts} media "
        f"({coverage.queued_total}/{coverage.required_posts}, copre circa {covered})"
    )


def get_or_initialize_next_publish_at(store: Store, now: datetime | None = None) -> datetime:
    current_time = now or utcnow()
    existing = parse_datetime_setting(store.get_setting("next_publish_at"))
    if existing is not None:
        adjusted = next_allowed_datetime(existing, get_timezone_name(store), get_posting_windows(store))
        if adjusted != existing:
            store.set_setting("next_publish_at", datetime_to_setting(adjusted))
        return adjusted

    last_published_at = parse_datetime_setting(store.latest_published_at())
    next_publish_at = initial_next_publish_at(
        current_time,
        store.get_int_setting("interval_minutes", 60),
        last_published_at=last_published_at,
    )
    next_publish_at = next_allowed_datetime(next_publish_at, get_timezone_name(store), get_posting_windows(store))
    store.set_setting("next_publish_at", datetime_to_setting(next_publish_at))
    return next_publish_at


def set_next_publish_after_interval(store: Store, now: datetime | None = None) -> datetime:
    next_publish_at = next_publish_after_interval(
        now or utcnow(),
        store.get_int_setting("interval_minutes", 60),
        get_timezone_name(store),
        get_posting_windows(store),
    )
    store.set_setting("next_publish_at", datetime_to_setting(next_publish_at))
    return next_publish_at


def format_next_publish_status(
    next_publish_at: datetime,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> str:
    current_time = now or utcnow()
    if next_publish_at <= current_time:
        return "appena possibile"
    minutes = max(1, math.ceil((next_publish_at - current_time).total_seconds() / 60))
    return f"{format_datetime_for_user(next_publish_at, timezone_name)} (tra {format_duration(minutes)})"


def parse_local_next_publish(raw: str, store: Store, now: datetime | None = None) -> datetime:
    value = raw.strip()
    zone = ZoneInfo(get_timezone_name(store))
    current_local = (now or utcnow()).astimezone(zone)

    if re.fullmatch(r"\d{1,2}:?\d{0,2}", value):
        chunks = value.split(":", 1)
        hour = int(chunks[0])
        minute = int(chunks[1]) if len(chunks) == 2 and chunks[1] else 0
        if hour > 23 or minute > 59:
            raise ValueError("Orario non valido. Esempio: /set_next 10:00")
        candidate = datetime.combine(current_local.date(), time(hour=hour, minute=minute), tzinfo=zone)
        if candidate <= current_local:
            candidate += timedelta(days=1)
        return next_allowed_datetime(candidate.astimezone(UTC), get_timezone_name(store), get_posting_windows(store))

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Uso: /set_next 10:00 oppure /set_next 2026-05-22 10:00") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return next_allowed_datetime(parsed.astimezone(UTC), get_timezone_name(store), get_posting_windows(store))


def is_configured_channel(store: Store, message: Message) -> bool:
    configured = store.get_setting("channel_id")
    if not configured:
        return False

    configured = configured.strip()
    chat = message.chat
    if configured.lstrip("-").isdigit():
        return str(chat.id) == configured
    if configured.startswith("@") and chat.username:
        return configured[1:].lower() == chat.username.lower()
    return False


async def handle_channel_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.channel_post
    if message is None or not message.text:
        return

    command = message.text.strip().split(maxsplit=1)[0].split("@", 1)[0]
    if command == "/channel_id":
        await context.bot.send_message(
            chat_id=message.chat.id,
            text=f"ID canale: {message.chat.id}",
        )
        return

    if command == "/set_channel_here":
        store = get_store(context)
        store.set_setting("channel_id", str(message.chat.id))
        await context.bot.send_message(
            chat_id=message.chat.id,
            text=f"Canale impostato per la pubblicazione automatica. ID: {message.chat.id}",
        )


async def ensure_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message
    if user is None:
        return False

    store = get_store(context)
    admin_ids = store.get_admin_ids()
    if not admin_ids:
        store.add_admin_id(user.id)
        if message:
            await message.reply_text(
                "Primo avvio completato: ti ho registrato come amministratore del bot."
            )
        return True

    if user.id not in admin_ids:
        if message:
            await message.reply_text("Non sei autorizzato a usare questo bot.")
        return False
    return True


def build_help_text() -> str:
    return (
        "Mandami foto o video in chat privata: li metto in coda e li pubblico sul canale.\n\n"
        "Comandi:\n"
        "/whoami - mostra il tuo ID Telegram\n"
        "/web_url - mostra l'URL pubblico per shortcut/API, se rilevabile\n"
        "/status - mostra configurazione e coda\n"
        "/queue - mostra i prossimi elementi in coda\n"
        "/set_channel @canale - imposta il canale di destinazione\n"
        "/set_channel_here - da scrivere nel canale privato per impostarlo\n"
        "/channel_id - da scrivere nel canale privato per vedere il suo ID\n"
        "/dashboard - pannello con bottoni\n"
        "/set_interval 2h - imposta ogni quanto pubblicare\n"
        "/set_next 10:00 - imposta manualmente il prossimo orario\n"
        "/set_timezone Europe/Rome - imposta il fuso orario\n"
        "/set_posting_hours 10:00-23:30 - limita le pubblicazioni\n"
        "/set_batch auto - batch automatico: 2 sopra 20, 3 sopra 40\n"
        "/set_batch 3 - batch fisso da 3 post singoli\n"
        "/set_queue_order random - pesca casuale dalla coda\n"
        "/set_queue_order chronological - usa ordine di arrivo\n"
        "/set_ratio 1 1 - imposta bilanciamento foto video\n"
        "/set_auto_backup 24h - backup automatico via Telegram\n"
        "/set_publish_backup telegram - backup dopo ogni post\n"
        "/post_now 3 - pubblica subito uno o piu contenuti\n"
        "/post_all CONFIRM - pubblica tutta la coda per svuotarla\n"
        f"Alert coda: ti avviso se non copre le prossime {QUEUE_ALERT_HOURS} ore\n"
        "/backup - ricevi un backup zip di coda e impostazioni\n"
        "/restore - istruzioni per ripristinare un backup zip\n"
        "/restore_state CONFIRM - ripristina rispondendo a un backup zip\n"
        "/pause - pausa la pubblicazione automatica\n"
        "/resume - riattiva la pubblicazione automatica\n"
        "/remove ID - rimuove un elemento dalla coda\n"
        "/mark_published - in risposta a un media, lo segna come gia pubblicato"
    )


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    await update.effective_message.reply_text(f"Il tuo ID Telegram e: {user.id}")


def configured_public_base_urls(store: Store) -> list[tuple[str, str]]:
    urls: list[tuple[str, str]] = []
    seen: set[str] = set()

    env_url = normalize_public_base_url(os.getenv("QUEUE_PUBLIC_BASE_URL", ""))
    if env_url:
        urls.append(("QUEUE_PUBLIC_BASE_URL", env_url))
        seen.add(env_url)

    for key in PUBLIC_URL_ENV_KEYS:
        env_url = normalize_public_base_url(os.getenv(key, ""))
        if env_url and env_url not in seen:
            urls.append((key, env_url))
            seen.add(env_url)

    remembered_url = normalize_public_base_url(store.get_setting(PUBLIC_URL_SETTING, "") or "")
    if remembered_url and remembered_url not in seen:
        urls.append(("rilevato dagli header HTTP", remembered_url))

    return urls


async def web_url_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    store = get_store(context)
    urls = configured_public_base_urls(store)
    if not urls:
        await update.effective_message.reply_text(
            "Non riesco ancora a vedere l'URL pubblico.\n\n"
            "Prova cosi:\n"
            "1. Apri da browser l'URL Tranger che trovi nel pannello, aggiungendo /healthz.\n"
            "2. Se vedi ok, torna qui e manda di nuovo /web_url.\n\n"
            "Se Tranger permette env custom, puoi anche aggiungere:\n"
            "QUEUE_PUBLIC_BASE_URL=https://tuo-dominio"
        )
        return

    lines = ["URL pubblico rilevato:"]
    for source, url in urls:
        lines.extend(
            [
                f"- {url}",
                f"  sorgente: {source}",
                f"  health: {url}/healthz",
                f"  foto: {url}/api/queue/photo",
                f"  video: {url}/api/queue/video",
            ]
        )
    await update.effective_message.reply_text("\n".join(lines))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    await update.effective_message.reply_text(build_help_text())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    await update.effective_message.reply_text(build_help_text())


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    store = get_store(context)
    queued = store.queued_counts_by_type()
    recent = store.recent_published_counts_by_type(store.get_int_setting("balance_window", 20))
    paused = store.get_bool_setting("paused")
    channel = store.get_setting("channel_id", "non impostato")
    interval = store.get_int_setting("interval_minutes", 60)
    batch_description = describe_batch_setting(store, queued)
    coverage = get_queue_coverage(store, queued)
    next_publish_at = get_or_initialize_next_publish_at(store)
    photo_ratio = store.get_int_setting("photo_ratio", 1)
    video_ratio = store.get_int_setting("video_ratio", 1)
    failed = store.failed_count()
    queue_order = store.get_setting("queue_order", "random") or "random"
    timezone_name = get_timezone_name(store)
    posting_windows = get_posting_windows(store)
    auto_backup = store.get_bool_setting("auto_backup_enabled")
    publish_backup = "spento"
    if store.get_bool_setting("backup_after_publish_enabled", True):
        publish_backup = "telegram" if store.get_bool_setting("backup_after_publish_send_telegram") else "locale"

    await update.effective_message.reply_text(
        "\n".join(
            [
                f"Canale: {channel}",
                f"Stato: {'in pausa' if paused else 'attivo'}",
                f"Timezone: {timezone_name}",
                f"Intervallo: {format_duration(interval)}",
                f"Fasce orarie: {format_posting_windows(posting_windows)}",
                f"Prossimo post: {format_next_publish_status(next_publish_at, timezone_name=timezone_name)}",
                f"Post per ciclo: {batch_description}",
                f"Ordine coda: {queue_order}",
                f"Auto-backup: {'attivo' if auto_backup else 'spento'}",
                f"Backup dopo post: {publish_backup}",
                f"Copertura {QUEUE_ALERT_HOURS}h: {format_coverage_status(coverage)}",
                f"Rapporto foto/video: {photo_ratio}:{video_ratio}",
                f"Coda: {queued[PHOTO]} foto, {queued[VIDEO]} video",
                f"Post recenti tracciati: {recent[PHOTO]} foto, {recent[VIDEO]} video",
                f"Falliti: {failed}",
            ]
        )
    )


def build_dashboard_text(store: Store) -> str:
    queued = store.queued_counts_by_type()
    coverage = get_queue_coverage(store, queued)
    next_publish_at = get_or_initialize_next_publish_at(store)
    timezone_name = get_timezone_name(store)
    paused = store.get_bool_setting("paused")
    publish_backup = "spento"
    if store.get_bool_setting("backup_after_publish_enabled", True):
        publish_backup = "telegram" if store.get_bool_setting("backup_after_publish_send_telegram") else "locale"
    return "\n".join(
        [
            "Queue Bot Dashboard",
            "",
            f"Stato: {'in pausa' if paused else 'attivo'}",
            f"Coda: {queued[PHOTO]} foto, {queued[VIDEO]} video",
            f"Prossimo: {format_next_publish_status(next_publish_at, timezone_name=timezone_name)}",
            f"Intervallo: {format_duration(store.get_int_setting('interval_minutes', 60))}",
            f"Fasce: {format_posting_windows(get_posting_windows(store))}",
            f"Timezone: {timezone_name}",
            f"Batch: {describe_batch_setting(store, queued)}",
            f"Ordine: {store.get_setting('queue_order', 'random') or 'random'}",
            f"Ratio: {store.get_int_setting('photo_ratio', 1)}:{store.get_int_setting('video_ratio', 1)}",
            f"Copertura 24h: {format_coverage_status(coverage)}",
            f"Auto-backup: {'attivo' if store.get_bool_setting('auto_backup_enabled') else 'spento'}",
            f"Backup dopo post: {publish_backup}",
        ]
    )


def dashboard_keyboard(store: Store, view: str = "main") -> InlineKeyboardMarkup:
    paused = store.get_bool_setting("paused")
    if view == "settings":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Intervallo 30m", callback_data="dash:int:30"),
                    InlineKeyboardButton("1h", callback_data="dash:int:60"),
                    InlineKeyboardButton("2h", callback_data="dash:int:120"),
                ],
                [
                    InlineKeyboardButton("Batch auto", callback_data="dash:batch:auto"),
                    InlineKeyboardButton("Batch 1", callback_data="dash:batch:1"),
                    InlineKeyboardButton("Batch 2", callback_data="dash:batch:2"),
                    InlineKeyboardButton("Batch 3", callback_data="dash:batch:3"),
                ],
                [
                    InlineKeyboardButton("Ordine random", callback_data="dash:order:random"),
                    InlineKeyboardButton("Cronologico", callback_data="dash:order:chronological"),
                ],
                [
                    InlineKeyboardButton("Ratio 1:1", callback_data="dash:ratio:1:1"),
                    InlineKeyboardButton("2:1", callback_data="dash:ratio:2:1"),
                    InlineKeyboardButton("1:2", callback_data="dash:ratio:1:2"),
                ],
                [InlineKeyboardButton("Indietro", callback_data="dash:main")],
            ]
        )
    if view == "schedule":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Prossimo 09:00", callback_data="dash:next:09:00"),
                    InlineKeyboardButton("10:00", callback_data="dash:next:10:00"),
                    InlineKeyboardButton("12:00", callback_data="dash:next:12:00"),
                ],
                [
                    InlineKeyboardButton("18:00", callback_data="dash:next:18:00"),
                    InlineKeyboardButton("20:00", callback_data="dash:next:20:00"),
                    InlineKeyboardButton("22:00", callback_data="dash:next:22:00"),
                ],
                [
                    InlineKeyboardButton("Tutto il giorno", callback_data="dash:hours:all"),
                    InlineKeyboardButton("10-23:30", callback_data="dash:hours:10:00-23:30"),
                ],
                [
                    InlineKeyboardButton("09-13,15-23", callback_data="dash:hours:09:00-13:00,15:00-23:00"),
                    InlineKeyboardButton("Europe/Rome", callback_data="dash:tz:Europe/Rome"),
                    InlineKeyboardButton("UTC", callback_data="dash:tz:UTC"),
                ],
                [InlineKeyboardButton("Indietro", callback_data="dash:main")],
            ]
        )
    if view == "backup":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Backup ora", callback_data="dash:backupnow"),
                    InlineKeyboardButton("Auto 24h", callback_data="dash:autobackup:24h"),
                    InlineKeyboardButton("Auto 12h", callback_data="dash:autobackup:12h"),
                ],
                [
                    InlineKeyboardButton("Auto off", callback_data="dash:autobackup:off"),
                    InlineKeyboardButton("Indietro", callback_data="dash:main"),
                ],
                [
                    InlineKeyboardButton("Dopo post: locale", callback_data="dash:publishbackup:local"),
                    InlineKeyboardButton("Dopo post: Telegram", callback_data="dash:publishbackup:telegram"),
                ],
                [InlineKeyboardButton("Dopo post: off", callback_data="dash:publishbackup:off")],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Pausa" if not paused else "Riprendi", callback_data="dash:pause" if not paused else "dash:resume"),
                InlineKeyboardButton("Posta 1 ora", callback_data="dash:post1"),
                InlineKeyboardButton("Aggiorna", callback_data="dash:main"),
            ],
            [
                InlineKeyboardButton("Impostazioni", callback_data="dash:view:settings"),
                InlineKeyboardButton("Orari", callback_data="dash:view:schedule"),
                InlineKeyboardButton("Backup", callback_data="dash:view:backup"),
            ],
            [
                InlineKeyboardButton("Coda", callback_data="dash:queue"),
                InlineKeyboardButton("Status", callback_data="dash:status"),
            ],
        ]
    )


async def send_or_edit_dashboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    view: str = "main",
    note: str | None = None,
) -> None:
    store = get_store(context)
    text = build_dashboard_text(store)
    if note:
        text = f"{note}\n\n{text}"
    markup = dashboard_keyboard(store, view)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text=text, reply_markup=markup)
        except TelegramError as exc:
            if "Message is not modified" not in str(exc):
                raise
    else:
        await update.effective_message.reply_text(text=text, reply_markup=markup)


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    await send_or_edit_dashboard(update, context)


async def dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()
    if not await ensure_admin(update, context):
        return

    store = get_store(context)
    data = query.data
    note: str | None = None
    view = "main"

    if data == "dash:main":
        pass
    elif data == "dash:pause":
        store.set_setting("paused", "true")
        note = "Pubblicazione automatica in pausa."
    elif data == "dash:resume":
        store.set_setting("paused", "false")
        note = "Pubblicazione automatica riattivata."
        await check_queue_coverage_alert(context.application, notify=True)
    elif data == "dash:post1":
        outcomes = await publish_many(context.application, count=1, manual=True)
        note = format_publish_outcomes(outcomes)
        if any(outcome.status == "published" for outcome in outcomes):
            try:
                await create_backup_after_publish(context.application)
            except Exception:
                LOGGER.exception("Post-publish backup failed")
        await check_queue_coverage_alert(context.application, notify=True)
    elif data == "dash:backupnow":
        await send_state_backup(context.application, query.message.chat_id, prefix="Backup")
        note = "Backup inviato."
        view = "backup"
    elif data == "dash:queue":
        await queue_command(update, context)
        return
    elif data == "dash:status":
        await status_command(update, context)
        return
    elif data.startswith("dash:view:"):
        view = data.rsplit(":", 1)[-1]
    elif data.startswith("dash:int:"):
        minutes = int(data.rsplit(":", 1)[-1])
        store.set_setting("interval_minutes", str(minutes))
        next_publish_at = set_next_publish_after_interval(store)
        schedule_publisher(context.application, store)
        note = f"Intervallo impostato: {format_duration(minutes)}. Prossimo: {format_next_publish_status(next_publish_at, timezone_name=get_timezone_name(store))}."
        view = "settings"
    elif data.startswith("dash:batch:"):
        value = data.rsplit(":", 1)[-1]
        if value == "auto":
            store.set_setting("batch_mode", "auto")
            note = "Batch automatico attivo."
        else:
            store.set_setting("batch_mode", "fixed")
            store.set_setting("posts_per_run", value)
            note = f"Batch fisso da {value}."
        view = "settings"
    elif data.startswith("dash:order:"):
        value = parse_queue_order(data.rsplit(":", 1)[-1])
        store.set_setting("queue_order", value)
        note = f"Ordine coda: {value}."
        view = "settings"
    elif data.startswith("dash:ratio:"):
        _, _, photo_ratio, video_ratio = data.split(":")
        store.set_setting("photo_ratio", photo_ratio)
        store.set_setting("video_ratio", video_ratio)
        note = f"Ratio foto/video: {photo_ratio}:{video_ratio}."
        view = "settings"
    elif data.startswith("dash:next:"):
        raw = data.removeprefix("dash:next:")
        next_publish_at = parse_local_next_publish(raw, store)
        store.set_setting("next_publish_at", datetime_to_setting(next_publish_at))
        schedule_publisher(context.application, store)
        note = f"Prossimo post: {format_next_publish_status(next_publish_at, timezone_name=get_timezone_name(store))}."
        view = "schedule"
    elif data.startswith("dash:hours:"):
        raw = data.removeprefix("dash:hours:")
        parse_posting_windows(raw)
        store.set_setting("posting_windows", raw)
        next_publish_at = next_allowed_datetime(get_or_initialize_next_publish_at(store), get_timezone_name(store), raw)
        store.set_setting("next_publish_at", datetime_to_setting(next_publish_at))
        schedule_publisher(context.application, store)
        note = f"Fasce orarie: {format_posting_windows(raw)}."
        view = "schedule"
    elif data.startswith("dash:tz:"):
        timezone_name = validate_timezone(data.removeprefix("dash:tz:"))
        store.set_setting("timezone", timezone_name)
        schedule_publisher(context.application, store)
        note = f"Timezone: {timezone_name}."
        view = "schedule"
    elif data.startswith("dash:autobackup:"):
        raw = data.rsplit(":", 1)[-1]
        if raw == "off":
            store.set_setting("auto_backup_enabled", "false")
            note = "Auto-backup spento."
        else:
            interval = parse_duration_minutes(raw)
            store.set_setting("auto_backup_enabled", "true")
            store.set_setting("auto_backup_interval_minutes", str(interval))
            store.set_setting("next_backup_at", datetime_to_setting(utcnow() + timedelta(minutes=interval)))
            note = f"Auto-backup ogni {format_duration(interval)}."
        schedule_auto_backup(context.application, store)
        view = "backup"
    elif data.startswith("dash:publishbackup:"):
        raw = data.rsplit(":", 1)[-1]
        if raw == "off":
            store.set_setting("backup_after_publish_enabled", "false")
            store.set_setting("backup_after_publish_send_telegram", "false")
            note = "Backup dopo pubblicazione spento."
        elif raw == "telegram":
            store.set_setting("backup_after_publish_enabled", "true")
            store.set_setting("backup_after_publish_send_telegram", "true")
            note = "Backup dopo pubblicazione: locale + invio Telegram."
        else:
            store.set_setting("backup_after_publish_enabled", "true")
            store.set_setting("backup_after_publish_send_telegram", "false")
            note = "Backup dopo pubblicazione: file locale rolling."
        view = "backup"

    await send_or_edit_dashboard(update, context, view=view, note=note)


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    store = get_store(context)
    items = store.list_queued(limit=10)
    queued = store.queued_counts_by_type()
    if not items:
        await update.effective_message.reply_text("La coda e vuota.")
        return

    lines = [f"In coda: {queued[PHOTO]} foto, {queued[VIDEO]} video", ""]
    for item in items:
        caption = " con caption" if item.caption_html else ""
        lines.append(f"#{item.id} - {item.media_type}{caption} - aggiunto {item.added_at}")
    await update.effective_message.reply_text("\n".join(lines))


async def set_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text("Uso: /set_channel @nome_canale oppure /set_channel -100...")
        return

    channel_id = context.args[0].strip()
    store = get_store(context)
    try:
        chat = await context.bot.get_chat(channel_id)
    except TelegramError as exc:
        await update.effective_message.reply_text(
            "Non riesco a leggere quel canale. Verifica che il bot sia amministratore "
            f"e che l'identificativo sia corretto.\nErrore: {exc}"
        )
        return

    store.set_setting("channel_id", channel_id)
    await update.effective_message.reply_text(
        f"Canale impostato: {chat.title or channel_id}. "
        "Ora posso pubblicare li se ho i permessi da amministratore."
    )


async def set_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text("Uso: /set_interval 30m oppure /set_interval 2h")
        return

    try:
        minutes = parse_duration_minutes(context.args[0])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store = get_store(context)
    store.set_setting("interval_minutes", str(minutes))
    next_publish_at = set_next_publish_after_interval(store)
    schedule_publisher(context.application, store)
    await update.effective_message.reply_text(
        f"Intervallo aggiornato: {format_duration(minutes)}. "
        f"Prossimo post: {format_next_publish_status(next_publish_at, timezone_name=get_timezone_name(store))}."
    )
    await check_queue_coverage_alert(context.application, notify=True)


async def set_next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text("Uso: /set_next 10:00 oppure /set_next 2026-05-22 10:00")
        return

    store = get_store(context)
    try:
        next_publish_at = parse_local_next_publish(" ".join(context.args), store)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store.set_setting("next_publish_at", datetime_to_setting(next_publish_at))
    schedule_publisher(context.application, store)
    await update.effective_message.reply_text(
        f"Prossimo post impostato: {format_next_publish_status(next_publish_at, timezone_name=get_timezone_name(store))}."
    )


async def set_timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    if not context.args:
        current = get_timezone_name(get_store(context))
        await update.effective_message.reply_text(f"Uso: /set_timezone Europe/Rome\nAdesso: {current}")
        return

    try:
        timezone_name = validate_timezone(context.args[0])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store = get_store(context)
    store.set_setting("timezone", timezone_name)
    next_publish_at = get_or_initialize_next_publish_at(store)
    schedule_publisher(context.application, store)
    await update.effective_message.reply_text(
        f"Timezone aggiornata: {timezone_name}.\n"
        f"Prossimo post: {format_next_publish_status(next_publish_at, timezone_name=timezone_name)}."
    )


async def set_posting_hours_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    if not context.args:
        current = format_posting_windows(get_posting_windows(get_store(context)))
        await update.effective_message.reply_text(
            "Uso: /set_posting_hours 10:00-23:30 oppure /set_posting_hours all\n"
            "Puoi usare piu finestre: /set_posting_hours 09:00-13:00,15:00-23:00\n"
            f"Adesso: {current}"
        )
        return

    raw = " ".join(context.args).strip()
    try:
        parse_posting_windows(raw)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store = get_store(context)
    store.set_setting("posting_windows", raw)
    next_publish_at = next_allowed_datetime(
        get_or_initialize_next_publish_at(store),
        get_timezone_name(store),
        raw,
    )
    store.set_setting("next_publish_at", datetime_to_setting(next_publish_at))
    schedule_publisher(context.application, store)
    await update.effective_message.reply_text(
        f"Fasce orarie aggiornate: {format_posting_windows(raw)}.\n"
        f"Prossimo post: {format_next_publish_status(next_publish_at, timezone_name=get_timezone_name(store))}."
    )


async def set_ratio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    try:
        photo_ratio, video_ratio = parse_ratio(context.args)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store = get_store(context)
    store.set_setting("photo_ratio", str(photo_ratio))
    store.set_setting("video_ratio", str(video_ratio))
    await update.effective_message.reply_text(f"Rapporto aggiornato: {photo_ratio}:{video_ratio}.")


async def set_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text("Uso: /set_batch auto oppure /set_batch 3")
        return

    store = get_store(context)
    mode = context.args[0].strip().lower()
    if mode in {"auto", "automatico"}:
        store.set_setting("batch_mode", "auto")
        queued = store.queued_counts_by_type()
        current = resolve_posts_per_run(store, queued)
        await update.effective_message.reply_text(
            "Modalita batch automatica attiva: 1 fino a 20 elementi in coda, "
            f"2 sopra 20, 3 sopra 40. Adesso pubblicherei {current} post per ciclo."
        )
        await check_queue_coverage_alert(context.application, notify=True)
        return

    try:
        count = parse_post_count(context.args[0])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store.set_setting("batch_mode", "fixed")
    store.set_setting("posts_per_run", str(count))
    await update.effective_message.reply_text(
        f"Ok: batch fisso da {count} contenuti singoli a ogni intervallo."
    )
    await check_queue_coverage_alert(context.application, notify=True)


async def set_queue_order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    if not context.args:
        current = get_store(context).get_setting("queue_order", "random") or "random"
        await update.effective_message.reply_text(
            "Uso: /set_queue_order random oppure /set_queue_order chronological\n"
            f"Adesso: {current}"
        )
        return

    try:
        queue_order = parse_queue_order(context.args[0])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    get_store(context).set_setting("queue_order", queue_order)
    if queue_order == "random":
        await update.effective_message.reply_text(
            "Ok: pubblichero rispettando il ratio foto/video, ma pescando casualmente dentro la coda."
        )
    else:
        await update.effective_message.reply_text(
            "Ok: pubblichero rispettando il ratio foto/video e scegliendo i media in ordine di arrivo."
        )


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    get_store(context).set_setting("paused", "true")
    await update.effective_message.reply_text("Pubblicazione automatica in pausa.")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    get_store(context).set_setting("paused", "false")
    await update.effective_message.reply_text("Pubblicazione automatica riattivata.")
    await check_queue_coverage_alert(context.application, notify=True)


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Uso: /remove ID")
        return
    try:
        media_item_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("ID non valido.")
        return

    removed = get_store(context).mark_removed(media_item_id)
    await update.effective_message.reply_text(
        "Elemento rimosso dalla coda." if removed else "Non trovo quell'ID in coda."
    )


async def post_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    try:
        count = parse_post_count(context.args[0]) if context.args else 1
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    outcomes = await publish_many(context.application, count=count, manual=True)
    if any(outcome.status == "published" for outcome in outcomes):
        try:
            await create_backup_after_publish(context.application)
        except Exception:
            LOGGER.exception("Post-publish backup failed")
    await update.effective_message.reply_text(format_publish_outcomes(outcomes))
    await check_queue_coverage_alert(context.application, notify=True)


async def post_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    store = get_store(context)
    queued = store.queued_counts_by_type()
    total = sum(queued.values())
    if total == 0:
        await update.effective_message.reply_text("La coda e gia vuota.")
        return

    if not context.args or context.args[0] != "CONFIRM":
        await update.effective_message.reply_text(
            "Comando di emergenza: pubblica tutta la coda sul canale, come post singoli, "
            "e quindi la svuota.\n\n"
            f"Adesso pubblicherebbe {total} media ({queued[PHOTO]} foto, {queued[VIDEO]} video).\n\n"
            "Per confermare usa:\n"
            "/post_all CONFIRM"
        )
        return

    await update.effective_message.reply_text(
        f"Ok, pubblico tutta la coda: {total} media. Potrebbe volerci qualche minuto."
    )
    outcomes = await publish_many(context.application, count=total, manual=True)
    if any(outcome.status == "published" for outcome in outcomes):
        try:
            await create_backup_after_publish(context.application)
        except Exception:
            LOGGER.exception("Post-publish backup failed")
    await update.effective_message.reply_text(format_publish_summary(outcomes, requested=total))
    await check_queue_coverage_alert(context.application, notify=True)


async def mark_published_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    replied = update.effective_message.reply_to_message
    if not replied:
        await update.effective_message.reply_text("Rispondi a una foto o a un video con /mark_published.")
        return

    media = extract_media(replied)
    if not media:
        await update.effective_message.reply_text("Il messaggio a cui hai risposto non contiene foto o video.")
        return

    store = get_store(context)
    is_new = store.mark_published(media.file_unique_id, media.media_type, source="manual_mark")
    await update.effective_message.reply_text(
        "Segnato come gia pubblicato."
        if is_new
        else "Era gia segnato come pubblicato."
    )


async def send_state_backup(application: Application, chat_id: int | str, prefix: str = "Backup") -> bool:
    store = get_store(application)
    try:
        archive_path = create_state_backup(store.path, Path("./state_backups"))
    except Exception as exc:
        LOGGER.exception("State backup failed")
        await application.bot.send_message(chat_id=chat_id, text=f"Backup fallito: {exc}")
        return False

    with archive_path.open("rb") as backup_file:
        await application.bot.send_document(
            chat_id=chat_id,
            document=backup_file,
            filename=archive_path.name,
            caption=(
                f"{prefix} stato Queue Bot. Conserva questo file: contiene coda, "
                "impostazioni, deduplica e prossimo orario di pubblicazione."
            ),
        )
    return True


async def create_backup_after_publish(application: Application) -> None:
    store = get_store(application)
    if not store.get_bool_setting("backup_after_publish_enabled", True):
        return

    raw_path = store.get_setting("backup_after_publish_path", "./state_backups/latest-state.zip")
    backup_path = create_rolling_state_backup(store.path, Path(raw_path or "./state_backups/latest-state.zip"))
    LOGGER.info("Rolling post-publish backup written to %s", backup_path)

    if not store.get_bool_setting("backup_after_publish_send_telegram"):
        return

    admin_ids = store.get_admin_ids()
    for admin_id in admin_ids:
        try:
            with backup_path.open("rb") as backup_file:
                await application.bot.send_document(
                    chat_id=admin_id,
                    document=backup_file,
                    filename=backup_path.name,
                    caption=(
                        "Backup automatico dopo pubblicazione. "
                        "Contiene coda, impostazioni, deduplica e prossimo orario."
                    ),
                )
        except TelegramError:
            LOGGER.exception("Failed to send post-publish backup to admin %s", admin_id)


async def backup_state_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    await update.effective_message.reply_text("Creo backup dello stato del bot...")
    await send_state_backup(context.application, update.effective_chat.id, prefix="Backup")


async def set_publish_backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    store = get_store(context)
    if not context.args:
        enabled = store.get_bool_setting("backup_after_publish_enabled", True)
        send_telegram = store.get_bool_setting("backup_after_publish_send_telegram")
        path = store.get_setting("backup_after_publish_path", "./state_backups/latest-state.zip")
        mode = "spento"
        if enabled:
            mode = "telegram" if send_telegram else "locale"
        await update.effective_message.reply_text(
            "Uso: /set_publish_backup local, /set_publish_backup telegram oppure /set_publish_backup off\n"
            f"Adesso: {mode}\n"
            f"File locale: {path}"
        )
        return

    value = context.args[0].strip().lower()
    if value in {"off", "false", "no", "0", "spento"}:
        store.set_setting("backup_after_publish_enabled", "false")
        store.set_setting("backup_after_publish_send_telegram", "false")
        await update.effective_message.reply_text("Backup dopo pubblicazione spento.")
        return

    if value in {"telegram", "tg", "send"}:
        store.set_setting("backup_after_publish_enabled", "true")
        store.set_setting("backup_after_publish_send_telegram", "true")
        await update.effective_message.reply_text(
            "Backup dopo pubblicazione attivo: creo il file rolling locale e lo invio agli admin su Telegram."
        )
        return

    if value in {"local", "locale", "on", "true", "yes", "1"}:
        store.set_setting("backup_after_publish_enabled", "true")
        store.set_setting("backup_after_publish_send_telegram", "false")
        await update.effective_message.reply_text(
            "Backup dopo pubblicazione attivo: sovrascrivo il file locale latest-state.zip."
        )
        return

    await update.effective_message.reply_text(
        "Uso: /set_publish_backup local, /set_publish_backup telegram oppure /set_publish_backup off"
    )


async def set_auto_backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    store = get_store(context)
    if not context.args:
        enabled = store.get_bool_setting("auto_backup_enabled")
        interval = store.get_int_setting("auto_backup_interval_minutes", 24 * 60)
        await update.effective_message.reply_text(
            "Uso: /set_auto_backup on, /set_auto_backup off oppure /set_auto_backup 24h\n"
            f"Adesso: {'attivo' if enabled else 'spento'}, intervallo {format_duration(interval)}"
        )
        return

    value = context.args[0].strip().lower()
    if value in {"off", "false", "no", "0", "spento"}:
        store.set_setting("auto_backup_enabled", "false")
        schedule_auto_backup(context.application, store)
        await update.effective_message.reply_text("Auto-backup spento.")
        return

    if value in {"on", "true", "yes", "1", "attivo"}:
        interval = store.get_int_setting("auto_backup_interval_minutes", 24 * 60)
    else:
        try:
            interval = parse_duration_minutes(value)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return

    store.set_setting("auto_backup_enabled", "true")
    store.set_setting("auto_backup_interval_minutes", str(interval))
    store.set_setting("next_backup_at", datetime_to_setting(utcnow() + timedelta(minutes=interval)))
    schedule_auto_backup(context.application, store)
    await update.effective_message.reply_text(f"Auto-backup attivo ogni {format_duration(interval)}.")


async def restore_state_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    if not context.args or context.args[0] != "CONFIRM":
        await update.effective_message.reply_text(
            "Per ripristinare lo stato, rispondi al file backup .zip con:\n"
            "/restore_state CONFIRM\n\n"
            "Shortcut mentale: manda /restore per rivedere queste istruzioni.\n"
            "Attenzione: sostituisce coda e impostazioni attuali."
        )
        return

    replied = update.effective_message.reply_to_message
    if not replied or not replied.document:
        await update.effective_message.reply_text("Rispondi a un file backup .zip con /restore_state CONFIRM.")
        return

    if not replied.document.file_name or not replied.document.file_name.endswith(".zip"):
        await update.effective_message.reply_text("Il file di backup deve essere uno .zip.")
        return

    temp_dir = Path("./state_backups/incoming")
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / replied.document.file_name

    try:
        telegram_file = await replied.document.get_file()
        await telegram_file.download_to_drive(custom_path=temp_path)
        store = get_store(context)
        result = restore_state_backup(temp_path, store.path)
        await update.effective_message.reply_text(
            "Stato ripristinato. Ora riavvio automaticamente il bot."
        )
        if result.safety_copy_path:
            LOGGER.info("State restored; previous database copied to %s", result.safety_copy_path)
        asyncio.create_task(shutdown_after_restore())
    except Exception as exc:
        LOGGER.exception("State restore failed")
        await update.effective_message.reply_text(f"Restore fallito: {exc}")
    finally:
        if temp_path.exists():
            temp_path.unlink()


async def shutdown_after_restore() -> None:
    await asyncio.sleep(2)
    LOGGER.info("Exiting after state restore so the host can restart the bot.")
    os._exit(0)


async def restore_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    await update.effective_message.reply_text(
        "Restore facile:\n"
        "1. Mandami il file backup .zip.\n"
        "2. Rispondi a quel file con /restore_state CONFIRM.\n"
        "3. Dopo il restore mi riavvio da solo.\n\n"
        "Per creare un backup usa /backup."
    )


def format_add_result(result: AddMediaResult) -> str:
    if result.status == "queued" and result.media_item:
        return f"Aggiunto in coda come #{result.media_item.id} ({result.media_item.media_type})."
    if result.status == "already_published":
        return "Questo media risulta gia pubblicato sul canale, quindi non lo metto in coda."
    if result.status == "duplicate":
        if result.existing_status == "queued" and result.media_item:
            return f"Questo media e gia in coda come #{result.media_item.id}."
        if result.existing_status == "published":
            return "Questo media e gia stato pubblicato."
        return f"Questo media e gia presente con stato: {result.existing_status}."
    return "Media ricevuto, ma non sono riuscito a inserirlo in coda."


async def handle_media_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    store = get_store(context)
    media = extract_media(message)
    if media is None:
        return

    if update.channel_post:
        if is_configured_channel(store, message):
            store.mark_published(
                media.file_unique_id,
                media.media_type,
                source="channel",
                channel_message_id=message.message_id,
            )
            LOGGER.info("Tracked channel media %s from message %s", media.file_unique_id, message.message_id)
        return

    if not await ensure_admin(update, context):
        return

    if message.caption and message.caption.strip().split(maxsplit=1)[0] == "/mark_published":
        is_new = store.mark_published(media.file_unique_id, media.media_type, source="manual_mark")
        await message.reply_text("Segnato come gia pubblicato." if is_new else "Era gia segnato.")
        return

    result = store.add_media(
        media.media_type,
        media.file_id,
        media.file_unique_id,
        media.caption_html,
        update.effective_user.id if update.effective_user else None,
    )
    await message.reply_text(format_add_result(result))
    await check_queue_coverage_alert(context.application, notify=False)


async def publish_next(application: Application, manual: bool = False) -> PublishOutcome:
    store = get_store(application)

    if store.get_bool_setting("paused") and not manual:
        return PublishOutcome(status="paused", message="Pubblicazione automatica in pausa.")

    channel_id = store.get_setting("channel_id")
    if not channel_id:
        return PublishOutcome(status="no_channel", message="Canale non impostato. Usa /set_channel.")

    queued = store.queued_counts_by_type()
    recent = store.recent_published_counts_by_type(store.get_int_setting("balance_window", 20))
    media_type = choose_media_type(
        queued,
        recent,
        store.get_int_setting("photo_ratio", 1),
        store.get_int_setting("video_ratio", 1),
        store.get_setting("last_published_type", "") or None,
    )
    if media_type is None:
        return PublishOutcome(status="empty", message="La coda e vuota.")

    queue_order = store.get_setting("queue_order", "random") or "random"
    item = store.get_queued_item(media_type, order=queue_order) or store.get_queued_item(order=queue_order)
    if item is None:
        return PublishOutcome(status="empty", message="La coda e vuota.")

    try:
        if item.media_type == PHOTO:
            sent_message = await application.bot.send_photo(
                chat_id=channel_id,
                photo=item.file_id,
                caption=item.caption_html,
                parse_mode=ParseMode.HTML if item.caption_html else None,
            )
        else:
            sent_message = await application.bot.send_video(
                chat_id=channel_id,
                video=item.file_id,
                caption=item.caption_html,
                parse_mode=ParseMode.HTML if item.caption_html else None,
            )
    except TelegramError as exc:
        store.mark_failed(item.id, str(exc))
        LOGGER.exception("Failed to publish media item %s", item.id)
        return PublishOutcome(
            status="failed",
            media_item=item,
            message=f"Pubblicazione fallita per #{item.id}: {exc}",
        )

    store.mark_published(
        item.file_unique_id,
        item.media_type,
        source="bot",
        channel_message_id=sent_message.message_id,
        media_item_id=item.id,
    )
    store.set_setting("last_published_type", item.media_type)
    return PublishOutcome(
        status="published",
        media_item=item,
        message=f"Pubblicato #{item.id} ({item.media_type}) sul canale.",
    )


async def publish_many(application: Application, count: int, manual: bool = False) -> list[PublishOutcome]:
    outcomes: list[PublishOutcome] = []
    for index in range(count):
        outcome = await publish_next(application, manual=manual)
        outcomes.append(outcome)
        if outcome.status != "published":
            break
        if index < count - 1:
            await asyncio.sleep(1)
    return outcomes


def format_publish_outcomes(outcomes: list[PublishOutcome]) -> str:
    if not outcomes:
        return "Nessuna pubblicazione eseguita."
    if len(outcomes) == 1:
        return outcomes[0].message

    published = [outcome for outcome in outcomes if outcome.status == "published"]
    lines = [f"Pubblicati {len(published)} contenuti singoli."]
    if published:
        lines.extend(
            f"- #{outcome.media_item.id} ({outcome.media_item.media_type})"
            for outcome in published
            if outcome.media_item
        )

    last = outcomes[-1]
    if last.status != "published":
        lines.append(f"Poi mi sono fermato: {last.message}")
    return "\n".join(lines)


def format_publish_summary(outcomes: list[PublishOutcome], requested: int) -> str:
    published = [outcome for outcome in outcomes if outcome.status == "published"]
    last = outcomes[-1] if outcomes else None
    lines = [
        f"Pubblicati {len(published)} contenuti su {requested} richiesti.",
    ]
    if last and last.status != "published":
        lines.append(f"Mi sono fermato: {last.message}")
    elif len(published) == requested:
        lines.append("Coda svuotata.")
    return "\n".join(lines)


def format_queue_alert(coverage: QueueCoverage, queued_counts: dict[str, int]) -> str:
    return "\n".join(
        [
            f"Alert coda: il materiale non copre piu le prossime {QUEUE_ALERT_HOURS} ore.",
            f"In coda: {coverage.queued_total} media ({queued_counts[PHOTO]} foto, {queued_counts[VIDEO]} video).",
            f"Servono circa {coverage.required_posts} media con le impostazioni attuali.",
            f"Mancano almeno {coverage.missing_posts} media.",
            f"Copertura stimata: {format_duration(coverage.covered_minutes)}.",
        ]
    )


async def check_queue_coverage_alert(application: Application, notify: bool = True) -> None:
    store = get_store(application)
    queued = store.queued_counts_by_type()
    coverage = get_queue_coverage(store, queued)
    alert_active = store.get_bool_setting("queue_alert_active")

    if coverage.covers:
        if alert_active:
            store.set_setting("queue_alert_active", "false")
        return

    if store.get_bool_setting("paused"):
        return

    if not notify or alert_active:
        return

    admin_ids = store.get_admin_ids()
    if not admin_ids:
        return

    message = format_queue_alert(coverage, queued)
    sent = False
    for admin_id in admin_ids:
        try:
            await application.bot.send_message(chat_id=admin_id, text=message)
            sent = True
        except TelegramError:
            LOGGER.exception("Failed to send queue coverage alert to admin %s", admin_id)

    if sent:
        store.set_setting("queue_alert_active", "true")


async def publisher_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    store = get_store(context)
    advance_after_run = True
    outcomes: list[PublishOutcome] = []
    try:
        now = utcnow()
        if not is_within_posting_windows(now, get_timezone_name(store), get_posting_windows(store)):
            next_publish_at = next_allowed_datetime(now, get_timezone_name(store), get_posting_windows(store))
            store.set_setting("next_publish_at", datetime_to_setting(next_publish_at))
            LOGGER.info("Outside posting window; next publish scheduled at %s", datetime_to_setting(next_publish_at))
            advance_after_run = False
            return

        queued = store.queued_counts_by_type()
        outcomes = await publish_many(
            context.application,
            count=resolve_posts_per_run(store, queued),
            manual=False,
        )
        if outcomes and outcomes[0].status not in {"empty", "paused"}:
            LOGGER.info(format_publish_outcomes(outcomes))
        await check_queue_coverage_alert(context.application, notify=True)
    finally:
        if advance_after_run:
            next_publish_at = set_next_publish_after_interval(store)
            LOGGER.info("Next publish scheduled at %s", datetime_to_setting(next_publish_at))
        if any(outcome.status == "published" for outcome in outcomes):
            try:
                await create_backup_after_publish(context.application)
            except Exception:
                LOGGER.exception("Post-publish backup failed")
        schedule_publisher(context.application, store)


def schedule_publisher(application: Application, store: Store) -> None:
    if application.job_queue is None:
        raise RuntimeError(
            "JobQueue non disponibile. Installa le dipendenze con: "
            'pip install "python-telegram-bot[job-queue]"'
        )

    for job in application.job_queue.get_jobs_by_name(PUBLISH_JOB_NAME):
        job.schedule_removal()

    now = utcnow()
    next_publish_at = get_or_initialize_next_publish_at(store, now=now)
    if next_publish_at <= now and not is_within_posting_windows(now, get_timezone_name(store), get_posting_windows(store)):
        next_publish_at = next_allowed_datetime(now, get_timezone_name(store), get_posting_windows(store))
    else:
        next_publish_at = next_allowed_datetime(next_publish_at, get_timezone_name(store), get_posting_windows(store))
    if next_publish_at != parse_datetime_setting(store.get_setting("next_publish_at")):
        store.set_setting("next_publish_at", datetime_to_setting(next_publish_at))
    first_delay_seconds = seconds_until_next_publish(next_publish_at, now)
    application.job_queue.run_once(
        publisher_job,
        when=timedelta(seconds=first_delay_seconds),
        name=PUBLISH_JOB_NAME,
    )
    LOGGER.info(
        "Publisher scheduled: run in %s seconds, interval %s minutes, next_publish_at=%s",
        first_delay_seconds,
        store.get_int_setting("interval_minutes", 60),
        datetime_to_setting(next_publish_at),
    )


async def auto_backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    store = get_store(context)
    try:
        if not store.get_bool_setting("auto_backup_enabled"):
            return

        admin_ids = store.get_admin_ids()
        for admin_id in admin_ids:
            try:
                await send_state_backup(context.application, admin_id, prefix="Auto-backup")
            except TelegramError:
                LOGGER.exception("Auto-backup failed for admin %s", admin_id)
    finally:
        if store.get_bool_setting("auto_backup_enabled"):
            interval = store.get_int_setting("auto_backup_interval_minutes", 24 * 60)
            store.set_setting("next_backup_at", datetime_to_setting(utcnow() + timedelta(minutes=interval)))
        schedule_auto_backup(context.application, store)


def schedule_auto_backup(application: Application, store: Store) -> None:
    if application.job_queue is None:
        return

    for job in application.job_queue.get_jobs_by_name(BACKUP_JOB_NAME):
        job.schedule_removal()

    if not store.get_bool_setting("auto_backup_enabled"):
        return

    now = utcnow()
    interval = store.get_int_setting("auto_backup_interval_minutes", 24 * 60)
    next_backup_at = parse_datetime_setting(store.get_setting("next_backup_at"))
    if next_backup_at is None or next_backup_at <= now:
        next_backup_at = now + timedelta(minutes=interval)
        store.set_setting("next_backup_at", datetime_to_setting(next_backup_at))

    delay = seconds_until_next_publish(next_backup_at, now)
    application.job_queue.run_once(
        auto_backup_job,
        when=timedelta(seconds=delay),
        name=BACKUP_JOB_NAME,
    )
    LOGGER.info("Auto-backup scheduled in %s seconds", delay)


def build_application(config: AppConfig) -> Application:
    auto_restore_state(config)
    store = Store(config.database_path)
    store.initialize()
    store.bootstrap(config)

    application = ApplicationBuilder().token(config.bot_token).build()
    application.bot_data["store"] = store

    application.add_handler(CallbackQueryHandler(dashboard_callback, pattern=r"^dash:"))
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler(["web_url", "url"], web_url_command))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("dashboard", dashboard_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CommandHandler("set_channel", set_channel_command))
    application.add_handler(CommandHandler("set_interval", set_interval_command))
    application.add_handler(CommandHandler("set_next", set_next_command))
    application.add_handler(CommandHandler("set_timezone", set_timezone_command))
    application.add_handler(CommandHandler("set_posting_hours", set_posting_hours_command))
    application.add_handler(CommandHandler("set_batch", set_batch_command))
    application.add_handler(CommandHandler(["set_queue_order", "set_order"], set_queue_order_command))
    application.add_handler(CommandHandler("set_ratio", set_ratio_command))
    application.add_handler(CommandHandler("set_auto_backup", set_auto_backup_command))
    application.add_handler(CommandHandler("set_publish_backup", set_publish_backup_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("post_now", post_now_command))
    application.add_handler(CommandHandler("post_all", post_all_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("mark_published", mark_published_command))
    application.add_handler(CommandHandler(["backup_state", "backup"], backup_state_command))
    application.add_handler(CommandHandler("restore_state", restore_state_command))
    application.add_handler(CommandHandler("restore", restore_help_command))
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & filters.TEXT, handle_channel_text))
    application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_media_message))

    schedule_publisher(application, store)
    schedule_auto_backup(application, store)
    return application


def auto_restore_state(config: AppConfig) -> None:
    if not config.backup_auto_restore_enabled:
        return

    try:
        result = restore_latest_backup_if_needed(
            config.database_path,
            Path(config.default_backup_after_publish_path),
            restore_if_empty=config.backup_auto_restore_if_empty,
        )
    except Exception:
        LOGGER.exception("Auto-restore from latest backup failed")
        return

    if result:
        LOGGER.warning(
            "Auto-restored Queue Bot state from %s into %s (reason=%s, safety_copy=%s)",
            result.backup_path,
            result.database_path,
            result.reason,
            result.safety_copy_path,
        )


def backup_before_shutdown(application: Application, config: AppConfig) -> None:
    if not config.backup_before_shutdown_enabled:
        return

    try:
        store = application.bot_data.get("store")
        if not store:
            return
        backup_path = create_rolling_state_backup(
            store.path,
            Path(store.get_setting("backup_after_publish_path", config.default_backup_after_publish_path)),
        )
        LOGGER.info("Shutdown backup written to %s", backup_path)
    except Exception:
        LOGGER.exception("Shutdown backup failed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    config = AppConfig.from_env()
    if not config.bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN non impostato. Copia .env.example in .env e compila i valori.")

    application = build_application(config)
    start_http_server_from_env(application.bot_data["store"], config.bot_token)
    LOGGER.info("Bot avviato.")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        backup_before_shutdown(application, config)
