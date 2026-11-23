#!/bin/sh
# Pellet burner monitor - easy install, part 1 (a few seconds).
# Hooks firstboot.sh into the Pi's start-up as a service that keeps trying until everything is installed.
#
# Started once, in one of these ways:
#   - by cloud-init through the file "vendor-data" next to this folder (normal case, nothing to edit), with --start
#   - by cmdline.txt (fallback): systemd.run=/boot/firmware/pelletpi/install.sh
#       systemd.run_success_action=reboot systemd.unit=kernel-command-line.target
#   - by scripts/make-image.sh, which does the same steps on a ready image.
# Safe to run again: it only (re)writes the same service file.
set -e
DIR=/boot/firmware/pelletpi
UNIT=/etc/systemd/system/pelletpi-firstboot.service

# Files copied from a Windows PC may have CRLF line ends, which break shell scripts.
for f in "$DIR"/*.sh "$DIR"/*.txt "$DIR"/*.service; do
  [ -f "$f" ] && sed -i 's/\r$//' "$f"
done

if [ -e /var/lib/pelletpi/installed ]; then
  echo "pelletpi: already installed, nothing to do"
else
  install -m 644 "$DIR/pelletpi-firstboot.service" "$UNIT"
  systemctl daemon-reload || true
  systemctl enable pelletpi-firstboot.service
  printf 'State: STARTING\nThe Pi has found the install files and will start installing in a moment.\n' \
    > /boot/firmware/pelletpi-status.txt
fi

# Fallback route: take ourselves out of cmdline.txt so this only runs once.
if grep -q 'pelletpi/install.sh' /boot/firmware/cmdline.txt 2>/dev/null; then
  sed -i 's| systemd.run=[^ ]*||; s| systemd.run_success_action=[^ ]*||; s| systemd.unit=kernel-command-line.target||' \
    /boot/firmware/cmdline.txt
fi

if [ "$1" = "--start" ] && [ ! -e /var/lib/pelletpi/installed ]; then
  # --no-block: cloud-init must finish first (the service is ordered after it)
  systemctl start --no-block pelletpi-firstboot.service
fi
sync
exit 0
