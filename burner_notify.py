#!/usr/bin/env python3
"""Phone alerts for burner_responder.py: push messages when something happens at the boiler.

  Notifier  - watches the monitor (alarm, contact with the boiler, silo, ash, Pi health, starts / stops, daily
              summary), turns what it sees into short messages in English or Danish and sends them in the
              background: a queue and one sending thread, so nothing here ever slows the boiler link down
  Backend   - how a message leaves the house. NtfyBackend (https://ntfy.sh or an own ntfy server) is the first;
              another service only needs a class with the same send()
  CaBundle  - the certificates used to check the server is the real one. Kept in the data folder, copied from
              the image on first use; can be updated from curl.se (checked with the current certificates) or
              from a file. Certificate checks are never turned off.

The topic works like a password: anyone who knows it can read the alerts. It is never published on MQTT and
never written in full to a log (mask()). Only outgoing connections; no port is opened.
"""
import collections, datetime, io, json, os, queue, re, shutil, ssl, threading, time, urllib.error, urllib.request

import burner_common
import burner_features as F

CLOCK_HINT = "check the Pi's clock — a wrong clock breaks certificates"
RATE_S = 1800                  # the same event at most once per 30 min (alarm raised / cleared: always)
LINK_LOST_S = 300              # no fresh data from the boiler for this long = contact lost
GRAPH_HOURS = (7, 22)          # the status picture only from 07:00 until 22:00 (local time)
GRAPH_EVERY_S = 3600           # and at most one an hour
_urlopen = urllib.request.urlopen     # tests replace this: no real network in a test


def mask(topic):
    """The topic for logs and messages: only the last 4 characters."""
    t = str(topic or "")
    return "***" + t[-4:] if len(t) > 4 else "***"


def _scrub(text, topic):
    s = str(text or "")
    return s.replace(topic, mask(topic)) if topic else s


# ============================================================================ texts (English / Danish)
# The boiler's alarm names: English as burner_ext.ALARM_TEXT (Home Assistant), Danish as the web page (home.html).
ALARM_EN = {k: v for k, v in burner_common.ALARM_TEXT.items() if k}
ALARM_DA = dict(burner_common.ALARM_TEXT_DA)

# "Started by: heating plan" - the words come from burner_ext.SOURCES (the same as the web page shows)
BY = {"en": {"burner_start": "Started by", "burner_stop": "Stopped by"},
      "da": {"burner_start": "Startet af", "burner_stop": "Stoppet af"}}

# key -> (priority, tags, {lang: (title, body)}); {x} are filled in by message()
TEXTS = {
    "alarm": ("high", "rotating_light", {
        "en": ("Boiler alarm: {name}", "The boiler stopped with alarm {code}: {name}. Look at the boiler."),
        "da": ("Kedel-alarm: {name}", "Kedlen stoppede med alarm {code}: {name}. Se til kedlen.")}),
    "alarm_clear": ("default", "white_check_mark", {
        "en": ("Alarm cleared", "The alarm ({name}) is gone."),
        "da": ("Alarm væk", "Alarmen ({name}) er væk.")}),
    "link_lost": ("high", "electric_plug", {
        "en": ("No contact with the boiler", "The monitor has had no data from the boiler for {min} minutes. "
                                             "Check the boiler's power and the cable."),
        "da": ("Ingen kontakt med kedlen", "Monitoren har ikke fået data fra kedlen i {min} minutter. "
                                           "Tjek kedlens strøm og kablet.")}),
    "link_back": ("default", "white_check_mark", {
        "en": ("Contact with the boiler is back", "The monitor gets data from the boiler again."),
        "da": ("Kontakt med kedlen igen", "Monitoren får data fra kedlen igen.")}),
    "silo_low": ("default", "warning", {
        "en": ("Silo low", "The boiler's silo counter shows {kg} kg (your limit: {lim} kg). Time to fill up."),
        "da": ("Lidt i siloen", "Kedlens silo-tæller viser {kg} kg (din grænse: {lim} kg). Tid til at fylde op.")}),
    "ash_bin": ("default", "wastebasket", {
        "en": ("Ash bin almost full", "The ash bin is about {pct} % full. Empty it soon, then press "
                                      "\"Just emptied\" on the page."),
        "da": ("Askeskuffen er næsten fuld", "Askeskuffen er ca. {pct} % fuld. Tøm den snart, og tryk så "
                                            "\"Lige tømt\" på siden.")}),
    "ash_clean": ("default", "broom", {
        "en": ("Burner needs cleaning", "Lighting is slower or the flame is weaker than normal for this burner. "
                                        "Clean the burner."),
        "da": ("Brænderen trænger til rensning", "Optænding er langsommere eller flammen svagere end normalt for "
                                                 "denne brænder. Rens brænderen.")}),
    "ash_plate": ("default", "warning", {
        "en": ("Ash plate", "{text}"),
        "da": ("Askeplade", "{text}")}),
    "pi_disk": ("default", "floppy_disk", {
        "en": ("Monitor: disk almost full", "Only {mb} MB free on the monitor's card."),
        "da": ("Monitor: disken er næsten fuld", "Kun {mb} MB fri på monitorens kort.")}),
    "pi_volt": ("default", "zap", {
        "en": ("Monitor: power too low", "The Raspberry Pi gets too little power (under-voltage). "
                                         "Use a proper power supply."),
        "da": ("Monitor: for lidt strøm", "Raspberry Pi'en får for lidt strøm (underspænding). "
                                              "Brug en ordentlig strømforsyning.")}),
    "pi_hot": ("default", "thermometer", {
        "en": ("Monitor: too hot", "The Raspberry Pi is {c} °C. Give it more air."),
        "da": ("Monitor: for varm", "Raspberry Pi'en er {c} °C. Giv den mere luft.")}),
    "net_weak": ("default", "signal_strength", {
        "en": ("Monitor: weak Wi-Fi", "The monitor's Wi-Fi signal has been weak for {min} minutes. Move the router "
                                      "or the Pi closer, or use a cable."),
        "da": ("Monitor: svag Wi-Fi", "Monitorens Wi-Fi-signal har været svagt i {min} minutter. Flyt routeren "
                                      "eller Pi'en tættere på, eller brug et kabel.")}),
    "net_down": ("default", "globe_with_meridians", {        # sent when it is back: no message gets out without it
        "en": ("Monitor: internet was down", "The monitor had no internet for {min} minutes. It works again.{wifi}"),
        "da": ("Monitor: internettet var væk", "Monitoren havde intet internet i {min} minutter. Det virker igen.{wifi}")}),
    "burner_start": ("low", "fire", {
        "en": ("Burner started", "The burner started.{who}"),
        "da": ("Brænderen startede", "Brænderen startede.{who}")}),
    "burner_stop": ("low", "black_square_for_stop", {
        "en": ("Burner stopped", "The burner stopped.{who}"),
        "da": ("Brænderen stoppede", "Brænderen stoppede.{who}")}),
    "monitor_start": ("low", "arrows_counterclockwise", {
        "en": ("Monitor started", "The burner monitor has (re)started."),
        "da": ("Monitoren startede", "Brænder-monitoren er (gen)startet.")}),
    "monitor_updated": ("default", "arrow_up", {
        "en": ("Monitor updated", "The monitor is now on {to} (was {frm})."),
        "da": ("Monitoren er opdateret", "Monitoren kører nu {to} (før {frm}).")}),
    "monitor_rolled_back": ("default", "warning", {
        "en": ("Update did not work", "{tried} did not work here. The monitor went back to {cur} by itself."),
        "da": ("Opdateringen virkede ikke", "{tried} virkede ikke her. Monitoren gik selv tilbage til {cur}.")}),
    "monitor_went_back": ("low", "arrow_down", {
        "en": ("Monitor went back", "The monitor is back on {cur}, as asked on the page."),
        "da": ("Monitoren gik tilbage", "Monitoren kører igen {cur}, som valgt på siden.")}),
    "daily": ("low", "bar_chart", {
        "en": ("Last 24 h at the boiler", "Burned {h} h, {starts} start(s), about {kg} kg pellets, {al} alarm(s)."),
        "da": ("Seneste 24 t ved kedlen", "Brændt {h} t, {starts} start(er), ca. {kg} kg piller, {al} alarm(er).")}),
    "farewell": ("default", "wave", {       # sent once to the OLD topic after "New topic"; no secrets in it
        "en": ("Subscription ended", "No more alerts will come here."),      # no hint at a new address
        "da": ("Abonnementet er afsluttet", "Der kommer ikke flere beskeder her.")}),
    "test": ("default", "bell", {
        "en": ("Test from the burner monitor", "Alerts work. You will get a message like this when something "
                                               "happens at the boiler."),
        "da": ("Test fra brænder-monitoren", "Beskederne virker. Du får en besked som denne, når der sker noget "
                                                 "ved kedlen.")}),
}
WIFI_TEXT = {"en": " Wi-Fi was lost at {lost_at} and came back at {back_at}.",
             "da": " Wi-Fi'en forsvandt kl. {lost_at} og kom igen kl. {back_at}."}
# which on/off switch (settings ntfy.events) a message belongs to
SWITCH = {"ash_bin": "ash", "ash_clean": "ash", "ash_plate": "ash", "pi_disk": "pi", "pi_volt": "pi", "pi_hot": "pi",
          "net_weak": "net", "net_down": "net",
          "monitor_updated": "monitor_start", "monitor_rolled_back": "monitor_start", "monitor_went_back": "monitor_start"}
NET_TELL_MIN = 30              # weak Wi-Fi / no internet this long before a message

ASH_PLATE_DA = {"stuck_out": "Askepladen er ikke kommet hjem igen. Tjek at intet blokerer den, og tjek kontakten.",
                "alarm_out": "Alarmen kom mens askepladen var ude af sin hjemposition. Tjek askepladen.",
                "alarm_no_turn": "Askeplade-alarm, og pladen blev ikke set forlade hjem: tjek motor, kæde/arm og stik.",
                "alarm_after_turn": "Alarmen kom lige efter askepladen drejede. Pladen er hjemme igen nu.",
                "no_turns": "Askepladen blev ikke set dreje ved de sidste starter. Tjek den på TEST MENU → INPUTS.",
                "slow_turns": "Askepladen er længere tid om at dreje end før. Den kan være stiv, slidt eller have aske under sig."}


def alarm_name(code, lang):
    names = ALARM_DA if lang == "da" else ALARM_EN
    if not code:
        return "Alarm" if lang != "da" else "Alarm"
    return names.get(int(code)) or f"Alarm {code}"


def who_words(key, source, lang):
    """' Started by: heating plan.' for a source code of a start / stop event (burner_ext.SOURCES), else ''."""
    try:
        from burner_ext import source_text
        w = source_text(source, lang) if source else None
    except Exception:
        w = None
    if not w:
        return ""
    return f" {BY.get(lang, BY['en'])[key]}: {w}."


def message(key, lang="en", **p):
    """-> (title, body, priority, tags) for a message key in English or Danish."""
    prio, tags, tx = TEXTS[key]
    title, body = tx.get(lang) or tx["en"]
    for k, v in p.items():
        title, body = title.replace("{" + k + "}", str(v)), body.replace("{" + k + "}", str(v))
    return title, body.replace("{who}", "").replace("{wifi}", ""), prio, tags


# ============================================================================ certificates
class CertError(Exception):
    pass


_PEM_BLOCK = re.compile(r"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+?-----END CERTIFICATE-----")


class CaBundle:
    """The certificate file (PEM) used for https. Lives in <data dir>/ca/cacert.pem so it survives updates."""

    URL = "https://curl.se/ca/cacert.pem"
    MAX_BYTES = 3 * 1024 * 1024

    def __init__(self, data_dir, log):
        self.log = log
        self.dir = os.path.join(data_dir, "ca") if data_dir else None
        self.path = os.path.join(self.dir, "cacert.pem") if self.dir else None
        self.lock = threading.Lock()

    @staticmethod
    def image_bundle():
        """The certificate file that came with the image / system (or certifi), or None."""
        cands = [os.environ.get("SSL_CERT_FILE")]
        try:
            vp = ssl.get_default_verify_paths()
            cands += [vp.cafile, vp.openssl_cafile]
        except Exception:
            pass
        cands.append("/etc/ssl/certs/ca-certificates.crt")
        try:
            import certifi
            cands.append(certifi.where())
        except ImportError:
            pass
        for c in cands:
            if c and os.path.isfile(c):
                return c
        return None

    @staticmethod
    def check_pem(data, min_certs=1, min_bytes=0):
        """Raise CertError unless `data` is a sane PEM certificate bundle. Returns the text."""
        if isinstance(data, bytes):
            if len(data) > CaBundle.MAX_BYTES:
                raise CertError("file too big")
            try:
                data = data.decode("utf-8")              # curl's file has UTF-8 names in its comments
            except UnicodeDecodeError:
                raise CertError("not a certificate file (PEM)")
        if len(data) > CaBundle.MAX_BYTES:
            raise CertError("file too big")
        if len(data) < min_bytes:
            raise CertError("file too small")
        blocks = _PEM_BLOCK.findall(data)
        n = data.count("-----BEGIN CERTIFICATE-----")
        if n < min_certs or n != len(blocks) or n != data.count("-----END CERTIFICATE-----"):
            raise CertError("not a certificate file (PEM)" if not n else f"only {len(blocks)} certificates")
        try:
            ssl.create_default_context(cadata="\n".join(blocks))
        except (ssl.SSLError, ValueError) as e:
            raise CertError(f"certificates can't be read ({e.__class__.__name__})")
        return data

    def ensure(self):
        """The data-folder bundle, copied from the image on first use. None = use the system's default."""
        if not self.path:
            return None
        if os.path.isfile(self.path):
            return self.path
        src = self.image_bundle()
        if not src:
            return None
        try:
            with open(src, "rb") as fh:
                self.install(fh.read(), "image")
        except (OSError, CertError) as e:
            self.log(f"# alerts: could not copy the certificates from the image ({e}); using the system's")
            return None
        return self.path

    def context(self):
        """An SSL context that always checks certificates and host names."""
        p = self.ensure()
        ctx = ssl.create_default_context(cafile=p) if p else ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = True, ssl.CERT_REQUIRED       # never anything else
        return ctx

    def install(self, data, source):
        """Check and put a new bundle in place (atomic; the old one is kept as cacert.pem.bak)."""
        if not self.path:
            raise CertError("no data folder")
        text = self.check_pem(data, min_certs=50 if source == "download" else 1,
                              min_bytes=50_000 if source == "download" else 0)
        with self.lock:
            os.makedirs(self.dir, exist_ok=True)
            if os.path.isfile(self.path):
                shutil.copy2(self.path, self.path + ".bak")
            burner_common.write_text_atomic(self.path, text, newline="\n")    # flushed to disk, then replaced
            with open(os.path.join(self.dir, "cacert.json"), "w", encoding="utf-8") as fh:
                json.dump({"source": source, "at": round(time.time())}, fh)
        return text.count("-----BEGIN CERTIFICATE-----")

    def update(self, timeout=30):
        """Download a new bundle from curl.se, checked with the CURRENT certificates. -> number of certificates.
        Any failure leaves the current bundle as it was."""
        ctx = self.context()
        req = urllib.request.Request(self.URL, headers={"User-Agent": "burner-monitor"})
        try:
            with _urlopen(req, timeout=timeout, context=ctx) as r:
                data = r.read(self.MAX_BYTES + 1)
        except Exception as e:
            kind, msg = classify(e)
            raise CertError(msg)
        return self.install(data, "download")

    def info(self):
        p = self.ensure()
        out = {"source": "system", "date": None, "count": None, "updated": None}
        if not p:
            return out
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            out["count"] = text.count("-----BEGIN CERTIFICATE-----")
            m = re.search(r"as of:\s*\w{3}\s+(\w{3})\s+(\d{1,2})\s+[\d:]+\s+(\d{4})", text)
            if m:
                out["date"] = datetime.datetime.strptime(" ".join(m.groups()), "%b %d %Y").date().isoformat()
            meta = {}
            try:
                with open(os.path.join(self.dir, "cacert.json"), encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                pass
            out["source"] = meta.get("source") or "image"
            out["updated"] = meta.get("at") or round(os.path.getmtime(p))
            if not out["date"]:
                out["date"] = datetime.date.fromtimestamp(out["updated"]).isoformat()
        except OSError:
            pass
        return out


def classify(e):
    """An exception from sending -> (kind, plain message). kind: cert / http / net."""
    r = getattr(e, "reason", None)
    if isinstance(e, ssl.SSLCertVerificationError) or isinstance(r, ssl.SSLCertVerificationError) \
            or "CERTIFICATE_VERIFY_FAILED" in str(e):
        return "cert", f"certificate problem - {CLOCK_HINT}"
    if isinstance(e, ssl.SSLError) or isinstance(r, ssl.SSLError):
        return "cert", f"secure connection failed - {CLOCK_HINT}"
    if isinstance(e, urllib.error.HTTPError):
        return "http", f"the server answered {e.code}"
    if isinstance(e, (TimeoutError,)) or "timed out" in str(e):
        return "net", "no answer from the server (timeout)"
    return "net", f"no connection to the server ({e.__class__.__name__})"


# ============================================================================ backends
class SendError(Exception):
    def __init__(self, kind, msg, retry):
        super().__init__(msg)
        self.kind, self.retry = kind, retry


class NtfyBackend:
    """POST <server>/<topic> with Title / Priority / Tags headers (https://docs.ntfy.sh/publish/)."""
    name = "ntfy"
    attachments = True             # send(attach=PNG) works; a backend without this gets the text only

    def __init__(self, ca, timeout=15):
        self.ca, self.timeout = ca, timeout

    @staticmethod
    def _header(s):
        try:
            s.encode("ascii")
            return s
        except UnicodeEncodeError:              # æøå: RFC 2047, which ntfy reads
            import base64
            return "=?UTF-8?B?" + base64.b64encode(s.encode("utf-8")).decode("ascii") + "?="

    @staticmethod
    def _message_header(s):
        """The text in the Message header (when the body is a picture): RFC 2047 when not plain one-line ASCII."""
        if "\n" in s or "\r" in s:
            import base64
            return "=?UTF-8?B?" + base64.b64encode(s.encode("utf-8")).decode("ascii") + "?="
        return NtfyBackend._header(s)

    def send(self, server, topic, title, body, priority="default", tags="", actions="", attach=None):
        """-> the message id the server gave (None if it gave none). actions = ntfy action buttons (header).
        attach = PNG bytes: sent as the body (PUT, Filename: today.png) with the text in the Message header."""
        url = f"{server.rstrip('/')}/{topic}"
        hdr = {"Title": self._header(title), "Priority": priority, "User-Agent": "burner-monitor"}
        if tags:
            hdr["Tags"] = tags
        if actions:
            hdr["Actions"] = actions
        if attach:
            hdr.update({"Filename": "today.png", "Message": self._message_header(body), "Content-Type": "image/png"})
            req = urllib.request.Request(url, data=attach, headers=hdr, method="PUT")
        else:
            hdr["Content-Type"] = "text/plain; charset=utf-8"
            req = urllib.request.Request(url, data=body.encode("utf-8"), headers=hdr, method="POST")
        kw = {"context": self.ca.context()} if url.startswith("https:") else {}
        try:
            with _urlopen(req, timeout=self.timeout, **kw) as r:
                raw = r.read(4096)
            try:
                return str(json.loads(raw).get("id") or "") or None
            except (ValueError, AttributeError, TypeError):
                return None
        except Exception as e:
            kind, msg = classify(e)
            code = getattr(e, "code", None)
            retry = kind == "net" or (kind == "http" and (code == 429 or (code or 0) >= 500))
            raise SendError(kind, _scrub(msg, topic), retry)


def qr_svg(text):
    """QR code of `text` as SVG (made here on the Pi, with segno). None when segno is not installed."""
    try:
        import segno
    except ImportError:
        return None
    buf = io.BytesIO()
    segno.make(text, error="m", micro=False).save(buf, kind="svg", scale=4, border=2, xmldecl=False,
                                                   omitsize=True, dark="#000", light="#fff")
    return buf.getvalue().decode("utf-8")


# ============================================================================ the notifier
class Notifier:
    RETRIES = (5, 20, 60)        # wait before each new try; then the message is dropped (logged)

    def __init__(self, settings, mod, data_dir, log, backend=None, sleep=time.sleep, start=True):
        self.settings, self.mod, self.log, self.sleep = settings, mod, log, sleep
        self.ca = CaBundle(data_dir, log)
        self.backend = backend or NtfyBackend(self.ca)
        self.q = queue.Queue(maxsize=50)
        self.lock = threading.Lock()
        self.state_path = os.path.join(data_dir, "ntfy-state.json") if data_dir else None
        self.state = self._load_state()
        self.last = self.state.get("last_result")
        self.t0 = time.time()
        self.pi_on = set()             # Pi problems already told about (until they go away)
        self._pi_t = self._ash_t = 0.0
        self._graph_t = 0.0            # when the last status picture was made (at most one an hour)
        self.graph_clock = time.time   # tests set their own clock
        self.phone = None              # burner_phone.PhoneCommands when start / stop from the phone is set up
        self.own_ids = collections.deque(maxlen=200)   # ids of messages sent from here (never read as commands)
        self._topic_now = self.cfg().get("topic") or ""
        settings.listeners.append(self._topic_changed)
        if start:
            threading.Thread(target=self._send_loop, daemon=True, name="ntfy-send").start()
            threading.Thread(target=self._watch_loop, daemon=True, name="ntfy-watch").start()
            threading.Thread(target=self._tell_start, args=(data_dir,), daemon=True, name="ntfy-start").start()

    def _tell_start(self, data_dir, wait=480, step=10, sleep=time.sleep):
        """The start message. When this start is the swap of an update, the updater is still testing the new
        version: wait (up to 8 min) until it has written the result, so the message can say "updated" or "went back"."""
        waited = 0
        while waited < wait and self.update_busy(data_dir):
            sleep(step)
            waited += step
        key, p = self.start_message(data_dir)
        self.notify(key, rate_key="monitor_start", **p)

    @staticmethod
    def update_busy(data_dir, now=None):
        """True while update.json (written in the last 15 min) says the updater is swapping or testing."""
        try:
            path = os.path.join(data_dir, "update.json")
            if (now or time.time()) - os.path.getmtime(path) > 900:
                return False
            with open(path, encoding="utf-8") as fh:
                return (json.load(fh) or {}).get("phase") in ("install", "back")
        except (TypeError, OSError, ValueError, AttributeError):
            return False

    @staticmethod
    def start_message(data_dir, now=None):
        """Why the monitor (re)started: an update in the last 20 min says so ("updated v1.0.2 -> v1.0.5", "did not
        work - went back"), otherwise the plain "monitor started". -> (key, {params})."""
        try:
            with open(os.path.join(data_dir, "update.json"), encoding="utf-8") as fh:
                d = json.load(fh)
            fresh = (now or time.time()) - float(d.get("checked") or 0) < 20 * 60
        except (TypeError, OSError, ValueError, AttributeError):
            return "monitor_start", {}
        if not isinstance(d, dict) or not fresh:
            return "monitor_start", {}
        code, cur, frm, to = d.get("code"), d.get("current") or "", d.get("from") or "", d.get("to") or ""
        if code == "updated" and frm and to:
            return "monitor_updated", {"frm": frm, "to": to}
        if code == "rolled_back" and cur:
            return "monitor_rolled_back", {"tried": (to if to != cur else frm) or "the new version", "cur": cur}
        if code == "went_back" and cur:
            return "monitor_went_back", {"cur": cur}
        return "monitor_start", {}

    # ------------------------------------------------------------------ settings / state
    def cfg(self):
        return self.settings.get("ntfy") or {}

    def lang(self):
        c = self.cfg()
        lg = c.get("lang") or (self.settings.get("setup") or {}).get("lang") or "en"
        return lg if lg in ("en", "da") else "en"

    def _load_state(self):
        try:                                    # broken after a power cut: the previous good copy (.bak)
            s = burner_common.read_json(self.state_path, {}, log=self.log)
            return s if isinstance(s, dict) else {}
        except TypeError:
            return {}

    def _save_state(self):
        if not self.state_path:
            return
        try:
            burner_common.write_json_atomic(self.state_path, self.state, backup=True)
        except OSError as e:
            self.log(f"# alerts: state not saved ({e})")

    # ------------------------------------------------------------------ queueing
    def notify(self, key, rate_key=None, now=None, **p):
        """Queue message `key` if alerts are on, its switch is on and it was not sent in the last 30 min.
        Returns True when queued."""
        c = self.cfg()
        if not c.get("enabled") or not c.get("topic"):
            return False
        if not (c.get("events") or {}).get(SWITCH.get(key, key), False):
            return False
        now = now or time.time()
        rk = rate_key or key
        with self.lock:
            sent = self.state.setdefault("sent", {})
            if key not in ("alarm", "alarm_clear") and now - (sent.get(rk) or 0) < RATE_S:
                return False
            sent[rk] = now
            self._save_state()
        title, body, prio, tags = message(key, self.lang(), **p)
        try:
            self.q.put_nowait({"key": key, "title": title, "body": body, "priority": prio, "tags": tags})
        except queue.Full:
            self.log(f"# alerts: queue full, dropped '{key}'")
            return False
        return True

    def _send_loop(self):
        while True:
            item = self.q.get()
            try:
                self.deliver(item)
            except Exception as e:                       # never let the sending thread die
                self.log(f"# alerts: error {e!r}")

    def deliver(self, item, retries=None):
        """Send one message now (with retries and growing waits); log and drop when it keeps failing."""
        waits = self.RETRIES if retries is None else retries
        png = self._graph(item) if getattr(self.backend, "attachments", False) else None
        for i in range(len(waits) + 1):
            c = self.cfg()
            topic = item.get("topic") or c.get("topic") or ""
            kw = {}
            if self.phone and item["key"] not in ("phone", "farewell"):
                act = self.phone.actions()                # "Start" / "Stop" buttons when phone commands are on
                if act:
                    kw["actions"] = act
            try:
                send = lambda **k: self.backend.send(c.get("server") or "https://ntfy.sh", topic, item["title"],
                                                     item["body"], item.get("priority", "default"),
                                                     item.get("tags", ""), **kw, **k)
                mid = None
                if png:
                    try:
                        mid = send(attach=png)
                    except SendError as e:
                        if e.retry:
                            raise
                        png = None                       # the server refused the picture: the text alone
                        self.log(f"ntfy: picture not sent ({_scrub(str(e), topic)}); text only")
                if not png:
                    mid = send()
                if mid:
                    self.own_ids.append(str(mid))
                self._result(item["key"], True, "sent")
                self.log(f"ntfy: sent {item['key']} (ok)")       # one line per message result (never the topic)
                return True
            except SendError as e:
                msg = _scrub(str(e), topic)
                if not e.retry or i == len(waits):
                    self._result(item["key"], False, msg, e.kind)
                    short = msg if len(msg) <= 80 else msg[:77] + "..."
                    self.log(f"ntfy: failed {item['key']}: {short}")
                    return False
                self.log(f"# alerts: '{item['key']}' to {mask(topic)} failed ({msg}); new try in {waits[i]} s")
                self.sleep(waits[i])
        return False

    def _graph(self, item):
        """Today's graph (PNG) for a message that may carry one, when the setting is on, between 07:00 and 22:00 and
        at most once an hour; None otherwise or on any error (then the text goes alone, one log line)."""
        if not item.get("graph") or not self.cfg().get("graph"):
            return None
        now = self.graph_clock()
        if not GRAPH_HOURS[0] <= datetime.datetime.fromtimestamp(now).hour < GRAPH_HOURS[1]:
            return None                                  # night: the text alone
        if 0 <= now - self._graph_t < GRAPH_EVERY_S:
            return None                                  # one picture an hour is enough
        self._graph_t = now
        try:
            import burner_graph
            return burner_graph.today_png(self.mod, self.lang())
        except Exception as e:
            self.log(f"ntfy: graph not made ({e.__class__.__name__}); text only")
            return None

    def _result(self, key, ok, msg, kind=None):
        self.last = {"t": round(time.time()), "ok": ok, "what": key, "msg": msg, "kind": kind}
        with self.lock:
            self.state["last_result"] = self.last
            self._save_state()

    def _topic_changed(self):
        """Settings listener: after "New topic", one last message to the OLD topic (when alerts are on)."""
        c = self.cfg()
        new = c.get("topic") or ""
        old, self._topic_now = self._topic_now, new
        if old and new and old != new and c.get("enabled"):
            title, body, prio, tags = message("farewell", self.lang())
            try:
                self.q.put_nowait({"key": "farewell", "title": title, "body": body, "priority": prio, "tags": tags,
                                   "topic": old})
            except queue.Full:
                self.log("# alerts: queue full, dropped 'farewell'")

    def send_test(self):
        """The "Send test" button: one message now, no retries; the answer goes back to the page."""
        c = self.cfg()
        if not c.get("topic"):
            return {"ok": False, "msg": "no topic"}
        title, body, prio, tags = message("test", self.lang())
        ok = self.deliver({"key": "test", "title": title, "body": body, "priority": prio, "tags": tags}, retries=())
        return dict(self.last or {}, ok=ok)

    # ------------------------------------------------------------------ what happened
    def on_event(self, ev):
        """Events from the monitor (state changes, warnings). Quick: only queues."""
        try:
            t = ev.get("type")
            if t == "transition":
                frm, to = ev.get("from"), ev.get("to")
                if frm in (0, 6) and to in (2, 3):
                    self.notify("burner_start", who=who_words("burner_start", ev.get("source"), self.lang()))
                elif to == 0 and frm not in (None, 0, 6):
                    self.notify("burner_stop", who=who_words("burner_stop", ev.get("source"), self.lang()))
            elif t == "warning" and ev.get("source") == "ash_plate":
                kind = str(ev.get("kind") or "")
                text = ASH_PLATE_DA.get(kind) if self.lang() == "da" else None
                self.notify("ash_plate", rate_key=f"ash_plate:{kind}", text=text or ev.get("text") or kind)
        except Exception as e:
            self.log(f"# alerts: event error {e!r}")

    def _watch_loop(self):
        while True:
            time.sleep(5)
            try:
                self.check()
            except Exception as e:
                self.log(f"# alerts: watch error {e!r}")

    def check(self, now=None):
        """Every few seconds: alarm, contact, silo; every minute Pi health; every 10 min ash; the daily summary."""
        now = now or time.time()
        lat = getattr(self.mod, "latest", None) or {}
        f, ep = lat.get("fields") or {}, lat.get("epoch")
        fresh = bool(ep) and now - ep <= LINK_LOST_S
        self._check_link(now, ep, fresh)
        if fresh:
            self._check_alarm(now, f)
            self._check_silo(f)
        if now - self._pi_t >= 60:
            self._pi_t = now
            self._check_pi(now)
            self._check_net(now)
        if now - self._ash_t >= 600:
            self._ash_t = now
            self._check_ash(now)
        self._check_daily(now)

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)
            self._save_state()

    def _check_link(self, now, ep, fresh):
        lost = self.state.get("link_lost")
        if not fresh and not lost:
            since = ep or self.t0
            if now - since > LINK_LOST_S:
                told = self.notify("link_lost", now=now, min=round((now - since) / 60))
                self._set(link_lost=True, link_told=told)
        elif fresh and lost:
            if self.state.get("link_told"):
                self.notify("link_back", now=now)
            self._set(link_lost=False, link_told=False)

    def _check_alarm(self, now, f):
        try:
            st, al = int(f.get("state")), int(f.get("alarm") or 0)
        except (TypeError, ValueError):
            return
        active = st == 6 or al != 0
        if active:
            code = al
            if not code:
                pa = getattr(getattr(self.mod, "tracker", None), "prealarm", None) or {}
                code = int(pa.get("alarm") or 0) if now - (pa.get("epoch") or 0) < 3600 else 0
            if self.state.get("alarm_on"):
                if code and code != self.state.get("alarm"):
                    if not self.state.get("alarm"):          # we only learned the number now: no new message
                        self._set(alarm=code)
                        return
                else:
                    return                               # this alarm was told already (also before a restart)
            told = self.notify("alarm", now=now, code=code or "?", name=alarm_name(code, self.lang()))
            self._set(alarm_on=True, alarm=code, alarm_told=told)
        elif self.state.get("alarm_on"):
            if self.state.get("alarm_told"):
                self.notify("alarm_clear", now=now, name=alarm_name(self.state.get("alarm"), self.lang()))
            self._set(alarm_on=False, alarm=0, alarm_told=False)

    def _check_silo(self, f):
        kg = F._f(f.get("silo_kg"))
        if kg is None:
            return
        lim = self.cfg().get("silo_low_kg") or 50
        if kg >= lim + 20:
            if not self.state.get("silo_armed"):
                self._set(silo_armed=True)               # seen full enough: the next drop below the limit is told
        elif kg < lim and self.state.get("silo_armed"):
            self.notify("silo_low", kg=round(kg), lim=lim)
            self._set(silo_armed=False)

    def _check_pi(self, now):
        try:
            import burner_ext as X
            r = X.read_pi_health(os.path.dirname(self.state_path) if self.state_path else None)
        except Exception:
            r = None
        if not r:
            return
        disk, cpu = F._f(r.get("disk_free_mb")), F._f(r.get("cpu_c"))
        volt = bool((r.get("power") or {}).get("undervolt_now"))
        checks = (("pi_disk", disk is not None and disk < 500, disk is not None and disk > 800, {"mb": round(disk or 0)}),
                  ("pi_volt", volt, not volt, {}),
                  ("pi_hot", cpu is not None and cpu >= 80, cpu is not None and cpu < 72, {"c": round(cpu or 0)}))
        for key, bad, good, p in checks:
            if bad and key not in self.pi_on:
                self.pi_on.add(key)
                self.notify(key, now=now, **p)
            elif good:
                self.pi_on.discard(key)

    def _check_net(self, now):
        """Weak Wi-Fi for 30 min: a message at once. Internet down for 30 min: a message when it is back."""
        try:
            import burner_ext as X
            r = X.net_trouble(os.path.dirname(self.state_path) if self.state_path else None, now=now)
        except Exception:
            r = None
        if not r:
            return
        weak, down = r.get("weak_min") or 0, r.get("down_min") or 0
        if weak >= NET_TELL_MIN and "net_weak" not in self.pi_on:
            self.pi_on.add("net_weak")
            self.notify("net_weak", now=now, min=weak)
        elif not weak:
            self.pi_on.discard("net_weak")
        since = self.state.get("net_down_since")
        if down >= NET_TELL_MIN and not since:
            self._set(net_down_since=now - (down - 1) * 60)      # the first bad line (each line = 1 min)
        elif not down and since:
            self._set(net_down_since=None)
            wifi = ""
            try:                                          # the Pi's Wi-Fi watchdog saw the Wi-Fi go and come back
                import burner_ext as X
                w = X.wifi_outage(os.path.dirname(self.state_path) if self.state_path else None, since - 600)
                if w:
                    wifi = WIFI_TEXT.get(self.lang(), WIFI_TEXT["en"]).format(**w)
            except Exception:
                wifi = ""
            self.notify("net_down", now=now, min=max(NET_TELL_MIN, round((now - since) / 60)), wifi=wifi)

    def _check_ash(self, now):
        opt = getattr(self.mod, "optimiser", None)
        if not opt:
            return
        try:
            a = opt.ash_status()
        except Exception:
            a = None
        if a and a.get("emptied_at") and (a.get("full_pct") or 0) >= 90 \
                and self.state.get("ash_bin_for") != a["emptied_at"]:
            self._set(ash_bin_for=a["emptied_at"])
            self.notify("ash_bin", now=now, pct=a["full_pct"])
        try:
            h = opt.health(opt.report_days())
        except Exception:
            h = None
        if h and h.get("status") == "clean":
            mark = (self.settings.get("health") or {}).get("cleaned_at") or 0
            if self.state.get("clean_for") != mark:
                self._set(clean_for=mark)
                self.notify("ash_clean", now=now)

    def _check_daily(self, now):
        at = str(self.cfg().get("daily_at") or "20:00")
        try:
            h, m = (int(x) for x in at.split(":"))
        except ValueError:
            return
        dt = datetime.datetime.fromtimestamp(now)
        target = dt.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
        today = dt.date().isoformat()
        if not 0 <= now - target < 1800 or self.state.get("daily_last") == today:
            return
        self._set(daily_last=today)
        s = self.window_summary(now)
        if s is not None:                            # counted like the pages count a day, over the last 24 h
            opt = getattr(self.mod, "optimiser", None)
            d = F.day_numbers(s, getattr(opt, "correction", lambda: 1.0)())
            self.notify("daily", now=now, h=d["burn_h"], starts=s.get("starts", 0), kg=round(d["fuel_kg"], 1),
                        al=sum((s.get("alarms") or {}).values()))

    def window_summary(self, now, hours=24):
        """Summary of the history rows of the last `hours` before `now` (the day files of both days, so it crosses
        midnight). Only rows inside the window count: a burn running at the window start counts only its hours,
        pellets after the start, not as a start; starts and alarms count when they happen."""
        store = getattr(self.mod, "store", None)
        if not store or not getattr(store, "dir", None):
            return None
        t0 = now - hours * 3600
        rows, d = [], datetime.date.fromtimestamp(t0)
        try:
            while d <= datetime.date.fromtimestamp(now):
                rows += [r for r in burner_common.rows_of(os.path.join(store.dir, f"hist-{d.isoformat()}.jsonl"))
                         if t0 <= (r.get("epoch") or 0) <= now]
                d += datetime.timedelta(days=1)
            return F.summarise_rows(rows)
        except Exception:                            # a broken row must not stop the notifier
            return None

    # ------------------------------------------------------------------ for the web page
    def status(self):
        c = self.cfg()
        url = f"{(c.get('server') or 'https://ntfy.sh').rstrip('/')}/{c.get('topic') or ''}"
        try:
            svg = qr_svg(url) if c.get("topic") else None
        except Exception as e:
            self.log(f"# alerts: QR code error {e!r}")
            svg = None
        try:
            cert = self.ca.info()
        except Exception:
            cert = None
        try:
            cmd = self.phone.status() if self.phone else None
        except Exception as e:
            self.log(f"# phone commands: status error {e!r}")
            cmd = None
        return {"backend": self.backend.name, "subscribe_url": url, "qr_svg": svg, "last": self.last, "cert": cert,
                "lang": self.lang(), "cmd": cmd}
