#!/usr/bin/env python3
"""Start / stop the boiler from the ntfy phone app (opt-in, off by default).

  1. The phone sends "start" or "stop" to the COMMAND topic (a second secret topic, not the alerts topic), typed
     in the app or with the "Start" / "Stop" buttons on an alert.
  2. The monitor answers on the ALERTS topic: "To start the boiler reply: 4821 <your PIN> (valid 2 min)".
  3. The phone replies "4821 123456" on the command topic. Only then the command goes the same way as a start /
     stop from Home Assistant or the web page (Module.request_run_mode): refused in ALARM, never resets an alarm.

Safety: the code is random, single-use and valid 2 min; one command waits at a time; 3 wrong tries lock phone
commands for 15 min (with an alert). Only a salted hash of the PIN is kept (burner_features.hash_pin).
The monitor only LISTENS outbound (an https stream from the ntfy server, the same certificates as the alerts);
no port is opened. Messages from before the listener started, seen before, or sent by the monitor itself are
ignored ("before" in the ntfy server's clock, so a wrong Pi clock does not matter). The command topic is never published on MQTT and never written in full to a log (burner_notify.mask).
"""
import collections, datetime, hmac, json, re, secrets, threading, time, unicodedata, urllib.parse, urllib.request

import burner_features as F
import burner_notify as N

CODE_S = 120                  # the one-time code is valid this long
MAX_TRIES = 3                 # wrong code / PIN tries before the lock
LOCK_S = 900                  # phone commands locked this long after MAX_TRIES wrong tries
READ_TIMEOUT = 75             # ntfy sends a keepalive every 45 s; nothing for this long = reconnect
BACKOFF = (5, 10, 30, 60, 120, 300)
_REPLY = re.compile(r"^([0-9]{4})\s+([0-9]{4,6})$")
SLACK_S = 5                   # a message this much older than the start still counts (whole-second times)
SEEN_S = 13 * 3600            # message ids are remembered this long (ntfy keeps messages 12 h); older = "old"
SEEN_MAX = 20000              # ... but never more than this many
CLOCK_WARN_S = 120            # log once when the Pi clock is this far off the ntfy server's


def ascii_digits(text):
    """Other digit scripts (Arabic-Indic, full-width, ...) as 0-9, so "٤٨٢١ ..." is read like "4821 ..."."""
    return "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() else ch for ch in text)

# key -> (priority, tags, {lang: (title, body)})
TEXTS = {
    "ask_start": ("high", "fire", {
        "en": ("Start the boiler?", "To start the boiler reply: {code} <your PIN> (valid 2 min)"),
        "da": ("Start kedlen?", "For at starte kedlen svar: {code} <din PIN> (gælder 2 min)")}),
    "ask_stop": ("high", "black_square_for_stop", {
        "en": ("Stop the boiler?", "To stop the boiler reply: {code} <your PIN> (valid 2 min)"),
        "da": ("Stop kedlen?", "For at stoppe kedlen svar: {code} <din PIN> (gælder 2 min)")}),
    "done_start": ("default", "white_check_mark", {
        "en": ("Boiler start sent", "The boiler gets the command within about 30 s."),
        "da": ("Start af kedlen sendt", "Kedlen får kommandoen inden for ca. 30 s.")}),
    "done_stop": ("default", "white_check_mark", {
        "en": ("Boiler stop sent", "The boiler gets the command within about 30 s."),
        "da": ("Stop af kedlen sendt", "Kedlen får kommandoen inden for ca. 30 s.")}),
    "refused_alarm": ("default", "no_entry", {
        "en": ("Refused: alarm", "The boiler is in alarm. Reset it on the boiler or the web page. "
                                 "The phone never resets alarms."),
        "da": ("Afvist: alarm", "Kedlen er i alarm. Nulstil den på kedlen eller websiden. "
                                "Telefonen nulstiller aldrig alarmer.")}),
    "refused": ("default", "no_entry", {
        "en": ("Refused", "Not sent: {why}"),
        "da": ("Afvist", "Ikke sendt: {why}")}),
    "wrong": ("default", "warning", {
        "en": ("Wrong code or PIN", "Tries left: {left}."),
        "da": ("Forkert kode eller PIN", "Forsøg tilbage: {left}.")}),
    "locked": ("high", "lock", {
        "en": ("Wrong PIN 3 times — phone commands locked 15 min", "Start / stop from the phone is locked until "
                                                                   "{until}. If this was not you, make a new command "
                                                                   "topic and a new PIN on the Settings page."),
        "da": ("Forkert PIN 3 gange — telefonkommandoer låst i 15 min", "Start / stop fra telefonen er låst til "
                                                                         "{until}. Var det ikke dig, så lav et nyt "
                                                                         "kommando-emne og en ny PIN under Indstillinger.")}),
    "still_locked": ("default", "lock", {
        "en": ("Phone commands are locked", "Locked until {until} after wrong tries."),
        "da": ("Telefonkommandoer er låst", "Låst til {until} efter forkerte forsøg.")}),
    "no_pending": ("default", "hourglass", {
        "en": ("No command waiting", "The code ran out or was used. Send start or stop again."),
        "da": ("Ingen kommando venter", "Koden er udløbet eller brugt. Send start eller stop igen.")}),
    "help": ("default", "information_source", {      # at most once a minute (HELP_S)
        "en": ("Not understood", "Not understood. Write: start, stop or status. For start/stop you then get a "
                                 "code — reply with: <code> <your PIN>. The monitor never sends your PIN; you type "
                                 "it only in your reply. The code changes every time."),
        "da": ("Ikke forstået", "Ikke forstået. Skriv: start, stop eller status. Til start/stop får du en kode – "
                                "svar med: <kode> <din PIN>. Monitoren sender aldrig din PIN; du skriver den kun i "
                                "dit svar. Koden er ny hver gang.")}),
    "status": ("low", "information_source", {
        "en": ("Boiler status", "{text}"),
        "da": ("Kedel-status", "{text}")}),
}
WORDS = {"start": "start", "stop": "stop", "status": "status"}     # the same in English and Danish
HELP_S = 60                   # "Not understood" at most once a minute
STATUS_S = 15                 # "status" (no PIN needed, read-only) at most every 15 s
STATUS_WORDS = {
    "en": {"none": "No fresh data from the boiler.", "state": "State: {s}", "temp": "Boiler: {c} °C",
           "alarm": "Alarm: {a}", "last": "Last start: {t}{by}", "by": " ({w})"},
    "da": {"none": "Ingen friske data fra kedlen.", "state": "Tilstand: {s}", "temp": "Kedel: {c} °C",
           "alarm": "Alarm: {a}", "last": "Sidste start: {t}{by}", "by": " ({w})"}}
REFUSED_DA = {"remote start/stop is off (--allow-remote-start)": "fjern-start/stop er slået fra (--allow-remote-start)",
              "no fresh status from the boiler - not sent": "ingen friske data fra kedlen"}


class PhoneCommands:
    def __init__(self, notifier, mod, log, clock=time.time, start=True, mono=time.monotonic):
        self.nt, self.mod, self.log, self.clock, self.mono = notifier, mod, log, clock, mono
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.srv = None                         # (ntfy server time, mono) from the last "open" event
        self._restart()
        self.seen = collections.OrderedDict()   # message id -> mono when seen (kept SEEN_S)
        self.pending = None                     # {"cmd", "code", "until"}
        self.fails = 0
        self.locked_until = 0.0
        self.lock_told = False
        self.help_t = self.status_t = -1e12       # when "Not understood" / "status" was last answered
        self.listening = False
        self.last_error = None
        self._resp = None
        self._key = self.key()
        notifier.phone = self
        notifier.settings.listeners.append(self._settings_changed)
        if start:
            threading.Thread(target=self._loop, daemon=True, name="ntfy-cmd").start()

    # ------------------------------------------------------------------ start time, in the ntfy server's clock
    def _restart(self):
        """Messages from before now are never commands. The cut is kept in the SERVER's clock (from its "open"
        event), so a Pi clock that is off, or jumps when it is set, does not make every message look old."""
        self.t0 = self.clock()
        self.t0_mono = self.mono()
        self.t0_srv = None                      # set at the next "open" event
        self.since = str(int(self.t0))
        self.old_told = False

    def _opened(self, t):
        """The stream's "open" event carries the server's time."""
        if not isinstance(t, (int, float)) or isinstance(t, bool):
            return
        m = self.mono()
        with self.lock:
            self.srv = (float(t), m)
            if self.t0_srv is None:
                self.t0_srv = t - (m - self.t0_mono)
                if self.since == str(int(self.t0)):
                    self.since = str(int(self.t0_srv))      # no message yet: ask from the start, server time
                off = self.clock() - t
                if abs(off) > CLOCK_WARN_S:
                    self.log(f"# phone commands: the Pi clock is {off:+.0f} s off the ntfy server's; "
                             f"phone commands use the server's time")

    def _srv_now(self):
        """Now in the server's clock (the Pi clock until the first "open" event)."""
        s = self.srv
        return s[0] + (self.mono() - s[1]) if s else self.clock()

    # ------------------------------------------------------------------ settings
    def cfg(self):
        return self.nt.cfg()

    def active(self):
        c = self.cfg()
        return bool(c.get("enabled") and c.get("cmd_enabled") and c.get("topic") and c.get("cmd_topic")
                    and c.get("cmd_pin_hash"))

    def key(self):
        c = self.cfg()
        return (self.active(), c.get("server") or "https://ntfy.sh", c.get("cmd_topic") or "")

    def url(self):
        _, server, topic = self.key()
        return f"{server.rstrip('/')}/{topic}"

    def _settings_changed(self):
        k = self.key()
        if k == self._key:
            return
        topic_changed = k[1:] != self._key[1:]
        self._key = k
        with self.lock:
            self.pending = None
            if topic_changed:                   # a new topic: start fresh, nothing from before counts
                self._restart()
        r = self._resp
        if r is not None:
            try:
                r.close()                       # the stream thread reconnects with the new settings
            except Exception:
                pass
        self.wake.set()

    def actions(self):
        """The ntfy "Actions" header for the alerts: Start / Stop buttons that post to the command topic."""
        if not self.active():
            return ""
        u = self.url()
        return (f"http, Start, {u}, method=POST, body=start, clear=true; "
                f"http, Stop, {u}, method=POST, body=stop, clear=true")

    def status(self):
        c = self.cfg()
        u = self.url() if c.get("cmd_topic") else ""
        try:
            svg = N.qr_svg(u) if u and c.get("cmd_enabled") else None
        except Exception:
            svg = None
        with self.lock:
            lk = self.locked_until if self.locked_until > self.clock() else None
        return {"active": self.active(), "listening": self.listening and self.active(), "subscribe_url": u,
                "qr_svg": svg, "locked_until": lk, "error": self.last_error,
                "armed": bool(getattr(self.mod, "remote_armed", False))}

    # ------------------------------------------------------------------ answers (on the ALERTS topic)
    def reply(self, key, **p):
        prio, tags, tx = TEXTS[key]
        lang = self.nt.lang()
        title, body = tx.get(lang) or tx["en"]
        for k, v in p.items():
            title, body = title.replace("{" + k + "}", str(v)), body.replace("{" + k + "}", str(v))
        try:
            self.nt.q.put_nowait({"key": "phone", "title": title, "body": body, "priority": prio, "tags": tags})
        except Exception:
            self.log("# phone commands: queue full, answer dropped")

    def _hhmm(self, t):
        return datetime.datetime.fromtimestamp(t).strftime("%H:%M")

    # ------------------------------------------------------------------ one message from the command topic
    def handle(self, msg):
        """One message (dict from the ntfy JSON stream). Returns what was done (for the tests and the log)."""
        if isinstance(msg, dict) and msg.get("event") == "open":
            self._opened(msg.get("time"))
            return "open"
        if not isinstance(msg, dict) or msg.get("event") != "message":
            return "skip"
        mid = str(msg.get("id") or "")
        m = self.mono()
        with self.lock:
            while self.seen and (len(self.seen) >= SEEN_MAX or next(iter(self.seen.values())) < m - SEEN_S):
                self.seen.popitem(last=False)
            if not mid or mid in self.seen:
                return "seen"
            self.seen[mid] = m
            self.since = mid
            t0 = self.t0 if self.t0_srv is None else self.t0_srv
        if mid in self.nt.own_ids:
            return "own"
        t = msg.get("time")
        if not isinstance(t, (int, float)) or isinstance(t, bool):
            return "old"
        if t < self._srv_now() - SEEN_S:                # older than the ids are kept: could be a repeat
            return "old"
        if t < t0 - SLACK_S:
            with self.lock:
                told, self.old_told = self.old_told, True
            if not told:                                # once per start: else a wrong clock is silent
                self.log(f"# phone commands: message {t0 - t:.0f} s older than the start ignored - "
                         f"sent before the start, or the Pi clock is wrong?")
            return "old"
        if not self.active():
            return "off"
        return self.command(str(msg.get("message") or "").strip()[:100])

    def command(self, text):
        now = self.clock()
        with self.lock:
            word = WORDS.get(" ".join(text.lower().split()).strip(" .!?\"'“”"))
            if word == "status":                         # read-only: no code, no PIN; works while locked too
                return self._status(now)
            if now < self.locked_until:
                if self.lock_told:
                    return "locked"
                self.lock_told = True
                self.reply("still_locked", until=self._hhmm(self.locked_until))
                return "locked"
            if word:
                if self._in_alarm():
                    self.pending = None
                    self.reply("refused_alarm")
                    self.log(f"# phone commands: {word} refused - boiler in alarm")
                    return "alarm"
                code = f"{secrets.randbelow(10000):04d}"
                self.pending = {"cmd": word, "code": code, "until": now + CODE_S}    # a new ask replaces the old
                self.reply("ask_" + word, code=code)
                self.log(f"# phone commands: {word} asked, waiting for code and PIN")
                return "asked"
            m = _REPLY.match(" ".join(ascii_digits(text).split()))
            if not m:
                if now - self.help_t < HELP_S:
                    return "help_quiet"
                self.help_t = now
                self.reply("help")
                return "help"
            p = self.pending
            if not p or now > p["until"]:
                self.pending = None
                self.reply("no_pending")
                return "no_pending"
            code_ok = hmac.compare_digest(m.group(1), p["code"])
            pin_ok = F.check_pin(self.cfg().get("cmd_pin_hash"), m.group(2))
            if not (code_ok and pin_ok):
                self.fails += 1
                if self.fails >= MAX_TRIES:
                    self.fails, self.pending = 0, None
                    self.locked_until, self.lock_told = now + LOCK_S, False
                    self.reply("locked", until=self._hhmm(self.locked_until))
                    self.log(f"# phone commands: wrong code or PIN {MAX_TRIES} times - locked for {LOCK_S // 60} min")
                    return "locked_now"
                self.reply("wrong", left=MAX_TRIES - self.fails)
                self.log(f"# phone commands: wrong code or PIN ({self.fails}/{MAX_TRIES})")
                return "wrong"
            self.pending, self.fails = None, 0           # the code is used: never again
        cmd = p["cmd"]
        ok, why, code = self.mod.request_run_mode(cmd, "phone", from_hub=True)
        if ok:
            self.reply("done_" + cmd)
        elif "ALARM" in str(why):
            self.reply("refused_alarm")
        else:
            w = REFUSED_DA.get(why, why) if self.nt.lang() == "da" else why
            self.reply("refused", why=w)
        self.log(f"# phone commands: {cmd} " + ("sent" if ok else f"refused ({why})"))
        return "sent" if ok else "refused"

    def _status(self, now):
        """The read-only "status" answer: state, boiler °C, alarm, last start. No PIN, no topic in it."""
        if now - self.status_t < STATUS_S:
            return "status_quiet"
        self.status_t = now
        self.reply("status", text=self.status_text(now))
        return "status"

    def status_text(self, now=None):
        import burner_common as C
        lang = self.nt.lang()
        w = STATUS_WORDS.get(lang, STATUS_WORDS["en"])
        lat = getattr(self.mod, "latest", None) or {}
        f = lat.get("fields") or {}
        now = now or self.clock()
        lines = []
        st = C.to_int(f.get("state"))
        if st is None or now - (lat.get("epoch") or 0) > N.LINK_LOST_S:
            lines.append(w["none"])
        else:
            word = C.STATE_PAGE["da"][st].capitalize() if lang == "da" and 0 <= st < 8 else C.STATE_WORDS.get(st, str(st))
            lines.append(w["state"].format(s=word))
            bc = C.to_float(f.get("boiler_c"))
            if bc is not None:
                lines.append(w["temp"].format(c=f"{bc:.0f}"))
            al = C.to_int(f.get("alarm")) or 0
            if st == 6 or al:
                lines.append(w["alarm"].format(a=N.alarm_name(al, lang)))
        ig = getattr(self.mod, "ignitions", None)
        recs = [r for r in list(getattr(ig, "records", None) or []) if isinstance(r, dict) and not r.get("relight")]
        if recs and recs[-1].get("epoch"):
            ep = recs[-1]["epoch"]
            d = datetime.datetime.fromtimestamp(ep)
            t = d.strftime("%H:%M") if d.date() == datetime.date.today() else d.strftime("%d-%m %H:%M")
            src = C.SOURCES.get(recs[-1].get("source") or "")
            by = w["by"].format(w=src.get(lang) or src["en"]) if src else ""
            lines.append(w["last"].format(t=t, by=by))
        return "\n".join(lines)

    def _in_alarm(self):
        f = (getattr(self.mod, "latest", None) or {}).get("fields") or {}
        return f.get("state") == 6 or bool(f.get("alarm") or 0)

    # ------------------------------------------------------------------ the listener (outgoing https only)
    def _loop(self):
        n = 0
        while True:
            if not self.active():
                self.listening = False
                self.wake.wait(10)
                self.wake.clear()
                continue
            try:
                self.stream_once()
                n = 0
                self.wake.wait(2)                       # the server closed the stream: reconnect soon
            except Exception as e:
                self.listening = False
                topic = self.cfg().get("cmd_topic") or ""
                kind, msg = N.classify(e)
                self.last_error = N._scrub(msg, topic)
                wait = BACKOFF[min(n, len(BACKOFF) - 1)]
                n += 1
                self.log(f"# phone commands: listening on {N.mask(topic)} failed ({self.last_error}); "
                         f"new try in {wait} s")
                self.wake.wait(wait)
            self.wake.clear()

    def stream_once(self):
        """One long-lived GET <server>/<cmd topic>/json (https://docs.ntfy.sh/subscribe/api/); one JSON per line."""
        key = self.key()
        with self.lock:
            since = self.since
        url = f"{self.url()}/json?since={urllib.parse.quote(since)}"
        req = urllib.request.Request(url, headers={"User-Agent": "burner-monitor"})
        kw = {"context": self.nt.ca.context()} if url.startswith("https:") else {}
        with N._urlopen(req, timeout=READ_TIMEOUT, **kw) as r:
            self._resp = r
            self.listening, self.last_error = True, None
            try:
                while self.key() == key:
                    line = r.readline(65536)
                    if not line:
                        break
                    self.feed(line)
            finally:
                self._resp = None
                self.listening = False

    def feed(self, line):
        try:
            msg = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return "bad"
        try:
            return self.handle(msg)
        except Exception as e:                          # never let the listener die on one message
            self.log(f"# phone commands: error {e!r}")
            return "error"
