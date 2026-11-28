"""The two graphs "Did the change help?" and "Pellets and the weather". Made-up data only.
A  change points: settings on the start records, the settings history, several at once, a batch, the burner cleaned
B  verdicts: cold vs warm starts (temperature band), relights and resumes left out, better, too few, a batch mixed in
C  pellets days 06:00-06:00 from the day summaries
D  the weather line: learning (days, spread, fit), a clean made-up house, a plan change starts again, the forecast
E  the forecast answer (the fetch is mocked) and the phone lines (EN, DA)
Browser: /home History and Pellets & ash, /classic Analysis, EN and DA, 375 and 1280 px (skipped without Playwright)."""
import datetime, json, os, random, sys, tempfile, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..")))
sys.path.insert(0, HERE)
import burner_features as F

FAILED = 0


def check(name, cond, extra=""):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {extra}"))
    FAILED += 0 if cond else 1


T0 = datetime.datetime(2000, 1, 3, 6, 0).timestamp()
H = 3600


def start(i, flame=5.0, full=30.0, boiler=40.0, res="ok", cfg=None, batch="A", **k):
    return dict({"epoch": T0 + i * 12 * H, "result": res, "minutes": full, "ignition_min": 4.0, "relight": False,
                 "flame_s": round(flame * 60) if res == "ok" else None, "boiler": boiler, "batch": batch,
                 "cfg": cfg if cfg is not None else {"014": 40, "017": 800}}, **k)


# ---------------------------------------------------------------- A: change points
R = [start(i) for i in range(10)] + [start(i, cfg={"014": 30, "017": 900}) for i in range(10, 20)]
cp = F.change_points(R)
check("A: a setting that differs from the start before is a change", len(cp) == 1 and cp[0]["t"] == round(R[10]["epoch"] - 1), cp)
check("A: two settings changed at once are one change, both listed",
      sorted((p["field"], p["old"], p["new"]) for p in cp[0]["parts"]) == [("014", 40, 30), ("017", 800, 900)], cp)
rows = [{"time": T0 + 9 * 12 * H + 600, "field": "014", "old": 40, "new": 30},
        {"time": T0 + 9 * 12 * H + 700, "field": "031", "old": 100, "new": 80},
        {"time": T0 + 3 * 12 * H + 60, "field": "001", "old": 3400, "new": 3500},
        {"time": T0 + 3 * 12 * H + 90, "field": "001", "old": 3500, "new": 3400}]          # changed and back: nothing
cp = F.change_points(R, rows)
check("A: the settings history gives the time; a setting changed and back is no change",
      len(cp) == 1 and cp[0]["t"] == round(rows[0]["time"]), cp)
check("A: history and start records together list each setting once (014, 017, 031)",
      sorted(p["field"] for p in cp[0]["parts"]) == ["014", "017", "031"], cp[0]["parts"])
cp = F.change_points(R, (), [{"name": "B", "since": T0 + 4 * 12 * H}], T0 + 15 * 12 * H)
check("A: a pellet batch and the burner cleaned are changes too",
      [c["parts"][0]["k"] for c in cp] == ["batch", "cfg", "cleaned"], cp)
cp = F.change_points(R, (), (), [{"t": T0 + 15 * 12 * H, "parts": ["burner", "fan"]},
                                 {"t": T0 + 15 * 12 * H + 600, "parts": ["sensor"]}])
check("A: clean marks say which parts; two close together are one cleaning with all the parts",
      cp[-1]["parts"] == [{"k": "cleaned", "what": ["burner", "fan", "sensor"]}], cp)
cp = F.change_points(R, (), [{"name": "B", "since": R[10]["epoch"] + 600}])
check("A: a batch within 6 h of a settings change is the same change",
      len(cp) == 1 and {p["k"] for p in cp[0]["parts"]} == {"cfg", "batch"}, cp)

# ---------------------------------------------------------------- B: verdicts
random.seed(1)
jit = lambda: random.uniform(-0.3, 0.3)
before = [start(i, flame=5.0 + jit(), full=34 + jit()) for i in range(9)]
after = [start(i, flame=3.8 + jit(), full=31 + jit(), cfg={"014": 30, "017": 800}) for i in range(9, 20)]
warm = [start(20 + i, flame=1.0, full=10, boiler=75.0, cfg={"014": 30, "017": 800}) for i in range(4)]
extra = [start(24, relight=True, flame=0.5, cfg={"014": 30, "017": 800}),
         start(25, flame=0.2, full=0.5, cfg={"014": 30, "017": 800}, ignition_min=0)]          # went on burning
recs = sorted(before + after + warm + extra, key=lambda r: r["epoch"])
recs.sort(key=lambda r: r["epoch"])
for j, r in enumerate(recs):
    r["epoch"] = T0 + j * 12 * H
now = recs[-1]["epoch"] + H
ch = F.change_help(recs, [], now=now)
c = ch["changes"][ch["pick"]]
rw = {r["k"]: r for r in c["rows"]}
check("B: better: time to flame 5.0 -> 3.8, the middle halves apart", c["head"]["k"] == "better"
      and abs(c["head"]["d"] - 1.2) < 0.3 and rw["flame"]["v"] == "better", c["head"])
check("B: warm starts (boiler 75 °C) left out by the temperature band; relight and resume left out",
      rw["flame"]["nb"] == 9 and rw["flame"]["na"] == 11 and c["left_out"] == 4, (rw["flame"], c["left_out"], c["band"]))
check("B: time to full heat better too, failed starts the same", rw["full"]["v"] == "better" and rw["failed"]["v"] == "same", rw)
check("B: day rows without days: too few; the weather row learning", rw["power_flame"]["v"] == "few"
      and rw["short"]["v"] == "few" and rw["wx"]["v"] == "learning", rw)
check("B: the dot strip has the starts that lit, grey before and orange after",
      sum(1 for d in c["dots"] if d[2] == 0) == 9 and sum(1 for d in c["dots"] if d[2] == 1) == 11)
few = F.change_help(before + after[:3], [], now=after[2]["epoch"] + H)["changes"][0]
check("B: 3 starts since the change: too few to say (3 of 8)", few["head"] == {"k": "few", "n": 3, "need": 8}
      and all(r["v"] in ("few", "learning") for r in few["rows"]), few["head"])
same = F.change_help([start(i, flame=5 + jit(), cfg={"014": 40 if i < 8 else 30}) for i in range(16)], [], now=T0 + 20 * 12 * H)
check("B: the same flame time on both sides -> the same", same["changes"][0]["head"]["k"] == "same", same["changes"][0]["head"])
mixed = F.change_help(before + after, [], (), [{"name": "B", "since": after[0]["epoch"] - 1800}], now=now)["changes"][0]
check("B: a pellet batch changed with the settings: say so, no verdict",
      mixed["head"]["k"] == "batch" and all(r["v"] in ("batch", "learning") for r in mixed["rows"]), mixed["head"])
inwin = [dict(r, batch="B") if r["epoch"] > after[4]["epoch"] else r for r in before + after]
mixed2 = F.change_help(inwin, [], now=now)["changes"][0]
check("B: a batch that differs inside the window (not one of the changes): no verdict", mixed2["head"]["k"] == "batch", mixed2["head"])
fails = [start(i) for i in range(8)] + [start(i, res="failed" if i % 2 else "ok", cfg={"014": 30, "017": 800}) for i in range(8, 16)]
fr = {r["k"]: r for r in F.change_help(fails, [], now=T0 + 20 * 12 * H)["changes"][0]["rows"]}["failed"]
check("B: 4 of 8 starts failed after, 0 of 8 before -> worse", fr["v"] == "worse" and fr["after"] == 4, fr)
check("B: rate rule: 30 points apart, 3 more on the bad side and Fisher p < 0.1",
      F.ch_judge_rate(0, 8, 2, 8) == "same" and F.ch_judge_rate(0, 8, 3, 8) == "same" and F.ch_judge_rate(0, 8, 4, 8) == "worse"
      and F.ch_judge_rate(4, 8, 0, 8) == "better" and F.ch_judge_rate(4, 20, 4, 8) == "same" and F.ch_judge_rate(0, 7, 4, 8) == "few")
check("B: IQR overlap -> the same even with other medians",
      F.ch_judge([4, 5, 6, 7, 8, 5, 6, 7], [3, 4, 5, 6, 7, 4, 5, 6], "flame") == "same")
# a change that did nothing, many times (fixed seed): time to flame must give "better" / "worse" under 5 % of the time
rnd = random.Random(7)
for sd in (0.5, 1.5, 3.0):
    n = 2000
    false = sum(F.ch_judge([max(0.3, rnd.gauss(5, sd)) for _ in range(F.CH_NEED)],
                           [max(0.3, rnd.gauss(5, sd)) for _ in range(F.CH_NEED)], "flame") != "same" for _ in range(n)) / n
    check(f"B: no-effect change, {F.CH_NEED} v {F.CH_NEED} starts, spread {sd} min: false verdict under 5 % ({false:.1%})", false < 0.05)
false = 0
for _ in range(2000):
    kb, ka = (sum(rnd.random() < 0.3 for _ in range(F.CH_NEED)) for _ in range(2))
    false += F.ch_judge_rate(kb, F.CH_NEED, ka, F.CH_NEED) != "same"
check(f"B: no-effect change, failed starts at 30 %: false verdict under 8 % ({false / 2000:.1%})", false / 2000 < 0.08)
# more starts did not light: the headline never says "Yes"
mx = [start(i, flame=5.0 + jit()) for i in range(10)] + [start(i, flame=3.8 + jit(), cfg={"014": 30, "017": 800},
      **({"res": "failed"} if i % 3 == 0 else {})) for i in range(10, 25)]
nmx = mx[-1]["epoch"] + H
hm = F.change_help(mx, [], now=nmx)["changes"][0]["head"]
check("B: flame sooner but more starts did not light -> mixed, not better", hm["k"] == "mixed" and hm["a"] >= 4, hm)
mx2 = [dict(r, flame_s=round(5.0 * 60)) if r.get("result") == "ok" else r for r in mx]
hm2 = F.change_help(mx2, [], now=nmx)["changes"][0]["head"]
check("B: same flame time but more starts did not light -> worse (failed)", hm2["k"] == "failed", hm2)
check("B: the phone says it too", "did not light" in F.phone_lines({"changes": [{"t": now, "head": hm}]}, None, "en", now=now)[0])
# batch names that differ only in case or spaces: no new batch (a delivery of the same pellets)
cp = F.change_points(R, (), [{"name": "Wood Pellets A", "since": T0 - 99}, {"name": "wood  pellets a", "since": T0 + 4 * 12 * H},
                             {"name": "Other", "since": T0 + 15 * 12 * H}])
check("A: a batch typed again in other case is no change; a real new batch is",
      [p["name"] for c in cp for p in c["parts"] if p["k"] == "batch"] == ["Wood Pellets A", "Other"], cp)
bk = [dict(r, batch="Wood Pellets A" if j % 2 else "wood pellets a") for j, r in enumerate(recs)]
check("B: batch names in other case inside the window are one batch (a verdict)",
      F.change_help(bk, [], now=now)["changes"][0]["head"]["k"] == "better")
# a setting not sent (None) is never a change
RN = [start(i) for i in range(5)] + [start(i, cfg={"014": None, "017": 800}) for i in range(5, 8)] + [start(i, cfg={"014": 40, "017": 800}) for i in range(8, 12)]
check("A: a value of None is no change (no 40 -> None, no None -> 40)", F.change_points(RN) == [], F.change_points(RN))
RN2 = RN[:8] + [start(i, cfg={"014": 30, "017": 800}) for i in range(8, 12)]
cpn = F.change_points(RN2)
check("A: after a None the value before it is compared (40 -> 30)", len(cpn) == 1 and cpn[0]["parts"][0]["old"] == 40, cpn)


def dayrec(day, flame90=None, runs=(60, 70)):
    return {"day": day, "running_s": 7200, "runs": list(runs), "flame_power": {"90": [1800, flame90 * 1800, 1800, 0, 0]} if flame90 else {}}


cday = datetime.date.fromtimestamp(after[0]["epoch"]).isoformat()
dd = lambda k: (datetime.date.fromisoformat(cday) + datetime.timedelta(days=k)).isoformat()
days = [dayrec(dd(-k), 140 + k % 3, (20, 25, 80)) for k in range(1, 10)] + [dayrec(dd(k), 155 + k % 3) for k in range(1, 10)]
rw = {r["k"]: r for r in F.change_help(recs, days, now=now + 4 * 86400)["changes"][0]["rows"]}
check("B: flame at full power (days) higher after -> better; fewer short burns -> better",
      rw["power_flame"]["v"] == "better" and rw["short"]["v"] == "better" and rw["short"]["before"] == 18, rw)
# the day rows stop at the neighbouring change: its day is not counted on either side
prev_t = datetime.datetime.fromisoformat(dd(-3) + "T15:00").timestamp()
cc = F.change_compare({"t": after[0]["epoch"] - 1, "parts": []}, prev_t, None, recs, days, now=now)
rw2 = {r["k"]: r for r in cc["rows"]}
check("B: day rows: before = only the whole days after the change before (dd-2, dd-1)", rw2["power_flame"]["nb"] == 2, rw2["power_flame"])

# ---------------------------------------------------------------- C: 06:00-06:00 days


def rows_day(day, kg_before6, kg_after6, hours=24):
    """Made-up readings of one calendar day: DRIFT at 50 % with a fuel rate giving the asked kg before / after 06:00."""
    t0 = datetime.datetime.fromisoformat(day + "T00:00").timestamp()
    out = []
    for m in range(0, hours * 60):
        e = t0 + m * 60
        part = kg_before6 / 360 if m < 360 else kg_after6 / (hours * 60 - 360)
        out.append({"epoch": e, "state": 4, "power_pct": 100, "fuel_gph": part * 60 * 1000})
    out.append({"epoch": t0 + hours * 3600 - 1, "state": 4, "power_pct": 100, "fuel_gph": 0})
    return out


s1 = F.summarise_rows(rows_day("2000-01-03", 2.0, 8.0)); s1["day"] = "2000-01-03"
s2 = F.summarise_rows(rows_day("2000-01-04", 3.0, 9.0)); s2["day"] = "2000-01-04"
check("C: the day summary keeps the part before 06:00", abs(s1["early"]["kg"] - 2.0) < 0.05 and abs(s1["early"]["s"] - 6 * H) < 120, s1["early"])
sp = F.day_split([s1, s2])
check("C: a pellets day = after 06:00 plus the next day before 06:00 (8 + 3 = 11 kg, 24 h)",
      len(sp) == 1 and sp[0]["day"] == "2000-01-03" and abs(sp[0]["kg_est"] - 11.0) < 0.1 and sp[0]["h"] >= 23.9, sp)
check("C: no next day yet -> no pellets day; an old summary without the split is skipped",
      F.day_split([s2]) == [] and F.day_split([dict(s1, early=None), s2]) == [])
check("C: outside temperature of a pellets day: 18 h of that day, 6 h of the next",
      F.wx_temp("2000-01-03", {"2000-01-03": 4.0, "2000-01-04": 0.0}) == 3.0 and F.wx_temp("2000-01-03", {"2000-01-03": 4.0}) == 4.0
      and F.wx_temp("2000-01-03", {}) is None)

# ---------------------------------------------------------------- D: the weather line


def house(n, base=3.0, slope=0.6, temps=None, noise=0.3, start_day="2000-01-01", hours=24.0, seed=2):
    """Made-up day summaries for a house that uses base + slope x (16 - T) kg a day, and its weather days."""
    rnd = random.Random(seed)
    sums, wd = [], {}
    d0 = datetime.date.fromisoformat(start_day)
    for i in range(n + 1):
        day = (d0 + datetime.timedelta(days=i)).isoformat()
        T = temps[i % len(temps)] if temps else 8 - 10 * ((i * 7) % 11) / 10
        kg = base + slope * max(0, 16 - T) + rnd.uniform(-noise, noise)
        wd[day] = T
        # a quarter of each day's kg before 06:00, recorded `hours` (the early part is always 6 h)
        tot = hours * 3600
        sums.append({"day": day, "state_s": {"4": tot}, "running_s": tot / 2, "fuel_est_kg": kg,
                     "early": {"s": 6 * 3600, "kg": kg / 4, "run_s": 3 * 3600}})
    # with the split, a pellets day's kg = 3/4 of day D + 1/4 of D+1: give D+1 the same weather, so use pairs
    return sums, wd


sums, wd = house(10)
wx = F.weather_fit(sums, wd, flat=9.5)
check("D: 10 days -> learning (10 of 14), the flat average kept", not wx["learned"] and wx["why"] == "days"
      and wx["n"] == 10 and wx["need"] == 14 and wx["flat"] == 9.5, wx)
sums, wd = house(20, temps=[5.0, 6.0, 7.0])
wx = F.weather_fit(sums, wd)
check("D: 20 days but only 2 °C apart -> learning (spread)", not wx["learned"] and wx["why"] == "spread", wx["spread"])
sums, wd = house(20, noise=4.0, slope=0.1)
wx = F.weather_fit(sums, wd)
check("D: days that do not follow the weather -> learning (fit)", not wx["learned"] and wx["why"] == "fit", wx.get("r2"))
sums, wd = house(30, temps=[t / 1.0 for t in range(-5, 13)], noise=0.2)
wx = F.weather_fit(sums, wd)
# the made-up kg are per calendar day; a pellets day mixes 3/4 of one day and 1/4 of the next with 18/24 + 6/24 of
# their temperatures, so the line is not exact, but close
check("D: a clean made-up house is learned: slope near 0.6, base near 3, at 0 °C near 12.6 kg",
      wx["learned"] and abs(wx["b"] - 0.6) < 0.08 and abs(wx["a"] - 3.0) < 1.0 and abs(wx["at0"] - 12.6) < 0.8, wx)
short = [dict(s, state_s={"4": 20 * 3600}) for s in sums]
check("D: days with under 23 h recorded do not count", F.weather_fit(short, wd)["n"] == 0)
idle = [dict(s, running_s=0, fuel_est_kg=0, early=dict(s["early"], run_s=0, kg=0)) for s in sums]
wi = F.weather_fit(idle, wd)
check("D: whole days without burning count, with 0 kg (mild days set the base)", wi["n"] == len(sums) - 1
      and all(p[2] == 0 for p in wi["pts"]), wi["n"])
silo = [dict(s, fuel_silo_kg=round(s["fuel_est_kg"] * 1.25)) for s in sums]
ws = F.weather_fit(silo, wd)
check("D: kg as the rest of the page (day_fuel: the silo counter where it moved), split at 06:00 by the estimate",
      abs(sum(p[2] for p in ws["pts"]) / sum(p[2] for p in wx["pts"]) - 1.25) < 0.03, (ws["pts"][:2], wx["pts"][:2]))
check("D: 0 °C inside the learned days: the reference is 0 °C", wx["ref_c"] == 0 and wx["t_lo"] <= 0 <= wx["t_hi"])
sw_, wdw = house(30, temps=[t / 1.0 for t in range(3, 13)], noise=0.2)
ww = F.weather_fit(sw_, wdw)
check("D: all learned days above 0 °C: no 'at 0 °C', the nearest learned temperature instead",
      ww["learned"] and ww["ref_c"] == 4 and abs(ww["at0"] - F.wx_kg(ww, 4)) < 0.1, (ww.get("ref_c"), ww.get("t_lo")))
check("D: under 21 days the line is young (early)", wx["early"] is False and F.weather_fit(sums[:20], wd).get("early") in (True, None))
mid = datetime.datetime.fromisoformat(sums[20]["day"] + "T12:00").timestamp()
wx2 = F.weather_fit(sums, wd, plan_since=mid)
check("D: a heating plan change starts a new fit (only days after it)", not wx2["learned"] and wx2["n"] < 14, wx2["n"])
wx3 = F.weather_fit(sums, wd, cfg_rows=[{"time": mid, "field": "000", "old": 70, "new": 65}])
check("D: a boiler set point change (cfg 000) also starts again", wx3["n"] == wx2["n"])
pd = tempfile.mkdtemp()
st = F.Settings(pd, lambda m: None)
check("D: plan marks: the first look only remembers", F.plan_marks(pd, st, now=T0) == 0)
st.update({"warm": {"boosts": ["06:00", "17:00"]}})
check("D: plan marks: a changed heating plan is a mark", F.plan_marks(pd, st, now=T0 + 99) == T0 + 99
      and F.plan_marks(pd, st, now=T0 + 500) == T0 + 99)
# forecast: 7 days, and how long the pellets last
today = datetime.date.fromtimestamp(T0).isoformat()
fc = [[(datetime.date.fromtimestamp(T0) + datetime.timedelta(days=i)).isoformat(), 0.0] for i in range(8)]
w = F.wx_forecast(dict(wx), fc, {"left": 300, "reserve_kg": 150}, now=T0)
per = F.wx_kg(wx, 0.0)
check("D: next 7 days = 7 x the line at 0 °C", len(w["fc"]) == 7 and w["fc_n"] == 7 and abs(w["fc_kg"] - 7 * per) <= 1, (w["fc_kg"], per))
w5 = F.wx_forecast(dict(wx), fc[:6], None, now=T0)
check("D: a forecast of 5 days says 5 days", w5["fc_n"] == 5 and F.phone_lines(None, w5, "en", now=T0)[0].startswith("Next 5 days"))
check("D: the weather's buy-by date only from 21 days and a forecast inside the learned temperatures",
      F.wx_use_buy(w) and not F.wx_use_buy(dict(w, n=20))
      and not F.wx_use_buy(F.wx_forecast(dict(wx), [[d, -20.0] for d, _ in fc], {"left": 300, "reserve_kg": 150}, now=T0)))
check("D: pellets last to about left / kg a day", abs(w["last_to"] - (T0 + 300 / per * 86400)) < 86400 * 0.6, (w["last_to"] - T0) / 86400)
check("D: buy by = when the reserve is reached (earlier)", w["buy_by"] < w["last_to"])
check("D: not learned -> no forecast", "fc" not in F.wx_forecast(dict(wx2), fc, None, now=T0))

# ---------------------------------------------------------------- E: the forecast answer (no internet) and phone lines
# Open-Meteo's daily unixtime is local midnight of the PLACE, sent in UTC, with utc_offset_seconds. The made-up place
# is 10 h ahead of UTC (not the test PC's zone), so reading the times in the PC's own zone would give the wrong days
OFF = 10 * 3600
pmid = lambda d: int((datetime.datetime.fromisoformat(str(d)) - datetime.datetime(1970, 1, 1)).total_seconds()) - OFF
ans = {"current": {"temperature_2m": 1.5}, "hourly": {"time": [], "temperature_2m": []}, "utc_offset_seconds": OFF,
       "daily": {"time": [pmid(d) for d, _ in fc[:8]],
                 "temperature_2m_mean": [1.0, None, -2.5, 3, "x", 4, 5, 6]}}
got = F.parse_forecast(ans, today=today)
check("E: forecast parsed: days from today, empty and broken values skipped",
      [x[1] for x in got] == [1.0, -2.5, 3.0, 4.0, 5.0, 6.0] and got[0][0] == today, got)
wd_dir = tempfile.mkdtemp()
wst = F.Settings(wd_dir, lambda m: None)
wst.update({"weather": {"enabled": True, "name": "Example", "lat": 55.0, "lon": 10.0}})
W = F.Weather(wst, wd_dir, lambda m: None)
asked = []
now_days = [pmid(datetime.date.today() + datetime.timedelta(days=i)) for i in range(8)]
ans2 = dict(ans, daily=dict(ans["daily"], time=now_days))
W._get = lambda url, params, timeout=15: asked.append(params) or ans2
W.fetch(55.0, 10.0)
check("E: one request, the same rounded place, asks for the daily mean", len(asked) == 1 and asked[0]["latitude"] == 55.0
      and asked[0]["daily"] == "temperature_2m_mean", asked)
check("E: the forecast is kept for the place in use", len(W.forecast()) >= 1 and all(len(x) == 2 for x in W.forecast()))
wst.update({"weather": {"lat": 56.0}})
check("E: another place -> no forecast until it is fetched", W.forecast() == [])
chg = F.change_help(recs, [], now=now)
chg["changes"][0]["t"] = now - 3 * 86400
L = F.phone_lines(chg, w, "en", now=now)
check("E: phone: since the change, flame sooner, and the next 7 days", len(L) == 2 and L[0].startswith("Since the change")
      and "min sooner (11 starts)" in L[0] and L[1].startswith("Next 7 days: about") and "Pellets last to about" in L[1], L)
L = F.phone_lines(chg, w, "da", now=now)
check("E: phone in Danish (decimal comma)", L[0].startswith("Siden ændringen") and "," in L[0] and L[1].startswith("De næste 7 dage"), L)
chg["changes"][0]["t"] = now - 20 * 86400
check("E: phone: a change older than 14 days is not mentioned", len(F.phone_lines(chg, None, "en", now=now)) == 0)

# ---------------------------------------------------------------- browser
try:
    from playwright.sync_api import sync_playwright
except Exception as ex:                          # noqa
    print(f"SKIP browser part ({ex})")
    sync_playwright = None
if sync_playwright:
    import _synth
    # made-up answers (the same shapes the server sends)
    sums, wd = house(30, temps=[t / 1.0 for t in range(-5, 13)], noise=0.2)
    WXL = F.weather_fit(sums, wd, flat=9.6)
    WXL = F.wx_forecast(WXL, [[d, t] for d, t in zip([(datetime.date.today() + datetime.timedelta(days=i)).isoformat()
                        for i in range(8)], [3, 1, -2, -4, -1, 2, 5, 4])], {"left": 420, "reserve_kg": 150})
    WXL["pts"][-1][0] = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    CH = json.dumps(F.change_help(recs, days, wx=WXL, now=now))
    WXN = F.weather_fit(sums[:7], wd, flat=9.6)
    SET = """(a)=>{ CH_PICK=null;
      if(typeof renderHistory==="function"){ OPT=Object.assign({},OPT||{},{change:a.ch,wx:a.wx}); renderHistory(); renderPellets(); }
      else renderOpt(Object.assign({},lastOptR||{},{change:a.ch,wx:a.wx}));
      const q=s=>document.querySelector(s);
      return {ch:q('#chHelp')?q('#chHelp').innerText:'', rows:document.querySelectorAll('#chHelp .chRow').length,
        dots:document.querySelectorAll('#chHelp svg circle.chDot').length, pick:document.querySelectorAll('#chHelp select option').length,
        wx:q('#wxHelp')?q('#wxHelp').innerText:'', wxrow:q('#chHelp .chRow[data-k=wx]')?q('#chHelp .chRow[data-k=wx]').innerText:'', bars:document.querySelectorAll('#wxHelp svg rect.wxBar').length,
        pts:document.querySelectorAll('#wxHelp svg circle.wxPt').length,
        wide:document.documentElement.scrollWidth>window.innerWidth+1}; }"""
    errs = []
    with _synth.Replay(8874) as rp, sync_playwright() as pw:
        br = pw.chromium.launch()
        for lang, wdt in (("en", 375), ("da", 375), ("en", 1280), ("da", 1280)):
            ctx = br.new_context(viewport={"width": wdt, "height": 900}, locale="en-GB" if lang == "en" else "da-DK")
            ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');}}catch(e){{}}")
            pg = ctx.new_page()
            pg.on("pageerror", lambda x: errs.append(str(x)))
            pg.on("console", lambda m: m.type == "error" and errs.append(m.text))
            tag = f"[{lang} {wdt}px]"
            pg.goto(rp.base + "/home?replaybanner=0#history", wait_until="load")
            pg.wait_for_selector("#health", timeout=30000)
            g = pg.evaluate(SET, {"ch": json.loads(CH), "wx": WXL})
            yes = "Yes:" if lang == "en" else "Ja:"
            check(f"{tag} /home: change graph with a headline, 6 table rows, the dots", yes in g["ch"] and g["rows"] == 6
                  and g["dots"] == 20, g)
            check(f"{tag} /home: the weather row filled once learned (kg at 0 °C)", g["wxrow"].count("kg") == 2, g["wxrow"])
            pg.evaluate("location.hash='#pellets'")
            pg.wait_for_timeout(400)
            g2 = pg.evaluate(SET, {"ch": json.loads(CH), "wx": WXL})
            check(f"{tag} /home Pellets: learned: dots, 7 bars, next 7 days in kg, no kroner",
                  g2["pts"] == len(WXL["pts"]) and g2["bars"] == 7 and ("Next 7 days" in g2["wx"] or "næste 7 dage" in g2["wx"])
                  and " kr" not in g2["wx"], g2)
            g3 = pg.evaluate(SET, {"ch": json.loads(CH), "wx": WXN})
            check(f"{tag} /home Pellets: learning n of 14 days, the flat average", ("6 of 14" in g3["wx"] or "6 af 14" in g3["wx"])
                  and g3["bars"] == 0, g3["wx"])
            check(f"{tag} fits the screen", not g["wide"] and not g2["wide"] and not g3["wide"])
            yday = pg.evaluate("(()=>{ const y=new Date(); y.setDate(y.getDate()-1); return y.getFullYear()+'-'+String(y.getMonth()+1).padStart(2,'0')+'-'+String(y.getDate()).padStart(2,'0'); })()")
            ydx = dict(WXL, pts=[list(p) for p in WXL["pts"]])
            ydx["pts"][-1][0] = yday                   # the page's own yesterday (a replay may run on another clock)
            pg.evaluate(SET, {"ch": json.loads(CH), "wx": ydx})
            yd = pg.evaluate("document.querySelector('#wxHelp .wxYd')&&document.querySelector('#wxHelp .wxYd').textContent")
            check(f"{tag} the newest dot is labelled yesterday when it is yesterday", yd == ("yesterday" if lang == "en" else "i går"), yd)
            old = dict(WXL, pts=[list(p) for p in WXL["pts"]])
            old["pts"][-1][0] = "2000-01-20"
            pg.evaluate(SET, {"ch": json.loads(CH), "wx": old})
            yd = pg.evaluate("document.querySelector('#wxHelp .wxYd').textContent")
            check(f"{tag} ... and with its date when it is not", yd in ("20 Jan", "20. jan"), yd)
            dd_ = pg.evaluate("[dmon(1764288000), healthHead([{st:'ok'},{st:'learn'}]).cls, healthHead([{st:'ok'},{st:'ok'}]).cls]")
            check(f"{tag} one full stop after a Danish date; 'Still learning' while a row learns", not dd_[0].endswith(".")
                  and dd_[1:] == ["learn", "ok"], dd_)
            ctx.close()
        for lang in ("en", "da"):
            ctx = br.new_context(viewport={"width": 1280, "height": 900}, locale="en-GB" if lang == "en" else "da-DK")
            ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');}}catch(e){{}}")
            pg = ctx.new_page()
            pg.on("pageerror", lambda x: errs.append(str(x)))
            pg.goto(rp.base + "/classic?replaybanner=0#opt", wait_until="load")
            pg.wait_for_timeout(2500)
            g = pg.evaluate(SET, {"ch": json.loads(CH), "wx": WXL})
            check(f"[{lang}] /classic Analysis: the change graph", g["rows"] == 6 and g["dots"] == 20, g)
            ctx.close()
        br.close()
    check("browser: no errors", not errs, errs[:3])

print("ALL PASS" if not FAILED else f"{FAILED} FAILED")
sys.exit(1 if FAILED else 0)
