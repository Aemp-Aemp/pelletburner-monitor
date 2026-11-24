#!/bin/sh
# Wi-Fi watchdog (new, not tested on a real Pi yet). Run once on the Pi:  sudo sh pi/install-wifi-watchdog.sh
# pi/install.sh, the easy install and the auto-update (install-auto-update.sh) run it by themselves. Safe to run again.
# - every minute: when the Pi has had no network for 3 minutes it reconnects the known Wi-Fi, then turns the
#   radio off/on, then reloads the Wi-Fi driver; after 10 minutes the setup hotspot "pelletpi-setup" opens
#   (see pelletpi_wifi_watchdog.py). Events: data/wifi-watchdog.jsonl. Never touches the boiler's USB link.
# - known Wi-Fi connections: NetworkManager tries them forever (autoconnect-retries 0), Wi-Fi power save off
# - also installs the Wi-Fi setup hotspot when it is missing (a Pi installed before it existed)
# Off: WIFI_WATCHDOG=no in pelletpi/settings.txt on the card's boot drive, or in /etc/default/pelletpi-wifi-watchdog.
# Remove: sudo systemctl disable --now pelletpi-wifi-watchdog.timer
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
APP=$(cd "$HERE/.." && pwd)
if ! command -v nmcli >/dev/null 2>&1; then
  echo "Wi-Fi watchdog skipped: no NetworkManager (nmcli) on this system"
  exit 0
fi
mkdir -p /usr/local/lib/pelletpi "$APP/data"

# the setup hotspot: installed when missing; when there, only its program file is refreshed (a service that
# was turned off on purpose stays off) and it restarts unless its hotspot is open right now
if [ ! -f /etc/systemd/system/pelletpi-wifi-setup.service ]; then
  sh "$HERE/install-wifi-setup.sh" || echo "Wi-Fi setup hotspot not installed"
else
  sed 's/\r$//' "$HERE/pelletpi_wifi_setup.py" > /usr/local/lib/pelletpi/pelletpi_wifi_setup.py.new
  if ! cmp -s /usr/local/lib/pelletpi/pelletpi_wifi_setup.py.new /usr/local/lib/pelletpi/pelletpi_wifi_setup.py; then
    mv -f /usr/local/lib/pelletpi/pelletpi_wifi_setup.py.new /usr/local/lib/pelletpi/pelletpi_wifi_setup.py
    if ! nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx pelletpi-setup; then
      systemctl try-restart pelletpi-wifi-setup.service || true
    fi
  else
    rm -f /usr/local/lib/pelletpi/pelletpi_wifi_setup.py.new
  fi
fi

sed 's/\r$//' "$HERE/pelletpi_wifi_watchdog.py" > /usr/local/lib/pelletpi/pelletpi_wifi_watchdog.py

# Wi-Fi power save off for every Wi-Fi connection (a known cause of brcmfmac drops on the Pi)
mkdir -p /etc/NetworkManager/conf.d
printf '# Pellet burner monitor: Wi-Fi power save off (2 = disable).\n[connection]\nwifi.powersave = 2\n' \
  > /etc/NetworkManager/conf.d/pelletpi-wifi.conf
nmcli general reload conf >/dev/null 2>&1 || true
# the known Wi-Fi client connections (found by type, access points skipped): try forever (autoconnect-retries 0)
# and power save off, from their next connect; only changed when not set yet. Power save off at once as well.
python3 /usr/local/lib/pelletpi/pelletpi_wifi_watchdog.py --fix-only || true
if [ -x /sbin/iw ] && [ -e /sys/class/net/wlan0 ]; then /sbin/iw dev wlan0 set power_save off 2>/dev/null || true; fi

if [ ! -f /etc/default/pelletpi-wifi-watchdog ]; then
  printf '# Pellet burner monitor Wi-Fi watchdog. WIFI_WATCHDOG=no turns it off (the card setting wins).\nWIFI_WATCHDOG=yes\n' \
    > /etc/default/pelletpi-wifi-watchdog
fi

cat > /etc/systemd/system/pelletpi-wifi-watchdog.service <<EOF
[Unit]
Description=Pellet burner monitor: Wi-Fi watchdog (reconnects, opens the setup hotspot)
After=NetworkManager.service
[Service]
Type=oneshot
TimeoutStartSec=300
ExecStart=/usr/bin/python3 /usr/local/lib/pelletpi/pelletpi_wifi_watchdog.py $APP/data
EOF

cat > /etc/systemd/system/pelletpi-wifi-watchdog.timer <<'EOF'
[Unit]
Description=Pellet burner monitor: Wi-Fi watchdog every minute
[Timer]
OnBootSec=90
OnUnitInactiveSec=60
AccuracySec=5
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable pelletpi-wifi-watchdog.timer
systemctl restart pelletpi-wifi-watchdog.timer
echo "Wi-Fi watchdog installed (events in $APP/data/wifi-watchdog.jsonl)."
