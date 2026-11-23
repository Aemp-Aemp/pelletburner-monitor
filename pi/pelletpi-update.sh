#!/bin/bash
# Pellet burner monitor - safe auto-update (runs on the Pi, as root). Installed by pi/install-auto-update.sh.
#
#   pelletpi-update auto    nightly timer: check, and install when auto-update is on
#   pelletpi-update check   "Check for update" button on the page: same as auto, right now
#   pelletpi-update now     by hand: install the newest release even when auto-update is off
#
# Rules:
# - Only release tags (v1.2.3) of REPO_URL are installed, never a branch.
# - The new version is BUILT while the old one keeps running; the swap (a few seconds) only happens when the
#   burner is off (state 0) or waiting (state 1). Never while lighting or burning: a gap in the module
#   traffic then can make the boiler show alarm 15.
# - If the new version does not answer within 2 minutes it goes back to the old one (old image, old files)
#   and never tries that version again.
# - The data folder is never touched. Everything is logged to data/update.log; data/update.json feeds the page.
# - Off: AUTO_UPDATE=no in pelletpi/settings.txt on the card's boot drive, or in /etc/default/pelletpi-update.
set -eEuo pipefail

MODE=${1:-auto}
CONF=/etc/default/pelletpi-update
STATE=/var/lib/pelletpi
APP=""
REPO_URL=""
AUTO_UPDATE=yes
# shellcheck disable=SC1090
if [ -f "$CONF" ]; then . "$CONF"; fi
CARD_SETTINGS=/boot/firmware/pelletpi/settings.txt
if [ -f "$CARD_SETTINGS" ]; then
  v=$(sed -n 's/^AUTO_UPDATE=//p' "$CARD_SETTINGS" | tr -d '\r' | tail -n 1)
  if [ -n "$v" ]; then AUTO_UPDATE=$v; fi
fi
[ -n "$APP" ] && [ -d "$APP" ] || { echo "pelletpi-update: APP not set in $CONF"; exit 1; }

DATA=$APP/data
JSON=$DATA/update.json
ULOG=$DATA/update.log
mkdir -p "$STATE" "$DATA"
rm -f "$DATA/update-request"
exec 9>"$STATE/update.lock"
flock -n 9 || { echo "another update is running"; exit 0; }

OWNER=$(stat -c %U "$APP")
G() { git -c safe.directory="$APP" -C "$APP" "$@"; }
dc() {
  if docker compose version >/dev/null 2>&1; then (cd "$APP" && docker compose "$@"); else (cd "$APP" && docker-compose "$@"); fi
}
log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$ULOG"
  if [ "$(wc -l < "$ULOG")" -gt 2000 ]; then tail -n 1000 "$ULOG" > "$ULOG.tmp" && mv -f "$ULOG.tmp" "$ULOG"; fi
}
CURRENT=$(cat "$STATE/version" 2>/dev/null || true)
if [ -z "$CURRENT" ]; then CURRENT=$(G describe --tags --always 2>/dev/null || echo unknown); fi
LATEST=""
RESULT=""
write_json() {
  python3 - "$JSON" "$CURRENT" "$LATEST" "$AUTO_UPDATE" "$RESULT" <<'EOF'
import json, sys, time
path, cur, latest, auto, result = sys.argv[1:6]
d = {"current": cur, "latest": latest or None, "available": bool(latest) and latest != cur,
     "auto": auto == "yes", "result": result, "checked": int(time.time())}
with open(path + ".tmp", "w") as f:
    json.dump(d, f)
import os
os.replace(path + ".tmp", path)
EOF
  chmod 644 "$JSON"
}
finish() { RESULT=$1; log "$RESULT"; write_json; exit "${2:-0}"; }
on_error() { RESULT="stopped by an error (line $1), see update.log"; log "$RESULT"; write_json || true; }
trap 'on_error $LINENO' ERR

burner_state() {  # prints the burner state number, or "?" when the page does not answer
  python3 -c 'import json, urllib.request
d = json.load(urllib.request.urlopen("http://127.0.0.1:8787/status.json", timeout=5))
s = ((d.get("latest") or {}).get("fields") or {}).get("state")
print("?" if s is None else int(s))' 2>/dev/null || echo "?"
}
safe_now() { case "$(burner_state)" in 0 | 1) return 0 ;; *) return 1 ;; esac; }
answers() {
  python3 -c 'import urllib.request
urllib.request.urlopen("http://127.0.0.1:8787/status.json", timeout=5)' >/dev/null 2>&1
}

# ---------------------------------------------------------------- 1) is there a newer release?
case "$REPO_URL" in "" | *CHANGE-ME*) finish "no REPO_URL set: updates are off" ;; esac
LATEST=$(git ls-remote --tags --refs "$REPO_URL" 2>/dev/null | sed 's|.*refs/tags/||' \
  | grep -E '^v[0-9]+(\.[0-9]+)*$' | sort -V | tail -n 1 || true)
if [ -z "$LATEST" ]; then finish "could not reach $REPO_URL or no release yet; will try again"; fi
if [ "$LATEST" = "$CURRENT" ]; then finish "up to date ($CURRENT)"; fi
if [ -e "$STATE/bad-$LATEST" ]; then finish "update $LATEST available, but it failed here before; skipped"; fi
if [ "$MODE" != now ] && [ "$AUTO_UPDATE" != yes ]; then finish "update $LATEST available (auto-update is off)"; fi
if ! safe_now; then finish "update $LATEST available; waits until the burner is off or waiting (state $(burner_state))"; fi

# ---------------------------------------------------------------- 2) build the new version next to the running one
log "updating $CURRENT -> $LATEST"
if [ ! -d "$APP/.git" ]; then            # installed from the release tarball: start a local history
  G init -q
  G add -A
  G -c user.name=pelletpi -c user.email=pelletpi@localhost commit -q -m "installed version $CURRENT"
fi
G remote remove origin 2>/dev/null || true
G remote add origin "$REPO_URL"
PREV=$(G rev-parse HEAD)
OLD_IMAGE=$(docker inspect -f '{{.Image}}' pelletburner)
IMAGE_NAME=$(docker inspect -f '{{.Config.Image}}' pelletburner)

back_to_old() {   # files and image of the old version; the old container is still running or restarted
  G checkout -q -f --detach "$PREV"
  docker tag "$OLD_IMAGE" "$IMAGE_NAME"
  if [ -n "$OWNER" ]; then chown -R --from=root "$OWNER:" "$APP" 2>/dev/null || true; fi
}

G fetch -q --depth 1 origin "refs/tags/$LATEST:refs/tags/$LATEST"
G checkout -q -f --detach "$LATEST"
if [ -n "$OWNER" ]; then chown -R --from=root "$OWNER:" "$APP" 2>/dev/null || true; fi
if ! dc build >>"$ULOG" 2>&1; then
  back_to_old
  finish "build of $LATEST failed; kept $CURRENT (will try again next night)" 1
fi

# ---------------------------------------------------------------- 3) swap only while the burner is off or waiting
waited=0
until safe_now; do
  if [ "$waited" -ge 1200 ]; then
    back_to_old
    finish "update $LATEST built, but the burner stayed busy for 20 minutes; will try again"
  fi
  sleep 30
  waited=$((waited + 30))
done
log "burner state $(burner_state): swapping now"
dc up -d >>"$ULOG" 2>&1 || true

# ---------------------------------------------------------------- 4) the new version must answer within 2 minutes
ok=no
for i in $(seq 1 24); do
  sleep 5
  if answers; then ok=yes; break; fi
done
if [ "$ok" != yes ]; then
  log "$LATEST did not answer within 2 minutes: going back to $CURRENT"
  touch "$STATE/bad-$LATEST"
  back_to_old
  dc up -d >>"$ULOG" 2>&1 || true
  finish "update to $LATEST failed and was undone; still on $CURRENT" 1
fi
echo "$LATEST" > "$STATE/version"
OLD=$CURRENT
CURRENT=$LATEST
docker image prune -f >/dev/null 2>&1 || true
finish "updated $OLD -> $LATEST"
