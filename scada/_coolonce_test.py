"""Warm mode: each cool-down is learned once per stop, also when the program restarts while the boiler cools.
Before, every restart measured the same cool-down again and added it to warm.cool_learn, pushing good older
nights out of the list. Fake clock, no serial. Run: py scada\\_coolonce_test.py"""
import os, sys, json, math, tempfile, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F

fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1

class Clock:
    t = 0.0
CLK = Clock()
F.time.time = lambda: CLK.t
def at(day, s):
    return datetime.datetime.strptime(f"2000-01-0{day} {s}", "%Y-%m-%d %H:%M").timestamp()

TAU = 4.0
def boiler(t, stop, c0=60.0):
    """A boiler that cools like Newton's law with TAU hours, from c0 at the stop."""
    return F.WarmMode.ROOM_C + (c0 - F.WarmMode.ROOM_C) * math.exp(-max(0.0, t - stop) / 3600 / TAU)

class Store:
    def __init__(self): self.evs, self.rows = [], []
    def _load(self, prefix, n, days=2): return list(self.evs if prefix == "events" else self.rows)
class Mod:
    remote_armed = True
    latest = None
    latest_cfg = {"raw": {}, "fields": {"setpoint_c": 65}}
class Tracker:
    listeners = []

P = [{"name": "Warm 06:30", "start": "06:30", "end": "07:00", "days": list(range(7)), "kind": "warmby"}]

def night(restarts):
    """Burn until 20:00, then cool all night until the warm-by start. Restart the program at the given times.
    Returns (commands sent with their time, the learned list)."""
    d, store, sent = tempfile.mkdtemp(), Store(), []
    s = F.Settings(d, lambda *a: None)
    assert s.update({"warm": {"enabled": True, "use_board": False, "stop_pct": 40, "pause_h": 0, "periods": P}})[0]
    stop, end = at(3, "20:00"), at(4, "07:00")
    def make():
        mod = Mod()
        mod.store = store
        w = F.WarmMode(F.Settings(d, lambda *a: None), Tracker(), mod,
                       lambda c, src: sent.append((c, CLK.t)), lambda *a: None, lambda e: None)
        return w, mod
    CLK.t = at(3, "19:50")
    w, mod = make()
    todo = sorted(restarts)
    while CLK.t < end and not sent:
        CLK.t += 30
        if todo and CLK.t >= todo[0]:
            todo.pop(0)
            w, mod = make()                                   # program restart: new objects, same settings file
        st = 4 if CLK.t < stop else 0
        if st == 0 and not any(e["to"] == 0 for e in store.evs):
            store.evs.append({"type": "transition", "from": 4, "to": 0, "epoch": CLK.t})
        bc = 60.0 if st == 4 else boiler(CLK.t, stop)
        store.rows.append({"epoch": CLK.t, "boiler_c": round(bc, 1)})
        mod.latest = {"epoch": CLK.t, "fields": {"state": st, "power_pct": 70 if st == 4 else 0, "alarm": 0,
                                                  "boiler_c": round(bc, 1)}}
        w.tick()
    return sent, F.Settings(d, lambda *a: None).get("warm")["cool_learn"]

# 1) several restarts while the boiler cools: one entry, the same as without restarts
sent0, cl0 = night([])
check("no restart: one cool-down learned", len(cl0) == 1 and abs(cl0[0]["tau_h"] - TAU) < 0.1)
check("the entry keeps its stop time", cl0[0].get("stop") is not None and abs(cl0[0]["stop"] - at(3, "20:00")) <= 60)
sent1, cl1 = night([at(3, "22:00"), at(3, "23:30"), at(4, "00:10"), at(4, "01:00"), at(4, "03:00"), at(4, "05:00")])
check("6 restarts: still one entry for the one stop", len(cl1) == 1)
check("6 restarts: the same entry as without restarts", cl1 == cl0)
check("a warm-by start happened", len(sent0) == 1 and sent0[0][0] == "start")
check("6 restarts: the same start at the same time", sent1 == sent0)

# 2) a list already holding copies (written by the old code): cleaned on load, the latest copy kept
stop = at(3, "19:32")
good = [{"epoch": at(1, "08:00") + i * 86400 - 3 * 86400, "from_c": 58.0, "to_c": 30.0, "h": 6.0,
         "tau_h": t} for i, t in enumerate([3.9, 4.1, 3.8, 4.2, 4.0])]
copies = [{"epoch": round(stop + h * 3600 + 40), "from_c": 61.5, "to_c": round(boiler(stop + h * 3600, stop, 61.5), 1),
           "h": h, "tau_h": round(3.0 + 0.02 * i, 2)} for i, h in enumerate([3.1, 4.6, 5.9, 7.2, 8.8, 10.4])]
d = tempfile.mkdtemp()
with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"warm": {"cool_learn": good + copies}}, fh)
cl = F.Settings(d, lambda *a: None).get("warm")["cool_learn"]
check("load: 6 copies of one cool-down become one", len(cl) == 6)
check("load: the good older nights are kept", cl[:5] == good)
check("load: the latest (longest) copy is kept", cl[5] == copies[-1])
check("load: a clean list stays exactly the same", F.cool_learn_dedup(good) == good)
check("old entries without 'stop': stop = end time - hours", abs(F.cool_stop(copies[0]) - stop) < 400)
check("two real nights are never merged", len(F.cool_learn_dedup([dict(good[0], stop=1000), dict(good[1], stop=1000 + 3 * 3600)])) == 2)

# 3) the warm-by start prediction: from the cleaned list the same as or closer to the real cooling than from the copies
class M:
    latest = {"fields": {"boiler_c": 55.0, "state": 0}}
    latest_cfg = {"fields": {"setpoint_c": 65}}
wm = F.WarmMode.__new__(F.WarmMode)
wm.mod = M()
base = {"warm_learn": []}
now = at(3, "21:00")
target = at(4, "06:30")
nights = good + [copies[0]]                       # what a run without restarts would have stored
buggy = (good + copies)[-10:]                     # what the old code stored: the copies push the older nights out
clean = F.cool_learn_dedup(buggy)
def lead(ls): return wm.lead_for(dict(base, cool_learn=ls), target, now)
def tau(ls): return wm.cool_tau_h({"cool_learn": ls})
true_lead = lead(nights)
check("cleaned list: cooling time as good or better", abs(tau(clean) - tau(nights)) <= abs(tau(buggy) - tau(nights)))
check("cleaned list: start time as good or better", abs(lead(clean) - true_lead) <= abs(lead(buggy) - true_lead))
check("copies did move the prediction (the bug)", tau(buggy) != tau(nights))
check("without copies: prediction exactly unchanged", lead(F.cool_learn_dedup(nights)) == true_lead
      and tau(F.cool_learn_dedup(nights)) == tau(nights))

print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
