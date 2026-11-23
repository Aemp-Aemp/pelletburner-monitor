#!/bin/sh
# Safe auto-update for the Pi (see pelletpi-update.sh for the rules). Run once on the Pi:
#   sudo REPO_URL=https://github.com/Aemp-Aemp/pelletburner-monitor.git sh pi/install-auto-update.sh
# The easy install (pi/firstboot) runs it by itself. Safe to run again.
# - every night between 01:00 and 06:00 it checks for a new release and installs it when the burner is
#   off or waiting; the "Check for update" button on the page does the same at once
# - turn it off: AUTO_UPDATE=no in pelletpi/settings.txt on the card's boot drive, or in
#   /etc/default/pelletpi-update
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
APP=$(cd "$HERE/.." && pwd)
if [ -z "$REPO_URL" ]; then
  REPO_URL=$(git -c safe.directory="$APP" -C "$APP" remote get-url origin 2>/dev/null || true)
fi
sed 's/\r$//' "$HERE/pelletpi-update.sh" > /usr/local/bin/pelletpi-update
chmod 755 /usr/local/bin/pelletpi-update

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
RandomizedDelaySec=30min
Persistent=false
[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/pelletpi-update-check.service <<'EOF'
[Unit]
Description=Pellet burner monitor: update check asked for on the page
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/pelletpi-update check
EOF

# The page's "Check for update" button leaves data/update-request; this starts the check.
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
systemctl enable --now pelletpi-update.timer pelletpi-update-check.path
echo "Auto-update installed for $APP (source: ${REPO_URL:-none}, auto: $AUTO)."
