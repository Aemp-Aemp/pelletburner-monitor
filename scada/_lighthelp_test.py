"""The lighting help card ("Trouble lighting?"): made-up values only.
1) burner_features.lighting_help: the verdict from the last 10 starts, each lighting setting against the factory
   value for the software and boiler type, the marks, the start list (no pellet batch names).
2) In a real browser (headless Chromium, Playwright) on the replay of synthetic data: the card on /home History and
   on /classic Analysis in English and Danish, the same texts in both languages, the lighting report for the
   clipboard, the link from the Burner health row, and that the pages send nothing (GET only).
Read only: nothing here can change a boiler setting. No boiler, no serial port, no internet.
Part 2 is skipped (line "SKIP") when Playwright is not installed in this Python."""
import inspect, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import burner_features as F

FAILED = 0


def check(name, cond, extra=""):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""), flush=True)
    FAILED += 0 if cond else 1


T0 = 946_700_000                                             # a made-up day in 2000


def rec(i, mins=4.0, res="ok", **kw):
    r = {"epoch": T0 + i * 3600, "result": res, "flame_s": round(mins * 60) if res == "ok" else None,
         "minutes": mins + 15 if res == "ok" else 14.0, "relight": False, "batch": "Made-up batch",
         "cfg": {"014": 40, "018": 20}}
    r.update(kw)
    return r


CFG = {"014": 40, "015": 80, "017": 800, "018": 20, "001": 3400, "033": 1, "035": 5.0}
H = F.lighting_help

# ---- the verdict
check("2 starts -> too few to say", H([rec(0), rec(1)], CFG, "1.40")["verdict"]["k"] == "few")
h = H([rec(i, 4.0 + (i % 3) * 0.5) for i in range(12)], CFG, "1.40", usual=4.5)
check("steady 4-5 min -> lights fine", h["verdict"] == {"k": "ok", "n": 10, "lit": 10, "failed": 0, "min": 4.5, "usual": 4.5},
      h["verdict"])
h = H([rec(i, 4.0) for i in range(9)] + [rec(9, res="failed", alarm=4)], CFG, "1.40", usual=4.0)
check("one failure in 10 -> still fine, the failure is counted", h["verdict"]["k"] == "ok" and h["verdict"]["failed"] == 1)
h = H([rec(i, 4.0) for i in range(8)] + [rec(8, res="failed", alarm=4), rec(9, res="failed", alarm=4)], CFG, "1.40")
check("2 of the last 10 did not light -> often fails", h["verdict"]["k"] == "fails" and h["verdict"]["failed"] == 2
      and h["verdict"]["n"] == 10)
h = H([rec(i, 8.5) for i in range(10)], CFG, "1.40")
check("over 6 min to flame -> slow", h["verdict"]["k"] == "slow" and h["verdict"]["min"] == 8.5)
h = H([rec(i, 3.0) for i in range(7)] + [rec(7 + i, 5.0) for i in range(3)], CFG, "1.40", usual=3.0)
check("clearly over this boiler's usual -> slow", h["verdict"]["k"] == "slow")
h = H([rec(i, 3.0) for i in range(7)] + [rec(7 + i, 3.6) for i in range(3)], CFG, "1.40", usual=3.0)
check("a little over the usual -> fine", h["verdict"]["k"] == "ok")
h = H([rec(i, 4.0) for i in range(10)] + [rec(10 + i, res="failed", relight=True) for i in range(3)], CFG, "1.40")
check("relights are left out", h["verdict"]["k"] == "ok" and h["verdict"]["n"] == 10)
h = H([rec(i, 4.0) for i in range(5)] + [rec(5 + i, res="stopped") for i in range(5)], CFG, "1.40")
check("starts the owner stopped are not judged", h["verdict"]["n"] == 5 and h["verdict"]["k"] == "ok")
h = H([rec(i, res="failed") for i in range(3)] + [rec(3 + i, 4.0) for i in range(10)], CFG, "1.40")
check("only the last 10 starts count", h["verdict"]["k"] == "ok" and len(h["starts"]) == 10)

# ---- the settings table
row = lambda h, k: [r for r in h["rows"] if r["id"] == k][0]
h = H([], CFG, "1.40.1")
check("software 1.40.1, 15 kW type", (h["family"], h["type"], h["sw"]) == ("1.40", 1, "1.40.1"))
check("all rows: the lighting settings, fuel and PID P", [r["id"] for r in h["rows"]] == list(F.LIGHT_IDS))
check("1.40 factory values, 15 kW", [r["fac"] for r in h["rows"]] == [40, 80, 800, 20, 3400, 5])
check("factory settings -> nothing marked (TÆND LUFT: not used by 1.40)",
      [r["mark"] for r in h["rows"]] == [None, "unused", None, None, None, None], [r["mark"] for r in h["rows"]])
check("worked on one 15 kW boiler: 40 s, 800 s, 20 doses (no TÆND LUFT: 1.40 does not use it)",
      [r["ref"] for r in h["rows"]] == [40, None, 800, 20, None, None])
h = H([], dict(CFG, **{"015": 55}), "1.40")
check("1.40: TÆND LUFT off the factory value is still only 'not used'", row(h, "015")["mark"] == "unused")
for doses, want in ((0, "doses"), (1, "doses"), (2, "diff"), (20, None)):
    h = H([], dict(CFG, **{"018": doses}), "1.40")
    check(f"MAX DOSER {doses} -> {want}", row(h, "018")["mark"] == want, row(h, "018"))
h = H([], CFG, "1.40", sync=True)
check("settings sync on -> said in the answer", h["sync"] is True and H([], CFG, "1.40")["sync"] is False)
h = H([], dict(CFG, **{"033": 0, "001": 2400}), "1.30.15")
check("1.30 factory values, 10 kW", (h["family"], [r["fac"] for r in h["rows"]]) == ("1.30", [28, 80, 600, 20, 2400, 5]))
check("1.30: start fuel 40 s and timeout 800 s differ from the factory",
      row(h, "014")["mark"] == "diff" and row(h, "017")["mark"] == "diff" and row(h, "015")["mark"] is None)
h = H([], dict(CFG, **{"033": 2, "001": 5400}), "1.41")
check("1.40 factory values, 25 kW", [r["fac"] for r in h["rows"]] == [28, 80, 600, 20, 5400, 5])
h = H([], dict(CFG, **{"014": 60, "018": 10}), "1.40")
check("start fuel 60 s -> marked high, 10 doses -> differs", row(h, "014")["mark"] == "high" and row(h, "018")["mark"] == "diff")
h = H([], dict(CFG, **{"014": 20, "017": 300}), "1.40")
check("start fuel 20 s -> low, timeout 300 s -> short", row(h, "014")["mark"] == "low" and row(h, "017")["mark"] == "short")
h = H([], dict(CFG, **{"001": 3000, "035": 9.0}), "1.40")
check("fuel rate and PID P off the factory value -> differ", row(h, "001")["mark"] == "diff" and row(h, "035")["mark"] == "diff")
h = H([], CFG, "")
check("software not typed in -> 1.40 values, sw None, not known", h["family"] == "1.40" and h["sw"] is None and h["sw_known"] is False)
check("software typed in -> known", H([], CFG, "1.40.1")["sw_known"] is True and H([], CFG, "1.30.15")["sw_known"] is True)
for old in ("1.13", "1.4", "V1.10"):
    h = H([], dict(CFG, **{"033": 0, "001": 2400}), old)
    check(f"software {old} (below 1.30 or unclear) -> unknown: the 1.40 values and the note, never the 1.30 column",
          h["sw_known"] is False and h["family"] == "1.40" and [r["fac"] for r in h["rows"]] == [40, 80, 800, 20, 2400, 5])
    check(f"software {old}: TÆND LUFT is not called 'not used' (only known 1.40 skips it)", row(h, "015")["mark"] != "unused", row(h, "015"))
check("software not typed in: TÆND LUFT is not called 'not used'", row(H([], dict(CFG, **{"015": 55}), ""), "015")["mark"] == "diff")
h = H([], {}, None)
check("no settings from the boiler yet -> rows without values, no error",
      h["type"] is None and all(r["now"] is None and r["fac"] is None and r["mark"] is None for r in h["rows"]))
h = H([], {"014": "abc", "033": 7, "017": None}, "v1.40")
check("odd values from the boiler -> ignored", h["type"] is None and row(h, "014")["now"] is None and h["family"] == "1.40")
check("software family", [F.sw_family(x) for x in ("1.30", "1.30.15", "V1.39", "1.40", "1.41", "2.0", "", None, "x", "1.13", "1.4")]
      == ["1.30", "1.30", "1.30", "1.40", "1.40", "1.40", None, None, None, None, None])

# ---- the start list (for the report): newest first, no pellet batch name, no free text
h = H([rec(0, 4.0), rec(1, res="failed", alarm=4), rec(2, 5.5, cfg={"014": 60, "018": 10})], CFG, "1.40")
check("starts: newest first", [s["epoch"] for s in h["starts"]] == [T0 + 7200, T0 + 3600, T0])
check("starts: time to flame, result, alarm, the settings at that start",
      h["starts"][0] == {"epoch": T0 + 7200, "result": "ok", "flame_min": 5.5, "minutes": 20.5, "alarm": None,
                         "start_fuel": 60, "max_doses": 10} and h["starts"][1]["alarm"] == 4
      and h["starts"][1]["flame_min"] is None, h["starts"])
check("nothing personal in the answer (no batch names)", "Made-up" not in json.dumps(h) and "batch" not in json.dumps(h))

# ---- read only: the function and the report only read
src = inspect.getsource(F.lighting_help)
check("lighting_help sends and saves nothing", not re.search(r"svc_once|cfg_chg|\.set\(|set_internal|open\(|run_mode", src))


class Mod:
    latest_cfg = {"raw": CFG}


class Tr:
    listeners = []


class St:
    def get(self, k):
        return {"sw": "1.40"} if k == "controller_info" else {}


ig = F.IgnitionLog(St(), Tr(), Mod(), None, lambda m: None)
ig.records = [rec(i, 4.0) for i in range(4)]
import time as _t
for r in ig.records:                                          # recent, so the usual value exists
    r["epoch"] = _t.time() - 86400 + r["epoch"] - T0
rp = ig.report()
check("/api/ignitions carries the help", rp["help"]["verdict"]["k"] == "ok" and rp["help"]["type"] == 1
      and rp["help"]["sw"] == "1.40" and rp["help"]["verdict"]["usual"] == 4.0, rp.get("help"))
Mod.latest_cfg = None
check("no settings read yet: the report still works", ig.report()["help"]["type"] is None)

# ---- the words: English and Danish have the same keys and {placeholders} (read from scada/shared.js)
with open(os.path.join(HERE, "shared.js"), encoding="utf-8") as f:
    js = f.read()
body = js[js.index("const LH_TXT={"):js.index("const lhT=")]
en, da = body[body.index(" en:{"):body.index(" da:{")], body[body.index(" da:{"):]
pairs = lambda s: dict(re.findall(r'(\w+):"((?:[^"\\]|\\.)*)"', s))
EN, DA = pairs(en), pairs(da)
holes = lambda s: sorted(set(re.findall(r"\{(\w+)\}", s)))
check(f"LH_TXT: {len(EN)} texts, the same keys in en and da", len(EN) > 40 and set(EN) == set(DA),
      sorted(set(EN) ^ set(DA)))
check("LH_TXT: the same placeholders", not [k for k in EN if k in DA and holes(EN[k]) != holes(DA[k])])
check("LH_TXT: no empty text", all(EN.values()) and all(DA.values()))
check("LH_TXT: Danish is not a copy of English",
      not [k for k in EN if EN[k] == DA.get(k) and k not in ("n_035",)], [k for k in EN if EN[k] == DA.get(k)])
check("the checklist starts with the physical checks, settings come later",
      "Clean the burner pot" in EN["s1"] and "flame sensor" in EN["s2"] and "igniter" in EN["s3"] and "pellets" in EN["s4"]
      and "factory settings" in EN["s5"] and "30 %" in EN["s6"] and "one setting" in EN["s7"])
check("values from one boiler are called an example", "example" in EN["fac_note"] and "eksempel" in DA["fac_note"])
check("read-only text: the card only reads, it never changes a boiler setting",
      EN["ro"] == "This card only reads; it never changes a boiler setting." and "kort" in DA["ro"])
check("MAX DOSER below 2 has a warning text (0 = about 255, 1 = none)", "255" in EN["m_doses"] and "255" in DA["m_doses"])
check("TÆND LUFT: 'not used by 1.40' in both languages", "1.40" in EN["m_unused"] and "1.40" in DA["m_unused"])
check("settings sync: a line for the 'change one setting' step", "sync" in EN["s7_sync"] and "page" in EN["s7_sync"]
      and "siden" in DA["s7_sync"])
check("step 6: the rpm points are from software 1.40 on the 10/15 kW types", "1.40" in EN["s6"] and "10 and 15 kW" in EN["s6"]
      and "1.40" in DA["s6"])
check("near Copy: the report includes your start times", "start times" in EN["copy_note"] and DA["copy_note"])
check("the unknown-software note covers 'unknown' too", "unknown" in EN["sw_note"] and "ukendt" in DA["sw_note"])
with open(os.path.join(ROOT, "docs", "guide.html"), encoding="utf-8") as f:
    guide = f.read()
check("guide: SUPPLERING spelled as on the boiler", "SUPPLERING" in guide and "SUPLERING" not in guide)
check("guide: the 'worked on one boiler' example has no TÆND LUFT",
      "TÆND LUFT" not in guide[guide.index("What worked on one 15 kW boiler"):][:400])
check("guide: the card only reads, and the settings sync note", "The card only reads; it never changes a boiler setting" in guide
      and "turn the sync off" in guide)

# ---- the pages, in a real browser
try:
    from playwright.sync_api import sync_playwright
    import _synth
except ImportError:
    print("SKIP: Playwright is not installed (pip install playwright; playwright install chromium)")
    print("ALL PASS" if not FAILED else f"{FAILED} FAILED")
    sys.exit(1 if FAILED else 0)

SLOW = H([rec(i, 9.0, cfg={"014": 60, "018": 10}) for i in range(9)] + [rec(9, res="failed", alarm=4)],
         dict(CFG, **{"014": 60, "018": 10}), "1.40", usual=4.0)
FINE = H([rec(i, 4.0) for i in range(10)], CFG, "1.40", usual=4.0)
WANT = {"en": ("Trouble lighting?", "Lights slowly: about 9.0 min to flame in the last 10 starts.",
               "Lights fine: about 4.0 min to flame in the last 10 starts.", "Copy lighting report",
               "Lighting report (pellet boiler monitor 9.9.9)", "did not light (alarm 4)", "lit, 9.0 min to flame",
               "Start fuel (TÆNDBRÆNDSEL): 60 s / 40 s  !", "Fuel rate (BRÆNDSEL KG/T): 3.4 kg/h / 3.4 kg/h"),
        "da": ("Problemer med optænding?", "Tænder langsomt: ca. 9,0 min til flamme i de seneste 10 starter.",
               "Tænder fint: ca. 4,0 min til flamme i de seneste 10 starter.", "Kopiér optændingsrapport",
               "Optændingsrapport (pillefyr-monitor 9.9.9)", "tændte ikke (alarm 4)", "tændte, 9,0 min til flamme",
               "Tændbrændsel (TÆNDBRÆNDSEL): 60 s / 40 s  !", "Brændsel (BRÆNDSEL KG/T): 3,4 kg/h / 3,4 kg/h")}
# how each page is given a made-up help answer and drawn again
# /home: the help is inside the Time to flame row of Burner health (#bh-light); BH_USER = the owner's open / close
DRAW = {"home": "h=>{ IGN=Object.assign({},IGN||{},{help:h}); BH_USER={}; renderHistory(); }",
        "classic": "h=>{ LH_OPEN=null; renderIgnitions(Object.assign({},lastIgR||{records:[]},{help:h})); }"}
CARD = """()=>{ const c=document.getElementById('lightHelp'), d=document.getElementById('lhDet')||document.getElementById('bh-light');
  if(!c||!d) return null;
  return {open:d.open, title:document.getElementById('lhTitle').textContent, verdict:document.getElementById('lhVerdict').textContent,
    rows:[...c.querySelectorAll('#lhTable tr[data-id]')].map(r=>[r.dataset.id,r.dataset.mark||'',...[...r.cells].map(x=>x.innerText.trim())]),
    steps:c.querySelectorAll('#lhSteps li').length, btn:c.querySelector('[data-lh=copy]').textContent,
    link:c.querySelector('a[href^=guide]').getAttribute('href'), text:c.innerText, wide:c.scrollWidth>c.clientWidth+1}; }"""

with _synth.Replay(8819) as rp, sync_playwright() as pw:
    br = pw.chromium.launch()
    got = rp.get("/api/ignitions")["help"]
    check("replay: /api/ignitions has the help (verdict, 6 rows, the last starts)",
          got and got["verdict"]["k"] in ("ok", "slow", "few") and len(got["rows"]) == 6 and len(got["starts"]) >= 1, got)
    with open(os.path.join(ROOT, "docs", "guide.html"), encoding="utf-8") as f:
        check("the guide has the section the card links to", 'id="trouble-after-upgrade"' in f.read())
    for lang in ("en", "da"):
        w = WANT[lang]
        for page, (path, view) in {"home": ("/home?replaybanner=0#history", {"width": 390, "height": 844}),
                                   "classic": ("/classic?replaybanner=0#opt", {"width": 1366, "height": 900})}.items():
            ctx = br.new_context(locale="da-DK" if lang == "da" else "en-GB", viewport=view)
            ctx.add_init_script(f"try{{localStorage.setItem('burner_lang','{lang}');}}catch(e){{}}")
            blocked, errs = [], []

            def guard(route, blocked=blocked):
                q = route.request
                if q.method != "GET" or not q.url.startswith(rp.base + "/"):
                    blocked.append(f"{q.method} {q.url}")
                    return route.abort()
                route.continue_()
            ctx.route("**/*", guard)
            pg = ctx.new_page()
            pg.on("console", lambda m, e=errs: m.type == "error" and e.append(m.text))
            pg.on("pageerror", lambda x, e=errs: e.append("pageerror: " + str(x)))
            pg.goto(rp.base + path, wait_until="load")
            pg.wait_for_function("document.getElementById('lhDet')||document.getElementById('bh-light')", timeout=20000)
            tag = f"[{lang}] {page}"
            c = pg.evaluate(CARD)
            check(f"{tag}: the card shows with the replay's own data, closed while lighting is fine",
                  c and c["title"] == w[0] and c["steps"] == 8 and not c["open"], c)
            # lighting slow, start fuel 60 s and 10 doses on a 15 kW boiler with software 1.40
            pg.evaluate(DRAW[page], SLOW)
            c = pg.evaluate(CARD)
            check(f"{tag}: slow lighting -> the card is open and says so", c["open"] and c["verdict"] == w[1], c["verdict"])
            rows = {r[0]: r for r in c["rows"]}
            check(f"{tag}: start fuel now 60 s (marked), factory 40 s, one 15 kW boiler 40 s",
                  rows["014"][1] == "high" and rows["014"][3].startswith("60 s") and rows["014"][4:] == ["40 s", "40 s"], rows["014"])
            check(f"{tag}: max doses 10 differs from factory 20", rows["018"][1] == "diff" and rows["018"][4] == "20", rows["018"])
            check(f"{tag}: timeout as the factory: no mark; TÆND LUFT: not used by 1.40",
                  rows["017"][1] == "" and rows["015"][1] == "unused" and (EN if lang == "en" else DA)["m_unused"] in rows["015"][2]
                  and rows["015"][5] == "–", rows)
            T = EN if lang == "en" else DA
            check(f"{tag}: the line near Copy says the report has the start times", T["copy_note"] in c["text"])
            check(f"{tag}: sync off, software known: no sync line, no software note",
                  T["s7_sync"] not in c["text"] and T["sw_note"] not in c["text"])
            pg.evaluate(DRAW[page], H([rec(i, 9.0) for i in range(10)], dict(CFG, **{"018": 0}), "1.13", usual=4.0, sync=True))
            c2 = pg.evaluate(CARD)
            check(f"{tag}: sync on, software 1.13, MAX DOSER 0: the sync line, the software note and the doses warning",
                  T["s7_sync"] in c2["text"] and T["sw_note"] in c2["text"] and T["m_doses"] in c2["text"]
                  and {r[0]: r for r in c2["rows"]}["018"][1] == "doses", c2["text"])
            pg.evaluate(DRAW[page], SLOW)
            check(f"{tag}: fuel rate in kg/h as on the boiler's screen", "kg/h" in rows["001"][3] and "3" in rows["001"][3], rows["001"])
            check(f"{tag}: the note about 60 s start fuel shows", ("60 s" in c["text"]) and ("3–5" in c["text"]))
            check(f"{tag}: no setting numbers (014 ...) and no batch name in the card",
                  not re.search(r"\b0(14|15|17|18|33|35)\b", c["text"]) and "Made-up" not in c["text"])
            check(f"{tag}: copy button and a link to the guide section",
                  c["btn"] == w[3] and c["link"] == "guide#trouble-after-upgrade", (c["btn"], c["link"]))
            check(f"{tag}: the card fits its width", not c["wide"])
            rep = pg.evaluate("h=>lhReport(h,'9.9.9')", SLOW)
            lines = rep.split("\n")
            check(f"{tag}: report: title with the monitor version, boiler type and software",
                  lines[0] == w[4] and "15 kW" in lines[1] and "1.40" in lines[1] and lines[2] == w[1], lines[:3])
            check(f"{tag}: report: the settings, now / factory", w[7] in rep and w[8] in rep, rep)
            check(f"{tag}: report: 10 starts, newest first, lit or not",
                  w[5] in [x for x in lines if x.startswith("1. ")][0] and w[6] in [x for x in lines if x.startswith("2. ")][0]
                  and any(x.startswith("10. ") for x in lines) and not any(x.startswith("11. ") for x in lines), rep)
            check(f"{tag}: report: nothing personal (no batch, address, topic, place)",
                  not re.search(r"Made-up|batch|http|ntfy|\d+\.\d+\.\d+\.\d+|@", rep), rep)
            check(f"{tag}: report: software known: no software note", T["sw_note"] not in rep, rep)
            rep2 = pg.evaluate("h=>lhReport(h,'9.9.9')", H([rec(i, 9.0) for i in range(10)], CFG, "1.13"))
            check(f"{tag}: report: software unknown: the software note is in it", T["sw_note"] in rep2, rep2)
            pg.click("[data-lh=copy]")
            pg.wait_for_timeout(300)
            check(f"{tag}: Copy lighting report sends nothing" + (f" {blocked[:2]}" if blocked else ""), not blocked)
            # closed by hand: it stays closed when the page draws again
            if page == "home":
                pg.click("#bh-light > summary")
            else:
                pg.evaluate("document.getElementById('lhDet').open=false")
            pg.wait_for_timeout(100)
            pg.evaluate(DRAW[page].replace("LH_OPEN=null;", "").replace("BH_USER={};", ""), SLOW)
            check(f"{tag}: closed by hand stays closed", pg.evaluate(CARD)["open"] is False)
            pg.evaluate(DRAW[page], FINE)
            c = pg.evaluate(CARD)
            check(f"{tag}: lights fine -> closed, and says so", not c["open"] and c["verdict"] == w[2], c["verdict"])
            if page == "home":
                # the Time to flame row opens by itself when the check is bad, and Now's line leads to it
                o = pg.evaluate("""h=>{ IGN=Object.assign({},IGN||{},{help:h}); BH_USER={};
                    OPT=Object.assign({},OPT,{health:{checks:[{k:"light",st:"bad",why:"fail",p:{a:2,n:10}}]}});
                    renderHistory(); return document.getElementById('bh-light').open; }""", FINE)
                check(f"{tag}: Burner health: Time to flame opens by itself when lighting is bad", o is True)
                pg.evaluate("""()=>{ const el=document.createElement('div'); el.id='tmpLine'; document.body.prepend(el); renderHealthLine(el); }""")
                check(f"{tag}: the Now line leads to that row", pg.evaluate("(document.querySelector('#tmpLine [data-bhgo]')||{}).dataset.bhgo") == "light")
                o = pg.evaluate("""()=>{ OPT=Object.assign({},OPT,{health:{checks:[{k:"light",st:"ok",why:"ok",p:{a:4,usual:4}}]}});
                    BH_USER={}; renderHistory(); return document.getElementById('bh-light').open; }""")
                check(f"{tag}: closed while lighting is fine", o is False)
            check(f"{tag}: no console errors" + (f" {errs[:3]}" if errs else ""), not errs)
            check(f"{tag}: the page sent nothing (GET only)" + (f" {blocked[:2]}" if blocked else ""), not blocked)
            ctx.close()
    br.close()

print("ALL PASS" if not FAILED else f"{FAILED} FAILED")
sys.exit(1 if FAILED else 0)
