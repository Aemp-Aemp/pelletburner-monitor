#!/bin/sh
# Install everything with one command on a fresh Raspberry Pi OS (or Debian), after cloning:
#   git clone https://github.com/Aemp-Aemp/pelletburner-monitor.git
#   cd pelletburner-monitor
#   sudo sh pi/install.sh
# It installs Docker, builds and starts the monitor, and sets up the page on port 80, Pi health logging,
# the safe nightly auto-update and the Wi-Fi setup hotspot. Safe to run again. Leave a part out: NO_PORT80=1,
#   NO_LOGGING=1, NO_UPDATE=1, NO_WIFI_SETUP=1
#   (example: sudo NO_UPDATE=1 sh pi/install.sh). It never talks to the boiler.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
APP=$(cd "$HERE/.." && pwd)
HOST=$(hostname)

if [ "$(id -u)" != 0 ]; then
  echo "Please run it with sudo:  sudo sh pi/install.sh"
  exit 1
fi
if [ ! -f "$APP/docker-compose.yml" ]; then
  echo "docker-compose.yml not found in $APP. Run it from the cloned folder."
  exit 1
fi
# The person who cloned the program (gets the docker group and owns the logs).
OWNER=${SUDO_USER:-}
if [ -z "$OWNER" ] || [ "$OWNER" = root ]; then OWNER=$(stat -c %U "$APP"); fi

say() { printf '\n== %s\n' "$1"; }
dc() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }

say "1/6 Docker"
export DEBIAN_FRONTEND=noninteractive
if ! command -v docker >/dev/null 2>&1; then
  apt-get update
  apt-get -y install docker.io docker-compose git
elif ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
  apt-get update
  apt-get -y install docker-compose
fi
systemctl enable --now docker
if [ "$OWNER" != root ]; then usermod -aG docker "$OWNER"; fi

say "2/6 Building and starting the monitor (about 5 minutes the first time)"
cd "$APP"
mkdir -p data
chown 1000:1000 data                       # the container runs as uid 1000
if [ "$HOST" != pelletpi ] && [ ! -f .env ]; then
  echo "BURNER_ALLOWED_HOSTS=lan,$HOST.local,pelletpi.local" > .env
  chown "$OWNER": .env
fi
PORT=$(sed -n 's/^BURNER_HTTP_PORT=//p' .env 2>/dev/null | tr -d '\r' | tail -n 1)
PORT=${PORT:-8787}
dc up -d --build
i=0
until python3 -c 'import sys, urllib.request, urllib.error
try:
    urllib.request.urlopen("http://127.0.0.1:" + sys.argv[1] + "/", timeout=5)
except urllib.error.HTTPError:
    pass' "$PORT" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "The monitor did not answer on port $PORT. See: docker compose logs"
    exit 1
  fi
  sleep 5
done
echo "The monitor answers on port $PORT"

say "3/6 Page without a port number (port 80)"
if [ -n "$NO_PORT80" ]; then
  echo "skipped (NO_PORT80)"
elif [ "$PORT" != 8787 ]; then
  echo "skipped: port 80 only works with the default port 8787"
else
  sh "$HERE/enable-port-80.sh"
fi

say "4/6 Pi health logging"
if [ -n "$NO_LOGGING" ]; then echo "skipped (NO_LOGGING)"; else PI_USER=$OWNER sh "$HERE/install-pi-logging.sh"; fi

say "5/6 Safe nightly auto-update (only while the burner is off or waiting)"
if [ -n "$NO_UPDATE" ]; then echo "skipped (NO_UPDATE)"; else sh "$HERE/install-auto-update.sh"; fi

say "6/6 Wi-Fi setup hotspot (only while the Pi has no network)"
if [ -n "$NO_WIFI_SETUP" ]; then echo "skipped (NO_WIFI_SETUP)"; else sh "$HERE/install-wifi-setup.sh"; fi

if [ -n "$NO_PORT80" ] || [ "$PORT" != 8787 ]; then URL="http://$HOST.local:$PORT"; else URL="http://$HOST.local"; fi
say "Done"
echo "Open $URL on your phone or PC (same network). The classic page: $URL/classic"
echo "Log out and in again to use docker without sudo."
