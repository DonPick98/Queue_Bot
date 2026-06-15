#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SERVICE_USER="${QUEUE_BOT_SERVICE_USER:-pi}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ ! -f "$PROJECT_DIR/bot.py" ]; then
  echo "Project directory does not look like Queue Bot: $PROJECT_DIR" >&2
  exit 1
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "Missing $PROJECT_DIR/.env. Create it before installing the service." >&2
  exit 1
fi

cd "$PROJECT_DIR"
mkdir -p logs data state_backups

if [ ! -x ".venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

sudo tee /etc/systemd/system/queue-bot.service >/dev/null <<EOF
[Unit]
Description=Queue Bot Telegram publisher
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
Environment=HEALTH_HOST=127.0.0.1
Environment=HEALTH_PORT=8080
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/bot.py
Restart=always
RestartSec=15
TimeoutStopSec=75
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/queue-bot-watchdog.service >/dev/null <<EOF
[Unit]
Description=Queue Bot self-healing watchdog
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Environment=QUEUE_BOT_PROJECT_DIR=$PROJECT_DIR
Environment=QUEUE_BOT_SERVICE_NAME=queue-bot.service
ExecStart=$PROJECT_DIR/scripts/raspberry/queue-bot-watchdog.sh
EOF

sudo tee /etc/systemd/system/queue-bot-watchdog.timer >/dev/null <<EOF
[Unit]
Description=Run Queue Bot watchdog every 5 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
EOF

chmod +x "$PROJECT_DIR/scripts/raspberry/queue-bot-watchdog.sh"

sudo systemctl daemon-reload
sudo systemctl enable --now queue-bot.service
sudo systemctl enable --now queue-bot-watchdog.timer

echo "Installed Queue Bot service and watchdog."
echo
echo "Useful commands:"
echo "  systemctl status queue-bot.service"
echo "  systemctl status queue-bot-watchdog.timer"
echo "  journalctl -u queue-bot.service -n 100 --no-pager"
echo "  tail -n 100 $PROJECT_DIR/logs/raspberry-watchdog.log"
