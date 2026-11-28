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
    # 1) too few starts (2; 3 that lit are enough for time to flame) -> learning, never a false alarm
    o = F.Optimiser(st, None, Mod([rec(i, 4.5, 150) for i in range(2)]), log)
    h = o.health(days=[])
    check("few starts -> learning", h["status"] == "learning")
    # 2) steady burner -> ok
    o.mod = Mod([rec(i, 4.5 + (i % 2) * 0.2, 150) for i in range(10)])
    h = o.health(days=[])
    ck = {c["k"]: c["st"] for c in h["checks"]}
    check("steady -> lighting and flame ok; the status says learning while other checks learn (no bad)",
          ck["light"] == "ok" and ck["flame"] == "ok" and h["status"] == "learning")
    # 3) lighting getting slow and flame weaker -> clean
    o.mod = Mod([rec(i, 4.5, 150) for i in range(8)] + [rec(8 + i, 7.5, 110) for i in range(3)])
    h = o.health(days=[])
    check("slower + weaker -> clean", h["status"] == "clean")
    # 4) a different burner with other numbers (8 min, flame 90) that is steady -> ok (own normal, not ours)
    o.mod = Mod([rec(i, 8.0, 90) for i in range(10)])
    ck = {c["k"]: c["st"] for c in o.health(days=[])["checks"]}
    check("other burner, own normal -> ok", ck["light"] == "ok" and ck["flame"] == "ok")
    # 4b) the status (Home Assistant, the phone's "time to clean") comes from the checklist the pages show
    real = o.checks
    for sts, want in ((["ok"] * 6, "ok"), (["ok"] * 5 + ["learn"], "learning"), (["ok", "bad", "learn"], "watch"),
                      (["bad", "ok", "bad"], "clean"), ([], "learning")):
        o.checks = lambda items, days, sts=sts: [{"k": str(i), "st": x} for i, x in enumerate(sts)]
        check(f"status from the checklist: {sts} -> {want}", o.health()["status"] == want)
    o.checks = real
    # 5) burner + fan cleaned (the dialog's default): time to flame and failed starts learn again, the flame sensor
    # items do not (the glass was not touched)
    st0 = lambda h, k: next(i for i in h["items"] if i["key"] == k)["state"]
    o.mod = Mod([rec(i, 4.5 + (i % 2) * 0.2, 150) for i in range(10)])
    mark = st.mark_cleaned(["fan", "burner"], now - 2 * 3600)
    h = o.health(days=[])
    check("burner+fan clean: parts kept in order", mark["parts"] == ["burner", "fan"]
          and h["last_clean"] == {"t": round(now - 2 * 3600), "parts": ["burner", "fan"]})
    check("burner+fan clean: time to flame and failed starts learn again",
          st0(h, "to_flame") == "learning" and st0(h, "failed") == "learning")
    check("burner+fan clean: flame at the start keeps its normal", st0(h, "start_flame") == "ok")
    ck = {c["k"]: c["st"] for c in h["checks"]}
    check("burner+fan clean: checklist light learns, flame sensor stays", ck["light"] == "learn" and ck["flame"] == "ok")
    check("burner+fan clean: the flame sensor was not reset", not st.get("lux")["reset_epoch"])
    # 5b) only the flame sensor glass: the flame items learn again, the lighting items keep going
    st.set_internal("health", "cleaned_at", None)
    st.mark_cleaned(["sensor"], now - 2 * 3600)
    check("sensor clean alone does not move cleaned_at", st.get("health")["cleaned_at"] is None)
    st.set_internal("lux", "reset_epoch", now - 2 * 3600)      # what the route's lux.reset() does
    h = o.health(days=[])
    ck = {c["k"]: c["st"] for c in h["checks"]}
    check("sensor clean: flame at the start learns again", st0(h, "start_flame") == "learning" and ck["flame"] == "learn")
    check("sensor clean: time to flame and failed starts keep their normal",
          st0(h, "to_flame") == "ok" and st0(h, "failed") == "ok" and ck["light"] == "ok")
    check("sensor clean: marks say what was cleaned",
          [m["parts"] for m in st.clean_marks()] == [["burner", "fan"], ["sensor"]])
    st.set_internal("lux", "reset_epoch", 0)
    st.set_internal("health", "marks", [])
    st.set_internal("health", "cleaned_at", now - 2 * 3600)
    check("after cleaning -> learning (lighting items)", st0(o.health(days=[]), "to_flame") == "learning")
    # 6) cleaned_at survives a restart
    check("cleaned_at saved", F.Settings(d, log).get("health")["cleaned_at"] is not None)
    # 6b) an old settings file: cleaned_at without parts loads, and counts as burner + fan
    d2 = tempfile.mkdtemp()
    try:
        with open(os.path.join(d2, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"health": {"cleaned_at": 1_700_000_000}}, f)
        old = F.Settings(d2, log)
        check("old cleaned_at loads", old.get("health")["cleaned_at"] == 1_700_000_000)
        check("old cleaned_at = burner + fan", old.clean_marks() == [{"t": 1_700_000_000, "parts": ["burner", "fan"]}])
        check("old cleaned_at in the change list", F.change_points([], (), (), old.clean_marks())[0]["parts"]
              == [{"k": "cleaned", "what": ["burner", "fan"]}])
        old.mark_cleaned(["sensor"], 1_700_100_000)
        check("a new mark keeps the old one", [m["parts"] for m in F.Settings(d2, log).clean_marks()]
              == [["burner", "fan"], ["sensor"]])
        with open(os.path.join(d2, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"health": {"cleaned_at": 5, "marks": [{"t": 9, "parts": ["boiler"]}, "x",
                                                             {"t": 7, "parts": ["fan", "oven"]}]}}, f)
        check("odd marks in the file are left out", F.Settings(d2, log).clean_marks() == [{"t": 7, "parts": ["fan"]}])
    finally:
        shutil.rmtree(d2, ignore_errors=True)
    # 7) running flame per power: last 2 days dimmer than the 2 before
    day = lambda n, fl: {"day": f"2026-11-{n:02d}", "running_s": 7200, "flame_power": {"50": [3600, fl * 3600, 3600, 0, 0]}}
    st.set_internal("health", "cleaned_at", None)
    o.mod = Mod([])
    o.report_days = lambda: [day(1, 150), day(2, 150), day(3, 120), day(4, 118)]
    h = o.health(days=[])                          # health always takes its own days (report_days)
    rf = [i for i in h["items"] if i["key"] == "run_flame"][0]
    check("running flame drop seen", rf["state"] == "worse" and rf["normal"] == 150)
finally:
    shutil.rmtree(d, ignore_errors=True)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
