"""The monitor starts while the boiler is already in ALARM: the alarm must still be logged (once)."""
import sys, os, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_ext as X
import burner_features as F

res = []
def check(name, cond): res.append(("PASS" if cond else "FAIL", name))

d = tempfile.mkdtemp()
store = X.Store(d, 30, lambda m: None)
t0 = time.time() - 600
evs = []

def tracker():
    tr = X.Tracker(store)
    tr.listeners.append(evs.append)
    return tr

def feed(tr, t, st, al):
    r = {"epoch": t, "state": st, "alarm": al}
    tr.on_row(r, [r])

# 1) start in ALARM 15: one transition into ALARM + one alarm event, marked, nothing invented before it
tr = tracker()
feed(tr, t0, 6, 15)
feed(tr, t0 + 10, 6, 15)
al = [e for e in evs if e["type"] == "alarm"]
tn = [e for e in evs if e["type"] == "transition"]
check("alarm event logged at start", len(al) == 1 and al[0]["alarm"] == 15 and al[0].get("at_start"))
check("transition into ALARM logged at start", len(tn) == 1 and tn[0]["to"] == 6 and tn[0]["from"] is None
      and tn[0].get("at_start") and tn[0]["alarm"] == 15)
check("no invented 'before' data", tr.prealarm["state_before"] is None and tr.prealarm["last_ok_row"] is None
      and tr.prealarm.get("at_start") and tr.prealarm["alarm"] == 15)
check("in the feed transitions", tr.feed()["transitions"][-1]["to"] == 6)
check("saved to the event file", sum(e.get("type") == "alarm" for e in store.load_events(300)) == 1)

# 2) monitor restarts again, still the same alarm: no duplicate
evs.clear()
tr2 = tracker()
feed(tr2, t0 + 60, 6, 15)
check("no duplicate after a restart in the same alarm", evs == [])

# 3) the alarm clears: the normal transition out of ALARM is logged
feed(tr2, t0 + 70, 0, 0)
check("clear logged normally", [(e["type"], e.get("from"), e.get("to")) for e in evs] == [("transition", 6, 0)])

# 4) link gap while still in the same alarm: no duplicate; gap with another code: logged once
evs.clear()
tr3 = X.Tracker(None); tr3.listeners.append(evs.append)
feed(tr3, t0, 4, 0)
feed(tr3, t0 + 10, 6, 4)
n = len([e for e in evs if e["type"] == "alarm"])
feed(tr3, t0 + 400, 6, 4)                       # gap, same alarm
check("no duplicate after a link gap", len([e for e in evs if e["type"] == "alarm"]) == n == 1)
feed(tr3, t0 + 900, 6, 15)                      # gap, a different alarm is now showing
feed(tr3, t0 + 910, 6, 15)
a2 = [e for e in evs if e["type"] == "alarm"]
check("new alarm code after a link gap logged once", len(a2) == 2 and a2[-1]["alarm"] == 15 and a2[-1].get("at_start"))

# 5) normal path untouched: start OK, then alarm -> transition with a real 'from' + alarm event
evs.clear()
tr4 = X.Tracker(None); tr4.listeners.append(evs.append)
feed(tr4, t0, 4, 0)
check("start while running: nothing logged", evs == [])
feed(tr4, t0 + 10, 6, 2)
check("normal alarm keeps its 'from'", evs[0]["from"] == 4 and not evs[0].get("at_start")
      and evs[1]["type"] == "alarm" and evs[1]["state_before"] == 4)

# 6) auto alarm reset leaves an alarm seen at start alone (the cause is not known)
s = F.Settings(tempfile.mkdtemp(), lambda m: None)
s.update({"auto_reset": {"enabled": True, "alarms": [4, 5, 6], "max_per_day": 2, "wait_min": 1}})
tr5 = X.Tracker(None)
feed(tr5, time.time(), 6, 4)
mod = type("M", (), {"latest": {"epoch": time.time()}, "remote_armed": True, "last_rx": 0, "latest_cfg": {}})()
sent = []
ar = F.AutoReset(s, tr5, mod, lambda c, src: sent.append(c), lambda m: None, lambda e: None)
ar.tick()
check("auto reset does not act on an alarm seen at start", sent == [] and ar.status.get("code") == "not_burning")

for r in res:
    print(*r)
fails = [r for r in res if r[0] != "PASS"]
print(f"{len(res) - len(fails)}/{len(res)} passed")
sys.exit(1 if fails else 0)

