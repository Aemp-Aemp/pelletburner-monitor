"""Synthetic recording for tests (no real readings): a made-up boiler that burns 06:00-09:00 and 16:00-20:00 every
day from 2000-01-01, written in the same files as the monitor's data folder. Always the same numbers.

    make_data(folder, days=3)                    hist-*.jsonl (10 s), events, settings, weather
    make_data(folder, days=3, raw=(2, 5, 10))    also raw-*.jsonl (1 s) for day index 2, 05:00-10:00
    Replay(port, at="08:00")                     the real program on that data, held at 2000-01-03 08:00
"""
import datetime, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
DAY0 = datetime.datetime(2000, 1, 1)
BURNS = ((6 * 3600, 9 * 3600), (16 * 3600, 20 * 3600))      # (start, stop) seconds of the day
FLAME_S = (240, 300)                # seconds from IGNITION (state 3) to flame > 17: first burn (+30 per day), second
PERIODS = [{"name": "Morning", "start": "07:00", "end": "09:00", "days": [0, 1, 2, 3, 4, 5, 6], "kind": "warmby", "on": True},
           {"name": "Evening", "start": "16:00", "end": "20:00", "days": [0, 1, 2, 3, 4, 5, 6], "kind": "keep", "on": True}]


def flame_delay(day, burn):
    return FLAME_S[burn] + (30 * day if burn == 0 else 0)


def _row(day, s, boiler, kg):
    """One reading at second `s` of day index `day`."""
    r = {"state": 1 if s < 20 * 3600 + 1800 and s >= BURNS[0][0] else 0, "power_pct": 0, "fan_pct": 0, "din2": 0,
         "auger_on": 0, "igniter_on": 0, "flame": 0}
    for n, (a, b) in enumerate(BURNS):
        u, fd = s - a, flame_delay(day, n)
        if 0 <= u < 60:                                          # STARTING: ash plate, air purge, getting ready
            r.update(state=2, din2=1 if u < 10 else 0, fan_pct=100 if u < 30 else 40)
        elif 60 <= u < 1200:                                     # IGNITION: feed, heat, flame, settle, step up
            v = u - 60
            r.update(state=3, fan_pct=50, auger_on=1 if v < 90 else 0, flame=120 if v >= fd else 0,
                     igniter_on=1 if v < fd + 60 else 0, power_pct=30 if v >= fd + 420 else 0)
        elif 1200 <= u and s < b:                                # RUNNING: full power, half power the last hour
            r.update(state=4, fan_pct=70, auger_on=1, flame=150, power_pct=50 if s >= b - 3600 else 100)
        elif b <= s < b + 600:                                   # cooling down after the stop
            r.update(fan_pct=25)
    st = r["state"]
    return {"epoch": (DAY0 + datetime.timedelta(days=day, seconds=s)).timestamp() + 0.2, "state": st,
            "boiler_c": round(boiler, 1), "flue_c": 120 if st == 4 else 30, "analog3": 0.0, "sensor4": r["flame"],
            "power_pct": r["power_pct"], "state2": st, "alarm": 0, "silo_kg": 250 - int(kg), "ignitions": 1000 + 2 * day,
            "phase_step": 1, "din1": 0, "din2": r["din2"], "fan_rpm": r["fan_pct"] * 30, "fan_pct": r["fan_pct"],
            "ch3_status": 0, "din3": 1, "auger_on": r["auger_on"], "igniter_on": r["igniter_on"], "flame": r["flame"],
            "setpoint": 70, "fuel_gph": 2000}


def make_data(folder, days=3, raw=None):
    """Write the synthetic data folder. raw = (day index, from hour, to hour) adds 1-second rows for that window."""
    os.makedirs(folder, exist_ok=True)
    boiler, kg, events = 30.0, 0.0, []
    for day in range(days):
        name = (DAY0 + datetime.timedelta(days=day)).strftime("%Y-%m-%d")
        fh_raw = open(os.path.join(folder, f"raw-{name}.jsonl"), "w") if raw and raw[0] == day else None
        with open(os.path.join(folder, f"hist-{name}.jsonl"), "w") as fh:
            for s in range(86400):
                r = _row(day, s, boiler, kg)
                if s % 10 == 0:
                    fh.write(json.dumps(r) + "\n")
                if fh_raw and raw[1] * 3600 <= s < raw[2] * 3600:
                    fh_raw.write(json.dumps(r) + "\n")
                p = r["power_pct"] if r["state"] == 4 else 0
                boiler = min(75.0, boiler + p / 100 * 0.004 - (boiler - 18) * 4.6e-5)
                kg += p / 100 * r["fuel_gph"] / 1000 / 3600
        if fh_raw:
            fh_raw.close()
        t0 = (DAY0 + datetime.timedelta(days=day, seconds=BURNS[0][0])).timestamp()
        events.append((name, {"type": "warm", "what": "start", "why": "warm by 07:00: Morning", "epoch": t0}))
    for name, e in events:
        with open(os.path.join(folder, f"events-{name}.jsonl"), "w") as fh:
            fh.write(json.dumps(e) + "\n")
    with open(os.path.join(folder, "settings.json"), "w") as fh:
        json.dump({"flame_field": "004", "warm": {"enabled": False, "periods": PERIODS}, "setup": {"done": True}}, fh)
    t0 = DAY0.timestamp()
    hourly = [[t0 + h * 3600, round(2.0 + (h % 24) / 6.0, 1)] for h in range(days * 24)]
    with open(os.path.join(folder, "weather_cache.json"), "w") as fh:
        json.dump({"at": hourly[-1][0], "c": hourly[-1][1], "hourly": hourly}, fh)
    with open(os.path.join(folder, "weather.jsonl"), "w") as fh:
        for t, c in hourly:
            fh.write(json.dumps({"epoch": t, "c": c}) + "\n")
    return folder


def _busy(port):
    """True if something already answers on 127.0.0.1:port."""
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Replay:
    """The real program (tools/replay.py) on the synthetic data, held (paused) at 2000-01-03 `at`. Nothing is sent
    anywhere: the replay opens no serial port and no MQTT. Use as:  with Replay(8815) as base: ..."""

    def __init__(self, port, at="08:00", data=None, t_from="2000-01-03 05:30", t_to="2000-01-03 09:30"):
        if _busy(port):     # a left-over replay there would answer instead of ours (Windows lets both bind)
            port = _free_port()
        self.port, self.at, self.data, self.t_from, self.t_to = port, at, data, t_from, t_to
        self.base = f"http://127.0.0.1:{port}"
        self.tmp = tempfile.mkdtemp(prefix="pb_synth_")

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return json.loads(r.read())

    def __enter__(self):
        data = self.data or make_data(os.path.join(self.tmp, "src"), days=3, raw=(2, 5, 10))
        self.log = open(os.path.join(self.tmp, "replay.log"), "w")
        self.p = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "replay.py"), "--data", data,
                                   "--from", self.t_from, "--to", self.t_to, "--port", str(self.port), "--at", self.at,
                                   "--work", os.path.join(self.tmp, "work")], stdout=self.log, stderr=subprocess.STDOUT)
        end = time.time() + 180
        while time.time() < end:
            time.sleep(0.3)
            if self.p.poll() is not None:
                break
            try:
                st = self.get("/api/replay")
                if st["paused"] and not st["ff"] and self.get("/api/insights").get("generated"):
                    return self
            except Exception:
                pass
        self.__exit__()
        raise RuntimeError("the replay did not start: " + open(os.path.join(self.tmp, "replay.log")).read()[-2000:])

    def __exit__(self, *a):
        self.p.terminate()
        try:
            self.p.wait(10)
        except Exception:
            self.p.kill()
        self.log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
