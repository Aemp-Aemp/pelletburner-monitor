import os
"""Offline test: warm-period mode (the web's own minimum mode). Fake clock, no serial."""
import sys, tempfile, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F

FAILED = 0


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


class Clock:
    t = datetime.datetime(2026, 11, 14, 4, 50).timestamp()
CLK = Clock()
F.time.time = lambda: CLK.t


class Mod:
    remote_armed = True
    latest = None
    latest_cfg = {"raw": {"005": 40, "006": 6, "007": 300, "008": 1020}}


class Tracker:
    listeners = []


def setup(**warm):
    s = F.Settings(tempfile.mkdtemp(), lambda *a: None)
    ok, msg = s.update({"warm": dict({"enabled": True}, **warm)})
    assert ok, msg
    mod, sent, ev = Mod(), [], []
    w = F.WarmMode(s, Tracker(), mod, lambda c, src: sent.append((c, src)), lambda *a: None, ev.append)
    return w, mod, sent, ev


def feed(mod, st, pw=0, al=0):
    mod.latest = {"epoch": CLK.t, "fields": {"state": st, "power_pct": pw, "alarm": al}}


def run(w, mod, st, secs, pw=0, al=0, step=5):
    for _ in range(int(secs / step)):
        CLK.t += step
        feed(mod, st, pw, al)
        w.tick()


# settings carried over from the boiler's own minimum mode
w, mod, sent, ev = setup()
c = w.cfg()
check("board settings carried over (stop 40 %, pause 6 h, boosts 05:00/17:00)",
      c["stop_pct"] == 40 and c["pause_h"] == 6 and c["boosts"] == ["05:00", "17:00"] and c["source"] == "board")

# low power in RUNNING -> one stop after stop_after_min
CLK.t = datetime.datetime(2026, 11, 13, 21, 0).timestamp()
w, mod, sent, ev = setup()
run(w, mod, 4, 60, pw=35)
check("no stop before 2 min at low power", sent == [])
run(w, mod, 4, 90, pw=35)
check("stop sent once after 2 min at/below STOP AT", sent == [("stop", "warm mode (low power)")])
run(w, mod, 4, 60, pw=35)
check("no second stop while the first is being delivered", len(sent) == 1)
run(w, mod, 0, 600)
check("resting after our stop, no start outside boost windows", len(sent) == 1 and w.status["code"] == "resting")
check("next start = stop + PAUSE 6 h (03:02), earlier than the 05:00 boost",
      datetime.datetime.fromtimestamp(w.feed()["next_start"]).strftime("%H:%M") == "03:02")
run(w, mod, 4, 120, pw=60)
check("normal power does not stop", len(sent) == 1)

# 05:00 start, no matter how warm; retries; served once
CLK.t = datetime.datetime(2026, 11, 14, 4, 58).timestamp()
w, mod, sent, ev = setup()
run(w, mod, 0, 60)
check("no start before 05:00", sent == [])
run(w, mod, 0, 60)
check("start at 05:00 from STOPPET", sent == [("start", "warm mode")])
run(w, mod, 0, 100)
check("retry after 90 s without reaction", sent.count(("start", "warm mode")) == 2)
run(w, mod, 2, 30)
run(w, mod, 3, 300)
run(w, mod, 0, 600)
check("after a successful start the 05:00 window is used (no restart in it)", sent.count(("start", "warm mode")) == 2)

# gives up after max_attempts and warns
CLK.t = datetime.datetime(2026, 11, 14, 16, 59).timestamp()
w, mod, sent, ev = setup()
run(w, mod, 0, 600)
check("17:00: 3 attempts then gives up with a warning",
      sent.count(("start", "warm mode")) == 3 and any(e.get("kind") == "start_failed" for e in ev))

# the boiler's own WAITING pause is ended at the boost time
CLK.t = datetime.datetime(2026, 11, 14, 4, 59).timestamp()
w, mod, sent, ev = setup()
run(w, mod, 1, 70)
check("WAITING at 05:00: stop first", sent == [("stop", "warm mode (end WAITING)")])
run(w, mod, 0, 60)
check("then start", sent[-1] == ("start", "warm mode"))

# alarms and stale data: never act
CLK.t = datetime.datetime(2026, 11, 14, 4, 59).timestamp()
w, mod, sent, ev = setup()
run(w, mod, 6, 120, al=4)
check("ALARM: no command", sent == [] and w.status["code"] == "alarm")
CLK.t += 10
mod.latest = {"epoch": CLK.t - 300, "fields": {"state": 0}}
w.tick()
check("stale data: no command", sent == [] and w.status["code"] == "no_link")

# pause over -> start (rest caused by us)
CLK.t = datetime.datetime(2026, 11, 14, 8, 0).timestamp()
w, mod, sent, ev = setup()
run(w, mod, 4, 150, pw=30)
run(w, mod, 0, 6 * 3600 + 60, step=60)
check("pause of 6 h over -> start", sent[-1] == ("start", "warm mode"))

# ---------------- heating plan (periods)
PLAN = [{"name": "Morning", "start": "05:00", "end": "07:00", "days": list(range(7)), "kind": "keep"},
        {"name": "Dinner", "start": "16:30", "end": "19:30", "days": list(range(7)), "kind": "keep"},
        {"name": "Noon", "start": "12:00", "end": "12:30", "days": [5, 6], "kind": "start"}]


def feedb(mod, st, pw=0, bc=60):
    mod.latest = {"epoch": CLK.t, "fields": {"state": st, "power_pct": pw, "alarm": 0, "boiler_c": bc}}


def runb(w, mod, st, secs, pw=0, bc=60, step=5):
    for _ in range(int(secs / step)):
        CLK.t += step
        feedb(mod, st, pw, bc)
        w.tick()


Mod.latest_cfg = {"raw": {"005": 40, "006": 6, "007": 300, "008": 1020}, "fields": {"setpoint_c": 65}}
CLK.t = datetime.datetime(2026, 11, 14, 4, 58).timestamp()          # Saturday
w, mod, sent, ev = setup(periods=PLAN, pause_h=0)
runb(w, mod, 0, 60, bc=62)
check("plan: nothing before the morning period", sent == [])
runb(w, mod, 0, 70, bc=62)
check("plan: start at 05:00 even with a warm boiler (62 °C)", sent == [("start", "warm mode")])
runb(w, mod, 3, 600)
runb(w, mod, 4, 600, pw=30)
check("keep period: NOT stopped at low power (30 % > keep limit 25 %)", sent == [("start", "warm mode")] and w.status["code"] == "keep")
w2, mod2, sent2, ev2 = setup(periods=PLAN, pause_h=0)
CLK.t = datetime.datetime(2026, 11, 14, 5, 40).timestamp()
runb(w2, mod2, 4, 150, pw=20)
check("keep period: never stopped by the web, even at 20 % (the boiler stops itself)", sent2 == [] and w2.status["code"] == "keep")
runb(w, mod, 1, 300, bc=63)                      # the boiler pauses itself, still warm
check("keep period: warm boiler is not re-lit", len(sent) == 1 and w.status["code"] == "keep_wait")
for _ in range(400):                             # cooled 7 °C below the target: WAITING until our stop arrives
    if ("stop", "warm mode (end WAITING)") in sent: break
    runb(w, mod, 1, 5, bc=58)
check("keep period: ends the boiler's pause when cooled 5 °C below the target", ("stop", "warm mode (end WAITING)") in sent)
runb(w, mod, 0, 120, bc=58)                      # the board obeys the stop -> STOPPET
check("keep period: then starts again", sent[-1] == ("start", "warm mode"))
runb(w, mod, 2, 30); runb(w, mod, 3, 120)
CLK.t = datetime.datetime(2026, 11, 14, 7, 10).timestamp()
w.stopped_at = None
runb(w, mod, 4, 600, pw=30)
check("just RUNNING: no web pause in the first 20 min", sent[-1] != ("stop", "warm mode (low power)"))
runb(w, mod, 4, 900, pw=30)
check("after the period: low power -> stop", sent[-1] == ("stop", "warm mode (low power)"))
n = len(sent)
runb(w, mod, 0, 4 * 3600, step=60, bc=40)
check("outside periods with pause 0: no restart, rests until the next period", len(sent) == n)
check("next start = 12:00 (Saturday start period)",
      datetime.datetime.fromtimestamp(w.feed()["next_start"]).strftime("%H:%M") == "12:00")
CLK.t = datetime.datetime(2026, 11, 14, 11, 59).timestamp()
runb(w, mod, 0, 120, bc=40)
check("start period: one start at 12:00", sent[-1] == ("start", "warm mode"))
runb(w, mod, 2, 30); runb(w, mod, 3, 300); runb(w, mod, 0, 600, bc=40)
check("start period: no restart inside it after the start", sent.count(("start", "warm mode")) == 3)
pl = w.feed()["plan"]
check("plan bands for the charts", any(b["name"] == "Dinner" and b["kind"] == "keep" for b in pl))
CLK.t = datetime.datetime(2026, 11, 14, 16, 0).timestamp()
w, mod, sent, ev = setup(periods=PLAN, pause_h=0)
runb(w, mod, 4, 2400, pw=50)
check("period starts while burning: no command, keeps warm", sent == [] and w.status["code"] == "keep")
s2 = F.Settings(tempfile.mkdtemp(), lambda *a: None)
check("bad period refused", not s2.update({"warm": {"periods": [{"start": "05:00", "end": "05:00", "days": [1]}]}})[0])
check("period without days refused", not s2.update({"warm": {"periods": [{"start": "05:00", "end": "06:00", "days": []}]}})[0])

# ---------------- warm by + never colder than
WB = [{"name": "Warm when we get up", "start": "05:30", "end": "06:00", "days": list(range(7)), "kind": "warmby"}]
CLK.t = datetime.datetime(2026, 11, 14, 4, 0).timestamp()
w, mod, sent, ev = setup(periods=WB, pause_h=0)
feedb(mod, 0, bc=30); w.tick()
check("warm by from 30 °C: lighting 35 + 1.5 x (60 - 52 at RUNNING) = 47 -> 50 min", w.lead_min(w.cfg()) == 50)
check("warm by: start temperature below the RUNNING temperature does not matter", w.lead_min(w.cfg(), bc=15) == 50)
check("warm by from 56 °C: 35 + 1.5 x 4 = 41 -> 45 min", w.lead_min(w.cfg(), bc=56) == 45)
check("warm by from a warm boiler: lighting only (35 min)", w.lead_min(w.cfg(), bc=63) == 35)
check("warm by: cap 90 min with odd learned values",
      w.lead_min(dict(w.cfg(), warm_learn=[{"ign": 80, "run_c": 30, "kh": 6}]), bc=20) == 90)
check("warm by: old learned records (k1/k2 only) fall back to the defaults",
      w.lead_min(dict(w.cfg(), warm_learn=[{"from_c": 25, "min": 47, "k1": 0.8, "k2": 1.4}])) == 50)
runb(w, mod, 0, 0, bc=30)
CLK.t = datetime.datetime(2026, 11, 14, 4, 38).timestamp()
check("warm by 05:30: no start before 04:40", sent == [])
runb(w, mod, 0, 120, bc=30)
check("warm by 05:30: started at ~04:40", sent == [("start", "warm mode")])
runb(w, mod, 3, 2400, bc=50)                    # a long lighting (40 min); the boiler warms meanwhile
runb(w, mod, 4, 300, pw=80, bc=54)              # RUNNING at 54 °C
runb(w, mod, 4, 600, pw=70, bc=61)              # warm (>= 65 - 5): learn
learned = (w.settings.get("warm") or {}).get("warm_learn") or []
check("warm by: warm-up learned", len(learned) == 1 and learned[0]["from_c"] == 30)
check("warm by: lighting time and °C at RUNNING learned", learned and 39 < learned[0].get("ign", 0) < 42
      and learned[0].get("run_c") == 54)
check("warm by: minutes per °C after RUNNING learned", learned and 0.5 < learned[0].get("kh", 0) < 1.5)
check("warm by: the learned lighting time lengthens the next lead", w.lead_min(w.cfg(), bc=30) >= 45)
runb(w, mod, 0, 1200, bc=55)                    # stops; the lead changes -> start time moves, same day: no 2nd start
check("warm by: no second start the same morning", sent.count(("start", "warm mode")) == 1)
CLK.t = datetime.datetime(2026, 11, 14, 1, 0).timestamp()
w, mod, sent, ev = setup(periods=PLAN, pause_h=0, min_boiler_c=30)
runb(w, mod, 0, 600, bc=35)
check("never colder than 30: nothing while 35 °C", sent == [])
runb(w, mod, 0, 60, bc=29)
check("never colder than 30: start at 29 °C", sent == [("start", "warm mode")])
runb(w, mod, 3, 300, bc=31); runb(w, mod, 4, 1500, pw=35, bc=45)
check("…then the normal low-power stop", sent[-1] == ("stop", "warm mode (low power)"))
s3 = F.Settings(tempfile.mkdtemp(), lambda *a: None)
check("warmby kind accepted", s3.update({"warm": {"periods": WB}})[0])

# ---------------- a Just start with its own "stop at" power (e.g. a short heat-up that stops at 50 %)
J = [{"name": "Start 09:00", "start": "09:00", "end": "09:30", "days": list(range(7)), "kind": "start", "stop_pct": 50}]
check("period stop at 50 % accepted", s3.update({"warm": {"periods": J}})[0] and s3.get("warm")["periods"][0]["stop_pct"] == 50)
check("period stop at 5 % refused", not s3.update({"warm": {"periods": [dict(J[0], stop_pct=5)]}})[0])
check("stop at not kept on a Keep warm period", s3.update({"warm": {"periods": [dict(J[0], kind="keep", stop_pct=50)]}})[0]
      and "stop_pct" not in s3.get("warm")["periods"][0])
CLK.t = datetime.datetime(2026, 11, 14, 8, 59).timestamp()
w, mod, sent, ev = setup(periods=J, pause_h=0)
runb(w, mod, 0, 90, bc=40)
check("09:00 start sent", sent == [("start", "warm mode")] and w.feed()["burn_stop"] == 50)
runb(w, mod, 3, 600, bc=45)
runb(w, mod, 4, 1500, pw=48, bc=60)                 # 48 % > normal 40 % but <= this burn's 50 %
check("this burn stops at its own 50 %", sent[-1] == ("stop", "warm mode (low power)"))
runb(w, mod, 0, 120, bc=60)
check("after the stop the burn's limit is gone", w.feed()["burn_stop"] is None)
CLK.t = datetime.datetime(2026, 11, 14, 11, 0).timestamp()
n0 = len(sent)
runb(w, mod, 4, 1500, pw=48, bc=60)                 # a later burn not started by the period: normal 40 %
check("other burns keep the normal 40 %", len(sent) == n0)

# learned warm-ups survive a restart (settings reloaded from the file)
d_ = tempfile.mkdtemp()
s4 = F.Settings(d_, lambda *a: None)
s4.update({"warm": {"enabled": True, "periods": WB}})
s4.set_internal("warm", "warm_learn", [{"epoch": 1, "from_c": 25, "min": 47, "k": 1.0, "k1": 0.8, "k2": 1.4}])
s4.update({"ash": {"emptied": True, "bin_kg": 12}})
s5 = F.Settings(d_, lambda *a: None)
check("restart keeps learned warm-ups", (s5.get("warm").get("warm_learn") or [{}])[0].get("k2") == 1.4)
check("restart keeps the ash bin (emptied time, size)", s5.get("ash").get("emptied_at") and s5.get("ash").get("bin_kg") == 12)

# off = nothing; bad settings refused
CLK.t = datetime.datetime(2026, 11, 14, 5, 0).timestamp()
w, mod, sent, ev = setup(enabled=False)
run(w, mod, 0, 120)
check("mode off: no command", sent == [])
s = F.Settings(tempfile.mkdtemp(), lambda *a: None)
check("bad boost time refused", not s.update({"warm": {"boosts": ["25:00"]}})[0])
check("bad stop % refused", not s.update({"warm": {"stop_pct": 5}})[0])
print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
