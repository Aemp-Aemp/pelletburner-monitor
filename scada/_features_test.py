import sys, os, tempfile, time as _time, datetime, json, random
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F
import burner_ext as X

class Clock:
    def __init__(self, t): self.t = t
    def time(self): return self.t
    def sleep(self, s): self.t += s
CLK = Clock(datetime.datetime(2026, 11, 12, 6, 0).timestamp())   # Thursday 06:00
F.time = CLK; X.time = CLK
res = []
def check(name, cond): res.append(("PASS" if cond else "FAIL", name))

class Mod:
    remote_armed = True
    last_rx = 0
    latest = None
    latest_cfg = {"raw": {"005": 40, "031": 100, "006": 6}}

def setup(windows, **sch):
    d = tempfile.mkdtemp()
    s = F.Settings(d, lambda m: None)
    s.update({"schedule": dict({"enabled": True, "windows": windows}, **sch)})
    tr = X.Tracker(None)
    mod = Mod()
    sent = []
    sc = F.Scheduler(s, tr, mod, lambda c, src: sent.append(c), lambda m: None, lambda e: sent.append(("ev", e.get("kind"))))
    return s, tr, mod, sc, sent

def row(tr, st, al=0):
    tr.on_row({"epoch": CLK.t, "state": st, "alarm": al}, [])

def step(sc, mod, tr, st, secs, al=0):
    end = CLK.t + secs
    while CLK.t < end:
        CLK.t += 5; mod.last_rx = CLK.t; mod.latest = {"epoch": CLK.t}; row(tr, st, al); sc.tick()

W = [{"on": True, "days": [0, 1, 2, 3, 4, 5, 6], "start": "05:00", "end": "07:00"}]
# 1 basic start after min-off satisfied
s, tr, mod, sc, sent = setup(W)
CLK.t -= 3600; row(tr, 0); CLK.t += 3600          # stopped for 1 h
step(sc, mod, tr, 0, 10)
check("start sent in window when STOPPET", sent[:1] == ["start"])
# 2 no reaction -> retries, warning after 2, give up after 3
step(sc, mod, tr, 0, 400)
starts = [x for x in sent if x == "start"]
check("3 attempts then stop", len(starts) == 3)
check("warning after 2 failed", ("ev", "start_failed") in sent)
check("gave up + blocked", ("ev", "gave_up") in sent and sc.status["code"] == "blocked")
# 3 success path
s, tr, mod, sc, sent = setup(W)
CLK.t -= 3600; row(tr, 0); CLK.t += 3600
step(sc, mod, tr, 0, 6); step(sc, mod, tr, 2, 30)
check("success resets attempts", sc.attempts == 0 and sc.status["code"] == "running")
# 4 manual stop blocks until next window
step(sc, mod, tr, 4, 600); step(sc, mod, tr, 0, 2000)
check("manual stop respected", sent.count("start") == 1 and sc.status["code"] == "blocked")
# 5 min-off cooldown
s, tr, mod, sc, sent = setup(W, min_off_min=30)
CLK.t = datetime.datetime(2026, 11, 13, 5, 0).timestamp()
CLK.t -= 600; row(tr, 4); CLK.t += 300; row(tr, 1); CLK.t += 300      # entered WAITING 5 min ago... then
s.update({"schedule": {"override_waiting": False}})
step(sc, mod, tr, 1, 30)
check("WAITING not overridden when off", "stop" not in sent and sc.status["code"] == "waiting")
s, tr, mod, sc, sent = setup(W, min_off_min=30)
CLK.t = datetime.datetime(2026, 11, 14, 5, 0).timestamp()
row(tr, 4); CLK.t += 60; row(tr, 0)      # stopped (outside our window logic: manual stop happens at 05:01 -> blocked)
# use a fresh scheduler so the manual stop isn't seen
sc2 = F.Scheduler(s, tr, mod, lambda c, src: sent.append(c), lambda m: None, lambda e: None)
step(sc2, mod, tr, 0, 60)
check("min off-time blocks early start", "start" not in sent and sc2.status["code"] == "cooldown")
step(sc2, mod, tr, 0, 30 * 60)
check("starts after min off-time", "start" in sent)
# 6 override WAITING: stop then start without waiting for min-off
s, tr, mod, sc, sent = setup(W, override_waiting=True)
CLK.t = datetime.datetime(2026, 11, 15, 4, 0).timestamp()
row(tr, 4); CLK.t += 10; row(tr, 1)                       # WAITING since 04:00 (1 h before the window)
CLK.t = datetime.datetime(2026, 11, 15, 5, 0).timestamp()
step(sc, mod, tr, 1, 10)
check("override sends stop", sent[:1] == ["stop"])
step(sc, mod, tr, 0, 60)
check("then start", "start" in sent)
# 6b manual stop shortly after an override start is respected (review finding 4)
s, tr, mod, sc, sent = setup(W, override_waiting=True)
CLK.t = datetime.datetime(2026, 11, 20, 4, 0).timestamp()
row(tr, 4); CLK.t += 10; row(tr, 1)
CLK.t = datetime.datetime(2026, 11, 20, 5, 0).timestamp()
step(sc, mod, tr, 1, 10); step(sc, mod, tr, 0, 60)     # override stop, then start
step(sc, mod, tr, 2, 30)                               # started
n = sent.count("start")
step(sc, mod, tr, 0, 600)                              # user presses STOP ~1.5 min after the override
check("manual stop after override is respected", sent.count("start") == n and sc.status["code"] == "blocked")
# 6c stale status: no action without fresh telemetry (review finding 6)
s, tr, mod, sc, sent = setup(W)
CLK.t = datetime.datetime(2026, 11, 21, 5, 0).timestamp()
CLK.t -= 3600; row(tr, 0); CLK.t += 3600
mod.last_rx = CLK.t; mod.latest = {"epoch": CLK.t - 300}      # keep-alives arrive, status is 5 min old
CLK.t += 6; sc.tick()
check("no start on stale status", not sent and sc.status["code"] == "no_link")
# 6d bad settings.json is ignored per section (review finding 13)
dd = tempfile.mkdtemp()
with open(os.path.join(dd, "settings.json"), "w") as fh:
    json.dump({"schedule": {"enabled": True, "windows": [{"days": [1], "start": "99:00", "end": "x"}]},
               "flame_field": "004", "lux": {"reset_epoch": 123}}, fh)
s2 = F.Settings(dd, lambda m: None)
check("bad schedule section ignored, rest kept", s2.get("schedule")["windows"] == [] and
      s2.get("flame_field") == "004" and s2.get("lux")["reset_epoch"] == 123)
# 7 alarm: never
s, tr, mod, sc, sent = setup(W)
CLK.t = datetime.datetime(2026, 11, 16, 5, 30).timestamp()
step(sc, mod, tr, 6, 60, al=4)
check("no start in ALARM", not sent and sc.status["code"] == "alarm")
# 8 outside window + midnight window
s, tr, mod, sc, sent = setup([{"on": True, "days": [0], "start": "22:00", "end": "02:00"}])
CLK.t = datetime.datetime(2026, 11, 17, 1, 0).timestamp()   # Tuesday 01:00, window started Monday 22:00
k, w = sc.active_window()
check("midnight window belongs to start day", k is not None)
CLK.t = datetime.datetime(2026, 11, 17, 12, 0).timestamp()
check("outside window", sc.active_window()[0] is None)
# 9 max starts/day
s, tr, mod, sc, sent = setup(W, max_starts_day=2, min_off_min=0)
CLK.t = datetime.datetime(2026, 11, 18, 5, 0).timestamp()
for _ in range(2):
    row(tr, 1); CLK.t += 10; row(tr, 2); CLK.t += 10; row(tr, 1); CLK.t += 10
row(tr, 0)                                   # (1 -> 0 inside the window = manual stop; clear it for this test)
sc.blocked_key = None
step(sc, mod, tr, 0, 30)
check("max starts reached", "start" not in sent and sc.status["code"] == "max_starts")
# 10 settings validation
s = F.Settings(tempfile.mkdtemp(), lambda m: None)
check("bad time rejected", not s.update({"schedule": {"windows": [{"days": [1], "start": "25:00", "end": "06:00"}]}})[0])
check("no days rejected", not s.update({"schedule": {"windows": [{"days": [], "start": "05:00", "end": "06:00"}]}})[0])
check("24 windows ok", s.update({"schedule": {"windows": [{"days": [1], "start": "05:00", "end": "06:00"}] * 24}})[0])
check("25 windows rejected", not s.update({"schedule": {"windows": [{"days": [1], "start": "05:00", "end": "06:00"}] * 25}})[0])

# ---- LUX monitor: 12 days of steady DRIFT, 6 h/day, LUX falling 140 -> 105 (soot) ----
d = tempfile.mkdtemp()
s = F.Settings(d, lambda m: None); s.update({"flame_field": "003"})
tr = X.Tracker(None)
evs = []
lm = F.LuxMonitor(s, tr, d, lambda m: None, evs.append)
CLK.t = datetime.datetime(2026, 11, 1, 6, 0).timestamp()
hist = []
for day in range(14):
    t0 = CLK.t
    row(tr, 4); tr.since = CLK.t - 3600
    for i in range(6 * 360):                      # 6 h at 10 s
        CLK.t += 10
        lux = 140 - 35 * (day * 6 * 360 + i) / (12 * 6 * 360) + random.uniform(-1, 1)
        r = {"epoch": CLK.t, "state": 4, "flame": lux, "power_pct": 60 + random.uniform(-2, 2),
             "fan_rpm": 1890 + random.uniform(-20, 20), "boiler_c": 62}
        hist.append(r); hist = hist[-80:]
        lm.on_row(r, hist)
    CLK.t = t0 + 86400
out = lm.analyse()
check("lux samples collected", len(out["samples"]) > 300)
check("soot trend warning", any(w["kind"] == "soot" for w in out["warnings"]))
check("drop warning", any(w["kind"] == "drop" for w in out["warnings"]))
check("warnings emitted as events", any(e.get("source") == "lux" for e in evs))
res.append(("info", f"lux trend {out.get('trend')} mid band {out['bands']['mid']}"))
lm.reset()
out = lm.analyse()
check("reset -> learning, no warnings", out["status"] == "learning" and not out["warnings"] or out["status"] == "no_data")
# summer gap: samples persist across idle days (running-time window)
check("samples reloaded from disk", len(F.LuxMonitor(s, tr, d, lambda m: None, evs.append).samples) > 300)

# ---- automatic alarm reset ----
def ar_setup(**cfg):
    d = tempfile.mkdtemp()
    s = F.Settings(d, lambda m: None)
    ok, msg = s.update({"auto_reset": dict({"enabled": True, "wait_min": 1}, **cfg)})
    assert ok, msg
    tr = X.Tracker(None); mod = Mod(); sent = []; evs = []
    ar = F.AutoReset(s, tr, mod, lambda c, src: sent.append(src), lambda m: None, evs.append)
    return s, tr, mod, ar, sent, evs
def ar_step(ar, mod, tr, st, secs, al=0):
    end = CLK.t + secs
    while CLK.t < end:
        CLK.t += 5; mod.latest = {"epoch": CLK.t}; tr.on_row({"epoch": CLK.t, "state": st, "alarm": al}, []); ar.tick()
CLK.t = datetime.datetime(2026, 11, 25, 10, 0).timestamp()
s, tr, mod, ar, sent, evs = ar_setup()
ar_step(ar, mod, tr, 4, 60)                      # burning
ar_step(ar, mod, tr, 6, 30, al=6)                # ALARM 6 (ash plate) while burning
check("auto-reset waits the cool-down", not sent and ar.status["code"] == "waiting")
ar_step(ar, mod, tr, 6, 60, al=6)
check("auto-reset clears after the wait", sent == ["auto_reset (clear)"])
ar_step(ar, mod, tr, 0, 10)
check("auto-reset restarts from STOPPET", sent[-1] == "auto_reset (restart)")
ar_step(ar, mod, tr, 2, 60); ar_step(ar, mod, tr, 4, 30)
check("saved by auto alarm reset counted", s.get("auto_reset_stats")["saves"] == 1 and any(e.get("kind") == "saved" for e in evs))
# max 2 per 24 h, then gives up and leaves the alarm
s, tr, mod, ar, sent, evs = ar_setup()
for _ in range(3):
    ar_step(ar, mod, tr, 4, 60); ar_step(ar, mod, tr, 6, 90, al=4); ar_step(ar, mod, tr, 0, 10); ar_step(ar, mod, tr, 2, 30)
ar_step(ar, mod, tr, 6, 200, al=4)
check("auto-reset max 2 attempts in 24 h", sent.count("auto_reset (clear)") == 2 and ar.status["code"] == "gave_up")
check("gave-up warning emitted", any(e.get("type") == "warning" and e.get("source") == "auto_reset" for e in evs))
# never for alarm 2, never when not burning, off by default
s, tr, mod, ar, sent, evs = ar_setup()
ar_step(ar, mod, tr, 4, 60); ar_step(ar, mod, tr, 6, 200, al=2)
check("alarm 2 (burn-back) never auto-reset", not sent and ar.status["code"] == "not_allowed")
check("alarm 2 cannot be enabled", not s.update({"auto_reset": {"alarms": [2, 6]}})[0])
s, tr, mod, ar, sent, evs = ar_setup()
ar_step(ar, mod, tr, 0, 60); ar_step(ar, mod, tr, 6, 200, al=6)
check("alarm while not burning is left alone", not sent and ar.status["code"] == "not_burning")
check("auto-reset off by default", F.Settings(tempfile.mkdtemp(), lambda m: None).get("auto_reset")["enabled"] is False)

# ---- ignition history ----
d = tempfile.mkdtemp()
s = F.Settings(d, lambda m: None)
s.add_batch("Brand A")
tr = X.Tracker(None)
class M3:
    latest = {"fields": {"boiler_c": 20.0, "flue_c": None}}
    latest_cfg = {"raw": {"014": 60, "015": 83, "016": 80, "017": 600, "018": 20, "019": 3, "020": 35}}
ig = F.IgnitionLog(s, tr, M3(), d, lambda m: None)
CLK.t = datetime.datetime(2026, 12, 2, 6, 0).timestamp()
def ig_row(st, **kw):
    r = dict({"epoch": CLK.t, "state": st, "alarm": 0, "fan_rpm": 2000, "auger_on": 0, "flame": 0}, **kw)
    tr.on_row(r, []); ig.on_row(r)
ig_row(0)
CLK.t += 10; ig_row(2)
CLK.t += 20; ig_row(3, auger_on=1)
for k in range(24):
    CLK.t += 10; ig_row(3, auger_on=1 if k < 5 else 0, flame=60 if k > 20 else 0)
CLK.t += 10; ig_row(4, flame=120)
rec = ig.records[-1]
check("ignition ok recorded", rec["result"] == "ok" and abs(rec["minutes"] - 4.67) < 0.2)
check("ignition details (batch, cfg, auger, lux, boiler)", rec["batch"] == "Brand A" and rec["cfg"].get("018") == 20
      and rec["auger_s"] >= 50 and rec["peak_lux"] == 60 and rec["boiler"] == 20.0)   # peak before RUNNING
CLK.t += 3600; ig_row(1); CLK.t += 60; ig_row(0)
CLK.t += 10; ig_row(2); CLK.t += 20; ig_row(3); CLK.t += 600; ig_row(6, alarm=4)
check("failed ignition recorded with alarm", ig.records[-1]["result"] == "failed" and ig.records[-1]["alarm"] == 4)
rep = ig.report()
check("ignition report per batch", rep["stats"]["Brand A"]["n"] == 2 and rep["stats"]["Brand A"]["failed"] == 1)
check("ignition history persisted", len(F.IgnitionLog(s, X.Tracker(None), M3(), d, lambda m: None).records) == 2)

# ---- fan monitor: steady samples, rpm drop at same output ----
d = tempfile.mkdtemp()
s = F.Settings(d, lambda m: None)
fm = F.FanMonitor(s, d, lambda m: None)
CLK.t = datetime.datetime(2026, 12, 1, 6, 0).timestamp()
hist = []
for i in range(6 * 24 * 10):                     # 10 days of 10-s rows... (only the last 5 min window matters)
    CLK.t += 10
    wear = 1 - 0.08 * i / (6 * 24 * 10)           # rpm slowly 8 % lower at the same output
    r = {"epoch": CLK.t, "state": 4, "fan_pct": 60, "fan_rpm": 1890 * wear + random.uniform(-10, 10)}
    hist.append(r); hist = hist[-40:]
    fm.on_row(r, hist)
fa = fm.analyse()
lv = [l for l in fa["levels"] if l["pct"] == 60]
check("fan samples recorded", len(fa["samples"]) >= 20)
check("fan rpm drop visible at same output", lv and lv[0]["change_pct"] is not None and lv[0]["change_pct"] < -3)
hist2 = [{"epoch": CLK.t + k * 10, "state": 4, "fan_pct": 40 + (k % 2) * 10, "fan_rpm": 1500} for k in range(40)]
n0 = len(fm.samples); CLK.t += 3600
fm.on_row(hist2[-1] | {"epoch": CLK.t}, [h | {"epoch": CLK.t - 290 + j * 10} for j, h in enumerate(hist2[-30:])])
check("no sample when fan output is changing", len(fm.samples) == n0)
fm.reset()
check("fan reset -> no data", fm.analyse()["status"] == "no_data")

# ---- optimiser summary ----
rows = []
t = datetime.datetime(2026, 11, 2, 0, 0).timestamp()
seq = [(0, 3600)] + [(2, 120), (3, 300), (4, 600), (1, 3000)] * 6      # 6 short runs
for st, dur in seq:
    for k in range(0, dur, 10):
        rows.append({"epoch": t + k, "state": st, "power_pct": 50 if st == 4 else 0, "fuel_gph": 2200,
                     "silo_kg": 100, "alarm": 0})
    t += dur
sm = F.summarise_rows(rows)
check("starts counted", sm["starts"] == 6)
check("runs measured (~17 min)", sm["runs"] and abs(sm["runs"][0] - 17) < 1)
check("fuel estimate", 0.5 < sm["fuel_est_kg"] < 1.5)
class M2: latest_cfg = {"raw": {"005": 40, "031": 100, "006": 6}}
s = F.Settings(tempfile.mkdtemp(), lambda m: None)
op = F.Optimiser(s, None, M2(), print)
sug = op.suggest([dict(sm, day="2026-11-02")])
check("short-run suggestion", any(x["title"] == "Short runs" for x in sug))
for st, name in res:
    print(st, name)
print("FAILED:", sum(1 for st, _ in res if st == "FAIL"))
