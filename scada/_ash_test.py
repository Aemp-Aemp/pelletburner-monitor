"""Offline test: ash-plate switch watcher (field 012 din2: 0 home, 1 out)."""
import sys, json, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burner_features as F

FAILED = 0


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


def run(rows):
    ev = []
    a = F.AshPlate(ev.append, lambda *x: None)
    a.HOME_VALUE = 0          # these cases are written with 0 = home; one case below checks the real 1 = home
    for r in rows:
        a.on_row(r)
    return a, [e["kind"] for e in ev], ev


def seq(spec, t0=1000.0, step=10):
    """spec: list of (state, alarm, din2, count)"""
    rows, t = [], t0
    for st, al, c, n in spec:
        for _ in range(n):
            rows.append({"epoch": t, "state": st, "alarm": al, "din2": c})
            t += step
    return rows


# normal start: home, one turn in OPSTART, home again -> no warnings
a, k, _ = run(seq([(0, 0, 0, 5), (2, 0, 1, 1), (3, 0, 0, 5), (4, 0, 0, 20)]))
check("normal start: no warning", k == [])
check("normal start: one turn counted", a.feed()["turns_24h"] >= 0 and len(a.turns) == 1 and a.feed()["home"])

# stuck out (dummy-board case): out for > 90 s, no alarm yet -> one stuck_out warning
a, k, _ = run(seq([(0, 0, 0, 2), (2, 0, 1, 15)]))
check("stuck out > 90 s warns once", k == ["stuck_out"])
check("problem shown in feed", a.feed()["problem"]["kind"] == "stuck_out" and a.feed()["out_s"] > 90)

# alarm 6 while the plate is out
a, k, ev = run(seq([(0, 0, 0, 2), (2, 0, 1, 2), (6, 6, 1, 2)]))
check("alarm 6 while out -> alarm_out", k == ["alarm_out"] and ev[0]["p"]["alarm"] == 6)

# alarm 6, plate never left home
a, k, _ = run(seq([(0, 0, 0, 3), (2, 0, 0, 2), (6, 6, 0, 2)]))
check("alarm 6, no turn -> alarm_no_turn", k == ["alarm_no_turn"])

# other alarm shortly after a turn, plate home again
a, k, _ = run(seq([(0, 0, 0, 2), (2, 0, 1, 1), (3, 0, 0, 3), (6, 4, 0, 2)]))
check("alarm soon after a turn -> alarm_after_turn", k == ["alarm_after_turn"])

# home again clears the problem
a, k, _ = run(seq([(0, 0, 0, 2), (2, 0, 1, 15), (3, 0, 0, 2)]))
check("back home clears the problem", a.feed()["problem"] is None and a.feed()["home"])

# a long data gap while out does not count as stuck
rows = seq([(0, 0, 0, 2), (2, 0, 1, 2)]) + seq([(2, 0, 1, 2)], t0=5000)
a, k, _ = run(rows)
check("data gap is not counted as stuck", k == [])

# three starts without a visible turn -> soft warning, once
spec = []
for _ in range(3):
    spec += [(0, 0, 0, 3), (2, 0, 0, 2), (3, 0, 0, 5), (4, 0, 0, 15), (1, 0, 0, 3)]
spec += [(0, 0, 0, 3), (2, 0, 0, 2), (3, 0, 0, 2)]
a, k, _ = run(seq(spec))
check("3 starts without a turn -> no_turns once", k == ["no_turns"])

# fast data (1 s): a 9-s turn is measured, rate is seen
a, k, _ = run(seq([(0, 0, 0, 10), (2, 0, 1, 9), (2, 0, 0, 5)], step=1))
check("fast data: turn time measured", a.feed()["last_out_s"] == 9 and a.feed()["rate_s"] <= 1.5 and k == [])

# motor test from the screen (boiler STOPPET): switch pulses 1/0 several times -> every change recorded
a, k, _ = run(seq([(0, 0, 0, 5), (0, 0, 1, 2), (0, 0, 0, 2), (0, 0, 1, 2), (0, 0, 0, 2), (0, 0, 1, 1), (0, 0, 0, 5)], step=1))
e = a.feed()["edges"]
check("motor test: 6 switch changes recorded", len(e) == 6 and [x["v"] for x in e] == [1, 0, 1, 0, 1, 0])
check("motor test: 3 pulses counted, no warning", len(a.turns) == 3 and k == [])

# turn times saved to disk + slow-turn warning (wear)
import tempfile
dd = tempfile.mkdtemp()
ev3 = []
c = F.AshPlate(ev3.append, lambda *x: None, dd); c.HOME_VALUE = 0
rows, t = [], 1000.0
for n in range(45):
    out = 6 if n < 35 else 10                      # 35 normal 6-s turns, then 10 slow 10-s turns
    for v, k in ((0, 20), (1, out)):
        for _ in range(k):
            rows.append({"epoch": t, "state": 0, "alarm": 0, "din2": v}); t += 1
rows.append({"epoch": t, "state": 0, "alarm": 0, "din2": 0})
for r in rows: c.on_row(r)
check("turn times kept", len(c.feed()["turn_hist"]) == 45 and c.feed()["turn_hist"][0][1] == 6)
check("slow turns warned once", [e["kind"] for e in ev3] == ["slow_turns"] and ev3[0]["p"]["was"] == 6)
c2 = F.AshPlate(lambda e: None, lambda *x: None, dd)
check("turn times reloaded from disk", len(c2.feed()["turn_hist"]) == 45)

# after a restart the 24-h turn count and the last turn come back from the saved history
import time as _t
dd2 = tempfile.mkdtemp()
now = _t.time()
with open(os.path.join(dd2, "ash_turns.jsonl"), "w", encoding="utf-8") as fh:
    for ep, s in ((now - 90000, 7), (now - 7200, 6), (now - 3600, 8)):   # one older than 24 h
        fh.write(json.dumps({"epoch": ep, "s": s, "state": 2}) + "\n")
    fh.write("not json\n")
c3 = F.AshPlate(lambda e: None, lambda *x: None, dd2)
f3 = c3.feed()
check("restart: turns in 24 h restored (2, the old one left out)", f3["turns_24h"] == 2)
check("restart: last turn restored", abs(f3["last_turn"] - (now - 3608)) < 1 and f3["last_out_s"] == 8)
check("restart: no saved history -> 0 turns", F.AshPlate(lambda e: None, lambda *x: None, tempfile.mkdtemp()).feed()["turns_24h"] == 0)

# thinning in the responder: 1-s rows -> ~1 kept per 10 s, plus every ash/state change
import burner_responder as R
m = R.Module(0x01)
m.thin = True
rows = seq([(4, 0, 0, 30), (4, 0, 1, 3), (4, 0, 0, 30)], step=1)
kept = [r for r in rows if m.keep_row(r)]
check("thinning keeps ~1 per 10 s", 6 <= len(kept) <= 10)
check("thinning keeps both ash-plate changes", sum(1 for x, y in zip(kept, kept[1:]) if x["din2"] != y["din2"]) == 2)
m2 = R.Module(0x01)
m2.thin = True
rows10 = seq([(4, 0, 0, 20)], step=10)
check("10-s data: every row kept (unchanged behaviour)", all(m2.keep_row(r) for r in rows10))

# HOME_VALUE can be flipped if a burner is wired the other way
ev2 = []; b = F.AshPlate(ev2.append, lambda *x: None); b.HOME_VALUE = 1
for r in seq([(0, 0, 1, 10), (2, 0, 0, 6), (3, 0, 1, 10)], step=1): b.on_row(r)
check("1 = home: one turn, home at the end, no warning", len(b.turns) == 1 and b.feed()["home"] and b.feed()["contact"] == 1 and b.feed()["last_out_s"] == 6 and not ev2)

# missing / odd values are ignored
a, k, _ = run([{"epoch": 1, "state": 0, "alarm": 0}, {"epoch": 2, "state": 0, "alarm": 0, "din2": None}])
check("no din2 -> nothing", k == [] and a.feed()["contact"] is None)

# optional replay of a local history log (only if the file exists; never in the repo)
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "hist-replay.jsonl")   # optional local replay, skipped if absent
if os.path.exists(p):
    rows = [json.loads(l) for l in open(p) if l.strip()]
    a, k, ev = run(rows)
    print("   real log warnings:", [(e["kind"], e["t"][11:19]) for e in ev])
    check("real log: dummy's stuck plate is reported", "stuck_out" in k or "alarm_out" in k)
    check("real log: turns seen on the real burner", len(a.turns) >= 2)
    check("real log: ends with the plate home and no open problem", a.feed()["home"] and a.feed()["problem"] is None)

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
