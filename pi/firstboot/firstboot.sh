#!/bin/bash
# Pellet burner monitor - easy install, part 2: installs everything, unattended.
# Run by pelletpi-firstboot.service (see install.sh) after every start until it is done; systemd starts it
# again 1 minute after any failure. Each finished step leaves a marker in /var/lib/pelletpi/done-<step>,
# so a new run (after a failure or a power cut) goes on where it stopped. When all is done it writes
# /var/lib/pelletpi/installed and the service is not started again.
#
# Progress for people: pelletpi-status.txt on the card's boot drive (readable on any PC), the same text as a
# page on http://pelletpi.local while installing, and the full log in pelletpi-install.log.
# Never writes Wi-Fi names, passwords or keys anywhere.
set -eEuo pipefail

CARD=/boot/firmware
DIR=$CARD/pelletpi
STATE=/var/lib/pelletpi
WWW=$STATE/www
STATUS_TXT=$CARD/pelletpi-status.txt
LOG=$CARD/pelletpi-install.log
STEPS=9
APT=(apt-get -y -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
export DEBIAN_FRONTEND=noninteractive

mkdir -p "$STATE" "$WWW"
exec > >(tee -a "$LOG") 2>&1
echo "== $(date) firstboot start"

if [ -e "$STATE/installed" ]; then
  echo "already installed"
  exit 0
fi

HOST=$(hostname)
CURRENT="starting"
HINT=""

# ---------------------------------------------------------------- status for people (card file + web page)
status() {  # status STATE [WHAT-TO-DO]
  local state=$1 todo=${2:-} now
  now=$(date '+%Y-%m-%d %H:%M')
  {
    echo "Pellet burner monitor - installation status"
    echo
    echo "State:    $state"
    echo "Step:     $CURRENT"
    if [ -n "$todo" ]; then echo "To do:    $todo"; fi
    echo "Updated:  $now (the Pi's clock; it can be wrong until the Pi has internet)"
    echo
    echo "When ready, open http://$HOST.local in a browser on the same network."
    usbip=$(ip -4 -o addr show usb0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
    if [ -n "$usbip" ]; then echo "Over the USB-C cable: http://$usbip"; fi
    echo "Full log: pelletpi-install.log on this card."
  } > "$STATUS_TXT.new"
  mv -f "$STATUS_TXT.new" "$STATUS_TXT"
  {
    echo '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="20">'
    echo '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pellet burner monitor</title>'
    echo '<style>body{font:18px/1.5 system-ui,sans-serif;max-width:36em;margin:2em auto;padding:0 16px}'
    echo 'b{color:#b2440d}</style></head><body><h1>Setting up&hellip;</h1>'
    echo "<p><b>$state</b></p><p>Step $CURRENT</p>"
    if [ -n "$todo" ]; then echo "<p>$todo</p>"; fi
    echo '<p>This takes about 15 minutes. The page refreshes by itself and shows the monitor when it is ready.'
    echo 'Do not switch the Pi off.</p></body></html>'
  } > "$WWW/index.html"
  sync
}

on_error() {
  status "PROBLEM - the Pi tries again in 1 minute" \
    "${HINT:-Nothing, if the Pi has internet. If this stays for more than 1 hour, look in pelletpi-install.log.}"
}
trap on_error ERR

fail() { HINT=$1; return 1; }

# step NAME NUMBER "Title" command... : runs the command once; skipped when its marker exists
step() {
  local name=$1 n=$2 title=$3
  shift 3
  if [ -e "$STATE/done-$name" ]; then
    echo "skip $name (done before)"
    return 0
  fi
  CURRENT="$n of $STEPS - $title"
  HINT=""
  status "WORKING - please wait, do not switch off"
  echo "== $(date) step $CURRENT"
  "$@"
  touch "$STATE/done-$name"
}

setting() {  # setting KEY : value from settings.txt (never sourced, only read)
  sed -n "s/^$1=//p" "$DIR/settings.txt" 2>/dev/null | tr -d '\r' | tail -n 1
}

online() {
  python3 -c 'import urllib.request
urllib.request.urlopen("http://deb.debian.org/debian/", timeout=15)' >/dev/null 2>&1
}

wait_internet() {
  local tries=0
  until online; do
    tries=$((tries + 1))
    status "WAITING FOR INTERNET (try $tries, again every minute)" \
      "Plug in a network cable, or join the Wi-Fi pelletpi-setup with your phone and open http://10.42.0.1:8099 to pick your Wi-Fi (it opens after 3 minutes without network)."
    sleep 60
  done
  status "WORKING - please wait, do not switch off"
  # The Pi has no clock battery: give it up to 3 minutes to set its clock, or secure downloads fail.
  local i
  for i in $(seq 1 36); do
    if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" = yes ]; then break; fi
    sleep 5
  done
}

dc() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi
}

# ---------------------------------------------------------------- who owns the program (the Imager user)
OWNER=""
for i in $(seq 1 30); do
  OWNER=$(getent passwd 1000 | cut -d: -f1 || true)
  if [ -n "$OWNER" ]; then break; fi
  sleep 10
done
if [ -n "$OWNER" ]; then
  APP=$(getent passwd 1000 | cut -d: -f6)/pelletburner-monitor
else
  APP=/opt/pelletburner-monitor
fi
echo "user: ${OWNER:-none}, program folder: $APP, host: $HOST"

# ---------------------------------------------------------------- "setting up" page on port 80
start_setup_page() {
  cat > /etc/systemd/system/pelletpi-setup-page.service <<EOF
[Unit]
Description=Pellet burner monitor: "setting up" page on port 80 while installing
After=network.target
[Service]
ExecStart=/usr/bin/python3 -m http.server 80 --bind :: --directory $WWW
Restart=always
RestartSec=5
EOF
  systemctl daemon-reload
  systemctl start pelletpi-setup-page.service || true
}
status "WORKING - please wait, do not switch off"
if [ ! -e "$STATE/done-port80" ]; then start_setup_page; fi

# ---------------------------------------------------------------- the steps
usb_network() {
  local s=$DIR/pelletpi-setup.sh
  if [ ! -f "$s" ]; then s=$APP/pi/pelletpi-setup.sh; fi
  sed 's/\r$//' "$s" > "$STATE/pelletpi-setup.sh"
  sh "$STATE/pelletpi-setup.sh"            # writes pi-setup.log on the card
  if ! grep -q '^dtoverlay=dwc2' "$CARD/config.txt"; then
    printf '\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$CARD/config.txt"
    touch "$STATE/reboot-needed"
  else
    systemctl start pelletpi-usbnet.service || true   # overlay already there (ready image): start it now
  fi
}

wifi_setup() {
  local d=$DIR
  if [ ! -f "$d/install-wifi-setup.sh" ]; then d=$APP/pi; fi
  if [ ! -f "$d/install-wifi-setup.sh" ]; then echo "no Wi-Fi setup files: skipped"; return 0; fi
  sh "$d/install-wifi-setup.sh"
}

install_docker() {
  wait_internet
  "${APT[@]}" update
  "${APT[@]}" install docker.io docker-compose git
  systemctl enable --now docker
}

get_program() {
  if [ -f "$APP/docker-compose.yml" ]; then
    echo "program already in $APP"
    return 0
  fi
  rm -rf "$APP.tmp"
  if [ -f "$DIR/app.tar.gz" ]; then
    mkdir -p "$APP.tmp"
    tar -xzf "$DIR/app.tar.gz" -C "$APP.tmp"
  else
    local url branch
    url=$(setting REPO_URL)
    branch=$(setting REPO_BRANCH)
    case "$url" in
      "" | *CHANGE-ME*)
        fail "This card folder has no program (app.tar.gz) and no REPO_URL in pelletpi/settings.txt. Use the release folder."
        ;;
    esac
    wait_internet
    git clone --depth 1 ${branch:+--branch "$branch"} "$url" "$APP.tmp"
  fi
  mv "$APP.tmp" "$APP"
  if [ -n "$OWNER" ]; then chown -R "$OWNER:" "$APP"; fi
  # the installed version, for the auto-update and the page
  if [ -f "$DIR/app-version.txt" ]; then
    tr -d '\r\n' < "$DIR/app-version.txt" > "$STATE/version"
  else
    git -c safe.directory="$APP" -C "$APP" describe --tags --always > "$STATE/version" || true
  fi
}

start_program() {
  wait_internet                            # the first build downloads the base image and pyserial
  cd "$APP"
  mkdir -p data
  chown 1000:1000 data                     # the container runs as uid 1000
  if [ "$HOST" != pelletpi ] && [ ! -f .env ]; then
    echo "BURNER_ALLOWED_HOSTS=lan,$HOST.local,pelletpi.local" > .env
  fi
  dc up -d --build
  if [ -n "$OWNER" ]; then usermod -aG docker "$OWNER"; fi
  local i
  for i in $(seq 1 60); do
    if python3 -c 'import urllib.request, urllib.error, sys
try:
    urllib.request.urlopen("http://127.0.0.1:8787/", timeout=5)
except urllib.error.HTTPError:
    pass
except Exception:
    sys.exit(1)'; then
      echo "program answers on port 8787"
      return 0
    fi
    sleep 5
  done
  fail "The program did not start. Look in pelletpi-install.log (docker compose logs)."
}

pi_logging() {
  PI_USER=${OWNER:-root} sh "$APP/pi/install-pi-logging.sh"
}

wifi_watchdog() {
  sh "$APP/pi/install-wifi-watchdog.sh"
}

auto_update() {
  local url
  url=$(setting REPO_URL)
  case "$url" in *CHANGE-ME*) url="" ;; esac
  REPO_URL=$url sh "$APP/pi/install-auto-update.sh"
}

port_80() {
  systemctl stop pelletpi-setup-page.service || true
  rm -f /etc/systemd/system/pelletpi-setup-page.service
  sh "$APP/pi/enable-port-80.sh"
}

if [ -f "$DIR/pelletpi-setup.sh" ]; then
  step usbnet 1 "USB-C network cable" usb_network
fi
step wifisetup 2 "Wi-Fi setup hotspot (only while the Pi has no network)" wifi_setup
step docker  3 "Installing Docker (needs internet, about 5 minutes)" install_docker
step program 4 "Getting the program" get_program
step usbnet  1 "USB-C network cable" usb_network
step start   5 "Building and starting the program (about 5 minutes)" start_program
step logging 6 "Pi health logging" pi_logging
step wifiwd  7 "Wi-Fi watchdog" wifi_watchdog
step update  8 "Nightly safe auto-update" auto_update
step port80  9 "Page on http://$HOST.local" port_80

# ---------------------------------------------------------------- done
touch "$STATE/installed"
systemctl disable pelletpi-firstboot.service || true
CURRENT="all done"
if [ -e "$STATE/reboot-needed" ]; then
  rm -f "$STATE/reboot-needed"
  status "READY - the Pi restarts once now, then open the page in 2 minutes" \
    "Open http://$HOST.local on your phone or PC (same network)."
  echo "== $(date) ready, restarting once for the USB-C network"
  sync
  systemctl reboot
else
  status "READY" "Open http://$HOST.local on your phone or PC (same network)."
  echo "== $(date) ready"
fi
exit 0
