#!/usr/bin/env python3
"""Pellet burner module-port (J4) responder.

SAFETY: transmits ONLY when --live is given.  Without --live (or with --dry-run)
the port is opened for reading and the answers are only printed.

  py burner_responder.py --dry-run --duration 30
  py burner_responder.py --live --duration 120 [--addr 0x01]
"""
import argparse, json, struct, time, datetime, sys, os, re, threading, collections, zlib
from burner_sniff import (Splitter, Logger, build_frame, Frame, TYPES, ts)
import burner_common
from burner_common import STATE_LABELS, ALARM_LABELS, SOURCES    # state / alarm names, who started or stopped it

EOT = b"\x04"
EXPERIMENT = False   # set by --experiment
OK = {"devicedrive": {"result": "OK"}}
STATUS = {"devicedrive": {"status": {"connection_status": "GOT_IP"}}}

# ---- status_info / cfg_data_1 field names (as seen in the module traffic) ----
# Field 004 ("sensor4") = flame / LUX (the boiler's flame limits, 17 in IGNITION and 3/50/129 in DRIFT, are on
# this scale; bench test still to confirm). Field 003 ("analog3") is
# another analog value, not the flame. Which field feeds 'flame' is a setting (default 004).
STATUS_NAMES = {
    "000": "state", "001": "boiler_c", "002": "flue_c", "003": "analog3", "004": "sensor4",
    "005": "power_pct", "006": "state2", "007": "alarm", "008": "silo_kg", "009": "ignitions",
    # 012 din2 = ASKEPLADEKONTAKT (0 = closed = plate home, 1 = open)
    "010": "phase_step", "011": "din1", "012": "din2", "013": "fan_rpm", "014": "fan_pct",
    "015": "ch3_status", "016": "din3", "017": "auger_on", "018": "igniter_on",
}
# a small, useful subset of cfg_data_1 (the most useful fields)
CFG_NAMES = {
    "000": "setpoint_c", "001": "fuel_gph", "004": "ash_interval_g", "005": "stop_ved_pct",
    "010": "silo_capacity", "031": "max_power", "033": "boiler_model",
    "035": "pid_kp", "036": "pid_ki", "037": "pid_kd",
}
_NUM3 = re.compile(r"^\d{3}$")
# parts of /status.json that change seldom: with ?since= they are sent only when changed (see feed_snapshot)
SLOW_PARTS = ("transitions", "events", "prealarm", "labels", "warm", "ash_plate", "weather", "schedule", "auto_reset",
              "warnings", "remote", "vvb", "cfg", "minmode", "cfg_sync", "cfg_changes")   # the pages hold the same list (keep in step)
_TICK_KEYS = ("t", "last_epoch")    # "checked at" times that move every poll; the pages don't show them
# small keys of a slow part that change often (a status line, a count-down, the data rate): they have their own version,
# so a change sends only them (in "hot"), not the whole part with its lists (warm plan, ash turns, weather hours)
HOT_KEYS = {"warm": ("status", "rest_since", "lead_now"),
            "ash_plate": ("contact", "out", "home", "since", "out_s", "last_turn", "last_home", "last_out_s", "rate_s",
                          "last_epoch", "turns_24h", "problem"),
            "weather": ("age_min",)}


def _steady(v, depth=0):
    """v without the per-poll "checked at" times, for the part's version only (the answer still carries them)."""
    if isinstance(v, dict) and depth < 3:
        return {k: _steady(x, depth + 1) for k, x in v.items()
                if not (k in _TICK_KEYS and isinstance(x, (int, float)))}
    return v

# Replay mode (tools/replay.py only). None in every normal run - then nothing below changes.
# When set it is an object with: attach(mod), status() -> dict, control(body) -> (http code, dict),
# inject_html(bytes) -> bytes, insights_path. In replay no serial port is opened and nothing is sent.
REPLAY = None


def _find_numeric_dict(o):
    """Return the first dict whose keys are all 3-digit strings (status_info / cfg_data_1)."""
    if isinstance(o, dict):
        if o and all(_NUM3.match(k) for k in o.keys()):
            return o
        for v in o.values():
            r = _find_numeric_dict(v)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_numeric_dict(v)
            if r is not None:
                return r
    return None


class Module:
    def __init__(self, addr, idle="ok"):
        self.addr = addr
        self.idle = idle
        self.json_in = bytearray()     # accumulated reg-3 write payload
        self.pending = None            # JSON text to return on next reg-3 read
        self.seen_json = collections.deque(maxlen=50)   # last JSON requests received (bounded for 24/7)
        self.sticky = None
        self.setup_n = 0
        self.reg1 = 1
        self.svc, self.svc_every, self.svc_last = None, 0.0, 0.0   # --live-interval: service request
        self.svc_once = None           # one-shot service object (e.g. {"run_mode":"start"}) sent on next keep-alive
        self.svc_once_meta = None      # {"t": queued epoch, "source": ...}
        self.cmd_lock = threading.Lock()
        self.fw_mode = False                                  # --fw-mode: replies as the controller expects them
        self.status_cands, self.status_n = [], 0              # --status-experiment: answers to the status command
        self.cc_cands, self.cc_n, self.cc_last = [], 0, 0.0  # --cc-experiment: answers to {"ConnectCode":N}
        self.inject_text, self.inject_at = None, 0.0  # --inject / --inject-after
        self.reg0 = 0.0               # --reg0: float answered to reg0 reads (maybe VVB/tank temperature)
        self.delay_seq = []            # set from --delay-seq: reg3 poll number at which the setup answer is given
        self.in_setup, self.poll_n = False, 0
        self.reg1_seq = []             # set from --reg1-seq: value tried per setup cycle (bit0 always kept 1)
        # ---- remote start/stop (restored OEM feature; arming = accidental-trigger hygiene) ----
        # bit0 of reg1 = "no remote start". It is FORCED to 1 on every reg1 read unless
        # allow_remote_start is explicitly armed (--allow-remote-start). Even when armed, a START is
        # only sent on demand (service.run_mode via the remote_cmd trigger file); STOP reverts bit0.
        self.allow_remote_start = False
        # ---- read-only live telemetry capture (for the SCADA feed) ----
        self.lock = threading.Lock()
        self.latest = None             # last status_info snapshot {epoch,t,fields,raw}
        self.latest_cfg = None         # last cfg_data_1 snapshot
        self.hist = collections.deque(maxlen=720)   # chart history rows
        self.last_rx = 0.0             # epoch of the last frame received from the controller
        self.started = time.time()     # epoch this program started (shown; the clock can jump at NTP sync)
        self.started_mono = time.monotonic()   # the updater's "quiet" rule uses this: it never jumps
        self.last_rx_mono = 0.0        # monotonic time of the last frame (0 = none yet)
        self.rx_mark = None            # data/last_rx: touched while frames arrive (the updater: still talking?)
        self._rx_mark_at = None
        # ---- optional extensions (burner_ext.py), all None unless enabled on the command line ----
        self.store = None              # Store: history/events on disk
        self.tracker = None            # Tracker: transition log + pre-alarm snapshot
        self.cfg_sync = None           # CfgSync: keep cfg_data_1 equal to a desired-settings file
        self.cfg_hist = None           # CfgHistory: log of the boiler's own settings changes (data/cfg_changes.jsonl)
        self.on_status = None          # callback after every status_info (MQTT publish)
        self.on_event = None           # callback(event dict) (MQTT event)
        self.last_cmd = None           # last remote run_mode command and its delivery
        self.remote_armed = False
        self.settings = None           # Settings: web-editable settings (schedule, LUX field, ...)
        self.scheduler = None          # Scheduler: start windows
        self.lux = None                # LuxMonitor: flame-sensor fouling
        self.optimiser = None          # Optimiser: daily summaries + suggestions
        self.fan = None                # FanMonitor: bearing-wear groundwork (beta, recording only)
        self.auto_reset = None         # AutoReset: clear + restart nuisance alarms (opt-in)
        self.ignitions = None          # IgnitionLog: time-to-flame history per start attempt
        self.warm = None               # WarmMode: the web's own minimum mode (opt-in)
        self.ash = None                # AshPlate: ash-plate home switch watcher (read-only)
        self.weather = None            # Weather: outside temperature from Open-Meteo (opt-in)
        self.minmode = None            # MinModeWatch: is the boiler's own minimum mode on? (seen from behaviour)
        self.vvb = None               # VvbInput: hot-water tank temperature from the owner's sensor (opt-in)
        self.run_mode_cb = None        # queue_run_mode(cmd, source) when remote start/stop is armed
        self.warnings = collections.deque(maxlen=20)   # recent warnings (schedule, LUX) for the dashboard
        self.recent_events = collections.deque(maxlen=150)   # event log for the dashboard (survives page reloads)
        d = lambda o: json.dumps(o, separators=(",", ":"))
        # one candidate per handshake cycle: (label, json text, hdr3 flags)
        # EXPERIMENT == 2: candidates built from the JSON keys the controller uses
        # (true, response, OK, cfg_data, cfg_chg, service, devicedrive, status, connection_status,
        #  GOT_IP, result, DeviceDrive, configuration, mac, device_state, rssi, start, stop)
        sf = lambda state: {"devicedrive": {"status": {"connection_status": "GOT_IP", "device_state": state,
                                                       "mac": "00:11:22:33:44:55", "rssi": -55}}}
        self.cands = [] if not EXPERIMENT else [
            ("fw: status GOT_IP + device_state DeviceDrive", d(sf("DeviceDrive")), 0x00),
            ("fw: status GOT_IP + device_state configuration", d(sf("configuration")), 0x00),
            ("fw: response true", d({"response": "true"}), 0x00),
            ("fw: devicedrive result OK + response true", d({"devicedrive": {"result": "OK"}, "response": "true"}), 0x00),
            ("fw: devicedrive response true", d({"devicedrive": {"response": "true"}}), 0x00),
            ("fw: result OK + status GOT_IP siblings", d({"devicedrive": {"result": "OK", "status": sf("DeviceDrive")["devicedrive"]["status"]}}), 0x00),
            ("fw: service response true", d({"service": {"response": "true"}}), 0x00),
            ("fw: devicedrive result true", d({"devicedrive": {"result": "true"}}), 0x00),
        ] if EXPERIMENT == 2 else [
            ("ACK-PAYLOAD result OK", d(OK), 0x21),
            ("ACK-PAYLOAD status GOT_IP", d(STATUS), 0x21),
            ("ACK-PAYLOAD response true", d({"response": "true"}), 0x21),
            ("top-level result OK", d({"result": "OK"}), 0x00),
            ("top-level response OK", d({"response": "OK"}), 0x00),
            ("top-level OK true", d({"OK": "true"}), 0x00),
            ("devicedrive setup OK", d({"devicedrive": {"setup": {"result": "OK"}}}), 0x00),
            ("status DISCONNECTED then GOT_IP?", d({"devicedrive": {"status": {"connection_status": "CONNECTING"}}}), 0x00),
        ] if EXPERIMENT == 3 else [
            ("ok+status combined", d({"devicedrive": {"result": "OK", "status": {"connection_status": "GOT_IP"}}}), 0x00),
            ("setup result key", d({"devicedrive": {"setup": "OK"}}), 0x00),
            ("status CONNECTED", d({"devicedrive": {"status": {"connection_status": "CONNECTED"}}}), 0x00),
            ("command echo + result OK", d({"devicedrive": {"command": "setup", "result": "OK"}}), 0x00),
            ("OK without EOT", "<noeot>" + d(OK), 0x00),
            ("status GOT_IP without EOT", "<noeot>" + d(STATUS), 0x00),
            ("result ERROR", d({"devicedrive": {"result": "ERROR"}}), 0x00),
            ("introspect-style", d({"devicedrive": {"introspect": {"result": "OK"}}}), 0x00),
            ("plain OK string", "OK", 0x00),
            ("OK without 0x04 (no payload at all)", "<none>", 0x00),
            ("OK as reg-3 write-flag echo (flags 0x20)", d(OK), 0x20),
            ("result OK with whitespace", '{"devicedrive": {"result": "OK"}}', 0x00),
        ]

    INJECT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inject.json")

    allow_inject_file = False          # --allow-inject-file (bench testing only)

    def take_inject_file(self):
        """One-shot command from inject.json (merged into the next {"response":"OK"} ack).
        Bench tool only: ignored unless --allow-inject-file, and a file older than 120 s is discarded,
        so a leftover file can never fire in 24/7 use."""
        import os
        if not self.allow_inject_file or not os.path.exists(self.INJECT_FILE):
            return None
        try:
            if time.time() - os.path.getmtime(self.INJECT_FILE) > 120:
                os.replace(self.INJECT_FILE, self.INJECT_FILE + f".stale_{int(time.time())}")
                print("# inject.json older than 120 s - discarded", flush=True)
                return None
        except OSError:
            return None
        try:
            obj = json.loads(open(self.INJECT_FILE, encoding="utf-8-sig").read())
        except Exception:
            obj = None
        try:
            os.replace(self.INJECT_FILE, self.INJECT_FILE + f".sent_{int(time.time())}")
        except Exception:
            pass
        return obj if isinstance(obj, dict) else None

    def ingest_json(self, txt, obj):
        """Read-only: capture status_info / cfg_data_1 the controller sends, for the SCADA feed."""
        nd = _find_numeric_dict(obj)
        if not nd:
            return
        is_cfg = ("cfg_data" in txt) or ("configuration" in txt) or (len(nd) > 25)
        now = time.time()
        if is_cfg:
            fields = {}
            for k, v in nd.items():
                fields[CFG_NAMES.get(k, "cfg_" + k)] = v
            with self.lock:
                self.latest_cfg = {"epoch": now, "fields": fields, "raw": dict(nd)}
            if self.settings is not None and "033" in nd:      # boiler type changed -> a fresh "usual" for lighting
                try:
                    self.settings.note_boiler_type(nd["033"])
                except Exception as e:
                    print(f"# boiler type check error: {e!r}", flush=True)
            if self.cfg_hist:
                try:
                    self.cfg_hist.on_cfg(dict(nd), now)
                except Exception as e:
                    print(f"# settings history error: {e!r}", flush=True)
            if self.cfg_sync:
                try:
                    self.cfg_sync.on_cfg(dict(nd))
                except Exception as e:
                    print(f"# cfg-sync error: {e!r}", flush=True)
            return
        # status_info
        fields = {}
        for k, v in nd.items():
            fields[STATUS_NAMES.get(k, "f_" + k)] = v
        snap = {"epoch": now, "t": datetime.datetime.now().isoformat(timespec="seconds"),
                "fields": fields, "raw": dict(nd)}
        # every status field is kept (named), so later analysis (LUX, fan, optimiser) has the full picture
        row = {"epoch": now}
        row.update(fields)
        # flame / LUX: field 003 or 004, chosen on the web once a bench test has identified it
        ff = self.settings.get("flame_field") if self.settings else None
        row["flame"] = nd.get(ff) if ff in ("003", "004") else None
        if self.latest_cfg:
            row["setpoint"] = self.latest_cfg["fields"].get("setpoint_c")
            row["fuel_gph"] = self.latest_cfg["fields"].get("fuel_gph")
        # Fast data (--live-interval 1..9): every row goes to the live view, the ash-plate watcher and the ignition
        # log (they work on time differences); history, files, MQTT and the slower analyses get a thinned stream
        # (one row per ~10 s, plus every change of state / alarm / ash plate / igniter), so they behave as at 10 s.
        keep = self.keep_row(row)
        with self.lock:
            self.latest = snap
            if keep:
                self.hist.append(row)
            hist = list(self.hist)
        every = (lambda: self.store and self.thin and self.store.add_raw(row),   # full-rate copy for analysis
                 lambda: self.ignitions and self.ignitions.on_row(row),
                 lambda: self.ash and self.ash.on_row(row))
        thinned = (lambda: self.store and self.store.add_row(row),
                   lambda: self.tracker and self.tracker.on_row(row, hist),
                   lambda: self.lux and self.lux.on_row(row, hist),
                   lambda: self.fan and self.fan.on_row(row, hist),
                   lambda: self.on_status and self.on_status())
        for fn in every + (thinned if keep else ()):
            try:
                fn()
            except Exception as e:          # an extension must never stop the bus handling
                print(f"# extension error: {e!r}", flush=True)

    CFG_READ_EVERY_S = 1800                      # without --cfg-sync: ask for the settings (read only) this often
    cfg_read_last = 0.0

    def cfg_read_due(self):
        now = time.time()
        have = (self.latest_cfg or {}).get("epoch") or 0
        if now - have < self.CFG_READ_EVERY_S or now - self.cfg_read_last < 90:
            return False
        self.cfg_read_last = now
        return True

    KEEP_EVERY_S = 9.5                          # thinned stream: at most one ordinary row per ~10 s
    KEEP_ON_CHANGE = ("state", "alarm", "din2", "igniter_on")

    thin = False                                 # True only with fast data (--live-interval below 10 s)

    def keep_row(self, row):
        if not self.thin:
            return True
        last = getattr(self, "_kept", None)
        if (last is None or row["epoch"] - last["epoch"] >= self.KEEP_EVERY_S
                or any(row.get(k) != last.get(k) for k in self.KEEP_ON_CHANGE)):
            self._kept = row
            return True
        return False

    def status_overdue(self):
        """Status watchdog: the controller forgets the 'send status_info every N s' request when it reboots
        (and sometimes stops early). If no status came for 3 intervals, ask again on the next keep-alive,
        at most every 30 s."""
        if not self.svc:
            return False
        iv = max(5, int(self.svc.get("interval", 10)))
        last = (self.latest or {}).get("epoch") or 0
        now = time.time()
        return now - last > 3 * iv and now - self.svc_last > 30

    def request_run_mode(self, cmd, source, from_hub=False, reset_alarm=False):
        """The one gate for a start / stop asked for by a person: the web page and Home Assistant (MQTT).
        Returns (ok, message, http code). Needs --allow-remote-start. Start or stop while the boiler is in ALARM
        resets the alarm: from a hub (MQTT, phone) always refused (also when there is no fresh status); from the
        web page only with reset_alarm=True, which the page sends after the owner confirmed the reset."""
        cmd = str(cmd or "").strip().lower()
        if cmd not in ("start", "stop"):
            return False, "cmd must be start or stop", 400
        if not (self.remote_armed and self.run_mode_cb):
            return False, "remote start/stop is off (--allow-remote-start)", 403
        f = (self.latest or {}).get("fields") or {}
        in_alarm = f.get("state") == 6 or bool(f.get("alarm") or 0)
        if from_hub:
            if time.time() - ((self.latest or {}).get("epoch") or 0) > 60:
                return False, "no fresh status from the boiler - not sent", 409
            if in_alarm:
                return False, "boiler is in ALARM - reset it on the boiler or the web page (never from MQTT)", 409
        elif in_alarm:
            if reset_alarm is not True:
                return False, "boiler is in ALARM - start/stop would reset the alarm; confirm the reset first", 409
            source = f"{source} (alarm reset)"
            print(f"# alarm reset by web (owner confirmed): {cmd}, alarm {f.get('alarm')}", flush=True)
            self.note_event({"type": "alarm_reset_web", "epoch": time.time(), "cmd": cmd,
                             "alarm": f.get("alarm"), "msg": "alarm reset by web (owner confirmed)"})
        self.run_mode_cb(cmd, source)
        return True, f"{cmd} queued for the next keep-alive (~30 s)", 200

    CMD_MAX_AGE = 90.0          # a queued command not delivered within this time is dropped

    def queue_command(self, cmd, source):
        """Thread-safe: queue service.run_mode for the next keep-alive (latest command wins)."""
        with self.cmd_lock:
            self.svc_once = {"run_mode": cmd}
            self.svc_once_meta = {"t": time.time(), "source": source}
            self.last_cmd = {"cmd": cmd, "source": source, "queued": time.time(), "delivered": None,
                             "state_at_queue": (self.latest or {}).get("fields", {}).get("state")}

    def take_command(self):
        """Called when building a keep-alive reply. Returns the service object to send, or None.
        Re-checks at delivery: drops commands older than CMD_MAX_AGE (e.g. queued while the controller was
        off) and drops a scheduler START unless the latest status (<= 30 s old) shows STOPPET without alarm."""
        with self.cmd_lock:
            svc, meta = self.svc_once, self.svc_once_meta or {}
            self.svc_once, self.svc_once_meta = None, None
            last = self.last_cmd
        if svc is None:
            return None
        now = time.time()
        why = None
        if now - meta.get("t", now) > self.CMD_MAX_AGE:
            why = f"too old ({now - meta['t']:.0f} s)"
        elif svc.get("run_mode") == "start" and str(meta.get("source", "")).startswith(("schedule", "auto_reset")):
            lat = self.latest or {}
            f = lat.get("fields") or {}
            src = str(meta.get("source", ""))
            if now - (lat.get("epoch") or 0) > 30:
                why = "no fresh status from the boiler"
            elif src == "auto_reset (clear)":
                if f.get("state") != 6:
                    why = f"alarm already gone (state {f.get('state')})"
            elif f.get("state") != 0 or (f.get("alarm") or 0):
                why = f"boiler not idle any more (state {f.get('state')}, alarm {f.get('alarm')})"
        if why:
            self.note_event({"type": "command_dropped", "epoch": now, "cmd": svc.get("run_mode"),
                             "source": meta.get("source"), "why": why})
            print(f"# command {svc} from {meta.get('source')} dropped at delivery: {why}", flush=True)
            return None
        if last and not last.get("delivered"):
            last["delivered"] = now
        self.note_event({"type": "command_sent", "epoch": now, "cmd": svc.get("run_mode"),
                         "source": meta.get("source")})
        return svc

    def note_event(self, ev):
        """Log a non-state event (commands etc.) to disk + MQTT."""
        with self.lock:
            if ev.get("type") == "warning":
                self.warnings.append(ev)
            if ev.get("type") in ("warning", "auto_reset", "command_dropped", "transition", "alarm", "lux_reset",
                                  "fan_reset", "cfg_sync", "warm", "alarm_reset_web"):
                self.recent_events.append(ev)
        try:
            if self.store:
                self.store.add_event(ev)
            if self.on_event:
                self.on_event(ev)
        except Exception as e:
            print(f"# extension error: {e!r}", flush=True)

    def note_rx(self):
        """A frame from the controller: for the SCADA "connected" flag, and data/last_rx for the updater. The page and
        the serial link are independent: the file shows the monitor still talks to the boiler when its page is silent.
        Written at most every 30 s, in one move; a write error never stops the serial loop."""
        self.last_rx = time.time()
        m = self.last_rx_mono = time.monotonic()
        if self.rx_mark and (self._rx_mark_at is None or m - self._rx_mark_at >= 30):
            self._rx_mark_at = m
            try:
                tmp = self.rx_mark + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(f"{self.last_rx:.0f}\n")
                os.replace(tmp, self.rx_mark)
            except OSError:
                try:                                    # a full card: the file's time needs no free space
                    os.utime(self.rx_mark)
                except OSError:
                    pass

    def feed_snapshot(self, since=None, tv=None):
        """Thread-safe snapshot for the HTTP server. since (epoch): only the history rows after it (the pages keep
        the rows they already have, so each poll is small); None = all rows (about 2 h, ~250 kB).
        With since the answer also has "tv" (versions of SLOW_PARTS); tv = the versions the page has: the parts
        still at that version are left out (the page keeps its copy). Without since: everything, as before."""
        import burner_ext
        with self.lock:
            hist = list(self.hist) if since is None else [r for r in self.hist if (r.get("epoch") or 0) > since]
            out = {
                "connected": bool(self.last_rx) and (time.time() - self.last_rx < 60),
                "last_rx": self.last_rx,
                "started": self.started,
                "uptime": round(time.monotonic() - self.started_mono, 1),
                "rx_age": round(time.monotonic() - self.last_rx_mono, 1) if self.last_rx_mono else None,
                "now": time.time(),
                "version": burner_ext.MONITOR_VERSION,
                "latest": self.latest,
                "cfg": self.latest_cfg,
                "history": hist,
                "labels": burner_common.labels(),
                "remote": {"armed": self.remote_armed, "last_cmd": self.last_cmd,
                           "pending": self.svc_once},
                "warnings": list(self.warnings),
                "events": list(self.recent_events)[-100:],
            }
        if self.scheduler:
            out["schedule"] = self.scheduler.feed()
        if self.auto_reset:
            out["auto_reset"] = self.auto_reset.feed()
        if self.settings:
            out["flame_field"] = self.settings.get("flame_field")
        if self.vvb:
            out["vvb"] = self.vvb.feed()
        if self.ash:
            out["ash_plate"] = self.ash.feed()
        if self.warm:
            out["warm"] = self.warm.feed()
        if self.weather:
            out["weather"] = self.weather.feed()
        if self.minmode:
            try:
                out["minmode"] = self.minmode.feed()
            except Exception:
                pass
        if self.tracker:
            out.update(self.tracker.feed())
        if self.cfg_sync:
            out["cfg_sync"] = self.cfg_sync.feed()
        if self.cfg_hist:
            out["cfg_changes"] = self.cfg_hist.feed()
        try:                                 # weak Wi-Fi / no internet for 15+ min: only then (a notice on /home)
            net = burner_ext.net_trouble(self.store.dir if self.store else None)
            if net and max(net.values()) >= 15:
                out["net"] = net
        except Exception:
            pass
        disk = burner_common.disk_trouble()  # writes failing now (SD card full?): only then (a notice on /home)
        if disk:
            out["disk"] = disk
        if since is not None:
            out["history_since"] = since
            # the slow-changing lists (~30 kB) only when they changed: "tv" names the version of each part; a page
            # that sends back the same part's version (&tv=) gets that part left out and keeps its own copy
            # HOT_KEYS have their own versions after the parts' ones; a left-out part sends its changed hot keys in "hot"
            had, vs, hot = (tv or "").split("-"), [], {}
            ver = lambda v: format(zlib.crc32(json.dumps(_steady(v), separators=(",", ":"), default=str).encode()), "x")
            for i, k in enumerate(SLOW_PARTS):
                part, hk = out.get(k), HOT_KEYS.get(k, ())
                vs.append(ver({x: y for x, y in part.items() if x not in hk} if hk and isinstance(part, dict) else part))
                if i < len(had) and had[i] == vs[-1] and k in out:
                    del out[k]
                    if hk and isinstance(part, dict):
                        hot[k] = {x: part[x] for x in hk if x in part}
            for i, k in enumerate(HOT_KEYS, len(SLOW_PARTS)):
                part = out.get(k) if k in out else hot.get(k)
                vs.append(ver({x: part[x] for x in HOT_KEYS[k] if x in part} if isinstance(part, dict) else None))
                if k in hot and (i >= len(had) or had[i] != vs[-1]) and hot[k]:
                    out.setdefault("hot", {})[k] = hot[k]
            out["tv"] = "-".join(vs)
            if isinstance(out.get("latest"), dict):     # the pages read latest.fields; raw is the same numbers again
                out["latest"] = {x: y for x, y in out["latest"].items() if x != "raw"}
        return out

    def ack(self, f: Frame, payload=b""):
        # echo header, error bit cleared, normally no payload
        return build_frame(self.addr, f.reg, f.flags & ~0x10, f.typ, payload)

    def reply(self, f: Frame):
        """Returns (frame_bytes or None, explanation)."""
        if f.hdr1 is None or not f.crc_ok:
            return None, "ignored (malformed / bad CRC)"
        # NOTE: a request with the error bit (0x10) set is the controller's own
        # "module was in error" state (seen after we answered a read with 0x10).
        # We still answer it normally, with the bit cleared, to let it recover.
        r = f.reg
        if not f.write:
            if r == 0:
                v = self.reg0
                if self.vvb:                     # opt-in tank sensor; stale -> safe "warm" value
                    try:
                        v = float(self.vvb.reg0(self.reg0))
                    except Exception:
                        v = self.reg0
                return build_frame(self.addr, 0, 0x00, 3, struct.pack(">f", v)), f"reg0 read -> float {v} (BE)"
            if r == 1:
                # SAFETY: force bit0=1 (no remote start) unless remote start is explicitly armed.
                val = self.reg1 if self.allow_remote_start else (self.reg1 | 1)
                nrs = "remote-start ALLOWED" if (val & 1) == 0 else "no remote start"
                return build_frame(self.addr, 1, 0x00, 1, struct.pack(">I", val)), f"reg1 read -> u32 0x{val:x} (BE)  [bit0: {nrs}]"
            if r == 3:
                txt = self.pending if self.pending is not None else {"ok": json.dumps(OK, separators=(",", ":")), "status": json.dumps(STATUS, separators=(",", ":")), "empty": ""}[self.idle]
                src = "pending answer" if self.pending is not None else "no request pending, default OK"
                flags = 0x00
                if self.inject_text and self.pending is None and self.sticky is None and time.time() >= self.inject_at:
                    # one-shot module-initiated message, delivered as the answer to an idle poll
                    txt, src = self.inject_text, "INJECTED one-shot message"
                    self.inject_text = None
                elif self.delay_seq and self.in_setup:
                    self.poll_n += 1
                    target = self.delay_seq[(self.setup_n - 1) % len(self.delay_seq)]
                    if self.poll_n == target:
                        txt, src = json.dumps(OK, separators=(",", ":")), f"DELAY-EXPERIMENT answer at poll {self.poll_n}"
                    else:
                        txt, src = "", f"DELAY-EXPERIMENT empty (poll {self.poll_n}, answer at {target})"
                    self.pending = None
                elif self.sticky is not None:
                    txt, flags = self.sticky
                    src = "sticky experiment answer"
                self.pending = None
                if txt == "<none>":
                    body = b""
                elif txt.startswith("<noeot>"):
                    body = txt[7:].encode()
                else:
                    body = txt.encode() + EOT
                return build_frame(self.addr, 3, flags, 4, body), f"reg3 read -> JSON ({src}, flags=0x{flags:02x}): {txt}"
            return None, f"read of reg{r}: no rule, not answering"
        # writes
        if r == 3:
            self.json_in += f.payload
            note = "reg3 write -> ack"
            if len(self.json_in) > 8192:                 # never grow without limit (lost frames)
                self.json_in.clear()
                return self.ack(f), note + "; oversize JSON buffer dropped"
            if self.json_in.endswith(EOT) or f.payload[-1:] == b"}" or True:
                txt = bytes(self.json_in).rstrip(EOT).decode("utf-8", "replace")
                try:
                    obj = json.loads(txt)
                    dd_ = obj.get("devicedrive") if isinstance(obj, dict) else None
                    cmd = dd_.get("command") if isinstance(dd_, dict) else None
                    self.seen_json.append(txt)
                    self.ingest_json(txt, obj)          # read-only capture for the SCADA feed
                    self.json_in.clear()
                    self.pending = json.dumps(STATUS if cmd == "status" else OK, separators=(",", ":"))
                    self.sticky = None
                    self.in_setup = False
                    if self.fw_mode:
                        # Replies as the controller expects them: one read must carry k messages.
                        dd = isinstance(obj, dict) and isinstance(obj.get("devicedrive"), dict)
                        okm = json.dumps(OK, separators=(",", ":"))
                        gotip = json.dumps(STATUS, separators=(",", ":"))
                        if dd and cmd == "status":
                            self.pending = gotip
                            note += "; FW status -> GOT_IP (code 3)"
                        elif dd and cmd == "setup" and "silent_connect" in obj["devicedrive"]:
                            first = okm
                            svc_now = self.take_command()       # checked again at delivery time
                            if svc_now is not None:
                                # one-shot command (e.g. run_mode start/stop) rides the keep-alive piggyback
                                first = json.dumps({"response": "OK", "service": svc_now}, separators=(",", ":"))
                                note += f"; SERVICE-ONCE {first}"
                            elif self.svc and (time.time() - self.svc_last >= self.svc_every or self.status_overdue()):
                                # piggy-back a service request on msg 0; controller still only checks msg 1 == GOT_IP
                                first = json.dumps({"response": "OK", "service": self.svc}, separators=(",", ":"))
                                self.svc_last = time.time()
                                note += f"; SERVICE {first}"
                            self.pending = first + "\x04" + gotip        # k=2: second msg must be code 3
                            note += "; FW keep-alive -> 2 msgs in ONE read (k=2)"
                        elif dd and cmd == "setup" and obj["devicedrive"].get("visibility") == "0":
                            self.pending = okm + "\x04" + okm            # k=2 in state 7: code 1
                            note += "; FW visibility 0 -> OK + OK in ONE read (k=2)"
                        elif dd:
                            self.pending = okm                           # setup/introspect: code 1
                            note += f"; FW {cmd} -> result OK (code 1)"
                        else:
                            resp = {"response": "OK"}
                            extra = self.take_inject_file()
                            if extra:
                                resp.update(extra)
                                note += f"; INJECT-FILE {json.dumps(extra, separators=(',', ':'))}"
                            elif self.cfg_sync:
                                extra = self.cfg_sync.next_extra()       # settings sync: read-back / re-push
                                if extra:
                                    resp.update(extra)
                                    note += f"; CFG-SYNC {json.dumps(extra, separators=(',', ':'))}"
                            elif self.cfg_read_due():
                                # no settings sync: still READ the settings (target temperature etc.), never write
                                resp["cfg_data"] = True
                                note += "; CFG-READ {\"cfg_data\":true}"
                            self.pending = json.dumps(resp, separators=(",", ":"))
                            note += "; FW data/ConnectCode -> response OK (code 2)"
                    if cmd == "status" and self.status_cands:
                        self.status_n += 1
                        slabel, stext = self.status_cands[(self.status_n - 1) % len(self.status_cands)]
                        self.pending = stext
                        note += f"; STATUS-EXPERIMENT #{self.status_n} cand[{(self.status_n - 1) % len(self.status_cands)}] {slabel}"
                    if isinstance(obj, dict) and "ConnectCode" in obj and self.cc_cands:
                        now = time.time()
                        if now - self.cc_last > 3.0:          # a new TILSLUT ENHED attempt = new candidate
                            self.cc_n += 1
                        self.cc_last = now
                        label, ctext = self.cc_cands[(self.cc_n - 1) % len(self.cc_cands)]
                        self.pending = ctext
                        self.sticky = (ctext, 0x00)
                        note += f"; CC-EXPERIMENT #{self.cc_n} cand[{(self.cc_n - 1) % len(self.cc_cands)}] {label}"
                    if cmd == "setup" and self.delay_seq:
                        self.in_setup, self.poll_n = True, 0
                        self.setup_n += 1
                        note += f"; DELAY-EXPERIMENT #{self.setup_n} answer at poll {self.delay_seq[(self.setup_n - 1) % len(self.delay_seq)]}"
                    if cmd == "setup" and self.reg1_seq:
                        self.reg1 = self.reg1_seq[self.setup_n % len(self.reg1_seq)] | 1
                        self.setup_n += 1
                        note += f"; REG1-EXPERIMENT #{self.setup_n} reg1=0x{self.reg1:x}"
                    if cmd == "setup" and self.cands and "silent_connect" in txt:   # rotate only on the blocking setup
                        self.setup_n += 1
                        label, ctext, cflags = self.cands[(self.setup_n - 1) % len(self.cands)]
                        self.pending = ctext
                        self.sticky = (ctext, cflags)
                        note += f"; EXPERIMENT #{self.setup_n} cand[{(self.setup_n - 1) % len(self.cands)}] {label}"
                    note += f"; JSON cmd={cmd!r}, queued answer {self.pending}"
                except ValueError:
                    if f.payload.endswith(EOT):
                        # a complete message (EOT) that doesn't parse = a frame was lost: start clean
                        note += f"; JSON message unparsable ({len(self.json_in)} B) - dropped"
                        self.json_in.clear()
                    else:
                        note += f"; JSON incomplete so far ({len(self.json_in)} B buffered)"
            if self.sticky is not None and self.sticky[1] == 0x21:   # experiment: JSON inside the write ack
                txt = self.sticky[0]
                self.sticky = None; self.pending = None
                return self.ack(f, txt.encode() + EOT), note + f"; ACK-WITH-PAYLOAD {txt}"
            return self.ack(f), note
        if r in (2, 4):
            return self.ack(f), f"reg{r} write -> ack"
        return self.ack(f), f"reg{r} write -> ack (no specific rule)"


def host_allowed(host_header, allowed=()):
    """May a page reached under this Host name use the web page and its buttons?
    Always: 127.0.0.1 / localhost / ::1. Plus the names in `allowed`; the word "lan" there allows any
    private IPv4 address typed as a number (10.x, 172.16-31.x, 192.168.x), e.g. a Raspberry Pi on the home
    network. Names that are not in the list are refused, which keeps DNS-rebinding pages out."""
    import ipaddress
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    names = {h.strip().lower() for h in allowed if h and h.strip()}
    if host in names:
        return True
    if "lan" in names:
        try:
            ip = ipaddress.ip_address(host)
            return ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local
        except ValueError:
            return False
    return False


def about_info(mod, http_port=None):
    """What the Settings page shows read-only about this monitor: the values from the .env file / the command line.
    No secrets: the MQTT password and user name are only "set" or not, and the serial adapter is only "auto" or
    "fixed" (its name can hold a serial number)."""
    import burner_ext as X
    info = {"version": X.MONITOR_VERSION, "http_port": http_port}
    info.update(getattr(mod, "about", None) or {})
    mq = info.get("mqtt")
    if isinstance(mq, dict) and mq.get("on"):
        c = getattr(getattr(mod, "mqtt_bridge", None), "c", None)
        info["mqtt"] = dict(mq, connected=bool(getattr(c, "connected", False)))
    return info


def start_http(mod, port, log, host="127.0.0.1", allowed_hosts=()):
    """Start a read-only HTTP server for the SCADA dashboard. GET only; never touches the bus."""
    import http.server, gzip, hashlib
    here = os.path.dirname(os.path.abspath(__file__))
    dash = os.path.join(here, "scada", "dashboard.html")
    gz_cache = {}                                   # ETag -> gzipped page

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # keep the responder log clean

        def _gzip_ok(self):
            return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

        def _send(self, code, body, ctype, page=False):
            try:
                self._send_now(code, body, ctype, page)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                # the page was closed or reloaded before the answer was sent: one short line, no traceback
                self.close_connection = True
                print(f"# http: page left before the answer ({self.command} {self.path.split('?', 1)[0][:60]})", flush=True)

        def _send_now(self, code, body, ctype, page=False):
            """page=True: a file that rarely changes (the pages, the guide): ETag, so a reload with an unchanged file
            gets a 304 with no body. GET JSON answers too. Everything else: no-store. Text bodies are gzipped when the browser accepts it
            (JSON shrinks ~10x - keeps the Wi-Fi quiet when a page stays open for hours)."""
            if isinstance(body, str):
                body = body.encode("utf-8")
            etag = None
            # a GET JSON answer gets an ETag too: the pages re-ask each minute (cache "no-cache"), unchanged = 304
            if page or (code == 200 and ctype == "application/json" and self.command in ("GET", "HEAD")):
                etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
                if etag in (self.headers.get("If-None-Match") or ""):
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    return
            gz = len(body) > 512 and self._gzip_ok()
            if gz:
                zb = gz_cache.get(etag) if page else None     # the pages are big; compress each version once
                if zb is None:
                    zb = gzip.compress(body, 6)
                    if page:
                        if len(gz_cache) > 8:
                            gz_cache.clear()
                        gz_cache[etag] = zb
                body = zb
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            if etag:
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")    # may keep it, but must ask (cheap 304) each load
            else:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()                   # no CORS header: other websites can't read the feed
            if self.command != "HEAD":
                self.wfile.write(body)

        def _local_host(self):
            return host_allowed(self.headers.get("Host"), allowed_hosts)

        @staticmethod
        def _code(ok, msg):              # a settings answer: 507 when settings.json could not be written, else 400
            import burner_features as F
            return 200 if ok else 507 if msg == F.Settings.SAVE_FAILED else 400

        @staticmethod
        def _disk(msg):                  # "disk": true tells the page to say the storage may be full or read-only
            import burner_features as F
            return {"disk": True} if msg == F.Settings.SAVE_FAILED else {}

        def _json(self, code, obj):
            # no CORS header (in _send): other sites can't read or write the API
            self._send(code, json.dumps(obj, separators=(",", ":")), "application/json")

        def do_POST(self):
            # a bug hit by odd input must still give an answer, not a closed connection
            try:
                return self._post()
            except Exception as e:
                print(f"# POST {self.path.split('?', 1)[0][:60]} error: {e!r}", flush=True)
                try:
                    return self._json(500, {"ok": False, "msg": "server error"})
                except OSError:
                    pass

        def _post(self):
            # Only the dashboard itself may change settings: custom header + JSON (forces a CORS pre-flight,
            # which we never answer) and a local Host header (blocks DNS rebinding).
            # a certificate file for the phone alerts may be up to 3 MB (as JSON text); everything else is small
            lim = 4 * 1024 * 1024 if self.path.split("?", 1)[0] == "/api/ntfy/cert/upload" else 65536
            try:                                     # read the body first, also when refusing: answering
                n = min(int(self.headers.get("Content-Length") or 0), lim)     # with unread data makes Windows
                raw = self.rfile.read(n) if n > 0 else b""                     # reset the connection
            except (ValueError, OSError):
                return self._json(400, {"ok": False, "msg": "bad request"})
            if not self._local_host() or self.headers.get("X-Burner") != "1" \
                    or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json(403, {"ok": False, "msg": "forbidden"})
            try:
                body = burner_common.strict_json_loads(raw or b"{}")    # no Infinity / NaN / too deep
            except ValueError:
                return self._json(400, {"ok": False, "msg": "bad JSON"})
            path = self.path.split("?", 1)[0]
            if REPLAY is not None:
                if path == "/api/replay":                # replay clock: pause / play / speed / jump forward
                    code, obj = REPLAY.control(body if isinstance(body, dict) else {})
                    return self._json(code, obj)
                if path == "/api/run_mode":              # start / stop are inert in a replay
                    return self._json(403, {"ok": False, "msg": "replay - nothing is sent to a boiler"})
            if path == "/api/settings" and mod.settings:
                # "Just emptied" with "how full was it": learn the real ash share before the new time is saved
                ash_b = body.get("ash") if isinstance(body, dict) else None
                if isinstance(ash_b, dict) and ash_b.get("emptied") is True and "fill" in ash_b and mod.optimiser:
                    try:
                        mod.optimiser.learn_ash(ash_b.get("fill"))
                    except Exception as e:
                        print(f"# ash learning error: {e!r}", flush=True)
                    ash_b.pop("fill", None)
                ok, msg = mod.settings.update(body)
                mod.note_event({"type": "settings", "epoch": time.time(), "ok": ok, "msg": msg,
                                "keys": sorted(body.keys()) if isinstance(body, dict) else []})
                return self._json(self._code(ok, msg), dict({"ok": ok, "msg": msg, "settings": mod.settings.public()},
                                                            **self._disk(msg)))
            if path in ("/api/update/check", "/api/update/retry", "/api/update/back"):
                # "Check for update" / "Try again" / "Go back": leave a one-word request file; the Pi's updater
                # (systemd path unit) does the rest and only swaps versions while the burner is off or waiting.
                # The word comes from the route, never from the body: no address, version or command can be sent.
                # Nothing is downloaded by this program.
                import burner_ext
                if REPLAY is not None:
                    return self._json(403, {"ok": False, "msg": "replay - no updates here"})
                d = getattr(mod.store, "dir", None)
                if not d:
                    return self._json(400, {"ok": False, "msg": "no data folder"})
                if (burner_ext.update_info(d).get("view") or {}).get("busy"):       # one thing at a time
                    return self._json(409, {"ok": False, "msg": "the updater is already at work"})
                ok, msg = burner_ext.update_request(d, path.rsplit("/", 1)[1])
                return self._json(200 if ok else 500, {"ok": ok, "msg": msg})
            nt = getattr(mod, "notifier", None)
            if path.startswith("/api/ntfy/") and nt:
                # phone alerts: test message, certificate update / upload (settings go through /api/settings)
                import burner_notify as N
                if path == "/api/ntfy/test":
                    r = nt.send_test()
                    return self._json(200, {"ok": bool(r.get("ok")), "msg": r.get("msg"), "kind": r.get("kind"),
                                            "ntfy": nt.status()})
                if path in ("/api/ntfy/cert/update", "/api/ntfy/cert/upload"):
                    try:
                        if path.endswith("update"):
                            n = nt.ca.update()
                        else:
                            pem = (body or {}).get("pem") if isinstance(body, dict) else None
                            if not isinstance(pem, str):
                                return self._json(400, {"ok": False, "msg": "no file"})
                            n = nt.ca.install(pem, "upload")
                        mod.note_event({"type": "ntfy_cert", "epoch": time.time(), "ok": True, "n": n})
                        return self._json(200, {"ok": True, "msg": f"{n} certificates", "ntfy": nt.status()})
                    except (N.CertError, OSError) as e:
                        return self._json(400, {"ok": False, "msg": str(e), "ntfy": nt.status()})
                return self._json(404, {"ok": False, "msg": "not found"})
            if path == "/api/lux/reset" and mod.lux:
                mod.lux.reset()
                return self._json(200, {"ok": True, "msg": "LUX baseline cleared - learning again"})
            if path == "/api/optimiser/reset" and mod.optimiser:
                mod.optimiser.reset()
                return self._json(200, {"ok": True, "msg": "optimiser history cleared"})
            if path == "/api/health/cleaned" and mod.settings:
                # the owner cleaned the burner: burner health learns this burner's normal again from now
                mod.settings.set_internal("health", "cleaned_at", round(time.time()))
                mod.note_event({"type": "cleaned", "epoch": time.time()})
                return self._json(200, {"ok": True, "msg": "burner cleaned - learning its normal again"})
            if path == "/api/minmode/off" and mod.minmode:
                # the owner says the boiler's own minimum mode is now off (page note only; nothing goes to the boiler)
                mod.minmode.turned_off()
                mod.note_event({"type": "minmode_off", "epoch": time.time()})
                return self._json(200, {"ok": True, "msg": "noted - the warning comes back if the boiler burns out by itself again"})
            if path == "/api/ignitions/reset" and mod.ignitions:
                mod.ignitions.reset()
                return self._json(200, {"ok": True, "msg": "ignition history starts now"})
            if path == "/api/pellets" and mod.settings:
                # new pellet batch (name) and/or a delivery (kg added to the pellets at home)
                body = body if isinstance(body, dict) else {}
                name, kg = body.get("name"), body.get("kg")
                if not name and kg in (None, ""):
                    return self._json(400, {"ok": False, "msg": "give a name and/or kg"})
                msgs = []
                if kg not in (None, ""):
                    import burner_features as F
                    add = F._f(kg)
                    if add is None or not 0 < add <= F.STOCK_MAX:
                        return self._json(400, {"ok": False, "msg": f"delivery must be 1..{F.STOCK_MAX} kg"})
                    cur = mod.optimiser.pellet_stock() if mod.optimiser else None
                    ok, msg = mod.settings.set_stock((cur["left"] if cur else 0) + add)
                    if not ok:
                        return self._json(self._code(ok, msg), dict({"ok": False, "msg": msg}, **self._disk(msg)))
                    msgs.append(f"delivery +{add:.0f} kg -> {msg}")
                if name:
                    ok, msg = mod.settings.add_batch(name)
                    if not ok:
                        return self._json(self._code(ok, msg), dict({"ok": False, "msg": msg}, **self._disk(msg)))
                    msgs.append(msg)
                msg = "; ".join(msgs)
                mod.note_event({"type": "pellets", "epoch": time.time(), "msg": msg})
                return self._json(200, {"ok": True, "msg": msg,
                                        "stock": mod.optimiser.pellet_stock() if mod.optimiser else None})
            if path == "/api/pellets/stock" and mod.settings:
                body = body if isinstance(body, dict) else {}
                f_ = None
                if mod.optimiser and body.get("kg") is not None:
                    try:                               # learn how far off the estimate was (before resetting it)
                        f_ = mod.optimiser.learn_count(body.get("kg"))
                    except Exception as e:
                        print(f"# pellets correction error: {e!r}", flush=True)
                ok, msg = mod.settings.set_stock(body.get("kg"), body.get("reserve_kg"))
                if ok:
                    mod.note_event({"type": "pellets", "epoch": time.time(), "msg": "counted: " + msg,
                                    "correction": f_})
                return self._json(self._code(ok, msg), dict({"ok": ok, "msg": msg,
                                  "stock": mod.optimiser.pellet_stock() if mod.optimiser else None}, **self._disk(msg)))
            if path == "/api/run_mode":
                # start / stop (= reset alarm) from the web page; same path and rules as Home Assistant's buttons:
                # only when remote start is armed (--allow-remote-start), delivered on the next keep-alive
                cmd = str((body or {}).get("cmd", "") if isinstance(body, dict) else "").lower()
                reset = isinstance(body, dict) and body.get("reset_alarm") is True   # only after the page's confirm
                ok, msg, code = mod.request_run_mode(cmd, "web", reset_alarm=reset)
                return self._json(code, {"ok": ok, "msg": msg})
            if path == "/api/vvb" and mod.vvb:
                ok, msg = mod.vvb.put((body or {}).get("c") if isinstance(body, dict) else None, "http")
                return self._json(200 if ok else 400, {"ok": ok, "msg": msg, "vvb": mod.vvb.feed()})
            if path == "/api/fan/reset" and mod.fan:
                mod.fan.reset()
                mod.note_event({"type": "fan_reset", "epoch": time.time()})
                return self._json(200, {"ok": True, "msg": "fan baseline cleared"})
            return self._json(404, {"ok": False, "msg": "not found"})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if not self._local_host():                   # blocks DNS-rebinding pages from reading anything
                if path.startswith("/api/") or path.endswith(".json"):
                    return self._json(403, {"ok": False, "msg": "forbidden (use http://127.0.0.1:PORT/)"})
                return self._send(403, "This page may not be opened under this name.\n\n"     # a person typed a name
                                  "Use the monitor's number address instead (for example http://192.168.1.20:8787),\n"
                                  "or add the name to BURNER_ALLOWED_HOSTS in the .env file and run: docker compose up -d\n",
                                  "text/plain; charset=utf-8")
            if path.startswith("/api/"):
                if path == "/api/replay" and REPLAY is not None:
                    return self._json(200, REPLAY.status())
                if path == "/api/settings" and mod.settings:
                    return self._json(200, {"settings": mod.settings.public(), "armed": mod.remote_armed,
                                            "schedule": mod.scheduler.feed() if mod.scheduler else None,
                                            "auto_reset": mod.auto_reset.feed() if mod.auto_reset else None,
                                            "vvb": mod.vvb.feed() if mod.vvb else None,
                                            "warm": mod.warm.feed() if mod.warm else None})
                if path == "/api/about":
                    return self._json(200, about_info(mod, port))
                if path == "/api/ntfy":                 # no notifier (replay, or it could not start): no card
                    nt = getattr(mod, "notifier", None)
                    return self._json(200, nt.status() if nt else {"available": False})
                if path == "/api/lux" and mod.lux:
                    return self._json(200, mod.lux.analyse())
                if path == "/api/optimiser" and mod.optimiser:
                    return self._json(200, mod.optimiser.report())
                if path == "/api/fan" and mod.fan:
                    return self._json(200, mod.fan.analyse())
                if path == "/api/ignitions" and mod.ignitions:
                    return self._json(200, mod.ignitions.report())
                if path == "/api/weather/find":
                    # place search for the outside temperature (the owner types a town; the PC asks Open-Meteo)
                    import urllib.parse
                    import burner_features as F
                    qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                    try:
                        return self._json(200, {"ok": True, "places": F.Weather.find((qs.get("q") or [""])[0], (qs.get("lang") or ["en"])[0])})
                    except Exception as e:
                        return self._json(200, {"ok": False, "places": [], "msg": "no answer from the weather service (internet?)"})
                if path == "/api/update":
                    # version / update state, written by the Pi's updater (pi/pelletpi-update.sh) into the data dir
                    # plus "view": the state in one word for the pages (burner_ext.update_info)
                    import burner_ext
                    return self._json(200, burner_ext.update_info(getattr(mod.store, "dir", None)))
                if path == "/api/insights":
                    # auto-generated discoveries (scada/insights_gen.py writes data/insights.json); read-only file.
                    # Fallback: scada/insights.json (where older versions wrote it).
                    if REPLAY is not None:
                        paths = [REPLAY.insights_path]
                    else:
                        paths = [os.path.join(os.path.dirname(dash), "insights.json")]
                        if mod.store is not None:
                            paths.insert(0, os.path.join(mod.store.dir, "insights.json"))
                    for ip in paths:
                        try:
                            with open(ip, encoding="utf-8") as fh:
                                return self._json(200, json.load(fh))
                        except (OSError, ValueError):
                            pass
                    return self._json(200, {"cards": [], "generated": None})
                return self._json(404, {"ok": False, "msg": "not found"})
            q = dict(p.split("=", 1) for p in (self.path.split("?", 1)[1:] or [""])[0].split("&") if "=" in p)
            try:
                since = float(q["since"]) if "since" in q else None   # only rows after this epoch (pages poll so)
            except ValueError:
                since = None
            if path in ("/status.json", "/status"):
                snap = mod.feed_snapshot() if since is None else mod.feed_snapshot(since, q.get("tv"))
                self._send(200, json.dumps(snap, separators=(",", ":")), "application/json")
            elif path == "/history.json":
                # longer history from the data dir: /history.json?h=24  (thinned to <= 2000 rows);
                # &since=EPOCH: only the rows after it (not thinned) - the page adds them to what it has
                try:
                    hours = max(0.1, min(24 * 14, float(q.get("h", 24))))
                except ValueError:
                    hours = 24
                rows = []
                if mod.store:
                    cut = time.time() - hours * 3600
                    if since is not None:
                        cut = max(cut, since + 1e-6)
                    rows = [r for r in mod.store._load("hist", 10 ** 6, days=int((time.time() - cut) // 86400) + 2)
                            if (r.get("epoch") or 0) >= cut]
                    if since is None:
                        step = max(1, len(rows) // 2000)
                        rows = rows[::step]
                out = {"hours": hours, "rows": rows}
                if since is not None:
                    out["since"] = since
                self._send(200, json.dumps(out, separators=(",", ":")), "application/json")
            elif path in ("/dashboard.html", "/classic"):
                try:
                    with open(dash, "rb") as fh:
                        page = fh.read()
                    self._send(200, REPLAY.inject_html(page) if REPLAY is not None else page, "text/html; charset=utf-8", page=True)
                except OSError:
                    self._send(404, "dashboard.html not found next to the responder "
                                    "(expected scada/dashboard.html)", "text/plain")
            elif path in ("/", "/index.html", "/home", "/home.html"):
                # the friendly page is the start page; the classic page stays at /classic
                try:
                    with open(os.path.join(os.path.dirname(dash), "home.html"), "rb") as fh:
                        page = fh.read()
                    self._send(200, REPLAY.inject_html(page) if REPLAY is not None else page, "text/html; charset=utf-8", page=True)
                except OSError:
                    self._send(404, "home.html not found next to dashboard.html", "text/plain")
            elif path in ("/guide", "/guide.html"):
                # the guide that came with this version (pictures are inside the file), so it works offline
                try:
                    with open(os.path.join(here, "docs", "guide.html"), "rb") as fh:
                        self._send(200, fh.read(), "text/html; charset=utf-8", page=True)
                except OSError:
                    self._send(404, "guide.html not found (expected docs/guide.html)", "text/plain")
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._send(404, "not found", "text/plain")

    class Server(http.server.ThreadingHTTPServer):
        request_queue_size = 64     # the page asks for several things at once; the default 5 can refuse some (seen on Windows)
        # On Windows SO_REUSEADDR lets a second program listen on a port that is already in use, and the answers
        # then come from either one at random. Without it a busy port gives a clear error. Linux keeps it
        # (there it only allows a quick restart, never two listeners).
        allow_reuse_address = os.name != "nt"
    srv = Server((host, port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    note = ""
    if host not in ("127.0.0.1", "localhost"):
        note = ("  - buttons and settings work when opened as: " + ", ".join(allowed_hosts) + " (and on this machine)"
                if allowed_hosts else "  - on the local network: VIEW ONLY, buttons and settings only work on this machine")
    log(f"# web page on http://{host}:{port}/  (status at /status.json)" + note)
    return srv


class RotatingLogger(Logger):
    """Logger that keeps the log file below max_mb (renames to .1, .2, ... and keeps `backups`)."""

    def __init__(self, path, max_mb=20.0, backups=3):
        super().__init__(path)
        self.path, self.max_b, self.backups = path, int(max_mb * 1024 * 1024), backups

    next_try = 0.0

    def __call__(self, msg):
        try:
            super().__call__(burner_common.log_stamp(msg))   # every line says when (frames already carry a time)
        except (OSError, ValueError):
            pass                                         # logging must never stop the bus loop
        if not (self.f and self.max_b) or time.time() < self.next_try:
            return
        try:
            if self.f.tell() > self.max_b:
                self.f.close()
                try:
                    for i in range(self.backups - 1, 0, -1):
                        if os.path.exists(f"{self.path}.{i}"):
                            os.replace(f"{self.path}.{i}", f"{self.path}.{i + 1}")
                    os.replace(self.path, f"{self.path}.1")
                except OSError:
                    # e.g. Windows: another program has the log open - try again in 10 min, keep appending
                    self.next_try = time.time() + 600
                self.f = open(self.path, "a", encoding="utf-8")
        except (OSError, ValueError):
            self.next_try = time.time() + 600
            try:
                self.f = open(self.path, "a", encoding="utf-8")
            except OSError:
                self.f = None


SERIAL_GLOBS = ("/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*")


def find_port(spec):
    """Turn --port into a device path. Returns (path, note).

    "auto": the one USB-serial adapter that is plugged in. Looked up again on every (re)open, so the
    adapter may be unplugged and plugged into another USB socket. With more than one adapter, path is
    None and note says which ones were found: then name the right one (best its /dev/serial/by-id name).
    Anything else (COM8, /dev/ttyUSB0, /dev/serial/by-id/...) is used as it is.
    """
    if spec.lower() != "auto":
        return spec, ""
    import glob
    for pat in SERIAL_GLOBS:          # by-id first: one stable name per adapter, no double counting
        found = sorted(glob.glob(pat))
        if len(found) == 1:
            return found[0], ""
        if len(found) > 1:
            return None, "more than one USB-serial adapter: " + ", ".join(found) + " - set BURNER_SERIAL to the right one"
    return None, "no USB-serial adapter found"


def open_serial(a, log, state={}):
    """Open the boiler port (auto-detected or named). Logs the device when it changes."""
    import serial
    path, note = find_port(a.port)
    if path is None:
        raise serial.SerialException(note)
    s = serial.Serial(path, a.baud, bytesize=8, parity="N", stopbits=1, timeout=0.02)
    if state.get("path") != path:
        real = os.path.realpath(path)
        log(f"# boiler port: {path}" + (f" ({real})" if real != path else ""))
        state["path"] = path
    return s


def env_str(name, default):
    """Setting from the environment (docker-compose / .env); empty or missing = default."""
    v = os.environ.get(name, "").strip()
    return v if v else default


def env_num(name, default, kind=float):
    v = env_str(name, "")
    if not v:
        return default
    try:
        return kind(v)
    except ValueError:
        sys.exit(f"{name}={v!r} is not a number")


def env_flag(name, default=False):
    v = env_str(name, "").lower()
    if v in ("1", "yes", "true", "on"):
        return True
    if v in ("0", "no", "false", "off"):
        return False
    return default


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=env_str("BURNER_PORT", "COM8"),
                    help='serial port, or "auto" = the one USB-serial adapter plugged in (env BURNER_PORT)')
    ap.add_argument("--baud", type=int, default=57600)
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0x01, help="hdr1 byte in answers (try 0x01, then 0x10)")
    ap.add_argument("--idle", choices=["ok","status","empty"], default="ok", help="answer to reg3 reads when nothing is queued")
    ap.add_argument("--experiment", action="store_true", help="try a different setup answer on every handshake cycle")
    ap.add_argument("--reg1-seq", default="", help="comma list of reg1 values (hex ok), one per setup cycle; bit0 forced to 1")
    ap.add_argument("--delay-seq", default="", help="comma list: poll number at which the setup answer is given, one per setup cycle")
    ap.add_argument("--experiment2", action="store_true", help="setup answers built from the controller's JSON keys")
    ap.add_argument("--reg0", type=float, default=0.0, help="float answered to reg0 reads")
    ap.add_argument("--reg1", type=lambda x: int(x, 0), default=1, help="u32 answered to reg1 reads (bit0 = 'no remote start'; keep it 1 on a real burner)")
    ap.add_argument("--allow-remote-start", action="store_true", default=env_flag("BURNER_REMOTE_START"),
                    help="(env BURNER_REMOTE_START=1) arm remote start/stop (restored OEM SMS/WiFi feature, works like the "
                         "button / auto-mode). Requires --fw-mode. OFF by default so nothing triggers by accident.")
    ap.add_argument("--remote-cmd", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "remote_cmd.txt"),
                    help="trigger file polled when --allow-remote-start is set: 'start' sends service.run_mode=start "
                         "(reg1 bit0->0), 'stop' sends run_mode=stop (bit0->1). Deleted after each read; "
                         "commands older than --remote-cmd-max-age are discarded.")
    ap.add_argument("--remote-cmd-max-age", type=float, default=120.0,
                    help="seconds: a trigger file older than this is discarded as stale (default 120)")
    ap.add_argument("--inject", default="", help='one-shot JSON given as the answer to an idle reg3 poll, e.g. \'{"cfg_chg":{"000":61}}\' (dummy board only)')
    ap.add_argument("--inject-after", type=float, default=30.0, help="seconds after start before the injection is allowed")
    ap.add_argument("--cc-experiment", action="store_true", help="rotate the answer to {\"ConnectCode\":N} on every new attempt")
    ap.add_argument("--status-experiment", action="store_true", help="rotate the answer to the status command every cycle")
    ap.add_argument("--experiment3", action="store_true", help="setup answers: JSON inside the write ack, top-level result/response")
    ap.add_argument("--live-interval", type=int, default=0, help="fw-mode: ask the controller for status_info every N s (service interval)")
    ap.add_argument("--live-periode", type=int, default=10, help="fw-mode: for M minutes (service periode); re-requested before it expires")
    ap.add_argument("--fw-mode", action="store_true", help="reply exactly as the controller expects (keep-alive = 2 msgs in one read)")
    ap.add_argument("--dry-run", action="store_true", help="never transmit, print planned answers")
    ap.add_argument("--live", action="store_true", help="actually transmit (only after explicit user approval)")
    ap.add_argument("--serve-host", default="127.0.0.1", help="address for the web page: 127.0.0.1 = this PC only (default), 0.0.0.0 = also phones on the local network (view only)")
    ap.add_argument("--allowed-hosts", default=env_str("BURNER_ALLOWED_HOSTS", ""),
                    help="extra names the web page may be opened under, comma separated (e.g. 'pelletpi.local,lan'); "
                         "'lan' = any private IPv4 address. Default: only 127.0.0.1 / localhost")
    ap.add_argument("--serve", type=int, default=env_num("BURNER_HTTP_PORT", 0, int), metavar="PORT",
                    help="serve the web page on this port, e.g. 8787 (env BURNER_HTTP_PORT); 0 = no page")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--log", default=os.path.join(here, "responder.log"))
    ap.add_argument("--log-max-mb", type=float, default=env_num("BURNER_LOG_MAX_MB", 20.0),
                    help="rotate the log at this size (3 old files kept; 0 = never) (env BURNER_LOG_MAX_MB)")
    # ---- 24/7 extensions (burner_ext.py) ----
    ap.add_argument("--data-dir", default=os.path.join(here, "data"),
                    help="history / events / pre-alarm files (daily JSONL); '' = keep nothing on disk")
    ap.add_argument("--keep-days", type=float, default=env_num("BURNER_KEEP_DAYS", 30.0),
                    help="delete data files older than this (default 30) (env BURNER_KEEP_DAYS)")
    ap.add_argument("--mqtt", default=env_str("BURNER_MQTT", ""), metavar="HOST[:PORT]",
                    help="publish to an MQTT broker, e.g. the Home Assistant Mosquitto add-on (env BURNER_MQTT)")
    ap.add_argument("--mqtt-user", default=None, help="or set env BURNER_MQTT_USER")
    ap.add_argument("--mqtt-read-only", action="store_true",
                    default=env_flag("BURNER_MQTT_READ_ONLY"),
                    help="publish only: no start/stop buttons and no plan switches in Home Assistant "
                         "(env BURNER_MQTT_READ_ONLY=1)")
    ap.add_argument("--mqtt-flue", choices=["auto", "yes", "no"], default="auto",
                    help="flue temperature entity: auto = only once a real flue sensor is seen (default)")
    ap.add_argument("--mqtt-pass", default=None, help="or set env BURNER_MQTT_PASS (keeps it out of the process list)")
    ap.add_argument("--mqtt-prefix", default=env_str("BURNER_MQTT_PREFIX", "burner"),
                    help="first part of every MQTT topic (env BURNER_MQTT_PREFIX)")
    ap.add_argument("--mqtt-discovery", default="homeassistant", help="HA discovery prefix; '' = no discovery")
    ap.add_argument("--cfg-sync", default="", metavar="FILE",
                    help="keep the board's settings equal to this JSON file: re-push differing values after a "
                         "power cycle / desync (needs --fw-mode --live). OFF unless given.")
    ap.add_argument("--cfg-sync-every", type=float, default=30, help="minutes between routine settings checks (default 30)")
    ap.add_argument("--allow-inject-file", action="store_true",
                    help="bench only: accept one-shot JSON from inject.json; "
                         "needs a time-boxed run (--duration <= 900)")
    ap.add_argument("--no-log-frames", action="store_true",
                    help="don't log every bus frame (recommended 24/7 on a Raspberry Pi: less SD-card wear)")
    return ap


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    a = build_parser().parse_args()
    live = a.live and not a.dry_run
    global EXPERIMENT
    EXPERIMENT = 3 if a.experiment3 else (2 if a.experiment2 else (1 if a.experiment else 0))
    log = RotatingLogger(a.log, a.log_max_mb)
    # Experiments stay capped at 15 min. The plain --fw-mode emulation (correct answers only,
    # no experiments, reg1 bit0 kept) may run without a limit (--duration 0) — including with
    # --allow-remote-start, which is normal 24/7 use (the OEM SMS module ran continuously).
    experimenting = any([a.experiment, a.experiment2, a.experiment3, a.status_experiment, a.cc_experiment,
                         a.reg1_seq, a.delay_seq, a.inject, a.allow_inject_file])
    if a.allow_remote_start and not a.fw_mode:
        sys.exit("refusing: --allow-remote-start needs --fw-mode (run_mode is delivered on the fw-mode keep-alive)")
    if a.cfg_sync and not (a.fw_mode and live):
        sys.exit("refusing: --cfg-sync writes settings, so it needs --fw-mode --live")
    if a.cfg_sync and not os.path.exists(a.cfg_sync):
        sys.exit(f"--cfg-sync file not found: {a.cfg_sync}")
    if live and (not a.fw_mode or experimenting or (a.reg1 & 1) == 0) and (not a.duration or a.duration > 900):
        sys.exit("refusing: --live requires --duration <= 900 (unlimited only for plain --fw-mode [+ --allow-remote-start])")

    import serial
    mode = "LIVE (TRANSMITTING)" if live else "DRY-RUN (no transmit)"
    log(f"# {mode} on {a.port} @ {a.baud}, answer hdr1=0x{a.addr:02x}")
    mod, sp, t0 = Module(a.addr, a.idle), Splitter(), time.time()
    mod.reg0, mod.reg1 = a.reg0, a.reg1
    mod.fw_mode = a.fw_mode
    mod.allow_remote_start = mod.remote_armed = a.allow_remote_start
    mod.allow_inject_file = a.allow_inject_file

    # ---- extensions: disk history, transition log / pre-alarm, settings sync, MQTT ----
    import burner_ext as X
    burner_common.set_write_log(log)                  # one line when the disk starts failing, one when it works again
    if a.data_dir:
        mod.store = X.Store(a.data_dir, a.keep_days, log)
        mod.rx_mark = os.path.join(a.data_dir, "last_rx")
        for r in mod.store.load_rows(mod.hist.maxlen):
            mod.hist.append(r)
        log(f"# data: {a.data_dir} (keep {a.keep_days:g} days); {len(mod.hist)} history rows reloaded")
        mod.cfg_hist = X.CfgHistory(a.data_dir, log)
    mod.tracker = X.Tracker(mod.store)
    mod.tracker.listeners.append(lambda ev: mod.on_event and mod.on_event(ev))
    def _keep_event(ev):                              # transitions/alarms into the dashboard's event log
        with mod.lock:
            mod.recent_events.append(ev)
    mod.tracker.listeners.append(_keep_event)
    if mod.store:                                     # event log survives restarts
        for ev in X.add_sources(mod.store.load_events(150)):
            if ev.get("type") in ("transition", "alarm", "warning", "auto_reset", "command_dropped"):
                mod.recent_events.append(ev)

    # who caused a state change: the source of a command we delivered shortly before, else the boiler
    mod.tracker.cause_fn = lambda frm, to, now: X.command_cause(mod.last_cmd, frm, to, now)
    import burner_features as F
    mod.settings = F.Settings(a.data_dir or None, log)
    if a.cfg_sync:
        mod.cfg_sync = X.CfgSync(a.cfg_sync, log, every_s=a.cfg_sync_every * 60, emit=mod.note_event)
        log("# SETTINGS SYNC ON (--cfg-sync): the board's cfg_data_1 is kept equal to the file (not saved on the board; "
            "save any one setting on the screen afterwards to make it permanent)")

    def queue_run_mode(cmd, source):
        """Queue service.run_mode start/stop on the next keep-alive (shared by trigger file and MQTT)."""
        if cmd == "start":
            mod.reg1 &= ~1
        else:
            mod.reg1 |= 1
        mod.queue_command(cmd, source)       # thread-safe; re-checked (age, scheduler state) at delivery
        mod.note_event({"type": "command", "epoch": time.time(), "cmd": cmd, "source": source})
        log(f"{ts()}  RC   remote {cmd.upper()} from {source}: queued service.run_mode={cmd} on next keep-alive; "
            f"reg1 bit0={mod.reg1 & 1}")

    mod.scheduler = F.Scheduler(mod.settings, mod.tracker, mod, queue_run_mode, log, mod.note_event)
    mod.warm = F.WarmMode(mod.settings, mod.tracker, mod, queue_run_mode, log, mod.note_event)
    mod.minmode = F.MinModeWatch(mod.settings, mod.tracker)
    mod.weather = F.Weather(mod.settings, mod.settings.path and os.path.dirname(mod.settings.path), log)
    if a.allow_remote_start:
        mod.run_mode_cb = queue_run_mode      # web start/stop (POST /api/run_mode), same rules as MQTT
    mod.lux = F.LuxMonitor(mod.settings, mod.tracker, a.data_dir or None, log, mod.note_event)
    mod.optimiser = F.Optimiser(mod.settings, mod.store, mod, log)
    mod.fan = F.FanMonitor(mod.settings, a.data_dir or None, log)
    mod.auto_reset = F.AutoReset(mod.settings, mod.tracker, mod, queue_run_mode, log, mod.note_event)
    mod.ignitions = F.IgnitionLog(mod.settings, mod.tracker, mod, a.data_dir or None, log)
    mod.vvb = F.VvbInput(mod.settings, log, mod.note_event)
    mod.ash = F.AshPlate(mod.note_event, log, a.data_dir if mod.store else None)

    # read-only facts for the Settings page (GET /api/about). Never a secret: only whether a password is set.
    mod.about = {"remote_start": bool(a.allow_remote_start), "keep_days": a.keep_days, "log_max_mb": a.log_max_mb,
                 "serial": "auto" if str(a.port).strip().lower() == "auto" else "fixed",
                 "mqtt": {"on": bool(a.mqtt), "host": a.mqtt.partition(":")[0], "prefix": a.mqtt_prefix,
                          "read_only": bool(a.mqtt_read_only),
                          "user_set": bool(a.mqtt_user if a.mqtt_user is not None else os.environ.get("BURNER_MQTT_USER")),
                          "pass_set": bool(a.mqtt_pass if a.mqtt_pass is not None else os.environ.get("BURNER_MQTT_PASS"))}}
    bridge = None
    if a.mqtt:
        bridge = mod.mqtt_bridge = X.MqttBridge(mod, a, log, queue_run_mode, mod.cfg_sync)
        mod.on_status = bridge.publish_state
        mod.on_event = bridge.event
        log(f"# mqtt: {a.mqtt} prefix '{a.mqtt_prefix}'"
            + (f", HA discovery '{a.mqtt_discovery}'" if a.mqtt_discovery else "")
            + ("; read-only (--mqtt-read-only)" if a.mqtt_read_only else
               "; plan switches on" + ("; start/stop buttons on (same rules as the web page)" if a.allow_remote_start
                                       else "; start/stop off (--allow-remote-start not given)")))
    # phone alerts (ntfy): off until turned on in the settings; outgoing messages only, never in a replay
    mod.notifier = None
    if REPLAY is None:
        try:
            import burner_notify as N
            mod.notifier = N.Notifier(mod.settings, mod, a.data_dir or None, log)
        except Exception as e:
            log(f"# alerts could not start: {e!r}")
    # start / stop from the phone (ntfy): off until set up in the settings; it only listens outbound (no open port)
    if mod.notifier:
        try:
            import burner_phone as P
            P.PhoneCommands(mod.notifier, mod, log)
        except Exception as e:
            log(f"# phone commands could not start: {e!r}")
    handlers = [h for h in (bridge.event if bridge else None, mod.notifier.on_event if mod.notifier else None) if h]
    if handlers:
        def _fan_out(ev):
            for h in handlers:
                try:
                    h(ev)
                except Exception as e:
                    log(f"# event handler error: {e!r}")
        mod.on_event = _fan_out
    if a.allow_remote_start:
        log("# REMOTE START/STOP ARMED (--allow-remote-start) — OEM-equivalent; boiler safeties stay active.")
        log(f"#    write 'start' or 'stop' to {a.remote_cmd} (discarded if older than {a.remote_cmd_max_age:.0f} s).")
        try:
            if os.path.exists(a.remote_cmd):
                os.remove(a.remote_cmd)                 # start clean: no stale command
        except OSError:
            pass
    if a.fw_mode and a.live_interval > 0:
        mod.svc = {"interval": a.live_interval, "periode": a.live_periode}
        mod.svc_every = max(60.0, a.live_periode * 60.0 - 90.0)       # renew before the periode runs out
        mod.thin = a.live_interval < 10                                # fast data: save a thinned stream
        log(f"# live data: requesting status_info every {a.live_interval} s for {a.live_periode} min (renewed)")
    if a.status_experiment:
        d2 = lambda o: json.dumps(o, separators=(",", ":"))
        def st(**kw):
            base = {"connection_status": "GOT_IP"}; base.update(kw)
            return {"devicedrive": {"status": base}}
        mod.status_cands = [
            ("GOT_IP + device_state DeviceDrive", d2(st(device_state="DeviceDrive"))),
            ("GOT_IP + device_state configuration", d2(st(device_state="configuration"))),
            ("GOT_IP + DeviceDrive + mac + rssi", d2(st(device_state="DeviceDrive", mac="00:11:22:33:44:55", rssi=-55))),
            ("GOT_IP + configuration + mac + rssi", d2(st(device_state="configuration", mac="00:11:22:33:44:55", rssi=-55))),
            ("GOT_IP + device_state start", d2(st(device_state="start"))),
            ("result OK + status GOT_IP + DeviceDrive", d2({"devicedrive": {"result": "OK", "status": {"connection_status": "GOT_IP", "device_state": "DeviceDrive"}}})),
        ]
    if a.cc_experiment:
        d_ = lambda o: json.dumps(o, separators=(",", ":"))
        mod.cc_cands = [
            ("response true", d_({"response": "true"})),
            ("response true (boolean)", d_({"response": True})),
            ("response OK", d_({"response": "OK"})),
            ("devicedrive response true", d_({"devicedrive": {"response": "true"}})),
            ("result OK", d_({"result": "OK"})),
            ("devicedrive result OK + response true", d_({"devicedrive": {"result": "OK"}, "response": "true"})),
            ("service response true", d_({"service": {"response": "true"}})),
            ("ConnectCode echo + response true", d_({"ConnectCode": 0, "response": "true"})),
        ]
    if a.inject:
        json.loads(a.inject)                      # must be valid JSON
        mod.inject_text, mod.inject_at = a.inject, time.time() + a.inject_after
        log(f"# will inject once after {a.inject_after:.0f} s: {a.inject}")
    if (a.reg1 & 1) == 0:
        log("# WARNING: reg1 bit0 = 0 may allow a REMOTE START. Never use this with a real burner.")
    if a.delay_seq:
        mod.delay_seq = [int(x) for x in a.delay_seq.split(",")]
    if a.reg1_seq:
        mod.reg1_seq = [int(x, 0) for x in a.reg1_seq.split(",")]
    if REPLAY is not None:                     # tools/replay.py: recorded data, never a real boiler
        if live or a.cfg_sync or a.mqtt:
            sys.exit("refusing: replay runs without --live, --cfg-sync and --mqtt")
        REPLAY.attach(mod)
    if a.serve:
        try:
            start_http(mod, a.serve, log, a.serve_host, [h for h in a.allowed_hosts.split(",") if h.strip()])
        except OSError as e:
            log(f"# SCADA feed could not start on port {a.serve}: {e}")
    n_in = n_out = n_bad = 0
    rc_last = 0.0
    def poll_remote_cmd():
        """Read the trigger file, queue run_mode start/stop. Deletes the file after; stale files are discarded."""
        nonlocal rc_last
        if not a.allow_remote_start:
            return
        now = time.time()
        if now - rc_last < 1.0:
            return
        rc_last = now
        try:
            if not os.path.exists(a.remote_cmd):
                return
            age = now - os.path.getmtime(a.remote_cmd)
            with open(a.remote_cmd, "r", encoding="utf-8-sig", errors="replace") as fh:
                cmd = fh.read().replace("﻿", "").strip().lower()
            os.remove(a.remote_cmd)
        except OSError:
            return
        if age > a.remote_cmd_max_age:
            # e.g. written while the serial link was down: never act on an old command hours later
            log(f"{ts()}  RC   discarded STALE remote_cmd {cmd!r} ({age:.0f} s old > {a.remote_cmd_max_age:.0f} s)")
            return
        # The real remote start/stop is a JSON service command on the J4 bus:
        # {"response":"OK","service":{"run_mode":"start"|"stop"}}. run_mode=start fires
        # only in STOPPET(0) or ALARM(6); stop fires when state!=0. Delivered as svc_once piggybacked
        # on the next fw-mode keep-alive. reg1 bit0 is also set (permission) for completeness.
        if cmd in ("start", "1", "remote-start"):
            queue_run_mode("start", "trigger file")
        elif cmd in ("stop", "nostart", "0"):
            queue_run_mode("stop", "trigger file")
        elif cmd:
            log(f"{ts()}  RC   ignored unknown remote_cmd {cmd!r} (use start / stop).")

    tick_last = 0.0
    insights_last = [time.time() - 3570]       # first Discoveries refresh ~30 s after start, then hourly
    def tick():
        """Once a second: trigger file, settings-sync timers."""
        nonlocal tick_last
        now = time.time()
        if now - tick_last < 1.0:
            return
        tick_last = now
        poll_remote_cmd()
        if mod.cfg_sync:
            try:
                mod.cfg_sync.tick()
            except Exception as e:
                log(f"# cfg-sync error: {e!r}")
        if mod.auto_reset:                       # before the scheduler: it may act on an alarm first
            try:
                mod.auto_reset.tick()
            except Exception as e:
                log(f"# auto-reset error: {e!r}")
        if mod.warm:
            try:
                mod.warm.tick()
            except Exception as e:
                log(f"# warm mode error: {e!r}")
        if mod.weather:
            try:
                mod.weather.tick()
            except Exception as e:
                log(f"# weather error: {e!r}")
        if REPLAY is None and time.time() - insights_last[0] > 3600:      # Discoveries tab: regenerate from the data files every hour
            insights_last[0] = time.time()
            def _insights():
                try:
                    import importlib.util
                    spec = importlib.util.spec_from_file_location("insights_gen", os.path.join(here, "scada", "insights_gen.py"))
                    m = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(m)
                    if mod.store is not None:              # the real data folder (--data-dir), also for the output
                        m.DATA = mod.store.dir
                        m.OUT = os.path.join(mod.store.dir, "insights.json")
                    m.main()
                except Exception as e:
                    log(f"# insights error: {e!r}")
            threading.Thread(target=_insights, daemon=True, name="insights").start()
        if mod.scheduler:
            try:
                mod.scheduler.tick()
            except Exception as e:
                log(f"# schedule error: {e!r}")
        if mod.vvb:
            try:
                mod.vvb.tick()
            except Exception as e:
                log(f"# tank sensor error: {e!r}")

    # 24/7 use: if the port is not there yet (adapter unplugged / USB not enumerated), wait for it
    # instead of dying. The dashboard / MQTT are already up meanwhile (showing "no link").
    # A time-boxed run (--duration) still gives up when its time is up.
    t_open, warned = time.time(), None
    while True:
        try:
            s = open_serial(a, log)
            break
        except serial.SerialException as e:
            if a.duration and time.time() - t_open >= a.duration:
                sys.exit(f"could not open {a.port}: {e}")
            if warned != str(e):
                log(f"# {a.port} not available ({e}); waiting for it (retry every 5 s)")
                warned = str(e)
            time.sleep(5)
    SILENT_REOPEN_S = 20          # the board polls every ~1 s; this long without a byte = dead link
    last_byte = last_reopen = time.time()
    try:
        while not a.duration or time.time() - t0 < a.duration:
            tick()
            try:
                data = s.read(256)
                now_r = time.time()
                if data:
                    last_byte = now_r
                elif now_r - last_byte > SILENT_REOPEN_S and now_r - last_reopen > SILENT_REOPEN_S:
                    # Boiler power cycle: the USB adapter resets with the board and the old handle can go deaf
                    # without any error (seen live: silence -> board ALARM 15 EXTERNAL IO). Reopen.
                    last_reopen = now_r
                    log(f"{ts()}  !!   no data from the board for {now_r - last_byte:.0f} s; reopening {a.port}")
                    try: s.close()
                    except Exception: pass
                    try:
                        s = open_serial(a, log)
                        sp = Splitter(); mod.json_in.clear()
                        log(f"{ts()}  !!   {a.port} reopened")
                    except serial.SerialException as e2:
                        raise serial.SerialException(f"reopen failed: {e2}")
                    continue
            except serial.SerialException as e:
                # USB unplugged / port lost: keep retrying instead of dying
                log(f"{ts()}  !!   serial error ({e}); reopening {a.port} in 2 s")
                try: s.close()
                except Exception: pass
                while True:
                    time.sleep(2)
                    try:
                        s = open_serial(a, log)
                        log(f"{ts()}  !!   {a.port} reopened"); sp = Splitter(); mod.json_in.clear(); break
                    except serial.SerialException:
                        if a.duration and time.time() - t0 >= a.duration:
                            break
                continue
            for f in sp.feed(data):
                n_in += 1
                if mod.cfg_sync and mod.last_rx and time.time() - mod.last_rx > 8:   # a board reboot takes ~10-15 s
                    mod.cfg_sync.request_check(f"controller was silent {time.time() - mod.last_rx:.0f} s (power cycle?)")
                mod.note_rx()                      # the SCADA "connected" flag, data/last_rx for the updater
                if f.hdr1 is not None and not f.crc_ok: n_bad += 1
                if not a.no_log_frames:
                    log(f"{ts()}  IN   {f.describe()}   | {f.raw.hex(' ')}")
                try:
                    out, why = mod.reply(f)
                except Exception as e:                  # never let one odd frame stop the service
                    mod.json_in.clear()
                    log(f"{ts()}  !!   error handling frame ({e!r}); ignored")
                    continue
                if out is None:
                    if not a.no_log_frames:
                        log(f"{ts()}  --   {why}")
                    continue
                if live:
                    try:
                        s.write(out); s.flush()
                    except serial.SerialException as e:
                        log(f"{ts()}  !!   write failed ({e}); the read side will reopen the port")
                        break
                    n_out += 1
                    if not a.no_log_frames or "SERVICE-ONCE" in why or "CFG-SYNC" in why:
                        log(f"{ts()}  OUT  {why}   | {out.hex(' ')}")
                elif not a.no_log_frames:
                    log(f"{ts()}  WOULD-SEND  {why}   | {out.hex(' ')}")
    except KeyboardInterrupt:
        pass
    finally:
        try: s.close()
        except Exception: pass
        if bridge and bridge.c.connected:
            bridge.c.publish(f"{bridge.p}/availability", "offline", retain=True)
            time.sleep(0.5)                       # let the MQTT thread send it
    log(f"\n===== frames in: {n_in}, CRC errors: {n_bad}, answers {'sent' if live else 'planned'}: "
        f"{n_out if live else 'n/a'}, last {len(mod.seen_json)} JSON requests: =====")
    for j in mod.seen_json:
        log(f"  JSON: {j}")


if __name__ == "__main__":
    main()

