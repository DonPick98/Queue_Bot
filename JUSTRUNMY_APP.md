# Deploy su JustRunMy.App

Questo progetto e pronto per JustRunMy.App con Docker oppure Zip/Git deploy.

## Opzione consigliata: Docker/Git

Usa il `Dockerfile` incluso. Il container avvia:

```bash
python bot.py
```

## Opzione semplice: Zip Upload

Da Windows puoi creare uno zip pulito con:

```text
build_justrunmy_zip.bat
```

Il file viene creato qui:

```text
dist\mouth-queue-justrunmy.zip
```

Lo zip non include `.env`, database locale, virtualenv, log o script Windows non necessari.

Il database viene salvato qui:

```text
/app/data/bot.sqlite3
```

Assicurati che `/app/data` rimanga persistente tra restart/redeploy. JustRunMy.App dichiara file/disk persistenti per i bot; se nel pannello c'e un'opzione volume/storage, montala su `/app/data`.

## Variabili ambiente

Nel pannello JustRunMy.App aggiungi le variabili in `.env.justrunmy.example`.

Minime:

```env
TELEGRAM_BOT_TOKEN=replace-with-botfather-token
ADMIN_USER_IDS=...
TELEGRAM_CHANNEL_ID=-100...
DATABASE_PATH=/app/data/bot.sqlite3
DEFAULT_BATCH_MODE=auto
DEFAULT_QUEUE_ORDER=random
DEFAULT_TIMEZONE=Europe/Rome
DEFAULT_POSTING_WINDOWS=all
AUTO_BACKUP_ENABLED=false
AUTO_BACKUP_INTERVAL_MINUTES=1440
BACKUP_AFTER_PUBLISH_ENABLED=true
BACKUP_AFTER_PUBLISH_SEND_TELEGRAM=false
BACKUP_AFTER_PUBLISH_PATH=/app/data/latest-state.zip
BACKUP_AUTO_RESTORE_ENABLED=true
BACKUP_AUTO_RESTORE_IF_EMPTY=true
BACKUP_BEFORE_SHUTDOWN_ENABLED=true
```

Non caricare il tuo `.env` locale: contiene il token.

## Prima di avviare online

Se usi lo stesso token Telegram, spegni il bot locale prima di avviare quello su JustRunMy.App. Due istanze in polling con lo stesso token possono contendersi gli update.

Su questo PC puoi disattivare l'avvio automatico con:

```text
disable_autostart.bat
```

Poi chiudi la finestra del bot locale o premi `CTRL+C`.

## Porta health opzionale

Se vuoi esporre una porta per health check:

```env
PORT=8080
```

Endpoint:

```text
/healthz
/
```

Risponde `ok`. Telegram continua comunque a funzionare in polling, quindi non serve webhook.

## Upload veloce via API privata

Se usi direttamente `api.telegram.org/bot.../sendPhoto`, Telegram lo considera un messaggio in uscita dal bot e non genera un update che il bot possa mettere in coda.

Per questo il progetto include endpoint privati che fanno la cosa giusta:

```text
POST /api/queue/photo
POST /api/queue/video
```

Imposta prima:

```env
QUEUE_API_TOKEN=<queue-api-token>
QUEUE_API_STAGING_CHAT_ID=il_tuo_user_id_telegram
QUEUE_API_DELETE_STAGING=false
QUEUE_API_MAX_MB=50
```

`QUEUE_API_STAGING_CHAT_ID` puo anche mancare: in quel caso usa il primo valore di `ADMIN_USER_IDS`. Il media viene mandato li solo per ottenere da Telegram `file_id` e `file_unique_id`, poi viene inserito direttamente in coda. Non viene postato sul canale.

Esempio foto:

```bash
curl -X POST "https://TUO-DOMINIO/api/queue/photo" \
  -H "Authorization: Bearer un-segreto-lungo-random" \
  -F "media=@foto.jpg" \
  -F "caption=Test caption" \
  -F "available_after_publish_count=0"
```

Esempio video:

```bash
curl -X POST "https://TUO-DOMINIO/api/queue/video" \
  -H "Authorization: Bearer un-segreto-lungo-random" \
  -F "media=@video.mp4" \
  -F "caption=Test caption"
```

Risposta tipica:

```json
{
  "ok": true,
  "queue_status": "queued",
  "media_id": 12,
  "media_type": "photo",
  "available_after_publish_count": 0
}
```

Se carichi lo stesso file due volte, `queue_status` diventa `duplicate` o `already_published`, quindi non sporca la coda.

## Migrare il database locale

Se vuoi portare online coda, impostazioni e deduplica gia presenti sul PC:

1. Ferma il bot locale.
2. Prendi il file:

```text
D:\CODEX\Telegram bot\data\bot.sqlite3
```

3. Caricalo su JustRunMy.App in:

```text
/app/data/bot.sqlite3
```

4. Avvia il container.

Se non migri il database, il bot parte pulito. In quel caso imposta almeno `ADMIN_USER_IDS` e `TELEGRAM_CHANNEL_ID` come variabili ambiente.

## Backup

Fai backup periodico di:

```text
/app/data/bot.sqlite3
```

Quel file contiene:

- coda media;
- impostazioni;
- media gia pubblicati;
- prossimo orario di pubblicazione;
- stato degli alert.

## Note

- Usa polling: non serve configurare webhook Telegram.
- Tieni le risorse basse: il bot dovrebbe stare nel free tier.
- Evita log enormi: JustRunMy.App ha un limite disco free ridotto.
- Prima del deploy cloud e consigliato rigenerare il token con `@BotFather`, dato che il token e stato condiviso in chat.
