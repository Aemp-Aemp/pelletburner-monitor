"""Burner health + ash due date: learns from the burner's own data, offline. Run: py scada\\_health_test.py"""
import os, sys, json, time, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F

log = lambda *a: None
fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1

class Ign:  # stand-in for IgnitionLog
    def __init__(self, recs): self.records = recs
class Mod:
    def __init__(self, recs): self.ignitions = Ign(recs); self.latest_cfg = {}

d = tempfile.mkdtemp()
try:
    st = F.Settings(d, log)
    now = time.time()
    def rec(i, mins, peak, res="ok"):
        return {"epoch": now - (20 - i) * 3600, "result": res, "flame_s": mins * 60 if res == "ok" else None, "peak_lux": peak}
    # 1) too few starts -> learning, never a false alarm
    o = F.Optimiser(st, None, Mod([rec(i, 4.5, 150) for i in range(3)]), log)
    h = o.health(days=[])
    check("few starts -> learning", h["status"] == "learning")
    # 2) steady burner -> ok
    o.mod = Mod([rec(i, 4.5 + (i % 2) * 0.2, 150) for i in range(10)])
    h = o.health(days=[])
    check("steady -> ok", h["status"] == "ok")
    # 3) lighting getting slow and flame weaker -> clean
    o.mod = Mod([rec(i, 4.5, 150) for i in range(8)] + [rec(8 + i, 7.5, 110) for i in range(3)])
    h = o.health(days=[])
    check("slower + weaker -> clean", h["status"] == "clean")
    # 4) a different burner with other numbers (8 min, flame 90) that is steady -> ok (own normal, not ours)
    o.mod = Mod([rec(i, 8.0, 90) for i in range(10)])
    check("other burner, own normal -> ok", o.health(days=[])["status"] == "ok")
    # 5) cleaned mark: older records are ignored -> learning again
    st.set_internal("health", "cleaned_at", now - 2 * 3600)
    check("after cleaning -> learning", o.health(days=[])["status"] == "learning")
    # 6) cleaned_at survives a restart
    check("cleaned_at saved", F.Settings(d, log).get("health")["cleaned_at"] is not None)
    # 7) running flame per power: last 2 days dimmer than the 2 before
    day = lambda n, fl: {"day": f"2026-11-{n:02d}", "running_s": 7200, "flame_power": {"50": [3600, fl * 3600, 3600, 0, 0]}}
    st.set_internal("health", "cleaned_at", None)
    o.mod = Mod([])
    h = o.health(days=[day(1, 150), day(2, 150), day(3, 120), day(4, 118)])
    rf = [i for i in h["items"] if i["key"] == "run_flame"][0]
    check("running flame drop seen", rf["state"] == "worse" and rf["normal"] == 150)
finally:
    shutil.rmtree(d, ignore_errors=True)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
