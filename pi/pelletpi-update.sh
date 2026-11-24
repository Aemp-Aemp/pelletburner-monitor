#!/bin/bash
# Pellet burner monitor - safe update (runs on the Pi, as root). Installed by pi/install-auto-update.sh.
#
#   pelletpi-update auto    nightly timer: check, and install when auto-update is on
#   pelletpi-update check   the page's button: check and install now (also when auto-update is off).
#                           The page leaves one word in data/update-request: check, retry or back.
#   pelletpi-update now     the same as check, by hand
#   pelletpi-update retry   install the newest release also when it failed here before
#   pelletpi-update back    go back to the version from before the last update
#
# Rules:
# - Only release tags (v1.2.3) of REPO_URL are installed, never a branch or a pre-release (v1.2.3-rc1),
#   and never an older version than the one that runs. REPO_URL comes from the Pi's own file, never from the page.
# - The new version is BUILT while the old one keeps running; the swap (a few seconds) only happens when the
#   burner is off (state 0) or waiting (state 1), or when the boiler has been silent for 10 minutes while the
#   monitor has run for 10 minutes (switched off, cable out), or when the monitor's container is stopped, or when
#   its page has not answered for 10 minutes AND nothing has come from the boiler for 10 minutes (data/last_rx,
#   data/hist-*: the page and the serial link are independent). Never while lighting or burning: a gap in the
#   module traffic then can make the boiler show alarm 15.
# - After the swap the new version is tested: it must answer within 2 minutes with a valid status, the settings
#   file must still load, and if the boiler was connected before it must be connected again within 5 more
#   minutes. If not, the Pi goes back to the old one (old image, old files) and never tries that version again
#   by itself.
# - The old version is kept for 14 days ("Go back" on the page).
# - A run that was cut short (power cut) is tidied up by the next run: the new version is kept when it runs and
#   answers, else the old one comes back (files, image name and program together, only when the burner is off or
#   waiting; until then the new version stays whole).
# - Local changes to the program's own files stop the update (they would be lost).
# - A running version that is not exactly a release or pre-release name (a copy cloned from a branch, v1.2.3-4-gabc,
#   a dirty copy) is never replaced at night; the button installs the release only when the running copy is older.
# - A release whose build failed 3 times is skipped until "Try again".
# - The data folder is never touched. The log is /var/lib/pelletpi/update.log (a copy is written to
#   data/update.log for the page); data/update.json feeds the page.
# - Off: AUTO_UPDATE=no in pelletpi/settings.txt on the card's boot drive, or in /etc/default/pelletpi-update.
#   The page's button still works then.
set -eEuo pipefail
export GIT_TERMINAL_PROMPT=0

MODE=${1:-auto}
CONF=${PELLETPI_CONF:-/etc/default/pelletpi-update}
STATE=/var/lib/pelletpi
URL=""               # the monitor's page; empty = http://127.0.0.1:<BURNER_HTTP_PORT of the container, or .env, or 8787>
CARD_SETTINGS=/boot/firmware/pelletpi/settings.txt
STEP=5               # seconds between two looks at the new version
START_TRIES=24       # x STEP = 2 minutes to answer
LINK_TRIES=60        # x STEP = 5 more minutes for the boiler to connect again
WAIT_STEP=30         # seconds between two looks at the burner while it is busy
WAIT_AUTO=1200       # the nightly run waits 20 minutes for the burner (it runs again an hour later)
WAIT_PAGE=21600      # a press on the page waits 6 hours (the download is done; no internet needed)
QUIET_WAIT=90        # a silent boiler must still be silent after this many seconds
DEAD_WAIT=600        # a monitor that has not answered for this long has no module traffic to interrupt
BUILD_TRIES=3        # a release whose build failed this often is skipped until "Try again"
BUILD_TIMEOUT=3600   # a build that takes longer than this (seconds) counts as failed
KEEP_DAYS=14         # how long the previous version is kept for "go back"
MIN_FREE_KB=500000   # free space needed before a download
APP=""
REPO_URL=""
AUTO_UPDATE=yes
# shellcheck disable=SC1090
if [ -f "$CONF" ]; then . "$CONF"; fi
if [ -f "$CARD_SETTINGS" ]; then
  v=$(sed -n 's/^AUTO_UPDATE=//p' "$CARD_SETTINGS" | tr -d '\r' | tail -n 1)
  if [ -n "$v" ]; then AUTO_UPDATE=$v; fi
fi
case "$MODE" in
  auto | check | now | retry | back) ;;
  *) echo "usage: pelletpi-update auto|check|now|retry|back"; exit 2 ;;
esac
if [ -z "$APP" ] || [ ! -d "$APP" ]; then
  # the page's request is removed all the same: else the path unit starts this again and again
  if [ "$MODE" = check ]; then rm -f "${PELLETPI_REQ:-/nonexistent}" "${APP:-/nonexistent}/data/update-request" 2>/dev/null || true; fi
  echo "pelletpi-update: APP not set in $CONF"; exit 1
fi

DATA=$APP/data
JSON=$DATA/update.json
ULOG=$STATE/update.log           # root's own folder: nothing there can be a planted link
PAGE_LOG=$DATA/update.log        # a copy for the page, written safely by write_json
REQ=$DATA/update-request
JOURNAL=$STATE/in-progress
KEPT=$STATE/previous
mkdir -p "$STATE" "$DATA"
exec 9>"$STATE/update.lock"
if [ "$MODE" = check ]; then
  # the page's request: wait for a run that is at work (the timer's), then handle it here, inside the lock
  flock -w 86400 9 || { rm -f "$REQ"; echo "another update is running"; exit 0; }
  # The page can only leave one of three words. Anything else counts as "check".
  ask=$(head -c 16 "$REQ" 2>/dev/null | tr -cd 'a-z' || true)
  case "$ask" in back) MODE=back ;; retry) MODE=retry ;; *) MODE=now ;; esac
  rm -f "$REQ"
else
  flock -n 9 || { echo "another update is running"; exit 0; }   # a page request is left for the path unit
fi

OWNER=$(stat -c %U "$APP" 2>/dev/null || true)
URL_FIXED=$URL
G() { git -c safe.directory="$APP" -C "$APP" "$@"; }
DC=(docker-compose)
if docker compose version >/dev/null 2>&1; then DC=(docker compose); fi
dc() { (cd "$APP" && "${DC[@]}" "$@"); }
log() {
  if [ -L "$ULOG" ] || [ -L "$ULOG.tmp" ]; then rm -f "$ULOG" "$ULOG.tmp"; fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$ULOG"
  if [ "$(wc -l < "$ULOG")" -gt 2000 ]; then tail -n 1000 "$ULOG" > "$ULOG.tmp" && mv -f "$ULOG.tmp" "$ULOG"; fi
}
is_release() { printf '%s\n' "$1" | grep -Eq '^v[0-9]+(\.[0-9]+)*$'; }
# a pre-release name (v1.2.3-rc1), not a build after a tag (v1.2.3-4-gabc) and not a dirty copy
is_pre() { printf '%s\n' "$1" | grep -Eq '^v[0-9]+(\.[0-9]+)*-[A-Za-z][A-Za-z0-9.]*$' && ! printf '%s\n' "$1" | grep -qi dirty; }
is_version() { printf '%s\n' "$1" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$'; }

CURRENT=$(cat "$STATE/version" 2>/dev/null || true)
if [ -z "$CURRENT" ]; then CURRENT=$(G describe --tags --always 2>/dev/null || echo unknown); fi
check_current() {  # a release name in the version file counts only when the files are that release (a stale file
  # after a developer checked out a branch must not make a branch copy look like a release)
  local h t
  if [ -d "$APP/.git" ] && { is_release "$CURRENT" || is_pre "$CURRENT"; }; then
    h=$(G rev-parse -q --verify HEAD 2>/dev/null || true)
    t=$(G rev-parse -q --verify "refs/tags/$CURRENT^{commit}" 2>/dev/null || true)
    if [ -n "$h" ] && [ "$h" = "$t" ]; then return 0; fi
    # a tarball install's own first commit has no tag: it is that version
    if [ -n "$h" ] && [ "$(G log -1 --format='%an %s' HEAD 2>/dev/null || true)" = "pelletpi installed version $CURRENT" ]; then return 0; fi
    CURRENT=$(G describe --tags --always 2>/dev/null || echo unknown)
    log "the files are not the release in the version file: running $CURRENT"
  fi
}
set_version() {  # written in one move and synced: a power cut leaves the old or the new name
  echo "$1" > "$STATE/version.tmp"
  mv -f "$STATE/version.tmp" "$STATE/version"
  sync
  CURRENT=$1
}
LATEST=""
PHASE=idle
CODE=""
WHY=""
FROM=""
TO=""
RESULT=""
PREV=""
OLD_IMAGE=""
NEW_IMAGE=""
IMAGE_NAME=""
KIND=""
SWAPPED=""          # 1 once docker was asked to start the other version: restore must start the old one again
P_H=down
P_CON=0
P_ST="?"

kept() { sed -n "s/^$1=//p" "$KEPT" 2>/dev/null | tail -n 1 || true; }   # VERSION / AT of the kept previous version
kept_at() { kept AT | tr -cd '0-9' | head -c 12 || true; }
write_json() {
  local pv="" pu=0
  if [ -f "$KEPT" ]; then
    pv=$(kept VERSION)
    pu=$(( $(kept_at) + KEEP_DAYS * 86400 ))
  fi
  U_CUR=$CURRENT U_LATEST=$LATEST U_AUTO=$AUTO_UPDATE U_RESULT=$RESULT U_PHASE=$PHASE U_CODE=$CODE U_WHY=$WHY \
    U_FROM=$FROM U_TO=$TO U_PV=$pv U_PU=$pu python3 - "$JSON" "$ULOG" "$PAGE_LOG" <<'EOF'
import json, os, sys, time
e = os.environ.get
path, cur, latest = sys.argv[1], e("U_CUR"), e("U_LATEST")


def put(dest, text):    # a new file next to it (never through a link), then moved in place
    tmp = dest + ".tmp"
    try:
        os.unlink(tmp)
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, dest)


try:                    # the page's copy of the log: never a reason for update.json not to be written
    with open(sys.argv[2], encoding="utf-8", errors="replace") as f:
        put(sys.argv[3], f.read())
except Exception:
    pass
prev = {"version": e("U_PV"), "until": int(e("U_PU") or 0)} if e("U_PV") else None
d = {"v": 2, "current": cur, "latest": latest or None, "available": bool(latest) and latest != cur,
     "auto": e("U_AUTO") == "yes", "phase": e("U_PHASE") or "idle", "code": e("U_CODE") or None,
     "why": e("U_WHY") or None, "from": e("U_FROM") or None, "to": e("U_TO") or None,
     "result": e("U_RESULT") or "", "checked": int(time.time()), "previous": prev}
put(path, json.dumps(d))
EOF
}
phase() { PHASE=$1; RESULT=$2; log "$RESULT"; write_json; }
alive() { touch -c "$JSON" 2>/dev/null || true; }          # tells the page that the updater is still at work
finish() {  # finish CODE "words for the log" [exit code]
  PHASE=idle; CODE=$1; RESULT=$2
  log "$RESULT"
  write_json
  exit "${3:-0}"
}

# ---------------------------------------------------------------- what the running program says
base_url() {  # the monitor's page: BURNER_HTTP_PORT of the container, else of .env, else 8787. Read at every look:
  # a new version may bring another default.
  local p=""
  if [ -n "$URL_FIXED" ]; then echo "$URL_FIXED"; return 0; fi
  p=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' pelletburner 2>/dev/null \
      | sed -n 's/^BURNER_HTTP_PORT=//p' | tr -cd '0-9' | head -c 5 || true)
  if [ -z "$p" ] && [ -f "$APP/.env" ]; then
    p=$(sed -n 's/^BURNER_HTTP_PORT=//p' "$APP/.env" | tr -d '\r' | tail -n 1 | tr -cd '0-9' | head -c 5 || true)
  fi
  echo "http://127.0.0.1:${p:-8787}"
}
probe() {  # prints: HEALTH LINK STATE   (ok|down|status, 1|0, a state number|quiet|?)
  { python3 - "$(base_url)" 2>/dev/null || echo "down 0 ?"; } <<'EOF' | tr -d '\r'
import json, sys, urllib.request
ask = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # this machine: never through a proxy
try:
    d = json.load(ask.open(sys.argv[1] + "/status.json?since=9999999999", timeout=5))
except Exception:
    print("down 0 ?")
    sys.exit(0)
if not isinstance(d, dict) or "connected" not in d:
    print("status 0 ?")
    sys.exit(0)
con = d.get("connected") is True
st = "?"
try:
    if con:
        st = int(((d.get("latest") or {}).get("fields") or {}).get("state"))
    else:
        # silent for 10 minutes, and the monitor has listened for 10 minutes (last_rx is 0 just after a start)
        rx, now, up = d.get("last_rx"), d.get("now"), d.get("started")
        upt, age = d.get("uptime"), d.get("rx_age")
        if isinstance(upt, (int, float)):                     # seconds by the monitor's monotonic clock: a Pi
            if upt >= 600 and (age is None or age > 600):     # without a clock chip jumps at NTP sync; these do not
                st = "quiet"
        elif now and up and now - up >= 600 and (not rx or now - rx > 600):
            st = "quiet"
        elif now and not up and rx and now - rx > 600:      # a monitor without "started" (older versions)
            st = "quiet"
except (TypeError, ValueError, AttributeError):
    st = "?"
print("ok", 1 if con else 0, st)
EOF
}
look() {  # asks once: P_H = health, P_CON = boiler link, P_ST = state
  local o
  o=$(probe) || o="down 0 ?"
  P_H=${o%% *}; o=${o#* }; P_CON=${o%% *}; P_ST=${o#* }
}
container_state() {  # yes | no | unknown (docker cannot tell, e.g. while its daemon restarts)
  local out
  if out=$(docker inspect -f '{{.State.Running}}' pelletburner 2>&1); then
    case "$out" in true) echo yes ;; false) echo no ;; *) echo unknown ;; esac
  else
    case "$out" in *[Nn]o\ such*) echo no ;; *) echo unknown ;; esac
  fi
}
rx_mark() {  # when the monitor last heard the boiler: the newest of data/last_rx and data/hist-*.jsonl (epoch s)
  find "$DATA" -maxdepth 1 -type f \( -name last_rx -o -name 'hist-*.jsonl' \) -printf '%T@\n' 2>/dev/null \
    | sort -n | tail -n 1 | cut -d. -f1 || true
}
safe_now() {  # may the program be restarted now?
  local waited=0 m0="" m1="" t
  look
  if [ "$P_H" != ok ]; then
    # No valid answer from the page. A stopped container sends nothing to the boiler. A running one may: the page
    # and the serial link are independent (another port, a page that could not start, a stuck page thread).
    m0=$(rx_mark)
    while [ "$P_H" != ok ]; do
      case "$(container_state)" in
        no) log "the monitor's container is not running: nothing to interrupt"; return 0 ;;
        unknown) log "docker cannot tell whether the monitor runs: not a safe time"; return 1 ;;
      esac
      m1=$(rx_mark)
      t=$(date +%s)
      if [ -n "$m1" ] && [ $((t - m1)) -lt "$DEAD_WAIT" ]; then
        log "the monitor's page does not answer, but it heard the boiler $((t - m1)) s ago: not a safe time"
        return 1
      fi
      if [ "$waited" -ge "$DEAD_WAIT" ]; then
        if [ -n "$m1" ] && [ "$m1" = "$m0" ]; then
          log "the monitor's page has not answered for $waited s and it has heard nothing from the boiler for $((t - m1)) s: nothing to interrupt"
          return 0
        fi
        log "the monitor's page does not answer, but it may still talk to the boiler: not a safe time"
        return 1
      fi
      sleep "$WAIT_STEP"
      alive
      waited=$((waited + WAIT_STEP))
      look
    done
  fi
  case "$P_ST" in
    0 | 1) return 0 ;;
    quiet)                       # no boiler on the line: sure only when it stays silent (not just after a restart)
      sleep "$QUIET_WAIT"
      look
      [ "$P_ST" = quiet ]
      ;;
    *) return 1 ;;
  esac
}
burner_word() { look; echo "$P_ST"; }
settings_keys() {  # the top-level names in data/settings.json, or "-" when there is no good file
  { python3 - "$DATA/settings.json" 2>/dev/null || echo "-"; } <<'EOF' | tr -d '\r'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    print(" ".join(sorted(d)) if isinstance(d, dict) else "-")
except Exception:
    print("-")
EOF
}
settings_ok() {  # $1 = settings_keys from before the swap: the file is still good and the program reads it
  python3 - "$(base_url)" "$DATA/settings.json" "$1" <<'EOF' >/dev/null 2>&1
import json, sys, urllib.request
url, path, before = sys.argv[1:4]
if before != "-":
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or not set(before.split()) <= set(d):
        sys.exit(1)
    ask = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    s = json.load(ask.open(url + "/api/settings", timeout=5))
    if not isinstance(s, dict) or not isinstance(s.get("settings"), dict):
        sys.exit(1)
EOF
}
health() {  # $1 = boiler link before the swap (1/0), $2 = settings_keys before. Sets WHY when it fails.
  local i
  P_H=down
  P_CON=0
  for i in $(seq 1 "$START_TRIES"); do
    sleep "$STEP"
    alive
    look
    if [ "$P_H" = ok ]; then break; fi
  done
  if [ "$P_H" != ok ]; then
    if [ "$P_H" = status ]; then WHY=status; else WHY=no_answer; fi
    return 1
  fi
  if ! settings_ok "$2"; then WHY=settings; return 1; fi
  if [ "$1" = 1 ]; then
    for i in $(seq 1 "$LINK_TRIES"); do
      if [ "$P_CON" = 1 ]; then return 0; fi
      sleep "$STEP"
      alive
      look
    done
    if [ "$P_CON" != 1 ]; then WHY=boiler; return 1; fi
  fi
  return 0
}

# ---------------------------------------------------------------- the old version: keep, restore
journal() {  # written before anything is changed, removed when the run is done
  printf 'KIND=%s\nFROM=%s\nTO=%s\nPREV=%s\nOLD_IMAGE=%s\nIMAGE_NAME=%s\nSWAPPED=%s\n' \
    "$KIND" "$FROM" "$TO" "$PREV" "$OLD_IMAGE" "$IMAGE_NAME" "$SWAPPED" > "$JOURNAL.tmp"
  mv -f "$JOURNAL.tmp" "$JOURNAL"
  sync
}
jget() { sed -n "s/^$1=//p" "$JOURNAL" 2>/dev/null | tail -n 1 || true; }
running_image() { docker inspect -f '{{.Image}}' pelletburner 2>/dev/null || true; }
image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }
own_files() { if [ -n "$OWNER" ]; then chown -h -R --from=root "$OWNER:" "$APP" 2>/dev/null || true; fi; }
local_changes() { [ -d "$APP/.git" ] && [ -n "$(G -c core.fileMode=false status --porcelain --untracked-files=no 2>/dev/null || echo x)" ]; }
swapped() {  # $1 = the image that should run now. 0 = it runs, 1 = it does not, 2 = docker cannot tell.
  # A release that changes only pi/ or docker-compose.yml builds the same image as before: that is fine.
  local s="" i
  for i in 1 2 3; do
    s=$(container_state)
    if [ "$s" != unknown ]; then break; fi
    sleep "$STEP"
  done
  if [ "$s" = unknown ]; then return 2; fi
  [ "$s" = yes ] && [ -n "$1" ] && [ "$(running_image)" = "$1" ]
}
back_to_old() {   # files and image name of the version from before this run
  if [ -n "$PREV" ]; then
    G checkout -q -f --detach "$PREV" >>"$ULOG" 2>&1 || log "could not restore the files of $FROM"
  fi
  if [ -n "$OLD_IMAGE" ] && [ -n "$IMAGE_NAME" ]; then
    docker tag "$OLD_IMAGE" "$IMAGE_NAME" >>"$ULOG" 2>&1 || log "could not restore the image of $FROM"
  fi
  own_files
}
restore() {       # back_to_old, and the old program started again if the new one was started
  back_to_old
  # Only after a swap or when another image runs: restore also follows a failed download or build, when the
  # burner may be burning. A release with the same image (only docker-compose.yml changed) needs the restart too.
  if [ -n "$OLD_IMAGE" ] && { [ "$SWAPPED" = 1 ] || [ "$(running_image)" != "$OLD_IMAGE" ]; }; then
    dc up -d >>"$ULOG" 2>&1 || true
  fi
  rm -f "$JOURNAL"
  sync
}
drop_kept() {
  docker rmi pelletpi-previous:latest >/dev/null 2>&1 || true
  G tag -d pelletpi-previous >/dev/null 2>&1 || true
  rm -f "$KEPT"
}
accept() {        # the new version passed the test: it is the current one; the old one is kept for "go back"
  set_version "$TO"
  rm -f "$STATE/buildfail-$TO"
  if docker tag "$OLD_IMAGE" pelletpi-previous:latest >>"$ULOG" 2>&1 && G tag -f pelletpi-previous "$PREV" >/dev/null 2>&1; then
    printf 'VERSION=%s\nAT=%s\n' "$FROM" "$(date +%s)" > "$KEPT"
  else
    drop_kept
  fi
  rm -f "$JOURNAL"
  sync
  docker image prune -f >/dev/null 2>&1 || true
  docker builder prune -f --filter until=336h >/dev/null 2>&1 || true    # build cache older than 14 days
}
refresh_updater() {   # the new version brings its own updater: install it (this run keeps its own copy in memory)
  if [ -f "$APP/pi/install-auto-update.sh" ] && bash -n "$APP/pi/pelletpi-update.sh" 2>/dev/null; then
    REPO_URL=$REPO_URL sh "$APP/pi/install-auto-update.sh" >>"$ULOG" 2>&1 || log "the updater itself was not refreshed"
  fi
}

on_error() {
  if [ "$BASHPID" != "$$" ]; then exit 1; fi      # inside $( ): the line that called it reports the error
  trap - ERR
  set +e
  log "stopped by an error (line $1)"
  if [ -f "$JOURNAL" ]; then restore; fi
  PHASE=idle; CODE=error; RESULT="stopped by an error (line $1), see update.log; still on $CURRENT"
  write_json
  exit 1
}
trap 'on_error $LINENO' ERR
wait_safe() {  # wait_safe LIMIT: until the burner is off or waiting (1 = gave up). Counts real seconds: one look
  local t0=$SECONDS   # can take long (a silent page is watched for DEAD_WAIT)
  until safe_now; do
    if [ $((SECONDS - t0)) -ge "$1" ]; then return 1; fi
    sleep "$WAIT_STEP"
    alive
  done
}
limit=$WAIT_PAGE
if [ "$MODE" = auto ]; then limit=$WAIT_AUTO; fi

# ---------------------------------------------------------------- 0) a run that was cut short (power cut)
if [ -f "$JOURNAL" ]; then
  KIND=$(jget KIND); FROM=$(jget FROM); TO=$(jget TO); PREV=$(jget PREV)
  OLD_IMAGE=$(jget OLD_IMAGE); IMAGE_NAME=$(jget IMAGE_NAME); SWAPPED=$(jget SWAPPED)
  if { [ "$KIND" != install ] && [ "$KIND" != back ]; } || [ -z "$OLD_IMAGE" ] || [ -z "$IMAGE_NAME" ]; then
    # written only half before the power went: keep what runs now, and the files of the version on record
    log "the journal of the last run is empty or broken: keeping the running program's image ($CURRENT)"
    OLD_IMAGE=$(running_image)
    IMAGE_NAME=$(docker inspect -f '{{.Config.Image}}' pelletburner 2>/dev/null || true)
    PREV=""
    if is_release "$CURRENT"; then PREV=$(G rev-parse -q --verify "refs/tags/$CURRENT^{commit}" 2>/dev/null || true); fi
    back_to_old
    rm -f "$JOURNAL"
    sync
  else
    log "the last run ($FROM -> $TO) was cut short: tidying up"
    now_image=$(running_image)
    look
    # the new version runs: another image, or the same image (only docker-compose.yml changed) after the swap;
    # and the files are still the new ones (an earlier tidy-up may have put the old files back already)
    new_runs=no
    if [ -n "$now_image" ] && { [ "$now_image" != "$OLD_IMAGE" ] \
         || { [ "$SWAPPED" = 1 ] && [ "$(image_id "$IMAGE_NAME")" = "$now_image" ]; }; }; then new_runs=yes; fi
    if [ "$KIND" = install ] && [ "$new_runs" = yes ] && [ "$P_H" = ok ] && is_release "$TO" \
       && [ "$(G rev-parse -q --verify HEAD 2>/dev/null || true)" = "$(G rev-parse -q --verify "refs/tags/$TO^{commit}" 2>/dev/null || echo x)" ]; then
      accept
      log "$TO was already running and answers: kept"
      refresh_updater
    elif [ "$now_image" = "$OLD_IMAGE" ] && [ "$SWAPPED" != 1 ]; then
      back_to_old                  # the old program runs: only its files and image name come back
      rm -f "$JOURNAL"
      sync
      log "kept $FROM"
    else
      # The other version runs (or nothing): files, image name and program go back together, at a safe time.
      # Until then nothing is changed, so a later run finds one whole version (and may still keep a good $TO).
      # After a swap the old image may run with the new docker-compose.yml: it is started again the same way.
      if [ "$(container_state)" = unknown ]; then
        finish tidy_wait "docker cannot tell what runs: a cut-short update is tidied up by a later run"
      fi
      phase waiting "tidying up after a cut-short update: $FROM comes back when the burner is off or waiting"
      if ! wait_safe "$limit"; then
        finish tidy_wait "a cut-short update is tidied up when the burner is off or waiting (now: $(burner_word))"
      fi
      back_to_old
      if ! dc up -d >>"$ULOG" 2>&1; then         # the journal stays: the next run tries again
        finish tidy_wait "could not start $FROM again; a later run tries again" 1
      fi
      rm -f "$JOURNAL"
      sync
      log "kept $FROM"
    fi
  fi
  FROM=""; TO=""; PREV=""; OLD_IMAGE=""; IMAGE_NAME=""; KIND=""; SWAPPED=""
fi
check_current
# the previous version is kept for KEEP_DAYS
if [ -f "$KEPT" ]; then
  at=$(kept_at)
  if [ -z "$at" ] || [ $(( $(date +%s) - at )) -gt $(( KEEP_DAYS * 86400 )) ]; then
    drop_kept
    log "the kept previous version is older than $KEEP_DAYS days: removed"
  fi
fi

# ---------------------------------------------------------------- go back to the previous version
if [ "$MODE" = back ]; then
  pv=$(kept VERSION)
  if [ -z "$pv" ] || ! is_version "$pv" || ! docker image inspect pelletpi-previous:latest >/dev/null 2>&1 \
     || ! G rev-parse -q --verify 'refs/tags/pelletpi-previous^{commit}' >/dev/null 2>&1; then
    finish no_previous "no previous version is kept; still on $CURRENT"
  fi
  KIND=back; FROM=$CURRENT; TO=$pv
  # the page shows this at once, and a second press is refused while it looks at the burner
  phase back "going back $FROM -> $TO: waits until the burner is off or waiting"
  if local_changes; then
    finish local_changes "local changes in the program folder; the update was not installed (still on $CURRENT)" 1
  fi
  if ! safe_now; then
    finish back_busy "going back needs the burner off or waiting (now: $(burner_word)); still on $CURRENT"
  fi
  PREV=$(G rev-parse HEAD)
  OLD_IMAGE=$(running_image)
  IMAGE_NAME=$(docker inspect -f '{{.Config.Image}}' pelletburner 2>/dev/null || true)
  if [ -z "$OLD_IMAGE" ] || [ -z "$IMAGE_NAME" ]; then
    finish error "the program's container was not found; nothing changed" 1
  fi
  look
  was=$P_CON
  keys=$(settings_keys)
  journal
  phase back "going back $FROM -> $TO"
  G checkout -q -f --detach 'refs/tags/pelletpi-previous^{commit}' >>"$ULOG" 2>&1
  docker tag pelletpi-previous:latest "$IMAGE_NAME" >>"$ULOG" 2>&1
  NEW_IMAGE=$(image_id "$IMAGE_NAME")
  own_files
  SWAPPED=1
  journal
  if ! dc up -d >>"$ULOG" 2>&1 || ! swapped "$NEW_IMAGE"; then
    log "docker did not start $TO: staying on $FROM"
    WHY=swap
    restore
    finish back_failed "the previous version $TO was not started ($WHY); still on $FROM" 1
  fi
  if health "$was" "$keys"; then
    set_version "$TO"
    if is_release "$FROM"; then touch "$STATE/bad-$FROM"; fi     # not installed again by itself
    rm -f "$JOURNAL"
    sync
    drop_kept
    docker image prune -f >/dev/null 2>&1 || true
    finish went_back "went back $FROM -> $TO"
  fi
  log "$TO did not pass the test ($WHY): staying on $FROM"
  restore
  finish back_failed "the previous version $TO did not start ($WHY); still on $FROM" 1
fi

# ---------------------------------------------------------------- 1) is there a newer release?
case "$REPO_URL" in
  "" | *CHANGE-ME*) finish no_repo "no REPO_URL set: updates are off" ;;
  https://*) ;;
  *) if [ "${PELLETPI_TEST:-}" != 1 ]; then finish no_repo "REPO_URL must start with https://: updates are off"; fi ;;
esac
PHASE=check; RESULT="checking $REPO_URL"; write_json
if ! tags=$(timeout 90 git ls-remote --tags --refs -- "$REPO_URL" 2>>"$ULOG"); then
  finish offline "could not reach $REPO_URL (no internet?); still on $CURRENT"
fi
LATEST=$(printf '%s\n' "$tags" | sed 's|.*refs/tags/||' | grep -E '^v[0-9]+(\.[0-9]+)*$' | sort -V | tail -n 1 || true)
if [ -z "$LATEST" ]; then finish no_release "no release in $REPO_URL yet; still on $CURRENT"; fi
# never go to an older version: compare the number part of the running one with the newest release.
# Only an exact release or pre-release name counts; v1.2.0-3-gabc (commits after a tag) or a dirty copy does not.
base=""
if is_release "$CURRENT" || is_pre "$CURRENT"; then base=$(printf '%s\n' "$CURRENT" | grep -Eo '^v[0-9]+(\.[0-9]+)*' || true); fi
if [ -n "$base" ]; then
  top=$(printf '%s\n%s\n' "$base" "$LATEST" | sort -V | tail -n 1)
  newer=no
  if [ "$top" = "$LATEST" ] && [ "$LATEST" != "$base" ]; then newer=yes; fi
  # a pre-release (v1.2.0-rc1) moves on to its release (v1.2.0)
  if [ "$LATEST" = "$base" ] && [ "$CURRENT" != "$base" ]; then newer=yes; fi
  if [ "$newer" != yes ]; then
    LATEST=$CURRENT
    finish uptodate "up to date ($CURRENT)"
  fi
else
  # Not a release (a copy cloned from a branch, v1.2.0-3-gabc, a local build): where it stands is not in its name.
  # The rule: never at night; on a press only when the folder's commit is in the release's history (older), or,
  # when that cannot be told (shallow clone), when the commit is older than the release's commit.
  # Same commit: it is that release. Newer, or as old: nothing is installed (no step back).
  if [ "$MODE" = auto ]; then
    finish unknown_base "running $CURRENT (not a release): $LATEST is not installed by itself; the button on the page installs it"
  fi
  where=older                    # no history at all (installed from a copy without .git): the press decides
  if [ -d "$APP/.git" ]; then
    # a full clone (a developer's) stays full: --depth only when it is shallow already
    depth=()
    if [ "$(G rev-parse --is-shallow-repository 2>/dev/null || true)" = true ]; then depth=(--depth 200); fi
    got=yes
    timeout 300 git -c safe.directory="$APP" -C "$APP" fetch -q ${depth[@]+"${depth[@]}"} -- "$REPO_URL" \
      "+refs/tags/$LATEST:refs/tags/$LATEST" >>"$ULOG" 2>&1 || got=no
    own_files                    # the fetch ran as root: hand the new objects back on every way out
    if [ "$got" != yes ]; then
      finish offline "could not reach $REPO_URL (no internet?); still on $CURRENT"
    fi
    h=$(G rev-parse HEAD); t=$(G rev-parse "refs/tags/$LATEST^{commit}")
    if [ "$h" = "$t" ]; then where=same
    elif G merge-base --is-ancestor "$h" "$t" 2>/dev/null; then where=older
    elif G merge-base --is-ancestor "$t" "$h" 2>/dev/null; then where=newer
    elif [ "$(G log -1 --format=%ct "$h")" -lt "$(G log -1 --format=%ct "$t")" ]; then where=older
    else where=newer
    fi
  fi
  case "$where" in
    same) set_version "$LATEST"; finish uptodate "up to date ($CURRENT)" ;;
    newer) was_cur=$CURRENT; LATEST=$CURRENT; finish uptodate "running $was_cur, which is not older than the newest release; nothing installed" ;;
  esac
  log "running $CURRENT (not a release), older than $LATEST: installing it, as asked on the page"
fi
if [ "$MODE" = retry ]; then rm -f "$STATE/bad-$LATEST" "$STATE/buildfail-$LATEST"; fi
if [ -e "$STATE/bad-$LATEST" ]; then finish bad_skipped "update $LATEST available, but it failed here before; skipped"; fi
fails=0
if [ -f "$STATE/buildfail-$LATEST" ]; then fails=$(tr -cd '0-9' < "$STATE/buildfail-$LATEST" || true); fi
fails=${fails:-0}
if [ "$fails" -ge "$BUILD_TRIES" ]; then
  finish bad_skipped "update $LATEST available, but its build failed $fails times here; skipped until Try again"
fi
if [ "$MODE" = auto ] && [ "$AUTO_UPDATE" != yes ]; then finish available "update $LATEST available (auto-update is off)"; fi
if [ "$MODE" = auto ] && ! safe_now; then
  finish later "update $LATEST available; waits until the burner is off or waiting (now: $(burner_word))"
fi

# ---------------------------------------------------------------- 2) build the new version next to the running one
KIND=install; FROM=$CURRENT; TO=$LATEST
free=""
if [ "$MIN_FREE_KB" -gt 0 ]; then free=$(df -Pk "$APP" 2>/dev/null | awk 'NR==2 {print $4}' | tr -cd '0-9' || true); fi
if [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_KB" ]; then
  finish disk_full "not enough free space for $TO ($free kB free); still on $CURRENT" 1
fi
if local_changes; then
  finish local_changes "local changes in the program folder; the update was not installed (still on $CURRENT)" 1
fi
phase download "updating $FROM -> $TO: downloading and building"
shallow=""
if [ ! -d "$APP/.git" ]; then            # installed from the release tarball: start a local history
  shallow=1
  # data and .env are never part of a version, whatever .gitignore says; a half-made history is removed again
  if ! { G init -q && G add -A && G rm -r -q --cached --ignore-unmatch -- data .env \
         && G -c user.name=pelletpi -c user.email=pelletpi@localhost commit -q -m "installed version $CURRENT"; } >>"$ULOG" 2>&1; then
    rm -rf "$APP/.git"
    finish error "could not record the installed files (disk full?); nothing changed" 1
  fi
fi
# a developer's full clone stays full, and keeps its remotes and their branches
if [ "$(G rev-parse --is-shallow-repository 2>/dev/null || true)" = true ]; then shallow=1; fi
if G remote get-url origin >/dev/null 2>&1; then G remote set-url origin "$REPO_URL"; else G remote add origin "$REPO_URL"; fi
depth=()
if [ -n "$shallow" ]; then depth=(--depth 1); fi
PREV=$(G rev-parse HEAD)
OLD_IMAGE=$(running_image)
IMAGE_NAME=$(docker inspect -f '{{.Config.Image}}' pelletburner 2>/dev/null || true)
if [ -z "$OLD_IMAGE" ] || [ -z "$IMAGE_NAME" ]; then
  finish error "the program's container was not found; nothing changed" 1
fi
journal
if ! timeout 900 git -c safe.directory="$APP" -C "$APP" fetch -q ${depth[@]+"${depth[@]}"} origin "+refs/tags/$TO:refs/tags/$TO" >>"$ULOG" 2>&1; then
  restore
  finish build_failed "could not download $TO (internet lost?); still on $CURRENT" 1
fi
if ! G checkout -q -f --detach "refs/tags/$TO" >>"$ULOG" 2>&1; then
  restore
  finish build_failed "could not unpack $TO (disk full?); still on $CURRENT" 1
fi
own_files
brc=0
(cd "$APP" && timeout "$BUILD_TIMEOUT" "${DC[@]}" build) >>"$ULOG" 2>&1 || brc=$?
if [ "$brc" != 0 ]; then
  if [ "$brc" = 124 ]; then log "the build took longer than $BUILD_TIMEOUT s: stopped"; fi
  echo $((fails + 1)) > "$STATE/buildfail-$TO"
  restore
  finish build_failed "build of $TO failed (internet lost or disk full?, $((fails + 1)) of $BUILD_TRIES); still on $CURRENT" 1
fi
NEW_IMAGE=$(image_id "$IMAGE_NAME")

# ---------------------------------------------------------------- 3) swap only while the burner is off or waiting
t0=$SECONDS
until safe_now; do
  if [ "$PHASE" != waiting ]; then
    phase waiting "$TO is built; waits until the burner is off or waiting (now: $(burner_word))"
  fi
  if [ $((SECONDS - t0)) -ge "$limit" ]; then
    restore
    finish busy "update $TO built, but the burner stayed busy; still on $CURRENT"
  fi
  sleep "$WAIT_STEP"
  alive
done
look
was=$P_CON
keys=$(settings_keys)
phase install "burner $(burner_word): swapping to $TO and testing it"
NOBAD=""                          # set when docker could not tell: then the release is not marked as failed
SWAPPED=1
journal
if ! dc up -d >>"$ULOG" 2>&1; then
  WHY=swap                        # docker did not start the new image: never report "updated"
  if [ "$(container_state)" = unknown ]; then NOBAD=1; fi
else
  sw=0
  swapped "$NEW_IMAGE" || sw=$?
  if [ "$sw" != 0 ]; then WHY=swap; fi
  if [ "$sw" = 2 ]; then NOBAD=1; fi
fi

# ---------------------------------------------------------------- 4) test the new version, or go back
if [ "$WHY" = swap ] || ! health "$was" "$keys"; then
  log "$TO did not pass the test ($WHY): going back to $FROM"
  if [ -z "$NOBAD" ]; then touch "$STATE/bad-$TO"; else log "docker could not tell what runs: $TO is not marked as failed"; fi
  why=$WHY
  restore
  P_H=down
  for i in $(seq 1 "$START_TRIES"); do
    sleep "$STEP"
    look
    if [ "$P_H" = ok ]; then break; fi
  done
  if [ "$P_H" != ok ]; then log "WARNING: $FROM does not answer either; look at: docker compose logs"; fi
  WHY=$why
  finish rolled_back "update to $TO failed ($WHY) and was undone; still on $CURRENT" 1
fi
accept
refresh_updater
finish updated "updated $FROM -> $TO"
