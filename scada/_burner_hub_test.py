"""Burner health as the hub, and what came with it. Made-up data only.
C  the old start windows: an old settings.json with them on still loads, they are turned off with one log line, the
   shared rules (short-run warning, starts per day) keep working, /classic has no card for them any more
D  /classic Settings: the tank sensor, outside temperature and ash bin cards are one line to the new page
E  fire rescues: when, and whether the boiler relit it (day summaries, suggestion, Burner health row)
F  starts and settings: resumed starts marked and left out, settings compared with time to flame, the settings
   changes between the starts, "off for" after a restart of the monitor
G  flame sensor: 5-min samples, ready after 30 min over 3 burns, learns from the saved history
Browser: /home History with made-up data, rows open in place, fits 375 px (skipped without Playwright)."""
import datetime, json, os, re, sys, tempfile, types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import burner_features as F
import burner_ext as X

FAILED = 0


def check(name, cond, extra=""):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {extra}"))
    FAILED += 0 if cond else 1


def page(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


T0 = datetime.datetime(2000, 1, 3, 6, 0).timestamp()

# ---------------------------------------------------------------- C: old start windows
d = tempfile.mkdtemp()
old = {"schedule": {"enabled": True, "min_off_min": 45, "max_starts_day": 5, "short_run_min": 25,
                    "windows": [{"on": True, "days": [0, 1], "start": "05:00", "end": "07:00"}]},
       "flame_field": "004"}
with open(os.path.join(d, "settings.json"), "w") as f:
    json.dump(old, f)
logs = []
st = F.Settings(d, logs.append)
sch = st.get("schedule")
check("C: an old settings.json with start windows loads", sch["min_off_min"] == 45 and sch["max_starts_day"] == 5
      and len(sch["windows"]) == 1 and st.get("flame_field") == "004", sch)
check("C: the old start windows are turned off, saved, with one log line",
      sch["enabled"] is False and json.load(open(os.path.join(d, "settings.json")))["schedule"]["enabled"] is False
      and sum("old start windows" in m for m in logs) == 1, logs)
logs.clear()
F.Settings(d, logs.append)
check("C: the next start says nothing more", not any("old start windows" in m for m in logs), logs)
check("C: a settings.json without the section still loads", F.Settings(tempfile.mkdtemp(), lambda m: None).get("schedule")["enabled"] is False)
# the shared rules: the short-run warning fires with the windows off; the suggestions use short run and starts per day
tr = X.Tracker(None)
evs = []
mod = types.SimpleNamespace(remote_armed=True, latest=None)
sc = F.Scheduler(st, tr, mod, lambda c, s: evs.append(("sent", c)), lambda m: None, evs.append)
tr.transitions.extend([{"from": 0, "to": 2, "epoch": T0}, {"from": 3, "to": 4, "epoch": T0 + 600}])
sc._on_event({"type": "transition", "from": 4, "to": 0, "epoch": T0 + 15 * 60})
check("C: short-run warning still works with the start windows off",
      any(isinstance(e, dict) and e.get("kind") == "short_run" and e["p"]["limit"] == 25 for e in evs), evs)
sc.last_eval = 0
sc.tick()
check("C: the start windows never start the boiler now", sc.status["code"] == "off" and ("sent", "start") not in evs)
opt = F.Optimiser(st, None, types.SimpleNamespace(latest_cfg={"raw": {}}), lambda m: None)
day = {"day": "2000-01-03", "running_s": 3600, "runs": [10.0, 12.0, 11.0], "starts": 9, "rescues": 0, "alarms": {},
       "drift_power_max_share": 0.1, "fuel_silo_kg": 5, "fuel_est_kg": 5}
keys = {x["key"]: x["p"] for x in opt.suggest([day], {"kg": 5})}
check("C: suggestions still use short run (25) and starts per day (5)",
      keys.get("short_runs", {}).get("target") == 25 and keys.get("many_starts", {}).get("limit") == 5, keys)
dash = page("dashboard.html")
check("C: /classic has no Old start windows card", not re.search(r'id="(oldSch|schSave|schWins|schEnabled)"', dash)
      and "saveSchedule" not in dash and "old_sch" not in dash)

# ---------------------------------------------------------------- D: three /classic cards -> one line
home = page("home.html")
check("D: /classic: no tank sensor, outside temperature or ash bin card",
      not re.search(r'id="(vvEnabled|vvSource|wxQ|wxFind|abSize|abSave)"', dash))
check("D: /classic: one line to the new page, EN and DA", 'href="home#settings" id="t_moved_home"' in dash
      and 'moved_home:"These settings are on the new page: Settings"' in dash
      and 'moved_home:"Disse indstillinger er på den nye side: Indstillinger"' in dash)
check("D: /home has the same controls", all(k in home for k in ('"vvb.enabled"', '"vvb.source"', '"vvb.max_age_min"',
      '"vvb.safe_c"', '"ash.bin_kg"', "api/weather/find", "weather:{enabled:false}")))
check("D: /home has the how-to for each tank sensor source (was only on /classic)",
      home.count("vv_how_mqtt:") == 2 and home.count("vv_how_onewire:") == 2 and 'id="vvHow"' in home)

# ---------------------------------------------------------------- E: fire rescues
rows, e = [], T0
for stt, mins in ((2, 5), (3, 5), (4, 30), (7, 2), (4, 40), (7, 3), (6, 1), (0, 60), (2, 5), (3, 5), (4, 20), (7, 2)):
    for _ in range(mins * 6):
        rows.append({"epoch": e, "state": stt, "alarm": 5 if stt == 6 else 0})
        e += 10
s = F.summarise_rows(rows)
check("E: the day summary keeps each rescue: relit, alarm, still going at the end of the day",
      s["rescues"] == 3 and [x[1] for x in s["rescue_at"]] == ["ok", "failed", None]
      and s["rescue_at"][0][0] == round(T0 + 40 * 60), s["rescue_at"])
days = [dict(day, day="2000-01-02", rescue_at=[[T0 - 86400, "ok"]], rescues=1),
        dict(day, day="2000-01-03", rescue_at=s["rescue_at"], rescues=3)]
p = {x["key"]: x["p"] for x in opt.suggest(days, {"kg": 5})}["rescues"]
check("E: the suggestion says how many, over which days, the last 3 times newest first and how many worked",
      p["n"] == 4 and p["days"] == 2 and p["from"] == "2000-01-02" and len(p["last"]) == 3
      and p["last"][0][0] > p["last"][1][0] > p["last"][2][0] and p["ok"] == 2, p)
old_day = dict(day, rescues=2)                 # a summary from before the times were kept
check("E: an old day summary without times still counts", F.rescue_list([old_day]) == [[None, None], [None, None]])
health = lambda ds: next(c for c in opt.checks([], ds) if c["k"] == "rescue")
check("E: Burner health row: none -> ok", health([dict(day, rescue_at=[], rescues=0)])["why"] == "none")
check("E: one that worked -> ok, noted", health([dict(day, rescue_at=[[T0, "ok"]], rescues=1)])["why"] == "one")
r = health(days)
check("E: one that ended in an alarm -> look at it, with the list of times",
      r["st"] == "bad" and r["why"] == "fail" and r["p"]["f"] == 1 and len(r["p"]["list"]) == 4, r)
check("E: no burning yet -> learning", health([])["st"] == "learn")
shared = page("shared.js")
check("E: words EN and DA ('Redder ilden'), with the advice kept",
      'bh_rescue:"Fire rescues"' in shared and 'bh_rescue:"Redder ilden"' in shared and shared.count("rs_advice:") == 2
      and "function rescText" in shared and 'key==="rescues"' in dash)

# ---------------------------------------------------------------- F: starts and settings
rec = lambda i, **k: dict({"epoch": T0 + i * 3600, "result": "ok", "minutes": 20.0, "ignition_min": 15.0, "relight": False,
                           "flame_s": 300, "batch": "—", "cfg": {"014": 60 if i < 5 else 40, "018": 20}}, **k)
R = [rec(i) for i in range(10)]
R[7] = rec(7, minutes=0.3, ignition_min=0.0, flame_s=None, auger_s=1, boiler=65)     # went on burning
check("F: a start with no lighting step under 2 min is a resume", F.resumed(R[7]) and not F.resumed(R[6])
      and not F.resumed({"result": "ok", "flame_s": 60}) and not F.lit(R[7]))
cd = tempfile.mkdtemp()
with open(os.path.join(cd, "cfg_changes.jsonl"), "w") as f:
    f.write(json.dumps({"time": T0 - 86400, "field": "014", "old": 80, "new": 60}) + "\n")
    f.write(json.dumps({"time": T0 + 4.5 * 3600, "field": "014", "old": 60, "new": 40}) + "\n")
ig = F.IgnitionLog(F.Settings(tempfile.mkdtemp(), lambda m: None), types.SimpleNamespace(listeners=[]),
                   types.SimpleNamespace(latest_cfg={"raw": {"014": 40, "018": 20}}, cfg_hist=types.SimpleNamespace(path=os.path.join(cd, "cfg_changes.jsonl"))),
                   None, lambda m: None)
ig.records = R
rep = ig.report()
check("F: the resume is marked in the records", rep["records"][7].get("resumed") is True and "resumed" not in rep["records"][6])
bs = {tuple(sorted(x["cfg"].items())): x for x in rep["by_settings"]}
a, b = bs[(("014", 40),)], bs[(("014", 60),)]
check("F: settings compared: resume left out, time to flame next to time to full heat, the settings in force now",
      a["n"] == 4 and a["flame_min"] == 5.0 and a["median_min"] == 20.0 and a["now"] and not b["now"] and b["n"] == 5, rep["by_settings"])
check("F: the settings changes between the last 10 starts (older ones left out)",
      [(c["field"], c["old"], c["new"]) for c in rep["cfg_changes"]] == [("014", 60, 40)], rep["cfg_changes"])
hd = tempfile.mkdtemp()
with open(os.path.join(hd, "hist-2000-01-02.jsonl"), "w") as f:
    for i, stt in enumerate((0, 2, 3, 4, 4, 0, 0)):
        f.write(json.dumps({"epoch": T0 - 86400 + i * 600, "state": stt}) + "\n")
with open(os.path.join(hd, "ignitions.jsonl"), "w") as f:
    f.write(json.dumps(rec(-30)) + "\n")
ig2 = F.IgnitionLog(F.Settings(tempfile.mkdtemp(), lambda m: None), types.SimpleNamespace(listeners=[]),
                    types.SimpleNamespace(latest=None, latest_cfg=None), hd, lambda m: None)
check("F: after a restart the monitor still knows when the last burn ended (Off (h) is not empty)",
      ig2.last_burn_end == T0 - 86400 + 5 * 600, ig2.last_burn_end)
o2 = F.Optimiser(F.Settings(tempfile.mkdtemp(), lambda m: None), None,
                 types.SimpleNamespace(ignitions=types.SimpleNamespace(records=[dict(r, peak_lux=120) for r in R])), lambda m: None)
fl = next(i for i in o2.health(days=[])["items"] if i["key"] == "failed")
check("F: Burner health counts the starts without the resume", fl["n"] == 9, fl)
check("F: /home shows the starts with the settings changes and the comparison (shared code)",
      "function startsHTML" in shared and "function cfgCmpHTML" in shared and "st_cfg_now" in shared
      and "startsHTML(IGN" in home)

# ---------------------------------------------------------------- G: flame sensor
def burn(t, minutes, lux=100.0, power=60.0):
    out = [{"epoch": t + i * 10, "state": 2} for i in range(30)]
    t += 300
    out += [{"epoch": t + i * 10, "state": 4, "flame": lux, "power_pct": power, "fan_rpm": 1500, "boiler_c": 60}
            for i in range(minutes * 6)]
    t += minutes * 60
    return out + [{"epoch": t + i * 10, "state": 0} for i in range(30)]

one = F.lux_samples_from_rows(burn(T0, 40))
check("G: a 40-min burn gives 6 samples (5 min each, from 10 min into the burn)", len(one) == 6
      and all(x["burn"] == one[0]["burn"] and x["v"] == F.LUX_V for x in one), len(one))
gd = tempfile.mkdtemp()
st_g = F.Settings(gd, lambda m: None)
for n, day_ in enumerate(("2000-01-01", "2000-01-02")):
    with open(os.path.join(gd, f"hist-{day_}.jsonl"), "w") as f:
        for r in burn(T0 - (2 - n) * 86400, 25) + burn(T0 - (2 - n) * 86400 + 6 * 3600, 25):
            f.write(json.dumps(r) + "\n")
lm = F.LuxMonitor(st_g, types.SimpleNamespace(since=None), gd, lambda m: None, lambda e: None)
b = lm.analyse()["bands"]["mid"]
check("G: learned from the saved history: 4 short burns of 25 min -> ready (30 min over 3 burns)",
      b["burns"] == 4 and b["min"] >= 30 and not b["learning"] and b["baseline"] == 100.0, b)
check("G: the samples file is written in the new format", all(json.loads(l).get("v") == F.LUX_V
      for l in open(os.path.join(gd, "lux_samples.jsonl"))))
lm.samples = lm.samples[:5]
b = lm.analyse()["bands"]["mid"]
check("G: progress per band while learning", b["learning"] and b["min"] == 25 and b["burns"] == 2, b)
lm.reset()
r = lm.summary()
check("G: sensor cleaned starts fresh", r["bands"]["mid"]["n"] == 0 and r["reset_epoch"] > 0 and "samples" not in r)
check("G: no '6 h' text left", "6 h of steady" not in dash and "6 timers stabil" not in dash
      and "about 6 hours of steady" not in home and "6 timers stabil" not in home)

# ---------------------------------------------------------------- browser: /home History, made-up data
try:
    from playwright.sync_api import sync_playwright
except Exception as ex:                          # noqa
    print(f"SKIP browser part ({ex})")
    sync_playwright = None
if sync_playwright:
    import _synth
    MADE = """()=>{ const b=nowEp()-3*86400;
      IGN=Object.assign({},IGN||{},{records:[{epoch:b,result:"ok",flame_min:9.0,minutes:24,off_h:7,relight:false,cfg:{"014":60}},
        {epoch:b+3600,result:"ok",minutes:0.3,ignition_min:0,relight:false,resumed:true},{epoch:b+7200,result:"failed",alarm:4,minutes:14}],
        cfg_changes:[{time:b+1800,field:"014",old:60,new:40}],by_settings:[{cfg:{"014":40},n:3,ok:3,flame_min:4,median_min:18,now:true},{cfg:{"014":60},n:2,ok:1,flame_min:9,median_min:24,now:false}]});
      OPT=Object.assign({},OPT,{lux:{status:"learning",need_min:30,need_burns:3,bands:{low:{n:0},mid:{n:4,min:20,burns:2,learning:true},high:{n:7,min:35,burns:3,learning:false,baseline:90,current:70,drop_pct:22}}},
        health:{checks:[{k:"light",st:"ok",why:"ok",p:{a:4,usual:4}},{k:"flame",st:"ok",why:"ok"},{k:"fan",st:"learn"},{k:"ash",st:"learn"},{k:"short",st:"ok",why:"ok"},
          {k:"rescue",st:"bad",why:"fail",p:{a:1,f:1,d:7,list:[[b+5000,"failed"]]}}]}});
      BH_USER={}; renderHistory();
      const q=s=>document.querySelector(s);
      return {open:[...document.querySelectorAll('#health .hc>details')].map(d=>d.id+":"+d.open), rows:document.querySelectorAll('#health .hc>*').length,
        marks:document.querySelectorAll('#bh-light .stMark').length, res:[...document.querySelectorAll('#bh-light .stRow')].map(r=>r.dataset.res),
        now:document.querySelectorAll('#cfgCmp [data-now]').length, bands:document.querySelectorAll('#fsBands .fsRow').length,
        rescue:q('#rsList')?q('#rsList').innerText:'', lh:!!q('#bh-light #lhSteps')&&!!q('#bh-light [data-lh=copy]'),
        card:!!q('#v-history > #lightHelp'), wide:document.documentElement.scrollWidth>window.innerWidth+1}; }"""
    errs = []
    with _synth.Replay(8873) as rp, sync_playwright() as pw:
        br = pw.chromium.launch()
        for lang, w in (("en", 375), ("da", 1280)):
            ctx = br.new_context(viewport={"width": w, "height": 800}, locale="en-GB" if lang == "en" else "da-DK")
            ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');}}catch(e){{}}")
            pg = ctx.new_page()
            pg.on("pageerror", lambda x: errs.append(str(x)))
            pg.on("console", lambda m: m.type == "error" and errs.append(m.text))
            pg.goto(rp.base + "/home?replaybanner=0#history", wait_until="load")
            pg.wait_for_selector("#health", timeout=30000)
            g = pg.evaluate(MADE)
            tag = f"[{lang} {w}px]"
            check(f"{tag} six rows; Fire rescues (bad) open, Time to flame and Flame sensor closed",
                  g["rows"] == 6 and g["open"] == ["bh-light:false", "bh-flame:false", "bh-rescue:true"], g["open"])
            check(f"{tag} the starts with a settings change line, a resume marked, the settings in force now",
                  g["marks"] == 1 and g["res"] == ["failed", "resumed", "ok"] and g["now"] == 1, g)
            check(f"{tag} the lighting help (steps, Copy report) is in the same row, not a card of its own",
                  g["lh"] and not g["card"], g)
            check(f"{tag} flame sensor: one line per band; the rescue time and that it did not relight",
                  g["bands"] == 3 and len(g["rescue"]) > 10, g["rescue"])
            check(f"{tag} fits the screen", not g["wide"])
            pg.click("#bh-rescue > summary")
            pg.evaluate("renderHistory()")
            check(f"{tag} closed by hand stays closed", pg.evaluate("document.getElementById('bh-rescue').open") is False)
            ctx.close()
        br.close()
    check("browser: no errors", not errs, errs[:3])

print("ALL PASS" if not FAILED else f"{FAILED} FAILED")
sys.exit(1 if FAILED else 0)
