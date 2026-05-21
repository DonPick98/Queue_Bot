# GitHub e stato del bot

Il repository GitHub deve contenere il codice, non la memoria viva del bot.

## Cosa NON va su GitHub

- `.env`: contiene token e configurazioni private.
- `data/bot.sqlite3`: contiene coda, impostazioni, media gia pubblicati e caption.
- `dist/`: pacchetti generati.
- `state_backups/`: backup dello stato.

Questi path sono gia in `.gitignore`.

## Stato portabile

Il bot salva il suo stato in:

```text
data/bot.sqlite3
```

Quel file contiene:

- coda media;
- impostazioni;
- deduplica;
- prossimo orario di pubblicazione;
- alert coda;
- log dei media pubblicati.

Per spostare il bot su un altro PC, Raspberry o servizio cloud devi portare con te questo stato.

## Backup

Su Windows:

```text
backup_state.bat
```

Oppure:

```powershell
.\.venv\Scripts\python.exe scripts\backup_state.py
```

Il backup viene creato in:

```text
state_backups\
```

Il backup usa la API SQLite `backup`, quindi crea una copia coerente anche se il bot e acceso. Per massima prudenza, prima di migrare su un nuovo host ferma comunque il bot locale.

## Restore

Ferma il bot, poi:

```text
restore_state.bat state_backups\mouth-queue-state-YYYYMMDD-HHMMSS.zip
```

Oppure:

```powershell
.\.venv\Scripts\python.exe scripts\restore_state.py state_backups\mouth-queue-state-YYYYMMDD-HHMMSS.zip
```

Il restore crea prima una copia di sicurezza del database esistente.

## Regola operativa importante

Usa una sola istanza attiva del bot per volta con lo stesso token Telegram.

Prima di spostarti su JustRunMy.App, Raspberry o un altro PC:

1. ferma il bot vecchio;
2. crea backup dello stato;
3. ripristina lo stato sul nuovo host;
4. avvia il bot nuovo.

In futuro, se vuoi stato realmente condiviso tra host senza backup manuale, la soluzione migliore e migrare da SQLite a PostgreSQL remoto, ad esempio Neon o Supabase.
