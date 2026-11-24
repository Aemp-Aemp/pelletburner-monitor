#!/usr/bin/env python3
"""Higher-level features for burner_responder.py (stdlib only):

  Settings    - web-editable settings, saved as data/settings.json
  Scheduler   - start windows: sends run_mode=start when the boiler is idle inside an enabled window,
                with retries, minimum off-time, max starts/day, manual-stop respect, short-run warnings
  LuxMonitor  - flame-sensor (LUX) fouling: steady-state DRIFT samples, a 4-week *running-time* baseline
                per power band, drop / slow-decline (soot) warnings, reset after service
  Optimiser   - daily summaries (fuel/day, starts, run length, time per state, alarms) over the last
                28 days the boiler actually ran, plus rule-based suggestions

States used here: 0 STOPPET, 1 WAITING, 2 OPSTART, 3 IGNITION, 4 DRIFT, 5 PAUSESTOP, 6 ALARM, 7 REDDER BAL.
"""
import json, os, re, threading, time, datetime, glob, statistics, copy, collections, math, secrets, urllib.parse
from burner_ext import source_of
# the shared tables and helpers (one copy: burner_common.py); the old short names stay usable from this module
from burner_common import (STATE_LABELS, STATES_ACTIVE as RUNNING, ALARM_NEVER_AUTO_RESET, FLAME_ON,
                           iso as _iso, to_int as _int, to_float as _f, hhmm_to_min, read_jsonl, rows_of,
                           append_jsonl, write_json_atomic, read_json, hist_files,
                           write_text_atomic, finite_num as _num)

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class _Bad(ValueError):
    """A wrong value in a settings update: its text is the plain answer to the web page."""


def _bool_in(v, name):
    """A switch from the web: only a real true / false (the string "false" is not off-by-accident on)."""
    if isinstance(v, bool):
        return v
    raise _Bad(f"{name} must be true or false")


def _list_in(v, name):
    """A list from the web (None = empty). A string or an object is refused, not taken apart."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    raise _Bad(f"{name} must be a list")


def _no_nonfinite(v):
    """A copy of a JSON value with Infinity and NaN left out (None in a list), so they never reach the pages."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _no_nonfinite(x) for k, x in v.items() if not (isinstance(x, float) and not math.isfinite(x))}
    if isinstance(v, list):
        return [_no_nonfinite(x) for x in v]
    return v


def _day_in(x):
    """A week day 0..6 from a list item (0 or "0"), else None. Not a bool, not "²" (isdigit but not a number)."""
    if isinstance(x, bool):
        return None
    v = _int(x)
    return v if v in range(7) else None


# ============================================================================ settings
# Phone alerts (ntfy): every event the monitor can send, and whether it is on by default.
NTFY_EVENTS = (("alarm", True), ("alarm_clear", True), ("link_lost", True), ("link_back", True),
               ("silo_low", True), ("ash", True), ("pi", True), ("net", True), ("burner_start", False), ("burner_stop", False),
               ("monitor_start", False), ("daily", False))
NTFY_TOPIC_PREFIX = "burner-"
_NTFY_TOPIC = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def new_ntfy_topic():
    """A topic nobody can guess: "burner-" + 24 random letters/digits (144 bits from `secrets`)."""
    return NTFY_TOPIC_PREFIX + secrets.token_urlsafe(18)


NTFY_CMD_PREFIX = "burner-cmd-"
_PIN = re.compile(r"^\d{4,6}$")
WEAK_PINS = {"1111", "1234", "0000", "2580", "1004", "6969", "4321", "123123", "112233", "159753"}


def weak_pin(pin):
    """True for codes that are easy to guess: one digit repeated, a straight run up or down, a repeated pair,
    or a well-known one."""
    p = str(pin)
    steps = {int(b) - int(a) for a, b in zip(p, p[1:])}
    return (p in WEAK_PINS or len(set(p)) == 1 or steps in ({1}, {-1})
            or (len(p) % 2 == 0 and p == p[:2] * (len(p) // 2)))
_PIN_HASH = re.compile(r"^pbkdf2\$\d{5,7}\$[0-9a-f]{32}\$[0-9a-f]{64}$")
PIN_ROUNDS = 200_000


def new_ntfy_cmd_topic():
    """The secret topic the phone sends start / stop to: "burner-cmd-" + 32 random characters (192 bits)."""
    return NTFY_CMD_PREFIX + secrets.token_urlsafe(24)


def hash_pin(pin, salt=None, rounds=PIN_ROUNDS):
    """Only a salted hash of the phone PIN is kept: pbkdf2$rounds$salt$hash."""
    import hashlib
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", str(pin).encode(), bytes.fromhex(salt), rounds).hex()
    return f"pbkdf2${rounds}${salt}${h}"


def check_pin(stored, pin):
    """True when `pin` matches the stored hash (constant-time compare)."""
    import hmac
    if not _PIN_HASH.match(str(stored or "")) or not _PIN.match(str(pin or "")):
        return False
    _, rounds, salt, _h = stored.split("$")
    return hmac.compare_digest(hash_pin(pin, salt, int(rounds)), stored)


def check_ntfy_server(url):
    """(ok, clean url or message): an http(s) address without user, password, query or #part."""
    u = str(url or "").strip().rstrip("/")
    if not u or len(u) > 200 or any(c.isspace() for c in u):
        return False, "server must be an http:// or https:// address"
    try:
        p = urllib.parse.urlsplit(u)
        p.port                                   # raises on a bad port
    except ValueError:
        return False, "server must be an http:// or https:// address"
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment:
        return False, "server must be an http:// or https:// address"
    return True, u


COOL_SAME_STOP_S = 900      # two cooling entries whose stops are this close are the same cool-down


def cool_stop(x):
    """When the burner stopped for this cooling entry (epoch): the saved 'stop', or for older entries end time - hours."""
    s = x.get("stop")
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return s
    e, h = x.get("epoch"), x.get("h")
    if isinstance(e, (int, float)) and isinstance(h, (int, float)) and not isinstance(e, bool) and not isinstance(h, bool):
        return e - h * 3600
    return None


def cool_learn_dedup(ls):
    """One cooling entry per stop. A restart used to measure the same cool-down again and add it once more; of those
    the latest (longest) one is kept, in the place of the first. Entries without a known stop are kept as they are."""
    out = []
    for x in ls:
        s = cool_stop(x)
        i = next((i for i, y in enumerate(out) if s is not None and cool_stop(y) is not None
                  and abs(cool_stop(y) - s) <= COOL_SAME_STOP_S), None)
        if i is None:
            out.append(x)
        elif (x.get("epoch") or 0) >= (out[i].get("epoch") or 0):
            out[i] = x
    return out


DEFAULTS = {
    # LUX = status field 004: the boiler's flame limits (17 in ignition, 3 / 50 / 129 in DRIFT) are on this scale.
    # Field 003 is another analog value.
    "flame_field": "004",
    "schedule": {
        "enabled": False,
        "override_waiting": False,          # end a minimum-drift WAITING pause (stop, then start)
        "min_off_min": 30,                  # hearth / cycling protection: min. minutes stopped before an auto-start
        "max_starts_day": 6,                # all starts that day, incl. the boiler's own restarts
        "retry_wait_s": 90,                 # time for the state to change after a start command
        "max_attempts": 3,                  # warning after 2 failed attempts, give up after 3
        "short_run_min": 30,                # a run shorter than this gives a "short run" warning
        "windows": [],                      # [{"on": true, "days": [0..6], "start": "05:00", "end": "07:00"}]
    },
    "lux": {"reset_epoch": 0, "drop_warn_pct": 10, "trend_warn_pct": 15, "low_abs": 40},
    "optimiser": {"reset_epoch": 0},
    "fan": {"reset_epoch": 0},
    # batches: [{"name": "...", "since": epoch}] - the last one is in use.
    # stock: pellets at home (store + silo), {"kg": counted/known amount, "at": epoch}; what burned since "at" is
    # subtracted. reserve_kg: "buy before" is the day the stock reaches this.
    "pellets": {"batches": [], "stock": None, "reserve_kg": 150, "corrections": []},
    "ash": {"emptied_at": None, "ash_pct": 0.7, "bin_kg": 0, "learn": []},
    "ignitions": {"since": 0},
    "health": {"cleaned_at": None},
    # outside temperature (optional): Open-Meteo, free, no key. Only the place's rounded coordinates are sent.
    "weather": {"enabled": False, "name": "", "cc": "", "lat": None, "lon": None},
    # phone alerts (optional, off): push messages through ntfy. The topic works like a password - anyone who knows
    # it can read the alerts. It is made once (new_ntfy_topic) and kept here; "new_topic" makes a new one.
    # lang = language of the messages (en / da; empty = the first-start guide's language).
    # Start / stop from the phone (opt-in, off): the phone sends "start" / "stop" to cmd_topic (a second secret
    # topic), the monitor answers with a one-time code, the phone replies "<code> <PIN>". A salted hash of
    # the PIN is used for checking (cmd_pin_hash, never sent to the page); the PIN itself (cmd_pin) only goes to the
    # owner's page through /api/settings (Settings.public()), never to logs, MQTT or ntfy. burner_phone.py does the rest.
    "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": "", "lang": "",
             "events": {k: on for k, on in NTFY_EVENTS}, "silo_low_kg": 50, "daily_at": "20:00",
             "graph": True,           # today's graph (PNG, burner_graph.py) with the "status" answer
             "cmd_enabled": False, "cmd_topic": "", "cmd_pin_hash": "", "cmd_pin": ""},
    # what the board does not send, typed in once by the owner for the "Picture for service" (never sent to the
    # boiler): software version (the board reports 0), MIN YDELSE %, fan rpm at 100 % (SUGER INDSTILLING)
    "controller_info": {"asked": False, "sw": "", "min_pct": None, "fan100_rpm": None},
    # first-start guide on the web page: done = never show it by itself again (also set by "Skip"). lang = the
    # language picked in the guide (default for devices that have not chosen one); boiler_kw = 10/15/25 kW model;
    # store_kg = what the pellet store / silo holds. Only for the page: nothing here is ever sent to the boiler.
    "setup": {"done": False, "at": None, "lang": "", "boiler_kw": None, "store_kg": None},               # burner health: the normal is learned again after a cleaning                      # ignition history shown from this time (hide bench tests)
    # automatic alarm reset: clear + restart like the owner does by hand (opt-in)
    "auto_reset": {"enabled": False, "alarms": [4, 5, 6], "max_per_day": 2, "wait_min": 10},
    "auto_reset_stats": {"saves": 0, "last_save": None, "attempts": [], "last_attempt": None},
    # hot-water tank (VVB) temperature from a sensor of your own, reported to the boiler as if the IO box measured
    # it, so VVB PRIORITET can work. Sources: "mqtt" (burner/cmd/vvb, e.g. from Home Assistant), "http"
    # (POST /api/vvb from a local script), "onewire" (DS18B20 on a Raspberry Pi). No fresh reading for
    # max_age_min -> report safe_c (tank "warm") so a dead sensor can never call for heat.
    "vvb": {"enabled": False, "source": "mqtt", "onewire_id": "", "max_age_min": 15, "safe_c": 75},
    # warm-period mode: the web does the minimum mode. In RUNNING at low power it STOPS the burner
    # (no 20-min burn-out / re-ignition loop), lets the house cool, and STARTS again at the boost times (always, also
    # when the boiler is still warm) or when the pause has run out. use_board = take STOP AT / PAUSE / BOOST 1+2 from
    # the boiler's own minimum-mode settings (cfg 005/006/007/008), so they carry over.
    "warm": {"enabled": False, "use_board": True, "stop_pct": 40, "stop_after_min": 2, "pause_h": 6,
             "boosts": ["05:00", "17:00"], "start_window_min": 30, "max_attempts": 3, "retry_wait_s": 90,
             "periods": [], "keep_restart_c": 5, "keep_min_off_min": 15,
             "min_boiler_c": 0, "warm_k": 1.8, "warm_learn": [], "cool_learn": [],
             # warm-up in two speeds: fast until the pump starts (heat stays in the boiler), slow after (house takes it).
             # Defaults measured on a real burner: 25 -> 50 °C 0.8 min/°C, 50 -> 60 °C 1.5 min/°C. pump_c = PUMP START TEMPERATURE.
             "pump_c": 50, "warm_k1": 0.8, "warm_k2": 1.5},
    # the boiler's own minimum mode (MINIMUMDRIFT AKTIV) is not among the settings the board sends: it is seen from
    # what the boiler does (DRIFT -> WAITING by itself). last = last such burn-out, off_at = the owner said "I turned it
    # off" (page only; nothing is ever sent to the boiler). Not editable through /api/settings.
    "minmode": {"last": None, "off_at": None},
    # heating plan: the period window already served ("YYYY-MM-DD#HH:MM"), so a program restart never starts it again.
    # Not editable through /api/settings.
    "warmstate": {"done_window": None, "at": None},
    # the boiler's own type setting (cfg 033) changed: the boiler lights differently, so the "usual" lighting
    # times start again from `since` (old starts are kept). kedel = the last 033 value seen. Not editable on the web.
    "baseline": {"since": None, "why": "", "kedel": None},
}
VVB_SOURCES = ("mqtt", "http", "onewire")
STOCK_MAX = 30000                                    # kg; a sanity limit for "pellets at home"
AUTO_RESET_NEVER = set(ALARM_NEVER_AUTO_RESET)   # overheat, burn-back/thermo, sensors, fuse, calibration, IO
MAX_WINDOWS = 24


class Settings:
    def __init__(self, data_dir, log):
        self.path = os.path.join(data_dir, "settings.json") if data_dir else None
        self.log = log
        self.lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.listeners = []
        if self.path and (os.path.exists(self.path) or os.path.exists(self.path + ".bak")):
            # broken after a power cut (empty, NUL bytes, cut short): the previous good file settings.json.bak
            obj = read_json(self.path, None, log=lambda m: log("# settings:" + m[1:]))
            if not isinstance(obj, dict):
                if obj is not None:
                    log(f"# settings: {self.path} is not a JSON object; using defaults")
                obj = {}
            obj = _no_nonfinite(obj)                    # Infinity / NaN from an old or hand-edited file: dropped
            # the phone PIN's hash is never accepted from the web: carry it over from the file before the checks
            ph = (obj.get("ntfy") or {}).get("cmd_pin_hash") if isinstance(obj.get("ntfy"), dict) else None
            if isinstance(ph, str) and _PIN_HASH.match(ph):
                self.data["ntfy"]["cmd_pin_hash"] = ph
                # the PIN itself is kept too, so the owner's page can show it (only /api/settings, never sent out)
                pn = obj["ntfy"].get("cmd_pin")
                if isinstance(pn, str) and _PIN.match(pn) and check_pin(ph, pn):
                    self.data["ntfy"]["cmd_pin"] = pn
            if isinstance(obj.get("ntfy"), dict):           # not hashed again on every start
                obj["ntfy"] = {x: y for x, y in obj["ntfy"].items() if x != "cmd_pin"}
            # a hand-edited file goes through the same validation as the web, section by section,
            # so one bad value can't break the scheduler or the status feed
            for k in ("flame_field", "schedule", "lux", "auto_reset", "vvb", "warm", "ash", "weather", "controller_info", "setup",
                      "ntfy"):
                if k in obj:
                    ok, msg = self.update({k: {x: y for x, y in obj[k].items() if x != "emptied"}} if isinstance(obj[k], dict) and k == "ash" else {k: obj[k]}, save=False)
                    if not ok:
                        log(f"# settings: ignored '{k}' from {self.path}: {msg}")
            # learned / measured values are not editable from the web, so update() skips them: carry them over here
            cl = (obj.get("warm") or {}).get("cool_learn") if isinstance(obj.get("warm"), dict) else None
            if isinstance(cl, list):
                self.data["warm"]["cool_learn"] = cool_learn_dedup([x for x in cl if isinstance(x, dict) and x.get("tau_h")])[-10:]
            wl = (obj.get("warm") or {}).get("warm_learn") if isinstance(obj.get("warm"), dict) else None
            if isinstance(wl, list):
                self.data["warm"]["warm_learn"] = [x for x in wl if isinstance(x, dict)][-10:]
            al = (obj.get("ash") or {}).get("learn") if isinstance(obj.get("ash"), dict) else None
            if isinstance(al, list):
                self.data["ash"]["learn"] = [x for x in al if isinstance(x, dict) and x.get("pct")][-8:]
            ea = (obj.get("ash") or {}).get("emptied_at") if isinstance(obj.get("ash"), dict) else None
            if isinstance(ea, (int, float)):
                self.data["ash"]["emptied_at"] = ea
            pc = (obj.get("pellets") or {}).get("corrections") if isinstance(obj.get("pellets"), dict) else None
            if isinstance(pc, list):
                self.data["pellets"]["corrections"] = [x for x in pc if isinstance(x, dict) and x.get("f")][-10:]
            for sect in ("lux", "optimiser", "fan"):
                v = (obj.get(sect) or {}).get("reset_epoch") if isinstance(obj.get(sect), dict) else None
                if isinstance(v, (int, float)):
                    self.data[sect]["reset_epoch"] = v
            hc = (obj.get("health") or {}).get("cleaned_at") if isinstance(obj.get("health"), dict) else None
            if isinstance(hc, (int, float)):
                self.data["health"]["cleaned_at"] = hc
            mm = obj.get("minmode")
            if isinstance(mm, dict):
                for k in ("last", "off_at"):
                    if isinstance(mm.get(k), (int, float)) and not isinstance(mm.get(k), bool):
                        self.data["minmode"][k] = mm[k]
            ws = obj.get("warmstate")
            if isinstance(ws, dict) and isinstance(ws.get("done_window"), str) and len(ws["done_window"]) <= 40:
                self.data["warmstate"] = {"done_window": ws["done_window"],
                                          "at": ws["at"] if isinstance(ws.get("at"), (int, float)) else None}
            bl = obj.get("baseline")
            if isinstance(bl, dict):
                for k in ("since", "kedel"):
                    if isinstance(bl.get(k), (int, float)) and not isinstance(bl.get(k), bool):
                        self.data["baseline"][k] = bl[k]
                if isinstance(bl.get("why"), str):
                    self.data["baseline"]["why"] = bl["why"][:60]
            ig = obj.get("ignitions")
            if isinstance(ig, dict) and isinstance(ig.get("since"), (int, float)):
                self.data["ignitions"]["since"] = ig["since"]
            pl = obj.get("pellets")
            if isinstance(pl, dict) and isinstance(pl.get("batches"), list):
                self.data["pellets"]["batches"] = [b for b in pl["batches"] if isinstance(b, dict) and b.get("name")][-50:]
            if isinstance(pl, dict) and isinstance(pl.get("stock"), dict):
                k, a = _f(pl["stock"].get("kg")), _f(pl["stock"].get("at"))
                if k is not None and a and 0 <= k <= STOCK_MAX:
                    self.data["pellets"]["stock"] = {"kg": k, "at": a}
            if isinstance(pl, dict) and _int(pl.get("reserve_kg")) is not None and 0 <= _int(pl["reserve_kg"]) <= 2000:
                self.data["pellets"]["reserve_kg"] = _int(pl["reserve_kg"])
            st = obj.get("auto_reset_stats")
            if isinstance(st, dict):
                d = self.data["auto_reset_stats"]
                if isinstance(st.get("saves"), int):
                    d["saves"] = st["saves"]
                if isinstance(st.get("last_save"), dict):
                    d["last_save"] = st["last_save"]
                if isinstance(st.get("last_attempt"), dict):
                    d["last_attempt"] = st["last_attempt"]
                if isinstance(st.get("attempts"), list):
                    d["attempts"] = [x for x in st["attempts"] if isinstance(x, (int, float))][-20:]
        if self.data["schedule"].get("enabled"):       # the old start windows have no page any more: turned off once
            self.data["schedule"]["enabled"] = False
            log("# settings: the old start windows were on; turned off (the heating plan on /home starts the boiler now)")
            self.save()
        if not self.data["ntfy"].get("topic"):         # phone alerts: the secret topic is made once and kept
            self.data["ntfy"]["topic"] = new_ntfy_topic()
            self.save()
        if not self.data["ntfy"].get("cmd_topic"):     # start / stop from the phone: its own secret topic
            self.data["ntfy"]["cmd_topic"] = new_ntfy_cmd_topic()
            self.save()

    @staticmethod
    def _merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                Settings._merge(dst[k], v)
            else:
                dst[k] = v

    def get(self, key, default=None):
        with self.lock:
            return copy.deepcopy(self.data.get(key, default))

    def all(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def public(self):
        """The settings for the owner's web page: as all(), but the phone PIN's hash is only "set" or not. The PIN
        itself is shown there (anyone on this page can start the boiler anyway); it is never logged or sent out."""
        d = self.all()
        nt = d.get("ntfy") or {}
        nt["cmd_pin_set"] = bool(nt.pop("cmd_pin_hash", ""))
        nt["cmd_pin_easy"] = bool(nt.get("cmd_pin")) and weak_pin(nt["cmd_pin"])
        return d

    SAVE_FAILED = "could not save - the monitor's storage may be full or read-only"

    def save(self):
        """Write settings.json. True when it is on disk (or there is no file to keep), False when it could not be
        written (read-only or full storage): the caller then keeps / restores the old values."""
        if not self.path:
            return True
        try:
            # allow_nan=False: never Infinity / NaN, which is not strict JSON
            text = json.dumps(self.data, indent=1, allow_nan=False)
            try:                     # unchanged: no write (spares the SD card, and .bak keeps the real previous version)
                with open(self.path, encoding="utf-8") as fh:
                    if json.load(fh) == json.loads(text):          # same values (0 and 0.0 count as the same)
                        return True
            except (OSError, ValueError):
                pass
            write_text_atomic(self.path, text, backup=True)     # never half a file (previous = .bak)
            return True
        except (OSError, ValueError) as e:
            self.log(f"# settings: save failed ({e})")
            try:
                os.remove(self.path + ".tmp")                   # a half-written temp file on a full disk
            except OSError:
                pass
            return False

    def update(self, patch, save=True):
        """Validate and apply a partial update from the web. Returns (ok, message)."""
        if not isinstance(patch, dict):
            return False, "expected a JSON object"
        for k, v in patch.items():                       # every section but flame_field is an object
            if k != "flame_field" and k in DEFAULTS and not isinstance(v, dict):
                return False, f"{k} must be an object"
        try:
            new = copy.deepcopy(self.data)
            if "flame_field" in patch:
                ff = patch["flame_field"]
                if ff not in (None, "", "003", "004"):
                    return False, "flame_field must be 003, 004 or empty"
                new["flame_field"] = ff or None
            if "schedule" in patch:
                s = patch["schedule"]
                d = new["schedule"]
                for k in ("enabled", "override_waiting"):
                    if k in s:
                        d[k] = _bool_in(s[k], k)
                for k, lo, hi in (("min_off_min", 0, 24 * 60), ("max_starts_day", 1, 48),
                                  ("retry_wait_s", 30, 900), ("max_attempts", 1, 10), ("short_run_min", 0, 600)):
                    if k in s:
                        v = _int(s[k])
                        if v is None or not lo <= v <= hi:
                            return False, f"{k} must be {lo}..{hi}"
                        d[k] = v
                if "windows" in s:
                    if not isinstance(s["windows"], list) or len(s["windows"]) > MAX_WINDOWS:
                        return False, f"at most {MAX_WINDOWS} windows"
                    wins = []
                    for w in s["windows"]:
                        if not isinstance(w, dict):
                            return False, "each window must be an object"
                        if not (_HHMM.match(str(w.get("start", ""))) and _HHMM.match(str(w.get("end", "")))):
                            return False, "window times must be HH:MM"
                        if w["start"] == w["end"]:
                            return False, "a window needs different start and end"
                        days = sorted({_day_in(x) for x in _list_in(w.get("days"), "days")} - {None})
                        if not days:
                            return False, "each window needs at least one day"
                        wins.append({"on": _bool_in(w.get("on", True), "on"), "days": days,
                                     "start": w["start"].zfill(5), "end": w["end"].zfill(5)})
                    d["windows"] = wins
            if "auto_reset" in patch:
                s, d = patch["auto_reset"], new["auto_reset"]
                if "enabled" in s:
                    d["enabled"] = _bool_in(s["enabled"], "enabled")
                if "alarms" in s:
                    al = sorted({None if isinstance(x, bool) else _int(x) for x in _list_in(s["alarms"], "alarms")} - {None})
                    if any(x in AUTO_RESET_NEVER or not 1 <= x <= 15 for x in al):
                        return False, "these alarms can never be reset automatically: 1-3, 7-10, 13-15"
                    d["alarms"] = al
                for k, lo, hi in (("max_per_day", 1, 2), ("wait_min", 1, 240)):
                    if k in s:
                        v = _int(s[k])
                        if v is None or not lo <= v <= hi:
                            return False, f"{k} must be {lo}..{hi}"
                        d[k] = v
            if "vvb" in patch:
                s, d = patch["vvb"], new["vvb"]
                if "enabled" in s:
                    d["enabled"] = _bool_in(s["enabled"], "enabled")
                if "source" in s:
                    if s["source"] not in VVB_SOURCES:
                        return False, "vvb source must be mqtt, http or onewire"
                    d["source"] = s["source"]
                if "onewire_id" in s:
                    oid = str(s["onewire_id"] or "").strip()
                    if oid and not re.match(r"^28-[0-9a-f]{12}$", oid):
                        return False, "onewire_id looks like 28-0123456789ab (or empty = first sensor found)"
                    d["onewire_id"] = oid
                for k, lo, hi in (("max_age_min", 1, 120), ("safe_c", 40, 95)):
                    if k in s:
                        v = _int(s[k])
                        if v is None or not lo <= v <= hi:
                            return False, f"{k} must be {lo}..{hi}"
                        d[k] = v
            if "ash" in patch:
                s, d = patch["ash"], new.setdefault("ash", copy.deepcopy(DEFAULTS["ash"]))
                if "emptied" in s and _bool_in(s["emptied"], "emptied"):
                    d["emptied_at"] = time.time()
                for k, lo, hi in (("bin_kg", 0, 200),):
                    if k in s:
                        v = _f(s[k])
                        if v is None or not lo <= v <= hi:
                            return False, f"{k} must be {lo}..{hi}"
                        d[k] = round(v, 1)
                if "ash_pct" in s:
                    v = _f(s["ash_pct"])
                    if v is None or not 0.1 <= v <= 5:
                        return False, "ash_pct must be 0.1..5 %"
                    d["ash_pct"] = round(v, 2)
            if "weather" in patch:
                s, d = patch["weather"], new.setdefault("weather", copy.deepcopy(DEFAULTS["weather"]))
                if not isinstance(s, dict):
                    return False, "weather must be an object"
                if "enabled" in s:
                    d["enabled"] = _bool_in(s["enabled"], "enabled")
                if "name" in s:
                    d["name"] = str(s["name"] or "")[:80]
                if "cc" in s:
                    cc = str(s["cc"] or "").upper()
                    if cc not in ("", "DK", "DE"):
                        return False, "country must be DK or DE"
                    d["cc"] = cc
                for k, lo, hi in (("lat", -90, 90), ("lon", -180, 180)):
                    if k in s:
                        if s[k] in (None, ""):
                            d[k] = None
                            continue
                        v = _f(s[k])
                        if v is None or not lo <= v <= hi:
                            return False, f"{k} must be {lo}..{hi}"
                        d[k] = round(v, 2)               # about 1 km: enough for weather, no exact address
                if d["enabled"] and (d.get("lat") is None or d.get("lon") is None):
                    return False, "pick a place first"
            if "ntfy" in patch:
                s, d = patch["ntfy"], new.setdefault("ntfy", copy.deepcopy(DEFAULTS["ntfy"]))
                if not isinstance(s, dict):
                    return False, "ntfy must be an object"
                if "enabled" in s:
                    d["enabled"] = _bool_in(s["enabled"], "enabled")
                if "server" in s:
                    ok, u = check_ntfy_server(s["server"])
                    if not ok:
                        return False, u
                    d["server"] = u
                if "new_topic" in s and _bool_in(s["new_topic"], "new_topic"):
                    d["topic"] = new_ntfy_topic()
                elif "topic" in s:
                    tp = str(s["topic"] or "").strip()
                    if not _NTFY_TOPIC.match(tp):
                        return False, "topic must be 20-64 letters, digits, - or _"    # never echo the topic
                    d["topic"] = tp
                if "lang" in s:
                    lg = str(s["lang"] or "")
                    if lg not in ("", "en", "da"):
                        return False, "language must be en or da"
                    d["lang"] = lg
                if "events" in s:
                    ev = s["events"]
                    if not isinstance(ev, dict):
                        return False, "events must be an object"
                    known = dict(NTFY_EVENTS)
                    for k, v in ev.items():
                        if k not in known:
                            return False, f"unknown event {str(k)[:30]}"
                        d.setdefault("events", {})[k] = _bool_in(v, "an event switch")
                if "silo_low_kg" in s:
                    v = _int(s["silo_low_kg"])
                    if v is None or not 1 <= v <= 5000:
                        return False, "silo_low_kg must be 1..5000"
                    d["silo_low_kg"] = v
                if "daily_at" in s:
                    if not _HHMM.match(str(s["daily_at"] or "")):
                        return False, "daily_at must be HH:MM"
                    d["daily_at"] = str(s["daily_at"]).zfill(5)
                if "graph" in s:
                    d["graph"] = bool(s["graph"])
                for k, on in NTFY_EVENTS:                # a settings file from before a new event: its default
                    d.setdefault("events", {}).setdefault(k, on)
                if d["enabled"] and not d.get("topic"):
                    return False, "make a topic first"
                # start / stop from the phone. Never echo a PIN or a topic in a message.
                if "new_cmd_topic" in s and _bool_in(s["new_cmd_topic"], "new_cmd_topic"):
                    d["cmd_topic"] = new_ntfy_cmd_topic()
                elif "cmd_topic" in s:
                    tp = str(s["cmd_topic"] or "").strip()
                    if not _NTFY_TOPIC.match(tp):
                        return False, "command topic must be 20-64 letters, digits, - or _"
                    d["cmd_topic"] = tp
                if "cmd_pin" in s:
                    pin = s["cmd_pin"]
                    if pin in (None, ""):
                        d["cmd_pin_hash"], d["cmd_pin"], d["cmd_enabled"] = "", "", False
                    elif not isinstance(pin, str) or not _PIN.match(pin):
                        return False, "PIN: use 4-6 digits only"
                    else:                                # an easy PIN is allowed; the page warns (cmd_pin_easy)
                        d["cmd_pin_hash"], d["cmd_pin"] = hash_pin(pin), pin
                if "cmd_enabled" in s:
                    d["cmd_enabled"] = _bool_in(s["cmd_enabled"], "cmd_enabled")
                    if d["cmd_enabled"] and not d.get("cmd_pin_hash"):
                        return False, "set a PIN first"
                    if d["cmd_enabled"] and not d["enabled"] and save:
                        return False, "turn alerts on first"
                if not d["enabled"]:                     # the answers go out as alerts: no alerts, no commands
                    d["cmd_enabled"] = False
                if d.get("cmd_topic") and d.get("cmd_topic") == d.get("topic"):
                    return False, "the command topic must differ from the alerts topic"
            if "controller_info" in patch:
                s, d = patch["controller_info"], new.setdefault("controller_info", copy.deepcopy(DEFAULTS["controller_info"]))
                if not isinstance(s, dict):
                    return False, "controller_info must be an object"
                if "asked" in s:
                    d["asked"] = _bool_in(s["asked"], "asked")
                if "sw" in s:
                    sw = str(s["sw"] or "").strip().lstrip("vV").strip()
                    if sw and not re.match(r"^\d{1,2}(\.\d{1,3}){0,3}$", sw):
                        return False, "software version looks like 1.40.1"
                    d["sw"] = sw
                for k, lo, hi in (("min_pct", 1, 100), ("fan100_rpm", 300, 5000)):
                    if k in s:
                        if s[k] in (None, ""):
                            d[k] = None
                            continue
                        v = _int(s[k])
                        if v is None or not lo <= v <= hi:
                            return False, f"{k} must be {lo}..{hi}"
                        d[k] = v
            if "setup" in patch:
                s, d = patch["setup"], new.setdefault("setup", copy.deepcopy(DEFAULTS["setup"]))
                if not isinstance(s, dict):
                    return False, "setup must be an object"
                if "lang" in s:
                    lg = str(s["lang"] or "")
                    if lg not in ("", "en", "da"):
                        return False, "language must be en or da"
                    d["lang"] = lg
                if "boiler_kw" in s:
                    if s["boiler_kw"] in (None, ""):
                        d["boiler_kw"] = None
                    else:
                        v = _int(s["boiler_kw"])
                        if v not in (10, 15, 25):
                            return False, "boiler size must be 10, 15 or 25 kW"
                        d["boiler_kw"] = v
                if "store_kg" in s:
                    if s["store_kg"] in (None, ""):
                        d["store_kg"] = None
                    else:
                        v = _f(s["store_kg"])
                        if v is None or v != v or not 0 < v <= STOCK_MAX:
                            return False, f"store size must be 1..{STOCK_MAX} kg"
                        d["store_kg"] = round(v)
                if _num(s.get("at")):
                    d["at"] = s["at"]                     # kept from the settings file
                if "done" in s:
                    d["done"] = _bool_in(s["done"], "done")
                    if d["done"] and not d.get("at"):
                        d["at"] = round(time.time())
                    if not d["done"]:
                        d["at"] = None
            if "pellets" in patch:                       # only the "buy before" reserve; stock and batches have their own calls
                s, d = patch["pellets"], new.setdefault("pellets", copy.deepcopy(DEFAULTS["pellets"]))
                if not isinstance(s, dict):
                    return False, "pellets must be an object"
                if "reserve_kg" in s:
                    v = _int(s["reserve_kg"])
                    if v is None or not 0 <= v <= 2000:
                        return False, "reserve must be 0..2000 kg"
                    d["reserve_kg"] = v
            if "warm" in patch:
                s, d = patch["warm"], new["warm"]
                for k in ("enabled", "use_board"):
                    if k in s:
                        d[k] = _bool_in(s[k], k)
                for k, lo, hi in (("stop_pct", 10, 100), ("stop_after_min", 0, 60), ("pause_h", 0, 48),
                                  ("start_window_min", 5, 120), ("max_attempts", 1, 10), ("retry_wait_s", 30, 900),
                                  ("keep_restart_c", 1, 30), ("keep_min_off_min", 0, 240),
                                  ("min_boiler_c", 0, 60), ("pump_c", 30, 80)):
                    if k in s:
                        v = _int(s[k])
                        if v is None or not lo <= v <= hi:
                            return False, f"{k} must be {lo}..{hi}"
                        d[k] = v
                if "boosts" in s:
                    b = [str(x).strip() for x in _list_in(s["boosts"], "boosts") if str(x).strip()]
                    if len(b) > 6 or any(not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", x) for x in b):
                        return False, "boost times must be HH:MM (up to 6)"
                    d["boosts"] = b
                if "periods" in s:
                    ps = []
                    for q in _list_in(s["periods"], "periods")[:8]:
                        if not isinstance(q, dict):
                            return False, "each period must be an object"
                        st_, en_ = str(q.get("start", "")), str(q.get("end", ""))
                        if not (re.match(r"^([01]\d|2[0-3]):[0-5]\d$", st_) and re.match(r"^([01]\d|2[0-3]):[0-5]\d$", en_)) or st_ == en_:
                            return False, "period times must be HH:MM and start must differ from end"
                        days = sorted({_day_in(x) for x in _list_in(q.get("days"), "days")} - {None})
                        kind = q.get("kind", "keep")
                        if kind not in ("keep", "start", "warmby") or not days:
                            return False, "period kind must be keep, start or warmby, with at least one day"
                        item = {"name": str(q.get("name", ""))[:30], "start": st_, "end": en_, "days": days,
                                "kind": kind, "on": _bool_in(q.get("on", True), "on")}
                        # Just start / Warm by: an own "stop at" power for the burn this period starts
                        # (e.g. only a small heat-up that stops already at 50 %). Empty = the normal limit.
                        if kind in ("start", "warmby") and q.get("stop_pct") not in (None, ""):
                            v = _int(q.get("stop_pct"))
                            if v is None or not 10 <= v <= 100:
                                return False, "a period's stop at power must be 10..100 %"
                            item["stop_pct"] = v
                        ps.append(item)
                    d["periods"] = ps
            for sect, keys in (("lux", ("drop_warn_pct", "trend_warn_pct", "low_abs")),):
                if sect in patch:
                    for k in keys:
                        if k in patch[sect]:
                            v = _int(patch[sect][k])
                            if v is None or not 0 < v < 200:
                                return False, f"{k} out of range"
                            new[sect][k] = v
        except _Bad as e:
            return False, str(e)
        except (AttributeError, TypeError, ValueError, KeyError, IndexError, OverflowError, RecursionError) as e:
            return False, f"bad request ({type(e).__name__})"
        with self.lock:
            old, self.data = self.data, new
            if save and not self.save():
                self.data = old                    # not on disk: keep using the old values (what the page shows)
                return False, self.SAVE_FAILED
        for fn in self.listeners:
            try:
                fn()
            except Exception:
                pass
        return True, "saved"

    def add_batch(self, name):
        name = str(name or "").strip()[:40]
        if not name:
            return False, "give the pellet batch a name"
        with self.lock:
            old = copy.deepcopy(self.data)
            bs = self.data.setdefault("pellets", {}).setdefault("batches", [])
            bs.append({"name": name, "since": time.time()})
            del bs[:-50]
            if not self.save():
                self.data = old
                return False, self.SAVE_FAILED
        return True, f"new pellet batch: {name}"

    def set_stock(self, kg, reserve_kg=None):
        """Pellets at home right now (counted, or the current amount + a delivery). Returns (ok, message)."""
        k = _f(kg)
        if k is None or k != k or not 0 <= k <= STOCK_MAX:
            return False, f"pellets at home must be 0..{STOCK_MAX} kg"
        r = None
        if reserve_kg is not None:
            r = _int(reserve_kg)
            if r is None or not 0 <= r <= 2000:
                return False, "reserve must be 0..2000 kg"
        with self.lock:
            old = copy.deepcopy(self.data)
            p = self.data.setdefault("pellets", {})
            p["stock"] = {"kg": round(k, 1), "at": time.time()}
            if r is not None:
                p["reserve_kg"] = r
            if not self.save():
                self.data = old
                return False, self.SAVE_FAILED
        return True, f"pellets at home: {k:.0f} kg"

    def set_internal(self, sect, key, value):
        with self.lock:
            self.data.setdefault(sect, {})[key] = value
            self.save()

    def note_boiler_type(self, value, now=None):
        """A cfg read: the boiler's type setting (033 KEDEL TYPE). When it changes, the lighting "usual" starts a
        fresh baseline from now. The first value ever seen is only remembered. True when a new baseline began."""
        v = _int(value)
        if v is None:
            return False
        with self.lock:
            b = self.data.setdefault("baseline", {})
            old = b.get("kedel")
            if old == v:
                return False
            b["kedel"] = v
            if old is not None:
                b["since"], b["why"] = round(now or time.time()), "boiler type changed"
            self.save()
        if old is not None:
            self.log(f"# boiler type changed ({old} -> {v}): usual lighting times start again from now")
        return old is not None


# ============================================================================ scheduler
def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class Scheduler:
    """Start windows. Only STOPPET counts as idle (the boiler ignores run_mode=start in WAITING;
    ALARM is never auto-cleared). With override_waiting, a WAITING (minimum-drift) pause inside a
    window is ended with stop -> start."""

    def __init__(self, settings, tracker, mod, send, log, emit):
        self.settings, self.tracker, self.mod, self.send, self.log, self.emit = settings, tracker, mod, send, log, emit
        self.attempts = 0
        self.last_attempt = 0.0
        self.attempt_state = None
        self.blocked_key = None          # window instance blocked by a manual stop / giving up
        self.blocked_why = ""
        self.blocked_code = ""
        self.override_stop_t = 0.0       # we sent a stop to end WAITING: the following STOPPET is ours
        self.last_eval = 0.0
        self.status = {"code": "off", "text": "disabled"}
        self.warned = set()
        tracker.listeners.append(self._on_event)

    # -- window logic --------------------------------------------------------------------------
    def active_window(self, now=None):
        """(key, window) of the enabled window covering 'now', or (None, None). Windows may cross midnight."""
        sch = self.settings.get("schedule")
        dt = datetime.datetime.fromtimestamp(now or time.time())
        m = dt.hour * 60 + dt.minute
        for i, w in enumerate(sch.get("windows", [])):
            if not w.get("on", True):
                continue
            a, b = _mins(w["start"]), _mins(w["end"])
            if a < b:
                if dt.weekday() in w["days"] and a <= m < b:
                    return f"{dt.date()}#{i}", w
            else:                                         # crosses midnight: belongs to the start day
                if dt.weekday() in w["days"] and m >= a:
                    return f"{dt.date()}#{i}", w
                y = dt - datetime.timedelta(days=1)
                if y.weekday() in w["days"] and m < b:
                    return f"{y.date()}#{i}", w
        return None, None

    def starts_today(self, now=None):
        day = datetime.date.fromtimestamp(now or time.time())
        trs = list(self.tracker.transitions)      # copy: also called from the HTTP / MQTT threads
        return sum(1 for t in trs if t.get("to") == 2 and datetime.date.fromtimestamp(t["epoch"]) == day)

    def _set(self, code, text, **p):
        if self.status.get("code") != code:
            self.log(f"# schedule: {text}")
        self.status = {"code": code, "text": text, "t": time.time(), "p": p}

    def _warn(self, kind, text, key, **p):
        tag = f"{kind}:{key}"
        if tag in self.warned:
            return
        self.warned.add(tag)
        self.log(f"# schedule WARNING: {text}")
        self.emit({"type": "warning", "source": "schedule", "kind": kind, "epoch": time.time(),
                   "t": _iso(), "text": text, "p": p})

    # -- events from the tracker -----------------------------------------------------------------
    def _on_event(self, ev):
        if ev.get("type") != "transition":
            return
        now, frm, to = ev["epoch"], ev.get("from"), ev.get("to")
        sch = self.settings.get("schedule")
        # short run: from OPSTART back to stopped/waiting/alarm
        if to in (0, 1, 6) and frm in RUNNING:
            start, burned = None, False
            for t in reversed(self.tracker.transitions):
                if t.get("to") == 4 and t["epoch"] <= now:
                    burned = True                  # it really burned (RUNNING) - a failed / stopped start is no "run"
                if t.get("to") == 2 and t["epoch"] <= now:
                    start = t["epoch"]
                    break
            if start and burned and sch.get("short_run_min") and now - start < sch["short_run_min"] * 60:
                self._warn("short_run", f"short run: {(now - start) / 60:.0f} min "
                                        f"(less than {sch['short_run_min']} min)", int(start),
                           min=round((now - start) / 60), limit=sch["short_run_min"])
        # manual stop inside a window -> respect it until the next window. Only the single WAITING -> STOPPET
        # that our own override stop causes is excluded; every later stop counts as manual.
        key, _ = self.active_window(now)
        ours = frm == 1 and to == 0 and self.override_stop_t and now - self.override_stop_t < 300
        if key and to == 0 and frm in RUNNING + (1,) and not ours:
            self.blocked_key, self.blocked_why, self.blocked_code = key, "stopped by hand in this window", "manual"
        # success
        if to == 2:
            self.attempts, self.last_attempt = 0, 0.0
            self.override_stop_t = 0.0             # the override is finished; later stops are manual

    # -- main loop -------------------------------------------------------------------------------
    def tick(self):
        now = time.time()
        if now - self.last_eval < 5:
            return
        self.last_eval = now
        sch = self.settings.get("schedule")
        if not sch.get("enabled"):
            return self._set("off", "disabled")
        if not self.mod.remote_armed:
            return self._set("not_armed", "remote start not armed (--allow-remote-start)")
        key, w = self.active_window(now)
        if not key:
            self.attempts, self.last_attempt = 0, 0.0
            return self._set("outside", "outside the start windows")
        if self.blocked_key == key:
            return self._set("blocked", f"{self.blocked_why}; waiting for the next window", why=self.blocked_code)
        st, al = self.tracker.state, self.tracker.alarm
        # act only on FRESH status telemetry (keep-alive frames alone don't tell us the state)
        lat = getattr(self.mod, "latest", None) or {}
        fresh = (now - (lat.get("epoch") or 0)) < 60
        if not fresh or st is None:
            return self._set("no_link", "no fresh status from the controller")
        if st == 6 or al:
            return self._set("alarm", "boiler in ALARM: auto-start does not clear alarms")
        if st in RUNNING:
            self.attempts, self.last_attempt = 0, 0.0
            return self._set("running", f"running ({STATE_LABELS.get(st)})", state=st)
        if st == 1 and not sch.get("override_waiting"):
            return self._set("waiting", "WAITING (minimum drift / timer) - override is off")
        # waiting for the result of the last attempt
        if self.last_attempt and now - self.last_attempt < sch["retry_wait_s"]:
            return self._set("pending", f"start sent, waiting for the boiler "
                                        f"(attempt {self.attempts}/{sch['max_attempts']})", n=self.attempts, max=sch["max_attempts"])
        if self.last_attempt and st == self.attempt_state:
            if self.attempts >= 2:
                self._warn("start_failed", f"no reaction after {self.attempts} start attempts", key, n=self.attempts)
            if self.attempts >= sch["max_attempts"]:
                self.blocked_key, self.blocked_why, self.blocked_code = key, f"gave up after {self.attempts} attempts", "gave_up"
                self._warn("gave_up", f"auto-start gave up after {self.attempts} attempts", key, n=self.attempts)
                return self._set("blocked", f"{self.blocked_why}; waiting for the next window", why="gave_up")
        # protections
        ours = st == 0 and self.override_stop_t and now - self.override_stop_t < 300   # STOPPET we caused
        since = self.tracker.since or now
        if st in (0, 1) and not ours and now - since < sch["min_off_min"] * 60:
            left = sch["min_off_min"] - (now - since) / 60
            return self._set("cooldown", f"minimum off-time: {left:.0f} min left", left=max(1, round(left)))
        n = self.starts_today(now)
        if n >= sch["max_starts_day"]:
            return self._set("max_starts", f"max starts reached today ({n}/{sch['max_starts_day']})", n=n, max=sch["max_starts_day"])
        # act
        self.attempts += 1
        self.last_attempt, self.attempt_state = now, st
        if st == 1:
            self.override_stop_t = now
            self.send("stop", "schedule (end WAITING)")
            self._set("pending", "ending WAITING pause (stop), start follows", ending=1)
            # the stop counts as an attempt (so a stop that doesn't work can't repeat forever);
            # re-check in ~40 s, then the start follows without the off-time wait
            self.last_attempt = now - sch["retry_wait_s"] + 40
            return
        self.send("start", "schedule")
        self._set("pending", f"start sent (attempt {self.attempts}/{sch['max_attempts']})", n=self.attempts, max=sch["max_attempts"])

    def feed(self):
        key, w = self.active_window()
        return {"status": self.status, "active_window": w, "starts_today": self.starts_today(),
                "attempts": self.attempts, "blocked": self.blocked_key == key and key is not None}


# ============================================================================ automatic alarm reset
class AutoReset:
    """Does what the owner does by hand when the boiler stops on a nuisance alarm: clear the alarm and start
    again. Opt-in. Only for the alarms ticked in the settings (never 1-3, 7-10, 13-15), only when the alarm came
    while burning, after a cool-down wait, and at most `max_per_day` attempts in 24 h. If the restarted boiler
    reaches RUNNING (DRIFT) within 30 min it counts as 'saved by auto alarm reset'.

    Sequence (boiler behaviour): run_mode=start in ALARM clears it to STOPPET (no relight); a second start
    from STOPPET starts the burner."""

    RESTART_WATCH_S = 30 * 60

    def __init__(self, settings, tracker, mod, send, log, emit):
        self.settings, self.tracker, self.mod, self.send, self.log, self.emit = settings, tracker, mod, send, log, emit
        self.phase, self.phase_t, self.alarm = None, 0.0, None     # None | waiting | cleared | restarted
        self.last_eval = 0.0
        self.status = {"code": "off", "text": "disabled"}
        self.warned = set()

    def _stats(self):
        return self.settings.get("auto_reset_stats") or {}

    def _save_stats(self, st):
        self.settings.set_internal("auto_reset_stats", "saves", st.get("saves", 0))
        for k in ("last_save", "attempts", "last_attempt"):
            self.settings.set_internal("auto_reset_stats", k, st.get(k))

    def _set(self, code, text, **p):
        if self.status.get("code") != code:
            self.log(f"# auto alarm reset: {text}")
        self.status = {"code": code, "text": text, "t": time.time(), "p": p}

    def _event(self, kind, text, **kw):
        ev = {"type": "auto_reset", "kind": kind, "epoch": time.time(), "t": _iso(), "text": text}
        ev.update(kw)
        self.emit(ev)

    def tick(self):
        now = time.time()
        if now - self.last_eval < 5:
            return
        self.last_eval = now
        cfg = self.settings.get("auto_reset")
        if not cfg.get("enabled"):
            self.phase = None
            return self._set("off", "disabled")
        if not self.mod.remote_armed:
            return self._set("not_armed", "remote start not armed (--allow-remote-start)")
        lat = getattr(self.mod, "latest", None) or {}
        if now - (lat.get("epoch") or 0) > 60 or self.tracker.state is None:
            return self._set("no_link", "no fresh status from the controller")
        st, al = self.tracker.state, self.tracker.alarm
        stats = self._stats()
        attempts = [a for a in (stats.get("attempts") or []) if now - a < 86400]

        # 1) a restart we did: did it reach RUNNING?
        if self.phase == "restarted":
            if st == 4:
                stats["saves"] = int(stats.get("saves") or 0) + 1
                stats["last_save"] = {"epoch": now, "t": _iso(now), "alarm": self.alarm}
                self._save_stats(stats)
                self._event("saved", f"saved by auto alarm reset: alarm {self.alarm} cleared, boiler is running again",
                            alarm=self.alarm)
                self.phase = None
                return self._set("saved", f"saved: running again after alarm {self.alarm}", a=self.alarm)
            if st == 6 or now - self.phase_t > self.RESTART_WATCH_S:
                self.phase = None                      # alarm again / never reached RUNNING: counts as a try
            else:
                return self._set("restarted", f"restarted after alarm {self.alarm} - waiting for RUNNING", a=self.alarm)

        # 2) we cleared the alarm: start from STOPPET
        if self.phase == "cleared":
            if st == 0 and not al:
                self.send("start", "auto_reset (restart)")
                self.phase, self.phase_t = "restarted", now
                return self._set("restarted", f"alarm {self.alarm} cleared - start sent", a=self.alarm)
            if now - self.phase_t < 120:
                return self._set("clearing", f"clearing alarm {self.alarm} ...", a=self.alarm)
            self.phase = None                          # clear didn't work: treat as a used attempt

        if st != 6 or not al:
            if self.phase == "waiting":                # cleared by someone else
                self.phase = None
            return self._set("idle", "no alarm")
        # 3) an alarm is active
        if al not in cfg.get("alarms", []):
            return self._set("not_allowed", f"alarm {al} is not set for automatic reset", a=al)
        before = (self.tracker.prealarm or {}).get("state_before")
        if before not in RUNNING:
            return self._set("not_burning", f"alarm {al} did not come while burning - left alone", a=al)
        if len(attempts) >= cfg["max_per_day"]:
            tag = f"gave_up:{int(attempts[0])}"
            if tag not in self.warned:
                self.warned.add(tag)
                self._event("gave_up", f"auto alarm reset gave up: {len(attempts)} attempts in 24 h, alarm {al} stays",
                            alarm=al)
                self.emit({"type": "warning", "source": "auto_reset", "kind": "gave_up", "epoch": now, "t": _iso(now),
                           "text": f"auto alarm reset: {len(attempts)} attempts in 24 h used - alarm {al} stays",
                           "p": {"n": len(attempts), "a": al}})
            return self._set("gave_up", f"{len(attempts)} of {cfg['max_per_day']} attempts used in 24 h", n=len(attempts), max=cfg["max_per_day"])
        since = self.tracker.since or now
        left = cfg["wait_min"] * 60 - (now - since)
        if left > 0:
            self.phase, self.alarm = "waiting", al
            return self._set("waiting", f"alarm {al}: clear + restart in {left / 60:.0f} min "
                                        f"(attempt {len(attempts) + 1}/{cfg['max_per_day']})",
                             a=al, left=max(1, round(left / 60)), n=len(attempts) + 1, max=cfg["max_per_day"])
        attempts.append(now)
        stats["attempts"] = attempts
        stats["last_attempt"] = {"epoch": now, "t": _iso(now), "alarm": al}
        self._save_stats(stats)
        self.alarm, self.phase, self.phase_t = al, "cleared", now
        self.send("start", "auto_reset (clear)")
        self._event("attempt", f"auto alarm reset: clearing alarm {al} (attempt {len(attempts)}/{cfg['max_per_day']})",
                    alarm=al)
        return self._set("clearing", f"clearing alarm {al} (attempt {len(attempts)}/{cfg['max_per_day']})", a=al, n=len(attempts), max=cfg["max_per_day"])

    def feed(self):
        stats = self._stats()
        now = time.time()
        return {"status": self.status, "saves": stats.get("saves", 0), "last_save": stats.get("last_save"),
                "last_attempt": stats.get("last_attempt"),
                "attempts_24h": len([a for a in (stats.get("attempts") or []) if now - a < 86400])}


# ============================================================================ ignition history
class IgnitionLog:
    """One record per start attempt: how long until the boiler was RUNNING (flame), or how it failed, with the
    boiler temperature at start, hours since the last burn and the pellet batch in use. Lets the owner see if
    ignition gets slower over time, with a new pellet batch, or from cold starts.

    Attempt starts: STOPPET/WAITING/ALARM -> OPSTART, or WAITING/PAUSESTOP -> IGNITION (relight).
    Ends: -> DRIFT (ok), -> ALARM (failed), -> STOPPET (stopped).

    TIME TO FLAME (the one definition, used by every page, Home Assistant and the Discoveries): `flame_s` =
    seconds from the start of lighting (IGNITION, state 3) until the flame reading is above FLAME_ON (17).
    None when no flame came. flame_min() gives it in minutes; report() adds the typical values.
    (`first_lux_s`, the older "first sign of light" time counted from the start of the attempt, is still saved
    but no longer shown anywhere.)"""

    def __init__(self, settings, tracker, mod, data_dir, log):
        self.settings, self.tracker, self.mod, self.log = settings, tracker, mod, log
        self.path = os.path.join(data_dir, "ignitions.jsonl") if data_dir else None
        self.records = _load_samples(self.path, 2000)
        self.cur = None
        self.last_burn_end = None
        if data_dir and any("flame_s" not in r for r in self.records):
            self._backfill_flame(data_dir)           # records from before `flame_s` was saved: once
        for r in self.records:                       # older records: a cause but no source yet (in memory only)
            if "source" not in r and r.get("cause"):
                s = source_of(r["cause"], None, 3 if r.get("relight") else 2)
                if s:
                    r["source"] = s
        if not self.records and data_dir:            # first run: rebuild from the saved history rows
            self._rebuild(data_dir)
        elif data_dir:                               # after a restart: when the last burn ended (for "off for h")
            self.last_burn_end = last_burn_end(data_dir)
        self.cur = None
        tracker.listeners.append(self._on_event)

    def feed_rows(self, rows):
        """Run saved rows (oldest first) through the same steps as live data: state changes, then the row."""
        prev = None
        for r in rows:
            st = _int(r.get("state"))
            if st is None or not r.get("epoch"):
                continue
            if prev is not None and st != prev:
                self._on_event({"type": "transition", "from": prev, "to": st, "epoch": r["epoch"],
                                "alarm": r.get("alarm")}, live=False, row=r)
            prev = st
            self.on_row(r)

    def _rebuild(self, data_dir):
        self.feed_rows(r for _, p in hist_files(data_dir) for r in rows_of(p))
        if self.records:
            self.log(f"# ignitions: rebuilt {len(self.records)} attempt(s) from the saved history")

    def _backfill_flame(self, data_dir):
        """Older records have no `flame_s`: work it out from the saved history rows of those days (the same steps
        as live), and save the file with it. A record whose day is no longer on disk gets None. Runs once."""
        todo = [r for r in self.records if "flame_s" not in r]
        day = lambda e: datetime.date.fromtimestamp(e).isoformat()
        want = {day(r["epoch"] + d) for r in todo for d in (-60, 0, 6 * 3600)}
        old = ignition_records(r for d, p in hist_files(data_dir) if d in want for r in rows_of(p))
        n = 0
        for r in todo:
            m = min(old, key=lambda o: abs(o["epoch"] - r["epoch"]), default=None)
            hit = m is not None and abs(m["epoch"] - r["epoch"]) <= 30 and bool(m.get("relight")) == bool(r.get("relight"))
            r["flame_s"] = m.get("flame_s") if hit else None
            n += r["flame_s"] is not None
        try:
            write_text_atomic(self.path, "".join(json.dumps(r) + "\n" for r in self.records))
        except OSError:
            pass
        self.log(f"# ignitions: time to flame worked out for {n} of {len(todo)} older start(s)")

    def batch_now(self, epoch=None):
        bs = (self.settings.get("pellets") or {}).get("batches") or []
        cur = None
        for b in bs:
            if b.get("since", 0) <= (epoch or time.time()):
                cur = b.get("name")
        return cur or "—"

    def _on_event(self, ev, live=True, row=None):
        if ev.get("type") != "transition":
            return
        frm, to, t = ev.get("from"), ev.get("to"), ev.get("epoch")
        if frm in (4, 5, 7) and to in (0, 1, 6):
            self.last_burn_end = t
        starting = (to == 2 and frm in (0, 1, 6)) or (to == 3 and frm in (1, 5) and self.cur is None)
        if starting:
            lat = ((getattr(self.mod, "latest", None) or {}).get("fields") or {}) if live else (row or {})
            raw = ((getattr(self.mod, "latest_cfg", None) or {}).get("raw") or {}) if live else {}
            self.cur = {"start": t, "ign_start": t if to == 3 else None, "relight": to == 3,
                        "boiler": _f(lat.get("boiler_c")), "flue": _f(lat.get("flue_c")),
                        "off_h": round((t - self.last_burn_end) / 3600, 2) if self.last_burn_end else None,
                        "batch": self.batch_now(t), "cause": ev.get("cause"),
                        "source": ev.get("source"), "after_alarm": bool(ev.get("after_alarm")),
                        # the ignition settings in force (TÆNDBRÆNDSEL, TÆND LUFT, OPVARMNING, TIMEOUT, MAX DOSER,
                        # SUPPLERING, INTERVAL) - so attempts with different settings can be compared
                        "cfg": {k: raw.get(k) for k in IGN_CFG if k in raw},
                        "auger_s": 0.0, "rpm": [], "first_lux_s": None, "flame_s": None, "peak_lux": None,
                        "_last": t}
            return
        if not self.cur:
            return
        if to == 5 and self.cur.get("ign_start") is None:
            self.cur["wait_from"] = t                # the boiler waits (PAUSESTOP, still warm) before lighting
            return
        if to == 3 and self.cur.get("ign_start") is None:
            self.cur["ign_start"] = t
            if self.cur.get("wait_from"):            # lighting really starts now: time to flame counts from here
                self.cur["waited_s"] = round(t - self.cur["wait_from"])
                self.cur["lux_base"] = t
            return
        if to == 4:
            self._finish(t, "ok")
        elif to == 6:
            self._finish(t, "failed", alarm=ev.get("alarm"))
        elif to in (0, 1):
            self._finish(t, "stopped")

    def on_row(self, row, hist=None):
        """While an attempt runs: fuel fed (auger-on time), fan rpm, first flame signal, peak flame."""
        c = self.cur
        if not c or not row.get("epoch"):
            return
        t = row["epoch"]
        dt = min(max(t - c["_last"], 0), 60)
        c["_last"] = t
        if row.get("auger_on"):
            c["auger_s"] += dt
        rpm = _f(row.get("fan_rpm"))
        if rpm:
            c["rpm"].append(rpm)
        lux = _f(row.get("flame"))
        if lux is not None:
            if lux > 0 and c["first_lux_s"] is None and _int(row.get("state")) != 5:
                c["first_lux_s"] = round(t - c.get("lux_base", c["start"]))
            if lux > FLAME_ON and c["flame_s"] is None and c.get("ign_start") is not None \
                    and _int(row.get("state")) in (3, 4):
                c["flame_s"] = max(0, round(t - c["ign_start"]))      # time to flame: lighting start -> flame > 17
            c["peak_lux"] = max(c["peak_lux"] or 0, lux)

    def _finish(self, t, result, alarm=None):
        c, self.cur = self.cur, None
        rpm = c.get("rpm") or []
        waited = c.get("waited_s") or 0
        rec = {"epoch": c["start"], "t": _iso(c["start"]), "result": result,
               "minutes": round((t - c["start"] - waited) / 60, 2), "waited_min": round(waited / 60, 1),
               "ignition_min": round((t - c["ign_start"]) / 60, 2) if c.get("ign_start") else 0.0,
               "relight": c["relight"], "boiler": c["boiler"], "flue": c["flue"], "off_h": c["off_h"],
               "batch": c["batch"], "cause": c.get("cause"), "cfg": c.get("cfg") or {},
               "auger_s": round(c.get("auger_s", 0)), "rpm_avg": round(statistics.mean(rpm)) if rpm else None,
               "rpm_min": round(min(rpm)) if rpm else None, "rpm_max": round(max(rpm)) if rpm else None,
               "first_lux_s": c.get("first_lux_s"), "flame_s": c.get("flame_s"), "peak_lux": c.get("peak_lux")}
        if alarm:
            rec["alarm"] = alarm
        if c.get("source"):                          # who / what started it (burner_ext.SOURCES)
            rec["source"] = c["source"]
            if c.get("after_alarm"):
                rec["after_alarm"] = True
        self.records.append(rec)
        self.records = self.records[-2000:]
        if self.path:
            append_jsonl(self.path, rec)

    def reset(self):
        self.settings.set_internal("ignitions", "since", time.time())

    def cfg_changes(self, recs, n=10):
        """The boiler's own settings changes (data/cfg_changes.jsonl) since the oldest of the last n starts, oldest
        first, at most 40: the starts list puts a line between the starts where a setting changed."""
        hist = getattr(self.mod, "cfg_hist", None)
        path = getattr(hist, "path", None)
        if not recs or not path:
            return []
        t0 = recs[-n:][0]["epoch"]
        rows = [r for r in rows_of(path) if isinstance(r.get("time"), (int, float)) and r["time"] >= t0
                and isinstance(r.get("field"), str)]
        return [{k: r.get(k) for k in ("time", "field", "old", "new")} for r in rows][-40:]

    def report(self):
        since = (self.settings.get("ignitions") or {}).get("since") or 0
        # each record also carries flame_min (minutes to flame, see the class text): the pages show that value
        # resumed = went on burning without lighting (see resumed()): marked, and left out of the comparisons
        recs = [dict(r, flame_min=flame_min(r), **({"resumed": True} if resumed(r) else {}))
                for r in self.records if r["epoch"] >= since][-500:]
        # batch names that differ only in case / spaces are one batch, shown with the first spelling
        # (only here, for the pages: the stored settings and records keep what was typed)
        batches = (self.settings.get("pellets") or {}).get("batches") or []
        show = batch_names([b.get("name") for b in batches] + [r.get("batch") for r in recs])
        seen, merged = set(), []
        for b in batches:
            if batch_key(b.get("name")) not in seen:
                seen.add(batch_key(b.get("name")))
                merged.append(dict(b, name=show(b.get("name"))))
        batches = merged
        for r in recs:
            r["batch"] = show(r.get("batch"))
        stats = {}
        for r in recs:
            s = stats.setdefault(r["batch"], {"n": 0, "ok": 0, "failed": 0, "minutes": [], "flame": []})
            s["n"] += 1
            if r["result"] == "ok":
                s["ok"] += 1
                s["minutes"].append(r["minutes"])
                if r["flame_min"] is not None:
                    s["flame"].append(r["flame_min"])
            elif r["result"] == "failed":
                s["failed"] += 1
        for s in stats.values():
            m, fl = s.pop("minutes"), s.pop("flame")
            s["median_min"] = round(statistics.median(m), 1) if m else None
            # time to flame of the starts that lit with this batch: how many, the median, the slowest
            s.update(flame_n=len(fl), flame_min=round(statistics.median(fl), 2) if fl else None,
                     flame_max_min=max(fl) if fl else None)
        typ, typ_n, bsince, older = usual_flame(self.records, self.settings)
        try:                                       # the lighting help card (read only): never breaks the report
            raw = (getattr(self.mod, "latest_cfg", None) or {}).get("raw") or {}
            sw = (self.settings.get("controller_info") or {}).get("sw")
            light_help = lighting_help(recs, raw, sw, typ, sync=bool(getattr(self.mod, "cfg_sync", None)))
        except Exception as e:
            self.log(f"# lighting help error: {e!r}")
            light_help = None
        # same comparison per ignition-settings combination (what helps ignition?)
        by_cfg = {}
        withcfg = [r for r in recs if not r.get("relight") and not r.get("resumed") and r.get("cfg")]
        # only the settings that actually differ between attempts are shown (the rest is the same everywhere)
        vary = [k for k in IGN_CFG if len({str(r["cfg"].get(k)) for r in withcfg}) > 1] or list(IGN_CFG)[:1]
        raw_now = (getattr(self.mod, "latest_cfg", None) or {}).get("raw") or {}
        for r in withcfg:
            key = " · ".join(f"{IGN_CFG[k]} {r['cfg'].get(k, '?')}" for k in vary)
            s = by_cfg.setdefault(key, {"n": 0, "ok": 0, "minutes": [], "flame": [],
                                        "cfg": {k: r["cfg"].get(k) for k in vary}})
            s["n"] += 1
            if r["result"] == "ok":
                s["ok"] += 1
                s["minutes"].append(r["minutes"])
                if lit(r):
                    s["flame"].append(flame_min(r))
        # median_min = typical time to full heat (start -> RUNNING), flame_min = typical time to flame,
        # cfg = the values of the settings that differ, now = the settings the boiler has now
        settings = [{"settings": k, "n": v["n"], "ok": v["ok"],
                     "median_min": round(statistics.median(v["minutes"]), 1) if v["minutes"] else None,
                     "flame_min": round(statistics.median(v["flame"]), 1) if v["flame"] else None, "cfg": v["cfg"],
                     "now": bool(raw_now) and all(str(raw_now.get(c)) == str(x) for c, x in v["cfg"].items())}
                    for k, v in by_cfg.items()]
        settings.sort(key=lambda s: (-(s["ok"] / s["n"]), s["median_min"] if s["median_min"] is not None else 1e9))
        out = {"records": recs, "batches": batches, "current_batch": show(self.batch_now()), "stats": stats,
               "by_settings": settings, "typical_flame_min": typ, "typical_flame_n": typ_n,
               "typical_days": TYPICAL_DAYS, "help": light_help, "cfg_changes": self.cfg_changes(recs)}
        if bsince:     # the boiler type changed: "usual" = starts after it (use), or older starts until 3 (old)
            out["baseline"] = {"since": bsince, "why": (self.settings.get("baseline") or {}).get("why") or "",
                               "use": not older, "old": older}
        return out


def last_burn_end(data_dir, files=3):
    """When the last burn ended (epoch): the newest change from running / pause / fire rescue to stopped / waiting /
    alarm in the newest saved day files, or None. Read once at start, so the first start after a restart of the
    monitor still knows how long the boiler was off."""
    for _, p in reversed(hist_files(data_dir)[-files:]):
        prev, end = None, None
        for r in rows_of(p):
            st = _int(r.get("state"))
            if st is None or not r.get("epoch"):
                continue
            if prev in (4, 5, 7) and st in (0, 1, 6):
                end = r["epoch"]
            prev = st
        if end:
            return end
    return None


def resumed(rec):
    """A start that went on burning without lighting: it reached RUNNING with no lighting step (IGNITION) in under
    2 minutes, so the fire was still there (a start right after a stop, or the boiler picking up its own fire).
    Not a real lighting: left out of every time to flame, lighting verdict and average, like a relight."""
    return (rec.get("result") == "ok" and not rec.get("relight") and rec.get("ignition_min") == 0
            and rec.get("minutes") is not None and rec["minutes"] < 2)


def flame_min(rec):
    """Minutes to flame of one start record (lighting start -> flame above FLAME_ON), or None when no flame came
    or the record is from before it was measured."""
    s = rec.get("flame_s")
    return round(s / 60.0, 2) if s is not None else None


def lit(rec):
    """A start that reached RUNNING and has a time to flame (a relight whose flame was still on is no lighting)."""
    return (rec.get("result") == "ok" and rec.get("flame_s") is not None and not (rec.get("relight") and rec.get("flame_s") == 0)
            and not resumed(rec))


TYPICAL_DAYS = 28                  # "typical" = the median of the starts that lit in the last 4 weeks ...
TYPICAL_MIN_N = 3                  # ... once there are at least 3 of them


def typical_flame(recs, now=None):
    """(typical minutes to flame, how many starts it is from): the median of the starts that lit in the last
    TYPICAL_DAYS days. (None, n) while there are fewer than TYPICAL_MIN_N."""
    t0 = (now or time.time()) - TYPICAL_DAYS * 86400
    v = [flame_min(r) for r in recs if r["epoch"] >= t0 and lit(r)]
    return (round(statistics.median(v), 2) if len(v) >= TYPICAL_MIN_N else None), len(v)


def usual_flame(records, settings, now=None):
    """THE usual time to flame (the Lighting time card, /api/ignitions, Burner health, MQTT): the start records
    since the last ignitions reset (newest 500), after the boiler-type baseline (baseline_split), then
    typical_flame. (minutes or None, how many starts, baseline since or None, True when from older starts)."""
    since = ((settings.get("ignitions") or {}).get("since") or 0) if settings is not None else 0
    recs = [r for r in records if (r.get("epoch") or 0) >= since][-500:]
    bsince = baseline_since(settings)
    base, older = baseline_split(recs, bsince)
    typ, n = typical_flame(base, now=now)
    return typ, n, bsince, older


def baseline_since(settings):
    """When the boiler type (cfg 033) last changed (epoch), or None. The usual lighting times start again there."""
    try:
        s = (settings.get("baseline") or {}).get("since")
    except Exception:
        return None
    return s if isinstance(s, (int, float)) and not isinstance(s, bool) and s > 0 else None


def baseline_split(recs, since):
    """(the starts to take the "usual" from, True when they are older starts): after the boiler type changed
    only the starts since then, once at least TYPICAL_MIN_N of them lit; until then all starts (older ones)."""
    if not since:
        return recs, False
    after = [r for r in recs if (r.get("epoch") or 0) >= since]
    if sum(1 for r in after if lit(r)) >= TYPICAL_MIN_N:
        return after, False
    return recs, True


# ---- Lighting help (the "Trouble lighting?" card on both pages). READ ONLY: it only compares what the boiler
# already told us (the settings it sent, the start records). Nothing here is ever sent to the boiler.
# Factory values per software family and boiler type (0 = 10 kW, 1 = 15 kW, 2 = 25 kW). 1.40 changed the start
# fuel and the lighting timeout on the 10 and 15 kW types, and the fuel rate on the 15 kW.
LIGHT_IDS = ("014", "015", "017", "018", "001", "035")      # start fuel s, ignition air %, timeout s, max doses, fuel g/h, PID P
LIGHT_FACTORY = {
    "1.30": {"014": (28, 28, 28), "015": (80, 80, 80), "017": (600, 600, 600), "018": (20, 20, 20),
             "001": (2400, 3300, 5400), "035": (5, 5, 5)},
    "1.40": {"014": (40, 40, 28), "015": (80, 80, 80), "017": (800, 800, 600), "018": (20, 20, 20),
             "001": (2400, 3400, 5400), "035": (5, 5, 5)},
}
# what lit well on ONE 15 kW boiler (software 1.40): an example to compare with, not the one right answer.
# 015 TÆND LUFT is left out: software 1.40 stores it but does not use it while lighting.
LIGHT_WORKED = {"014": 40, "017": 800, "018": 20}
LIGHT_SLOW_MIN = 6.0               # the lighting chart's normal band ends at 6 minutes to flame
LIGHT_LAST_N = 10


def sw_family(sw):
    """'1.30' for boiler software 1.30 up to 1.40, '1.40' for 1.40 and newer, None when it is not known: not typed
    in, not readable, or below 1.30 (e.g. "1.13", or "1.4", which could mean anything)."""
    m = re.match(r"\s*[vV]?(\d+)\.(\d+)", str(sw or ""))
    if not m:
        return None
    v = (int(m.group(1)), int(m.group(2)))
    return None if v < (1, 30) else "1.30" if v < (1, 40) else "1.40"


def lighting_help(records, cfg_raw, sw=None, usual=None, sync=False):
    """What the lighting help card shows, as codes and numbers (the pages hold the words):
    verdict  from the last 10 starts (relights left out): "few" (under 3 judged), "fails" (2 or more did not light),
             "slow" (over 6 min to flame, or clearly over this boiler's usual), else "ok"
    rows     each lighting setting: now / factory for this software and boiler type / worked on one 15 kW boiler,
             mark "high" / "low" / "short" / "doses" (worth a look), "unused" (TÆND LUFT: not used by software
             1.40, only when 1.40 or newer is known) or "diff" (not the factory value)
    sw_known False when the software version is not typed in, not readable or below 1.30: the 1.40 values are shown
    sync     the monitor's settings sync is on (it puts its saved values back when one is changed on the screen)
    starts   the last 10 starts, newest first (no pellet batch names: the report is made for sharing)."""
    def num(k):
        try:
            v = float((cfg_raw or {}).get(k))
            return int(v) if v == int(v) else v
        except (TypeError, ValueError, OverflowError):
            return None
    known = sw_family(sw)
    fam, typ = known or "1.40", num("033")
    typ = typ if typ in (0, 1, 2) else None
    rows = []
    for k in LIGHT_IDS:
        now, fac = num(k), (LIGHT_FACTORY[fam][k][typ] if typ is not None else None)
        mark = None
        if k == "015" and known == "1.40" and now is not None:
            mark = "unused"                      # stored, but not used by the 1.40 software (only when it is known)
        elif now is not None:
            if k == "018" and now < 2:
                mark = "doses"                   # 0 = about 255 top-ups (only the timeout stops them), 1 = none
            elif k == "014" and now >= 60:
                mark = "high"
            elif k == "014" and now < 28:
                mark = "low"
            elif k == "017" and now < 600:
                mark = "short"
            elif fac is not None and abs(now - fac) > 1e-9:
                mark = "diff"
        rows.append({"id": k, "now": now, "fac": fac, "ref": LIGHT_WORKED.get(k), "mark": mark})
    last = [r for r in records if not r.get("relight") and not resumed(r)][-LIGHT_LAST_N:]
    judged = [r for r in last if r.get("result") in ("ok", "failed")]
    failed = sum(1 for r in judged if r["result"] == "failed")
    mins = [flame_min(r) for r in judged if lit(r)]
    med = round(statistics.median(mins), 1) if mins else None
    if len(judged) < 3:
        k = "few"
    elif failed >= 2:
        k = "fails"
    elif med is not None and (med > LIGHT_SLOW_MIN or (usual is not None and len(mins) >= 3 and
                                                      statistics.median(mins[-3:]) - usual > max(1.0, 0.3 * usual))):
        k = "slow"
    else:
        k = "ok"
    starts = [{"epoch": r.get("epoch"), "result": r.get("result"), "flame_min": flame_min(r) if lit(r) else None,
               "minutes": r.get("minutes"), "alarm": r.get("alarm"),
               "start_fuel": (r.get("cfg") or {}).get("014"), "max_doses": (r.get("cfg") or {}).get("018")}
              for r in reversed(last)]
    return {"verdict": {"k": k, "n": len(judged), "lit": len(mins), "failed": failed, "min": med, "usual": usual},
            "sw": str(sw).strip() if sw else None, "family": fam, "sw_known": known is not None, "sync": bool(sync),
            "type": typ, "rows": rows, "starts": starts}


def batch_key(name):
    """Pellet batch names that differ only in case or spaces are the same batch."""
    return " ".join(str(name).split()).casefold() if isinstance(name, str) else name


def batch_names(names):
    """A function giving each batch name its first spelling (names in order: the first one wins)."""
    first = {}
    for n in names:
        if isinstance(n, str) and n.strip():
            first.setdefault(batch_key(n), n)
    return lambda n: first.get(batch_key(n), n) if isinstance(n, str) else n


class _NoSettings:
    def get(self, key):
        return {}


def ignition_records(rows):
    """The start records for saved rows (oldest first), made by the same steps as live. Nothing is read or written:
    used for older records and by the Discoveries (scada/insights_gen.py)."""
    ig = IgnitionLog(_NoSettings(), collections.namedtuple("T", "listeners")([]), None, None, lambda m: None)
    ig.feed_rows(rows)
    return ig.records


IGN_CFG = {"014": "start fuel s", "015": "ign. air %", "016": "warm-up s", "017": "timeout s",
           "018": "max doses", "019": "dose s", "020": "dose interval s"}


def _load_samples(path, keep):
    """Read a samples JSONL file; if it holds far more than we keep, rewrite it (atomically) so it never
    grows without limit."""
    out = []
    if not (path and os.path.exists(path)):
        return out
    out = rows_of(path)
    n = len(out)
    out = out[-keep:]
    if n > keep + keep // 2:
        try:
            write_text_atomic(path, "".join(json.dumps(s) + "\n" for s in out))
        except OSError:
            pass
    return out


# ============================================================================ LUX fouling
BANDS = ((0, 45, "low"), (45, 70, "mid"), (70, 1000, "high"))
LUX_WIN_S = 300                 # one sample per 5 min of steady burning
LUX_SETTLE_S = 600              # ... once the boiler has been in DRIFT for 10 min (the flame has settled)
LUX_READY_MIN = 30              # a power band is ready with 30 min of steady burning (6 samples) ...
LUX_READY_BURNS = 3             # ... from at least 3 different burns
LUX_V = 2                       # sample format: 5 min samples with the burn they belong to
RUN_WINDOW = 28 * 24 * 12       # samples = 5 min of steady running each -> 4 weeks of RUNNING time


def _band(p):
    for lo, hi, name in BANDS:
        if lo <= p < hi:
            return name
    return "high"


def lux_sample(win, now, drift_since):
    """One flame-sensor sample from the rows of the last LUX_WIN_S seconds, or None when the burning was not steady:
    in DRIFT for LUX_SETTLE_S, every row DRIFT with a flame reading, power within +-5 %-points, fan rpm within
    +-3 %, boiler temperature change <= 1 C. burn = when that DRIFT began (samples of one burn share it)."""
    if not drift_since or now - drift_since < LUX_SETTLE_S:
        return None
    if len(win) < 5 or any(_int(r.get("state")) != 4 or r.get("flame") is None for r in win):
        return None
    pw = [_f(r.get("power_pct")) for r in win]
    rpm = [_f(r.get("fan_rpm")) for r in win if _f(r.get("fan_rpm"))]
    bt = [_f(r.get("boiler_c")) for r in win if _f(r.get("boiler_c")) is not None]
    if None in pw or max(pw) - min(pw) > 10:
        return None
    if rpm and (max(rpm) - min(rpm)) > 0.06 * statistics.mean(rpm):
        return None
    if bt and abs(bt[-1] - bt[0]) > 1.0:
        return None
    p = statistics.median(pw)
    return {"epoch": now, "lux": round(statistics.median(_f(r["flame"]) for r in win), 1), "power": round(p, 1),
            "band": _band(p), "burn": int(drift_since), "v": LUX_V}


def lux_samples_from_rows(rows):
    """The samples the live monitor would have taken from these saved rows (oldest first)."""
    out, win = [], collections.deque()
    prev, since, last = None, None, 0.0
    for r in rows:
        e, st = r.get("epoch"), _int(r.get("state"))
        if not e or st is None:
            continue
        if st != prev:
            since, prev = (e if st == 4 else None), st
        win.append(r)
        while win and win[0]["epoch"] < e - LUX_WIN_S:
            win.popleft()
        if st == 4 and e - last >= LUX_WIN_S:
            x = lux_sample(list(win), e, since)
            if x:
                out.append(x)
                last = e
    return out


class LuxMonitor:
    """Takes one sample per 5 min of *steady* DRIFT (lux_sample). The usual level per power band is the median of
    its samples from the last 4 weeks of running time (summer stand-still doesn't count), without the newest 3.
    A band is ready after LUX_READY_MIN minutes of steady burning spread over LUX_READY_BURNS burns: short burns
    add up, so with 1-2 h burns it is ready in 2-3 days. On the first start with this format it learns from the
    saved history (hist-*.jsonl). "Sensor cleaned" (reset) starts again from that moment; no warnings meanwhile."""

    def __init__(self, settings, tracker, data_dir, log, emit):
        self.settings, self.tracker, self.log, self.emit = settings, tracker, log, emit
        self.path = os.path.join(data_dir, "lux_samples.jsonl") if data_dir else None
        self.warned = set()
        self.result = None
        self.samples = _load_samples(self.path, 3 * RUN_WINDOW)
        if data_dir and (not self.samples or any(x.get("v") != LUX_V for x in self.samples)):
            self._learn_saved(data_dir)
        self.last_sample = self.samples[-1]["epoch"] if self.samples else 0.0

    def _learn_saved(self, data_dir):
        """Learn from the saved day files (the last 5 weeks): once, when there are no samples of this format yet."""
        try:
            got = lux_samples_from_rows(r for _, p in hist_files(data_dir)[-35:] for r in rows_of(p))
        except Exception as e:                  # never stops the monitor from starting
            self.log(f"# flame sensor: could not learn from the saved history ({e!r})")
            return
        if not got:
            return
        self.samples = got[-3 * RUN_WINDOW:]
        try:
            write_text_atomic(self.path, "".join(json.dumps(x) + "\n" for x in self.samples))
        except OSError:
            pass
        self.log(f"# flame sensor: learned {len(self.samples)} steady 5-min samples from the saved history")

    def on_row(self, row, hist):
        if row.get("flame") is None or _int(row.get("state")) != 4:
            return
        now = row["epoch"]
        if now - self.last_sample < LUX_WIN_S:
            return
        win = [r for r in hist if r.get("epoch") and now - LUX_WIN_S <= r["epoch"] <= now]
        s = lux_sample(win, now, self.tracker.since)
        if not s:
            return
        self.samples.append(s)
        self.samples = self.samples[-3 * RUN_WINDOW:]
        self.last_sample = now
        if self.path:
            append_jsonl(self.path, s)
        self.analyse(notify=True)

    def reset(self):
        self.settings.set_internal("lux", "reset_epoch", time.time())
        self.warned.clear()
        self.emit({"type": "lux_reset", "epoch": time.time(), "t": _iso()})
        self.analyse()

    def analyse(self, notify=False):
        cfg = self.settings.get("lux")
        ff = self.settings.get("flame_field")
        use = [s for s in self.samples if s["epoch"] >= (cfg.get("reset_epoch") or 0)][-RUN_WINDOW:]
        per_h = 3600 / LUX_WIN_S
        out = {"flame_field": ff, "samples": use, "bands": {}, "warnings": [], "status": "ok",
               "running_h": round(len(use) / per_h, 1), "reset_epoch": cfg.get("reset_epoch") or 0,
               "need_min": LUX_READY_MIN, "need_burns": LUX_READY_BURNS}
        if not ff:
            out["status"] = "no_field"
        elif not use:
            out["status"] = "no_data"
        learning = True
        norm = []                                   # (running-hour index, lux / band baseline)
        pos = {id(s): i for i, s in enumerate(use)}
        for name in ("low", "mid", "high"):
            bs = [s for s in use if s["band"] == name]
            burns = len({s.get("burn") or datetime.date.fromtimestamp(s["epoch"]).isoformat() for s in bs})
            mins = round(len(bs) * LUX_WIN_S / 60)
            ready = mins >= LUX_READY_MIN and burns >= LUX_READY_BURNS
            # min / burns = how far it has learned (the pages show "x of 30 min, y of 3 burns")
            b = {"n": len(bs), "min": mins, "burns": burns, "baseline": None, "current": None, "drop_pct": None,
                 "learning": not ready}
            if ready:
                learning = False
                base = statistics.median(s["lux"] for s in bs[:-3])
                cur = statistics.median(s["lux"] for s in bs[-3:])
                b.update(baseline=round(base, 1), current=round(cur, 1),
                         drop_pct=round(100 * (1 - cur / base), 1) if base else None)
                if base and b["drop_pct"] is not None and b["drop_pct"] >= cfg["drop_warn_pct"]:
                    out["warnings"].append({"kind": "drop", "band": name, "p": {"lux": round(cur), "pct": round(b["drop_pct"]), "base": round(base)},
                        "text": f"LUX {cur:.0f} is {b['drop_pct']:.0f} % below the usual {base:.0f} ({name} power)"})
                if cur >= 150:
                    b["saturated"] = True
                if base:
                    for s in bs:
                        norm.append((pos[id(s)] / per_h, s["lux"] / base))
            out["bands"][name] = b
            if b["current"] is not None and b["current"] < cfg["low_abs"]:
                out["warnings"].append({"kind": "low", "band": name, "p": {"lux": round(b["current"])},
                    "text": f"LUX {b['current']:.0f} is close to the fire-rescue limit (<= 10)"})
        if ff and use and learning:
            out["status"] = "learning"
        # slow decline over running time = soot on the sensor
        if len(norm) >= 72:
            norm.sort()
            xs, ys = [x for x, _ in norm], [y for _, y in norm]
            mx, my = statistics.mean(xs), statistics.mean(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            if sxx > 0:
                slope = sum((x - mx) * (y - my) for x, y in norm) / sxx
                span = xs[-1] - xs[0]
                ss_tot = sum((y - my) ** 2 for y in ys) or 1e-9
                r2 = 1 - sum((y - (my + slope * (x - mx))) ** 2 for x, y in norm) / ss_tot
                change = 100 * slope * span
                out["trend"] = {"change_pct": round(change, 1), "span_running_h": round(span, 1), "r2": round(r2, 2)}
                if span >= 72 and change <= -cfg["trend_warn_pct"] and r2 >= 0.3:
                    out["warnings"].append({"kind": "soot", "p": {"pct": round(abs(change)), "h": round(span)},
                        "text": f"LUX has fallen {abs(change):.0f} % over {span:.0f} running hours: "
                                f"the flame sensor probably needs cleaning (soot)"})
        if out["warnings"] and out["status"] == "ok":
            out["status"] = "warning"
        if notify:
            for w in out["warnings"]:
                tag = w["kind"] + w.get("band", "")
                if tag not in self.warned:
                    self.warned.add(tag)
                    self.emit({"type": "warning", "source": "lux", "kind": w["kind"], "epoch": time.time(),
                               "t": _iso(), "text": w["text"], "p": w.get("p", {}), "band": w.get("band")})
            self.warned &= {w["kind"] + w.get("band", "") for w in out["warnings"]}
        self.result = out
        return out

    def summary(self):
        """The per-band table for the pages (Burner health -> Flame sensor), without the samples."""
        r = self.analyse()
        return {k: r[k] for k in ("status", "bands", "warnings", "running_h", "reset_epoch", "need_min", "need_burns",
                                  "flame_field")}


# ============================================================================ fan bearing wear (beta, recording only)
class FanMonitor:
    """Groundwork for bearing-wear detection. One sample per 10 min when the fan has run steadily for 5 min
    (same state, fan output within +-2 %-points, rpm > 0): output %, median rpm and rpm jitter (std/mean).
    Grouped by output level (5 % steps). Per level: baseline = median of the last 4 weeks of running time
    (excluding the newest hour), 'now' = median of the last 3 samples. A worn bearing should show as lower rpm
    at the same output (more friction) and/or more jitter. BETA: no warnings until real data shows what is
    normal for this fan (a hot chimney alone changes rpm by a few %)."""

    def __init__(self, settings, data_dir, log):
        self.settings, self.log = settings, log
        self.path = os.path.join(data_dir, "fan_samples.jsonl") if data_dir else None
        self.samples = _load_samples(self.path, 3 * RUN_WINDOW)
        self.last_sample = self.samples[-1]["epoch"] if self.samples else 0.0

    def on_row(self, row, hist):
        now = row.get("epoch") or time.time()
        if now - self.last_sample < 600:
            return
        win = [r for r in hist if r.get("epoch") and now - 300 <= r["epoch"] <= now]
        if len(win) < 5:
            return
        st = {_int(r.get("state")) for r in win}
        pct = [_f(r.get("fan_pct")) for r in win]
        rpm = [_f(r.get("fan_rpm")) for r in win]
        if len(st) != 1 or None in pct or None in rpm or min(rpm) <= 0 or min(pct) <= 0 or max(pct) - min(pct) > 2:
            return
        mean = statistics.mean(rpm)
        s = {"epoch": now, "state": st.pop(), "pct": round(statistics.median(pct), 1),
             "rpm": round(statistics.median(rpm)), "jitter": round(100 * statistics.pstdev(rpm) / mean, 2),
             "boiler": _f(win[-1].get("boiler_c")), "flue": _f(win[-1].get("flue_c"))}
        s["bucket"] = int(5 * round(s["pct"] / 5))
        self.samples.append(s)
        self.samples = self.samples[-3 * RUN_WINDOW:]
        self.last_sample = now
        if self.path:
            append_jsonl(self.path, s)

    def reset(self):
        self.settings.set_internal("fan", "reset_epoch", time.time())

    def analyse(self):
        reset = (self.settings.get("fan") or {}).get("reset_epoch") or 0
        use = [s for s in self.samples if s["epoch"] >= reset][-RUN_WINDOW:]
        out = {"status": "recording" if use else "no_data", "running_h": round(len(use) / 6, 1),
               "reset_epoch": reset, "levels": [], "samples": use[-2000:]}
        for b in sorted({s["bucket"] for s in use}):
            bs = [s for s in use if s["bucket"] == b]
            lv = {"pct": b, "n": len(bs), "baseline_rpm": None, "now_rpm": None, "change_pct": None,
                  "jitter_base": None, "jitter_now": None, "rpm_per_pct": None}
            if len(bs) >= 12:
                base = statistics.median(s["rpm"] for s in bs[:-6])
                cur = statistics.median(s["rpm"] for s in bs[-3:])
                lv.update(baseline_rpm=round(base), now_rpm=round(cur),
                          change_pct=round(100 * (cur / base - 1), 1) if base else None,
                          jitter_base=round(statistics.median(s["jitter"] for s in bs[:-6]), 2),
                          jitter_now=round(statistics.median(s["jitter"] for s in bs[-3:]), 2),
                          rpm_per_pct=round(base / b, 1) if b else None)
            out["levels"].append(lv)
        return out


# ============================================================================ optimiser
SUMMARY_VERSION = 5
ALARM_HINTS = {2: "thermo switch: check burner plug/cable and for burn-back",
               4: "ignition failed: check igniter, start fuel (TÆNDBRÆNDSEL) and the flame sensor",
               5: "fire rescue failed: check pellets (silo/bridging), auger and the flame sensor",
               6: "ash plate: check for clinker under the plate and the ash-plate switch",
               12: "flue fan: check fan, bearings and the rpm sensor",
               13: "fuse on the board blown"}


def summarise_rows(rows):
    """One day's history rows -> summary dict."""
    rows = sorted((r for r in rows if r.get("epoch")), key=lambda r: r["epoch"])
    # rescue_at: each fire rescue as [epoch, how it ended]: "ok" (burning again), "failed" (alarm), "stopped",
    # or None while the day ends in the rescue
    s = {"v": SUMMARY_VERSION, "state_s": {}, "starts": 0, "runs": [], "rescues": 0, "rescue_at": [], "alarms": {},
         "fuel_silo_kg": 0.0, "fuel_est_kg": 0.0, "running_s": 0.0, "drift_power": [], "rows": len(rows),
         "flame_power": {}}
    run_start, run_burned = None, False
    prev = None
    for r in rows:
        st = _int(r.get("state"))
        if prev is not None:
            pst = _int(prev.get("state"))
            dt = min(r["epoch"] - prev["epoch"], 60.0)
            if pst is not None:
                s["state_s"][str(pst)] = s["state_s"].get(str(pst), 0) + dt
                if pst in RUNNING:
                    s["running_s"] += dt
                if pst == 4:
                    p, g = _f(prev.get("power_pct")), _f(prev.get("fuel_gph"))
                    if p is not None:
                        s["drift_power"].append(p)
                        # flame / fan per 10 % power step (time-weighted sums, so days can be added up):
                        # [seconds, flame*s, seconds with flame, rpm*s, seconds with rpm]
                        lux, rpm = _f(prev.get("flame")), _f(prev.get("fan_rpm"))
                        b = s["flame_power"].setdefault(str(min(int(p // 10) * 10, 90)), [0.0, 0.0, 0.0, 0.0, 0.0])
                        b[0] += dt
                        if lux is not None:
                            b[1] += lux * dt
                            b[2] += dt
                        if rpm:
                            b[3] += rpm * dt
                            b[4] += dt
                        if g:
                            s["fuel_est_kg"] += p / 100 * g / 1000 * dt / 3600
            a, b = _f(prev.get("silo_kg")), _f(r.get("silo_kg"))
            if a is not None and b is not None and 0 < a - b < 5:      # increases = refill, big jumps = reset
                s["fuel_silo_kg"] += a - b
            if st != pst:
                if st == 2:
                    s["starts"] += 1
                    run_start, run_burned = r["epoch"], False
                if st == 4:
                    run_burned = True              # only starts that really burned count as a "run"
                if st == 7:
                    s["rescues"] += 1
                    s["rescue_at"].append([round(r["epoch"]), None])
                elif pst == 7 and s["rescue_at"] and s["rescue_at"][-1][1] is None:
                    s["rescue_at"][-1][1] = "ok" if st == 4 else "failed" if st == 6 else "stopped"
                if st == 6:
                    al = str(_int(r.get("alarm"), 0))
                    s["alarms"][al] = s["alarms"].get(al, 0) + 1
                if st in (0, 1, 6) and pst in RUNNING and run_start:
                    if run_burned:
                        s["runs"].append(round((r["epoch"] - run_start) / 60, 1))
                    run_start, run_burned = None, False
        prev = r
    dp = s.pop("drift_power")
    s["drift_power_avg"] = round(statistics.mean(dp), 1) if dp else None
    s["drift_power_max_share"] = round(sum(1 for p in dp if p >= 95) / len(dp), 2) if dp else None
    s["fuel_silo_kg"] = round(s["fuel_silo_kg"], 2)
    s["fuel_est_kg"] = round(s["fuel_est_kg"], 2)
    s["running_s"] = round(s["running_s"])
    s["flame_power"] = {k: [round(x, 1) for x in v] for k, v in s["flame_power"].items()}
    return s


def flame_by_power(days):
    """Add up the per-day flame/fan sums -> one row per 10 % power step: minutes, average flame, average fan rpm.
    The flame number is brightness at the sensor, not efficiency - but on one burner it shows where the
    air/fuel mix gives the steadiest, brightest flame (here: brightest at 40-60 %, dimmer at full power)."""
    tot = {}
    for d in days:
        for k, v in (d.get("flame_power") or {}).items():
            t = tot.setdefault(k, [0.0] * 5)
            for i in range(5):
                t[i] += v[i] if i < len(v) else 0
    out = []
    for k in sorted(tot, key=int):
        sec, lx, lxs, rp, rps = tot[k]
        if sec < 120:
            continue
        out.append({"power": int(k), "minutes": round(sec / 60), "flame": round(lx / lxs) if lxs else None,
                    "rpm": round(rp / rps) if rps else None})
    return out


WHOLE_DAY_H = 20                   # a day counts as recorded in full with at least this many hours of readings
PER_DAY_FILES = 15                 # "per day" looks at the last 15 day files (today is left out) ...
PER_DAY_N = 14                     # ... and uses at most 14 days of them


def day_fuel(s):
    """kg of pellets one day summary stands for, before the owner's correction: the boiler's silo counter when it
    moved that day, otherwise the estimate from power x fuel rate."""
    return (s.get("fuel_silo_kg") or 0) if (s.get("fuel_silo_kg") or 0) > 0 else (s.get("fuel_est_kg") or 0)


SHORT_BURN_MIN = 30                 # a burn shorter than this (minutes from running to the stop) is a "short burn"


def short_burns(runs):
    """How many of these burns (minutes each, None = unknown) were short."""
    return sum(1 for r in (runs or []) if r is not None and r < SHORT_BURN_MIN)


def rescue_list(days):
    """All fire rescues of these day summaries, newest first: [[epoch, "ok" / "failed" / "stopped" / None], ...].
    A day summary from before the times were kept counts its rescues with no time ([None, None])."""
    out = []
    for d in days:
        at = d.get("rescue_at")
        out += [list(x) for x in at] if isinstance(at, list) else [[None, None]] * (d.get("rescues") or 0)
    return sorted(out, key=lambda x: x[0] or 0, reverse=True)


RESCUE_DAYS = 7                     # the Fire rescues check looks at the last 7 days that burned


def day_numbers(s, corr=1.0):
    """The numbers every page, Home Assistant and the phone alerts show for one day (from its summary):
    fuel_kg = pellets burned (day_fuel x the owner's correction), burn_h = hours the burner was on (starting,
    lighting, running, pause, fire rescue), recorded_h = hours of readings that day."""
    return {"fuel_kg": round(day_fuel(s) * corr, 2), "burn_h": round((s.get("running_s") or 0) / 3600, 1),
            "recorded_h": round(sum(v or 0 for v in (s.get("state_s") or {}).values()) / 3600, 1)}


class Optimiser:
    def __init__(self, settings, store, mod, log):
        self.settings, self.store, self.mod, self.log = settings, store, mod, log
        self.cache_today = (0.0, None)

    def _day_summary(self, path, day):
        today = datetime.date.today().isoformat()
        cpath = os.path.join(self.store.dir, f"summary-{day}.json")
        if day != today and os.path.exists(cpath) and os.path.getmtime(cpath) >= os.path.getmtime(path):
            s = read_json(cpath)
            if isinstance(s, dict) and s.get("v") == SUMMARY_VERSION:
                return s
        if day == today and time.time() - self.cache_today[0] < 60 and self.cache_today[1]:
            return self.cache_today[1]
        try:
            rows = list(read_jsonl(path))
        except OSError:
            return None
        s = summarise_rows(rows)
        s["day"] = day
        if day == today:
            self.cache_today = (time.time(), s)
        else:
            try:
                write_json_atomic(cpath, s)
            except OSError:
                pass
        return s

    def reset(self):
        self.settings.set_internal("optimiser", "reset_epoch", time.time())

    _fuel = staticmethod(day_fuel)

    def per_day_days(self):
        """The days "per day" is worked out from - THE ONE RULE for pellets per day and starts per day (pellet
        stock, History, the share picture, Home Assistant): finished days (not today) that burned, from the last
        PER_DAY_FILES day files, at most PER_DAY_N of them. Days recorded in full (WHOLE_DAY_H hours or more) only;
        if there is none yet, the days recorded in part."""
        if not (self.store and self.store.dir):
            return []
        today, done = datetime.date.today().isoformat(), []
        for day, path in hist_files(self.store.dir)[-PER_DAY_FILES:]:
            if day == today:
                continue                           # today is not finished
            s = self._day_summary(path, day)
            if s and s["running_s"] > 0:
                done.append(s)
        whole = [s for s in done if day_numbers(s)["recorded_h"] >= WHOLE_DAY_H]
        return (whole or done)[-PER_DAY_N:]

    def per_day(self):
        """{"kg": pellets per day, "starts": starts per day, "days": how many days it is from, "whole_days": they
        were recorded in full} - see per_day_days(). kg and starts are None while there is no such day."""
        use, corr = self.per_day_days(), self.correction()
        return {"kg": round(statistics.mean(day_fuel(s) * corr for s in use), 1) if use else None,
                "starts": round(statistics.mean(s.get("starts") or 0 for s in use), 1) if use else None,
                "days": len(use), "whole_days": bool(use) and day_numbers(use[0])["recorded_h"] >= WHOLE_DAY_H}

    def pellet_stock(self):
        """Pellets at home = the amount the owner gave (counted / after a delivery) minus what burned since then
        (the boiler's silo counter when it moved that day, otherwise the power-based estimate). Days left use the
        pellets per day of per_day_days(). None until the owner has entered an amount."""
        p = self.settings.get("pellets") or {}
        st = p.get("stock")
        if not st or not (self.store and self.store.dir):
            return None
        at = st["at"]
        raw = self.burned_since(at)
        corr = self.correction()
        return self._stock_out(st, p, raw * corr, raw, corr, [day_fuel(s) * corr for s in self.per_day_days()])

    def correction(self):
        """Factor from the owner's counts: real kg burned / estimated kg burned (median of the last 3, 0.5..1.5)."""
        cs = [c["f"] for c in ((self.settings.get("pellets") or {}).get("corrections") or []) if c.get("f")]
        return round(statistics.median(cs[-3:]), 3) if cs else 1.0

    def burned_since(self, at):
        """Estimated kg of pellets burned since epoch `at` (silo counter where it moved, else power x fuel rate)."""
        if not (self.store and self.store.dir) or not at:
            return 0.0
        day_at = datetime.date.fromtimestamp(at).isoformat()
        burned = 0.0
        for day, path in hist_files(self.store.dir):
            if day < day_at:
                continue
            if day == day_at:                      # only the part of that day after the amount was entered
                try:
                    rows = [r for r in read_jsonl(path) if (r.get("epoch") or 0) >= at]
                except OSError:
                    continue
                burned += self._fuel(summarise_rows(rows))
            else:
                s = self._day_summary(path, day)
                if s:
                    burned += self._fuel(s)
        return burned

    def _stock_out(self, st, p, burned, raw, corr, recent):
        at = st["at"]
        recent = recent[-14:]
        per_day = statistics.mean(recent) if recent else None
        left = max(0.0, st["kg"] - burned)
        reserve = p.get("reserve_kg", 150)
        out = {"kg_set": st["kg"], "at": at, "burned": round(burned, 1), "left": round(left, 1),
               "per_day": round(per_day, 1) if per_day else None, "reserve_kg": reserve,
               "days_left": None, "buy_by": None, "days_basis": len(recent),
               "estimate": True, "correction": corr, "counts": len((p.get("corrections") or []))}
        if per_day and per_day > 0.2 and len(recent) >= 5:     # a guess from 1-2 days would be nonsense
            out["days_left"] = round(left / per_day)
            out["buy_by"] = time.time() + max(0.0, left - reserve) / per_day * 86400
        return out

    def learn_count(self, counted_kg):
        """The owner counted the pellets: compare with the estimate and keep a correction factor (before set_stock)."""
        p = self.settings.get("pellets") or {}
        st = p.get("stock")
        k = _f(counted_kg)
        if not st or k is None:
            return None
        est = self.burned_since(st["at"])
        real = st["kg"] - k
        if est < 15 or real <= 0:                  # too little burned to learn from (or a delivery in between)
            return None
        f = max(0.5, min(1.5, real / est))
        cs = list(p.get("corrections") or [])[-9:]
        cs.append({"epoch": round(time.time()), "est": round(est, 1), "real": round(real, 1), "f": round(f, 3)})
        self.settings.set_internal("pellets", "corrections", cs)
        return f

    def learn_ash(self, fill):
        """The owner emptied the bin and said how full it was (0.25 / 0.5 / 0.75 / 1). From the pellets burned since
        the last emptying and the bin size, work out the real ash share and keep the median of the last 5.
        fill None = "forgot to press last time": nothing is learned. Call BEFORE the new emptied time is saved."""
        a = self.settings.get("ash") or {}
        at, cap, f = a.get("emptied_at"), a.get("bin_kg") or 0, _f(fill)
        if not at or not cap or f is None or not 0 < f <= 1.2:
            return None
        burned = self.burned_since(at) * self.correction()
        if burned < 20:                                    # too few pellets since the last time to learn from
            return None
        pct = max(0.1, min(5.0, f * cap / burned * 100))
        ls = list(a.get("learn") or [])[-7:]
        ls.append({"epoch": round(time.time()), "fill": f, "burned": round(burned, 1), "pct": round(pct, 2)})
        self.settings.set_internal("ash", "learn", ls)
        self.settings.set_internal("ash", "ash_pct", round(statistics.median(x["pct"] for x in ls[-5:]), 2))
        return pct

    def ash_status(self):
        """Ash bin: estimated ash since it was last emptied = pellets burned x ash content. No ash signal exists,
        so it is an estimate; the owner presses "Just emptied" and can enter the bin size (kg) to see % full."""
        a = self.settings.get("ash") or {}
        at = a.get("emptied_at")
        if not at:
            return {"emptied_at": None}
        burned = self.burned_since(at) * self.correction()
        ash = burned * (a.get("ash_pct") or 0.7) / 100.0
        cap = a.get("bin_kg") or 0
        days = (time.time() - at) / 86400
        out = {"emptied_at": at, "burned": round(burned, 1), "ash_kg": round(ash, 2), "ash_pct": a.get("ash_pct") or 0.7,
               "learned": len(a.get("learn") or []),
               "bin_kg": cap, "full_pct": round(min(100.0, 100.0 * ash / cap)) if cap else None,
               "days": round(days, 1), "full_at": None, "per_day": None}
        # ash per day: burned since emptying / days, once there is at least a day of it (no fixed numbers)
        if days >= 1 and burned > 1:
            per_day = ash / days
            out["per_day"] = round(per_day, 3)
            if cap and per_day > 0:
                out["full_at"] = round(time.time() + max(0.0, cap - ash) / per_day * 86400)
        return out

    def health(self, days=None):
        """Burner health against this burner's OWN normal - no numbers taken from one particular boiler, works
        offline. Normal = the first starts / burning days after the last "burner cleaned" mark, now = the latest
        ones. Checks: minutes to flame, flame at the start, lightings that failed, flame at the same power while
        running. Each check says "learning" until there is enough of its own data."""
        cleaned = (self.settings.get("health") or {}).get("cleaned_at") or 0
        ig = getattr(self.mod, "ignitions", None)
        recs = [r for r in (list(ig.records) if ig else [])
                if (r.get("epoch") or 0) >= cleaned and not r.get("relight") and not resumed(r)]
        # the boiler type changed: lighting is judged against the starts since then (older ones until 3 lit)
        ok = [r for r in baseline_split(recs, baseline_since(self.settings))[0] if lit(r)]
        items = []

        def cmp(key, vals, unit, worse_when_up, rel, absd, need=6):
            if len(vals) < need:
                items.append({"key": key, "state": "learning", "n": len(vals), "need": need, "unit": unit})
                return
            base = statistics.median(vals[:max(3, min(10, len(vals) - 3))])
            now = statistics.median(vals[-3:])
            d = (now - base) if worse_when_up else (base - now)
            items.append({"key": key, "state": "worse" if d > max(absd, rel * abs(base)) else "ok",
                          "now": round(now, 1), "normal": round(base, 1), "unit": unit, "n": len(vals)})

        # time to flame: "usual" is THE typical time to flame (usual_flame, same as /api/ignitions and the
        # Lighting time card); "now" = the latest 3 starts that lit since the cleaning. Ready as soon as the
        # card is (a usual value) and 3 starts lit, so health and the card never disagree
        vals = [flame_min(r) for r in ok]
        typ = usual_flame(list(ig.records) if ig else [], self.settings)[0]
        if typ is None or len(vals) < 3:
            items.append({"key": "to_flame", "state": "learning", "n": len(vals), "need": 3, "unit": "min"})
        else:
            now = statistics.median(vals[-3:])
            items.append({"key": "to_flame", "state": "worse" if now - typ > max(1.0, 0.3 * typ) else "ok",
                          "now": round(now, 1), "normal": typ, "unit": "min", "n": len(vals),
                          "last": round(vals[-1], 1)})     # the newest start that lit (the pages show it as "now")
        cmp("start_flame", [float(r["peak_lux"]) for r in ok if r.get("peak_lux")], "", False, 0.15, 10)
        last = recs[-10:]
        failed = sum(1 for r in last if r.get("result") == "failed")
        items.append({"key": "failed", "state": "learning" if len(last) < 5 else ("worse" if failed >= 2 else "ok"),
                      "now": failed, "n": len(last), "need": 5})
        # flame at the same power while running: the latest 2 burning days against the days before them
        if days is None:
            days = self.report_days()
        cday = datetime.date.fromtimestamp(cleaned).isoformat() if cleaned else ""
        burn = [d for d in days if d.get("running_s", 0) > 1800 and d.get("day", "") >= cday]
        if len(burn) < 4:
            items.append({"key": "run_flame", "state": "learning", "n": len(burn), "need": 4, "unit": "days"})
        else:
            def by_band(ds):
                t = {}
                for d in ds:
                    for k, v in (d.get("flame_power") or {}).items():
                        x = t.setdefault(k, [0.0, 0.0])
                        x[0] += v[1]
                        x[1] += v[2]
                return {k: x[0] / x[1] for k, x in t.items() if x[1] >= 600}
            old, new = by_band(burn[:-2]), by_band(burn[-2:])
            common = [k for k in new if k in old]
            if not common:
                items.append({"key": "run_flame", "state": "learning", "n": len(burn), "need": 4, "unit": "days"})
            else:
                o = statistics.mean(old[k] for k in common)
                n = statistics.mean(new[k] for k in common)
                items.append({"key": "run_flame", "state": "worse" if o - n > max(10, 0.12 * o) else "ok",
                              "now": round(n), "normal": round(o), "unit": "", "n": len(burn)})
        known = [i for i in items if i["state"] != "learning"]
        worse = [i for i in known if i["state"] == "worse"]
        status = "learning" if not known else ("clean" if len(worse) >= 2 else ("watch" if worse else "ok"))
        return {"status": status, "items": items, "cleaned_at": cleaned or None, "checks": self.checks(items, days)}

    def checks(self, items, days):
        """THE burner-health checklist, the one every page and the share picture show: time to flame, flame sensor,
        fan, ash plate, short burns. Each: {"k": check, "st": "ok" / "bad" / "learn", "why": which text, "p": numbers}.
        Built from the health items above, the fan monitor, the ash plate and the day summaries."""
        it = {i["key"]: i for i in items}
        known = lambda i: bool(i) and i.get("state") != "learning"
        worse = lambda i: known(i) and i["state"] == "worse"
        out = []
        tf, fl = it.get("to_flame"), it.get("failed")
        if worse(fl):
            out.append({"k": "light", "st": "bad", "why": "fail", "p": {"a": fl["now"], "n": fl["n"]}})
        elif worse(tf):
            out.append({"k": "light", "st": "bad", "why": "slow", "p": {"a": tf["now"], "usual": tf["normal"]}})
        elif known(tf):                            # the latest starts (median of 3), against the usual
            out.append({"k": "light", "st": "ok", "why": "ok", "p": {"a": tf["now"], "usual": tf["normal"]}})   # the last 3 starts (median), the same value the check judges
        else:
            out.append({"k": "light", "st": "learn"})
        a, b = it.get("start_flame"), it.get("run_flame")
        if worse(a) or worse(b):
            out.append({"k": "flame", "st": "bad", "why": "bad"})
        elif known(a) or known(b):
            out.append({"k": "flame", "st": "ok", "why": "ok"})
        else:
            out.append({"k": "flame", "st": "learn"})
        try:
            fan = getattr(self.mod, "fan", None)
            lv = [x for x in ((fan.analyse() if fan else {}).get("levels") or []) if x.get("change_pct") is not None]
        except Exception:
            lv = []
        if not lv:
            out.append({"k": "fan", "st": "learn"})
        else:
            # the board holds the fan at a set speed, so the speed at the same setting barely moves from day to
            # day; a clear drop means the fan cannot keep up any more. "a" = the biggest drop (%), 0 when none.
            drop = max(0.0, -min(x["change_pct"] for x in lv))
            slow = drop >= 8
            out.append({"k": "fan", "st": "bad" if slow else "ok", "why": "bad" if slow else "ok",
                        "p": {"a": round(drop, 1), "n": sum(x.get("n") or 0 for x in lv)}})
        try:
            ash = getattr(self.mod, "ash", None)
            A = ash.feed() if ash else {}
        except Exception:
            A = {}
        s = [x[1] for x in (A.get("turn_hist") or []) if x[1] is not None and x[1] > 0]
        if A.get("problem"):
            out.append({"k": "ash", "st": "bad", "why": "prob"})
        elif len(s) < 10:
            out.append({"k": "ash", "st": "learn"})
        else:
            base, now = statistics.median(s[:max(5, len(s) - 5)]), statistics.median(s[-5:])
            slow = now > base * 1.3 and now - base >= 1
            out.append({"k": "ash", "st": "bad" if slow else "ok", "why": "slow" if slow else "ok",
                        "p": {"a": round(now, 1), "usual": round(base, 1)}})
        runs = [r for d in [d for d in (days or []) if d.get("runs")][-7:] for r in d["runs"] if r is not None]
        sh = short_burns(runs)
        if len(runs) < 3:
            out.append({"k": "short", "st": "learn"})
        elif sh >= 3 and sh / len(runs) >= 0.4:
            out.append({"k": "short", "st": "bad", "why": "bad", "p": {"a": sh, "n": len(runs)}})
        else:
            out.append({"k": "short", "st": "ok", "why": "ok"})
        burnt = [d for d in (days or []) if (d.get("running_s") or 0) > 0]
        resc = rescue_list(burnt[-RESCUE_DAYS:])
        failed = sum(1 for x in resc if x[1] == "failed")
        # fire rescues: the boiler lost the flame while burning and tried to relight it. One that worked is "ok" (noted);
        # one that ended in an alarm, or 2 or more in a week, is worth a look. list = the newest 10 of all the days
        rp = {"a": len(resc), "f": failed, "d": len(burnt[-RESCUE_DAYS:]), "list": rescue_list(burnt)[:10]}
        if not burnt:
            out.append({"k": "rescue", "st": "learn"})
        elif failed or len(resc) >= 2:
            out.append({"k": "rescue", "st": "bad", "why": "fail" if failed else "many", "p": rp})
        else:
            out.append({"k": "rescue", "st": "ok", "why": "one" if resc else "none", "p": rp})
        return out

    def report_days(self):
        files = hist_files(self.store.dir)[-60:] if self.store and self.store.dir else []
        out = []
        for day, p in files:
            s = self._day_summary(p, day)
            if s:
                s.setdefault("day", day)
                out.append(s)
        return out

    def report(self):
        if not (self.store and self.store.dir):
            return {"days": [], "suggestions": [], "note": "no data dir"}
        reset = self.settings.get("optimiser").get("reset_epoch") or 0
        reset_day = datetime.date.fromtimestamp(reset).isoformat() if reset else ""
        days = []
        for day, p in reversed(hist_files(self.store.dir)[-120:]):     # newest first, stop at 28 days that actually ran
            if day < reset_day:
                break
            s = self._day_summary(p, day)
            if s and (s["running_s"] > 0 or day == datetime.date.today().isoformat()):
                days.append(s)
            if len([d for d in days if d["running_s"] > 0]) >= 28:
                break
        days.reverse()
        corr = self.correction()
        # each day also carries fuel_kg, burn_h and recorded_h (day_numbers) and short_burns (how many of its
        # burns were shorter than SHORT_BURN_MIN): the pages show those values
        days = [dict(d, short_burns=short_burns(d.get("runs")), **day_numbers(d, corr)) for d in days]
        per_day = self.per_day()
        return {"days": days, "suggestions": self.suggest(days, per_day), "reset_epoch": reset, "per_day": per_day,
                "flame_power": flame_by_power(days), "stock": self.pellet_stock(), "ash": self.ash_status(),
                "health": self._safe_health(days), "lux": self._lux(),
                "window": "last 28 days with burning (idle days are skipped)"}

    def _lux(self):
        """The flame sensor's per-band table (Burner health -> Flame sensor), or None."""
        lux = getattr(self.mod, "lux", None)
        try:
            return lux.summary() if lux else None
        except Exception as e:                      # never breaks the report
            self.log(f"# flame sensor error: {e!r}")
            return None

    def _safe_health(self, days):
        try:
            return self.health(days)
        except Exception as e:                      # a health bug must never break the report
            self.log(f"# health error: {e!r}")
            return {"status": "learning", "items": [], "checks": [], "error": str(e)}

    def suggest(self, days, per_day=None):
        ran = [d for d in days if d["running_s"] > 0]
        if not ran:
            return [{"level": "info", "key": "no_data", "p": {}, "title": "No burning recorded yet",
                     "text": "Suggestions appear after a few days of real burning."}]
        cfg = ((self.mod.latest_cfg or {}).get("raw") or {})
        sch = self.settings.get("schedule")
        out = []
        runs = [r for d in ran for r in d["runs"]]
        starts = sum(d["starts"] for d in ran) / len(ran)
        # each suggestion carries key + p (numbers) so the dashboard writes it in the selected language;
        # title/text stay as an English fallback (MQTT, logs)
        if len(runs) >= 3:
            avg = statistics.mean(runs)
            if avg < sch["short_run_min"]:
                p = {"avg": round(avg), "target": sch["short_run_min"], "stop": cfg.get("005", "?"),
                     "max": cfg.get("031", "?"), "pause": cfg.get("006", "?")}
                out.append({"level": "warn", "key": "short_runs", "p": p, "title": "Short runs",
                    "text": f"Runs last {p['avg']} min on average (target > {p['target']} min). Many short runs wear "
                            f"the igniter and the hearth. Try lowering MINIMUM MODE → STOP AT (now {p['stop']} %) or "
                            f"PERFORMANCE (max power, now {p['max']} %), or a longer PAUSE (now {p['pause']} h)."})
        if starts > sch["max_starts_day"]:
            p = {"starts": round(starts, 1), "limit": sch["max_starts_day"]}
            out.append({"level": "warn", "key": "many_starts", "p": p, "title": "Many starts per day",
                "text": f"{p['starts']} starts per day on average (limit {p['limit']}). "
                        f"See 'Short runs'; a longer PAUSE also reduces starts."})
        resc = sum(d["rescues"] for d in ran)
        if resc:
            # "the period" = the burning days of this report (up to 28, idle days skipped): from day `from`, `days` days.
            # last = the newest 3 rescues [epoch, "ok" / "failed" / "stopped" / None], newest first; ok = how many worked
            at = rescue_list(ran)
            p = {"n": resc, "days": len(ran), "from": ran[0].get("day"), "last": at[:3],
                 "ok": sum(1 for x in at if x[1] == "ok")}
            out.append({"level": "warn", "key": "rescues", "p": p, "title": "Fire rescues",
                "text": f"{resc} fire rescue(s) in the last {len(ran)} burning days: the flame was lost while burning. "
                        f"Check pellet feed (silo, bridging, auger) and clean the flame sensor."})
        alarms = {}
        for d in ran:
            for k, v in d["alarms"].items():
                alarms[k] = alarms.get(k, 0) + v
        for k, v in sorted(alarms.items()):
            out.append({"level": "warn", "key": "alarm", "p": {"a": _int(k), "n": v}, "title": f"Alarm {k} × {v}",
                        "text": ALARM_HINTS.get(_int(k), "see the alarm guide")})
        share = [d["drift_power_max_share"] for d in ran if d["drift_power_max_share"] is not None]
        if share and statistics.mean(share) > 0.5:
            out.append({"level": "info", "key": "full_power", "p": {"max": cfg.get("031", "?")},
                "title": "Often at full power",
                "text": "More than half of the burning time is at >= 95 % power: on cold days the boiler is at its "
                        f"limit, or PERFORMANCE (max power, {cfg.get('031', '?')} %) is set low."})
        if not out:
            kg = (per_day or self.per_day())["kg"]          # the one "pellets per day" (per_day_days)
            if kg is None:                                   # no finished day yet: what there is so far
                kg = round(statistics.mean(day_fuel(d) for d in ran) * self.correction(), 1)
            p = {"kg": kg}
            out.append({"level": "ok", "key": "ok", "p": p, "title": "No problems found",
                        "text": f"Average {p['kg']} kg pellets per burning day."})
        return out


# ============================================================================ hot-water tank (VVB) sensor
class VvbInput:
    """Hot-water tank temperature from the owner's own sensor -> answered to the boiler's reg 0 reads (the boiler
    shows it as "VVB: xx °C" and uses it for VVB PRIORITET / the VVB pump).

    Safety: a falsely LOW tank temperature can make the boiler start a normal burn for
    hot water; a falsely HIGH one only means no hot-water priority. So: readings outside -20..100 °C are refused,
    and when no fresh reading has arrived for max_age_min the boiler gets safe_c (default 75 °C = "tank is warm").
    Off (default) -> the responder's fixed --reg0 value, exactly as before."""

    W1_GLOB = "/sys/bus/w1/devices/28-*/w1_slave"

    def __init__(self, settings, log, emit):
        self.settings, self.log, self.emit = settings, log, emit
        self.c, self.at, self.src = None, 0.0, None
        self.last_poll = 0.0
        self.was_fresh = False
        self.lock = threading.Lock()

    def cfg(self):
        return self.settings.get("vvb") or DEFAULTS["vvb"]

    def put(self, value, src):
        """A new reading (MQTT / HTTP / 1-wire). Returns (ok, message)."""
        c = _f(value)
        if c is None or c != c or not -20 <= c <= 100:
            return False, "tank temperature must be a number between -20 and 100 °C"
        cfg = self.cfg()
        if not cfg.get("enabled"):
            return False, "hot-water tank sensor is switched off (settings)"
        if src != cfg.get("source"):
            return False, f"the tank sensor source is set to {cfg.get('source')}, not {src}"
        with self.lock:
            self.c, self.at, self.src = round(c, 1), time.time(), src
        return True, f"tank {self.c} °C"

    def fresh(self, now=None):
        cfg = self.cfg()
        return self.c is not None and (now or time.time()) - self.at <= cfg["max_age_min"] * 60

    def reg0(self, default):
        """The value to answer to a reg 0 read."""
        cfg = self.cfg()
        if not cfg.get("enabled"):
            return default
        return self.c if self.fresh() else float(cfg["safe_c"])

    def tick(self):
        cfg = self.cfg()
        if not cfg.get("enabled"):
            self.was_fresh = False
            return
        now = time.time()
        if cfg["source"] == "onewire" and now - self.last_poll >= 30:
            self.last_poll = now
            v = self.read_onewire(cfg.get("onewire_id"))
            if v is not None:
                self.put(v, "onewire")
        f = self.fresh(now)
        if self.was_fresh and not f:
            self.emit({"type": "warning", "source": "vvb", "kind": "stale", "epoch": now, "t": _iso(now),
                       "p": {"min": cfg["max_age_min"], "safe": cfg["safe_c"]},
                       "text": f"No reading from the hot-water tank sensor for {cfg['max_age_min']} min - "
                               f"the boiler is told {cfg['safe_c']} °C (no hot-water demand) until it is back"})
        self.was_fresh = f

    def read_onewire(self, oid=""):
        """DS18B20 via the Linux w1-therm driver: '... YES' on line 1, 't=52312' on line 2."""
        paths = [f"/sys/bus/w1/devices/{oid}/w1_slave"] if oid else sorted(glob.glob(self.W1_GLOB))[:1]
        for p in paths:
            try:
                with open(p, encoding="ascii", errors="replace") as fh:
                    lines = fh.read().splitlines()
                if len(lines) >= 2 and lines[0].strip().endswith("YES") and "t=" in lines[1]:
                    c = int(lines[1].split("t=")[1]) / 1000.0
                    if c != 85.0:                    # 85.000 = DS18B20 power-on value, not a reading
                        return c
            except (OSError, ValueError, IndexError):
                pass
        return None

    def feed(self):
        cfg = self.cfg()
        now = time.time()
        return {"enabled": bool(cfg.get("enabled")), "source": cfg.get("source"), "c": self.c,
                "age_s": round(now - self.at) if self.c is not None else None, "fresh": self.fresh(now),
                "sent_c": self.reg0(None), "safe_c": cfg.get("safe_c"), "max_age_min": cfg.get("max_age_min")}


# ============================================================================ ash plate (ASKEPLADEKONTAKT)
class AshPlate:
    """Watches the ash-plate home switch (status field 012 'din2': 0 = plate at home, 1 = out / turning).
    Confirmed on a real burner (0 at rest, 1 during the OPSTART turn) and on the boiler's
    TEST MENU -> INPUTS page. The boiler already raises ALARM 6 itself; this adds plain-language
    warnings around it. Read-only: it never sends anything to the boiler.

    Status comes every ~10 s, so short turns (3-15 s) can be missed and times are coarse."""

    OUT_MAX_S = 90          # the boiler gives up after 60 s for a whole job; +30 s for the 10-s sampling
    NEAR_S = 120            # an alarm this soon after a turn started counts as "during a turn"
    GAP_S = 60              # rows further apart than this: don't count time across the gap
    QUIET_STARTS = 3        # warn when no turn was seen at this many starts in a row
    HOME_VALUE = 0          # 0 = switch closed = plate home (0 at rest, 1 while turning). 1 at rest = circuit
                            # open: loose wire / bad switch (e.g. a test board with no switch) -> ALARM 6

    SLOW_FACTOR, SLOW_MIN_S = 1.3, 2.0   # "slower": median of the last 10 turns 30 % and 2 s above the earlier turns

    def __init__(self, emit, log=print, data_dir=None):
        self.emit, self.log = emit, log
        self.path = os.path.join(data_dir, "ash_turns.jsonl") if data_dir else None
        self.turn_hist = collections.deque(maxlen=2000)    # timed turns (fast data only): {"epoch", "s", "state"}
        self.slow_warned = False
        if self.path:
            self.turn_hist.extend(rows_of(self.path))
        self.contact = None             # 0 = plate home, 1 = plate out (after HOME_VALUE)
        self.raw = None                 # the switch value as the boiler sends it
        self.since = None               # epoch the contact got its current value
        self.out_s = 0.0                # how long it has been out (sums rows, skips gaps)
        self.last_epoch = None
        self.rate_s = None
        self.last_turn = None           # epoch of the last 0 -> 1
        self.last_home = None           # epoch of the last 1 -> 0
        self.last_out_s = None          # how long the last turn kept the plate out (s)
        self.turns = collections.deque(maxlen=200)
        self.edges = collections.deque(maxlen=400)   # every switch change {t, v, state} for the web's check / turn list
        self.prev_state = self.prev_alarm = None
        self.start_at = None            # OPSTART seen, waiting for a turn
        self.quiet_starts = 0
        self.stuck_warned = False
        self.quiet_warned = False
        self.problem = None             # {"kind", "text", "epoch"} until the plate is home again / next good turn
        self.lock = threading.Lock()
        self._restore_turns()

    def _restore_turns(self, now=None):
        """After a restart: refill the 24-h turn count and the last turn from the saved turn history
        (ash_turns.jsonl, epoch = plate home again, s = seconds out), so 'turns in 24 h' is not 0."""
        now = now or time.time()
        recent = sorted((r for r in self.turn_hist
                         if isinstance(r, dict) and isinstance(r.get("epoch"), (int, float))
                         and isinstance(r.get("s"), (int, float)) and 0 <= now - r["epoch"] <= 86400),
                        key=lambda r: r["epoch"])
        for r in recent:
            self.turns.append(r["epoch"] - r["s"])
        if recent:
            last = recent[-1]
            self.last_turn, self.last_home, self.last_out_s = last["epoch"] - last["s"], last["epoch"], last["s"]

    def _warn(self, kind, text, now, p=None):
        self.problem = {"kind": kind, "text": text, "epoch": now, "p": p or {}}
        self.emit({"type": "warning", "source": "ash_plate", "kind": kind, "epoch": now, "t": _iso(now),
                   "p": p or {}, "text": text})

    def on_row(self, row):
        c = row.get("din2")
        if c not in (0, 1, True, False):
            return
        raw = int(c)
        c = 0 if raw == self.HOME_VALUE else 1          # from here on: 0 = home, 1 = out
        self.raw = raw
        now = row.get("epoch") or time.time()
        st, al = row.get("state"), int(row.get("alarm") or 0)
        with self.lock:
            dt = (now - self.last_epoch) if self.last_epoch else 0
            self.last_epoch = now
            if 0 < dt <= self.GAP_S:                 # data rate, smoothed (10 s normal, ~1 s with fast data)
                self.rate_s = round(dt if self.rate_s is None else 0.8 * self.rate_s + 0.2 * dt, 1)
            if self.contact is None or c != self.contact:
                if self.contact is not None:
                    self.edges.append({"t": round(now, 1), "v": c, "raw": raw, "state": st})
                if c == 1 and self.contact == 0:
                    self.last_turn = now
                    self.turns.append(now)
                    self.start_at, self.quiet_starts, self.quiet_warned = None, 0, False
                    if self.problem and self.problem["kind"] == "no_turns":
                        self.problem = None
                if c == 0 and self.contact == 1:
                    self.last_home = now
                    self.last_out_s = round(now - self.since, 1) if self.since else None   # exact only with fast data
                    if self.last_out_s is not None and self.rate_s is not None and self.rate_s <= 2.5:
                        self._timed_turn(now, self.last_out_s, st)
                    self.stuck_warned = False
                    if self.problem and self.problem["kind"] in ("stuck_out", "alarm_out"):
                        self.problem = None
                self.contact, self.since, self.out_s = c, now, 0.0
            elif c == 1 and dt <= self.GAP_S:
                self.out_s += dt

            # 1) plate out of home for too long
            if c == 1 and self.out_s > self.OUT_MAX_S and not self.stuck_warned and al == 0:
                self.stuck_warned = True
                self._warn("stuck_out", f"The ash plate has not come back home for {int(self.out_s)} s. "
                                        "Check that nothing blocks it, and check the ash-plate switch.", now,
                           {"s": int(self.out_s)})

            # 2) the boiler goes into an alarm during / right after a turn
            if al and not self.prev_alarm:
                near = self.last_turn is not None and now - self.last_turn <= self.NEAR_S
                if c == 1:
                    txt = (f"Alarm {al} came while the ash plate was out of its home position. "
                           + ("The plate is stuck or the switch does not close: check for ash or clinker in the way, "
                              "and the switch." if al == 6 else "Check the ash plate as well when you look at the boiler."))
                    self._warn("alarm_out", txt, now, {"alarm": al, "contact": c})
                elif al == 6 and not near:
                    self._warn("alarm_no_turn", "Ash-plate alarm, and the plate was not seen leaving home: "
                                                "check the ash-plate motor, its chain/arm and the plug.", now,
                               {"alarm": al, "contact": c})
                elif near:
                    self._warn("alarm_after_turn", f"Alarm {al} came {int(now - self.last_turn)} s after the ash plate "
                                                   "turned. The plate is home again now.", now,
                               {"alarm": al, "contact": c, "s": int(now - self.last_turn)})

            # 3) starts without a visible turn (the boiler turns the plate once at every start)
            if st == 2 and self.prev_state != 2 and c == 0:   # (stuck out is reported above, not here)
                if self.start_at is not None:          # previous start never showed a turn
                    self.quiet_starts += 1
                self.start_at = now
            if self.start_at and st not in (2, 3) and now - self.start_at > self.NEAR_S:
                self.quiet_starts += 1
                self.start_at = None
            if self.quiet_starts >= self.QUIET_STARTS and not self.quiet_warned:
                self.quiet_warned = True
                self._warn("no_turns", f"The ash plate was not seen turning at the last {self.quiet_starts} starts. "
                                       "Data comes every 10 s, so a quick turn can be missed — check it on "
                                       "TEST MENU → INPUTS if this keeps coming.", now, {"n": self.quiet_starts})
            self.prev_state, self.prev_alarm = st, al

    def _timed_turn(self, now, secs, st):
        """A turn timed with fast data: keep it (memory + disk) and check for slowly longer turns (wear, stiffness)."""
        if not 1 <= secs <= 120:
            return
        rec = {"epoch": round(now, 1), "s": secs, "state": st}
        self.turn_hist.append(rec)
        if self.path:
            append_jsonl(self.path, rec)
        h = [r["s"] for r in self.turn_hist]
        if len(h) >= 30:
            recent, before = statistics.median(h[-10:]), statistics.median(h[-60:-10])
            slow = recent >= before * self.SLOW_FACTOR and recent - before >= self.SLOW_MIN_S
            if slow and not self.slow_warned:
                self.slow_warned = True
                self._warn("slow_turns", f"The ash plate now takes about {recent:.0f} s per turn, it used to take "
                                         f"{before:.0f} s. It may be getting stiff or worn, or ash is building up under it.",
                           now, {"s": round(recent), "was": round(before)})
            elif not slow:
                self.slow_warned = False

    def feed(self):
        now = time.time()
        with self.lock:
            return {"turn_hist": [[r["epoch"], r["s"]] for r in list(self.turn_hist)[-300:]],
                    "contact": self.raw, "out": self.contact, "home_value": self.HOME_VALUE,
                    "home": None if self.contact is None else self.contact == 0,
                    "since": self.since, "out_s": round(self.out_s) if self.contact == 1 else 0,
                    "last_turn": self.last_turn, "last_home": self.last_home, "last_out_s": self.last_out_s,
                    "rate_s": self.rate_s, "last_epoch": self.last_epoch,
                    "edges": list(self.edges)[-120:],
                    "turns_24h": sum(1 for t in self.turns if now - t <= 86400),
                    "problem": self.problem}


# ============================================================================ warm-period mode (own minimum mode)
class WarmMode:
    """The web's own heating rules (opt-in), replacing the boiler's minimum mode in daily use.

    Heating plan = periods {name, start, end, days, kind, on}:
      kind "keep"  : at the start the burner is started (always, also when the boiler is warm); during the period it
                     is NOT stopped at low power, and it is started again if it stopped and the boiler has cooled
                     keep_restart_c below the target (no pointless re-lighting of a warm boiler).
      kind "start" : one start at the period start, then the normal rules.
    Outside periods: RUNNING at or below STOP AT for stop_after_min -> STOP (the house may cool; no 20-min burn-out /
    re-lighting loop). Optional restart when the pause (pause_h, 0 = never) after our own stop has run out.
    No periods set -> the boost times (the boiler's BOOST 1/2 or own) act as "start" periods of start_window_min.
    Never acts on an ALARM or on stale data; max_attempts per start; the boiler's own WAITING is ended by stop->start."""

    KINDS = ("keep", "start", "warmby")
    # "warm by": lead = lighting time + minutes per °C from the boiler temperature at RUNNING up to "warm".
    # The boiler already warms during the long lighting, so from a cold boiler the lighting time is most of the lead.
    IGN_MIN = 35          # minutes from start until RUNNING (cold firebox); learned
    RUN_C = 52            # boiler °C when RUNNING begins; learned
    HEAT_K = 1.5          # minutes per °C after RUNNING; learned
    WARM_BELOW_C = 5      # "warm" = setpoint - 5 °C (the last degrees come slowly while the boiler turns down)
    LEAD_MIN, LEAD_MAX = 15, 90

    def __init__(self, settings, tracker, mod, send, log, emit):
        self.settings, self.tracker, self.mod, self.send, self.log, self.emit = settings, tracker, mod, send, log, emit
        self.low_since = None          # RUNNING at/below STOP AT since
        self.rest_since = None         # we stopped it at
        self.stopped_at = None         # last time the burner was seen stopping (any cause), for the keep min-off
        # period start already served; kept in settings, so a program restart inside the window never starts it again
        try:
            self._done_window = (settings.get("warmstate") or {}).get("done_window")
        except Exception:
            self._done_window = None
        self.pending_key = None        # period start we are trying
        self.attempts, self.last_attempt, self.attempt_state = 0, 0.0, None
        self.stop_sent_t = 0.0
        self.last_eval = 0.0
        self.prev_state = None
        self.learn = None              # warm-by start: {"t","bc","sp","t_run","bc_run"} until the boiler is warm
        self.status = {"code": "off", "text": "off"}
        self.history = collections.deque(maxlen=50)     # what the mode did: {"epoch","what","why"}

    @property
    def done_window(self):
        return self.__dict__.get("_done_window")

    @done_window.setter
    def done_window(self, key):
        if key == self.__dict__.get("_done_window"):
            return
        self._done_window = key
        try:
            self.settings.set_internal("warmstate", "done_window", key)
            self.settings.set_internal("warmstate", "at", round(time.time()))
        except Exception:
            pass

    # ------------------------------------------------------------------ settings
    def cfg(self):
        """Effective values; STOP AT / PAUSE / BOOST from the boiler's own minimum mode when use_board."""
        w = copy.deepcopy(self.settings.get("warm") or DEFAULTS["warm"])
        for k, v in DEFAULTS["warm"].items():
            w.setdefault(k, copy.deepcopy(v))
        raw = ((getattr(self.mod, "latest_cfg", None) or {}).get("raw")) or {}
        w["source"] = "web"
        if w.get("use_board") and raw:
            try:
                if raw.get("005") is not None:
                    w["stop_pct"] = int(raw["005"])
                if raw.get("006") is not None and int(raw["006"]) > 0 and not w.get("periods"):
                    w["pause_h"] = int(raw["006"])
                bs = [int(raw[k]) for k in ("007", "008") if raw.get(k) is not None and 0 < int(raw[k]) < 1440]
                if bs:
                    w["boosts"] = [f"{m // 60:02d}:{m % 60:02d}" for m in bs]
                w["source"] = "board"
            except (TypeError, ValueError):
                pass
        if not w.get("periods"):                       # no plan of our own: the boost times as start periods
            w["plan"] = [{"name": f"Start {b}", "start": b,
                          "end": f"{(_mins(b) + w['start_window_min']) // 60 % 24:02d}:{(_mins(b) + w['start_window_min']) % 60:02d}",
                          "days": list(range(7)), "kind": "start", "on": True} for b in w["boosts"]]
        else:
            w["plan"] = []
            for p in w["periods"]:
                if not p.get("on", True):
                    continue
                if p.get("kind") == "warmby":
                    # "warm by HH:MM": start early enough: learned lighting time + warm-up from the
                    # boiler temperature predicted for the start time
                    t = _mins(p["start"])
                    _n = datetime.datetime.fromtimestamp(time.time())
                    _tg = datetime.datetime.combine(_n.date(), datetime.time(t // 60, t % 60))
                    if _tg.timestamp() < _n.timestamp() - 1800:
                        _tg += datetime.timedelta(days=1)
                    lead = self.lead_for(w, _tg.timestamp())
                    q = dict(p, kind="start", target=p["start"], lead=lead, warmby=True,
                             start=f"{(t - lead) % 1440 // 60:02d}:{(t - lead) % 1440 % 60:02d}",
                             end=f"{(t + 30) % 1440 // 60:02d}:{(t + 30) % 1440 % 60:02d}", window=lead + 30)
                    if (t - lead) < 0:                 # the early start is the evening before: days shift back
                        q["days"] = [(d - 1) % 7 for d in p.get("days", list(range(7)))]
                    w["plan"].append(q)
                else:
                    w["plan"].append(p)
        return w

    ROOM_C = 18            # a stopped boiler cools towards about room temperature

    def cool_tau_h(self, w):
        """How fast this boiler cools when stopped (hours, Newton cooling), learned from its own nights; 4 h until then."""
        ls = [x for x in (w.get("cool_learn") or []) if x.get("tau_h")]
        return statistics.median(x["tau_h"] for x in ls[-7:]) if ls else 4.0

    def predict_bc(self, w, bc, hours):
        """Boiler temperature after `hours` without burning."""
        if bc is None or hours <= 0:
            return bc
        r = self.ROOM_C
        return r + max(0.0, bc - r) * math.exp(-hours / self.cool_tau_h(w))

    def lead_for(self, w, target, now=None):
        """Lead for a warm-by `target` (epoch): with the burner off, use the boiler temperature it will have cooled to
        by the start time (solved by a few rounds), so the start shown in the evening is the real one."""
        now = now or time.time()
        f = ((getattr(self.mod, "latest", None) or {}).get("fields")) or {}
        bc, st = _f(f.get("boiler_c")), _int(f.get("state"))
        if bc is None or st not in (0, 1) or target <= now:
            return self.lead_min(w)
        lead = self.lead_min(w)
        for _ in range(6):
            ts = max(now, target - lead * 60)
            lead = self.lead_min(w, bc=self.predict_bc(w, bc, (ts - now) / 3600))
        return lead

    def lead_min(self, w, bc=None):
        """Minutes before a "warm by" time to start: lighting time (until RUNNING) + minutes per °C from the
        boiler temperature at RUNNING (or the start temperature, if warmer) up to warm. Rounded up to 5 min."""
        f = ((getattr(self.mod, "latest", None) or {}).get("fields")) or {}
        bc, sp = (_f(f.get("boiler_c")) if bc is None else bc), self._setpoint()
        if bc is None or sp is None:
            return 60
        # learned values (median of the last 10 warm-by starts) replace the defaults. After the boiler type
        # changed: only starts since then, and the defaults until there are 3 of them.
        lr = w.get("warm_learn") or []
        bs = baseline_since(getattr(self, "settings", None))
        if bs:
            lr = [x for x in lr if (x.get("epoch") or 0) >= bs]
            lr = lr if len(lr) >= TYPICAL_MIN_N else []
        lr = lr[-10:]

        def med(k, d):
            v = [x[k] for x in lr if isinstance(x.get(k), (int, float))]
            return statistics.median(v) if v else d
        ign, run_c, kh = med("ign", self.IGN_MIN), med("run_c", self.RUN_C), med("kh", self.HEAT_K)
        lead = ign + kh * max(0.0, sp - self.WARM_BELOW_C - max(bc, run_c))
        return int(max(self.LEAD_MIN, min(self.LEAD_MAX, math.ceil(lead / 5 - 1e-9) * 5)))

    def _setpoint(self):
        f = ((getattr(self.mod, "latest_cfg", None) or {}).get("fields")) or {}
        return _f(f.get("setpoint_c"))

    # ------------------------------------------------------------------ plan helpers
    @staticmethod
    def _active(p, dt):
        """Key of period p if it covers datetime dt (periods may cross midnight; days = start day), else None."""
        m = dt.hour * 60 + dt.minute
        a, b = _mins(p["start"]), _mins(p["end"])
        days = p.get("days", list(range(7)))
        tag = p.get("target", p["start"])            # warm-by: the start moves with the boiler temperature, the key not
        if a < b:
            if dt.weekday() in days and a <= m < b:
                return f"{dt.date()}#{tag}"
        elif a > b:
            if dt.weekday() in days and m >= a:
                return f"{dt.date()}#{tag}"
            y = dt - datetime.timedelta(days=1)
            if y.weekday() in days and m < b:
                return f"{y.date()}#{tag}"
        return None

    KEEP_SOON_MIN = 30
    MIN_RUN_MIN = 20       # no web pause in the first 20 min of RUNNING
    drift_since = None
    cool = None            # {"t","bc"} from when the burner stopped, to learn how fast the boiler cools
    burn_stop = None       # "stop at" power of the period that started this burn (None = the normal limit)
    burn_name = None

    def period_soon(self, w, now=None):
        """Any period (keep warm, just start, warm by) that begins within KEEP_SOON_MIN minutes (or None).
        Periods that already cover now do not count here (they have their own rules)."""
        dt = datetime.datetime.fromtimestamp(now or time.time())
        for p in w.get("plan") or []:
            if p.get("on") is False or self._active(p, dt):
                continue
            for m in range(1, self.KEEP_SOON_MIN + 1):
                if self._active(p, dt + datetime.timedelta(minutes=m)):
                    return p
        return None

    def period_now(self, w, now=None):
        dt = datetime.datetime.fromtimestamp(now or time.time())
        for p in w["plan"]:
            k = self._active(p, dt)
            if k:
                since = (dt.hour * 60 + dt.minute - _mins(p["start"])) % 1440
                return k, p, since
        return None, None, None

    def next_start(self, w, now=None):
        """Epoch of the next planned start (next period start, or pause end after our own stop if earlier)."""
        now = now or time.time()
        dt = datetime.datetime.fromtimestamp(now)
        cands = []
        for d in range(8):
            day = (dt + datetime.timedelta(days=d)).date()
            for p in w["plan"]:
                if day.weekday() not in p.get("days", list(range(7))):
                    continue
                h, m = map(int, p["start"].split(":"))
                c = datetime.datetime.combine(day, datetime.time(h, m)).timestamp()
                if c > now:
                    cands.append(c)
            if cands:
                break
        if self.rest_since and w.get("pause_h"):
            cands.append(self.rest_since + w["pause_h"] * 3600)
        return min(cands) if cands else None

    def plan_days(self, w, days=2, now=None):
        """Plan bands for charts: [{a, b, name, kind}] from today 00:00 for `days` days."""
        now = now or time.time()
        d0 = datetime.datetime.combine(datetime.date.fromtimestamp(now), datetime.time())
        out = []
        for d in range(-1, days):
            day = d0 + datetime.timedelta(days=d)
            for p in w["plan"]:
                if day.weekday() not in p.get("days", list(range(7))):
                    continue
                a = day + datetime.timedelta(minutes=_mins(p["start"]))
                b = day + datetime.timedelta(minutes=_mins(p["end"]))
                if b <= a:
                    b += datetime.timedelta(days=1)
                out.append({"a": a.timestamp(), "b": b.timestamp(), "name": p.get("name", ""), "kind": p["kind"],
                            "warmby": bool(p.get("warmby")), "target": p.get("target"), "lead": p.get("lead")})
        return out

    # ------------------------------------------------------------------ helpers
    def _set(self, code, text, **p):
        if self.status.get("code") != code:
            self.log(f"# warm mode: {text}")
        self.status = {"code": code, "text": text, "t": time.time(), "p": p}

    def _did(self, what, why):
        now = time.time()
        self.history.append({"epoch": now, "what": what, "why": why})
        self.emit({"type": "warm", "epoch": now, "t": _iso(now), "what": what, "why": why})

    # ------------------------------------------------------------------ main loop
    def _restore_rest(self):
        """After a program restart: if the web's own last stop is still in force (no start since), remember it,
        so the page keeps saying "Web pause" and the pause timer carries on."""
        st = getattr(self.mod, "store", None)
        try:
            evs = st._load("events", 5000, days=2) if st else []
        except Exception:
            return
        last_stop = None
        for e in evs:
            if e.get("type") == "warm" and e.get("what") == "stop" \
                    and not str(e.get("why") or "").startswith("end the boiler's own pause"):   # that one is part of a start
                last_stop = e.get("epoch")
            elif last_stop and e.get("type") == "transition" and e.get("to") in (2, 3, 4) and (e.get("epoch") or 0) > last_stop:
                last_stop = None
        if last_stop and time.time() - last_stop < 2 * 86400:
            self.rest_since = last_stop
        # the cooling measurement for the 'warm by' start also survives a restart: find when the burner last
        # stopped (any stop, web or boiler) with no start since, and the boiler temperature right then
        stop_t = None
        for e in evs:
            if e.get("type") != "transition":
                continue
            if e.get("to") in (0, 1) and e.get("from") not in (0, 1, None):
                stop_t = e.get("epoch")
            elif e.get("to") in (2, 3, 4, 5, 7):
                stop_t = None
        if stop_t and getattr(self, "stopped_at", None) is None:
            self.stopped_at = stop_t                  # the keep-warm min-off also counts from a stop before the restart
        sets = getattr(self, "settings", None)
        done = [x for x in ((sets.get("warm") or {}).get("cool_learn") or []) if isinstance(x, dict)] if sets else []
        if stop_t and any(cool_stop(x) is not None and abs(cool_stop(x) - stop_t) <= COOL_SAME_STOP_S for x in done):
            stop_t = None                             # this cool-down is measured already: not again after a restart
        if stop_t and time.time() - stop_t < 12 * 3600 and getattr(self, "cool", None) is None:
            try:
                rows = st._load("hist", 20000, days=2)
            except Exception:
                rows = []
            bc = next((_f(r.get("boiler_c")) for r in rows
                       if (r.get("epoch") or 0) >= stop_t and _f(r.get("boiler_c")) is not None), None)
            if bc is not None:
                self.cool = {"t": stop_t, "bc": bc}

    def tick(self):
        now = time.time()
        if not getattr(self, "_restored", False):
            self._restored = True
            self._restore_rest()
        if now - self.last_eval < 2:
            return
        self.last_eval = now
        w = self.cfg()
        if not w.get("enabled"):
            self.low_since = None
            return self._set("off", "off")
        if not self.mod.remote_armed:
            return self._set("not_armed", "remote start/stop not armed (--allow-remote-start)")
        lat = getattr(self.mod, "latest", None) or {}
        f = lat.get("fields") or {}
        if now - (lat.get("epoch") or 0) > 60:
            return self._set("no_link", "no fresh data from the boiler")
        st, al = f.get("state"), int(f.get("alarm") or 0)
        pw, bc = _f(f.get("power_pct")), _f(f.get("boiler_c"))
        if st in (0, 1) and self.prev_state not in (0, 1, None):
            self.stopped_at = now
            self.cool = {"t": now, "bc": bc} if bc is not None else None
            self.burn_stop = self.burn_name = None            # the burn is over: its own stop limit too
        if st not in (0, 1):
            self.cool = None
        elif self.cool and bc is not None and now - self.cool["t"] >= 3 * 3600 and self.cool["bc"] - bc >= 8 \
                and bc - self.ROOM_C > 3 and self.cool["bc"] - self.ROOM_C > 3:
            try:
                tau = (now - self.cool["t"]) / 3600 / math.log((self.cool["bc"] - self.ROOM_C) / (bc - self.ROOM_C))
                if 0.5 < tau < 30:
                    ls = list((self.settings.get("warm") or {}).get("cool_learn") or [])
                    ls.append({"epoch": round(now), "stop": round(self.cool["t"]), "from_c": round(self.cool["bc"], 1),
                               "to_c": round(bc, 1), "h": round((now - self.cool["t"]) / 3600, 1), "tau_h": round(tau, 2)})
                    self.settings.set_internal("warm", "cool_learn", cool_learn_dedup(ls)[-10:])   # once per stop
            except (ValueError, ZeroDivisionError):
                pass
            self.cool = None
        if st == 4 and self.prev_state != 4:
            self.drift_since = now if self.prev_state is not None else (getattr(self, "drift_since", None) or now - self.MIN_RUN_MIN * 60)
        elif st != 4:
            self.drift_since = None
        self.prev_state = st
        if st == 6 or al:
            self.low_since = None
            return self._set("alarm", "boiler in ALARM: the plan waits")
        key, per, since_min = self.period_now(w, now)
        name = (per or {}).get("name") or ""
        keep = bool(per) and per.get("kind") == "keep"
        # ---------------------------------------------------------------- burning
        if self.learn and bc is not None and self.learn.get("sp") is not None:
            ln = self.learn
            warm_to = ln["sp"] - self.WARM_BELOW_C
            if "t_run" not in ln and st == 4:           # RUNNING: the lighting is over
                ln["t_run"], ln["bc_run"] = now, bc
            if bc >= warm_to:                          # warm: learn lighting time, °C at RUNNING, minutes per °C after
                dc = warm_to - (ln["bc"] or 0)
                mins = (now - ln["t"]) / 60
                if dc >= 8 and 5 < mins < 300:
                    hist_ = list((self.settings.get("warm") or {}).get("warm_learn") or [])[-9:]
                    rec = {"epoch": round(now), "from_c": round(ln["bc"], 1), "min": round(mins, 1)}
                    tr = ln.get("t_run")
                    if tr and 5 < (tr - ln["t"]) / 60 < 90:
                        rec["ign"] = round((tr - ln["t"]) / 60, 1)
                        rec["run_c"] = round(ln["bc_run"], 1)
                        if warm_to - ln["bc_run"] >= 3:
                            rec["kh"] = round(min(6.0, max(0.2, (now - tr) / 60 / (warm_to - ln["bc_run"]))), 2)
                    hist_.append(rec)
                    try:
                        self.settings.set_internal("warm", "warm_learn", hist_)
                    except Exception:
                        pass
                self.learn = None
            elif now - ln["t"] > 5 * 3600 or st == 6:
                self.learn = None
        if st == 4:
            self.attempts, self.last_attempt = 0, 0.0
            if key and since_min is not None and since_min < ((per or {}).get("window") or w["start_window_min"]):
                if key != self.done_window and (per or {}).get("stop_pct") and not keep:
                    self.burn_stop, self.burn_name = per["stop_pct"], name   # already burning: its limit applies now
                self.done_window = key                # the period started while burning: nothing to do
            # when ANY period (keep warm, just start, warm by) begins within 30 min, the web does
            # not pause the burner - it carries on and the boiler's own control turns it down / stops it. A web
            # pause right before a period only means an extra lighting with a hot boiler minutes later.
            if not keep:
                soon = self.period_soon(w, now)
                if soon:
                    self.low_since = None
                    return self._set("soon", f"'{soon.get('name') or ''}' starts soon: the burner carries on",
                                     name=soon.get("name") or "")
            # (a relit burner at its lowest power must not be stopped minutes after RUNNING):
            # inside a Keep warm period the web never stops the burner - the boiler stops itself (PAUSESTOP)
            if keep:
                self.low_since = None
                return self._set("keep", f"keeping warm ({name})", name=name)
            # never pause a burner that only just reached RUNNING: right after lighting the boiler often runs at
            # its lowest power for a while, which is not "the house needs no more heat"
            if self.drift_since and now - self.drift_since < self.MIN_RUN_MIN * 60:
                self.low_since = None
                return self._set("running", "burning normally")
            limit = self.burn_stop or w["stop_pct"]          # this burn's own limit (from its period), else the normal one
            if pw is not None and pw <= limit:
                self.low_since = self.low_since or now
                if now - self.low_since >= w["stop_after_min"] * 60 and now - self.stop_sent_t > 120:
                    self.stop_sent_t = now
                    self.rest_since = now
                    self.send("stop", "warm mode (low power)")
                    self._did("stop", f"power {pw:.0f} % for {w['stop_after_min']} min (stop at {limit} %)")
                    return self._set("stopping", "low power: stop sent, the house may cool down")
                return self._set("low", f"power {pw:.0f} % at/below {limit} %",
                                 left=max(0, round(w["stop_after_min"] * 60 - (now - self.low_since))))
            self.low_since = None
            return self._set("running", "burning normally")
        self.low_since = None
        if st in (2, 3, 5, 7):
            if st in (2, 3):
                self.rest_since = None
                if self.last_attempt:                  # our start worked
                    if self.pending_key:
                        self.done_window = self.pending_key
                    self.attempts, self.last_attempt = 0, 0.0
            if key and since_min is not None and since_min < ((per or {}).get("window") or w["start_window_min"]):
                self.done_window = key
            return self._set("busy", f"boiler busy ({STATE_LABELS.get(st)})")
        # ---------------------------------------------------------------- start decision (STOPPET / WAITING)
        why, wkey = None, None
        sp = self._setpoint()
        win = (per or {}).get("window") or w["start_window_min"]
        if key and key != self.done_window and since_min is not None and since_min < win and self.rest_since:
            # we stopped the boiler inside this window (e.g. by the period's own stop limit): the window is served
            t0 = datetime.datetime.fromtimestamp(now).replace(second=0, microsecond=0).timestamp() - since_min * 60
            if self.rest_since >= t0:
                self.done_window = key
        if key and key != self.done_window and since_min is not None and since_min < win:
            why, wkey = (f"warm by {per['target']}: {name}" if per.get("warmby") else f"plan: {name or per['start']}"), key
            if per.get("warmby"):
                self.learn = {"t": now, "bc": bc, "sp": sp}
        elif keep and sp is not None and bc is not None and bc <= sp - w["keep_restart_c"] \
                and (not self.stopped_at or now - self.stopped_at >= w["keep_min_off_min"] * 60):
            why = f"keep warm: {name} ({bc:.0f} °C)"
        elif not per and self.rest_since and w.get("pause_h") and now - self.rest_since >= w["pause_h"] * 3600:
            why = "pause over"
        elif not keep and w.get("min_boiler_c") and bc is not None and bc < w["min_boiler_c"] \
                and (not self.stopped_at or now - self.stopped_at >= w["keep_min_off_min"] * 60):
            why = f"boiler below {w['min_boiler_c']} °C ({bc:.0f} °C)"            # never colder than
        nxt = self.next_start(w, now)
        if not why:
            self.attempts, self.last_attempt = 0, 0.0
            if keep:
                return self._set("keep_wait", f"in '{name}': waiting until the boiler cools {w['keep_restart_c']} °C",
                                 name=name, next=nxt, c=w["keep_restart_c"])
            return self._set("resting", "resting until the next start", next=nxt, by_web=bool(self.rest_since))
        if self.last_attempt and now - self.last_attempt < w["retry_wait_s"]:
            return self._set("pending", f"start sent, waiting for the boiler (attempt {self.attempts}/{w['max_attempts']})",
                             n=self.attempts, max=w["max_attempts"])
        if self.attempts >= w["max_attempts"]:
            if wkey:
                self.done_window = wkey
            self.stopped_at = now                     # keep-warm: wait min-off before trying again
            self.emit({"type": "warning", "source": "warm", "kind": "start_failed", "epoch": now, "t": _iso(now),
                       "p": {"n": self.attempts}, "text": f"heating plan: no start after {self.attempts} attempts"})
            self.attempts, self.last_attempt = 0, 0.0
            return self._set("gave_up", "gave up starting; tries again later")
        self.attempts += 1
        self.last_attempt, self.attempt_state = now, st
        if st == 1:                                   # the boiler's own minimum-mode pause: end it first
            self.send("stop", "warm mode (end WAITING)")
            self.last_attempt = now - w["retry_wait_s"] + 40
            self._did("stop", f"end the boiler's own pause ({why})")
            return self._set("pending", "ending the boiler's pause, start follows")
        self.pending_key = wkey
        # a Just start / Warm by period with its own "stop at" power: that limit holds for the burn it starts
        self.burn_stop = (per or {}).get("stop_pct") if wkey else None
        self.burn_name = name if self.burn_stop else None
        self.send("start", "warm mode")
        self._did("start", why)
        self._set("pending", f"start sent ({why})", n=self.attempts, max=w["max_attempts"])

    def _next_warm_lead(self, w, now=None):
        """Lead of the next "warm by" period, the same one its planned start uses (so the plan note matches the next start)."""
        now = datetime.datetime.fromtimestamp(now or time.time())
        nm = now.hour * 60 + now.minute
        ws = [p for p in w.get("plan") or [] if p.get("warmby") and p.get("lead") is not None]
        if not ws:
            return self.lead_min(w)
        return min(ws, key=lambda p: (_mins(p["target"]) - nm) % 1440)["lead"]

    def feed(self):
        w = self.cfg()
        on = bool(w.get("enabled"))
        key, per, _ = self.period_now(w) if on else (None, None, None)
        return {"enabled": on, "status": self.status, "stop_pct": w["stop_pct"], "burn_stop": self.burn_stop, "burn_name": self.burn_name,
                "stop_after_min": w["stop_after_min"], "min_run_min": self.MIN_RUN_MIN, "pause_h": w["pause_h"], "boosts": w["boosts"],
                "periods": w.get("periods") or [], "keep_restart_c": w["keep_restart_c"],
                "min_boiler_c": w.get("min_boiler_c", 0), "lead_now": self._next_warm_lead(w) if on else None, "pump_c": w.get("pump_c", 50),
                "learned": len(w.get("warm_learn") or []),
                "start_window_min": w["start_window_min"],
                "source": w["source"], "use_board": bool(w.get("use_board")), "rest_since": self.rest_since,
                "period_now": per, "next_start": self.next_start(w) if on else None,
                "plan": self.plan_days(w) if on else [], "history": list(self.history)[-10:]}


# ============================================================================ the boiler's own minimum mode
class MinModeWatch:
    """Is the boiler's own minimum mode (MINIMUMDRIFT AKTIV) on? The board does not send that switch (cfg 005-008 are
    only its STOP VED / PAUSE / BOOST values), so it is read from behaviour: with minimum mode on, the boiler burns out
    at low power by itself and goes DRIFT -> WAITING ("low-power rule"); without it, it goes DRIFT -> PAUSESTOP, or the
    web plan stops it (DRIFT -> STOPPET, with the plan as cause).

    seen  = such a burn-out by the boiler itself in the last WINDOW_S (3 days), or longer ago with no burn that ended
            normally since (no proof yet that it is off).
    Cleared when 3 days pass with burns that ended without such a burn-out, or when the owner says "I turned it off"
    (off_at); a new burn-out after off_at shows it again. Only watches - never sends anything to the boiler."""

    WINDOW_S = 3 * 86400

    def __init__(self, settings, tracker):
        self.settings, self.tracker = settings, tracker
        tracker.listeners.append(self._on_event)

    @staticmethod
    def is_burnout(t):
        """DRIFT -> WAITING that the boiler did by itself (no command of ours / plan / MQTT as cause)."""
        return (t.get("type", "transition") == "transition" and t.get("from") == 4 and t.get("to") == 1
                and t.get("cause") in (None, "boiler") and not t.get("at_start"))

    @classmethod
    def detect(cls, transitions, last=None, off_at=None, now=None):
        now = now or time.time()
        outs = [t["epoch"] for t in transitions if t.get("epoch") and cls.is_burnout(t)]
        cands = outs + ([last] if isinstance(last, (int, float)) else [])
        last = max(cands) if cands else None
        res = {"seen": False, "last": last, "off_at": off_at, "window_h": cls.WINDOW_S // 3600}
        if not last:
            return res
        if off_at and off_at >= last:                       # the owner turned it off after the last burn-out
            return res
        if now - last >= cls.WINDOW_S:
            clean = any((t.get("epoch") or 0) > last and t.get("from") in (4, 5) and t.get("to") in (0, 1)
                        and not cls.is_burnout(t) for t in transitions)
            if clean:                                       # 3 days, and burns that ended without a burn-out
                return res
        res["seen"] = True
        return res

    def _on_event(self, ev):
        if ev.get("type") == "transition" and self.is_burnout(ev) and ev.get("epoch"):
            last = (self.settings.get("minmode") or {}).get("last") or 0
            if ev["epoch"] > last:                          # kept in settings: survives the transition log rolling over
                self.settings.set_internal("minmode", "last", round(ev["epoch"]))

    def turned_off(self, now=None):
        self.settings.set_internal("minmode", "off_at", round(now or time.time()))

    def feed(self, now=None):
        mm = self.settings.get("minmode") or {}
        return self.detect(list(self.tracker.transitions), mm.get("last"), mm.get("off_at"), now)


class Weather:
    """Outside temperature (optional) from Open-Meteo - free, no key, no account. Runs only when the owner turns it
    on and picks a place; only that place's rounded coordinates are sent. Without internet nothing else is
    affected: the last reading is kept and shown as old. Every reading is also saved to weather.jsonl so later
    analysis can compare heat use with the weather - offline."""
    URL = "https://api.open-meteo.com/v1/forecast"
    GEO = "https://geocoding-api.open-meteo.com/v1/search"
    EVERY_S = 1800

    def __init__(self, settings, data_dir, log):
        self.settings, self.log = settings, log
        self.dir = data_dir
        self.cache_path = os.path.join(data_dir, "weather_cache.json") if data_dir else None
        self.cache, self.last_try, self.error, self.busy, self.place = {}, 0.0, None, False, None
        self.fails, self.err_logged = 0, 0.0            # failed tries in a row; when the last failure was logged
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                self.cache = json.load(fh) or {}
        except (OSError, ValueError, TypeError):
            self.cache = {}
        self.days_cache = self.days() if self.dir else {}

    def cfg(self):
        return self.settings.get("weather") or {}

    def tick(self):
        c = self.cfg()
        if not c.get("enabled") or c.get("lat") is None or self.busy:
            return
        place = (c.get("lat"), c.get("lon"))
        wait = self.EVERY_S if not self.error else 300
        if place != self.place or time.time() - self.last_try > wait:
            self.last_try, self.place, self.busy = time.time(), place, True
            threading.Thread(target=self._fetch_safe, args=place, daemon=True, name="weather").start()

    def _fetch_safe(self, lat, lon):
        try:
            self.fetch(lat, lon)
            if self.error and self.fails > 1:
                self.log(f"# weather: works again after {self.fails} failed tries")
            self.error, self.fails = None, 0
        except Exception as e:                      # no internet, service down: keep the last reading
            self.error = str(e)[:120]
            self.fails += 1
            now = time.time()
            if now - self.err_logged >= 3600:       # tries every 5 min while failing: log at most once an hour
                self.err_logged = now
                self.log(f"# weather: {self.error}" + (f" ({self.fails} failed tries in a row)" if self.fails > 1 else ""))
        finally:
            self.busy = False

    @staticmethod
    def _get(url, params, timeout=15):
        import urllib.request, urllib.parse
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": "burner-local/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def fetch(self, lat, lon):
        j = self._get(self.URL, {"latitude": lat, "longitude": lon, "current": "temperature_2m",
                                 "hourly": "temperature_2m", "past_hours": 336, "forecast_hours": 48,
                                 "timeformat": "unixtime", "timezone": "auto"})
        cur = j.get("current") or {}
        h = j.get("hourly") or {}
        hourly = [[int(t), round(float(c), 1)] for t, c in zip(h.get("time") or [], h.get("temperature_2m") or []) if c is not None]
        c = cur.get("temperature_2m")
        if c is None and not hourly:
            raise ValueError("no temperature in the answer")
        # daily average outside temperature (local days, only days that are over), kept forever in weather_days.json
        # so pellets per day can be compared with the weather later - also when the PC is offline then
        per = {}
        today = datetime.date.today().isoformat()
        for t_, c_ in hourly:
            d_ = datetime.date.fromtimestamp(t_).isoformat()
            if d_ < today:
                per.setdefault(d_, []).append(c_)
        days = self.days()
        for d_, v in per.items():
            if len(v) >= 20:
                days[d_] = round(sum(v) / len(v), 1)
        if self.dir and per:
            try:
                with open(os.path.join(self.dir, "weather_days.json"), "w", encoding="utf-8") as fh:
                    json.dump(dict(sorted(days.items())[-800:]), fh)
                self.days_cache = days
            except OSError:
                pass
        self.cache = {"at": time.time(), "c": round(float(c), 1) if c is not None else None,
                      "hourly": [x for x in hourly if x[0] >= time.time() - 2 * 86400], "lat": lat, "lon": lon}
        if self.cache_path:
            write_json_atomic(self.cache_path, self.cache)
            if self.cache["c"] is not None:
                append_jsonl(os.path.join(self.dir, "weather.jsonl"), {"epoch": round(time.time()), "c": self.cache["c"]})

    @classmethod
    def find(cls, q, lang="en"):
        """Place search by name (Open-Meteo geocoding). Returns up to 6 places with rounded coordinates."""
        q = str(q or "").strip()[:60]
        if len(q) < 2:
            return []
        j = cls._get(cls.GEO, {"name": q, "count": 20, "language": "da" if lang == "da" else "en", "format": "json"})
        out = []
        for r in j.get("results") or []:
            out.append({"name": r.get("name"), "area": ", ".join(x for x in (r.get("admin1"), r.get("country")) if x),
                        "cc": r.get("country_code") or "",
                        "lat": round(float(r["latitude"]), 2), "lon": round(float(r["longitude"]), 2)})
        # the burner is sold in Denmark and Germany: only places there, Denmark first (stable sort keeps the service's order)
        out = [x for x in out if x["cc"] in ("DK", "DE")]
        out.sort(key=lambda x: x["cc"] != "DK")
        return out

    def days(self):
        """{"YYYY-MM-DD": average °C} for the days we have (local file, works offline)."""
        try:
            with open(os.path.join(self.dir, "weather_days.json"), encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def feed(self):
        c, k = self.cfg(), self.cache
        if not c.get("enabled"):
            return {"enabled": False}
        now = time.time()
        same = k.get("lat") == c.get("lat") and k.get("lon") == c.get("lon")
        hourly = [x for x in (k.get("hourly") or []) if now - 86400 <= x[0] <= now + 86400] if same else []
        nxt = [x[1] for x in hourly if x[0] >= now]
        return {"enabled": True, "name": c.get("name") or "", "c": k.get("c") if same else None,
                "at": k.get("at") if same else None, "age_min": round((now - k["at"]) / 60) if same and k.get("at") else None,
                "hourly": hourly, "low_24h": min(nxt) if nxt else None, "high_24h": max(nxt) if nxt else None,
                "days": dict(sorted(self.days_cache.items())[-60:]),
                "error": self.error}
