# Queue Bot

Bot Telegram per ricevere foto e video in chat privata, metterli in coda e pubblicarli su un canale a intervalli regolari con bilanciamento foto/video.

## Cosa fa

- Riceve foto e video dagli amministratori.
- Salva tutto in un database SQLite locale.
- Pubblica automaticamente sul canale con intervallo configurabile.
- Evita duplicati usando `file_unique_id`, l'identificativo stabile dei file Telegram.
- Traccia anche i media pubblicati manualmente sul canale, se il bot riceve gli aggiornamenti del canale.
- Bilancia foto e video guardando gli ultimi post tracciati, non solo alternando alla cieca.
- Avvisa gli admin quando la coda non copre piu le prossime 24 ore di pubblicazione.
- Riprende correttamente dopo downtime locali: se un post era gia dovuto mentre il PC era spento, pubblica appena si riavvia.

Nota importante: il Bot API di Telegram non permette a un bot di leggere tutta la cronologia vecchia di un canale. Il bot evita i duplicati che conosce: media ricevuti da lui, media pubblicati da lui, media che vede nel canale dopo essere stato aggiunto come amministratore, e media marcati manualmente con `/mark_published`.

## Setup

1. Crea un bot con [@BotFather](https://t.me/BotFather) e copia il token.
2. Aggiungi il bot come amministratore del canale con permesso di pubblicare.
3. Copia `.env.example` in `.env` e compila:

```env
TELEGRAM_BOT_TOKEN=replace-with-botfather-token
ADMIN_USER_IDS=123456789
TELEGRAM_CHANNEL_ID=@nome_del_tuo_canale
DEFAULT_POST_INTERVAL_MINUTES=120
DEFAULT_SCHEDULE_MODE=anchored
DEFAULT_BATCH_MODE=fixed
DEFAULT_POSTS_PER_RUN=1
DEFAULT_QUEUE_ORDER=random
DEFAULT_PHOTO_RATIO=1
DEFAULT_VIDEO_RATIO=1
BALANCE_WINDOW=20
DEFAULT_TIMEZONE=Europe/Rome
DEFAULT_AUDIBLE_POSTS_PER_DAY=3
DEFAULT_POSTING_WINDOWS=all
AUTO_BACKUP_ENABLED=false
AUTO_BACKUP_INTERVAL_MINUTES=1440
BACKUP_AFTER_PUBLISH_ENABLED=true
BACKUP_AFTER_PUBLISH_SEND_TELEGRAM=false
BACKUP_AFTER_PUBLISH_PATH=./state_backups/latest-state.zip
BACKUP_SQLITE_VACUUM_ENABLED=true
BACKUP_AUTO_RESTORE_ENABLED=true
BACKUP_AUTO_RESTORE_IF_EMPTY=true
BACKUP_TELEGRAM_AUTO_DOWNLOAD_ENABLED=true
BACKUP_BEFORE_SHUTDOWN_ENABLED=true
DATABASE_PATH=./data/bot.sqlite3
```

Per trovare il tuo ID utente puoi scrivere a bot come `@userinfobot`, oppure avviare questo bot senza `ADMIN_USER_IDS`: il primo utente che invia `/start` diventa amministratore. Per sicurezza, e meglio impostare `ADMIN_USER_IDS` subito.

`DEFAULT_AUDIBLE_POSTS_PER_DAY` controlla le notifiche dei soli post automatici nel canale contenuti. Accetta valori da `0` a `3`; con il valore predefinito e 12 post al giorno, i post 1, 5 e 9 hanno la notifica normale e gli altri vengono inviati silenziosamente. Foto e video restano sempre post Telegram singoli.

## Avvio

Su Windows puoi fare doppio click su:

```text
start_bot.bat
```

La finestra rimane aperta anche in caso di errore, cosi puoi leggere il messaggio.

Oppure da PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

## Avvio automatico su Windows

Per far partire il bot quando accedi a Windows con una finestra terminale visibile:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup_shortcut.ps1
```

Questo crea un collegamento nella cartella Startup di Windows verso `start_bot.bat`.

Per rimuovere l'avvio automatico visibile:

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall_startup_shortcut.ps1
```

Scorciatoie comode:

```text
enable_autostart.bat
disable_autostart.bat
autostart_status.bat
```

In alternativa, se un giorno vuoi avviarlo in background con Task Scheduler:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup_task.ps1
```

Il task usa `run_bot_scheduled.ps1` e scrive i log in:

```text
logs\bot.log
```

Per rimuovere l'avvio automatico:

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall_startup_task.ps1
```

## Comandi

- `/start` o `/help`: guida rapida.
- `/dashboard`: pannello con bottoni per gestire il bot senza ricordare i comandi.
- `/web_url`: mostra l'URL pubblico per Apple Shortcuts/API, se il bot riesce a rilevarlo.
- `/status`: configurazione, coda e conteggi.
- `/queue`: primi elementi in coda.
- `/set_channel @canale`: imposta il canale.
- `/set_interval 30m`: pubblica ogni 30 minuti.
- `/set_interval 2h`: pubblica ogni 2 ore.
- `/set_next 10:00`: imposta manualmente il prossimo orario di pubblicazione nel timezone configurato.
- `/set_timezone Europe/Rome`: imposta il fuso orario usato da status, dashboard e orari manuali.
- `/set_posting_hours 10:00-23:30`: pubblica solo dentro quella fascia.
- `/set_posting_hours all`: rimuove limiti di fascia oraria.
- `/set_batch auto`: pubblica 1 media fino a 20 in coda, 2 sopra 20, 3 sopra 40.
- `/set_batch 3`: pubblica 3 media a ogni intervallo, come 3 post singoli separati.
- `/set_queue_order random`: default, rispetta il ratio foto/video ma pesca casualmente dalla coda.
- `/set_queue_order chronological`: rispetta il ratio foto/video e usa l'ordine di arrivo.
- `/set_ratio 1 1`: bilanciamento pari tra foto e video.
- `/set_ratio 2 1`: circa due foto per ogni video.
- `/post_now`: pubblica subito il prossimo contenuto, anche se il bot e in pausa.
- `/post_now 3`: pubblica subito 3 contenuti, come post singoli separati.
- `/post_all CONFIRM`: comando di emergenza, pubblica tutta la coda come post singoli e la svuota.
- `/x`: ricevi in chat 3 foto gia pubblicate, pronte da scaricare e riusare su X.
- `/set_auto_backup 24h`: invia automaticamente un backup zip agli admin ogni 24 ore.
- `/set_auto_backup off`: spegne il backup automatico.
- `/set_publish_backup local`: dopo ogni post sovrascrive un backup rolling locale.
- `/set_publish_backup telegram`: dopo ogni post crea il backup e lo invia agli admin.
- `/set_publish_backup off`: spegne il backup dopo pubblicazione.
- `/pause`: ferma la pubblicazione automatica.
- `/resume`: riattiva la pubblicazione automatica.
- `/remove ID`: rimuove un contenuto dalla coda.
- `/mark_published`: rispondi a una foto/video per segnarla come gia pubblicata.

## Alert coda

Il bot controlla automaticamente se i media in coda bastano per le prossime 24 ore, usando intervallo e batch correnti. Se la coda scende sotto quella soglia, invia un messaggio privato agli amministratori. L'alert non viene ripetuto finche la coda non torna a coprire 24 ore e poi scende di nuovo.

## Riavvii e downtime

Il bot salva nel database il prossimo orario previsto di pubblicazione. Se chiudi `start_bot.bat` o il PC resta spento, al riavvio succede questo:

- con `DEFAULT_SCHEDULE_MODE=anchored`, pubblica sugli slot fissi calcolati da mezzanotte nel timezone configurato;
- con intervallo 120 minuti gli slot sono 00:00, 02:00, 04:00, ...;
- se riparte alle 17:20, il prossimo slot e 18:00;
- se una pubblicazione programmata per 00:00 riesce alle 00:01, lo slot successivo resta 02:00;
- con `DEFAULT_SCHEDULE_MODE=interval`, mantiene il vecchio comportamento basato sull'intervallo dall'ultimo post.

Il comando `/post_now` resta manuale e non sposta la schedule automatica.

## Raspberry self-healing

Per Raspberry e installazioni sempre attive usa il kit `systemd` in:

```text
scripts/raspberry/
RASPBERRY_RELIABILITY.md
```

Installa un servizio che riavvia automaticamente il bot se crasha e un watchdog che controlla ogni 5 minuti health endpoint, rete, database e servizio. Se la recovery fallisce piu volte, il Raspberry viene riavviato automaticamente.

Sul Raspberry:

```bash
cd /home/pi/Queue_Bot
git pull
chmod +x scripts/raspberry/install_queue_bot_service.sh
./scripts/raspberry/install_queue_bot_service.sh
```

## Backup self-healing

Il bot puo proteggersi da restart e deploy:

- dopo ogni pubblicazione crea o sovrascrive `latest-state.zip`;
- quando invia `latest-state.zip` su Telegram, salva anche il `file_id` del documento;
- all'avvio scarica da Telegram l'ultimo backup noto prima di pianificare pubblicazioni;
- prima dello shutdown crea un altro backup rolling;
- all'avvio, se il database manca, e vuoto, non sembra valido, o il backup e piu recente del DB, prova a ripristinare automaticamente da `latest-state.zip`.

Limite Telegram: il Bot API non permette al bot di cercare liberamente nella cronologia vecchia della chat. Il restore automatico da Telegram funziona quando il bot ha salvato il `file_id` dell'ultimo backup inviato.

Su host senza storage persistente conviene anche inviare il backup su Telegram:

```text
/set_publish_backup telegram
```

## Mouth Preview

Mouth Preview e un flusso separato dal canale Premium:

- pubblica solo immagini, mai video;
- massimo 2 immagini al giorno, come post Telegram individuali;
- usa foto gia pubblicate in Premium da almeno 48 ore;
- evita di ripetere la stessa sorgente nello stesso giorno;
- invia la prima foto con notifica normale e la seconda silenziosa;
- pubblica le foto senza caption e applica un watermark semitrasparente in basso a sinistra;
- mantiene silenziosi welcome, upgrade card e recap settimanale;
- crea una upgrade card ogni 6 anteprime e un mosaic settimanale con conteggi reali.

Configurazione predefinita:

```env
PREVIEW_CHANNEL_ID=
PREVIEW_DELAY_HOURS=48
PREVIEW_POSTS_PER_DAY=2
PREVIEW_POSTING_TIMES=10:00,20:00
PREVIEW_MEMBERPASS_URL=https://my.memberpass.net/306354e7c4
PREVIEW_MEMBERPASS_LINK_VERSION=v1
PREVIEW_WATERMARK_ENABLED=true
PREVIEW_WATERMARK_TEXT=@MouthPreview
PREVIEW_WATERMARK_OPACITY=82
PREVIEW_RECAP_WEEKDAY=6
PREVIEW_RECAP_TIME=21:00
```

Lascia `PREVIEW_CHANNEL_ID` vuoto fino a quando il bot non e amministratore del canale.
Concedi i permessi per pubblicare e fissare messaggi, poi invia al bot in privato:

```text
/set_preview_channel @MouthPreview
```
## Deploy cloud

Il progetto include anche supporto per deploy container su JustRunMy.App:

```text
Dockerfile
.dockerignore
.env.justrunmy.example
JUSTRUNMY_APP.md
build_justrunmy_zip.bat
```

Leggi `JUSTRUNMY_APP.md` per variabili ambiente, path del database persistente e backup.

Per Tranger Cloud:

```text
TRANGER_CLOUD.md
```

Include anche una API privata opzionale per upload veloci:

```text
POST /api/queue/photo
POST /api/queue/video
```

Da proteggere con `QUEUE_API_TOKEN`.
Il form accetta anche `priority` e `available_after_publish_count`: il secondo lascia il media in coda ma lo rende pubblicabile solo dopo quel numero totale di pubblicazioni nel `publish_log`.

Per inviare rapidamente foto e video da iPhone con Apple Shortcuts o Scriptable:

```text
APPLE_SHORTCUTS.md
apple_shortcuts/QueueToBot.scriptable.js
```

## GitHub e stato portabile

Il codice puo stare su GitHub, ma lo stato del bot resta fuori dal repository. Per migrare coda e impostazioni tra PC, Raspberry e servizi cloud usa:

```text
backup_state.bat
restore_state.bat
```

Oppure direttamente da Telegram:

```text
/backup
/restore
/restore_state CONFIRM
```

Dettagli in `STATE_AND_GITHUB.md`.

## Come lavora il bilanciamento

Il bot guarda la sequenza finale dei post tracciati e rispetta il rapporto foto/video come ritmo corrente, senza recuperare vecchi "debiti" creati quando un tipo di media mancava dalla coda. Per esempio, con `/set_ratio 2 1`, dopo un video pubblica due foto, se disponibili, prima di scegliere un altro video. Se manca un tipo di media, pubblica comunque quello disponibile invece di bloccare la coda.

## Deduplica

Telegram assegna a foto e video un `file_unique_id`. Il bot lo usa per:

- non mettere in coda due volte lo stesso media;
- non ripubblicare un media gia pubblicato dal bot;
- non ripubblicare un media visto nel canale mentre il bot e amministratore;
- segnare manualmente vecchi media tramite `/mark_published`.

## Test locali

I test non chiamano Telegram:

```powershell
$env:PYTHONPATH="src"
python -m unittest discover -s tests
```
