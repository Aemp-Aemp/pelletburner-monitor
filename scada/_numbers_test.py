"""Offline test of the numbers that have ONE definition on the server (burner_features), on synthetic data
(scada/_synth.py): time to flame, typical time to flame, pellets and starts per day, the day numbers.
The pages, Home Assistant, the phone alerts and the Discoveries only show these values."""
import datetime, json, os, shutil, sys, tempfile, time, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import _synth
import burner_common as C
import burner_ext as X
import burner_features as F
import insights_gen as G

FAILED = 0
log = lambda *a: None


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


TMP = tempfile.mkdtemp(prefix="numbers_test_")
d = _synth.make_data(os.path.join(TMP, "d"), days=3)
rows = [r for _, p in C.hist_files(d) for r in C.read_jsonl(p)]
want_s = [_synth.flame_delay(day, b) for day in range(3) for b in (0, 1)]

# ---- time to flame: from the start of lighting (state 3) until the flame is above FLAME_ON (17)
recs = F.ignition_records(rows)
check("one record per start", len(recs) == 6 and all(r["result"] == "ok" for r in recs))
check("flame_s = lighting start -> flame > 17", [r["flame_s"] for r in recs] == want_s)
check("flame_min is flame_s in minutes", [F.flame_min(r) for r in recs] == [round(s / 60, 2) for s in want_s])
check("the limit is 17", C.FLAME_ON == 17)

ig = F.IgnitionLog(F._NoSettings(), types.SimpleNamespace(listeners=[]), None, None, log)
t0 = 1_000_000.0
ig._on_event({"type": "transition", "from": 0, "to": 2, "epoch": t0})
ig.on_row({"epoch": t0 + 30, "state": 2, "flame": 60})                 # light before lighting starts: not counted
ig._on_event({"type": "transition", "from": 2, "to": 3, "epoch": t0 + 60})
ig.on_row({"epoch": t0 + 100, "state": 3, "flame": 17})                # 17 is not above 17
ig.on_row({"epoch": t0 + 200, "state": 3, "flame": 18})
ig.on_row({"epoch": t0 + 300, "state": 3, "flame": 90})
ig._on_event({"type": "transition", "from": 3, "to": 4, "epoch": t0 + 900})
check("counted from state 3, first reading above 17 (140 s)", ig.records[-1]["flame_s"] == 140)
ig._on_event({"type": "transition", "from": 4, "to": 0, "epoch": t0 + 2000})
ig._on_event({"type": "transition", "from": 0, "to": 2, "epoch": t0 + 5000})
ig._on_event({"type": "transition", "from": 2, "to": 3, "epoch": t0 + 5060})
ig.on_row({"epoch": t0 + 5100, "state": 3, "flame": 5})
ig._on_event({"type": "transition", "from": 3, "to": 6, "epoch": t0 + 5700, "alarm": 4})
check("a start without a flame has no time to flame", ig.records[-1]["flame_s"] is None
      and F.flame_min(ig.records[-1]) is None and not F.lit(ig.records[-1]))

# ---- typical: the median of the starts that lit in the last 28 days, from 3 starts on
now = recs[-1]["epoch"] + 3600
typ, n = F.typical_flame(recs, now=now)
check("typical = median of the lit starts", n == 6 and typ == round(C.median(s / 60 for s in want_s), 2))
check("fewer than 3 starts: no typical value yet", F.typical_flame(recs[:2], now=now) == (None, 2))
check("starts older than 28 days are left out", F.typical_flame(recs, now=now + 40 * 86400) == (None, 0))


class Settings:
    def __init__(self, data):
        self.data = data

    def get(self, k):
        return self.data.get(k) or {}

    def set_internal(self, a, b, v):
        self.data.setdefault(a, {})[b] = v


# ---- older records (no flame_s in the file): worked out once from the saved history, then saved
d2 = _synth.make_data(os.path.join(TMP, "d2"), days=3)
with open(os.path.join(d2, "ignitions.jsonl"), "w", encoding="utf-8") as fh:
    for r in recs:
        fh.write(json.dumps({k: v for k, v in r.items() if k != "flame_s"}) + "\n")
    fh.write(json.dumps({"epoch": 1000.0, "result": "ok", "minutes": 9.0, "batch": "x", "relight": False}) + "\n")
ig2 = F.IgnitionLog(Settings({}), types.SimpleNamespace(listeners=[]), None, d2, log)
check("older records get their time to flame from the history", [r["flame_s"] for r in ig2.records[:6]] == want_s)
check("a record whose day is gone gets none", ig2.records[6]["flame_s"] is None)
saved = C.rows_of(os.path.join(d2, "ignitions.jsonl"))
check("the file is saved with it, nothing else changed",
      len(saved) == 7 and all("flame_s" in r for r in saved)
      and [{k: v for k, v in r.items() if k != "flame_s"} for r in saved[:6]]
      == [{k: v for k, v in r.items() if k != "flame_s"} for r in recs])
m0 = os.path.getmtime(os.path.join(d2, "ignitions.jsonl"))
F.IgnitionLog(Settings({}), types.SimpleNamespace(listeners=[]), None, d2, log)
check("done once: the next start does not write the file again", os.path.getmtime(os.path.join(d2, "ignitions.jsonl")) == m0)

rep = ig2.report()
check("report: every record carries flame_min", [r["flame_min"] for r in rep["records"][:6]] == [round(s / 60, 2) for s in want_s])
st_b = rep["stats"][recs[0]["batch"]]
check("report: per batch how many lit, the median and the slowest",
      st_b["flame_n"] == 6 and st_b["flame_min"] == round(C.median(s / 60 for s in want_s), 2)
      and st_b["flame_max_min"] == round(max(want_s) / 60, 2))
check("report: typical value and what it is from", "typical_flame_min" in rep and rep["typical_days"] == 28)

# ---- the Discoveries card shows the same times
G.DATA = d
card = G.idea_ignition_times(G.rows_best())
check("Discoveries: the same minutes to flame", card["chart"]["series"][0]["data"] == [round(s / 60, 1) for s in want_s])

# ---- health uses the same time to flame
tn = time.time() - 20 * 3600                     # recent: the usual counts the last 4 weeks
mod = types.SimpleNamespace(ignitions=types.SimpleNamespace(records=[
    {"epoch": tn + i * 3600, "result": "ok", "flame_s": 270 + (30 if i == 9 else 0), "peak_lux": 150} for i in range(10)]),
    latest_cfg={})
h = F.Optimiser(Settings({}), None, mod, log).health(days=[])
tf = next(i for i in h["items"] if i["key"] == "to_flame")
check("health: minutes to flame from flame_s, with the newest start", tf["normal"] == 4.5 and tf["last"] == 5.0)


def api_typical(records, sets):
    ig = F.IgnitionLog(sets, types.SimpleNamespace(listeners=[]), None, None, log)
    ig.records = [dict(r, minutes=20.0, batch="x") for r in records]
    return ig.report()["typical_flame_min"]


# health "usual" IS /api/ignitions typical: same starts (relights too), same 4 weeks, same rounding, also
# with a "burner cleaned" mark and starts older than 4 weeks around
mix = ([{"epoch": tn - 40 * 86400 + i, "result": "ok", "flame_s": 900, "peak_lux": 150} for i in range(5)]
       + [{"epoch": tn - 5 * 86400 + i * 3600, "result": "ok", "flame_s": 250 + 7 * i, "peak_lux": 150,
           "relight": i % 3 == 0} for i in range(9)]
       + [{"epoch": tn + i * 3600, "result": "ok", "flame_s": 283 + 11 * i, "peak_lux": 150} for i in range(6)])
for sets in (Settings({}), Settings({"health": {"cleaned_at": tn - 3600}})):
    mod.ignitions.records = mix
    tf = next(i for i in F.Optimiser(sets, None, mod, log).health(days=[])["items"] if i["key"] == "to_flame")
    typ = api_typical(mix, sets)
    check("health usual == /api/ignitions typical", typ is not None and tf["normal"] == typ
          and tf["normal"] == F.usual_flame(mix, sets)[0])
mod.ignitions.records = mix[:5]
tf = next(i for i in F.Optimiser(Settings({}), None, mod, log).health(days=[])["items"] if i["key"] == "to_flame")
check("health: no typical (only starts older than 4 weeks) -> learning", tf["state"] == "learning"
      and api_typical(mix[:5], Settings({})) is None)

# health is ready when the Lighting time card is: a usual value and 3 starts that lit (not 6)
mod.ignitions.records = [{"epoch": tn + i * 3600, "result": "ok", "flame_s": s, "peak_lux": 150}
                         for i, s in enumerate((200, 260, 230))]
tf = next(i for i in F.Optimiser(Settings({}), None, mod, log).health(days=[])["items"] if i["key"] == "to_flame")
check("health: 3 lit starts and a usual value -> not learning, now = median of the 3, last = newest",
      tf["state"] == "ok" and tf["normal"] == F.usual_flame(mod.ignitions.records, Settings({}))[0] == 3.83
      and tf["now"] == 3.8 and tf["last"] == 3.8 and tf["n"] == 3)
mod.ignitions.records = mod.ignitions.records[:2]
tf = next(i for i in F.Optimiser(Settings({}), None, mod, log).health(days=[])["items"] if i["key"] == "to_flame")
check("health: 2 lit starts -> still learning, needs 3", tf["state"] == "learning" and tf["n"] == 2 and tf["need"] == 3)
mod.ignitions.records = [{"epoch": tn + i * 3600, "result": "ok", "flame_s": 270 + (30 if i == 9 else 0), "peak_lux": 150}
                         for i in range(10)]

# ---- the burner-health checklist: one list from the server (Pellets & ash, History, the share picture)
days7 = [{"day": f"2000-01-0{i}", "runs": [90.0, 20.0], "running_s": 7200} for i in range(1, 4)]
mod.fan = types.SimpleNamespace(analyse=lambda: {"levels": [{"change_pct": -2.0, "n": 20}, {"change_pct": 1.0, "n": 15}, {"change_pct": None, "n": 4}]})
mod.ash = types.SimpleNamespace(feed=lambda: {"turn_hist": [[i, 6.0] for i in range(12)], "problem": None})
hh = F.Optimiser(Settings({}), None, mod, log).health(days=days7)
ch, tf7 = hh["checks"], next(i for i in hh["items"] if i["key"] == "to_flame")
check("checklist: the six checks, in order", [c["k"] for c in ch] == ["light", "flame", "fan", "ash", "short", "rescue"])
check("checklist: time to flame shows the judged value (last 3 starts), not only the newest",
      ch[0] == {"k": "light", "st": "ok", "why": "ok", "p": {"a": tf7["now"], "usual": 4.5}})
check("checklist: flame, fan and ash plate as usual", [c["st"] for c in ch[1:4]] == ["ok", "ok", "ok"]
      and ch[3]["p"] == {"a": 6.0, "usual": 6.0})
check("checklist: the fan with its biggest drop and how many measurements", ch[2] == {"k": "fan", "st": "ok", "why": "ok", "p": {"a": 2.0, "n": 35}})
check("checklist: 3 of 6 burns under 30 min -> short burns", ch[4] == {"k": "short", "st": "bad", "why": "bad", "p": {"a": 3, "n": 6}})
mod.fan = types.SimpleNamespace(analyse=lambda: {"levels": [{"change_pct": -9.0, "n": 12}]})
mod.ash = types.SimpleNamespace(feed=lambda: {"turn_hist": [[i, 6.0] for i in range(10)] + [[i, 9.0] for i in range(5)], "problem": None})
ch = F.Optimiser(Settings({}), None, mod, log).health(days=[])["checks"]
check("checklist: slow fan, slow ash plate, too few burns to tell",
      (ch[2]["st"], ch[2]["p"], ch[3]["st"], ch[3]["why"], ch[3]["p"], ch[4]["st"])
      == ("bad", {"a": 9.0, "n": 12}, "bad", "slow", {"a": 9.0, "usual": 6.0}, "learn"))
mod.ash = types.SimpleNamespace(feed=lambda: {"turn_hist": [], "problem": "stuck"})
mod.fan = None
mod.ignitions.records = [{"epoch": t0 + i * 3600, "result": "failed" if i > 6 else "ok", "flame_s": None if i > 6 else 270, "peak_lux": 150} for i in range(10)]
ch = F.Optimiser(Settings({}), None, mod, log).health(days=[])["checks"]
check("checklist: failed starts, ash-plate problem, no fan data",
      ch[0] == {"k": "light", "st": "bad", "why": "fail", "p": {"a": 3, "n": 10}} and ch[2] == {"k": "fan", "st": "learn"}
      and ch[3] == {"k": "ash", "st": "bad", "why": "prob"})

# ---- pellets per day and starts per day: one rule
today = datetime.date.today()
d3 = os.path.join(TMP, "d3")
os.makedirs(d3)


def day_file(back, hours, burn_h, power=50):
    """A day `back` days ago: readings for `hours` hours from 00:00, the first `burn_h` of them burning."""
    day = today - datetime.timedelta(days=back)
    e0 = datetime.datetime.combine(day, datetime.time(0)).timestamp()
    with open(os.path.join(d3, f"hist-{day.isoformat()}.jsonl"), "w") as fh:
        for i in range(int(hours * 60) + 1):
            on = i < burn_h * 60
            fh.write(json.dumps({"epoch": e0 + i * 60, "state": 4 if on else 0, "power_pct": power if on else 0,
                                 "fuel_gph": 2000}) + "\n")


day_file(4, 23.9, 6)             # whole day: 6 h at 50 % of 2 kg/h = 6 kg
day_file(3, 23.9, 8)             # whole day: 8 kg
day_file(2, 10, 2)               # recorded in part: 2 kg - not used while whole days exist
day_file(1, 23.9, 0)             # whole day, no burning: not a burning day
day_file(0, 5, 4)                # today: never used
sets = F.Settings(d3, log)
opt = F.Optimiser(sets, X.Store(d3, 365, log), types.SimpleNamespace(latest_cfg={}, ignitions=None), log)
pd = opt.per_day()
check("per day: finished whole days that burned (7.0 kg, 2 days)",
      pd == {"kg": 7.0, "starts": 0.0, "days": 2, "whole_days": True})
sets.set_internal("pellets", "corrections", [{"f": 1.2}])
check("per day: with the owner's correction", opt.per_day()["kg"] == 8.4)
rp = opt.report()
check("report carries the same per-day value", rp["per_day"] == opt.per_day())
dn = {x["day"]: x for x in rp["days"]}
d4 = (today - datetime.timedelta(days=4)).isoformat()
check("report days carry fuel_kg (corrected), burn_h and recorded_h",
      dn[d4]["fuel_kg"] == 7.2 and dn[d4]["burn_h"] == 6.0 and dn[d4]["recorded_h"] == 23.9)
check("report days carry short_burns (burns under 30 min)", all(x["short_burns"] == F.short_burns(x.get("runs")) for x in rp["days"])
      and F.short_burns([90.0, 20.0, None, 29.9, 30.0]) == 2 and F.short_burns(None) == 0)
sets.set_internal("pellets", "stock", {"kg": 500, "at": time.time() - 3600})
stock = opt.pellet_stock()
check("pellet stock uses the same per-day value", stock["per_day"] == 8.4 and stock["days_basis"] == 2)
sets.set_internal("pellets", "corrections", [])
sets.set_internal("pellets", "stock", None)
for back in (4, 3):
    os.remove(os.path.join(d3, f"hist-{(today - datetime.timedelta(days=back)).isoformat()}.jsonl"))
check("per day: no whole day yet -> the days recorded in part",
      opt.per_day() == {"kg": 2.0, "starts": 0.0, "days": 1, "whole_days": False})
check("day numbers", F.day_numbers({"fuel_silo_kg": 3.0, "fuel_est_kg": 2.0, "running_s": 5400, "state_s": {"4": 5400, "0": 3600}}, 1.1)
      == {"fuel_kg": 3.3, "burn_h": 1.5, "recorded_h": 2.5})
check("day fuel: the estimate when the silo counter did not move", F.day_fuel({"fuel_silo_kg": 0, "fuel_est_kg": 2.0}) == 2.0)

shutil.rmtree(TMP, ignore_errors=True)
print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
