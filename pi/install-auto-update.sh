#!/bin/sh
# Safe auto-update for the Pi (see pelletpi-update.sh for the rules). Run once on the Pi:
#   sudo REPO_URL=https://github.com/Aemp-Aemp/pelletburner-monitor.git sh pi/install-auto-update.sh
# The easy install (pi/firstboot) runs it by itself. Safe to run again.
# - every night between 01:00 and 06:00 it checks for a new release and installs it when the burner is
#   off or waiting; the "Check for update" button on the page does the same at once, and "Go back" returns
#   to the version from before the last update
# - a normal update runs this file again by itself, so the updater on the Pi follows the program
# - it also installs or refreshes the Wi-Fi watchdog (install-wifi-watchdog.sh), so a Pi gets it with an update
# - turn it off: AUTO_UPDATE=no in pelletpi/settings.txt on the card's boot drive, or in
#   /etc/default/pelletpi-update
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
APP=$(cd "$HERE/.." && pwd)
if [ -z "$REPO_URL" ]; then
  REPO_URL=$(git -c safe.directory="$APP" -C "$APP" remote get-url origin 2>/dev/null || true)
fi
# written next to the old one and moved in place: an update that is running keeps its own copy
sed 's/\r$//' "$HERE/pelletpi-update.sh" > /usr/local/bin/pelletpi-update.new
chmod 755 /usr/local/bin/pelletpi-update.new
mv -f /usr/local/bin/pelletpi-update.new /usr/local/bin/pelletpi-update

# Keep an existing AUTO_UPDATE choice when this runs again.
AUTO=yes
if [ -f /etc/default/pelletpi-update ]; then
  AUTO=$(sed -n 's/^AUTO_UPDATE=//p' /etc/default/pelletpi-update | tail -n 1)
  [ -n "$AUTO" ] || AUTO=yes
fi
cat > /etc/default/pelletpi-update <<EOF
# Pellet burner monitor auto-update (read by /usr/local/bin/pelletpi-update)
APP=$APP
REPO_URL=$REPO_URL
AUTO_UPDATE=$AUTO
EOF

cat > /etc/systemd/system/pelletpi-update.service <<'EOF'
[Unit]
Description=Pellet burner monitor: nightly update check
After=network-online.target docker.service
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/pelletpi-update auto
EOF

cat > /etc/systemd/system/pelletpi-update.timer <<'EOF'
[Unit]
Description=Pellet burner monitor: update check every night (hourly 01-05, waits while the burner works)
[Timer]
OnCalendar=*-*-* 01..05:10:00
# also a few minutes after every start: tidies up an update that a power cut interrupted
OnBootSec=5min
RandomizedDelaySec=30min
Persistent=false
[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/pelletpi-update-check.service <<EOF
[Unit]
Description=Pellet burner monitor: update or go back, asked for on the page
After=docker.service
[Service]
Type=oneshot
# after the download the updater may wait hours for the burner to be off
TimeoutStartSec=infinity
# where the page leaves its request (removed even when the settings file is broken)
Environment=PELLETPI_REQ=$APP/data/update-request
ExecStart=/usr/local/bin/pelletpi-update check
EOF

# The page's buttons leave data/update-request (one word: check, retry or back); this starts the updater.
cat > /etc/systemd/system/pelletpi-update-check.path <<EOF
[Unit]
Description=Pellet burner monitor: watch for the page's update request
[Path]
PathExists=$APP/data/update-request
Unit=pelletpi-update-check.service
[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$APP/data"
systemctl daemon-reload
systemctl enable pelletpi-update.timer pelletpi-update-check.path
# restarted, so that changed units take effect now (a running update is not stopped by this)
systemctl restart pelletpi-update.timer pelletpi-update-check.path
# the Wi-Fi watchdog follows the program too (NO_WIFI_WATCHDOG=1 leaves it out); a failure here never stops the update
if [ -z "$NO_WIFI_WATCHDOG" ] && [ -f "$HERE/install-wifi-watchdog.sh" ]; then
  sh "$HERE/install-wifi-watchdog.sh" || echo "Wi-Fi watchdog not installed"
fi
echo "Auto-update installed for $APP (source: ${REPO_URL:-none}, auto: $AUTO)."
