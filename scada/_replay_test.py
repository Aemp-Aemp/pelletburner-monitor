"""Offline test of tools/replay.py (no browser): time parsing, the cut copy of the data folder (source untouched),
recorded row -> status_info round trip through the real Module, the replay clock, the page-clock injection, the
serial lock-out, and one short end-to-end replay of a synthetic day in a subprocess."""
import os, sys, json, time, glob, shutil, tempfile, hashlib, subprocess, urllib.request, urllib.error, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import replay as RP
import burner_responder as R

FAILED = 0


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


def ep(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()


# ------------------------------------------------------------------ a small synthetic recording
TMP = tempfile.mkdtemp(prefix="replay_test_")
SRC = os.path.join(TMP, "src")
os.makedirs(SRC)
T0 = ep("2000-01-01 05:50:00")
PLAN = [(0, 0), (600, 2), (625, 3), (900, 4), (1500, 1)]          # (seconds after T0, state)


def state_at(s):
    st = 0
    for t, x in PLAN:
        if s >= t:
            st = x
    return st


def row(s):
    st = state_at(s)
    return {"epoch": T0 + s + 0.2, "state": st, "boiler_c": 40 + s / 100, "flue_c": 30, "analog3": 0.0,
            "sensor4": 120 if st in (3, 4) and s > 700 else 0, "power_pct": 60 if st == 4 else 0, "state2": st,
            "alarm": 0, "silo_kg": 250, "ignitions": 1000, "phase_step": 1, "din1": 0, "din2": 0, "fan_rpm": 1500,
            "fan_pct": 40, "ch3_status": 0, "din3": 1, "auger_on": 0, "igniter_on": 1 if st == 3 else 0,
            "flame": 120 if st in (3, 4) and s > 700 else 0, "setpoint": 70, "fuel_gph": 2000}


with open(os.path.join(SRC, "raw-2000-01-01.jsonl"), "w") as fh:
    for s in range(0, 1800):
        fh.write(json.dumps(row(s)) + "\n")
with open(os.path.join(SRC, "hist-2000-01-01.jsonl"), "w") as fh:
    for s in range(0, 1800, 10):
        fh.write(json.dumps(row(s)) + "\n")
with open(os.path.join(SRC, "events-2000-01-01.jsonl"), "w") as fh:
    fh.write(json.dumps({"type": "transition", "epoch": T0 - 3600, "from": 6, "to": 0}) + "\n")
    fh.write(json.dumps({"type": "transition", "epoch": T0 + 600, "from": 0, "to": 2}) + "\n")
open(os.path.join(SRC, "events-2000-01-02.jsonl"), "w").write(json.dumps({"type": "x", "epoch": T0 + 86400}) + "\n")
open(os.path.join(SRC, "summary-2000-01-01.json"), "w").write("{}")
open(os.path.join(SRC, "summary-1999-12-31.json"), "w").write("{}")
open(os.path.join(SRC, "prealarm-20000101-055000.json"), "w").write(json.dumps({"epoch": T0, "alarm": 4}))
open(os.path.join(SRC, "prealarm-20000101-070000.json"), "w").write(json.dumps({"epoch": T0 + 4200, "alarm": 5}))
open(os.path.join(SRC, "prealarm-last.json"), "w").write(json.dumps({"epoch": T0 + 4200, "alarm": 5}))
open(os.path.join(SRC, "settings.json"), "w").write(json.dumps(
    {"flame_field": "004", "warm": {"enabled": False, "cool_learn": [{"epoch": T0 - 10, "h": 3}, {"epoch": T0 + 9999, "h": 4}]}}))
open(os.path.join(SRC, "weather_days.json"), "w").write(json.dumps({"1999-12-31": 1.4, "2000-01-01": 0.2}))
with open(os.path.join(SRC, "pi-health-2000-01-01.jsonl"), "w") as fh:
    for s in range(0, 1800, 60):
        fh.write(json.dumps({"epoch": T0 + s, "cpu_c": 40}) + "\n")


def tree_hash(d):
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(d, "*"))):
        h.update(os.path.basename(p).encode()); h.update(open(p, "rb").read())
    return h.hexdigest()


SRC_HASH = tree_hash(SRC)

# ------------------------------------------------------------------ time parsing
check("parse 'YYYY-MM-DD HH:MM'", RP.parse_when("2000-01-01 06:00") == ep("2000-01-01 06:00:00"))
check("parse 'HH:MM' on the base day", RP.parse_when("06:25", base=ep("2000-01-01 06:00:00")) == ep("2000-01-01 06:25:00"))
check("parse 'HH:MM' earlier than base = next day", RP.parse_when("05:30", base=ep("2000-01-01 06:00:00")) == ep("2000-01-02 05:30:00"))
try:
    RP.parse_when("yesterday"); check("bad time refused", False)
except ValueError:
    check("bad time refused", True)

# ------------------------------------------------------------------ the cut copy
CUT = T0 + 600                                     # 06:00:00
W = os.path.join(TMP, "work")
RP.prepare_copy(SRC, W, CUT, log=lambda m: None)
late = []
for p in glob.glob(os.path.join(W, "*.jsonl")):
    for line in open(p):
        if json.loads(line).get("epoch", 0) > CUT:
            late.append(os.path.basename(p))
check("copy: no jsonl line after the cut", not late)
check("copy: day files after the cut day left out", not os.path.exists(os.path.join(W, "events-2000-01-02.jsonl")))
check("copy: day summary of the cut day left out, older kept",
      not os.path.exists(os.path.join(W, "summary-2000-01-01.json")) and os.path.exists(os.path.join(W, "summary-1999-12-31.json")))
check("copy: later pre-alarm snapshot left out, prealarm-last = last one before the cut",
      not os.path.exists(os.path.join(W, "prealarm-20000101-070000.json"))
      and json.load(open(os.path.join(W, "prealarm-last.json")))["alarm"] == 4)
st = json.load(open(os.path.join(W, "settings.json")))
check("copy: settings learned after the cut dropped", [x["h"] for x in st["warm"]["cool_learn"]] == [3])
check("copy: weather days of the cut day and later dropped", json.load(open(os.path.join(W, "weather_days.json"))) == {"1999-12-31": 1.4})
check("copy: source folder unchanged", tree_hash(SRC) == SRC_HASH)
for bad in (SRC, os.path.join(SRC, "sub")):
    try:
        RP.prepare_copy(SRC, bad, CUT, log=lambda m: None); check("copy: refuses to work inside the data folder", False)
    except SystemExit:
        check("copy: refuses to work inside the data folder", True)

# ------------------------------------------------------------------ recorded row -> status_info -> the real Module
codes = {v: k for k, v in R.STATUS_NAMES.items()}
r0 = row(800)
nd = RP.row_to_status(r0, codes)
check("row -> status_info uses the board's field numbers", nd["000"] == 3 and nd["004"] == 120 and nd["013"] == 1500
      and "flame" not in nd and "epoch" not in nd)
mod = R.Module(0x01)
mod.ingest_json('{"cfg_data_1":{}}', {"cfg_data_1": {"000": 70, "001": 2000}})
mod.ingest_json(json.dumps({"status_info": nd}), {"status_info": nd})
got = mod.hist[-1]
check("round trip: the Module rebuilds the recorded row",
      all(got.get(k) == r0[k] for k in r0 if k not in ("epoch", "flame")) and got["setpoint"] == 70)

# ------------------------------------------------------------------ the replay clock
ck = RP.Clock(1000.0, speed=100.0)
ck.barrier = 1e12
time.sleep(0.05)
n1 = ck.now()
check("clock runs at the replay speed", 1003 <= n1 <= 1030)
ck.barrier = 1001.0
check("clock never passes the barrier (row being processed)", ck.now() == 1001.0)
ck.barrier = 1e12
ck.pause(); a_ = ck.now(); time.sleep(0.03)
check("paused clock stands still", ck.now() == a_)
ck.jump(5000.0, "pause")
check("jump: free time is the target at once", ck.free() == 5000.0)
ck.ff_done()
check("jump done: holds (paused) at the target", ck.paused and ck.now() == 5000.0 and ck.ff_target is None)

# ------------------------------------------------------------------ safety: serial lock-out, page injection
stub = RP.SerialStub()
try:
    stub.Serial("COM8", 57600); check("serial stub refuses to open a port", False)
except RuntimeError:
    check("serial stub refuses to open a port", True)
check("normal runs: no replay object in the program", R.REPLAY is None)


class _A:
    data, port = SRC, 0


fd = RP.Feeder(SRC, W, CUT, CUT + 300, RP.Clock(CUT, 1), codes)
try:
    fd.write(b"x"); check("feeder refuses to write", False)
except RuntimeError:
    check("feeder refuses to write", True)
rp = RP.Replay(_A(), RP.Clock(CUT, 1), fd, W, CUT, CUT + 300)
page = rp.inject_html(b"<html><head><title>x</title></head><body></body></html>")
check("page clock script injected right after <head>", page.startswith(b"<html><head><script>") and b"window.Date=D" in page
      and b"api/run_mode" in page and b'"replay": true' in page)
code, j = rp.control({"action": "jump", "to": "2000-01-01 05:00"})
check("jump backwards refused", code == 409)
code, j = rp.control({"action": "jump", "to": "2000-01-02 05:00"})
check("jump past --to refused", code == 400)
code, j = rp.control({"action": "nonsense"})
check("unknown control action refused", code == 400)

# ------------------------------------------------------------------ end to end: a short replay in a subprocess
PORT = 8814
W2 = os.path.join(TMP, "work2")
p = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "replay.py"), "--data", SRC,
                      "--from", "2000-01-01 06:00", "--to", "2000-01-01 06:20", "--speed", "600", "--port", str(PORT),
                      "--at", "06:09", "--work", W2],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def get(u):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{u}", timeout=5) as r:
        return r.read()


def post(u, body, hdr=True):
    h = {"Content-Type": "application/json", "X-Burner": "1"} if hdr else {"Content-Type": "application/json"}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{u}", data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


try:
    stt = None
    for _ in range(150):
        time.sleep(0.2)
        try:
            stt = json.loads(get("/api/replay"))
            if not stt["ff"] and stt["paused"]:
                break
        except Exception:
            pass
    check("--at: fast-forwards and holds at 06:09", stt and stt["now_iso"] == "2000-01-01T06:09:00" and stt["paused"])
    s = json.loads(get("/status.json"))
    check("status.json 'now' is the replay time", abs(s["now"] - ep("2000-01-01 06:09:00")) < 1)
    check("live view shows the recorded state at 06:09 (DRIFT)", s["latest"]["fields"]["state"] == 4)
    tr = [(t.get("from"), t.get("to"), round(t["epoch"] - T0)) for t in s["transitions"]]
    check("transitions recomputed at the recorded seconds", (2, 3, 625) in tr)
    h = json.loads(get("/history.json?h=1"))["rows"]
    check("history has no row from the future", h and max(r["epoch"] for r in h) <= ep("2000-01-01 06:09:00"))
    pg = get("/home")
    check("/home carries the replay clock", b"window.__REPLAY__" in pg)
    check("start/stop refused in replay", post("/api/run_mode", {"cmd": "start"})[0] == 403)
    check("control needs the X-Burner header", post("/api/replay", {"action": "play"}, hdr=False)[0] == 403)
    c_, j_ = post("/api/replay", {"action": "step", "seconds": 360})
    for _ in range(100):
        time.sleep(0.1)
        stt = json.loads(get("/api/replay"))
        if not stt["ff"]:
            break
    s = json.loads(get("/status.json"))
    check("step +6 min -> 06:15, burning (DRIFT)", c_ == 200 and stt["now_iso"] == "2000-01-01T06:15:00"
          and s["latest"]["fields"]["state"] == 4)
    check("pi-health lines played into the copy up to the replay time",
          max(json.loads(x)["epoch"] for x in open(os.path.join(W2, "pi-health-2000-01-01.jsonl"))) <= ep("2000-01-01 06:15:00"))
    st_file = json.load(open(os.path.join(W2, "replay_state.json")))
    check("replay_state.json written", st_file.get("replay") is True)
    post("/api/replay", {"action": "jump", "to": "06:20", "then": "play"})
    for _ in range(100):
        time.sleep(0.1)
        stt = json.loads(get("/api/replay"))
        if stt["ended"]:
            break
    s = json.loads(get("/status.json"))
    check("end of the period: clock stops at --to", stt["ended"] and stt["now_iso"] == "2000-01-01T06:20:00")
    check("WAITING (rest) at 06:15 seen at the end", any(t.get("to") == 1 and round(t["epoch"] - T0) == 1500 for t in s["transitions"]))
finally:
    p.terminate()
    try:
        p.wait(5)
    except Exception:
        p.kill()
check("source folder still unchanged after the replay", tree_hash(SRC) == SRC_HASH)
shutil.rmtree(TMP, ignore_errors=True)
print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
