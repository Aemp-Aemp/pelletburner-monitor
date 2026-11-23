"""A program restart inside a heating-plan window must not start the boiler again for a window already served
(burning at the window start, then stopped by the period's own limit). Fake clock, no serial.
Run: py scada\\_warm_restart_test.py"""
import os, sys, tempfile, datetime
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
def at(s):
    return datetime.datetime.strptime("2000-01-03 " + s, "%Y-%m-%d %H:%M:%S").timestamp()

class Store:
    def __init__(self, evs): self.evs = evs
    def _load(self, prefix, n, days=2): return list(self.evs) if prefix == "events" else []
class Mod:
    remote_armed = True
    latest = None
    latest_cfg = {"raw": {}, "fields": {"setpoint_c": 65}}
    store = None
class Tracker:
    listeners = []

J = [{"name": "Start 09:00", "start": "09:00", "end": "09:30", "days": list(range(7)), "kind": "start", "stop_pct": 50}]

def make(d, evs=None, new=True):
    s = F.Settings(d, lambda *a: None)
    if new:
        assert s.update({"warm": {"enabled": True, "use_board": False, "stop_pct": 40, "pause_h": 0, "periods": J}})[0]
    mod, sent, ev = Mod(), [], []
    mod.store = Store(evs) if evs is not None else None
    w = F.WarmMode(s, Tracker(), mod, lambda c, src: sent.append((c, src)), lambda *a: None, ev.append)
    return w, mod, sent, ev

def run(w, mod, st, secs, pw=0, bc=60, step=5):
    for _ in range(int(secs / step)):
        CLK.t += step
        mod.latest = {"epoch": CLK.t, "fields": {"state": st, "power_pct": pw, "alarm": 0, "boiler_c": bc}}
        w.tick()

def burn_then_stop(d):
    """Burning at 09:00, the period's 50 % limit applies, the web stops it at low power, the boiler goes STOPPET."""
    CLK.t = at("08:58:00")
    w, mod, sent, ev = make(d)
    run(w, mod, 4, 150, pw=70)                       # burning when the window opens
    run(w, mod, 4, 180, pw=48)                       # 48 % <= the period's 50 %: stop after 2 min
    stop_t = next((e["epoch"] for e in ev if e.get("type") == "warm" and e.get("what") == "stop"), None)
    run(w, mod, 0, 30)
    evs = [e for e in ev if e.get("type") == "warm"] + [{"type": "transition", "from": 4, "to": 0, "epoch": stop_t + 20}]
    return sent, evs

# 1) today's case: burning at the window start -> stopped by the period limit -> restart -> no new start
d = tempfile.mkdtemp()
sent, evs = burn_then_stop(d)
check("stopped by the period's own 50 % limit", sent == [("stop", "warm mode (low power)")])
CLK.t = at("09:04:00")
w, mod, sent2, _ = make(d, evs, new=False)
run(w, mod, 0, 25 * 60, bc=55)
check("restart inside the window: no new start", sent2 == [])

# 1b) each guard on its own: the window kept in settings (no event log) ...
CLK.t = at("09:04:00")
w, mod, sent2, _ = make(d, None, new=False)
run(w, mod, 0, 25 * 60, bc=55)
check("restart: the served window is kept in settings", sent2 == [] and w.done_window and w.done_window.endswith("#09:00"))
# ... and our own stop inside the window (settings without the served window, e.g. an older file)
d2 = tempfile.mkdtemp()
CLK.t = at("09:04:00")
w, mod, sent2, _ = make(d2, evs)
run(w, mod, 0, 25 * 60, bc=55)
check("restart: our stop inside the window counts as served", sent2 == [])

# 2) window not served yet -> restart -> the normal start still happens
d3 = tempfile.mkdtemp()
CLK.t = at("08:55:00")
w, mod, sent, _ = make(d3, [])
run(w, mod, 0, 240, bc=40)
check("before the window: nothing", sent == [])
CLK.t = at("08:59:30")
w, mod, sent, _ = make(d3, [], new=False)                    # restart just before 09:00
run(w, mod, 0, 90, bc=40)
check("restart before the window: start at 09:00", sent == [("start", "warm mode")])
d4 = tempfile.mkdtemp()
CLK.t = at("09:02:00")
w, mod, sent, _ = make(d4, [{"type": "transition", "from": 4, "to": 0, "epoch": at("07:00:00")}])
run(w, mod, 0, 30, bc=40)
check("restart inside an unserved window (stopped before it): start", sent == [("start", "warm mode")])

# 3) a stop that only ends the boiler's own WAITING is part of a start, not "served"
d5 = tempfile.mkdtemp()
evs5 = [{"type": "warm", "what": "stop", "why": "end the boiler's own pause (plan: Start 09:00)", "epoch": at("09:00:05")}]
CLK.t = at("09:00:30")
w, mod, sent, _ = make(d5, evs5)
run(w, mod, 0, 30, bc=40)
check("restart after ending WAITING: the start still follows", sent == [("start", "warm mode")])

# 4) yesterday's served window does not block today's
d6 = tempfile.mkdtemp()
s = F.Settings(d6, lambda *a: None)
s.set_internal("warmstate", "done_window", "2000-01-02#09:00")
CLK.t = at("08:59:30")
w, mod, sent, _ = make(d6, [], new=True)
run(w, mod, 0, 60, bc=40)
check("yesterday's served window: today's start happens", sent == [("start", "warm mode")])
check("not editable through the settings API", not F.Settings(d6, lambda *a: None).update({"warmstate": {"done_window": "x"}})[0]
      or F.Settings(d6, lambda *a: None).get("warmstate")["done_window"] != "x")

print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
