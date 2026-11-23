"""MQTT / Home Assistant test: the real responder program, a simulated boiler (no serial port, no boiler) and a
local MQTT broker on 127.0.0.1. Nothing leaves this PC; every value is made up.

  py scada\\_mqtt_test.py

Broker: amqtt when it is installed (a real broker, e.g. `pip install amqtt`), else the small test broker from
mqtt_check.py. Value templates are rendered with jinja2 when it is installed (like Home Assistant), else the
referenced keys are checked.

Checks: every discovery config is valid JSON with the required keys, unique ids are unique, units / device classes
fit, every state topic has a value, flue only with a flue sensor, Pi health, start/stop only through the web page's
gate (--allow-remote-start, never in ALARM), plan switches, read-only mode, and availability goes offline when the
program is killed (LWT) and when it stops normally.
"""
import datetime, importlib.util, json, os, socket, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ============================================================================ child: the program + a simulated boiler
def child():
    import serial
    from burner_sniff import build_frame, Splitter
    import burner_ext as X
    import burner_features as F
    cmd_log = os.environ["SIM_CMDLOG"]
    flue_hot = os.environ.get("SIM_FLUE_HOT") == "1"
    alarm_at = float(os.environ.get("SIM_ALARM_AT") or 0)
    t0 = time.time()
    # no internet in a test: the outside temperature comes from the cache file; the heating plan never acts
    F.Weather.tick = lambda self: None
    F.WarmMode.tick = lambda self: None
    X.MqttBridge.STATS_EVERY_S, X.MqttBridge.STATE_EVERY_S, X.MqttBridge.PI_EVERY_S = 3, 2, 2
    ram = {"000": 62, "001": 2000, "004": 2000, "005": 40, "006": 6, "010": 150, "031": 100, "033": 0}

    def status():
        el = time.time() - t0
        if alarm_at and el >= alarm_at:
            st, al = 6, 4
        else:
            st, al = (0 if el < 2 else 2 if el < 3 else 3 if el < 4 else 4), 0
        run = st == 4
        return {"000": st, "001": round(48 + min(el, 20) * 0.5, 1), "002": (150 if flue_hot else 31) if run else 24,
                "003": 0.0, "004": 90 if run else 0, "005": 55 if run else 0, "006": st, "007": al, "008": 120,
                "009": 321, "010": 1, "011": 0, "012": 0, "013": 1500 if st in (2, 3, 4) else 0,
                "014": 60 if st in (2, 3, 4) else 0, "015": 1, "016": 1, "017": 1 if run else 0,
                "018": 1 if st == 3 else 0}

    class FakeSerial:
        def __init__(self, *a, **k):
            self.out, self.sp, self.waiting, self.next_t, self.steps, self.dump = bytearray(), Splitter(), False, 0, [], True

        def read(self, n):
            time.sleep(0.005)
            if self.waiting:
                return b""
            if not self.steps:
                if time.time() < self.next_t:
                    return b""
                self.steps = [("w", '{"devicedrive":{"command":"setup", "silent_connect":"0"}}'), ("r", None),
                              ("w", json.dumps({"status_info": status()}, separators=(",", ":"))), ("r", None)]
                if self.dump:
                    self.dump = False
                    self.steps += [("w", json.dumps({"cfg_data_1": ram}, separators=(",", ":"))), ("r", None)]
                self.next_t = time.time() + 0.5
            kind, txt = self.steps.pop(0)
            self.waiting = True
            p = (txt.encode() + b"\x04") if txt is not None else b""
            return build_frame(0x01, 3, 0x20 if kind == "w" else 0x00, 4, p)

        def write(self, b):
            for f in self.sp.feed(b):
                self.waiting = False
                if f.flags & 0x20 or not f.payload:
                    continue
                for msg in f.payload.decode().split("\x04"):
                    try:
                        o = json.loads(msg)
                    except ValueError:
                        continue
                    rm = (o.get("service") or {}).get("run_mode") if isinstance(o, dict) else None
                    if rm:
                        with open(cmd_log, "a") as fh:
                            fh.write(rm + "\n")
                    if isinstance(o, dict) and (o.get("cfg_data_1") or o.get("cfg_chg")):
                        with open(cmd_log, "a") as fh:            # a settings write would be a bug here
                            fh.write("settings-write\n")
            return len(b)

        def flush(self):
            pass

        def close(self):
            pass

    serial.Serial = FakeSerial
    import burner_responder as R
    sys.argv = ["burner_responder.py"] + sys.argv[sys.argv.index("--child") + 1:]
    R.main()


# ============================================================================ parent: broker, data, checks
def make_data(d):
    """Made-up history: 8 earlier days with 8 h of burning, pellets / ash / plan settings, starts, Pi health."""
    os.makedirs(d, exist_ok=True)
    now = time.time()
    today = datetime.date.today()
    for back in range(8, 0, -1):
        day = today - datetime.timedelta(days=back)
        t = datetime.datetime.combine(day, datetime.time(0, 0)).timestamp()
        silo = 140.0
        with open(os.path.join(d, f"hist-{day.isoformat()}.jsonl"), "w", encoding="utf-8") as fh:
            for i in range(1440):
                run = 360 <= i < 840
                if run:
                    silo -= 0.05
                st = 4 if run else 0
                fh.write(json.dumps({"epoch": t + i * 60, "state": st, "boiler_c": 60 if run else 40,
                                     "flue_c": 30 if run else 22, "power_pct": 50 if run else 0, "alarm": 0,
                                     "silo_kg": round(silo, 2), "ignitions": 300 + back, "fan_rpm": 1500 if run else 0,
                                     "fan_pct": 60 if run else 0, "auger_on": 1 if run else 0, "igniter_on": 0,
                                     "din2": 0, "flame": 90 if run else 0, "setpoint": 62, "fuel_gph": 2000}) + "\n")
    with open(os.path.join(d, "ignitions.jsonl"), "w", encoding="utf-8") as fh:
        for k in range(8):
            e = now - (8 - k) * 86400 + 6 * 3600
            fh.write(json.dumps({"epoch": e, "t": "x", "result": "ok", "minutes": 7.0 + k * 0.1, "relight": False,
                                 "batch": "-", "first_lux_s": 300, "flame_s": 300, "peak_lux": 100, "cfg": {}}) + "\n")
    settings = {
        "pellets": {"stock": {"kg": 900, "at": now - 9 * 86400}, "reserve_kg": 150},
        "ash": {"emptied_at": now - 6 * 86400, "bin_kg": 20},
        "vvb": {"enabled": True, "source": "mqtt", "max_age_min": 15, "safe_c": 75},
        "weather": {"enabled": True, "name": "Testtown", "cc": "DK", "lat": 55.0, "lon": 10.0},
        "warm": {"enabled": True, "use_board": False, "periods": [
            {"name": "Morning", "start": "05:30", "end": "06:00", "days": [0, 1, 2, 3, 4, 5, 6], "kind": "start", "on": True}]},
    }
    with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(settings, fh)
    with open(os.path.join(d, "weather_cache.json"), "w", encoding="utf-8") as fh:
        json.dump({"at": now, "c": 3.5, "lat": 55.0, "lon": 10.0, "hourly": []}, fh)
    with open(os.path.join(d, f"pi-health-{today.isoformat()}.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"epoch": now, "t": "x", "uptime_s": 7200, "cpu_c": 41.5, "load1": 0.2,
                             "mem_free_mb": 3000, "disk_free_mb": 20000,
                             "power": {"undervolt_now": False, "undervolt_since_boot": False},
                             "net": {"wifi_signal": 60}, "internet": {"ok": True}, "usb_serial": ["usb-test-adapter"]}) + "\n")


def start_broker(port, tmp):
    if importlib.util.find_spec("amqtt"):
        cfg = os.path.join(tmp, "amqtt.yaml")
        with open(cfg, "w") as fh:
            fh.write(f"listeners:\n  default:\n    type: tcp\n    bind: 127.0.0.1:{port}\n"
                     "plugins:\n  amqtt.plugins.authentication.AnonymousAuthPlugin:\n    allow_anonymous: true\n")
        log = open(os.path.join(tmp, "broker.log"), "w")
        pr = subprocess.Popen([sys.executable, "-m", "amqtt.scripts.broker_script", "-c", cfg], stdout=log, stderr=log)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return "amqtt", pr
            except OSError:
                time.sleep(0.1)
        pr.kill()
        raise SystemExit("amqtt broker did not start")
    import mqtt_check
    mqtt_check.Broker("127.0.0.1", port, verbose=False)
    return "built-in test broker", None


RES = []


def check(name, ok, info=""):
    RES.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{info}]" if info and not ok else ""), flush=True)


def main():
    import burner_ext as X
    import mqtt_check
    tmp = tempfile.mkdtemp(prefix="burner_mqtt_")
    data = os.path.join(tmp, "data")
    make_data(data)
    port = free_port()
    kind, broker = start_broker(port, tmp)
    print(f"broker: {kind} on 127.0.0.1:{port}; jinja2: {'yes' if importlib.util.find_spec('jinja2') else 'no'}")
    seen, lock = {}, threading.RLock()
    msgs = []

    def on_msg(t, p, r):
        with lock:
            seen[t] = p.decode("utf-8", "replace")
            msgs.append((time.time(), t, seen[t]))
    ha = X.MqttClient("127.0.0.1", port, "ha-test", lambda m: None)
    ha.on_message = on_msg
    ha.subscribe("burner/#")
    ha.subscribe("homeassistant/#")
    t0 = time.time()
    while not ha.connected and time.time() - t0 < 10:
        time.sleep(0.1)
    check("test client connected to the broker", ha.connected)
    cmdlog = os.path.join(tmp, "cmds.txt")
    open(cmdlog, "w").close()

    def wait(cond, secs):
        t = time.time()
        while time.time() - t < secs:
            try:
                with lock:
                    if cond():
                        return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def js(topic):
        with lock:
            return json.loads(seen.get(topic) or "null")

    def result_after(t, cmd):
        for ts, top, p in list(msgs):
            if ts >= t and top == "burner/cmd/result":
                o = json.loads(p)
                if o.get("cmd") == cmd:
                    return o
        return None

    def send(sub, payload, cmd):
        t = time.time()
        ha.publish(f"burner/cmd/{sub}", payload)
        wait(lambda: result_after(t, cmd), 10)
        return result_after(t, cmd) or {}

    def spawn(name, extra, env_extra):
        env = dict(os.environ, SIM_CMDLOG=cmdlog, **env_extra)
        out = open(os.path.join(tmp, f"child-{name}.txt"), "w")
        args = [sys.executable, os.path.abspath(__file__), "--child", "--fw-mode", "--live", "--port", "FAKE",
                "--mqtt", f"127.0.0.1:{port}", "--data-dir", data, "--log", os.path.join(tmp, f"resp-{name}.log"),
                "--no-log-frames"] + extra
        return subprocess.Popen(args, env=env, stdout=out, stderr=out)

    def cfg(comp, obj):
        with lock:
            v = seen.get(f"homeassistant/{comp}/burner/{obj}/config")
        return json.loads(v) if v else None

    # ---------------------------------------------------------------- A: default (start/stop not armed)
    print("--- A: default options, killed at the end (LWT)")
    pa = spawn("A", [], {})
    ok = wait(lambda: seen.get("burner/availability") == "online" and json.loads(seen.get("burner/state") or "{}").get("state") == 4
              and json.loads(seen.get("burner/stats") or "{}").get("pellets_left_kg") is not None
              and seen.get("burner/pi") and seen.get("homeassistant/sensor/burner/outside_c/config"), 40)
    check("A program online, state RUNNING, stats + Pi health published", ok)
    time.sleep(2.5)                                     # one more state round
    with lock:
        snap = dict(seen)
    ents, problems, notes = mqtt_check.check_discovery(snap, "homeassistant", "burner")
    check("A every discovery config valid (JSON, keys, units, classes, values)", not problems, "; ".join(problems[:8]))
    uids = [c["unique_id"] for _, _, c in ents]
    check("A unique_ids unique", len(uids) == len(set(uids)))
    check("A all retained discovery under homeassistant/<component>/burner/<object>/config",
          all(t.count("/") == 4 for t in snap if t.startswith("homeassistant/")))
    check("A one device 'Pellet burner', no brand name",
          {c["device"]["name"] for _, _, c in ents} == {"Pellet burner"}
          and {c["device"]["manufacturer"] for _, _, c in ents} == {"Pellet burner monitor project"})
    check("A every entity has availability online/offline", all(c.get("availability_topic") == "burner/availability"
          and c.get("payload_not_available") == "offline" for _, _, c in ents))
    objs = {o for _, o, _ in ents}
    want = {"state_label", "state", "alarm_label", "alarm_code", "alarm", "boiler_c", "outside_c", "setpoint",
            "power_pct", "flame", "fan_pct", "fan_rpm", "auger", "igniter", "silo_kg", "ignitions", "link", "data_age_s",
            "version", "pellets_home", "pellets_days_left", "pellets_per_day", "ash_bin_pct", "ash_kg", "health",
            "health_to_flame", "health_failed", "last_start_min", "last_start_result", "last_start_at", "next_start",
            "pi_cpu_c", "pi_load", "pi_disk_free", "pi_mem_free", "pi_uptime", "pi_wifi", "pi_internet",
            "pi_undervolt", "pi_usb_adapter", "ash_out", "ash_turns_24h", "schedule", "heating_plan", "vvb"}
    check("A all expected entities discovered", want <= objs, f"missing {sorted(want - objs)}")
    check("A no flue entity without a flue sensor", cfg("sensor", "flue_c") is None)
    check("A no start/stop buttons without --allow-remote-start", cfg("button", "start") is None and cfg("button", "stop") is None)
    st, stats, pi = js("burner/state"), js("burner/stats"), js("burner/pi")
    check("A state in words + number", st["state_text"] == "Running" and st["state"] == 4)
    check("A alarm number + plain text", st["alarm"] == 0 and st["alarm_text"] == "No alarm" and st["alarm_active"] is False)
    check("A temperatures, setpoint, power, fan, auger, flame",
          isinstance(st["boiler_c"], (int, float)) and st["outside_c"] == 3.5 and st["setpoint"] == 62
          and st["power_pct"] == 55 and st["fan_rpm"] == 1500 and st["auger_on"] == 1 and st["flame"] == 90)
    check("A data age / link / version", st["connected"] is True and 0 <= st["data_age_s"] <= 10 and st["version"] == X.MONITOR_VERSION)
    check("A pellets left / per day / days left", stats["pellets_left_kg"] < 900 and 15 < (stats["pellets_per_day_kg"] or 0) < 30
          and stats["pellets_days_left"] is not None, str(stats))
    check("A ash bin %, health, last start, next start",
          stats["ash_bin_pct"] is not None and stats["health"] and isinstance(stats["last_start_min"], (int, float))
          and stats["last_start_result"] == "ok" and datetime.datetime.fromisoformat(stats["next_start"]).tzinfo is not None,
          str(stats))
    check("A last start: who started it (stats + attribute of the 'Last start' sensor)",
          stats["last_start_source"] == "panel" and stats["last_start_by"] == "boiler panel (by hand)"   # the fake boiler started by itself
          and "last_start_by" in cfg("sensor", "last_start_at")["json_attributes_template"], str(stats))
    check("A Pi health values", pi["cpu_c"] == 41.5 and pi["uptime_h"] == 2.0 and pi["internet_ok"] is True
          and pi["undervolt"] is False and pi["usb_adapter"] is True)
    numeric = [c for comp, _, c in ents if comp == "sensor" and c.get("state_class")]
    check("A long-term statistics: measurement / total_increasing set", len(numeric) >= 20
          and cfg("sensor", "ignitions")["state_class"] == "total_increasing")
    r = send("run_mode", "start", "start")
    check("A MQTT start refused when --allow-remote-start is off", r.get("ok") is False and "allow-remote-start" in r.get("msg", ""), str(r))
    r = send("heating_plan", "OFF", "heating_plan")
    check("A heating plan switch OFF works", r.get("ok") is True and wait(lambda: json.loads(seen["burner/warm"])["enabled"] is False, 5))
    r = send("heating_plan", "ON", "heating_plan")
    check("A heating plan switch ON works", r.get("ok") is True and wait(lambda: json.loads(seen["burner/warm"])["enabled"] is True, 5))
    r = send("cfg", '{"000": 70}', "cmd/cfg")
    check("A boiler settings cannot be changed over MQTT (cmd/cfg refused)", r.get("ok") is False, str(r))
    ha.publish("burner/cmd/vvb", "52.5")
    check("A hot-water tank temperature from HA (cmd/vvb) is taken",
          wait(lambda: json.loads(seen.get("burner/vvb") or "{}").get("sent_c") == 52.5, 10), seen.get("burner/vvb"))
    r = send("vvb", "hot", "vvb")
    check("A a bad tank temperature is refused", r.get("ok") is False, str(r))
    pr = subprocess.run([sys.executable, os.path.join(HERE, "mqtt_check.py"), "--host", f"127.0.0.1:{port}", "--seconds", "4"],
                        capture_output=True, text=True)
    check("A mqtt_check.py finds the program and no discovery problem", pr.returncode == 0 and "discovery check   : OK" in pr.stdout,
          pr.stdout[-500:])
    pa.kill()
    pa.wait()
    check("A availability -> offline when the program is killed (LWT)", wait(lambda: seen.get("burner/availability") == "offline", 15))
    check("A no command reached the boiler", open(cmdlog).read() == "")

    # ---------------------------------------------------------------- B: --allow-remote-start, flue sensor, alarm
    print("--- B: --allow-remote-start, flue sensor fitted, alarm later, normal stop at the end")
    t = time.time()
    ha.publish("burner/cmd/run_mode", "start", retain=True)      # a stale command left on the broker while offline
    time.sleep(0.5)
    pb = spawn("B", ["--allow-remote-start", "--duration", "40"], {"SIM_FLUE_HOT": "1", "SIM_ALARM_AT": "16"})
    check("B program online again", wait(lambda: seen.get("burner/availability") == "online", 20))
    check("B a retained (old) start command is ignored on connect",
          wait(lambda: "RETAINED" in ((result_after(t, "cmd/run_mode") or {}).get("msg") or ""), 15))
    ha.publish("burner/cmd/run_mode", b"", retain=True)
    time.sleep(1.0)
    check("B ... and not sent to the boiler", open(cmdlog).read() == "")
    check("B start/stop buttons discovered", wait(lambda: cfg("button", "start") and cfg("button", "stop"), 20))
    check("B flue entity appears once a real flue sensor is seen",
          wait(lambda: cfg("sensor", "flue_c") and json.loads(seen["burner/state"]).get("flue_c") == 150, 20))
    r = send("run_mode", "start", "start")
    check("B MQTT start accepted (same gate as the web page)", r.get("ok") is True, str(r))
    check("B ... and delivered to the (simulated) boiler", wait(lambda: "start" in open(cmdlog).read(), 10))
    check("B boiler goes to ALARM", wait(lambda: json.loads(seen["burner/state"]).get("state") == 6, 25))
    st = js("burner/state")
    check("B alarm number + text + problem flag", st["alarm"] == 4 and st["alarm_text"] == "Start-up failed" and st["alarm_active"] is True)
    time.sleep(1.0)
    n = open(cmdlog).read().count("\n")
    r = send("run_mode", "stop", "stop")
    check("B MQTT stop refused in ALARM (no alarm reset from MQTT)", r.get("ok") is False and "ALARM" in r.get("msg", ""), str(r))
    r = send("run_mode", "start", "start")
    check("B MQTT start refused in ALARM", r.get("ok") is False and "ALARM" in r.get("msg", ""), str(r))
    time.sleep(1.5)
    check("B ... and nothing sent to the boiler", open(cmdlog).read().count("\n") == n)
    with lock:
        snap = dict(seen)
    ents, problems, _ = mqtt_check.check_discovery(snap, "homeassistant", "burner")
    check("B discovery still valid (buttons, flue)", not problems, "; ".join(problems[:8]))
    nB = len(ents)
    check("B normal stop -> availability offline", wait(lambda: pb.poll() is not None, 40)
          and wait(lambda: seen.get("burner/availability") == "offline", 10))

    # ---------------------------------------------------------------- C: --mqtt-read-only
    print("--- C: --mqtt-read-only")
    pc = spawn("C", ["--allow-remote-start", "--mqtt-read-only"], {})
    check("C program online", wait(lambda: seen.get("burner/availability") == "online", 20))
    check("C no buttons / switches, plan shown read-only",
          wait(lambda: cfg("binary_sensor", "heating_plan_on") and not cfg("button", "start")
               and not cfg("switch", "heating_plan") and not cfg("switch", "schedule"), 15))
    r = send("heating_plan", "OFF", "cmd/heating_plan")
    check("C commands refused when read-only", r.get("ok") is False and "read-only" in r.get("msg", ""), str(r))
    r = send("run_mode", "start", "cmd/run_mode")
    check("C start refused when read-only", r.get("ok") is False)
    ha.publish("burner/cmd/vvb", "47")
    check("C tank temperature still taken when read-only (a reading, not a command)",
          wait(lambda: json.loads(seen.get("burner/vvb") or "{}").get("sent_c") == 47.0, 10))
    pc.kill()
    pc.wait()
    check("C killed -> offline", wait(lambda: seen.get("burner/availability") == "offline", 15))
    if broker:
        broker.kill()
    failed = [n for n, ok in RES if not ok]
    print(f"\nentities with start/stop armed: {nB}")
    print(f"FAILED: {len(failed)}" + (f"  {failed}" if failed else ""))
    print("tmp:", tmp)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    if "--child" in sys.argv:
        child()
    else:
        main()
