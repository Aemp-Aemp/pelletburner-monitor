#!/bin/bash
# Maintainers: build a ready-to-flash card image from the official Raspberry Pi OS Lite 64-bit image.
# Runs on Linux or on a Pi (needs root for loop mounts):
#
#   sudo scripts/make-image.sh 2025-xx-xx-raspios-trixie-arm64-lite.img.xz [pelletpi.img] [REPO_URL]
#
# What it does:
#   1. builds the card folder (python3 scripts/make_card.py, with the program inside as app.tar.gz)
#   2. copies the image, mounts its two partitions through a loop device
#   3. puts the card folder on the boot partition, adds the USB-C overlay to config.txt and enables
#      pelletpi-firstboot.service in the root file system (so it does not depend on cloud-init vendor-data)
#   4. unmounts and compresses to <out>.xz
# People then flash it with Raspberry Pi Imager ("Use custom") and still set host name, user and Wi-Fi in
# Imager's settings. No Wi-Fi, passwords or keys go into the image.
# UNTESTED - try it on a spare card first.
set -euo pipefail

IN=${1:?usage: sudo $0 <raspios-lite-arm64.img[.xz]> [out.img] [REPO_URL]}
OUT=${2:-pelletpi.img}
REPO_URL=${3:-}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
WORK=$(mktemp -d)
LOOP=""

cleanup() {
  set +e
  umount "$WORK/boot" "$WORK/root" 2>/dev/null
  if [ -n "$LOOP" ]; then losetup -d "$LOOP"; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
for c in losetup mount xz python3 git; do command -v "$c" >/dev/null || { echo "missing: $c"; exit 1; }; done

# 1) card folder
python3 "$ROOT/scripts/make_card.py" --out "$WORK/dist" ${REPO_URL:+--repo-url "$REPO_URL"}
CARD=$WORK/dist/pelletpi-card

# 2) image copy + loop device
case "$IN" in
  *.xz) xz -dc "$IN" > "$OUT" ;;
  *) cp "$IN" "$OUT" ;;
esac
LOOP=$(losetup -P --show -f "$OUT")
mkdir -p "$WORK/boot" "$WORK/root"
mount "${LOOP}p1" "$WORK/boot"
mount "${LOOP}p2" "$WORK/root"

# 3) card folder on the boot partition, USB-C overlay, service enabled
cp -r "$CARD/." "$WORK/boot/"
if ! grep -q '^dtoverlay=dwc2' "$WORK/boot/config.txt"; then
  printf '\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$WORK/boot/config.txt"
fi
install -m 644 "$CARD/pelletpi/pelletpi-firstboot.service" \
  "$WORK/root/etc/systemd/system/pelletpi-firstboot.service"
mkdir -p "$WORK/root/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/pelletpi-firstboot.service \
  "$WORK/root/etc/systemd/system/multi-user.target.wants/pelletpi-firstboot.service"

# 4) done
sync
umount "$WORK/boot" "$WORK/root"
losetup -d "$LOOP"
LOOP=""
xz -T0 -f "$OUT"
echo "ready: $OUT.xz  (flash with Raspberry Pi Imager -> Use custom; then set host name, user and Wi-Fi)"
