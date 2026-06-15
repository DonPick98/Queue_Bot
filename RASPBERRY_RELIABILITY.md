# Raspberry Reliability Guide

Questa guida serve a far girare Queue Bot sul Raspberry in modo piu autonomo possibile.

L'obiettivo e avere tre livelli di protezione:

1. `systemd` riavvia il bot se il processo Python crasha.
2. Un watchdog ogni 5 minuti controlla bot, health endpoint, rete, database e prova recovery.
3. Se il watchdog fallisce piu volte di fila, riavvia il Raspberry.

## Installazione

Sul Raspberry, dalla cartella del repo:

```bash
cd /home/pi/Queue_Bot
git pull
chmod +x scripts/raspberry/install_queue_bot_service.sh
./scripts/raspberry/install_queue_bot_service.sh
```

Lo script crea:

- `queue-bot.service`
- `queue-bot-watchdog.service`
- `queue-bot-watchdog.timer`

Il servizio espone anche `http://127.0.0.1:8080/healthz`, usato dal watchdog.

## Comandi utili

Stato del bot:

```bash
systemctl status queue-bot.service
```

Log del bot:

```bash
journalctl -u queue-bot.service -n 150 --no-pager
```

Stato del watchdog:

```bash
systemctl status queue-bot-watchdog.timer
systemctl status queue-bot-watchdog.service
```

Log del watchdog:

```bash
tail -n 150 /home/pi/Queue_Bot/logs/raspberry-watchdog.log
```

Test manuale health:

```bash
curl http://127.0.0.1:8080/healthz
```

Riavvio manuale del bot:

```bash
sudo systemctl restart queue-bot.service
```

## Cosa controlla il watchdog

- `queue-bot.service` attivo
- endpoint locale `/healthz`
- accesso a `https://api.telegram.org`
- presenza e integrita rapida del database SQLite, se `sqlite3` e installato

Se qualcosa fallisce:

- riavvia il servizio bot
- prova a riavviare `NetworkManager`, `dhcpcd` o `systemd-networkd`
- dopo 3 fallimenti consecutivi riavvia il Raspberry
- prova ad avvisare il primo admin via Telegram se rete e token sono disponibili

## Recovery dopo riavvio

Queue Bot e gia configurato per:

- fare backup rolling dopo ogni pubblicazione
- ripristinare automaticamente il database se manca, e vuoto o non valido
- riprendere la schedule se il Raspberry e stato spento o offline

## Raccomandazioni hardware

Per un servizio pagato, il software da solo non basta. Consigli pratici:

- alimentatore ufficiale Raspberry o comunque stabile
- microSD di qualita, meglio A2/endurance
- backup rolling locale attivo
- se possibile Ethernet invece di Wi-Fi
- mini UPS o power bank UPS per evitare corruzioni da blackout

## Se il Raspberry non torna raggiungibile

Se anche il watchdog non basta e il Raspberry sparisce dalla rete:

1. spegnilo
2. metti la SD nel PC
3. controlla la partizione boot
4. cerca `berry-repair.log` o i log del watchdog se accessibili

In quel caso il problema e spesso sotto il livello del bot: alimentazione, SD, filesystem root o rete di sistema.
