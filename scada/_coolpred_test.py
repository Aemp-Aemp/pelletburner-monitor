"""Warm-by start predicted from how the boiler cools + lighting time without the PAUSESTOP wait.
Run: py scada\\_coolpred_test.py"""
import os, sys, time, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F

fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1

class Mod:   # warm now (62 C): below the RUNNING temperature the start temperature no longer changes the lead
    latest = {"fields": {"boiler_c": 62.0, "state": 0}}
    latest_cfg = {"fields": {"setpoint_c": 65}}

wm = F.WarmMode.__new__(F.WarmMode)
wm.mod = Mod()
w = {"warm_learn": []}
check("cooling with 4 h default: 49 C -> ~23 C after 7,5 h", 21 < wm.predict_bc(w, 49.0, 7.5) < 26)
now = time.mktime((2000, 1, 1, 20, 10, 0, 0, 0, -1))           # fixed clock: the result must not depend on when the test runs
lead_now = wm.lead_min(w)                                   # from 62 C as it is now
lead_pred = wm.lead_for(w, now + 8 * 3600, now)            # start in the night: the boiler will be colder by then
check("evening estimate uses the colder morning boiler (longer lead)", lead_pred > lead_now)
check("learned cooling replaces the default", wm.cool_tau_h({"cool_learn": [{"tau_h": 3.0}, {"tau_h": 3.4}]}) == 3.2)
_tg = time.localtime(now + 8 * 3600)
_pl = {"plan": [{"warmby": True, "target": "%02d:%02d" % (_tg.tm_hour, _tg.tm_min), "lead": 47},
                {"warmby": True, "target": "%02d:%02d" % ((_tg.tm_hour + 6) % 24, _tg.tm_min), "lead": 31}]}
check("plan note lead = lead of the next warm-by start (not the boiler as it is now)", wm._next_warm_lead(_pl, now) == 47)
check("no warm-by period: lead from the boiler as it is", wm._next_warm_lead(dict(w, plan=[]), now) == wm.lead_min(w))
Mod.latest = {"fields": {"boiler_c": 62.0, "state": 4}}
check("while burning: lead from the boiler as it is", wm.lead_for(w, now + 8 * 3600, now) == wm.lead_min(w))

# lighting time: the boiler waits in PAUSESTOP 20 min first -> not counted as lighting
class Tracker:
    listeners = []
st = F.Settings(tempfile.mkdtemp(), lambda *a: None)
ig = F.IgnitionLog(st, Tracker(), type("M", (), {"latest": {"fields": {}}, "latest_cfg": {}})(), None, lambda *a: None)
t0 = 1_000_000.0
ig._on_event({"type": "transition", "from": 0, "to": 2, "epoch": t0})
ig._on_event({"type": "transition", "from": 2, "to": 5, "epoch": t0 + 18})
ig.on_row({"epoch": t0 + 600, "state": 5, "flame": 3})              # glow while waiting: not a new flame
ig._on_event({"type": "transition", "from": 5, "to": 3, "epoch": t0 + 1243})
ig.on_row({"epoch": t0 + 1243 + 300, "state": 3, "flame": 40})       # flame 5 min after lighting began
ig._on_event({"type": "transition", "from": 3, "to": 4, "epoch": t0 + 1243 + 960})
r = ig.records[-1]
check("time to flame counted from when lighting began (300 s)", r["first_lux_s"] == 300 and r["flame_s"] == 300)
check("wait shown separately (~20,4 min)", abs(r["waited_min"] - 20.4) < 0.2 and r["minutes"] < 17)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
