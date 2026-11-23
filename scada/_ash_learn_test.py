"""Ash share learned from "how full was it" at Just emptied. Run: py scada\\_ash_learn_test.py"""
import os, sys, time, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F

fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1

d = tempfile.mkdtemp()
try:
    st = F.Settings(d, lambda *a: None)
    o = F.Optimiser(st, None, None, lambda *a: None)
    o.burned_since = lambda at: 150.0                       # 150 kg pellets since the last emptying
    check("no learning without an earlier emptying", o.learn_ash(1) is None)
    st.update({"ash": {"emptied": True, "bin_kg": 1.2}})
    check("half full of 1,2 kg after 150 kg pellets = 0,4 %", round(o.learn_ash(0.5), 2) == 0.4)
    check("ash share now 0,4 %", st.get("ash")["ash_pct"] == 0.4)
    check("'forgot last time' learns nothing", o.learn_ash(None) is None and len(st.get("ash")["learn"]) == 1)
    o.burned_since = lambda at: 10.0
    check("too few pellets since: nothing learned", o.learn_ash(1) is None)
    o.burned_since = lambda at: 150.0
    o.learn_ash(1); o.learn_ash(0.75)                       # 0,8 % and 0,6 %
    check("median of the learned values (0,4 / 0,8 / 0,6 -> 0,6)", st.get("ash")["ash_pct"] == 0.6)
    s2 = F.Settings(d, lambda *a: None)
    check("learned values survive a restart", len(s2.get("ash")["learn"]) == 3 and s2.get("ash")["ash_pct"] == 0.6)
finally:
    shutil.rmtree(d, ignore_errors=True)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
