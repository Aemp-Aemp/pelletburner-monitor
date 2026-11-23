#!/usr/bin/env python3
"""The one place for what the other modules share (stdlib only, imports none of them):

  tables   - state and alarm names (the boiler's own, plain English, Danish), who started / stopped it,
             which alarms are never reset automatically, the flame limit
  helpers  - time as text, number parsing, reading / writing the data files

The web pages carry their tables as a block written by tools/sync_pages.py from this file (never typed by hand);
scada/_sync_test.py fails when a page is not up to date."""
import datetime, glob, json, math, os, shutil, threading, time

# ---------------------------------------------------------------------------- states
# The boiler's own names (screen words, used in logs and *_label fields)
STATE_LABELS = {0: "STOPPET", 1: "WAITING", 2: "OPSTART", 3: "IGNITION",
                4: "DRIFT", 5: "PAUSESTOP", 6: "ALARM", 7: "REDDER BAL"}
# Plain words (Home Assistant, phone alerts)
STATE_WORDS = {0: "Stopped", 1: "Waiting", 2: "Starting", 3: "Lighting", 4: "Running",
               5: "Pause / burning out", 6: "Alarm", 7: "Fire rescue"}
# The words on the web pages (their "states" lists), English and Danish
STATE_PAGE = {"en": ["STOPPED", "WAITING", "STARTING", "IGNITION", "RUNNING", "PAUSE/STOP", "ALARM", "FIRE RESCUE"],
              "da": ["STOPPET", "AFVENTER", "OPSTART", "OPTÆNDING", "DRIFT", "PAUSESTOP", "ALARM", "REDDER BÅL"]}
STATES_ACTIVE = (2, 3, 4, 5, 7)     # the burner is "on": starting, lighting, running, pause, fire rescue
STATE_IGNITION, STATE_RUNNING = 3, 4

# ---------------------------------------------------------------------------- flame
# The flame is "strong enough" above this reading (the boiler's own limit while lighting).
# Time to flame = from the start of lighting (IGNITION, state 3) until the flame reading is above it.
FLAME_ON = 17

# ---------------------------------------------------------------------------- alarms
# The boiler's own names (screen words, ASCII)
ALARM_LABELS = {0: "", 2: "THERMOSIKRING", 4: "OPTAENDINGSFEJL", 5: "MANGLER BRAENDSEL",
                6: "ASKEPLADE", 7: "FOLER IKKE MONT. KEDEL", 8: "FOLER DEFEKT KEDEL",
                9: "FOLER ROG", 12: "ROGSUGER", 13: "SIKRING", 14: "SNEGL AFVEJNING",
                15: "EKSTERN IO"}
# Plain words: English (Home Assistant, phone alerts) and Danish (phone alerts)
ALARM_TEXT = {0: "No alarm", 2: "Burner disconnected / pellet shaft hot", 4: "Start-up failed", 5: "No fuel",
              6: "Ash plate", 7: "Boiler sensor not fitted", 8: "Boiler sensor defect", 9: "Flue sensor not fitted",
              12: "Fan", 13: "Fuse", 14: "Auger calibration", 15: "External IO"}
ALARM_TEXT_DA = {2: "Brænder frakoblet / skakt varm", 4: "Optændingsfejl", 5: "Mangler brændsel", 6: "Askeplade",
                 7: "Føler ikke mont. (kedel)", 8: "Føler defekt (kedel)", 9: "Føler ikke mont. (røg)", 12: "Røgsuger",
                 13: "Sikring", 14: "Snegl afvejning", 15: "Ekstern IO"}
# The names on the web pages (the boiler's own words, in English and Danish)
ALARM_PAGE = {
    "en": {2: "BURNER DISCONNECTED / PELLET SHAFT HOT", 4: "STARTUP FAILED", 5: "NO FUEL", 6: "ASH PLATE",
           7: "SENSOR NOT MOUNTED (boiler)", 8: "SENSOR DEFECT (boiler)", 9: "SENSOR NOT MOUNTED (flue)", 12: "FAN",
           13: "FUSE", 14: "AUGER CALIBRATION", 15: "EXTERNAL IO"},
    "da": {2: "BRÆNDER FRAKOBLET / SKAKT VARM", 4: "OPTÆNDINGSFEJL", 5: "MANGLER BRÆNDSEL", 6: "ASKEPLADE",
           7: "FØLER IKKE MONT. (kedel)", 8: "FØLER DEFEKT (kedel)", 9: "FØLER IKKE MONT. (røg)", 12: "RØGSUGER",
           13: "SIKRING", 14: "SNEGL AFVEJNING", 15: "EKSTERN IO"}}
# Never cleared automatically: thermo switch, sensor faults, fuse, auger calibration, IO box, unknown codes
ALARM_NEVER_AUTO_RESET = (1, 2, 3, 7, 8, 9, 10, 11, 13, 14, 15)
# The alarms the owner may pick for the automatic reset
ALARM_AUTO_RESET_ALLOWED = (4, 5, 6, 12)

# ---------------------------------------------------------------------------- who started / stopped it
# The one table of "source" names: the `source` field on a start / stop event (transition) and on a start record.
SOURCES = {
    "plan":        {"en": "heating plan", "da": "varmeplanen"},
    "schedule":    {"en": "schedule", "da": "tidsplanen"},
    "mqtt":        {"en": "Home Assistant (MQTT)", "da": "Home Assistant (MQTT)"},
    "web":         {"en": "web page", "da": "websiden"},
    "phone":       {"en": "phone (ntfy)", "da": "telefon (ntfy)"},
    "remote":      {"en": "remote command", "da": "fjernkommando"},
    "auto_reset":  {"en": "auto alarm reset", "da": "automatisk alarmnulstilling"},
    "boiler_min":  {"en": "boiler itself (minimum mode)", "da": "kedlen selv (minimumsdrift)"},
    "boiler_temp": {"en": "boiler itself (temperature)", "da": "kedlen selv (temperatur)"},
    "panel":       {"en": "boiler panel (by hand)", "da": "kedlens panel (manuelt)"},
    "boiler":      {"en": "boiler / by hand", "da": "kedlen / manuelt"},
    "link_down":   {"en": "not seen (link was down)", "da": "ikke set (forbindelsen var nede)"},
    "after_alarm": {"en": "after alarm reset", "da": "efter alarmnulstilling"},   # a note, not a source
}


def labels():
    """The names sent in /status.json ("labels"): the boiler's own state and alarm names, and who started /
    stopped it. (The other tables are written into the pages by tools/sync_pages.py.)"""
    return {"states": STATE_LABELS, "alarms": ALARM_LABELS, "sources": SOURCES}


# ---------------------------------------------------------------------------- time and numbers
def iso(epoch=None):
    """Local time as text, to the second: 2000-01-03T08:00:00 (now when no epoch is given)."""
    return datetime.datetime.fromtimestamp(epoch or time.time()).isoformat(timespec="seconds")


def log_stamp(msg, epoch=None):
    """A log line with the local date and time in front ('2000-01-03 08:00:00  # weather: ...'), so every line in
    responder.log says when it happened. Lines that already start with a time (bus frames: '08:00:00.123  IN ...')
    and empty lines are kept as they are; leading blank lines stay in front."""
    s = str(msg)
    body = s.lstrip("\n")
    if not body.strip() or (len(body) >= 8 and body[2] == ":" and body[5] == ":" and body[:2].isdigit()):
        return s
    t = datetime.datetime.fromtimestamp(epoch or time.time()).strftime("%Y-%m-%d %H:%M:%S")
    return s[:len(s) - len(body)] + t + "  " + body


def to_float(v):
    """A number or None. Infinity and NaN are None too: they are never a real value and break strict JSON."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def to_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):     # OverflowError: int(float("inf"))
        return default


def finite_num(v):
    """True for a real JSON number: int or float, not a bool, not Infinity or NaN."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def strict_json_loads(raw):
    """json.loads for data from outside: Infinity, NaN and numbers too big (1e999) raise ValueError, and JSON nested
    too deep raises ValueError instead of RecursionError, so the caller can answer "bad JSON"."""
    def bad_const(s):
        raise ValueError(f"{s} is not a JSON number")

    def flt(s):
        f = float(s)
        if not math.isfinite(f):
            raise ValueError("number too big")
        return f
    try:
        return json.loads(raw, parse_constant=bad_const, parse_float=flt)
    except RecursionError:
        raise ValueError("JSON nested too deep") from None


def hhmm_to_min(s):
    """"07:30" -> 450 (minutes after midnight)."""
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def median(vals):
    """The middle value (the mean of the two middle ones for an even count), or None for an empty list."""
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v) // 2
    return v[n] if len(v) % 2 else (v[n - 1] + v[n]) / 2.0


# ---------------------------------------------------------------------------- data files
def parse_jsonl_line(line):
    """One .jsonl line -> its dict, or None for an empty, broken or cut line. After a power cut a file can hold
    a half line and a run of NUL bytes, with the next good record glued to the end of them: the text after the
    last NUL is tried."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.strip().strip("\x00").strip()
    if "\x00" in line:
        line = line.rsplit("\x00", 1)[1].strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def read_jsonl(path):
    """The rows of a .jsonl file, one at a time. Empty, broken or cut lines (a half-written last line, NUL bytes
    after a power cut) are skipped. A missing or unreadable file gives no rows: OSError is raised only before the
    first row, so callers that want to know can catch it; use rows_of() for 'no file = no rows'."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            obj = parse_jsonl_line(line)
            if obj is not None:
                yield obj


_append_checked = set()      # files checked by this program; a failed write takes the file out (it may end cut)

# ---- disk trouble (full or broken SD card): one log line when writes start failing, one when they work again
_disk = {"since": None, "n": 0}
_disk_lock = threading.Lock()
_disk_log = [None]


def set_write_log(log):
    """Where the 'write failed' / 'writes work again' lines go (the monitor's log)."""
    _disk_log[0] = log


def write_failed(path, err, log=None):
    """A write to `path` failed (disk full, read-only, ...). The file may now end with half a line, so the next
    append checks it again. Only the first failure of an outage is logged; the rest are counted."""
    _append_checked.discard(path)
    with _disk_lock:
        first = _disk["since"] is None
        if first:
            _disk["since"] = time.time()
        _disk["n"] += 1
    log = _disk_log[0] or log
    if first and log:
        log(f"# store: write failed ({err}); more failures are counted, not logged")


def write_ok(log=None):
    """A write worked: ends an outage with one log line and the count."""
    if _disk["since"] is None:
        return
    with _disk_lock:
        since, n = _disk["since"], _disk["n"]
        if since is None:
            return
        _disk.update(since=None, n=0)
    log = _disk_log[0] or log
    if log:
        log(f"# store: writes work again ({n} failed writes in {max(1, round((time.time() - since) / 60))} min)")


def disk_trouble():
    """{"min": minutes, "n": failed writes} while writes are failing (for the /home notice), else None."""
    with _disk_lock:
        if _disk["since"] is None:
            return None
        return {"min": int((time.time() - _disk["since"]) // 60), "n": _disk["n"]}


def open_append(path, **open_args):
    """open(path, "a") that first ends a cut last line (power cut, earlier failed write) with a newline, so the
    next record starts on a line of its own. Checked once per file and program run, and again after a failed
    write (write_failed()); reading a just-written file is slow on Windows."""
    cut = False
    if path not in _append_checked:
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                if fh.tell() > 0:
                    fh.seek(-1, 2)
                    cut = fh.read(1) != b"\n"
        except OSError:
            pass
        if len(_append_checked) > 1000:
            _append_checked.clear()
        _append_checked.add(path)
    fh = open(path, "a", **open_args)
    if cut:
        try:
            fh.write("\n")
        except OSError:
            _close_quietly(fh)
            raise
    return fh


def _close_quietly(fh):
    try:
        fh.close()
    except OSError:
        pass


def rows_of(path):
    """All rows of a .jsonl file as a list; an empty list when the file is missing or cannot be read."""
    try:
        return list(read_jsonl(path))
    except OSError:
        return []


def append_jsonl(path, obj):
    """Add one row to a .jsonl file. True when written; a disk error is swallowed (False)."""
    try:
        with open_append(path, encoding="utf-8") as fh:
            fh.write(json.dumps(obj) + "\n")
    except OSError as e:
        write_failed(path, e)
        return False
    write_ok()
    return True


def _fsync_dir(path):
    """Make a rename in this file's folder survive a power cut (Linux); best effort, not possible on Windows."""
    try:
        fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass


def _json_file_ok(path):
    try:
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
        return True
    except (OSError, ValueError):
        return False


def write_text_atomic(path, text, backup=False, newline=None):
    """Write a file so that a reader (or a power cut) never sees half a file: write next to it, flush it to the
    disk, then replace. backup=True first keeps the previous file as <path>.bak - only when that file is good JSON,
    so a broken file never replaces a good backup. Raises OSError like open() does."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if backup and _json_file_ok(path):
            try:
                shutil.copyfile(path, path + ".bak")
            except OSError:
                pass
        os.replace(tmp, path)
    except OSError as e:
        write_failed(path, e)
        raise
    write_ok()
    _fsync_dir(path)


def write_json_atomic(path, obj, backup=False, **dump_args):
    """write_text_atomic() of a JSON value (backup=True keeps the previous good file as <path>.bak)."""
    write_text_atomic(path, json.dumps(obj, **dump_args), backup=backup)


def read_json(path, default=None, log=None):
    """A JSON file's content, or `default` when it is missing or broken. A broken file (empty, NUL bytes after a
    power cut, cut short) falls back to <path>.bak when there is one; `log` gets one line about it."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        why = None
    except (OSError, ValueError) as e:
        why = e
    try:
        with open(path + ".bak", encoding="utf-8") as fh:
            obj = json.load(fh)
        if log:
            log(f"# {os.path.basename(path)} " + (f"unreadable ({why})" if why else "missing") + "; using the backup .bak")
        return obj
    except (OSError, ValueError):
        if why is not None and log:
            log(f"# {os.path.basename(path)} unreadable ({why}); no good backup, using defaults")
        return default


def hist_files(data_dir, prefix="hist"):
    """The day files of a data folder, oldest first: [(day "2000-01-03", path), ...]."""
    n = len(prefix) + 1
    return [(os.path.basename(p)[n:n + 10], p)
            for p in sorted(glob.glob(os.path.join(data_dir, f"{prefix}-????-??-??.jsonl")))]
