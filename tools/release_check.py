#!/usr/bin/env python3
"""Release check: every function of the monitor, end to end, on this PC only, with made-up data.

    <python with playwright> tools/release_check.py            everything (about 10 min)
    <python with playwright> tools/release_check.py --quick    without the full test suite (about 5 min)

Starts the REAL monitor program (burner_responder.main) in a child process against
  * a fake boiler: a small controller emulator on a 127.0.0.1 TCP socket; the program's serial port is replaced by a
    socket to it (so the "USB cable" can be pulled and put back),
  * a fake ntfy server on 127.0.0.1 (records every message, serves the JSON stream the phone commands listen to),
  * a small MQTT broker on 127.0.0.1 (scada/mqtt_check.py) and a recorder client,
and checks the boiler link, the pages, the settings, the heating plan, the start/stop buttons, alarms, phone alerts,
phone commands and Home Assistant. Then the Pi scripts (fakes), the Docker file list and the full test suite.

Safety: the child refuses every network connection that is not 127.0.0.1 (and logs the try), no real serial port is
opened, the browser aborts every request to another host, the weather service is a fake. Nothing here talks to a real
boiler, ntfy.sh, curl.se or the internet. Prints PASS / FAIL per check and a table per item; exit 1 on any FAIL.
"""
import argparse, base64, datetime, glob, http.server, json, os, queue, re, secrets, shutil, socket, subprocess
import sys, tempfile, threading, time, types, urllib.parse, urllib.request

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SCADA = os.path.join(ROOT, "scada")
LOCAL = ("127.0.0.1", "localhost", "::1")


# ============================================================================ child: the real program + guards
def child(cfg_path):
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    blocked_log = cfg["blocked_log"]

    def blocked(what):
        with open(blocked_log, "a", encoding="utf-8") as fh:
            fh.write(f"{time.time():.0f} {what}\n")

    # 1) no network except 127.0.0.1
    real_connect, real_connect_ex, real_gai = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def host_of(addr):
        return addr[0] if isinstance(addr, tuple) else str(addr)

    def g_connect(self, addr):
        if host_of(addr) not in LOCAL:
            blocked(f"connect {host_of(addr)}")
            raise OSError(f"release check: network blocked ({host_of(addr)})")
        return real_connect(self, addr)

    def g_connect_ex(self, addr):
        if host_of(addr) not in LOCAL:
            blocked(f"connect_ex {host_of(addr)}")
            return 111
        return real_connect_ex(self, addr)

    def g_gai(host, *a, **k):
        if host not in LOCAL and host is not None:
            blocked(f"lookup {host}")
            raise socket.gaierror(f"release check: name lookup blocked ({host})")
        return real_gai(host, *a, **k)
    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = g_connect, g_connect_ex, g_gai

    # 2) the serial port is a socket to the fake boiler (gone = SerialException, like a pulled USB cable)
    sys.path.insert(0, ROOT)
    import serial

    class SockSerial:
        def __init__(self, path, *a, **k):
            if path != "FAKE-BOILER":
                raise serial.SerialException(f"release check: only the fake boiler may be opened, not {path}")
            try:
                self.s = socket.create_connection(("127.0.0.1", cfg["boiler_port"]), timeout=1)
            except OSError as e:
                raise serial.SerialException(f"fake boiler not there ({e.__class__.__name__})")
            self.s.settimeout(0.02)

        def read(self, n):
            try:
                d = self.s.recv(n)
            except socket.timeout:
                return b""
            except OSError as e:
                raise serial.SerialException(f"fake boiler link lost ({e.__class__.__name__})")
            if not d:
                raise serial.SerialException("fake boiler link closed")
            return d

        def write(self, b):
            try:
                self.s.sendall(b)
            except OSError as e:
                raise serial.SerialException(f"write failed ({e.__class__.__name__})")
            return len(b)

        def flush(self):
            pass

        def close(self):
            try:
                self.s.close()
            except OSError:
                pass
    serial.Serial = SockSerial

    # 3) the weather service is a fake (never the internet)
    import burner_features as F

    def fake_get(url, params, timeout=15):
        if "geocoding" in url:
            return {"results": [{"name": "Testtown", "country_code": "DK", "latitude": 55.0, "longitude": 10.0}]}
        now = int(time.time())
        return {"current": {"temperature_2m": 3.0},
                "hourly": {"time": [now - 3600 * k for k in range(30, -1, -1)], "temperature_2m": [2.0 + (k % 5) * 0.5 for k in range(31)]}}
    F.Weather._get = staticmethod(fake_get)
    import burner_ext as X
    X.MqttBridge.STATS_EVERY_S, X.MqttBridge.STATE_EVERY_S, X.MqttBridge.PI_EVERY_S = 3, 2, 2

    for k in [k for k in os.environ if k.startswith("BURNER_")]:
        os.environ.pop(k)
    sys.argv = ["burner_responder.py", "--fw-mode", "--live", "--allow-remote-start", "--live-interval", "10",
                "--port", "FAKE-BOILER", "--serve", str(cfg["http_port"]), "--serve-host", "127.0.0.1",
                "--data-dir", cfg["data"], "--log", cfg["log"], "--no-log-frames",
                "--remote-cmd", os.path.join(os.path.dirname(cfg["log"]), "remote_cmd.txt"),
                "--mqtt", f"127.0.0.1:{cfg['mqtt_port']}"]
    import burner_responder as R
    R.main()


# ============================================================================ parent: fakes
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeBoiler:
    """A pretend controller behind a TCP socket. It polls the module like the real one (setup write, keep-alive
    read, status_info write, read; settings dump once) every 0.5 s and acts on service.run_mode like the real board:
    start in STOPPET -> OPSTART -> IGNITION -> DRIFT; start in ALARM clears it to STOPPET; stop -> STOPPET."""
    CFG = {"000": 70, "001": 2400, "004": 2000, "005": 30, "006": 3, "007": 0, "008": 0, "009": 1500, "010": 500,
           "014": 40, "015": 80, "016": 80, "017": 800, "018": 20, "019": 3, "020": 35, "031": 100, "033": 1,
           "035": 5.0, "036": 0.005, "037": 0.0}

    def __init__(self):
        sys.path.insert(0, ROOT)
        from burner_sniff import build_frame, Splitter
        self.build_frame, self.Splitter = build_frame, Splitter
        self.lock = threading.RLock()
        self.state, self.alarm, self.t_state = 0, 0, time.time()
        self.boiler_c, self.power, self.silo, self.ign = 41.0, 80, 300.0, 120
        self.auto = True
        self.cmds = []                  # (epoch, cmd, state before)
        self.cfg_writes = []
        self.plugged = True
        self.conns = []
        self.frames_out = 0
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def set(self, state=None, power=None, alarm=None, boiler_c=None):
        with self.lock:
            if state is not None and state != self.state:
                self.state, self.t_state = state, time.time()
            if power is not None:
                self.power = power
            if alarm is not None:
                self.alarm = alarm
            if boiler_c is not None:
                self.boiler_c = boiler_c

    def unplug(self):
        self.plugged = False
        for c in list(self.conns):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass
        self.conns.clear()

    def plug(self):
        self.plugged = True

    def _accept(self):
        while True:
            c, _ = self.srv.accept()
            if not self.plugged:
                c.close()
                continue
            self.conns.append(c)
            threading.Thread(target=self._session, args=(c,), daemon=True).start()

    def _status(self):
        with self.lock:
            el = time.time() - self.t_state
            if self.auto and self.state == 2 and el > 1.0:
                self.state, self.t_state = 3, time.time()
            elif self.auto and self.state == 3 and el > 2.0:
                self.state, self.t_state, self.ign = 4, time.time(), self.ign + 1
            s = self.state
            return {"000": s, "001": self.boiler_c, "002": 60 if s == 4 else 25, "003": 0.0,
                    "004": 90 if s in (3, 4) else 0, "005": self.power if s == 4 else 0, "006": s, "007": self.alarm,
                    "008": self.silo, "009": self.ign, "010": 1, "011": 0, "012": 0,
                    "013": 1500 if s in (2, 3, 4) else 0, "014": 60 if s in (2, 3, 4) else 0, "015": 1, "016": 1,
                    "017": 1 if s == 4 else 0, "018": 1 if s == 3 else 0}

    def _on_reply(self, payload):
        for msg in payload.decode("utf-8", "replace").split("\x04"):
            if not msg.strip():
                continue
            try:
                o = json.loads(msg)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            rm = (o.get("service") or {}).get("run_mode") if isinstance(o.get("service"), dict) else None
            with self.lock:
                if rm:
                    self.cmds.append((time.time(), rm, self.state))
                    if rm == "start" and self.state == 6:
                        self.state, self.alarm, self.t_state = 0, 0, time.time()
                    elif rm == "start" and self.state in (0, 1):
                        self.state, self.t_state = 2, time.time()
                    elif rm == "stop" and self.state != 0:
                        self.state, self.alarm, self.t_state = 0, 0, time.time()
                if o.get("cfg_data_1") or o.get("cfg_chg"):
                    self.cfg_writes.append(o)

    def _session(self, c):
        c.settimeout(0.05)
        sp, steps, next_t, waiting, dump = self.Splitter(), [], 0.0, 0.0, True
        try:
            while self.plugged:
                if waiting:
                    try:
                        d = c.recv(4096)
                    except socket.timeout:
                        d = None
                    if d == b"":
                        return
                    for f in sp.feed(d or b""):
                        waiting = 0.0
                        if not (f.flags & 0x20) and f.payload:
                            self._on_reply(f.payload)
                    if waiting and time.time() - waiting > 2.0:
                        waiting = 0.0
                    continue
                if not steps:
                    if time.time() < next_t:
                        time.sleep(0.01)
                        continue
                    steps = [("w", '{"devicedrive":{"command":"setup", "silent_connect":"0"}}'), ("r", None),
                             ("w", json.dumps({"status_info": self._status()}, separators=(",", ":"))), ("r", None)]
                    if dump:
                        dump = False
                        steps += [("w", json.dumps({"cfg_data_1": self.CFG}, separators=(",", ":"))), ("r", None)]
                    next_t = time.time() + 0.5
                kind, txt = steps.pop(0)
                p = (txt.encode() + b"\x04") if txt is not None else b""
                c.sendall(self.build_frame(0x01, 3, 0x20 if kind == "w" else 0x00, 4, p))
                self.frames_out += 1
                waiting = time.time()
        except OSError:
            return
        finally:
            try:
                c.close()
            except OSError:
                pass

    def cmds_since(self, t):
        with self.lock:
            return [x for x in self.cmds if x[0] >= t]


def _dec_header(s):
    m = re.match(r"^=\?UTF-8\?B\?(.*)\?=$", s or "")
    return base64.b64decode(m.group(1)).decode("utf-8") if m else (s or "")


class FakeNtfy:
    """A tiny ntfy server: POST /<topic> records a message (and passes it to listeners), GET /<topic>/json is the
    long-lived JSON stream (one message per line, keepalive every 10 s)."""

    def __init__(self):
        self.msgs, self.subs, self.lock, self.n = [], {}, threading.Lock(), 0
        me = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                topic = self.path.split("?", 1)[0].strip("/")
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                m = me.publish(topic, body, title=_dec_header(self.headers.get("Title")),
                               prio=self.headers.get("Priority"), tags=self.headers.get("Tags"),
                               actions=self.headers.get("Actions"))
                out = json.dumps({k: m[k] for k in ("id", "time", "event", "topic")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                path = self.path.split("?", 1)[0].strip("/")
                if not path.endswith("/json"):
                    self.send_response(404)
                    self.end_headers()
                    return
                topic = path[:-5]
                q = queue.Queue()
                with me.lock:
                    me.subs.setdefault(topic, []).append(q)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    self.wfile.write((json.dumps({"event": "open", "time": int(time.time()), "topic": topic}) + "\n").encode())
                    self.wfile.flush()
                    while True:
                        try:
                            m = q.get(timeout=10)
                        except queue.Empty:
                            m = {"event": "keepalive", "time": int(time.time()), "topic": topic}
                        self.wfile.write((json.dumps(m) + "\n").encode())
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    with me.lock:
                        if q in me.subs.get(topic, []):
                            me.subs[topic].remove(q)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def publish(self, topic, text, title="", prio=None, tags=None, actions=None, mid=None, t=None):
        with self.lock:
            self.n += 1
            m = {"id": mid or f"m{self.n:05d}{secrets.token_hex(4)}", "time": int(t if t is not None else time.time()),
                 "event": "message", "topic": topic, "message": text, "title": title or "",
                 "priority": prio, "tags": tags, "actions": actions, "rx": time.time()}
            self.msgs.append(m)
            subs = list(self.subs.get(topic, []))
        for q in subs:
            q.put({k: v for k, v in m.items() if k not in ("rx", "actions", "priority", "tags")})
        return m

    def inject(self, topic, m):
        """Pass a raw stream message to the listeners (as a server re-sending an old one would)."""
        with self.lock:
            subs = list(self.subs.get(topic, []))
        for q in subs:
            q.put(dict(m))

    def listening(self, topic):
        with self.lock:
            return bool(self.subs.get(topic))

    def to(self, topic, since=0.0, title=None):
        with self.lock:
            return [m for m in self.msgs if m["topic"] == topic and m["rx"] >= since
                    and (title is None or title in m["title"])]


# ============================================================================ parent: the monitor process
class Monitor:
    def __init__(self, work, boiler, mqtt_port, py):
        self.work, self.boiler, self.mqtt_port, self.py = work, boiler, mqtt_port, py
        self.data = os.path.join(work, "data")
        self.log = os.path.join(work, "resp.log")
        self.blocked = os.path.join(work, "blocked.txt")
        self.out = os.path.join(work, "child-output.txt")
        self.p, self.port, self.n_starts = None, None, 0
        os.makedirs(self.data, exist_ok=True)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.port = free_port()
        cfg = os.path.join(self.work, "child.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"boiler_port": self.boiler.port, "http_port": self.port, "data": self.data, "log": self.log,
                       "mqtt_port": self.mqtt_port, "blocked_log": self.blocked}, fh)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen([self.py, os.path.abspath(__file__), "--child", cfg], cwd=ROOT, env=env,
                                  stdout=open(self.out, "a", encoding="utf-8"), stderr=subprocess.STDOUT)
        self.n_starts += 1
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                self.get("/api/settings")
                break
            except Exception:
                time.sleep(0.2)
        return wait_for(lambda: self.status().get("connected"), 20)

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.p = None

    def restart(self):
        self.stop()
        time.sleep(0.5)
        return self.start()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def status(self):
        try:
            return self.get("/status.json")
        except Exception:
            return {}

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Burner": "1", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except ValueError:
                return e.code, {}

    def settings(self):
        return self.get("/api/settings")["settings"]

    def events(self):
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from burner_common import rows_of           # skips empty, broken and NUL (power cut) lines
        out = []
        for p in sorted(glob.glob(os.path.join(self.data, "events-*.jsonl"))):
            out.extend(rows_of(p))
        return out

    def text_of_logs(self):
        out = ""
        for p in (self.log, self.out):
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    out += fh.read()
            except OSError:
                pass
        return out


def wait_for(cond, secs, step=0.25):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


# ============================================================================ results
ITEMS = {1: "Boiler link", 2: "Web pages", 3: "Settings", 4: "Heating plan", 5: "Web start/stop",
         6: "Alarms", 7: "Phone alerts", 8: "Phone commands", 9: "MQTT / Home Assistant",
         10: "Pi scripts", 11: "Docker", 12: "Full test suite"}
RES = []          # (item, name, "PASS"/"FAIL"/"NOT TESTED", info)


def check(item, name, ok, info=""):
    RES.append((item, name, "PASS" if ok else "FAIL", "" if ok else str(info)[:300]))
    print(("PASS " if ok else "FAIL ") + f"[{item}] {name}" + ("" if ok or not info else f"   [{str(info)[:300]}]"), flush=True)
    return ok


def not_tested(item, name, why):
    RES.append((item, name, "NOT TESTED", why))
    print(f"NOT TESTED [{item}] {name}: {why}", flush=True)


# ============================================================================ the scenario
def hhmm(t):
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M")


def scenario(py, work, browser_ok):
    sys.path.insert(0, SCADA)
    sys.path.insert(0, ROOT)
    import mqtt_check
    import burner_ext as X
    boiler, ntfy = FakeBoiler(), FakeNtfy()
    mport = free_port()
    mqtt_check.Broker("127.0.0.1", mport, verbose=False)
    seen, mq_all, mlock = {}, [], threading.Lock()

    def on_msg(t, p, r):
        with mlock:
            seen[t] = p.decode("utf-8", "replace")
            mq_all.append((time.time(), t, seen[t]))
    ha = X.MqttClient("127.0.0.1", mport, "rc-recorder", lambda m: None)
    ha.on_message = on_msg
    ha.subscribe("#")
    wait_for(lambda: ha.connected, 10)

    TOPIC = "burner-" + secrets.token_urlsafe(18)
    CMD = "burner-cmd-" + secrets.token_urlsafe(18)
    PIN = "482915"
    mon = Monitor(work, boiler, mport, py)
    with open(os.path.join(mon.data, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump({"setup": {"done": True, "lang": "en"},
                   "ntfy": {"enabled": True, "server": ntfy.base, "topic": TOPIC, "cmd_topic": CMD, "lang": "en",
                            "daily_at": "03:00", "events": {"burner_start": True, "burner_stop": True,
                                                            "monitor_start": True, "daily": True}},
                   "warm": {"enabled": False, "use_board": False},
                   "vvb": {"enabled": True, "source": "mqtt"}}, fh)
    t_begin = time.time()
    secrets_seen = {TOPIC, CMD, PIN}

    # ------------------------------------------------------------------ 1 boiler link
    ok = mon.start()
    check(1, "monitor starts and gets live data from the fake boiler", ok, mon.status().get("latest"))
    st = mon.status()
    check(1, "status fields decoded (state 0, boiler 41 C, setpoint 70 from the settings dump)",
          (st.get("latest") or {}).get("fields", {}).get("state") == 0
          and (st.get("latest") or {}).get("fields", {}).get("boiler_c") == 41.0
          and ((st.get("cfg") or {}).get("fields") or {}).get("setpoint_c") == 70, st.get("latest"))
    t = time.time()
    code, r = mon.post("/api/run_mode", {"cmd": "start"})
    check(1, "states change: start -> OPSTART -> IGNITION -> DRIFT seen by the monitor",
          code == 200 and wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 4, 20), r)
    trans = [(x.get("from"), x.get("to")) for x in mon.status().get("transitions", [])]
    check(1, "transitions logged in order", all(p in trans for p in [(0, 2), (2, 3), (3, 4)]), trans)
    boiler.set(power=60)
    time.sleep(2)
    files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(mon.data, "*")))
    check(1, "data files written: hist-*.jsonl, events-*.jsonl, settings.json",
          any(f.startswith("hist-") for f in files) and any(f.startswith("events-") for f in files) and "settings.json" in files, files)
    check(1, "ignition log written (ignitions.jsonl)",
          wait_for(lambda: os.path.exists(os.path.join(mon.data, "ignitions.jsonl")), 20), files)
    h = mon.get("/history.json?h=1")
    check(1, "/history.json serves the rows", len(h.get("rows") or []) > 5, len(h.get("rows") or []))
    # unplug / replug
    rx0 = mon.status().get("last_rx")
    boiler.unplug()
    time.sleep(6)
    rx1 = mon.status().get("last_rx")
    check(1, "unplugged: no data arrives, the page keeps answering", rx1 == mon.status().get("last_rx") and rx1 >= rx0
          and mon.status() != {}, (rx0, rx1))
    boiler.plug()
    check(1, "plugged back: the monitor reopens the port and data flows again",
          wait_for(lambda: (mon.status().get("last_rx") or 0) > (rx1 or 0) + 0.5, 25),
          "no new data after replug")
    lg = mon.text_of_logs()
    check(1, "the log tells about the lost and reopened port", "serial error" in lg and "reopened" in lg)
    code, r = mon.post("/api/run_mode", {"cmd": "stop"})
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 0, 15)

    # ------------------------------------------------------------------ 2 web pages (browser)
    if browser_ok:
        web_pages(mon.base)
    else:
        not_tested(2, "pages in a browser", "Playwright not installed in this Python")

    # ------------------------------------------------------------------ 3 settings, group by group (the page's own calls)
    groups = [
        ("plan", {"warm": {"stop_pct": 45}}, {"warm": {"stop_pct": 5}}, lambda s: s["warm"]["stop_pct"] == 45),
        ("boiler", {"controller_info": {"min_pct": 20, "sw": "1.40.1", "asked": True}}, {"controller_info": {"min_pct": 500}},
         lambda s: s["controller_info"]["min_pct"] == 20 and s["controller_info"]["sw"] == "1.40.1"),
        ("boiler (size)", {"setup": {"boiler_kw": 25}}, {"setup": {"boiler_kw": 12}}, lambda s: s["setup"]["boiler_kw"] == 25),
        ("pellets", {"setup": {"store_kg": 2000}, "pellets": {"reserve_kg": 200}}, {"pellets": {"reserve_kg": 2001}},
         lambda s: s["setup"]["store_kg"] == 2000 and s["pellets"]["reserve_kg"] == 200),
        ("pellets (ash bin)", {"ash": {"bin_kg": 1.5}}, {"ash": {"bin_kg": -1}}, lambda s: s["ash"]["bin_kg"] == 1.5),
        ("alerts", {"ntfy": {"silo_low_kg": 60}}, {"ntfy": {"silo_low_kg": 0}}, lambda s: s["ntfy"]["silo_low_kg"] == 60),
        ("alerts (summary time)", {"ntfy": {"daily_at": "03:30"}}, {"ntfy": {"daily_at": "25:00"}}, lambda s: s["ntfy"]["daily_at"] == "03:30"),
        ("weather", {"weather": {"enabled": True, "name": "Testtown", "cc": "DK", "lat": 55.123, "lon": 10.456}},
         {"weather": {"cc": "SE"}}, lambda s: s["weather"]["enabled"] is True and s["weather"]["lat"] == 55.12),
        ("reset", {"auto_reset": {"alarms": [4, 5, 6, 12]}}, {"auto_reset": {"alarms": [1]}},
         lambda s: s["auto_reset"]["alarms"] == [4, 5, 6, 12] and s["auto_reset"]["enabled"] is False),
        ("tank", {"vvb": {"max_age_min": 20}}, {"vvb": {"source": "x"}}, lambda s: s["vvb"]["max_age_min"] == 20),
    ]
    for g, good, bad, test in groups:
        code, r = mon.post("/api/settings", good)
        check(3, f"settings/{g}: a good value is saved", code == 200 and r.get("msg") == "saved" and test(mon.settings()), r.get("msg"))
        before = mon.settings()
        code, r = mon.post("/api/settings", bad)
        check(3, f"settings/{g}: a bad value gives one plain line and changes nothing",
              code == 400 and r.get("ok") is False and 5 < len(r.get("msg") or "") < 90 and "\n" not in r.get("msg", "")
              and mon.settings() == before, r)
    code, r = mon.post("/api/fan/reset", {})
    check(3, "settings/service: a relearn button works", code == 200 and r.get("ok") is True, r)
    ab = mon.get("/api/about")
    check(3, "settings/about + ha: read-only facts, no secret", ab.get("version") and ab.get("remote_start") is True
          and not any(s in json.dumps(ab) for s in secrets_seen), ab)
    check(3, "the page never gets the PIN hash", "cmd_pin_hash" not in json.dumps(mon.settings()))
    if browser_ok:
        settings_alerts_browser(mon)
    mon.restart()
    s = mon.settings()
    check(3, "after a restart every saved value is still there", all(test(s) for _, _, _, test in groups),
          {k: s.get(k) for k in ("warm", "controller_info", "pellets", "ntfy")})
    mon.post("/api/settings", {"warm": {"stop_pct": 40}, "weather": {"enabled": False}})
    not_tested(3, "settings/screen", "page-only choices kept in the browser; covered by _pages_test / _settings_e2e_test")

    # ------------------------------------------------------------------ 7 phone alerts (basics)
    check(7, "'Monitor started' message at the start", wait_for(lambda: ntfy.to(TOPIC, title="Monitor started"), 10),
          [m["title"] for m in ntfy.to(TOPIC)])
    t = time.time()
    code, r = mon.post("/api/ntfy/test", {})
    check(7, "'Send test' reaches the (fake) server", code == 200 and r.get("ok") is True
          and ntfy.to(TOPIC, t, "Test from the burner monitor"), r)
    nt = mon.get("/api/ntfy")
    check(7, "subscribe link + QR shown on the page", TOPIC in (nt.get("subscribe_url") or "") and (nt.get("qr_svg") or "").startswith("<svg"), nt.keys())

    # ------------------------------------------------------------------ 8 phone commands
    code, r = mon.post("/api/settings", {"ntfy": {"cmd_pin": "1234"}})
    check(8, "an easy PIN is accepted, with a warning flag", code == 200 and r["settings"]["ntfy"]["cmd_pin_easy"] is True, r.get("msg"))
    code, r = mon.post("/api/settings", {"ntfy": {"cmd_pin": "12a4"}})
    check(8, "a PIN with letters is refused, with the reason", code == 400 and r.get("msg") == "PIN: use 4-6 digits only", r)
    code, r = mon.post("/api/settings", {"ntfy": {"cmd_pin": PIN, "cmd_enabled": True}})
    check(8, "a good PIN is saved, shown to the owner's page, and commands turned on",
          code == 200 and mon.settings()["ntfy"]["cmd_pin"] == PIN and mon.settings()["ntfy"]["cmd_enabled"] is True
          and not mon.settings()["ntfy"]["cmd_pin_easy"], r.get("msg"))
    check(8, "the monitor listens on the command topic (outgoing stream only)",
          wait_for(lambda: ntfy.listening(CMD) and mon.get("/api/ntfy")["cmd"]["listening"], 20), mon.get("/api/ntfy").get("cmd"))

    def phone(text, title, secs=10):
        t0 = time.time()
        ntfy.publish(CMD, text)
        ok = wait_for(lambda: ntfy.to(TOPIC, t0, title), secs)
        got = ntfy.to(TOPIC, t0)
        return ok, got
    ok, got = phone("status", "Boiler status")
    check(8, "'status' answers without a PIN", ok and "State: Stopped" in got[-1]["message"], [m["message"] for m in got])
    ok, got = phone("start", "Start the boiler?")
    m = re.search(r"reply: (\d{4}) ", got[-1]["message"] if got else "")
    check(8, "'start' asks for the one-time code + PIN (the PIN is never in the message)",
          ok and m and PIN not in got[-1]["message"], [g["message"] for g in got])
    t = time.time()
    ok, got = phone(f"{m.group(1) if m else '0000'} {PIN}", "Boiler start sent")
    check(8, "'<code> <PIN>' sends the start", ok, [g["title"] for g in got])
    check(8, "... the fake boiler gets the start", wait_for(lambda: any(c[1] == "start" for c in boiler.cmds_since(t)), 10))
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 4, 15)
    evs = mon.events()
    check(8, "... and it is logged with source 'phone'",
          any(e.get("type") == "command_sent" and e.get("source") == "phone" for e in evs)
          and any(e.get("type") == "transition" and e.get("to") == 2 and e.get("source") == "phone" for e in evs),
          [(e.get("type"), e.get("source")) for e in evs if e.get("type") in ("command_sent", "transition")][-6:])
    used = [x for x in ntfy.to(CMD) if x["message"].startswith(m.group(1) if m else "x")]
    t = time.time()
    if used:
        ntfy.inject(CMD, {k: used[-1][k] for k in ("id", "time", "event", "topic", "message")})
    time.sleep(3)
    check(8, "a replayed (already seen) message does nothing", used and not ntfy.to(TOPIC, t) and not boiler.cmds_since(t),
          [g["title"] for g in ntfy.to(TOPIC, t)])
    t = time.time()
    ntfy.inject(CMD, {"id": "old" + secrets.token_hex(4), "time": int(t_begin - 600), "event": "message", "topic": CMD, "message": "stop"})
    time.sleep(3)
    check(8, "an old 'stop' (sent before the monitor started) is ignored", not ntfy.to(TOPIC, t) and not boiler.cmds_since(t),
          [g["title"] for g in ntfy.to(TOPIC, t)])
    ok, got = phone("hello there", "Not understood")
    check(8, "unknown text gets the short help", ok, [g["title"] for g in got])
    t = time.time()
    ntfy.publish(CMD, "what?")
    time.sleep(3)
    check(8, "... at most once a minute", not ntfy.to(TOPIC, t, "Not understood"))
    ok, got = phone("stop", "Stop the boiler?")
    m = re.search(r"reply: (\d{4}) ", got[-1]["message"] if got else "")
    t = time.time()
    ok, got = phone(f"{m.group(1) if m else '0000'} {PIN}", "Boiler stop sent")
    check(8, "stop from the phone works the same way", ok and wait_for(lambda: any(c[1] == "stop" for c in boiler.cmds_since(t)), 10))
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 0, 15)

    # ------------------------------------------------------------------ 4 heating plan
    now = time.time()
    if datetime.datetime.now().second > 45:              # keep a clear minute boundary ahead
        time.sleep(62 - datetime.datetime.now().second)
        now = time.time()
    t_start = (int(now // 60) + 1) * 60
    per = {"name": "RC start", "start": hhmm(t_start), "end": hhmm(t_start + 20 * 60), "days": list(range(7)), "kind": "start"}
    code, r = mon.post("/api/settings", {"warm": {"enabled": True, "use_board": False, "stop_pct": 40, "stop_after_min": 0,
                                                  "periods": [per]}})
    check(4, "a 'Just start' period is saved and the plan turned on", code == 200 and mon.status()["warm"]["enabled"], r)
    t0 = time.time()
    while time.time() < t_start - 1.5:
        time.sleep(0.3)
    check(4, "no start before the period begins", not boiler.cmds_since(t0), boiler.cmds_since(t0))
    check(4, "the boiler is started when the period begins",
          wait_for(lambda: any(c[1] == "start" for c in boiler.cmds_since(t0)), 20),
          f"{mon.status()['warm']['status']}")
    late = [c[0] - t_start for c in boiler.cmds_since(t0) if c[1] == "start"]
    check(4, "... within 15 s of the period start", late and -1 <= late[0] <= 15, late)
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 4, 15)
    evs = mon.events()
    check(4, "... logged as started by the heating plan",
          any(e.get("type") == "transition" and e.get("to") == 2 and e.get("source") == "plan" and e.get("epoch", 0) >= t0 for e in evs),
          [(e.get("to"), e.get("source")) for e in evs if e.get("type") == "transition"][-4:])
    # low power in RUNNING: the plan stops it (restart = no 20-min 'just lit' guard)
    boiler.set(power=25)
    t = time.time()
    mon.restart()
    check(4, "warm mode stops the burner at/below the stop % (25 % <= 40 %)",
          wait_for(lambda: any(c[1] == "stop" for c in boiler.cmds_since(t)), 25), mon.status().get("warm", {}).get("status"))
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 0, 15)
    t = time.time()
    time.sleep(8)
    check(4, "inside the served window, stopped: no new start", not boiler.cmds_since(t), boiler.cmds_since(t))
    t = time.time()
    mon.restart()
    time.sleep(15)
    check(4, "monitor restart inside the handled window sends NO extra start", not boiler.cmds_since(t),
          (boiler.cmds_since(t), mon.status().get("warm", {}).get("status")))
    # Keep warm: never stopped at low power
    boiler.set(state=4, power=25)
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 4, 10)
    keep = {"name": "RC keep", "start": hhmm(time.time() - 60), "end": hhmm(time.time() + 40 * 60), "days": list(range(7)), "kind": "keep"}
    mon.post("/api/settings", {"warm": {"periods": [keep]}})
    t = time.time()
    mon.restart()                              # no 'just lit' guard: only the Keep-warm rule holds it
    time.sleep(15)
    ws = mon.status().get("warm", {}).get("status", {})
    check(4, "Keep warm: burning at low power is never stopped", not boiler.cmds_since(t) and ws.get("code") == "keep",
          (boiler.cmds_since(t), ws))
    # Warm by: lead from the boiler temperature and the setpoint
    target = time.time() + 3 * 3600
    wb = {"name": "RC warm by", "start": hhmm(target), "end": hhmm(target + 30 * 60), "days": list(range(7)), "kind": "warmby"}
    mon.post("/api/settings", {"warm": {"periods": [wb]}})
    boiler.set(state=0, power=0, boiler_c=41.0)
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 0, 10)
    time.sleep(3)
    w = mon.status().get("warm", {})
    # 35 min lighting + 1.5 min/C from 52 C (RUNNING) to 70-5 C = 54.5 -> 55 (rounded up to 5 min)
    pl = [p for p in (w.get("plan") or []) if p.get("warmby")]
    check(4, "Warm by: lead computed (55 min = lighting 35 + 1.5 min/C x 13 C, rounded up)",
          w.get("lead_now") == 55 and pl and pl[0].get("lead") == 55, (w.get("lead_now"), pl[:1]))
    tgt_m = int(hhmm(target)[:2]) * 60 + int(hhmm(target)[3:])
    st_m = (tgt_m - 55) % 1440
    check(4, "Warm by: the planned start is 55 min before the target",
          w.get("next_start") and hhmm(w["next_start"]) == f"{st_m // 60:02d}:{st_m % 60:02d}", (w.get("next_start") and hhmm(w["next_start"])))
    mon.post("/api/settings", {"warm": {"enabled": False, "periods": []}})

    # ------------------------------------------------------------------ 5 web start/stop
    t = time.time()
    code, r = mon.post("/api/run_mode", {"cmd": "start"})
    check(5, "web Start reaches the fake boiler", code == 200 and wait_for(lambda: any(c[1] == "start" for c in boiler.cmds_since(t)), 10), r)
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 4, 15)
    t = time.time()
    code, r = mon.post("/api/run_mode", {"cmd": "stop"})
    check(5, "web Stop reaches the fake boiler", code == 200 and wait_for(lambda: any(c[1] == "stop" for c in boiler.cmds_since(t)), 10), r)
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 0, 15)
    evs = mon.events()
    check(5, "web commands logged with source 'web'", any(e.get("type") == "command_sent" and e.get("source") == "web" for e in evs))
    code, r = mon.post("/api/run_mode", {"cmd": "restart"})
    check(5, "an unknown command is refused", code == 400, r)
    if browser_ok:
        web_button(mon, boiler)

    # ------------------------------------------------------------------ 9 MQTT / Home Assistant
    check(9, "HA discovery published (state, start/stop buttons, plan switch)",
          wait_for(lambda: any(t_.startswith("homeassistant/button/burner/start") for _, t_, _ in mq_all)
                   and any(t_ == "homeassistant/switch/burner/heating_plan/config" for _, t_, _ in mq_all), 15),
          sorted({t_.split("/")[1] for _, t_, _ in mq_all if t_.startswith("homeassistant/")}))
    check(9, "state topic burner/state published", wait_for(lambda: "burner/state" in seen and json.loads(seen["burner/state"]).get("state") == 0, 10),
          seen.get("burner/state", "")[:120])
    t = time.time()
    ha.publish("burner/cmd/run_mode", "start")
    check(9, "MQTT start reaches the fake boiler", wait_for(lambda: any(c[1] == "start" for c in boiler.cmds_since(t)), 10))
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 4, 15)
    t = time.time()
    ha.publish("burner/cmd/run_mode", "stop")
    check(9, "MQTT stop reaches the fake boiler", wait_for(lambda: any(c[1] == "stop" for c in boiler.cmds_since(t)), 10))
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 0, 15)
    ha.publish("burner/cmd/vvb", "52.5")
    check(9, "VVB (tank) temperature command taken", wait_for(lambda: json.loads(seen.get("burner/vvb") or "{}").get("sent_c") == 52.5, 10),
          seen.get("burner/vvb"))
    ha.publish("burner/cmd/cfg", '{"000": 66}')
    check(9, "boiler settings can't be changed over MQTT",
          wait_for(lambda: any(t_ == "burner/cmd/result" and "unknown command" in p for _, t_, p in mq_all), 10) and not boiler.cfg_writes)

    # ------------------------------------------------------------------ 6 alarms (+ phone / MQTT refusals in alarm)
    boiler.set(state=4, power=60)
    wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 4, 10)
    time.sleep(2)
    t_al = time.time()
    boiler.set(state=6, alarm=4)
    check(6, "fake alarm 4 shown in the status", wait_for(lambda: mon.status()["latest"]["fields"]["alarm"] == 4, 10))
    check(6, "pre-alarm snapshot kept", wait_for(lambda: (mon.status().get("prealarm") or {}).get("alarm") == 4, 10),
          mon.status().get("prealarm"))
    check(6, "alarm phone message with the alarm's name",
          wait_for(lambda: ntfy.to(TOPIC, t_al, "Boiler alarm: Start-up failed"), 15), [m["title"] for m in ntfy.to(TOPIC, t_al)])
    if browser_ok:
        alarm_page(mon)
    t = time.time()
    ha.publish("burner/cmd/run_mode", "stop")
    check(6, "MQTT stop refused in ALARM", wait_for(lambda: any(t_ == "burner/cmd/result" and "ALARM" in p and tt >= t
                                                                for tt, t_, p in mq_all), 10))
    ok, got = phone("start", "Refused: alarm")
    check(8, "phone 'start' refused in ALARM (no code asked)", ok and not any("Start the boiler?" in g["title"] for g in got),
          [g["title"] for g in got])
    time.sleep(12)
    check(6, "alarm never reset by itself (auto reset off): nothing sent for 25 s", not boiler.cmds_since(t_al), boiler.cmds_since(t_al))
    mon.post("/api/settings", {"auto_reset": {"enabled": True, "alarms": [4, 5, 6], "max_per_day": 2, "wait_min": 1}})
    check(6, "auto reset on: after the wait the alarm is cleared and the boiler restarted",
          wait_for(lambda: [c[1] for c in boiler.cmds_since(t_al)][:2] == ["start", "start"], 75),
          (boiler.cmds_since(t_al), mon.status().get("auto_reset")))
    check(6, "... counted as 'saved by auto alarm reset'",
          wait_for(lambda: (mon.status().get("auto_reset") or {}).get("saves") == 1, 25), mon.status().get("auto_reset"))
    check(6, "'Alarm cleared' phone message", wait_for(lambda: ntfy.to(TOPIC, t_al, "Alarm cleared"), 15))
    mon.post("/api/settings", {"auto_reset": {"max_per_day": 1}})
    time.sleep(3)
    t = time.time()
    boiler.set(state=6, alarm=4)
    check(6, "auto reset limited: 1 try in 24 h used -> it gives up, the alarm stays",
          wait_for(lambda: ((mon.status().get("auto_reset") or {}).get("status") or {}).get("code") == "gave_up", 20),
          mon.status().get("auto_reset"))
    time.sleep(8)
    check(6, "... and nothing is sent to the boiler", not boiler.cmds_since(t), boiler.cmds_since(t))
    mon.post("/api/settings", {"auto_reset": {"enabled": False}})
    t = time.time()
    code, r = mon.post("/api/run_mode", {"cmd": "stop"})
    check(6, "web Stop in ALARM without the confirm flag is refused", code == 409, r)
    code, r = mon.post("/api/run_mode", {"cmd": "stop", "reset_alarm": True})
    check(6, "web 'Reset alarm' clears it (the owner's own page may, after its confirm)", code == 200
          and wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 0 and mon.status()["latest"]["fields"]["alarm"] == 0, 15), r)

    # ------------------------------------------------------------------ 8 wrong PIN x3 (last: it locks phone commands)
    ok, got = phone("start", "Start the boiler?")
    t = time.time()
    titles = []
    for k in range(3):
        t1 = time.time()
        ntfy.publish(CMD, f"0000 {PIN[::-1]}")
        wait_for(lambda: ntfy.to(TOPIC, t1), 8)
        titles += [g["title"] for g in ntfy.to(TOPIC, t1)]
    check(8, "wrong PIN x3: 'Tries left' twice, then locked 15 min (with an alert)",
          titles[:2] == ["Wrong code or PIN"] * 2 and "locked" in (titles[2] if len(titles) > 2 else ""), titles)
    m2 = re.search(r"reply: (\d{4}) ", got[-1]["message"] if got else "")
    t1 = time.time()
    ok, got2 = phone(f"{m2.group(1) if m2 else '0000'} {PIN}", "locked")
    time.sleep(2)
    check(8, "... locked: even the right code + PIN does nothing", not boiler.cmds_since(t), boiler.cmds_since(t))

    # ------------------------------------------------------------------ 7 more alerts
    t = time.time()
    mon.post("/api/settings", {"ntfy": {"daily_at": hhmm(time.time())}})
    check(7, "daily summary at the chosen time", wait_for(lambda: ntfy.to(TOPIC, t, "Last 24 h at the boiler"), 15),
          [m["title"] for m in ntfy.to(TOPIC, t)])
    starts = [c for c in boiler.cmds if c[1] == "start" and c[2] in (0, 1)]
    bs = ntfy.to(TOPIC, title="Burner started")
    check(7, f"rate limit: {len(starts)} starts in a few minutes, one 'Burner started' message", len(starts) >= 3 and len(bs) == 1,
          (len(starts), len(bs)))
    check(7, "start message says who started it", bs and "Started by: web page" in bs[0]["message"], bs[:1])
    check(7, "alerts carry Start/Stop buttons only when phone commands are on",
          any(m.get("actions") for m in ntfy.to(TOPIC, t)) and not any(m.get("actions") for m in ntfy.to(TOPIC) if m["title"] == "Monitor started"))
    old = TOPIC
    t = time.time()
    code, r = mon.post("/api/settings", {"ntfy": {"new_topic": True}})
    new = mon.settings()["ntfy"]["topic"]
    secrets_seen.add(new)
    check(7, "New topic: a new random topic", code == 200 and new != old and new.startswith("burner-"), r.get("msg"))
    check(7, "... the old topic gets one last 'Subscription ended'",
          wait_for(lambda: ntfy.to(old, t, "Subscription ended"), 10) and old not in ntfy.to(old, t, "Subscription ended")[0]["message"]
          and new not in ntfy.to(old, t, "Subscription ended")[0]["message"])
    t = time.time()
    mon.post("/api/ntfy/test", {})
    check(7, "... and new messages go to the new topic only", ntfy.to(new, t, "Test") and not ntfy.to(old, t + 0.01, "Test"))

    # ------------------------------------------------------------------ secrecy, errors, network (whole run)
    lg = mon.text_of_logs()
    leaked = [s for s in secrets_seen if s in lg]
    check(7, "topics and PIN never written in full to the logs", not leaked, [s[-4:] for s in leaked])
    mq_text = json.dumps(mq_all)
    check(9, "no secret on MQTT (alert topic, command topic, PIN)", not any(s in mq_text for s in secrets_seen))
    check(9, "the boiler was never sent a settings change", not boiler.cfg_writes, boiler.cfg_writes[:2])
    bad = [l for l in lg.splitlines() if "Traceback" in l or re.search(r"# [a-z ]+ error[: ]", l) or "extension error" in l]
    check(1, "no Python errors in the monitor's log / output during the whole run", not bad, bad[:5])
    try:
        with open(mon.blocked, encoding="utf-8") as fh:
            blk = fh.read().splitlines()
    except OSError:
        blk = []
    check(1, "the monitor never tried to reach the internet (only 127.0.0.1)", not blk, blk[:5])
    mon.stop()
    return mon


# ============================================================================ browser parts
def _ctx(br, base, lang, view, blocked):
    vp = {"phone": {"width": 390, "height": 844}, "desktop": {"width": 1366, "height": 900}}[view]
    ctx = br.new_context(viewport=vp, locale="da-DK" if lang == "da" else "en-GB", is_mobile=view == "phone",
                         has_touch=view == "phone", device_scale_factor=2 if view == "phone" else 1)
    ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');}}catch(e){{}}")

    def guard(route):
        if not route.request.url.startswith(base + "/"):
            blocked.append(route.request.url[:80])
            return route.abort()
        route.continue_()
    ctx.route("**/*", guard)
    return ctx


def _page(ctx):
    pg = ctx.new_page()
    pg.errs = []
    pg.on("console", lambda m: m.type == "error" and pg.errs.append(m.text[:150]))
    pg.on("pageerror", lambda e: pg.errs.append("pageerror: " + str(e)[:150]))
    pg.on("dialog", lambda d: (pg.errs.append("popup: " + d.message), d.dismiss()))
    return pg


def web_pages(base):
    from playwright.sync_api import sync_playwright
    pages = {"home": ("/home", ("now", "pellets", "history", "plan", "settings"), "#v-{}"),
             "classic": ("/classic", ("live", "seq", "sch", "opt", "disc"), "#tab-{}")}
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for lang in ("en", "da"):
            for view in ("phone", "desktop"):
                blocked, bad, empty, wide = [], [], [], []
                ctx = _ctx(br, base, lang, view, blocked)
                for name, (path, tabs, sel) in pages.items():
                    pg = _page(ctx)
                    for tab in tabs:            # a fresh load per tab: the classic page reads the #tab only when it loads
                        pg.goto(f"{base}{path}?tab={tab}#{tab}", wait_until="load")
                        pg.wait_for_timeout(1300)
                        txt = pg.evaluate("s=>{const e=document.querySelector(s);return e&&e.offsetParent!==null?e.innerText.trim():''}",
                                          sel.format(tab))
                        if len(txt) < 40:
                            empty.append(f"{name}#{tab}")
                        if pg.evaluate("document.documentElement.scrollWidth>window.innerWidth+1"):
                            wide.append(f"{name}#{tab}")
                    if pg.get_attribute("html", "data-jserr"):
                        bad.append(f"{name}: data-jserr")
                    bad += [f"{name}: {e}" for e in pg.errs]
                    pg.close()
                pg = _page(ctx)
                pg.goto(f"{base}/guide", wait_until="load")
                pg.wait_for_timeout(800)
                gtxt = pg.inner_text("body")
                bad += [f"guide: {e}" for e in pg.errs]
                gwide = pg.evaluate("document.documentElement.scrollWidth>window.innerWidth+1")
                pg.close()
                tag = f"{lang} {view}"
                check(2, f"[{tag}] /home all 5 tabs + /classic all 5 tabs show content", not empty, empty)
                check(2, f"[{tag}] no console errors", not bad, bad[:4])
                check(2, f"[{tag}] /guide loads (pictures inside)", len(gtxt) > 2000 and "data:image" in pg_content(base), len(gtxt))
                check(2, f"[{tag}] no sideways scrolling (pages and guide)", not wide and not gwide, wide + (["guide"] if gwide else []))
                check(2, f"[{tag}] the pages ask nothing from other hosts", not blocked, blocked[:3])
                ctx.close()
        br.close()


_GUIDE = {}


def pg_content(base):
    if base not in _GUIDE:
        with urllib.request.urlopen(base + "/guide", timeout=10) as r:
            _GUIDE[base] = r.read().decode("utf-8", "replace")
    return _GUIDE[base]


def settings_alerts_browser(mon):
    """The Phone alerts group is not in a replay (the suite's browser test): check its save rule here."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        blocked = []
        ctx = _ctx(br, mon.base, "en", "phone", blocked)
        pg = _page(ctx)
        try:
            pg.goto(f"{mon.base}/home#settings/alerts", wait_until="load")
            pg.wait_for_selector("#ntSilo", timeout=15000)
            pg.fill("#ntSilo", "75")
            pg.press("#ntSilo", "Tab")
            pg.wait_for_timeout(900)
            msg = pg.inner_text('[data-fmsg="nt.silo_low"]')
            check(3, "settings/alerts in the browser: a value is saved with 'Saved' under it",
                  mon.settings()["ntfy"]["silo_low_kg"] == 75 and "Saved" in msg, msg)
            pg.fill("#ntSilo", "0")
            pg.press("#ntSilo", "Tab")
            pg.wait_for_timeout(700)
            msg = pg.inner_text('[data-fmsg="nt.silo_low"]')
            check(3, "settings/alerts in the browser: a wrong value gives one plain line, nothing saved",
                  mon.settings()["ntfy"]["silo_low_kg"] == 75 and 5 < len(msg) < 80, msg)
            pg.reload(wait_until="load")
            pg.wait_for_selector("#ntSilo", timeout=15000)
            check(3, "settings/alerts in the browser: reload shows the saved value", pg.input_value("#ntSilo") == "75")
            txt = pg.inner_text("#stPane")
            check(3, "settings/alerts: topic shown with a Copy button (only on this page)",
                  pg.query_selector('[data-nt="copy"]') is not None, txt[:100])
            check(3, "settings/alerts: no console errors", not pg.errs and not blocked, pg.errs[:3] + blocked[:3])
            mon.post("/api/settings", {"ntfy": {"silo_low_kg": 60}})
        except Exception as e:
            check(3, "settings/alerts in the browser", False, repr(e)[:200])
        ctx.close()
        br.close()


def web_button(mon, boiler):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        blocked = []
        ctx = _ctx(br, mon.base, "en", "phone", blocked)
        pg = _page(ctx)
        try:
            pg.goto(f"{mon.base}/home#now", wait_until="load")
            pg.wait_for_selector('[data-act="start"]', timeout=15000)
            t = time.time()
            pg.click('[data-act="start"]')
            pg.wait_for_selector('.scrim [data-a="1"]', timeout=5000)
            pg.click('.scrim [data-a="1"]')
            check(5, "the page's Start button (with its question) reaches the fake boiler",
                  wait_for(lambda: any(c[1] == "start" for c in boiler.cmds_since(t)), 10))
            wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 4, 15)
            pg.wait_for_selector('[data-act="stop"]', timeout=60000)
            t = time.time()
            pg.click('[data-act="stop"]')
            pg.wait_for_selector('.scrim [data-a="1"]', timeout=5000)
            pg.click('.scrim [data-a="1"]')
            check(5, "the page's Stop button reaches the fake boiler", wait_for(lambda: any(c[1] == "stop" for c in boiler.cmds_since(t)), 10))
            wait_for(lambda: mon.status()["latest"]["fields"]["state"] == 0, 15)
            check(5, "no console errors while pressing the buttons", not pg.errs and not blocked, pg.errs[:3])
        except Exception as e:
            check(5, "the page's Start/Stop buttons", False, repr(e)[:200])
        ctx.close()
        br.close()


def alarm_page(mon):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        blocked = []
        ctx = _ctx(br, mon.base, "en", "desktop", blocked)
        pg = _page(ctx)
        try:
            pg.goto(f"{mon.base}/home#now", wait_until="load")
            pg.wait_for_selector(".ahero", timeout=15000)
            check(6, "alarm shown on the page with its name", "Start-up failed" in pg.inner_text(".ahero") or "ALARM 4" in pg.inner_text(".ahero"),
                  pg.inner_text(".ahero")[:120])
            check(5, "in ALARM the page offers no Start button (only 'Reset alarm')",
                  pg.query_selector('[data-act="start"]') is None and pg.query_selector('[data-act="reset"]') is not None)
            check(6, "no console errors on the alarm page", not pg.errs and not blocked, pg.errs[:3])
        except Exception as e:
            check(6, "alarm on the page", False, repr(e)[:200])
        ctx.close()
        br.close()


# ============================================================================ item 10: Pi scripts
def pi_health_fake(work):
    """pi/pi_health.py on made-up inputs: every command and /proc file is a fake (no ping, no DNS lookup)."""
    import importlib.util
    d = os.path.join(work, "pi-health")
    os.makedirs(d, exist_ok=True)
    spec = importlib.util.spec_from_file_location("pi_health_rc", os.path.join(ROOT, "pi", "pi_health.py"))
    m = importlib.util.module_from_spec(spec)
    argv = sys.argv
    sys.argv = ["pi_health.py", d]
    try:
        spec.loader.exec_module(m)
    finally:
        sys.argv = argv
    calls = []

    def case(dns_ok, ping_ok, throttled):
        outs = {"vcgencmd": f"throttled={throttled}", "timedatectl": "yes",
                "nmcli": "wlan0:wifi:connected\neth0:ethernet:unavailable", "ip": "lo UNKNOWN 127.0.0.1/8\nwlan0 UP 192.0.2.5/24",
                "getent": "192.0.2.1  pool.ntp.org" if dns_ok else "", "docker": "running 2 2000-01-01T00:00:00Z"}

        def fake_run(*cmd, timeout=5):
            calls.append(cmd[0])
            if cmd[0] == "nmcli" and "wifi" in cmd:
                return "*:61\n :40"
            return outs.get(cmd[0], "")
        m.run = fake_run
        m.subprocess = types.SimpleNamespace(run=lambda cmd, **k: (calls.append(cmd[0]), types.SimpleNamespace(returncode=0 if ping_ok else 1))[1])
        files = {"/sys/class/thermal/thermal_zone0/temp": "48312", "/proc/meminfo": "MemTotal: 4000000 kB\nMemAvailable:  2048000 kB",
                 "/proc/sys/kernel/random/boot_id": "abcdef12-3456", "/proc/uptime": "7200.5 100.0", "/proc/loadavg": "0.25 0.20 0.10 1/100 1"}
        m.read = lambda p: files.get(p, "")
        real_glob = glob.glob
        m.glob = types.SimpleNamespace(glob=lambda p: ["/dev/serial/by-id/usb-test-adapter"] if "by-id" in p else
                                       ([] if p.startswith("/dev/") else real_glob(p)))
        had = hasattr(os, "statvfs")
        old = getattr(os, "statvfs", None)
        os.statvfs = lambda p: types.SimpleNamespace(f_bavail=5 * 1024 * 256, f_frsize=4096)
        try:
            for p in glob.glob(os.path.join(d, "*.jsonl")):
                os.remove(p)
            m.main()
        finally:
            if had:
                os.statvfs = old
            else:
                del os.statvfs
        with open(glob.glob(os.path.join(d, "pi-health-*.jsonl"))[0], encoding="utf-8") as fh:
            return json.loads(fh.read().splitlines()[-1])
    try:
        r = case(True, True, "0x50005")
        check(10, "pi_health.py on fake inputs: one line with CPU, power, memory, disk, links, USB adapter",
              r["cpu_c"] == 48.3 and r["power"]["undervolt_now"] is True and r["power"]["undervolt_since_boot"] is True
              and r["mem_free_mb"] == 2000 and r["disk_free_mb"] == 5120 and r["net"]["wifi_signal"] == 61
              and r["net"]["ip"] == {"wlan0": "192.0.2.5"} and r["usb_serial"] == ["usb-test-adapter"]
              and r["container"]["restarts"] == 2 and r["clock_synced"] is True, r)
        check(10, "pi_health.py: internet + DNS check OK", r["internet"]["ok"] is True and r["internet"]["dns_ok"] is True, r["internet"])
        r = case(False, True, "0x0")
        check(10, "pi_health.py: broken DNS seen (ping OK, name lookup fails)", r["internet"]["ok"] is True and r["internet"]["dns_ok"] is False
              and r["internet"]["dns_ms"] is None and r["power"]["undervolt_now"] is False, r["internet"])
        r = case(False, False, "")
        check(10, "pi_health.py: no internet and no vcgencmd handled", r["internet"]["ok"] is False and r["power"] is None, r)
        sys.path.insert(0, ROOT)
        import burner_ext as X
        got = X.read_pi_health(d)
        check(10, "the monitor reads the Pi health line (for the page, alerts and HA)", got and got.get("cpu_c") == 48.3, got)
        check(10, "pi_health.py writes no network names", "SSID" not in json.dumps(r) and "ssid" not in json.dumps(r))
    except Exception as e:
        check(10, "pi_health.py on fake inputs", False, repr(e)[:300])


def bash_exe():
    for p in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "bin", "bash.exe"),
              r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash") or ""):
        if p and os.path.isfile(p) and "System32" not in p:
            return p
    return None


def pi_scripts(py, work):
    pi_health_fake(work)
    for name in ("_wifi_setup_test.py", "_card_test.py"):
        r = subprocess.run([py, os.path.join(ROOT, "pi", name)], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=300)
        fails = [l for l in r.stdout.splitlines() if l.startswith("FAIL")]
        check(10, f"pi/{name} (fake nmcli / static card checks)", r.returncode == 0, (fails or r.stdout.splitlines()[-5:] + r.stderr.splitlines()[-3:]))
    bash = bash_exe()
    shs = sorted(set(glob.glob(os.path.join(ROOT, "**", "*.sh"), recursive=True)))
    for p in glob.glob(os.path.join(ROOT, "pi", "firstboot", "*")):
        try:
            with open(p, "rb") as fh:
                if fh.read(2) == b"#!" and p not in shs:
                    shs.append(p)
        except OSError:
            pass
    if not bash:
        not_tested(10, "install scripts bash -n", "no Git bash found")
        return
    bad = []
    for p in shs:
        r = subprocess.run([bash, "-n", p.replace("\\", "/")], capture_output=True, text=True)
        if r.returncode:
            bad.append(f"{os.path.relpath(p, ROOT)}: {r.stderr.strip()[:120]}")
    check(10, f"bash -n on every shell script ({len(shs)})", not bad and len(shs) >= 5, bad[:5])


# ============================================================================ item 11: Docker
def docker_check():
    with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
        df = fh.read()
    copied = []
    for line in re.findall(r"^COPY\s+(.+)$", df, re.M):
        copied += line.split()[:-1]
    mods = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "burner_*.py")))
    miss = [m for m in mods if m not in copied]
    check(11, "Dockerfile copies every burner_*.py", not miss, miss)
    check(11, "Dockerfile copies scada/ (pages, shared.js, insights, places) and docs/guide.html",
          "scada/" in copied and "docs/guide.html" in copied, copied)
    need = ["scada/home.html", "scada/dashboard.html", "scada/insights_gen.py", "scada/dk_places.json", "scada/de_places.json", "docs/guide.html"]
    check(11, "the files the program opens at run time exist", all(os.path.isfile(os.path.join(ROOT, p)) for p in need),
          [p for p in need if not os.path.isfile(os.path.join(ROOT, p))])
    std = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()
    local = {os.path.splitext(m)[0] for m in mods} | {"insights_gen"}
    import ast
    third, opt_third = set(), set()
    for p in [os.path.join(ROOT, m) for m in mods] + [os.path.join(SCADA, "insights_gen.py")]:
        with open(p, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        optional = set()                       # imports inside "try: ... except ImportError" may be missing
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and any("ImportError" in ast.dump(h) for h in node.handlers):
                optional |= {id(n) for b in node.body for n in ast.walk(b)}
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
            for mm in names:
                mm = mm.split(".")[0]
                if mm not in std and mm not in local and mm != "__future__":
                    (opt_third if id(node) in optional else third).add(mm)
    pip = re.search(r"pip install[^\n]*", df).group(0) if "pip install" in df else ""
    pipname = {"serial": "pyserial"}
    check(11, f"third-party imports {sorted(third)} are installed in the image",
          all(pipname.get(x, x) in pip for x in third), pip)
    want_opt = sorted(opt_third - {"certifi"})          # certifi: the image's own certificate store is used instead
    check(11, f"optional imports used by the page {want_opt} are installed too (QR code)",
          all(pipname.get(x, x) in pip for x in want_opt), (sorted(opt_third), pip))
    ign = os.path.join(ROOT, ".dockerignore")
    if os.path.isfile(ign):
        with open(ign, encoding="utf-8") as fh:
            ig = fh.read()
        check(11, ".dockerignore does not drop needed files", not any(x in ig for x in ("scada", "docs/guide", "burner_")), ig)
    if shutil.which("docker"):
        r = subprocess.run(["docker", "build", "-t", "pelletburner-release-check", ROOT], capture_output=True, text=True)
        check(11, "docker build", r.returncode == 0, r.stderr[-300:])
    else:
        not_tested(11, "docker build / run", "no Docker on this PC: image build not tested here")


# ============================================================================ item 12: the suite
SUITE_MAP = {"_setup_e2e_test.py": 3, "_settings_e2e_test.py": 3, "_pages_test.py": 2, "_mqtt_test.py": 9,
             "_ntfy_test.py": 7, "_phone_test.py": 8, "_warm_test.py": 4, "_warm_restart_test.py": 4,
             "_warm_restore_test.py": 4, "_coolpred_test.py": 4, "_keep_soon_test.py": 4, "_e2e_test.py": 1,
             "_alarm_at_start_test.py": 6, "_web_routes_test.py": 5, "_sync_test.py": 11, "_env_test.py": 11}


def start_suite(py, work):
    out = open(os.path.join(work, "suite.txt"), "w", encoding="utf-8")
    return subprocess.Popen([py, os.path.join(SCADA, "run_tests.py")], cwd=SCADA, stdout=out, stderr=subprocess.STDOUT,
                            env=dict(os.environ, PYTHONIOENCODING="utf-8")), out


def finish_suite(py, work, proc, out):
    proc.wait()
    out.close()
    with open(os.path.join(work, "suite.txt"), encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    rows = re.findall(r"^(PASS|FAIL|SKIP)\s+(\S+_test\.py)\s+(\d+) s", text, re.M)
    for res, name, secs in rows:
        if res == "FAIL":                               # ran beside the scenario: try it once more on its own
            r = subprocess.run([py, os.path.join(SCADA, name)], cwd=SCADA, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            res = "PASS" if r.returncode == 0 else "FAIL"
            if res == "PASS":
                print(f"note: {name} failed beside the scenario but passed alone", flush=True)
        if name in SUITE_MAP:
            if res == "SKIP":
                not_tested(SUITE_MAP[name], f"suite {name}", "skipped (Playwright missing?)")
            else:
                check(SUITE_MAP[name], f"suite {name}", res == "PASS", "see suite output")
        check(12, f"{name}", res in ("PASS", "SKIP"), "failed")
    m = re.search(r"(\d+) tests in (\d+) s: (\d+) passed, (\d+) failed, (\d+) skipped", text)
    check(12, "the whole suite ran" + (f" ({m.group(0)})" if m else ""), bool(m) and len(rows) > 20, text[-400:])


# ============================================================================ main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--quick", action="store_true", help="skip the full unit suite (item 12)")
    ap.add_argument("--keep", action="store_true", help="keep the work folder (data, logs)")
    a = ap.parse_args()
    py = sys.executable
    t0 = time.time()
    work = tempfile.mkdtemp(prefix="pb-release-check-")
    try:
        import playwright.sync_api  # noqa: F401
        browser_ok = True
    except ImportError:
        browser_ok = False
    print(f"release check: work folder {work}; browser {'yes' if browser_ok else 'NO (Playwright missing)'}", flush=True)
    suite = None if a.quick else start_suite(py, work)
    try:
        scenario(py, os.path.join(work, "scenario"), browser_ok)
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(1, "the scenario ran to the end", False, repr(e)[:300])
    pi_scripts(py, work)
    docker_check()
    if suite:
        finish_suite(py, work, *suite)
    else:
        not_tested(12, "full unit suite", "--quick")

    print("\n" + "=" * 78)
    print(f"{'item':<28} {'result':<11} checks")
    worst_all = "PASS"
    for n, title in ITEMS.items():
        rs = [r for r in RES if r[0] == n]
        fails = [r for r in rs if r[2] == "FAIL"]
        nts = [r for r in rs if r[2] == "NOT TESTED"]
        res = "FAIL" if fails else "PASS" if any(r[2] == "PASS" for r in rs) else "NOT TESTED"
        if res == "FAIL":
            worst_all = "FAIL"
        extra = f"{len(rs) - len(fails) - len(nts)} pass, {len(fails)} fail" + (f", {len(nts)} not tested" if nts else "")
        print(f"{n:>2} {title:<25} {res:<11} {extra}")
        for r in fails:
            print(f"      FAIL {r[1]}  [{r[3][:160]}]")
        for r in nts:
            print(f"      NOT TESTED {r[1]}: {r[3]}")
    print(f"\n{worst_all}  ({time.time() - t0:.0f} s)")
    if a.keep or worst_all == "FAIL":
        print(f"work folder kept: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    sys.exit(1 if worst_all == "FAIL" else 0)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        child(sys.argv[2])
    else:
        main()
