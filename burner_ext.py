#!/usr/bin/env python3
"""Extensions for burner_responder.py (stdlib only, so a Raspberry Pi needs no pip installs):

  MqttClient  - minimal MQTT 3.1.1 client (QoS 0, retain, LWT, auto-reconnect)
  MqttBridge  - publishes everything to MQTT + Home Assistant discovery; start/stop + plan switches
  Store       - appends history / events to daily JSONL files in a data dir, reloads them on start,
                deletes files older than --keep-days
  Tracker     - state-machine transition log + pre-alarm snapshot
  CfgSync     - keeps the controller's cfg_data_1 equal to a "desired settings" file: re-pushes
                values after a power cycle / desync (J4 writes are not saved on the controller)

Nothing in here ever crashes the responder: every disk / network error is logged and swallowed.
"""
import json, os, socket, struct, threading, time, datetime, glob, collections
# the shared tables and helpers (one copy: burner_common.py); the old names stay usable from this module
from burner_common import (STATE_LABELS, STATE_WORDS, ALARM_TEXT, SOURCES, iso as _now_iso, to_float as _num,
                           rows_of, read_json, write_json_atomic, parse_jsonl_line, open_append,
                           write_failed, write_ok, append_jsonl)

MONITOR_VERSION = "1.0.9"   # the top entry of CHANGELOG.md; on the pages, in /status.json and in Home Assistant

# ============================================================================ who started / stopped it
# SOURCES (burner_common) is the one table of "source" names: the `source` field on a start / stop event
# (transition) and on a start record. The web pages get these words in /status.json (labels.sources), MQTT uses
# the English ones.
AFTER_ALARM_S = 900                # a start this soon after ALARM -> STOPPET is marked "after alarm reset"


def command_cause(lc, frm, to, now):
    """The `cause` of a state change: the source text of a command we delivered shortly before (lc = the last
    command, marked used), else "boiler" for a start / stop nobody here asked for, else None (a normal
    automatic step such as IGNITION -> RUNNING)."""
    lc = lc or {}
    d = lc.get("delivered")
    if d and now - d < 120 and not lc.get("used"):
        if (lc.get("cmd") == "start" and ((frm in (0, 1) and to in (2, 3)) or (frm == 6 and to == 0))) or \
           (lc.get("cmd") == "stop" and (to == 0 or (frm == 4 and to == 1))):
            lc["used"] = True
            return lc.get("source")
    if (frm in (0, 1) and to == 2) or (to == 0 and frm != 0):
        return "boiler"                            # screen button, the boiler's own timer / minimum mode
    return None


def source_of(cause, frm, to, prev=None, gap=False):
    """Short code (a key of SOURCES) for who / what made this state change, or None when it is not a start or
    stop, or when nothing is known (events from before causes were logged).
    cause = what command_cause() gave; prev = the state before `frm`; gap = the link was down in between."""
    c = str(cause or "")
    if c and c != "boiler":                        # a command of ours was delivered just before
        for key, code in (("warm", "plan"), ("schedule", "schedule"), ("auto_reset", "auto_reset"),
                          ("mqtt", "mqtt"), ("web", "web"), ("phone", "phone")):
            if c.startswith(key):
                return code
        return "remote"                            # trigger file
    own = None                                     # nobody here asked for it
    if to == 2 and c:
        own = "boiler_min" if frm == 1 else "panel" if frm == 0 else "boiler"
    elif to == 3 and frm == 1:
        own = "boiler_min"                         # relight out of WAITING
    elif to == 3 and frm == 5 and prev == 4:
        own = "boiler_temp"                        # heat needed again after a pause
    elif to == 0 and c:
        own = "panel" if frm in (1, 2, 3, 4, 6) else "boiler"
    elif frm == 4 and to == 1:
        own = "boiler_min"                         # burned out at low power by itself
    elif frm == 4 and to == 5:
        own = "boiler_temp"                        # warm enough: pause
    return "link_down" if own and gap else own


def add_sources(events):
    """Give older saved transitions (which have a `cause` but no `source`) their source, in memory only."""
    prev = None
    for ev in events:
        if ev.get("type") != "transition":
            continue
        if "source" not in ev and not ev.get("at_start"):
            s = source_of(ev.get("cause"), ev.get("from"), ev.get("to"), prev)
            if s:
                ev["source"] = s
        prev = ev.get("from")
    return events


def source_text(code, lang="en"):
    w = SOURCES.get(code)
    return (w.get(lang) or w["en"]) if w else None


# ============================================================================ persistence
class Store:
    """Daily JSONL files: hist-YYYY-MM-DD.jsonl (one telemetry row per line) and
    events-YYYY-MM-DD.jsonl (transitions, alarms, commands, cfg-sync). prealarm-*.json per alarm."""

    def __init__(self, data_dir, keep_days, log):
        self.dir, self.keep_days, self.log = data_dir, keep_days, log
        self.lock = threading.Lock()
        self.last_cleanup = 0.0
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as e:
            log(f"# data dir {self.dir} not usable ({e}); history will not be saved")
            self.dir = None
        self.cleanup()

    def _day(self, epoch):
        return datetime.date.fromtimestamp(epoch).isoformat()

    def _append(self, prefix, obj):
        if not self.dir:
            return
        p = os.path.join(self.dir, f"{prefix}-{self._day(obj.get('epoch', time.time()))}.jsonl")
        try:
            with self.lock, open_append(p, encoding="utf-8") as fh:
                fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
        except OSError as e:
            write_failed(p, e, self.log)           # logged once per outage; the next write ends a cut line first
        else:
            write_ok(self.log)
        if time.time() - self.last_cleanup > 6 * 3600:
            self.cleanup()

    def add_row(self, row):
        self._append("hist", row)

    def add_raw(self, row):
        """Every status row at full rate (1 s with fast data) -> raw-YYYY-MM-DD.jsonl, for later analysis.
        ~35 MB per day at 1 s; the normal history (hist-*) stays thinned. Same clean-up age as the rest."""
        self._append("raw", row)

    def add_event(self, ev):
        self._append("events", ev)

    def save_prealarm(self, snap):
        if not self.dir:
            return
        try:
            stamp = datetime.datetime.fromtimestamp(snap["epoch"]).strftime("%Y%m%d-%H%M%S")
            for name in (f"prealarm-{stamp}.json", "prealarm-last.json"):
                with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
                    json.dump(snap, fh, separators=(",", ":"))
        except OSError as e:
            self.log(f"# store: prealarm save failed ({e})")

    def _load(self, prefix, max_rows, days=2):
        if not self.dir:
            return []
        files = sorted(glob.glob(os.path.join(self.dir, f"{prefix}-????-??-??.jsonl")))[-days:]
        rows = collections.deque(maxlen=max_rows)
        for p in files:
            rows.extend(rows_of(p))   # skips a half-written last line after a power cut
        return list(rows)

    def load_rows(self, n):
        return self._load("hist", n)

    def load_events(self, n):
        return self._load("events", n, days=7)

    def load_prealarm(self):
        if not self.dir:
            return None
        return read_json(os.path.join(self.dir, "prealarm-last.json"))

    def cleanup(self):
        """Delete day files and prealarm snapshots older than keep_days (keeps prealarm-last.json)."""
        self.last_cleanup = time.time()
        if not self.dir or not self.keep_days:
            return
        cutoff = time.time() - self.keep_days * 86400
        n = 0
        for p in glob.glob(os.path.join(self.dir, "*")):
            base = os.path.basename(p)
            if base == "prealarm-last.json" or not (base.startswith(("hist-", "events-", "prealarm-", "summary-", "raw-"))):
                continue
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p); n += 1
            except OSError:
                pass
        if n:
            self.log(f"# store: removed {n} file(s) older than {self.keep_days} days")


# ============================================================================ transitions / pre-alarm
class Tracker:
    """Records every state change (from, to, time in previous state) and freezes the last few
    minutes of telemetry when an alarm starts - our own PRE ALARM DATA."""

    LINK_GAP_S = 90                   # rows further apart than this: the link was down in between

    def __init__(self, store=None, prealarm_s=300):
        self.store = store
        self.prealarm_s = prealarm_s
        self.transitions = collections.deque(maxlen=300)
        self.prealarm = store.load_prealarm() if store else None
        self.state = None
        self.since = None
        self.prev_state = None
        self.alarm = 0
        self.last_epoch = None        # time of the last row seen (to spot a link gap)
        self.listeners = []           # callables(event dict)
        self.cause_fn = None          # (from, to, epoch) -> "schedule" / "auto_reset" / "mqtt" / "boiler" / None
        #                               (see command_cause); the short `source` code is made from it (source_of)
        if store:
            for ev in store.load_events(300):
                if ev.get("type") == "transition":
                    self.transitions.append(ev)
            add_sources(self.transitions)
            if self.transitions:
                last = self.transitions[-1]
                self.prev_state = last.get("from")

    def _emit(self, ev):
        if self.store:
            self.store.add_event(ev)
        for fn in self.listeners:
            try:
                fn(ev)
            except Exception:
                pass

    def on_row(self, row, hist):
        st, al = row.get("state"), row.get("alarm") or 0
        now = row.get("epoch") or time.time()
        try:
            st = int(st) if st is not None else None
            al = int(al)
        except (TypeError, ValueError):
            return
        if st is None:
            return
        gap = self.last_epoch is not None and now - self.last_epoch > self.LINK_GAP_S
        self.last_epoch = now
        if self.state is None:                       # first row after (re)start: no transition
            self.state, self.since, self.alarm = st, now, al
            if self.transitions and self.transitions[-1].get("to") == st:
                self.since = self.transitions[-1].get("epoch") or now   # keep the real time-in-state
            if (st == 6 or al) and not self._alarm_open(st, al):
                self._alarm_at_start(now, st, al, hist)
            return
        if st != self.state:
            ev = {"type": "transition", "epoch": now, "t": _now_iso(now), "from": self.state, "to": st,
                  "from_label": STATE_LABELS.get(self.state, str(self.state)),
                  "to_label": STATE_LABELS.get(st, str(st)),
                  "dwell_s": round(now - self.since, 1) if self.since else None, "alarm": al}
            if self.cause_fn:
                try:
                    c = self.cause_fn(self.state, st, now)
                    if c:
                        ev["cause"] = c
                except Exception:
                    pass
            src = source_of(ev.get("cause"), self.state, st, self.prev_state, gap)
            if src:
                ev["source"] = src                   # who / what started or stopped it (a key of SOURCES)
                last = self.transitions[-1] if self.transitions else {}
                if st == 2 and last.get("from") == 6 and last.get("to") == 0 \
                        and now - (last.get("epoch") or 0) <= AFTER_ALARM_S:
                    ev["after_alarm"] = True
            self.transitions.append(ev)
            self.prev_state, self.state, self.since = self.state, st, now
            self._emit(ev)
        if al and not self.alarm:
            self._freeze(now, al, hist)
        elif al and gap and st == self.state == 6 and al != self.alarm:
            # still ALARM after a link gap but another code: a new alarm came while we could not see
            self._freeze(now, al, hist, at_start=True)
        self.alarm = al

    def _alarm_open(self, st, al):
        """True when the saved log already holds this alarm as still open (logged before a restart and
        never left): the last transition went into ALARM with the same code, or - for an alarm code
        without the ALARM state - the last pre-alarm snapshot is that code and nothing changed since."""
        last = self.transitions[-1] if self.transitions else None
        if st == 6:
            if not last or last.get("to") != 6:
                return False
            code = last.get("alarm") or (self.prealarm or {}).get("alarm") or 0
            return not al or not code or int(code) == al
        p = self.prealarm or {}
        return bool(p.get("alarm") == al and (not last or (p.get("epoch") or 0) >= (last.get("epoch") or 0)))

    def _alarm_at_start(self, now, st, al, hist):
        """The boiler was already in alarm when the monitor started: log it like a normal step into
        ALARM, marked 'at_start'. What came before is unknown, so no 'from' state and no last-good row."""
        if st == 6:
            ev = {"type": "transition", "epoch": now, "t": _now_iso(now), "from": None, "to": 6,
                  "from_label": "?", "to_label": STATE_LABELS.get(6, "6"), "dwell_s": None, "alarm": al,
                  "at_start": True}
            self.transitions.append(ev)
            self.prev_state = None
            self._emit(ev)
        if al:
            self._freeze(now, al, hist, at_start=True)

    def _freeze(self, now, al, hist, at_start=False):
        if at_start:                                 # only rows we really saw in alarm, no invented 'before'
            rows = [r for r in hist if r.get("epoch") and now - self.prealarm_s <= r["epoch"] <= now
                    and (r.get("alarm") or 0)]
            snap = {"type": "prealarm", "epoch": now, "t": _now_iso(now), "alarm": al, "at_start": True,
                    "state_before": None, "state_before_label": None, "last_ok_row": None, "rows": rows,
                    "window_s": self.prealarm_s}
            self.prealarm = snap
            if self.store:
                self.store.save_prealarm(snap)
            self._emit({"type": "alarm", "epoch": now, "t": _now_iso(now), "alarm": al,
                        "state_before": None, "at_start": True})
            return
        rows = [r for r in hist if r.get("epoch") and now - self.prealarm_s <= r["epoch"] <= now]
        before = None
        for r in reversed(rows):
            if not (r.get("alarm") or 0):
                before = r
                break
        from_state = self.prev_state if self.state == 6 else self.state
        snap = {"type": "prealarm", "epoch": now, "t": _now_iso(now), "alarm": al,
                "state_before": from_state, "state_before_label": STATE_LABELS.get(from_state, str(from_state)),
                "last_ok_row": before, "rows": rows, "window_s": self.prealarm_s}
        self.prealarm = snap
        if self.store:
            self.store.save_prealarm(snap)
        self._emit({"type": "alarm", "epoch": now, "t": _now_iso(now), "alarm": al,
                    "state_before": from_state})

    def feed(self):
        return {"transitions": list(self.transitions)[-100:], "prev_state": self.prev_state,
                "state_since": self.since, "prealarm": self.prealarm}


# ============================================================================ settings change history
class CfgHistory:
    """A history of the boiler's own settings (cfg_data_1): each read that differs from the one before adds one
    row per changed field, {"time": epoch, "field": "017", "old": 600, "new": 800}, to cfg_changes.jsonl.
    The last values are kept in last_cfg.json, so the first read after a restart is compared with the read before
    the restart (a change made while the monitor was off is still seen, and nothing is logged twice)."""
    IGNORE = ("011",)            # an internal flag that flips during every ignition: not a setting
    KEEP = 10                    # rows on the pages

    def __init__(self, data_dir, log=None):
        self.log = log or (lambda m: None)
        self.path = os.path.join(data_dir, "cfg_changes.jsonl") if data_dir else None
        self.last_path = os.path.join(data_dir, "last_cfg.json") if data_dir else None
        last = read_json(self.last_path, None, self.log) if self.last_path else None
        self.last = ({k: v for k, v in last.items() if isinstance(k, str) and len(k) == 3 and k.isdigit()}
                     if isinstance(last, dict) else None) or None
        rows = rows_of(self.path) if self.path else []
        if self.last:                            # the .bak after a power cut is one save behind: the history fills it in
            t0 = last.get("time") or 0
            for r in rows:
                if isinstance(r.get("time"), (int, float)) and r["time"] > t0 and r.get("field") in self.last:
                    self.last[r["field"]] = r.get("new")
        self.recent = collections.deque((r for r in rows if isinstance(r.get("field"), str) and "time" in r),
                                        maxlen=self.KEEP)
        self.lock = threading.Lock()

    def on_cfg(self, raw, now=None):
        """One settings read (the board's numeric dict). Returns the new change rows."""
        now = int(now if now is not None else time.time())
        cur = {k: v for k, v in raw.items()
               if isinstance(k, str) and len(k) == 3 and k.isdigit() and k not in self.IGNORE}
        if not cur:
            return []
        with self.lock:
            first = self.last is None
            old = self.last or {}
            rows = [] if first else [{"time": now, "field": k, "old": old[k], "new": v}
                                     for k, v in sorted(cur.items()) if k in old and old[k] != v]
            new_last = dict(old)
            new_last.update(cur)                 # a field missing from this read keeps its last value
            self.last = new_last
            for r in rows:
                self.recent.append(r)
        if first:
            self.log(f"# settings history: first read kept ({len(cur)} fields); changes are logged from now on")
        elif rows:
            self.log("# settings changed on the boiler: "
                     + ", ".join(f"{r['field']} {r['old']} -> {r['new']}" for r in rows))
        for r in rows:                           # history first: if the save below fails, a restart logs it again
            if self.path:
                append_jsonl(self.path, r)
        if self.last_path and new_last != old:
            try:
                write_json_atomic(self.last_path, dict(new_last, time=now), backup=True, sort_keys=True)
            except OSError:
                pass
        return rows

    def feed(self):
        with self.lock:
            return list(self.recent)


# ============================================================================ parameter sync
class CfgSync:
    """Keeps cfg_data_1 equal to a desired-settings JSON file.

    The controller keeps settings in flash, but a J4 write only changes the running value, so after a power
    cycle the board is back on its own saved values. With sync on, this re-pushes every differing
    value (max 7 per reply, the size the controller accepts) and reads back to verify.
    To make the values permanent on the board itself: after a successful sync, change and save any
    one setting on the screen - the controller then saves all settings, including ours.

    File format:  {"cfg_data_1": {"000": 65, "001": 2200, ...}, "check_only": ["033"]}
    check_only keys are compared and reported but never written (033 KEDEL TYPE reloads the
    model's factory defaults when changed on the screen - set it there first)."""

    BATCH = 7
    MAX_ROUNDS = 3

    def __init__(self, path, log, every_s=1800, emit=None):
        self.path, self.log, self.every_s, self.emit = path, log, every_s, emit
        self.lock = threading.RLock()     # re-entrant: emit() -> MQTT -> feed() runs inside on_cfg()
        self.desired, self.check_only, self.mtime = {}, set(), 0.0
        self.status = "starting"
        self.diff = {}               # key -> [board, desired]
        self.check_diff = {}
        self.queue = []              # batches still to write
        self.want_dump = True
        self.dump_asked = 0.0
        self.last_check = 0.0
        self.rounds = 0
        self.last_ok = None
        self.writes = 0
        self.stuck = set()           # keys the board kept refusing: not rewritten until the file changes
        self._load()

    # values that are unsafe on the boiler: 018 MAX DOSER 0 = ~255 top-up doses (1 = none),
    # 016 OPVARMNING 0 = ~18 h wait with no timeout. 017 TIMEOUT below 60 s makes no sense either.
    SAFE_MIN = {"016": 1, "017": 60, "018": 1}

    @staticmethod
    def valid(k, v):
        """A settings value we are willing to write: key 000..041, finite number, |v| <= 100000,
        and never one of the known unsafe values (SAFE_MIN)."""
        if not (isinstance(k, str) and len(k) == 3 and k.isdigit() and int(k) <= 41):
            return False
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        if k in CfgSync.SAFE_MIN and v < CfgSync.SAFE_MIN[k]:
            return False
        return v == v and abs(v) <= 100000                     # v == v rejects NaN

    def _load(self):
        try:
            m = os.path.getmtime(self.path)
        except OSError as e:
            if self.mtime != -1:
                self.status, self.mtime = f"file error: {e}", -1
                self.log(f"# cfg-sync: cannot read {self.path} ({e})")
            return False
        if m == self.mtime or m == getattr(self, "bad_mtime", None):
            return False
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                obj = json.load(fh)
            want = obj.get("cfg_data_1", obj)
            desired = {k: v for k, v in want.items() if len(k) == 3 and k.isdigit()}
            bad = {k: v for k, v in desired.items() if not self.valid(k, v)}
            if bad:
                raise ValueError(f"invalid values {bad}")
            if not desired:
                raise ValueError("no cfg_data_1 values in the file")
            co = obj.get("check_only", ["033"])
            if not isinstance(co, list):
                raise ValueError("check_only must be a list like [\"033\"]")
            self.desired, self.check_only = desired, {str(x).zfill(3) for x in co}
            self.mtime, self.stuck = m, set()
            self.log(f"# cfg-sync: {len(self.desired)} desired values from {self.path} "
                     f"(check only: {sorted(self.check_only) or 'none'})")
            return True
        except (OSError, ValueError, AttributeError, TypeError) as e:
            self.bad_mtime = m                                  # report a broken file once, not every second
            self.status = f"file error: {e}"
            self.log(f"# cfg-sync: cannot use {self.path} ({e}) - keeping the previous values")
            return False

    def set_values(self, changes):
        """Update desired values (e.g. from MQTT) and write the file back atomically. Returns (ok, msg)."""
        bad = {k: v for k, v in changes.items() if not self.valid(k, v)}
        if bad:
            return False, f"refused (key 000-041, number, |value| <= 100000): {bad}"
        co = {k for k in changes if k in self.check_only}
        if co:
            return False, f"{sorted(co)} are check-only (set them on the boiler's screen)"
        with self.lock:
            self.desired.update(changes)
            self.stuck = set()
            try:                                                # a power cut never leaves half a file
                write_json_atomic(self.path, {"cfg_data_1": dict(sorted(self.desired.items())),
                                              "check_only": sorted(self.check_only)}, indent=1)
                self.mtime = os.path.getmtime(self.path)
            except OSError as e:
                self.log(f"# cfg-sync: could not save {self.path} ({e})")
            self.request_check("desired values changed")
        return True, "saved"

    def request_check(self, why):
        self.want_dump, self.rounds = True, 0
        self.log(f"# cfg-sync: check requested ({why})")

    def tick(self, link_gap_s=0.0):
        """Called ~1/s from the main loop."""
        if self._load():
            self.request_check("file changed")
        now = time.time()
        if link_gap_s > 90:
            self.request_check(f"controller was silent {link_gap_s:.0f} s (power cycle?)")
        if self.every_s and now - self.last_check > self.every_s and not self.queue:
            self.request_check("periodic")
            self.last_check = now
        if self.dump_asked and now - self.dump_asked > 90:      # no dump came back: ask again
            self.dump_asked, self.want_dump = 0.0, True

    def next_extra(self):
        """Extra keys to merge into the next {"response":"OK"} data reply (one item per reply)."""
        with self.lock:
            if self.queue:
                batch = self.queue.pop(0)
                self.writes += len(batch)
                self.log(f"# cfg-sync: writing {batch}")
                if not self.queue:
                    self.want_dump = True                    # read back to verify
                return {"cfg_data_1": batch}
            if self.want_dump and not self.dump_asked:
                self.want_dump, self.dump_asked = False, time.time()
                self.last_check = time.time()
                return {"cfg_data": True}
        return None

    def on_cfg(self, raw):
        with self.lock:
            if self.queue:
                # the controller also sends dumps on its own; mid-round they are half-updated, so ignore
                # them - the read-back after the last batch does the verification
                return
            self.dump_asked = 0.0
            diff, cdiff = {}, {}
            for k, v in self.desired.items():
                have = raw.get(k)
                a, b = _num(have), _num(v)
                same = (a is not None and b is not None and abs(a - b) <= 1e-6 * max(1.0, abs(b))) or have == v
                if not same:
                    (cdiff if k in self.check_only else diff)[k] = [have, v]
            self.diff, self.check_diff = diff, cdiff
            if not self.desired:
                self.status = "file error: no values"
                return
            if not diff:
                if self.status != "in_sync":
                    self.log("# cfg-sync: board matches the desired settings")
                    self._event("in_sync", {})
                self.status, self.rounds, self.last_ok = "in_sync", 0, time.time()
                return
            todo = {k: v for k, v in diff.items() if k not in self.stuck}
            if not todo or self.rounds >= self.MAX_ROUNDS:
                self.stuck |= set(diff)                         # stop fighting the board over these keys
                if self.status != "mismatch":
                    self.log(f"# cfg-sync: board keeps different values after {self.rounds} rounds; not rewriting "
                             f"them until the file changes: {diff}")
                    self._event("mismatch", {"diff": diff})
                self.status = "mismatch"
                return
            self.rounds += 1
            items = [(k, v[1]) for k, v in sorted(todo.items())]
            self.queue = self._batches(items)
            self.status = "writing"
            self.log(f"# cfg-sync: {len(items)} value(s) differ, re-pushing (round {self.rounds}): {diff}")
            self._event("writing", {"diff": diff, "round": self.rounds})

    MAX_REPLY = 100        # bytes incl. EOT; ~105 B frames are accepted by the controller, ~330 B are not

    def _batches(self, items):
        """Split (key, value) pairs into replies that stay under MAX_REPLY bytes (and <= BATCH values)."""
        out, cur = [], {}
        for k, v in items:
            trial = dict(cur, **{k: v})
            size = len(json.dumps({"response": "OK", "cfg_data_1": trial}, separators=(",", ":"))) + 1
            if cur and (size > self.MAX_REPLY or len(trial) > self.BATCH):
                out.append(cur)
                cur = {k: v}
            else:
                cur = trial
        if cur:
            out.append(cur)
        return out

    def _event(self, what, data):
        if self.emit:
            ev = {"type": "cfg_sync", "epoch": time.time(), "t": _now_iso(), "status": what}
            ev.update(data)
            try:
                self.emit(ev)
            except Exception:
                pass

    def feed(self):
        with self.lock:
            return {"enabled": True, "file": self.path, "status": self.status, "diff": self.diff,
                    "check_only_diff": self.check_diff, "last_ok": self.last_ok,
                    "values_written": self.writes, "desired_count": len(self.desired)}


# ============================================================================ MQTT client
class MqttClient:
    """Minimal MQTT 3.1.1, QoS 0. Runs its own thread; reconnects forever with backoff."""

    def __init__(self, host, port, client_id, log, user=None, password=None,
                 will_topic=None, will_payload=b"offline", keepalive=60):
        self.host, self.port, self.cid, self.log = host, port, client_id, log
        self.user, self.password = user, password
        self.will_topic, self.will_payload, self.keepalive = will_topic, will_payload, keepalive
        self.sock = None
        self.subs = []
        self.on_message = None
        self.on_connect = None
        self.connected = False
        self.stop = False
        # Only the MQTT thread touches the socket. Other threads just queue packets, so a slow or dead broker
        # can never block the bus loop, and there are no races on the socket.
        self.outq = collections.deque(maxlen=2000)
        self.rbuf = bytearray()
        self.last_in = 0.0
        threading.Thread(target=self._run, daemon=True, name="mqtt").start()

    @staticmethod
    def _str(s):
        b = s.encode("utf-8") if isinstance(s, str) else s
        return struct.pack(">H", len(b)) + b

    @staticmethod
    def _len(n):
        out = bytearray()
        while True:
            d, n = n % 128, n // 128
            out.append(d | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def _queue(self, hdr, body):
        self.outq.append(bytes([hdr]) + self._len(len(body)) + body)

    def _drop(self):
        self.connected = False
        s, self.sock = self.sock, None
        try:
            if s:
                s.close()
        except OSError:
            pass
        self.rbuf.clear()
        self.outq.clear()                 # stale packets are not sent on the next connection

    def _parse(self):
        """One complete packet from the receive buffer, or None (partial packets wait for more bytes)."""
        b = self.rbuf
        if len(b) < 2:
            return None
        mult, n, i = 1, 0, 1
        while True:
            if i >= len(b):
                return None
            d = b[i]
            n += (d & 127) * mult
            i += 1
            if not d & 128:
                break
            mult *= 128
            if i > 4:
                raise OSError("bad packet length")
        if len(b) < i + n:
            return None
        h, body = b[0], bytes(b[i:i + n])
        del b[:i + n]
        return h, body

    def _pump(self, s, wait):
        """Send queued packets, then wait up to `wait` s for incoming bytes."""
        import select
        while self.outq:
            s.sendall(self.outq.popleft())
        r, _, _ = select.select([s], [], [], wait)
        if r:
            chunk = s.recv(65536)
            if not chunk:
                raise OSError("connection closed by broker")
            self.rbuf += chunk
            self.last_in = time.time()

    def publish(self, topic, payload, retain=False):
        if not self.connected:
            return False                  # retained state is re-published on (re)connect anyway
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, separators=(",", ":"))
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._queue(0x30 | (1 if retain else 0), self._str(topic) + payload)
        return True

    def subscribe(self, topic):
        if topic not in self.subs:
            self.subs.append(topic)
            if self.connected:
                self._queue(0x82, struct.pack(">H", len(self.subs)) + self._str(topic) + b"\x00")

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=10)
        flags = 0x02                                         # clean session
        payload = self._str(self.cid)
        if self.will_topic:
            flags |= 0x04 | 0x20                             # will flag + will retain, QoS 0
            payload += self._str(self.will_topic) + self._str(self.will_payload)
        if self.user:                                        # payload order: id, will, user, password
            flags |= 0x80
            payload += self._str(self.user)
        if self.password is not None and self.user:
            flags |= 0x40
            payload += self._str(self.password)
        body = self._str("MQTT") + bytes([4, flags]) + struct.pack(">H", self.keepalive) + payload
        self.rbuf.clear()
        self.outq.clear()
        s.settimeout(10)                                     # bounds a blocked sendall
        s.sendall(bytes([0x10]) + self._len(len(body)) + body)
        self.sock = s
        t0, pkt = time.time(), None
        while pkt is None:
            if time.time() - t0 > 10:
                raise OSError("no CONNACK from broker")
            self._pump(s, 0.5)
            pkt = self._parse()
        h, b = pkt
        if h >> 4 != 2 or len(b) < 2 or b[1] != 0:
            code = b[1] if len(b) > 1 else -1
            raise OSError(f"CONNACK refused (code {code}: "
                          f"{ {4: 'bad user/password', 5: 'not authorised'}.get(code, 'see MQTT spec') })")
        self.connected = True
        self.last_in = time.time()
        for i, t in enumerate(self.subs):
            self._queue(0x82, struct.pack(">H", i + 1) + self._str(t) + b"\x00")
        if self.on_connect:
            try:
                self.on_connect()
            except Exception as e:                           # a bad discovery/state publish must not loop reconnects
                self.log(f"# mqtt: on_connect error {e!r}")

    def _run(self):
        backoff = 2
        while not self.stop:
            try:
                self._connect()
                self.log(f"# mqtt: connected to {self.host}:{self.port}")
                backoff = 2
                last_ping = time.time()
                s = self.sock
                while not self.stop:
                    now = time.time()
                    if now - last_ping > self.keepalive / 2:
                        self._queue(0xC0, b"")                 # PINGREQ; the PINGRESP refreshes last_in
                        last_ping = now
                    if now - self.last_in > self.keepalive * 1.5:
                        raise OSError(f"no packets from the broker for {now - self.last_in:.0f} s (dead link)")
                    self._pump(s, 0.2)
                    while True:
                        pkt = self._parse()
                        if pkt is None:
                            break
                        h, b = pkt
                        if h >> 4 == 3:                      # PUBLISH (we only subscribe at QoS 0)
                            tl = struct.unpack(">H", b[:2])[0]
                            topic = b[2:2 + tl].decode("utf-8", "replace")
                            off = 2 + tl + (2 if (h >> 1) & 3 else 0)
                            if self.on_message:
                                try:
                                    self.on_message(topic, b[off:], bool(h & 1))
                                except Exception as e:
                                    self.log(f"# mqtt: handler error {e!r}")
            except Exception as e:                           # anything: drop, back off, reconnect - never die
                if self.connected or backoff == 2:
                    self.log(f"# mqtt: {self.host}:{self.port} not reachable ({e}); retrying")
            self._drop()
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ============================================================================ MQTT <-> Pellet burner
# Plain words for Home Assistant: STATE_WORDS and ALARM_TEXT (burner_common); the boiler's own Danish names stay in
# the *_label fields
HEALTH_WORDS = {"ok": "OK", "watch": "Watch", "clean": "Needs cleaning", "learning": "Learning", "worse": "Worse"}
HEALTH_KEYS = (("to_flame", "Health: time to flame"), ("start_flame", "Health: flame at start"),
               ("failed", "Health: failed lightings"), ("run_flame", "Health: flame while running"))


def _iso_tz(epoch):
    """ISO time with the local UTC offset (what Home Assistant's timestamp sensors need), or None."""
    try:
        return datetime.datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds") if epoch else None
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _bin_tpl(key):
    """Binary-sensor template: ON / OFF, or None (= unknown in Home Assistant) when there is no value."""
    return (f"{{% if value_json.{key} is none %}}None{{% elif value_json.{key} %}}ON{{% else %}}OFF{{% endif %}}")


# ---- software update (Raspberry Pi): the page only leaves a one-word request; pi/pelletpi-update.sh does the work
UPDATE_REQUESTS = ("check", "install", "retry", "back")
UPDATE_PHASES = ("check", "download", "waiting", "install", "back")
UPDATE_CODES = ("uptodate", "offline", "no_release", "no_repo", "updated", "rolled_back", "build_failed", "disk_full",
                "busy", "later", "available", "bad_skipped", "left_skipped", "went_back", "no_previous", "back_busy", "back_failed",
                "error", "local_changes", "unknown_base", "tidy_wait")
UPDATE_WHY = ("no_answer", "status", "settings", "boiler", "swap")


def update_auto(data_dir, on):
    """The page's "Automatic updates at night" switch: <data dir>/update-auto = "on" / "off". The Pi's updater reads it
    on its next run (AUTO_UPDATE=no on the SD card stays off). Returns (ok, msg)."""
    if not data_dir:
        return False, "no data folder"
    path = os.path.join(data_dir, "update-auto")
    try:
        if os.path.islink(path):
            os.remove(path)
        with open(path + ".tmp", "w", encoding="ascii") as fh:
            fh.write(("on" if on else "off") + "\n")
        os.replace(path + ".tmp", path)
    except OSError:
        return False, "could not save"
    return True, "saved"


def update_request(data_dir, what):
    """Leave the page's request for the Pi's updater: one fixed word in <data dir>/update-request. Returns (ok, msg).
    Nothing else can be asked for: no address, no version, no command."""
    if what not in UPDATE_REQUESTS:
        return False, "unknown request"
    if not data_dir:
        return False, "no data folder"
    path = os.path.join(data_dir, "update-request")
    try:
        with open(path + ".tmp", "w", encoding="ascii") as fh:
            fh.write(what + "\n")
        os.replace(path + ".tmp", path)
    except OSError:
        return False, "could not write the request"
    return True, "asked"


def _upd_ver(v):
    """A version name from update.json as short plain text, or None."""
    if not isinstance(v, str):
        return None
    v = "".join(c for c in v[:40] if c.isalnum() or c in "._-+")
    return v or None


def update_info(data_dir, now=None):
    """What the pages show about updates: the updater's data/update.json plus "view" - the state in one word
    ("key", the pages hold the texts), whether it is still at work ("busy"), which button fits ("btn": check /
    install / retry) and the version "Go back" would return to ("back"). {"current": None} = no updater here."""
    try:
        path = os.path.join(data_dir, "update.json")
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        age = (now or time.time()) - os.path.getmtime(path)
    except (TypeError, OSError, ValueError):
        return {"current": None}
    if not isinstance(d, dict) or not _upd_ver(d.get("current")):
        return {"current": None}
    now = now or time.time()
    try:
        req_age = now - os.path.getmtime(os.path.join(data_dir, "update-request"))
    except OSError:
        req_age = None
    cur, latest = _upd_ver(d.get("current")), _upd_ver(d.get("latest"))
    prev = d.get("previous") if isinstance(d.get("previous"), dict) else {}
    try:
        prev_v = _upd_ver(prev.get("version")) if float(prev.get("until") or 0) > now else None
    except (TypeError, ValueError):
        prev_v = None
    phase, code = d.get("phase"), d.get("code")
    old = "phase" not in d and "code" not in d                  # written by the first updater (before "Go back")
    busy, why = False, None
    # the updater touches the file while it waits; the download + build is one long step
    working = not old and phase in UPDATE_PHASES and age <= (3600 if phase == "download" else 900)
    if working:
        key, busy = "ph_" + phase, True
    elif req_age is not None and req_age < 90:
        key, busy = "asked", True
    elif req_age is not None:
        key = "no_updater"                                      # nobody took the request: no updater on this machine
    elif old:
        key = "old"
    elif phase in UPDATE_PHASES:
        key = "interrupted"
    else:
        key = code if code in UPDATE_CODES else "error"
        if key == "rolled_back" and d.get("why") in UPDATE_WHY:
            why = d["why"]
    left = _upd_ver(d.get("from")) if key == "went_back" else None    # "Go back" was pressed: offer the version that was left
    btn = None if busy else "install" if key in ("available", "later", "unknown_base") else \
        "retry" if key in ("bad_skipped", "left_skipped") or left else "check"
    auto = bool(d.get("auto"))
    try:                                    # a switch newer than the updater's last word: show the choice
        ap = os.path.join(data_dir, "update-auto")
        if not os.path.islink(ap) and os.path.getmtime(ap) >= os.path.getmtime(path):
            with open(ap, encoding="ascii", errors="ignore") as fh:
                w = fh.read(4).strip()
            if w in ("on", "off"):
                auto = w == "on"
    except OSError:
        pass
    out = {"current": cur, "latest": latest, "available": bool(d.get("available")), "auto": auto,
           "checked": d.get("checked") if isinstance(d.get("checked"), (int, float)) else None,
           "result": str(d.get("result") or "")[:200],
           "view": {"key": key, "busy": busy, "btn": btn, "auto": auto, "back": None if busy or old else prev_v, "why": why,
                    "current": cur, "latest": left or latest, "to": _upd_ver(d.get("to")) or latest,
                    "from": _upd_ver(d.get("from"))}}
    return out


def read_pi_health(data_dir, max_age_s=600):
    """Newest line of <data dir>/pi-health-*.jsonl (written every minute by pi/pi_health.py on the Raspberry Pi),
    or None when there is none or it is older than max_age_s."""
    if not data_dir:
        return None
    files = sorted(glob.glob(os.path.join(data_dir, "pi-health-????-??-??.jsonl")))
    if not files:
        return None
    try:
        with open(files[-1], "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            lines = fh.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            rec = parse_jsonl_line(line)
            if rec is None:
                continue                       # half-written line, NUL bytes after a power cut, or a cut-off start
            if time.time() - (rec.get("epoch") or 0) <= max_age_s:
                return rec
            return None
    except OSError:
        return None
    return None


NET_WEAK_PCT = 35           # Wi-Fi signal under this (%) counts as weak
_NET_CACHE = {}


def net_trouble(data_dir, now=None, cache_s=60):
    """How long the Pi's network has been bad, from the newest pi-health lines: {"weak_min": n, "down_min": n}
    (0 = fine now). Weak = Wi-Fi signal under NET_WEAK_PCT %; down = no ping or no name lookup. None without
    pi-health lines from the last 10 min. A gap of more than 10 min between lines ends a bad spell. Cached cache_s."""
    if not data_dir:
        return None
    now = time.time() if now is None else now
    hit = _NET_CACHE.get(data_dir)
    if hit and 0 <= now - hit[0] < cache_s:
        return hit[1]
    recs = []
    for p in sorted(glob.glob(os.path.join(data_dir, "pi-health-????-??-??.jsonl")))[-2:]:
        try:
            with open(p, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 98304))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        recs += [r for r in map(parse_jsonl_line, lines) if isinstance(r, dict) and _num(r.get("epoch"))]
    recs.sort(key=lambda r: r["epoch"])
    out = None
    if recs and now - recs[-1]["epoch"] <= 600:
        def weak(r):
            s = _num((r.get("net") or {}).get("wifi_signal"))
            return s is not None and s < NET_WEAK_PCT

        def down(r):
            i = r.get("internet") or {}
            return i.get("ok") is False or i.get("dns_ok") is False
        out = {}
        for key, bad in (("weak_min", weak), ("down_min", down)):
            first, nxt = None, None
            for r in reversed(recs):
                if not bad(r) or (nxt is not None and nxt - r["epoch"] > 600):
                    break
                first = nxt = r["epoch"]
            out[key] = int((recs[-1]["epoch"] - first) // 60) + 1 if first is not None else 0
    _NET_CACHE[data_dir] = (now, out)
    return out


def wifi_outage(data_dir, since):
    """The Wi-Fi watchdog's newest "back" event (<data dir>/wifi-watchdog.jsonl, pi/pelletpi_wifi_watchdog.py)
    for an outage that ended after `since` (epoch): {"lost_at": "hh:mm", "back_at": "hh:mm"} (the Pi's own clock),
    or None."""
    if not data_dir:
        return None
    try:
        with open(os.path.join(data_dir, "wifi-watchdog.jsonl"), "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 16384))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        r = parse_jsonl_line(line)
        if isinstance(r, dict) and r.get("ev") == "back" and (_num(r.get("epoch")) or 0) >= since                 and r.get("lost_at") and r.get("back_at"):
            return {"lost_at": str(r["lost_at"])[:5], "back_at": str(r["back_at"])[:5]}
    return None


class MqttBridge:
    """Topics (prefix default 'burner'):
      <p>/availability   online/offline (retained, LWT)
      <p>/state          JSON: live values + plain-word state/alarm, data age, version (retained, every status row
                         and at least every 30 s, so "data age" keeps counting when the boiler link is down)
      <p>/stats          JSON: pellets left / per day, ash bin, burner health, last start (with who / what started
                         it: last_start_source, last_start_by), heating plan (retained, 5 min)
      <p>/pi             JSON: Raspberry Pi health from pi-health-*.jsonl (retained, only when that file exists)
      <p>/cfg            JSON cfg_data_1 (retained, read only)
      <p>/event          transitions, alarms, commands, cfg-sync (not retained)
      <p>/prealarm, /cfg_sync, /schedule, /warm, /auto_reset, /pellets, /vvb, /ash_plate, /lux, /warning (retained)
      <p>/cmd/run_mode   <- 'start' / 'stop'  (same gate as the web page: needs --allow-remote-start; refused in ALARM)
      <p>/cmd/schedule   <- ON / OFF   auto-start windows on/off (a setting of this program, not of the boiler)
      <p>/cmd/heating_plan <- ON / OFF heating plan on/off (a setting of this program, not of the boiler)
      <p>/cmd/vvb        <- hot-water tank temperature (only when that sensor input is turned on in the settings)
    Boiler settings are never changed over MQTT (there is no cmd/cfg).
      <p>/cmd/result     -> result of each command
    With --mqtt-read-only every cmd topic except vvb is refused and no buttons / switches are offered.
    Retained command messages are ignored, so an old command can never fire after a reconnect.
    Home Assistant discovery: <discovery>/<component>/<node>/<object>/config, retained, one device "Pellet burner"."""

    STATS_EVERY_S = 300        # pellets / ash / health / last start / heating plan (reads the data files)
    STATE_EVERY_S = 30         # state is re-sent at least this often (data age, link)
    PI_EVERY_S = 60
    FLUE_FITTED_C = 60         # a real flue sensor reads far above this while burning; the board reports ~25-35 without one

    def __init__(self, mod, args, log, run_mode_cb=None, cfg_sync=None):
        self.mod, self.log, self.cfg_sync = mod, log, cfg_sync
        self.p = args.mqtt_prefix.rstrip("/")
        self.node = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in self.p) or "burner"
        self.disc = args.mqtt_discovery
        self.allow_remote = bool(getattr(args, "allow_remote_start", False))
        self.read_only = bool(getattr(args, "mqtt_read_only", False))
        self.flue_mode = getattr(args, "mqtt_flue", "auto") or "auto"
        self.data_dir = getattr(args, "data_dir", None) or None
        self.flue_seen = any(self._flue_hot(r) for r in list(getattr(mod, "hist", []) or []))
        self.stats, self.pi = {}, None
        self._stats_t = self._state_t = self._pi_t = 0.0
        self._disc_sig = None
        self._lock = threading.RLock()
        host, _, port = args.mqtt.partition(":")
        pw = args.mqtt_pass if args.mqtt_pass is not None else os.environ.get("BURNER_MQTT_PASS")
        user = args.mqtt_user if args.mqtt_user is not None else (os.environ.get("BURNER_MQTT_USER") or None)
        self.c = MqttClient(host, int(port or 1883), f"burner-{socket.gethostname()}"[:23], log,
                            user=user, password=pw,
                            will_topic=f"{self.p}/availability", will_payload=b"offline")
        self.c.on_connect = self._on_connect
        self.c.on_message = self._on_message
        self.c.subscribe(f"{self.p}/cmd/#")
        threading.Thread(target=self._loop, daemon=True, name="mqtt-ha").start()

    # ------------------------------------------------------------------ helpers
    def _flue_hot(self, r):
        try:
            return int(r.get("state")) == 4 and float(r.get("flue_c")) >= self.FLUE_FITTED_C
        except (TypeError, ValueError, AttributeError):
            return False

    def flue_fitted(self):
        return self.flue_mode == "yes" or (self.flue_mode == "auto" and self.flue_seen)

    def commands_on(self):
        return not self.read_only

    def _weather_on(self):
        w = getattr(self.mod, "weather", None)
        try:
            return bool(w and w.cfg().get("enabled"))
        except Exception:
            return False

    def _vvb_on(self):
        v = getattr(self.mod, "vvb", None)
        try:
            return bool(v and v.cfg().get("enabled"))
        except Exception:
            return False

    def _signature(self):
        """What decides which entities exist; discovery is re-sent when it changes."""
        return (self.flue_fitted(), self._weather_on(), self._vvb_on(), self.pi is not None,
                bool(self.mod.settings), bool(getattr(self.mod, "warm", None)), bool(self.mod.lux),
                bool(getattr(self.mod, "auto_reset", None)), bool(getattr(self.mod, "optimiser", None)),
                bool(getattr(self.mod, "ash", None)), bool(self.cfg_sync), self.commands_on(), self.allow_remote)

    def _loop(self):
        """Own thread: periodic state (data age), slow stats, Pi health, discovery changes. Never touches the bus."""
        while not self.c.stop:
            time.sleep(1.0)
            if not self.c.connected:
                continue
            try:
                now = time.time()
                if now - self._pi_t >= self.PI_EVERY_S:
                    self._pi_t = now
                    self.pi = read_pi_health(self.data_dir)
                    if self.pi is not None:
                        self.c.publish(f"{self.p}/pi", self._pi_payload(self.pi), retain=True)
                if now - self._stats_t >= self.STATS_EVERY_S:
                    self._stats_t = now
                    self.publish_stats()
                if self.disc and self._disc_sig is not None and self._signature() != self._disc_sig:
                    self._discovery()
                    self.publish_state()
                if now - self._state_t >= self.STATE_EVERY_S:
                    self.publish_state()
            except Exception as e:                       # never let the helper thread die
                self.log(f"# mqtt: helper error {e!r}")

    def _on_connect(self):
        self.c.publish(f"{self.p}/availability", "online", retain=True)
        self.pi = read_pi_health(self.data_dir)
        if self.pi is not None:
            self.c.publish(f"{self.p}/pi", self._pi_payload(self.pi), retain=True)
        if self.disc:
            self._discovery()
        self.publish_state()
        with self.mod.lock:                              # "Last warning" has a value from the start
            last = [e for e in self.mod.recent_events if e.get("type") == "warning"][-1:]
        ev = last[0] if last else {"text": "No warning"}
        self.c.publish(f"{self.p}/warning", {"text": ev.get("text") or "", "source": ev.get("source"),
                                             "kind": ev.get("kind"), "t": ev.get("t")}, retain=True)
        if self.stats:
            self.c.publish(f"{self.p}/stats", self.stats, retain=True)
        self._stats_t = 0.0                              # fresh stats soon (on the helper thread)

    def _dev(self):
        return {"identifiers": [f"{self.node}_j4"], "name": "Pellet burner", "model": "Pellet burner monitor",
                "manufacturer": "Pellet burner monitor project", "sw_version": MONITOR_VERSION}

    # ------------------------------------------------------------------ discovery
    def entities(self):
        """[(component, object_id, config or None)] - None = not active now (its retained config is removed)."""
        p, S, T, PI = self.p, f"{self.p}/state", f"{self.p}/stats", f"{self.p}/pi"
        mod, out = self.mod, []
        opt = bool(getattr(mod, "optimiser", None))
        cmds = self.commands_on()

        def add(comp, obj, name, topic=None, key=None, active=True, uid=None, tpl=None, **kw):
            if not active:
                out.append((comp, obj, None))
                return
            cfg = {"name": name, "unique_id": f"{self.node}_{uid or obj}"}
            if topic:
                cfg["state_topic"] = topic
            if key:
                cfg["value_template"] = tpl or (_bin_tpl(key) if comp == "binary_sensor" else f"{{{{ value_json.{key} }}}}")
            elif tpl:
                cfg["value_template"] = tpl
            cfg.update({k: v for k, v in kw.items() if v is not None})
            out.append((comp, obj, cfg))

        T_C = dict(unit_of_measurement="°C", device_class="temperature", state_class="measurement")
        PCT = dict(unit_of_measurement="%", state_class="measurement")
        DIAG = "diagnostic"
        # live values
        add("sensor", "state_label", "State", S, "state_text", device_class="enum", options=list(STATE_WORDS.values()),
            icon="mdi:state-machine")
        add("sensor", "state", "State number", S, "state", uid="state_code", icon="mdi:numeric", entity_category=DIAG)
        add("sensor", "alarm_label", "Alarm", S, "alarm_text", icon="mdi:alert")
        add("sensor", "alarm_code", "Alarm number", S, "alarm", icon="mdi:numeric", entity_category=DIAG)
        add("binary_sensor", "alarm", "Alarm active", S, "alarm_active", uid="alarm_active", device_class="problem")
        add("sensor", "boiler_c", "Boiler temperature", S, "boiler_c", **T_C)
        add("sensor", "flue_c", "Flue temperature", S, "flue_c", active=self.flue_fitted(), **T_C)
        add("sensor", "outside_c", "Outside temperature", S, "outside_c", active=self._weather_on(), **T_C)
        add("sensor", "setpoint", "Boiler setpoint", S, "setpoint", icon="mdi:thermometer-check", **T_C)
        add("sensor", "power_pct", "Burner power", S, "power_pct", icon="mdi:fire", **PCT)
        add("sensor", "flame", "Flame", S, "flame", state_class="measurement", icon="mdi:fire-circle")
        add("sensor", "fan_pct", "Fan", S, "fan_pct", icon="mdi:fan", **PCT)
        add("sensor", "fan_rpm", "Fan speed", S, "fan_rpm", unit_of_measurement="rpm", state_class="measurement",
            icon="mdi:fan")
        add("binary_sensor", "auger", "Auger", S, "auger_on", device_class="running", icon="mdi:screw-lag")
        add("binary_sensor", "igniter", "Igniter", S, "igniter_on", device_class="heat")
        add("sensor", "silo_kg", "Silo content", S, "silo_kg", unit_of_measurement="kg", device_class="weight",
            state_class="measurement", icon="mdi:silo")
        add("sensor", "ignitions", "Ignitions", S, "ignitions", state_class="total_increasing", icon="mdi:counter")
        add("binary_sensor", "link", "Boiler link", S, "connected", device_class="connectivity", entity_category=DIAG)
        add("sensor", "data_age_s", "Data age", S, "data_age_s", unit_of_measurement="s", device_class="duration",
            state_class="measurement", entity_category=DIAG)
        add("sensor", "version", "Monitor version", S, "version", icon="mdi:tag", entity_category=DIAG)
        add("binary_sensor", "remote_armed", "Remote start/stop allowed", S, "remote_armed",
            icon="mdi:remote", entity_category=DIAG)
        # slow values (data files)
        add("sensor", "pellets_home", "Pellets left", T, "pellets_left_kg", active=opt, unit_of_measurement="kg",
            device_class="weight", state_class="measurement", icon="mdi:sack",
            json_attributes_topic=f"{p}/pellets")
        add("sensor", "pellets_days_left", "Pellets days left", T, "pellets_days_left", active=opt,
            unit_of_measurement="d", device_class="duration", state_class="measurement", icon="mdi:calendar-end")
        add("sensor", "pellets_per_day", "Pellets per day", T, "pellets_per_day_kg", active=opt,
            unit_of_measurement="kg", device_class="weight", state_class="measurement", icon="mdi:chart-bar")
        add("sensor", "ash_bin_pct", "Ash bin fill", T, "ash_bin_pct", active=opt, icon="mdi:delete-variant", **PCT)
        add("sensor", "ash_kg", "Ash in bin", T, "ash_kg", active=opt, unit_of_measurement="kg",
            device_class="weight", state_class="measurement", icon="mdi:delete-variant")
        add("sensor", "health", "Burner health", T, "health", active=opt, icon="mdi:heart-pulse",
            json_attributes_topic=T, json_attributes_template="{{ value_json.health_detail | tojson }}")
        for k, name in HEALTH_KEYS:
            add("sensor", f"health_{k}", name, T, f"health_{k}", active=opt, icon="mdi:stethoscope",
                json_attributes_topic=T,
                json_attributes_template=f"{{{{ value_json.health_detail.{k} | tojson }}}}")
        ign = bool(getattr(mod, "ignitions", None))
        add("sensor", "last_start_min", "Last start lighting time", T, "last_start_min", active=ign,
            unit_of_measurement="min", device_class="duration", state_class="measurement", icon="mdi:timer-outline")
        add("sensor", "last_start_result", "Last start result", T, "last_start_result", active=ign, icon="mdi:fire-alert")
        add("sensor", "last_start_at", "Last start", T, "last_start_at", active=ign, device_class="timestamp",
            json_attributes_topic=T,                       # who / what started it, as attributes
            json_attributes_template="{{ {'source': value_json.last_start_source, "
                                     "'started_by': value_json.last_start_by} | tojson }}")
        warm = bool(getattr(mod, "warm", None))
        add("sensor", "next_start", "Heating plan next start", T, "next_start", active=warm, device_class="timestamp",
            icon="mdi:calendar-clock")
        # Raspberry Pi health (only on a Pi with pi_health.py writing the file)
        pi = self.pi is not None
        add("sensor", "pi_cpu_c", "Pi CPU temperature", PI, "cpu_c", active=pi, entity_category=DIAG, **T_C)
        add("sensor", "pi_load", "Pi CPU load", PI, "load1", active=pi, state_class="measurement", icon="mdi:cpu-64-bit",
            entity_category=DIAG)
        add("sensor", "pi_disk_free", "Pi free disk", PI, "disk_free_mb", active=pi, unit_of_measurement="MB",
            device_class="data_size", state_class="measurement", entity_category=DIAG)
        add("sensor", "pi_mem_free", "Pi free memory", PI, "mem_free_mb", active=pi, unit_of_measurement="MB",
            device_class="data_size", state_class="measurement", entity_category=DIAG)
        add("sensor", "pi_uptime", "Pi uptime", PI, "uptime_h", active=pi, unit_of_measurement="h",
            device_class="duration", state_class="measurement", entity_category=DIAG)
        add("sensor", "pi_wifi", "Pi Wi-Fi signal", PI, "wifi_signal", active=pi, unit_of_measurement="%",
            state_class="measurement", icon="mdi:wifi", entity_category=DIAG)
        add("binary_sensor", "pi_internet", "Pi internet", PI, "internet_ok", active=pi, device_class="connectivity",
            entity_category=DIAG)
        add("binary_sensor", "pi_undervolt", "Pi under-voltage", PI, "undervolt", active=pi, device_class="problem",
            entity_category=DIAG)
        add("binary_sensor", "pi_usb_adapter", "Pi USB adapter", PI, "usb_adapter", active=pi, device_class="plug",
            entity_category=DIAG)
        # features that were there before (same unique ids, so Home Assistant keeps its history)
        add("sensor", "cfg_sync", "Settings sync", f"{p}/cfg_sync", "status", active=bool(self.cfg_sync),
            json_attributes_topic=f"{p}/cfg_sync", icon="mdi:sync")
        add("sensor", "schedule_status", "Auto-start status", f"{p}/schedule", "code", active=bool(mod.settings),
            json_attributes_topic=f"{p}/schedule", icon="mdi:calendar-clock")
        add("sensor", "lux_status", "Flame sensor", f"{p}/lux", "status", active=bool(mod.lux),
            json_attributes_topic=f"{p}/lux", icon="mdi:eye-check")
        add("sensor", "auto_reset", "Saved by auto alarm reset", f"{p}/auto_reset", "saves", uid="auto_reset_saves",
            active=bool(getattr(mod, "auto_reset", None)), json_attributes_topic=f"{p}/auto_reset",
            state_class="total_increasing", icon="mdi:restart-alert")
        add("sensor", "vvb", "Hot-water tank (sent to boiler)", f"{p}/vvb", "sent_c", uid="vvb_sent",
            active=self._vvb_on(), json_attributes_topic=f"{p}/vvb", icon="mdi:water-boiler", **T_C)
        ash = bool(getattr(mod, "ash", None))
        add("binary_sensor", "ash_out", "Ash plate out of home", f"{p}/ash_plate", None, active=ash,
            tpl="{% if value_json.out is none %}None{% elif value_json.out == 1 %}ON{% else %}OFF{% endif %}",
            icon="mdi:rotate-3d-variant")
        add("binary_sensor", "ash_problem", "Ash plate problem", f"{p}/ash_plate", "problem", active=ash,
            device_class="problem", json_attributes_topic=f"{p}/ash_plate",
            tpl="{{ 'ON' if value_json.problem else 'OFF' }}")
        add("sensor", "ash_turns_24h", "Ash plate turns (24 h)", f"{p}/ash_plate", "turns_24h", active=ash,
            state_class="measurement", icon="mdi:rotate-3d-variant")
        add("sensor", "last_warning", "Last warning", f"{p}/warning", None, tpl="{{ value_json.text[:250] }}",
            json_attributes_topic=f"{p}/warning", icon="mdi:alert-outline")
        # commands: switches for this program's own plans, buttons for start / stop (same gate as the web page)
        add("switch", "schedule", "Auto-start schedule", f"{p}/schedule", None, uid="schedule_switch",
            active=bool(mod.settings) and cmds, command_topic=f"{p}/cmd/schedule",
            tpl="{{ 'ON' if value_json.enabled else 'OFF' }}", payload_on="ON", payload_off="OFF",
            icon="mdi:calendar-clock")
        add("binary_sensor", "schedule_on", "Auto-start schedule", f"{p}/schedule", "enabled",
            active=bool(mod.settings) and not cmds, icon="mdi:calendar-clock")
        add("switch", "heating_plan", "Heating plan", f"{p}/warm", None, uid="heating_plan_switch",
            active=warm and bool(mod.settings) and cmds, command_topic=f"{p}/cmd/heating_plan",
            tpl="{{ 'ON' if value_json.enabled else 'OFF' }}", payload_on="ON", payload_off="OFF",
            json_attributes_topic=f"{p}/warm", icon="mdi:home-thermometer")
        add("binary_sensor", "heating_plan_on", "Heating plan", f"{p}/warm", "enabled",
            active=warm and bool(mod.settings) and not cmds, icon="mdi:home-thermometer")
        for cmd, name, icon in (("start", "Start burner", "mdi:play"), ("stop", "Stop burner", "mdi:stop")):
            add("button", cmd, name, None, None, uid=f"btn_{cmd}", active=cmds and self.allow_remote,
                command_topic=f"{p}/cmd/run_mode", payload_press=cmd, icon=icon)
        avail = {"availability_topic": f"{p}/availability", "payload_available": "online",
                 "payload_not_available": "offline"}
        dev = self._dev()
        origin = {"name": "Pellet burner monitor", "sw_version": MONITOR_VERSION}
        for _, _, cfg in out:
            if cfg is not None:
                cfg.update(avail)
                cfg["has_entity_name"] = True
                cfg["device"] = dev
                cfg["origin"] = origin
        return out

    def _discovery(self):
        d = self.disc.rstrip("/")
        for comp, obj, cfg in self.entities():
            self.c.publish(f"{d}/{comp}/{self.node}/{obj}/config", cfg if cfg is not None else b"", retain=True)
        self._disc_sig = self._signature()

    # ------------------------------------------------------------------ state
    def publish_state(self):
        if not self.c.connected:
            return
        with self._lock:
            self._state_t = time.time()
            self._publish_state()

    def _publish_state(self):
        snap = self.mod.feed_snapshot()
        now = time.time()
        latest, cfg = snap.get("latest") or {}, snap.get("cfg") or {}
        f = dict(latest.get("fields") or {})
        for k in ("state", "boiler_c", "flue_c", "power_pct", "silo_kg", "ignitions", "fan_rpm", "fan_pct",
                  "auger_on", "igniter_on"):
            f.setdefault(k, None)
        st, al = f.get("state"), f.get("alarm") or 0
        try:
            st_i = int(st) if st is not None else None
            al_i = int(al)
        except (TypeError, ValueError):
            st_i, al_i = None, 0
        if self._flue_hot(f):
            self.flue_seen = True
        outside = None
        w = getattr(self.mod, "weather", None)
        if w and self._weather_on():
            try:
                wf = w.feed()
                if wf.get("c") is not None and (wf.get("age_min") or 0) <= 180:   # an old reading is not "now"
                    outside = wf["c"]
            except Exception:
                pass
        ep = latest.get("epoch")
        f.update({"state": st_i, "state_text": STATE_WORDS.get(st_i) if st_i is not None else None,
                  "state_label": STATE_LABELS.get(st_i, "unknown") if st_i is not None else "unknown",
                  "alarm": al_i if st_i is not None else None,
                  "alarm_active": (bool(al_i) or st_i == 6) if st_i is not None else None,
                  "alarm_text": (ALARM_TEXT.get(al_i) or f"Alarm {al_i}") if st_i is not None else None,
                  "alarm_label": (snap["labels"]["alarms"].get(al_i) or (f"alarm {al_i}" if al_i else "none")),
                  "connected": bool(snap.get("connected")), "t": latest.get("t"),
                  "data_age_s": round(now - ep) if ep else None,
                  "setpoint": (cfg.get("fields") or {}).get("setpoint_c"),
                  "outside_c": outside, "flue_fitted": self.flue_fitted(),
                  "remote_armed": self.allow_remote, "commands": self.commands_on(), "version": MONITOR_VERSION})
        ff = snap.get("flame_field")
        f["flame"] = (latest.get("raw") or {}).get(ff) if ff in ("003", "004") else None
        self.c.publish(f"{self.p}/state", f, retain=True)
        if cfg.get("raw"):
            self.c.publish(f"{self.p}/cfg", cfg["raw"], retain=True)
        if snap.get("cfg_sync"):
            self.c.publish(f"{self.p}/cfg_sync", snap["cfg_sync"], retain=True)
        if self.mod.settings and snap.get("schedule"):
            sc = snap["schedule"]
            self.c.publish(f"{self.p}/schedule", {
                "enabled": bool(self.mod.settings.get("schedule").get("enabled")),
                "code": sc["status"].get("code"), "text": sc["status"].get("text"),
                "starts_today": sc.get("starts_today"), "blocked": sc.get("blocked")}, retain=True)
        if snap.get("warm"):
            wm = snap["warm"]
            self.c.publish(f"{self.p}/warm", {"enabled": bool(wm.get("enabled")),
                                              "code": (wm.get("status") or {}).get("code"),
                                              "text": (wm.get("status") or {}).get("text"),
                                              "next_start": _iso_tz(wm.get("next_start"))}, retain=True)
        if snap.get("auto_reset"):
            ar = snap["auto_reset"]
            self.c.publish(f"{self.p}/auto_reset", {"saves": ar.get("saves", 0), "status": ar["status"].get("code"),
                                                    "text": ar["status"].get("text"), "last_save": ar.get("last_save"),
                                                    "attempts_24h": ar.get("attempts_24h")}, retain=True)
        if snap.get("vvb") and snap["vvb"].get("enabled"):
            self.c.publish(f"{self.p}/vvb", snap["vvb"], retain=True)
        if snap.get("ash_plate"):
            a = snap["ash_plate"]
            self.c.publish(f"{self.p}/ash_plate", {
                "out": a.get("out"), "home": a.get("home"), "turns_24h": a.get("turns_24h"),
                "last_turn": _iso_tz(a.get("last_turn")), "last_out_s": a.get("last_out_s"),
                "problem": a.get("problem"), "problem_text": (a.get("problem") or {}).get("text")}, retain=True)
        if self.mod.lux and time.time() - getattr(self, "_lux_t", 0) > 60:
            self._lux_t = time.time()
            r = self.mod.lux.result or self.mod.lux.analyse()      # cached result; never the sample list
            self.c.publish(f"{self.p}/lux", {"status": r.get("status"), "running_h": r.get("running_h"),
                                             "warnings": [w["text"] for w in r.get("warnings", [])],
                                             "trend": r.get("trend")}, retain=True)

    # ------------------------------------------------------------------ slow stats
    def compute_stats(self):
        """Pellets, ash bin, burner health, last start, heating plan. Reads the data files: helper thread only."""
        m = self.mod
        out = {"t": _iso_tz(time.time()), "pellets_left_kg": None, "pellets_days_left": None,
               "pellets_per_day_kg": None, "pellets_buy_by": None, "ash_bin_pct": None, "ash_kg": None,
               "ash_full_at": None, "health": None, "health_detail": {k: {} for k, _ in HEALTH_KEYS},
               "last_start_min": None, "last_start_result": None, "last_start_at": None,
               "last_start_source": None, "last_start_by": None, "next_start": None,
               "stock": None}
        for k, _ in HEALTH_KEYS:
            out[f"health_{k}"] = None

        def safe(fn):
            try:
                return fn()
            except Exception as e:
                self.log(f"# mqtt: stats error {e!r}")
                return None
        opt = getattr(m, "optimiser", None)
        if opt:
            st = safe(opt.pellet_stock)
            if st:
                out.update(pellets_left_kg=st.get("left"), pellets_days_left=st.get("days_left"),
                           pellets_per_day_kg=st.get("per_day"), pellets_buy_by=_iso_tz(st.get("buy_by")))
                out["stock"] = st
            days = safe(opt.report_days) or []
            if out["pellets_per_day_kg"] is None:          # no pellet count entered: the same rule, without a stock
                out["pellets_per_day_kg"] = (safe(opt.per_day) or {}).get("kg")
            ash = safe(opt.ash_status)
            if ash and ash.get("emptied_at"):
                out.update(ash_bin_pct=ash.get("full_pct"), ash_kg=ash.get("ash_kg"),
                           ash_full_at=_iso_tz(ash.get("full_at")))
            h = safe(lambda: opt.health(days))
            if h:
                out["health"] = HEALTH_WORDS.get(h.get("status"), h.get("status"))
                for i in h.get("items") or []:
                    if i.get("key") in out["health_detail"]:
                        out[f"health_{i['key']}"] = HEALTH_WORDS.get(i.get("state"), i.get("state"))
                        out["health_detail"][i["key"]] = {k: v for k, v in i.items() if k != "key"}
        ig = getattr(m, "ignitions", None)
        if ig:
            recs = [r for r in list(ig.records) if not r.get("relight")]
            if recs:
                last = recs[-1]
                out["last_start_result"] = last.get("result")
                out["last_start_at"] = _iso_tz(last.get("epoch"))
                out["last_start_source"] = last.get("source")
                out["last_start_by"] = source_text(last.get("source"))
                out["last_start_min"] = last.get("minutes") if last.get("result") == "ok" else None
        warm = getattr(m, "warm", None)
        if warm:
            wf = safe(warm.feed)
            if wf and wf.get("enabled"):
                out["next_start"] = _iso_tz(wf.get("next_start"))
        return out

    def publish_stats(self):
        s = self.compute_stats()
        stock = s.pop("stock", None)
        self.stats = s
        self.c.publish(f"{self.p}/stats", s, retain=True)
        if stock:
            self.c.publish(f"{self.p}/pellets", stock, retain=True)

    @staticmethod
    def _pi_payload(r):
        pw, net, inet = r.get("power") or {}, r.get("net") or {}, r.get("internet") or {}
        return {"t": r.get("t"), "cpu_c": r.get("cpu_c"), "load1": r.get("load1"),
                "disk_free_mb": r.get("disk_free_mb"), "mem_free_mb": r.get("mem_free_mb"),
                "uptime_h": round(r["uptime_s"] / 3600, 1) if isinstance(r.get("uptime_s"), (int, float)) else None,
                "wifi_signal": net.get("wifi_signal"), "internet_ok": inet.get("ok"),
                "undervolt": (bool(pw.get("undervolt_now")) or bool(pw.get("undervolt_since_boot"))) if pw else None,
                "usb_adapter": bool(r.get("usb_serial")) if "usb_serial" in r else None}

    # ------------------------------------------------------------------ events / commands
    def event(self, ev):
        if not self.c.connected:
            return
        self.c.publish(f"{self.p}/event", ev)
        if ev.get("type") == "transition" and ev.get("to") in (0, 1, 4, 6):
            self._stats_t = 0.0                          # a start just ended: fresh "last start" soon
        if ev.get("type") == "alarm" and self.mod.tracker and self.mod.tracker.prealarm:
            self.c.publish(f"{self.p}/prealarm", self.mod.tracker.prealarm, retain=True)
        if ev.get("type") == "cfg_sync" and self.cfg_sync:
            self.c.publish(f"{self.p}/cfg_sync", self.cfg_sync.feed(), retain=True)
        if ev.get("type") == "warning":
            # retained, so HA's "Last warning" sensor survives restarts; an automation can notify on change
            self.c.publish(f"{self.p}/warning", {"text": ev.get("text", ""), "source": ev.get("source"),
                                                 "kind": ev.get("kind"), "t": ev.get("t")}, retain=True)

    def _result(self, cmd, ok, msg):
        self.log(f"# mqtt: cmd {cmd}: {msg}")
        self.c.publish(f"{self.p}/cmd/result", {"cmd": cmd, "ok": ok, "msg": msg, "t": _now_iso()})

    @staticmethod
    def _on_off(text):
        t = text.strip().upper()
        if t in ("ON", "1", "TRUE", "ENABLE"):
            return True
        if t in ("OFF", "0", "FALSE", "DISABLE"):
            return False
        return None

    def _on_message(self, topic, payload, retained):
        sub = topic[len(self.p) + 1:] if topic.startswith(self.p + "/") else topic
        if sub == "cmd/result" or not payload:
            return                                       # an empty message only clears a retained one
        if retained:
            self._result(sub, False, "ignored a RETAINED command (never act on old commands)")
            return
        text = payload.decode("utf-8", "replace").strip()
        if sub == "cmd/vvb":
            # hot-water tank temperature (a sensor reading, turned on in the settings); answered only when refused
            vv = getattr(self.mod, "vvb", None)
            if not vv:
                return
            try:
                v = json.loads(text)
                v = v.get("c") if isinstance(v, dict) else v
            except ValueError:
                v = text
            ok, msg = vv.put(v, "mqtt")
            if not ok:
                self._result("vvb", False, msg)
            return
        if not self.commands_on():
            self._result(sub, False, "MQTT is read-only (--mqtt-read-only)")
            return
        if sub == "cmd/run_mode":
            cmd = text.lower()
            # the web page's own gate (--allow-remote-start), plus: never in ALARM (no automatic alarm reset)
            ok, msg, _ = self.mod.request_run_mode(cmd, "mqtt", from_hub=True)
            self._result(cmd, ok, msg)
        elif sub in ("cmd/schedule", "cmd/heating_plan"):
            what = "schedule" if sub == "cmd/schedule" else "warm"
            if not self.mod.settings or (what == "warm" and not getattr(self.mod, "warm", None)):
                self._result(sub[4:], False, "not available")
                return
            on = self._on_off(text)
            if on is None:
                self._result(sub[4:], False, "send ON or OFF")
                return
            ok, msg = self.mod.settings.update({what: {"enabled": on}})
            name = "auto-start schedule" if what == "schedule" else "heating plan"
            self.mod.note_event({"type": "settings", "epoch": time.time(), "ok": ok, "msg": f"{name} {'ON' if on else 'OFF'} (MQTT)",
                                 "keys": [what]})
            self._result(sub[4:], ok, f"{name} {'ON' if on else 'OFF'}: {msg}")
            self.publish_state()
        else:                                            # incl. the old cmd/cfg: boiler settings never change over MQTT
            self._result(sub, False, "unknown command topic")
