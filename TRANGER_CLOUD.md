# Deploy su Tranger Cloud

Il repository GitHub e pronto per un import diretto:

```text
https://github.com/DonPick98/Queue_Bot
```

Branch:

```text
main
```

## Metodo consigliato

Se Tranger Cloud offre import GitHub:

1. Crea una nuova app/progetto.
2. Scegli GitHub come source.
3. Seleziona:

```text
DonPick98/Queue_Bot
```

4. Usa il `Dockerfile` nella root del repo.
5. Lascia come comando di avvio:

```bash
python bot.py
```

Se Tranger rileva automaticamente il Dockerfile, non dovresti dover impostare altro per la build.

## Variabili ambiente

Imposta almeno:

```env
TELEGRAM_BOT_TOKEN=<botfather-token>
ADMIN_USER_IDS=<tuo-user-id-telegram>
TELEGRAM_CHANNEL_ID=-1003886424027
DATABASE_PATH=/app/data/bot.sqlite3
DEFAULT_POST_INTERVAL_MINUTES=60
DEFAULT_BATCH_MODE=auto
DEFAULT_POSTS_PER_RUN=1
DEFAULT_QUEUE_ORDER=random
DEFAULT_PHOTO_RATIO=1
DEFAULT_VIDEO_RATIO=1
BALANCE_WINDOW=20
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
PORT=8080
```

Per upload veloci via API privata:

```env
QUEUE_API_TOKEN=<queue-api-token>
QUEUE_API_STAGING_CHAT_ID=<tuo-user-id-telegram>
QUEUE_API_DELETE_STAGING=true
QUEUE_API_MAX_MB=50
```

`PORT=8080` espone:

```text
/healthz
/
```

Telegram continua a funzionare in polling, quindi non serve webhook.

## Persistenza dello stato

Il codice sta su GitHub, ma la coda e le impostazioni stanno nel database:

```text
/app/data/bot.sqlite3
```

Quindi su Tranger Cloud devi verificare una di queste opzioni:

- volume/storage persistente montato su `/app/data`;
- file storage persistente tra restart/redeploy;
- upload manuale del database dopo il deploy.

Se `/app/data` non e persistente, il bot funziona ma perde coda, impostazioni e deduplica quando il container viene ricreato.

## Migrare lo stato attuale

Sul PC locale:

```text
backup_state.bat
```

Poi carica il backup o il database sul nuovo host. Il file importante e:

```text
data/bot.sqlite3
```

Sul nuovo host deve finire in:

```text
/app/data/bot.sqlite3
```

## Prima di avviare online

Spegni il bot locale o disattiva l'autostart:

```text
disable_autostart.bat
```

Due istanze in polling con lo stesso token Telegram possono contendersi gli update.

## Se GitHub import non compare

Alternative:

- usa deploy da Dockerfile caricando lo zip generato con `build_justrunmy_zip.bat`;
- collega il repo tramite URL GitHub;
- se il repo e privato, autorizza l'app GitHub di Tranger Cloud o usa un deploy key/token.
