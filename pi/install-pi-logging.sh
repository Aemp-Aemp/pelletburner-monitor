#!/bin/sh
# Logging for long unattended tests, also without internet. Run once on the Pi:
#   sudo sh pi/install-pi-logging.sh
# - pi-health-YYYY-MM-DD.jsonl in the data folder every minute (see pi_health.py)
# - the system journal is kept on the card across restarts (USB plug/unplug, Wi-Fi, power warnings)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
DATA=$(cd "$HERE/.." && pwd)/data
# The user that owns the data folder (default "pi"); the easy-install sets PI_USER to the card's own user.
PI_USER=${PI_USER:-pi}
install -m 755 "$HERE/pi_health.py" /usr/local/bin/pi_health.py

cat > /etc/systemd/system/pi-health.service <<EOF
[Unit]
Description=Pellet burner monitor: one line of Pi health
[Service]
Type=oneshot
User=$PI_USER
ExecStart=/usr/bin/python3 /usr/local/bin/pi_health.py $DATA
EOF

cat > /etc/systemd/system/pi-health.timer <<'EOF'
[Unit]
Description=Pellet burner monitor: Pi health every minute
[Timer]
OnBootSec=30
OnUnitActiveSec=60
AccuracySec=5
[Install]
WantedBy=timers.target
EOF

mkdir -p /etc/systemd/journald.conf.d /var/log/journal
cat > /etc/systemd/journald.conf.d/pelletburner.conf <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=200M
EOF

systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable --now pi-health.timer
echo "Pi logging installed: $DATA/pi-health-*.jsonl and a persistent journal (journalctl -b -1 = the start before)."
