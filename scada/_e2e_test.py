"""End-to-end test of burner_responder.main() with a simulated controller (fake serial port),
a mini MQTT broker, settings sync, transitions/pre-alarm, persistence and the HTTP feed."""
import sys, os, json, time, threading, socket, struct, tempfile, urllib.request, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import serial
from burner_sniff import build_frame, Splitter
import burner_ext as X

TMP = tempfile.mkdtemp(prefix="burner_e2e_")

def free_port():
    # a fixed port could already be used by another run (another worktree, a replay): on Windows both programs
    # then listen and the answers come from the wrong one (that was the KeyError 'cfg_sync')
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

HTTP_PORT, MQTT_PORT = free_port(), free_port()
FAST = float(os.environ.get("CYCLE", "0.5"))

# ------------------------------------------------------------------ mini MQTT broker
class Broker:
    def __init__(self, port):
        self.retained, self.clients, self.lock = {}, [], threading.Lock()
        self.srv = socket.socket()          # no SO_REUSEADDR: on Windows it would share a busy port
        self.srv.bind(("127.0.0.1", port)); self.srv.listen(5)
        threading.Thread(target=self._accept, daemon=True).start()

    @staticmethod
    def match(flt, topic):
        f, t = flt.split("/"), topic.split("/")
        for i, p in enumerate(f):
            if p == "#": return True
            if i >= len(t) or (p != "+" and p != t[i]): return False
        return len(f) == len(t)

    def _accept(self):
        while True:
            c, _ = self.srv.accept()
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _rx(self, c, n):
        b = b""
        while len(b) < n:
            x = c.recv(n - len(b))
            if not x: raise OSError
            b += x
        return b

    def _send(self, c, hdr, body):
        c.sendall(bytes([hdr]) + X.MqttClient._len(len(body)) + body)

    def _client(self, c):
        me = {"sock": c, "subs": []}
        try:
            while True:
                h = self._rx(c, 1)[0]; mult = 1; n = 0
                while True:
                    d = self._rx(c, 1)[0]; n += (d & 127) * mult
                    if not d & 128: break
                    mult *= 128
                b = self._rx(c, n) if n else b""
                t = h >> 4
                if t == 1:
                    self._send(c, 0x20, b"\x00\x00")
                    with self.lock: self.clients.append(me)
                elif t == 8:
                    pid = b[:2]; i = 2; fl = []
                    while i < len(b):
                        L = struct.unpack(">H", b[i:i+2])[0]; fl.append(b[i+2:i+2+L].decode()); i += 3 + L
                    me["subs"] += fl
                    self._send(c, 0x90, pid + b"\x00" * len(fl))
                    for top, pl in list(self.retained.items()):
                        if any(self.match(f, top) for f in fl):
                            self._send(c, 0x31, X.MqttClient._str(top) + pl)
                elif t == 3:
                    L = struct.unpack(">H", b[:2])[0]; top = b[2:2+L].decode(); pl = b[2+L:]
                    if h & 1:
                        if pl: self.retained[top] = pl
                        else: self.retained.pop(top, None)
                    with self.lock: cl = list(self.clients)
                    for o in cl:
                        if any(self.match(f, top) for f in o["subs"]):
                            try: self._send(o["sock"], 0x30, X.MqttClient._str(top) + pl)
                            except OSError: pass
                elif t == 12:
                    self._send(c, 0xD0, b"")
        except OSError:
            with self.lock:
                if me in self.clients: self.clients.remove(me)

# ------------------------------------------------------------------ simulated controller
SAVED = {"000": 60, "001": 2400, "002": 45, "003": 10, "004": 2000, "005": 30, "006": 3, "007": 0, "008": 0,
         "009": 1500, "010": 500, "011": 1, "012": 60, "013": 5, "014": 40, "015": 80, "016": 80, "017": 800,
         "018": 20, "019": 3, "020": 35, "031": 100, "032": 80, "033": 0, "034": 25, "035": 5.0, "036": 0.005, "037": 0.0}

class Ctl:
    def __init__(self):
        self.ram = dict(SAVED); self.state = 0; self.alarm = 0; self.t_state = time.time()
        self.silent_until = 0; self.want_dump = True; self.log = []; self.writes = []
        self.force_alarm_at = None
CTL = Ctl()

class FakeSerial:
    def __init__(self, port, *a, **k):
        self.out = bytearray(); self.sp = Splitter(); self.waiting = False; self.next_t = 0; self.steps = []

    def _frame(self, reg, write, txt=None):
        p = (txt.encode() + b"\x04") if txt is not None else b""
        return build_frame(0x01, reg, 0x20 if write else 0x00, 4, p)

    def _plan(self):
        c = CTL
        # state progression for the simulation
        el = time.time() - c.t_state
        if c.state == 2 and el > 2 * FAST: c.state, c.t_state = 3, time.time()
        if c.force_alarm_at and time.time() > c.force_alarm_at:
            c.state, c.alarm, c.t_state, c.force_alarm_at = 6, 4, time.time(), None
        st = {"000": c.state, "001": 55.0, "002": 80, "003": 0.0, "004": 0, "005": 30 if c.state in (3, 4) else 0,
              "006": 0, "007": c.alarm, "008": 300, "009": 1000, "010": 1, "011": 0, "012": 1, "013": 900,
              "014": 33, "015": 1, "016": 1, "017": 0, "018": 1 if c.state == 3 else 0}
        steps = [("w", '{"devicedrive":{"command":"setup", "silent_connect":"0"}}'), ("r", "keepalive"),
                 ("w", json.dumps({"status_info": st}, separators=(",", ":"))), ("r", "data")]
        if c.want_dump:
            c.want_dump = False
            steps += [("w", "DUMP"), ("r", "data")]      # built when sent, like the real controller
        return steps

    def read(self, n):
        if self.out:
            b = bytes(self.out); self.out.clear(); return b
        time.sleep(0.005)
        if time.time() < CTL.silent_until or self.waiting:
            return b""
        if not self.steps:
            if time.time() < self.next_t: return b""
            self.steps = self._plan(); self.next_t = time.time() + FAST
        kind, txt = self.steps.pop(0)
        if txt == "DUMP":
            txt = json.dumps({"cfg_data_1": CTL.ram}, separators=(",", ":"))
        self.cur = (kind, txt)
        self.waiting = True
        return self._frame(3, kind == "w", txt if kind == "w" else None)

    def write(self, b):
        for f in self.sp.feed(b):
            self.waiting = False
            if f.flags & 0x20 or not f.payload:
                continue                                   # write ack
            for msg in f.payload.decode().split("\x04"):
                if not msg.strip(): continue
                try: o = json.loads(msg)
                except ValueError: continue
                svc = o.get("service", {})
                rm = svc.get("run_mode")
                if rm == "start" and CTL.state in (0, 6):
                    CTL.log.append(("start", CTL.state))
                    CTL.state, CTL.alarm, CTL.t_state = (0, 0, time.time()) if CTL.state == 6 else (2, 0, time.time())
                elif rm == "stop" and CTL.state != 0:
                    CTL.log.append(("stop", CTL.state)); CTL.state, CTL.alarm, CTL.t_state = 0, 0, time.time()
                if o.get("cfg_data") is True:
                    CTL.want_dump = True
                if isinstance(o.get("cfg_data_1"), dict):
                    CTL.writes.append(dict(o["cfg_data_1"])); CTL.ram.update(o["cfg_data_1"])
                    assert len(msg) < 110, f"reply too long for the controller: {len(msg)} B"
        return len(b)

    def flush(self): pass
    def close(self): pass

serial.Serial = FakeSerial

# ------------------------------------------------------------------ run
def main():
    broker = Broker(MQTT_PORT)
    desired = os.path.join(TMP, "desired.json")
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_cfg_example.json"), desired)
    want = json.load(open(desired))
    want["cfg_data_1"]["033"] = 1        # check-only key that differs from the board (0): must be reported, never written
    json.dump(want, open(desired, "w"))
    sys.argv = ["burner_responder.py", "--fw-mode", "--live", "--allow-remote-start", "--live-interval", "10",
                "--serve", str(HTTP_PORT), "--mqtt", f"127.0.0.1:{MQTT_PORT}", "--cfg-sync", desired,
                "--data-dir", os.path.join(TMP, "data"), "--log", os.path.join(TMP, "resp.log"), "--port", "FAKE",
                "--remote-cmd", os.path.join(TMP, "remote_cmd.txt")]
    import burner_responder as R
    got = []
    ha = X.MqttClient("127.0.0.1", MQTT_PORT, "ha-test", lambda m: None)
    ha.on_message = lambda t, p, r: got.append((t, p.decode("utf-8", "replace"), r))
    ha.subscribe("burner/#"); ha.subscribe("homeassistant/#")
    time.sleep(1.5)
    ha.publish("burner/cmd/run_mode", "start", retain=True)   # stale retained command BEFORE the responder connects
    time.sleep(0.5)
    sys.stdout = open(os.path.join(TMP, "stdout.txt"), "w", encoding="utf-8")
    threading.Thread(target=R.main, daemon=True).start()
    res = {}
    def status():
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/status.json").read())
    def wait(cond, secs, what):
        t0 = time.time()
        while time.time() - t0 < secs:
            try:
                if cond(): res[what] = "PASS"; return True
            except Exception: pass
            time.sleep(0.2)
        res[what] = "FAIL"; return False

    wait(lambda: status()["cfg_sync"]["status"] == "in_sync", 30, "1 cfg-sync pushes desired values and verifies")
    res["   writes"] = CTL.writes
    res["1b 033 (check_only) reported, never written"] = "PASS" if (
        CTL.ram["033"] == 0 and not any("033" in w for w in CTL.writes)
        and status()["cfg_sync"]["check_only_diff"].get("033") == [0, 1]) else "FAIL"
    wait(lambda: any(t.startswith("homeassistant/button/burner/start") for t, p, r in got), 10, "2 HA discovery incl. buttons")
    wait(lambda: any(t == "burner/state" for t, p, r in got), 10, "3 state published")
    # the stale retained 'start' delivered on connect must have been ignored
    res["4 stale retained command ignored on connect"] = "PASS" if (CTL.state == 0 and not CTL.log and
        any("RETAINED" in p for t, p, r in got)) else "FAIL"
    ha.publish("burner/cmd/run_mode", b"", retain=True)        # clear it
    res["   initial writes (no duplicates)"] = len(CTL.writes)
    ha.publish("burner/cmd/run_mode", "start")
    wait(lambda: CTL.state == 3, 20, "5 MQTT start -> STOPPET->OPSTART->IGNITION")
    CTL.force_alarm_at = time.time() + 1
    wait(lambda: status().get("prealarm") and status()["prealarm"]["alarm"] == 4, 20, "6 pre-alarm snapshot on alarm 4")
    s = status()
    res["   prealarm state_before"] = s["prealarm"]["state_before_label"]
    res["   transitions"] = [(x["from_label"], x["to_label"]) for x in s["transitions"]]
    res["   prev_state"] = s["prev_state"]
    ha.publish("burner/cmd/run_mode", "stop")
    wait(lambda: any(t == "burner/cmd/result" and "ALARM" in p and '"ok": false' in p.replace('":false', '": false')
                     for t, p, r in got), 10, "7a MQTT stop in ALARM refused (no alarm reset from MQTT)")
    res["7a2 still in alarm"] = "PASS" if CTL.state == 6 else "FAIL"
    req = urllib.request.Request(f"http://127.0.0.1:{HTTP_PORT}/api/run_mode", data=b'{"cmd":"stop"}', method="POST",
                                 headers={"X-Burner": "1", "Content-Type": "application/json"})
    try:                                                          # in ALARM: refused without the page's confirm
        urllib.request.urlopen(req).read(); code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    res["7b web stop in ALARM without reset_alarm refused"] = "PASS" if code == 409 and CTL.state == 6 else "FAIL"
    req = urllib.request.Request(f"http://127.0.0.1:{HTTP_PORT}/api/run_mode", method="POST",
                                 data=b'{"cmd":"stop","reset_alarm":true}',
                                 headers={"X-Burner": "1", "Content-Type": "application/json"})
    urllib.request.urlopen(req).read()
    wait(lambda: CTL.state == 0 and status()["latest"]["fields"]["state"] == 0, 20, "7 web stop clears alarm")
    # power cycle: silent > 90 s, board RAM back to saved values
    CTL.ram = dict(SAVED); CTL.silent_until = time.time() + 95
    nw = len(CTL.writes)
    wait(lambda: len(CTL.writes) > nw and status()["cfg_sync"]["status"] == "in_sync", 140, "8 re-sync after power cycle")
    before = (CTL.ram["000"], json.load(open(desired))["cfg_data_1"]["000"])
    ha.publish("burner/cmd/cfg", '{"000": 66}')            # removed topic: boiler settings never change over MQTT
    wait(lambda: any(t == "burner/cmd/result" and "unknown command" in p for t, p, r in got), 10,
         "9 MQTT cfg change refused (no settings over MQTT)")
    time.sleep(3)
    res["9 board + file unchanged"] = "PASS" if (CTL.ram["000"], json.load(open(desired))["cfg_data_1"]["000"]) == before else "FAIL"
    # Home Assistant extras: schedule switch, new entities, the checker tool
    wait(lambda: any(t == "homeassistant/switch/burner/schedule/config" for t, p, r in got), 10, "9a HA schedule switch discovered")
    ha.publish("burner/cmd/schedule", "ON")
    wait(lambda: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/api/settings").read())
         ["settings"]["schedule"]["enabled"] is True, 10, "9b MQTT schedule ON -> setting saved")
    wait(lambda: any(t == "burner/schedule" and '"enabled": true' in p.replace('":true', '": true') for t, p, r in got),
         15, "9c burner/schedule state published")
    ha.publish("burner/cmd/schedule", "OFF")
    import subprocess
    pr = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mqtt_check.py"),
                         "--host", f"127.0.0.1:{MQTT_PORT}", "--seconds", "3"], capture_output=True, text=True)
    res["9d mqtt_check.py sees the responder"] = "PASS" if pr.returncode == 0 and "state" in pr.stdout else "FAIL\n" + pr.stdout[-600:]
    d = os.path.join(TMP, "data")
    res["10 data files"] = sorted(os.listdir(d))
    h = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/history.json?h=1").read())
    res["11 /history.json rows"] = len(h["rows"])
    st = X.Store(d, 30, lambda m: None)
    res["12 reload rows/events"] = (len(st.load_rows(720)), len(st.load_events(300)))
    res["13 board 033 never written in the whole run"] = "PASS" if (
        CTL.ram["033"] == 0 and not any("033" in w for w in CTL.writes)) else "FAIL"
    sys.stdout = sys.__stdout__
    for k, v in res.items():
        print(k, ":", v)
    print("tmp:", TMP)
    bad = [k for k, v in res.items() if isinstance(v, str) and v.startswith("FAIL")]
    for k in bad:                                   # run_tests.py shows lines that start with FAIL
        print("FAIL", k)
    return 1 if bad else 0

sys.exit(main())
