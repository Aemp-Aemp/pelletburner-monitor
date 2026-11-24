#!/bin/sh
# Wi-Fi setup hotspot (new, not tested on a real Pi yet). Run once on the Pi:  sudo sh pi/install-wifi-setup.sh
# pi/install.sh and the easy install run it by themselves. Safe to run again.
# When the Pi has no network for 3 minutes, it opens the open Wi-Fi "pelletpi-setup"; join it with a phone and
# open http://10.42.0.1:8099 to pick your home Wi-Fi. Off after success or 15 minutes. Needs NetworkManager
# (Raspberry Pi OS has it); installs nothing. Remove: sudo systemctl disable --now pelletpi-wifi-setup
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
if ! command -v nmcli >/dev/null 2>&1; then
  echo "Wi-Fi setup hotspot skipped: no NetworkManager (nmcli) on this system"
  exit 0
fi
mkdir -p /usr/local/lib/pelletpi
sed 's/\r$//' "$HERE/pelletpi_wifi_setup.py" > /usr/local/lib/pelletpi/pelletpi_wifi_setup.py

# Wi-Fi power save off for every Wi-Fi connection, also new ones (a known cause of drops on the Pi's Wi-Fi chip).
# install-wifi-watchdog.sh writes the same file and also fixes the connections that exist already.
mkdir -p /etc/NetworkManager/conf.d
printf '# Pellet burner monitor: Wi-Fi power save off (2 = disable).\n[connection]\nwifi.powersave = 2\n' \
  > /etc/NetworkManager/conf.d/pelletpi-wifi.conf
nmcli general reload conf >/dev/null 2>&1 || true

cat > /etc/systemd/system/pelletpi-wifi-setup.service <<'EOF'
[Unit]
Description=Pellet burner monitor: Wi-Fi setup hotspot while the Pi has no network
After=NetworkManager.service
Wants=NetworkManager.service
[Service]
ExecStart=/usr/bin/python3 /usr/local/lib/pelletpi/pelletpi_wifi_setup.py
Restart=on-failure
RestartSec=60
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable pelletpi-wifi-setup.service
systemctl restart pelletpi-wifi-setup.service || true
echo "Wi-Fi setup hotspot installed (pelletpi-setup, only while the Pi has no network)."
