"""After a program restart the heating plan remembers its own last stop (page keeps "Web pause"). Run: py scada\\_warm_restore_test.py"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F

fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1

class Store:
    def __init__(self, evs): self.evs = evs
    def _load(self, prefix, n, days=2): return list(self.evs)
class Mod:
    def __init__(self, evs): self.store = Store(evs)

now = time.time()
def wm(evs):
    w = F.WarmMode.__new__(F.WarmMode)
    w.mod, w.rest_since = Mod(evs), None
    w._restore_rest()
    return w.rest_since
stop = {"type": "warm", "what": "stop", "epoch": now - 3600}
check("our stop still in force -> remembered", wm([stop, {"type": "transition", "to": 0, "epoch": now - 3590}]) == now - 3600)
check("started again since -> forgotten", wm([stop, {"type": "transition", "to": 2, "epoch": now - 600}]) is None)
check("no stop -> nothing", wm([{"type": "transition", "to": 0, "epoch": now - 60}]) is None)
check("very old stop -> nothing", wm([{"type": "warm", "what": "stop", "epoch": now - 3 * 86400}]) is None)
def wc(evs):
    w = F.WarmMode.__new__(F.WarmMode)
    w.mod, w.rest_since = Mod(evs), None
    w._restore_rest()
    return getattr(w, "cool", None)
stopT = {"type": "transition", "from": 4, "to": 0, "epoch": now - 3600}
row = {"epoch": now - 3590, "boiler_c": 61.5}
check("cooling start restored after a restart", wc([stopT, row]) == {"t": now - 3600, "bc": 61.5})
check("cooling start: started again since -> none", wc([stopT, row, {"type": "transition", "from": 0, "to": 2, "epoch": now - 60}]) is None)
check("cooling start: no boiler reading -> none", wc([stopT]) is None)
check("cooling start: too long ago -> none", wc([dict(stopT, epoch=now - 13 * 3600), dict(row, epoch=now - 13 * 3600 + 5)]) is None)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
