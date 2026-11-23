"""No web pause just before any period. Run: py scada\\_keep_soon_test.py"""
import os, sys, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F

fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1

wm = F.WarmMode.__new__(F.WarmMode)
keep = {"name": "Showers & dinner", "start": "17:15", "end": "19:30", "days": list(range(7)), "kind": "keep", "on": True}
start = {"name": "Start 13:00", "start": "13:00", "end": "13:30", "days": list(range(7)), "kind": "start", "on": True}
w = {"plan": [start, keep]}
at = lambda h, m: datetime.datetime(2026, 11, 14, h, m).timestamp()
check("17:04 -> Showers & dinner starts within 30 min", (wm.period_soon(w, at(17, 4)) or {}).get("name") == "Showers & dinner")
check("16:40 -> 35 min away, normal web pause", wm.period_soon(w, at(16, 40)) is None)
check("12:45 -> a Just start period counts too", (wm.period_soon(w, at(12, 45)) or {}).get("name") == "Start 13:00")
check("13:10 -> inside a period: not soon", wm.period_soon(w, at(13, 10)) is None)
check("switched-off period does not count", wm.period_soon({"plan": [dict(keep, on=False)]}, at(17, 4)) is None)
over = dict(keep, start="23:50", end="01:00")
check("period across midnight seen", wm.period_soon({"plan": [over]}, at(23, 30)) is not None)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
