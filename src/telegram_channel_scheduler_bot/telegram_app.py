from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import math
import os
from pathlib import Path
import re

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .balancer import PHOTO, VIDEO, choose_media_type
from .config import AppConfig
from .health import start_http_server_from_env
from .posting_plan import (
    QUEUE_ALERT_HOURS,
    QueueCoverage,
    auto_posts_per_run,
    estimate_queue_coverage,
    initial_next_publish_at,
    seconds_until_next_publish,
)
from .state_archive import create_state_backup, restore_state_backup
from .storage import AddMediaResult, MediaItem, Store


LOGGER = logging.getLogger(__name__)
PUBLISH_JOB_NAME = "publisher"
MAX_POSTS_PER_RUN = 20


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


def format_datetime_for_user(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


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
        return existing

    last_published_at = parse_datetime_setting(store.latest_published_at())
    next_publish_at = initial_next_publish_at(
        current_time,
        store.get_int_setting("interval_minutes", 60),
        last_published_at=last_published_at,
    )
    store.set_setting("next_publish_at", datetime_to_setting(next_publish_at))
    return next_publish_at


def set_next_publish_after_interval(store: Store, now: datetime | None = None) -> datetime:
    next_publish_at = (now or utcnow()) + timedelta(
        minutes=store.get_int_setting("interval_minutes", 60)
    )
    store.set_setting("next_publish_at", datetime_to_setting(next_publish_at))
    return next_publish_at


def format_next_publish_status(next_publish_at: datetime, now: datetime | None = None) -> str:
    current_time = now or utcnow()
    if next_publish_at <= current_time:
        return "appena possibile"
    minutes = max(1, math.ceil((next_publish_at - current_time).total_seconds() / 60))
    return f"{format_datetime_for_user(next_publish_at)} (tra {format_duration(minutes)})"


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
        "/status - mostra configurazione e coda\n"
        "/queue - mostra i prossimi elementi in coda\n"
        "/set_channel @canale - imposta il canale di destinazione\n"
        "/set_channel_here - da scrivere nel canale privato per impostarlo\n"
        "/channel_id - da scrivere nel canale privato per vedere il suo ID\n"
        "/set_interval 2h - imposta ogni quanto pubblicare\n"
        "/set_batch auto - batch automatico: 2 sopra 20, 3 sopra 40\n"
        "/set_batch 3 - batch fisso da 3 post singoli\n"
        "/set_ratio 1 1 - imposta bilanciamento foto video\n"
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

    await update.effective_message.reply_text(
        "\n".join(
            [
                f"Canale: {channel}",
                f"Stato: {'in pausa' if paused else 'attivo'}",
                f"Intervallo: {format_duration(interval)}",
                f"Prossimo post: {format_next_publish_status(next_publish_at)}",
                f"Post per ciclo: {batch_description}",
                f"Copertura {QUEUE_ALERT_HOURS}h: {format_coverage_status(coverage)}",
                f"Rapporto foto/video: {photo_ratio}:{video_ratio}",
                f"Coda: {queued[PHOTO]} foto, {queued[VIDEO]} video",
                f"Post recenti tracciati: {recent[PHOTO]} foto, {recent[VIDEO]} video",
                f"Falliti: {failed}",
            ]
        )
    )


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
        f"Prossimo post: {format_next_publish_status(next_publish_at)}."
    )
    await check_queue_coverage_alert(context.application, notify=True)


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


async def backup_state_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return

    store = get_store(context)
    await update.effective_message.reply_text("Creo backup dello stato del bot...")
    try:
        archive_path = create_state_backup(store.path, Path("./state_backups"))
    except Exception as exc:
        LOGGER.exception("State backup failed")
        await update.effective_message.reply_text(f"Backup fallito: {exc}")
        return

    with archive_path.open("rb") as backup_file:
        await update.effective_message.reply_document(
            document=backup_file,
            filename=archive_path.name,
            caption=(
                "Backup stato Queue Bot. Conserva questo file: contiene coda, "
                "impostazioni, deduplica e prossimo orario di pubblicazione."
            ),
        )


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

    item = store.get_oldest_queued(media_type) or store.get_oldest_queued()
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
    try:
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
        next_publish_at = set_next_publish_after_interval(store)
        LOGGER.info("Next publish scheduled at %s", datetime_to_setting(next_publish_at))


def schedule_publisher(application: Application, store: Store) -> None:
    if application.job_queue is None:
        raise RuntimeError(
            "JobQueue non disponibile. Installa le dipendenze con: "
            'pip install "python-telegram-bot[job-queue]"'
        )

    for job in application.job_queue.get_jobs_by_name(PUBLISH_JOB_NAME):
        job.schedule_removal()

    now = utcnow()
    interval_minutes = store.get_int_setting("interval_minutes", 60)
    next_publish_at = get_or_initialize_next_publish_at(store, now=now)
    first_delay_seconds = seconds_until_next_publish(next_publish_at, now)
    application.job_queue.run_repeating(
        publisher_job,
        interval=timedelta(minutes=interval_minutes),
        first=timedelta(seconds=first_delay_seconds),
        name=PUBLISH_JOB_NAME,
    )
    LOGGER.info(
        "Publisher scheduled: first run in %s seconds, interval %s minutes, next_publish_at=%s",
        first_delay_seconds,
        interval_minutes,
        datetime_to_setting(next_publish_at),
    )


def build_application(config: AppConfig) -> Application:
    store = Store(config.database_path)
    store.initialize()
    store.bootstrap(config)

    application = ApplicationBuilder().token(config.bot_token).build()
    application.bot_data["store"] = store

    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CommandHandler("set_channel", set_channel_command))
    application.add_handler(CommandHandler("set_interval", set_interval_command))
    application.add_handler(CommandHandler("set_batch", set_batch_command))
    application.add_handler(CommandHandler("set_ratio", set_ratio_command))
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
    return application


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
    application.run_polling(allowed_updates=Update.ALL_TYPES)
