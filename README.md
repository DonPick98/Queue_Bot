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
DEFAULT_POST_INTERVAL_MINUTES=60
DEFAULT_BATCH_MODE=fixed
DEFAULT_POSTS_PER_RUN=1
DEFAULT_PHOTO_RATIO=1
DEFAULT_VIDEO_RATIO=1
BALANCE_WINDOW=20
DATABASE_PATH=./data/bot.sqlite3
```

Per trovare il tuo ID utente puoi scrivere a bot come `@userinfobot`, oppure avviare questo bot senza `ADMIN_USER_IDS`: il primo utente che invia `/start` diventa amministratore. Per sicurezza, e meglio impostare `ADMIN_USER_IDS` subito.

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
- `/status`: configurazione, coda e conteggi.
- `/queue`: primi elementi in coda.
- `/set_channel @canale`: imposta il canale.
- `/set_interval 30m`: pubblica ogni 30 minuti.
- `/set_interval 2h`: pubblica ogni 2 ore.
- `/set_batch auto`: pubblica 1 media fino a 20 in coda, 2 sopra 20, 3 sopra 40.
- `/set_batch 3`: pubblica 3 media a ogni intervallo, come 3 post singoli separati.
- `/set_ratio 1 1`: bilanciamento pari tra foto e video.
- `/set_ratio 2 1`: circa due foto per ogni video.
- `/post_now`: pubblica subito il prossimo contenuto, anche se il bot e in pausa.
- `/post_now 3`: pubblica subito 3 contenuti, come post singoli separati.
- `/pause`: ferma la pubblicazione automatica.
- `/resume`: riattiva la pubblicazione automatica.
- `/remove ID`: rimuove un contenuto dalla coda.
- `/mark_published`: rispondi a una foto/video per segnarla come gia pubblicata.

## Alert coda

Il bot controlla automaticamente se i media in coda bastano per le prossime 24 ore, usando intervallo e batch correnti. Se la coda scende sotto quella soglia, invia un messaggio privato agli amministratori. L'alert non viene ripetuto finche la coda non torna a coprire 24 ore e poi scende di nuovo.

## Riavvii e downtime

Il bot salva nel database il prossimo orario previsto di pubblicazione. Se chiudi `start_bot.bat` o il PC resta spento, al riavvio succede questo:

- se il prossimo orario non e ancora arrivato, il bot aspetta il tempo residuo;
- se il prossimo orario e passato, il bot pubblica appena possibile e poi riparte da quel momento con l'intervallo configurato.

Il comando `/post_now` resta manuale e non sposta la schedule automatica.

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

Il bot conta foto e video negli ultimi `BALANCE_WINDOW` post tracciati. Se il rapporto e `1:1` e negli ultimi post ci sono troppe foto, il prossimo contenuto preferito sara un video, se disponibile. Se manca un tipo di media, pubblica comunque quello disponibile invece di bloccare la coda.

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
