#!/usr/bin/env python3
"""Check the MQTT side of the Pellet burner responder (stdlib only, no installs).

  Listen to a broker (e.g. Home Assistant's Mosquitto) and report what the responder publishes:
      py scada\\mqtt_check.py --host homeassistant.local --user burner --pass secret
      py scada\\mqtt_check.py --host homeassistant.local --discovery        # also show HA discovery configs
  Send a test command (only acted on when the responder runs with --allow-remote-start):
      py scada\\mqtt_check.py --host homeassistant.local --send stop
  No Home Assistant? Run a tiny local test broker, then start the responder with --mqtt 127.0.0.1:1883:
      py scada\\mqtt_check.py --broker            (Ctrl-C to stop)

  Use the same .env file as docker compose (BURNER_MQTT, BURNER_MQTT_USER, BURNER_MQTT_PASS):
      py scada\\mqtt_check.py --env .env
  The summary checks every Home Assistant discovery config (valid JSON, unique ids, units / device classes,
  a value on every state topic, availability). --discovery also prints the configs.

Exit code 0 = connected, the responder's state was seen and the discovery check found no problem; 1 = not.
"""
import argparse, json, os, socket, struct, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_ext as X


# ------------------------------------------------------------------ tiny test broker (QoS 0, retained, wildcards)
class Broker:
    def __init__(self, host, port, verbose=True):
        self.retained, self.clients, self.lock, self.verbose = {}, [], threading.Lock(), verbose
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    @staticmethod
    def match(flt, topic):
        f, t = flt.split("/"), topic.split("/")
        for i, p in enumerate(f):
            if p == "#":
                return True
            if i >= len(t) or (p != "+" and p != t[i]):
                return False
        return len(f) == len(t)

    def _accept(self):
        while True:
            c, addr = self.srv.accept()
            threading.Thread(target=self._client, args=(c, addr), daemon=True).start()

    @staticmethod
    def _rx(c, n):
        b = b""
        while len(b) < n:
            x = c.recv(n - len(b))
            if not x:
                raise OSError("closed")
            b += x
        return b

    @staticmethod
    def _send(c, hdr, body):
        c.sendall(bytes([hdr]) + X.MqttClient._len(len(body)) + body)

    def _client(self, c, addr):
        me = {"sock": c, "subs": [], "id": "?", "will": None}
        clean = False
        try:
            while True:
                h = self._rx(c, 1)[0]
                mult, n = 1, 0
                while True:
                    d = self._rx(c, 1)[0]
                    n += (d & 127) * mult
                    if not d & 128:
                        break
                    mult *= 128
                b = self._rx(c, n) if n else b""
                t = h >> 4
                if t == 1:                                           # CONNECT
                    pl = struct.unpack(">H", b[:2])[0]
                    i = 2 + pl + 1
                    flags = b[i]; i += 3
                    L = struct.unpack(">H", b[i:i + 2])[0]; me["id"] = b[i + 2:i + 2 + L].decode(); i += 2 + L
                    if flags & 0x04:
                        L = struct.unpack(">H", b[i:i + 2])[0]; wt = b[i + 2:i + 2 + L].decode(); i += 2 + L
                        L = struct.unpack(">H", b[i:i + 2])[0]; wm = b[i + 2:i + 2 + L]; i += 2 + L
                        me["will"] = (wt, wm, bool(flags & 0x20))
                    self._send(c, 0x20, b"\x00\x00")
                    with self.lock:
                        self.clients.append(me)
                    if self.verbose:
                        print(f"[broker] client '{me['id']}' connected from {addr[0]}")
                elif t == 8:                                         # SUBSCRIBE
                    pid, i, fl = b[:2], 2, []
                    while i < len(b):
                        L = struct.unpack(">H", b[i:i + 2])[0]; fl.append(b[i + 2:i + 2 + L].decode()); i += 3 + L
                    me["subs"] += fl
                    self._send(c, 0x90, pid + b"\x00" * len(fl))
                    for top, p in list(self.retained.items()):
                        if any(self.match(f, top) for f in fl):
                            self._send(c, 0x31, X.MqttClient._str(top) + p)
                elif t == 3:                                         # PUBLISH
                    L = struct.unpack(">H", b[:2])[0]; top = b[2:2 + L].decode(); p = b[2 + L:]
                    self._route(top, p, bool(h & 1))
                elif t == 12:
                    self._send(c, 0xD0, b"")
                elif t == 14:
                    clean = True
                    break
        except OSError:
            pass
        with self.lock:
            if me in self.clients:
                self.clients.remove(me)
        if me["will"] and not clean:
            self._route(*me["will"])
        if self.verbose:
            print(f"[broker] client '{me['id']}' disconnected")

    def _route(self, top, p, retain):
        if retain:
            if p:
                self.retained[top] = p
            else:
                self.retained.pop(top, None)
        with self.lock:
            cl = list(self.clients)
        for o in cl:
            if any(self.match(f, top) for f in o["subs"]):
                try:
                    self._send(o["sock"], 0x30, X.MqttClient._str(top) + p)
                except OSError:
                    pass


def run_broker(a):
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    Broker(host, a.port)
    print(f"Test broker on {host}:{a.port}. Start the responder with --mqtt 127.0.0.1:{a.port}, "
          f"then in another window: py scada\\mqtt_check.py --host 127.0.0.1 --port {a.port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------------ Home Assistant discovery check
COMPONENTS = {"sensor", "binary_sensor", "switch", "button"}
DEVICE_CLASSES = {
    "sensor": {"temperature", "weight", "duration", "timestamp", "enum", "data_size", "power_factor", "humidity",
               "signal_strength", "power", "energy", "voltage", "current", "battery", "frequency"},
    "binary_sensor": {"problem", "connectivity", "running", "heat", "plug", "power", "safety", "moving", "opening"},
    "switch": {"switch", "outlet"}, "button": {"restart", "update", "identify"}}
UNITS = {"temperature": {"°C", "°F", "K"}, "weight": {"kg", "g", "lb"}, "duration": {"s", "min", "h", "d", "ms"},
         "data_size": {"B", "kB", "MB", "GB", "KiB", "MiB", "GiB"}, "timestamp": {None}, "enum": {None}}
STATE_CLASSES = {"measurement", "total", "total_increasing"}


def _render(tpl, payload_text):
    """Render a value template like Home Assistant does. Returns (text, how): how = 'jinja' or 'keys'."""
    try:
        obj = json.loads(payload_text)
    except ValueError:
        obj = None
    try:
        import jinja2                                    # optional: exact rendering when it is installed
        return jinja2.Environment().from_string(tpl).render(value_json=obj, value=payload_text).strip(), "jinja"
    except ImportError:
        pass
    import re                                            # no jinja2: check that the referenced keys exist
    missing = [k for k in re.findall(r"value_json\.(\w+)", tpl) if not (isinstance(obj, dict) and k in obj)]
    if missing:
        return "", "keys"
    m = re.fullmatch(r"\{\{\s*value_json\.(\w+)\s*\}\}", tpl)
    return (str(obj[m.group(1)]) if m else "?"), "keys"


def check_discovery(seen, disc="homeassistant", prefix="burner"):
    """Check the Home Assistant discovery configs in `seen` ({topic: payload text}).
    Returns (entities, problems, notes): entities = [(component, object, config)], problems = list of strings."""
    problems, notes, ents, uids = [], [], [], {}
    for topic in sorted(t for t in seen if t.startswith(disc + "/") and t.endswith("/config")):
        txt = seen[topic]
        if not txt:
            continue                                         # an empty retained config = entity removed
        parts = topic.split("/")
        if len(parts) != 5 or parts[1] not in COMPONENTS:
            problems.append(f"{topic}: topic is not <prefix>/<component>/<node>/<object>/config")
            continue
        comp, obj = parts[1], parts[3]
        try:
            c = json.loads(txt)
            assert isinstance(c, dict)
        except (ValueError, AssertionError):
            problems.append(f"{topic}: config is not a JSON object")
            continue
        ents.append((comp, obj, c))
        need = ["name", "unique_id", "device", "availability_topic"]
        need += ["command_topic"] if comp in ("button", "switch") else []
        need += ["state_topic", "value_template"] if comp != "button" else []
        for k in need:
            if not c.get(k):
                problems.append(f"{obj}: missing {k}")
        if not (c.get("device") or {}).get("identifiers"):
            problems.append(f"{obj}: device has no identifiers")
        uid = c.get("unique_id")
        if uid in uids:
            problems.append(f"{obj}: unique_id {uid} also used by {uids[uid]}")
        uids[uid] = obj
        dc, unit, sc = c.get("device_class"), c.get("unit_of_measurement"), c.get("state_class")
        if dc and dc not in DEVICE_CLASSES.get(comp, set()):
            problems.append(f"{obj}: device_class {dc} is not a {comp} class")
        if dc in UNITS and unit not in UNITS[dc]:
            problems.append(f"{obj}: unit {unit!r} does not fit device_class {dc}")
        if sc and sc not in STATE_CLASSES:
            problems.append(f"{obj}: state_class {sc} unknown")
        if dc in ("timestamp", "enum") and (sc or unit):
            problems.append(f"{obj}: a {dc} sensor must not have a unit or state_class")
        if dc == "enum" and not c.get("options"):
            problems.append(f"{obj}: enum sensor without options")
        if comp == "binary_sensor" and (unit or sc):
            problems.append(f"{obj}: a binary sensor has no unit / state_class")
        av = seen.get(c.get("availability_topic") or "")
        if av not in ("online", "offline"):
            problems.append(f"{obj}: availability topic has no online/offline value")
        st = c.get("state_topic")
        if comp == "button":
            continue
        if not seen.get(st):
            problems.append(f"{obj}: nothing published on its state topic {st}")
            continue
        val, how = _render(c.get("value_template", ""), seen[st])
        if val == "":
            problems.append(f"{obj}: value template gives nothing (missing key in {st}?)")
            continue
        if val in ("None", "?"):
            notes.append(f"{obj}: no value yet")
            continue
        if comp == "binary_sensor" and how == "jinja" and val not in ("ON", "OFF"):
            problems.append(f"{obj}: binary sensor value {val!r} is not ON/OFF")
        if comp == "sensor" and (unit or sc) and dc != "timestamp":
            try:
                float(val)
            except ValueError:
                problems.append(f"{obj}: numeric sensor got {val!r}")
        if dc == "timestamp" and how == "jinja":
            import datetime
            try:
                if datetime.datetime.fromisoformat(val).tzinfo is None:
                    problems.append(f"{obj}: timestamp without time zone {val!r}")
            except ValueError:
                problems.append(f"{obj}: not an ISO timestamp {val!r}")
        if dc == "enum" and how == "jinja" and val not in (c.get("options") or []):
            problems.append(f"{obj}: {val!r} is not one of its options")
    return ents, problems, notes


# ------------------------------------------------------------------ checker
def run_check(a):
    host, _, port = a.host.partition(":")
    port = int(port or a.port)
    logs = []
    pw = a.password if a.password is not None else os.environ.get("BURNER_MQTT_PASS")
    c = X.MqttClient(host, port, f"burner-check-{os.getpid()}"[:23], logs.append, user=a.user, password=pw)
    got, seen = [], {}
    def on_msg(topic, payload, retained):
        txt = payload.decode("utf-8", "replace")
        got.append((time.time(), topic, txt, retained))
        seen[topic] = txt
        if a.discovery or not topic.startswith(a.discovery_prefix + "/"):
            short = txt if len(txt) < 160 else txt[:157] + "..."
            print(f"{time.strftime('%H:%M:%S')}  {'R ' if retained else '  '}{topic}  {short}")
    c.on_message = on_msg
    c.subscribe(f"{a.prefix}/#")
    node = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in a.prefix) or "burner"
    c.subscribe(f"{a.discovery_prefix}/+/{node}/#")
    t0 = time.time()
    while not c.connected and time.time() - t0 < 10:
        time.sleep(0.2)
    if not c.connected:
        print(f"NOT CONNECTED to {host}:{port}: {logs[-1] if logs else 'no answer'}")
        print("Check: broker address/port, firewall, user/password (Mosquitto add-on: create an HA user for it).")
        return 1
    print(f"connected to {host}:{port} - listening on {a.prefix}/# for {a.seconds} s (R = retained)")
    if a.send:
        time.sleep(1.0)
        if a.send.startswith(("schedule-", "heating-plan-")):
            sub = "schedule" if a.send.startswith("schedule") else "heating_plan"
            c.publish(f"{a.prefix}/cmd/{sub}", "ON" if a.send.endswith("-on") else "OFF")
        else:
            c.publish(f"{a.prefix}/cmd/run_mode", a.send)
        print(f"sent {a.send!r}; waiting for {a.prefix}/cmd/result ...")
    try:
        while time.time() - t0 < a.seconds:
            time.sleep(0.5)
            if a.send and f"{a.prefix}/cmd/result" in seen and time.time() - t0 > 3:
                break
    except KeyboardInterrupt:
        pass
    # ---- summary ----
    print("\n===== summary =====")
    av = seen.get(f"{a.prefix}/availability")
    print(f"availability      : {av or 'not seen (responder not connected to this broker?)'}")
    st = seen.get(f"{a.prefix}/state")
    if st:
        try:
            s = json.loads(st)
            print(f"state             : {s.get('state_label')}  boiler {s.get('boiler_c')} C  power {s.get('power_pct')} %"
                  f"  alarm {s.get('alarm_label')}  link {'ok' if s.get('connected') else 'NO DATA FROM BOILER'}"
                  f"  remote armed: {s.get('remote_armed')}")
        except ValueError:
            print("state             : (not JSON?)", st[:80])
    else:
        print("state             : not seen")
    ents, problems, notes = check_discovery(seen, a.discovery_prefix, a.prefix)
    kinds = {}
    for comp, _, _ in ents:
        kinds[comp] = kinds.get(comp, 0) + 1
    print(f"HA discovery      : {len(ents)} entities " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
          + (" (start/stop buttons present)" if kinds.get("button") else ""))
    if ents:
        print(f"discovery check   : {'OK' if not problems else str(len(problems)) + ' problem(s)'}"
              + (f", {len(notes)} without a value yet" if notes else ""))
        for x in problems[:40]:
            print(f"   PROBLEM {x}")
        if a.discovery:
            for x in notes:
                print(f"   note    {x}")
    pi = seen.get(f"{a.prefix}/pi")
    if pi:
        print(f"pi                : {pi[:110]}")
    for k in ("cfg_sync", "schedule", "prealarm"):
        v = seen.get(f"{a.prefix}/{k}")
        if v:
            print(f"{k:<18}: {v[:110]}")
    res = seen.get(f"{a.prefix}/cmd/result")
    if res:
        print(f"last cmd result   : {res}")
    return 0 if st and not problems else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", help="broker HOST[:PORT] to check")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--user")
    ap.add_argument("--pass", dest="password", help="or env BURNER_MQTT_PASS")
    ap.add_argument("--prefix", default="burner")
    ap.add_argument("--discovery-prefix", default="homeassistant")
    ap.add_argument("--discovery", action="store_true", help="also print the HA discovery configs")
    ap.add_argument("--seconds", type=float, default=40, help="how long to listen (default 40)")
    ap.add_argument("--send", choices=["start", "stop", "schedule-on", "schedule-off", "heating-plan-on",
                                       "heating-plan-off"],
                    help="publish a command (start/stop only act with --allow-remote-start, never in ALARM)")
    ap.add_argument("--env", metavar="FILE", help="read BURNER_MQTT / BURNER_MQTT_USER / BURNER_MQTT_PASS from "
                                                   "this .env file (the one docker compose uses)")
    ap.add_argument("--broker", action="store_true", help="run a local test broker instead")
    ap.add_argument("--lan", action="store_true", help="with --broker: listen on all interfaces (default: this PC only)")
    a = ap.parse_args()
    if a.broker:
        return run_broker(a)
    if a.env:
        try:
            with open(a.env, encoding="utf-8-sig") as fh:
                kv = dict(l.strip().split("=", 1) for l in fh if "=" in l and not l.strip().startswith("#"))
        except OSError as e:
            ap.error(f"cannot read {a.env}: {e}")
        kv = {k.strip(): v.strip().strip('"').strip("'") for k, v in kv.items()}
        a.host = a.host or kv.get("BURNER_MQTT")
        a.user = a.user or kv.get("BURNER_MQTT_USER") or None
        if a.password is None and kv.get("BURNER_MQTT_PASS"):
            a.password = kv["BURNER_MQTT_PASS"]
    a.user = a.user or os.environ.get("BURNER_MQTT_USER") or None
    a.host = a.host or os.environ.get("BURNER_MQTT")
    if not a.host:
        ap.error("give --host (or --env .env, or --broker)")
    sys.exit(run_check(a))


if __name__ == "__main__":
    main()
