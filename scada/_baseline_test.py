"""Offline test (synthetic data, year 2000): when the boiler's type setting (cfg 033) changes, the "usual" lighting
numbers start again from then (typical time to flame, health, Discoveries, warm-by lead), with older starts used
until 3 new ones lit. And pellet batch names that differ only in case / spaces are one batch."""
import datetime, json, os, sys, tempfile, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import burner_features as F
import insights_gen as G

FAILED = 0
log = lambda *a: None


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


T0 = datetime.datetime(2000, 1, 10, 6, 0).timestamp()
CHG = T0 + 10 * 86400                                   # the boiler type changes on day 10

# ---- the cfg read: the first value is only remembered, a change starts a new baseline (kept in the file)
tmp = tempfile.mkdtemp(prefix="baseline_test_")
s = F.Settings(tmp, log)
check("first boiler type seen: no new baseline", s.note_boiler_type(0, now=T0) is False and F.baseline_since(s) is None)
check("same type again: nothing", s.note_boiler_type("0", now=T0 + 60) is False and F.baseline_since(s) is None)
check("type 0 -> 1: new baseline from then", s.note_boiler_type(1, now=CHG) is True and F.baseline_since(s) == CHG
      and s.get("baseline")["why"] == "boiler type changed")
check("not a number: ignored", s.note_boiler_type(None) is False and F.baseline_since(s) == CHG)
s2 = F.Settings(tmp, log)
check("kept after a restart", F.baseline_since(s2) == CHG and s2.get("baseline")["kedel"] == 1)
check("not editable from the web", not s2.update({"baseline": {"since": 1}})[0] or F.baseline_since(s2) == CHG)


def start(t, mins, batch="—"):
    return {"epoch": t, "result": "ok", "minutes": mins + 20, "ignition_min": mins + 15, "relight": False,
            "flame_s": round(mins * 60), "peak_lux": 150, "batch": batch, "cfg": {}}


old = [start(T0 + i * 86400, 6.0) for i in range(8)]
new = [start(CHG + 3600 + i * 3 * 3600, 3.0) for i in range(3)]


def report(recs, sets):
    ig = F.IgnitionLog(sets, types.SimpleNamespace(listeners=[]), None, None, log)
    ig.records = list(recs)
    return ig.report()


F.time.time = lambda: CHG + 86400
r = report(old + new[:2], s)
check("2 starts since the change: typical from older starts, and it says so",
      r["typical_flame_min"] == 6.0 and r["baseline"] == {"since": CHG, "why": "boiler type changed", "use": False, "old": True})
r = report(old + new, s)
check("3 starts since the change: typical from them only",
      r["typical_flame_min"] == 3.0 and r["typical_flame_n"] == 3 and r["baseline"]["use"] and not r["baseline"]["old"])
check("old starts are still in the list", len(r["records"]) == 11)
r = report(old + new, F.Settings(tempfile.mkdtemp(), log))
check("no type change: no baseline note, all starts", "baseline" not in r and r["typical_flame_min"] == 6.0)

# ---- burner health: lighting judged against the starts since the change
mod = types.SimpleNamespace(ignitions=types.SimpleNamespace(records=old + [start(CHG + 3600 + i * 3600, 3.0 + 0.1 * i) for i in range(6)]),
                            latest_cfg={})
tf = next(i for i in F.Optimiser(s, None, mod, log).health(days=[])["items"] if i["key"] == "to_flame")
check("health: usual time to flame from the starts since the change", tf["state"] == "ok" and tf["normal"] == 3.25
      and tf["normal"] == report(mod.ignitions.records, s)["typical_flame_min"])
mod.ignitions.records = old + [start(CHG + 3600, 9.0), start(CHG + 7200, 9.0)]
tf = next(i for i in F.Optimiser(s, None, mod, log).health(days=[])["items"] if i["key"] == "to_flame")
check("health: 2 starts since the change: judged against the older starts", tf["normal"] == 6.0 and tf["state"] == "worse"
      and tf["normal"] == report(mod.ignitions.records, s)["typical_flame_min"])

# ---- Discoveries: the same typical, and it says when it is from older starts
d = tempfile.mkdtemp(prefix="baseline_disc_")
with open(os.path.join(d, "ignitions.jsonl"), "w", encoding="utf-8") as fh:
    fh.write("".join(json.dumps(x) + "\n" for x in old + new[:2]))
with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"baseline": {"since": CHG, "why": "boiler type changed", "kedel": 1}}, fh)
G.DATA = d
card = G.idea_ignition_times([])
check("Discoveries: older starts, said in both languages",
      "typical 6.0 min (based on older starts" in card["text"]["en"] and "typisk 6,0 min (ud fra ældre starter" in card["text"]["da"])
with open(os.path.join(d, "ignitions.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(new[2]) + "\n")
card = G.idea_ignition_times([])
check("Discoveries: 3 starts since the change: typical from them", "typical 3.0 min." in card["text"]["en"])

# ---- warm by: lead learned only from warm-by starts after the change, defaults until 3


class Mod:
    latest = {"fields": {"boiler_c": 30, "state": 0}}
    latest_cfg = {"raw": {}, "fields": {"setpoint_c": 65}}


def lead(sets, learn):
    w = F.WarmMode(sets, types.SimpleNamespace(listeners=[]), Mod(), lambda *a: None, log, lambda *a: None)
    return w.lead_min(dict(w.cfg(), warm_learn=learn))


plain = F.Settings(tempfile.mkdtemp(), log)
oldl = [{"epoch": T0 + i * 86400, "ign": 80, "run_c": 30, "kh": 2.0} for i in range(5)]
newl = [{"epoch": CHG + 3600 * (i + 1), "ign": 20, "run_c": 50, "kh": 1.0} for i in range(3)]
dflt = lead(plain, [])
check("no type change: learned from all warm-by starts", lead(plain, oldl) == 90 and dflt == 50)
check("after the change, 2 new starts: the defaults", lead(s, oldl + newl[:2]) == dflt)
check("after the change, 3 new starts: learned from them only", lead(s, oldl + newl) == 30)

# ---- pellet batches: case / spaces do not make a new batch; the first spelling is shown
sb = F.Settings(tempfile.mkdtemp(), log)
sb.data["pellets"]["batches"] = [{"name": "Brown Sack", "since": T0}, {"name": "Other", "since": T0 + 86400},
                                 {"name": "brown  sack ", "since": T0 + 2 * 86400}]
recs = [start(T0 + 3600, 4.0, "Brown Sack"), start(T0 + 86400 + 3600, 5.0, "Other"),
        start(T0 + 2 * 86400 + 3600, 4.0, "brown  sack "), start(T0 + 3 * 86400, 6.0, "BROWN SACK")]
r = report(recs, sb)
check("one batch in the stats, under the first spelling", sorted(r["stats"]) == ["Brown Sack", "Other"]
      and r["stats"]["Brown Sack"]["n"] == 3 and r["stats"]["Brown Sack"]["flame_min"] == 4.0)
check("records and the current batch show the first spelling",
      {x["batch"] for x in r["records"]} == {"Brown Sack", "Other"} and r["current_batch"] == "Brown Sack")
check("the batch list has it once", [b["name"] for b in r["batches"]] == ["Brown Sack", "Other"])
check("the stored names are not changed", [b["name"] for b in sb.get("pellets")["batches"]] == ["Brown Sack", "Other", "brown  sack "])

print("\n" + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
sys.exit(1 if FAILED else 0)
