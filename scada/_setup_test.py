"""Offline test: the first-start guide on the home page (texts in both languages, steps, what it saves, that it never
touches the boiler) and the monitor's "setup" settings (defaults, checks, kept after a restart)."""
import os, re, sys, tempfile, shutil

FAILED = 0


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


here = os.path.dirname(os.path.abspath(__file__))
html = open(os.path.join(here, "home.html"), encoding="utf-8").read()

# ---- texts: every su_ text in English and Danish, every used one defined
keys = sorted(set(re.findall(r"\b(su_[a-z0-9_]+):\"", html)))
check("guide has texts", len(keys) > 50)
for k in keys:
    check(f"text {k} in English and Danish", len(re.findall(r"\b" + k + r':"', html)) == 2)
used = (set(re.findall(r'"(su_[a-z0-9_]+)"', html)) - {"su_p_"}) |{"su_p_" + x for x in ("morning", "evening", "both", "none")}
check("every su_ text the code uses is defined", not (used - set(keys)))

# ---- the guide code
code = html[html.index("// ============================================================ first-start guide"):
            html.index("// ============================================================ polling")]
steps = re.search(r"const SU_STEPS=\[([^\]]*)\]", code).group(1).replace('"', "").split(",")
check("8 steps in order", steps == ["lang", "hi", "boil", "wx", "pel", "ash", "plan", "done"])
for b in ("back", "skip", "next", "open", "skipall"):
    check(f"button '{b}'", f'"{b}"' in code)
check("in-page: no browser popups", not re.search(r"\b(alert|confirm|prompt)\(", code) and "window.open" not in code)
for bad in ("run_mode", "cfg_set", "set_values", "cfg_data", "api/fan", "api/lux", "/reset"):
    check(f"never touches the boiler ({bad})", bad not in code)
posts = set(re.findall(r'suPost\("([^"]+)"', code))
check("only the monitor's own settings / pellet API", posts == {"api/settings", "api/pellets/stock", "api/pellets"})
check("plan presets keep the plan switched off (no 'enabled' sent)", "warm:{periods:P}" in code and "enabled:true" not in code)
check("setup done at the end", '{setup:{done:true}}' in code and 'SU_STEPS[SU.i]==="done") await suMarkDone()' in code)
check("Skip all also sets done", 'a==="skipall"' in code and "suMarkDone()" in code)
check("opens by itself only when not done", "if(c&&!c.done&&!SU.open) suOpen();" in code)
check("boiler step reuses the service-picture fields", "ctlFields(d.ci||ctlInfo())" in code and "controller_info:ci" in code)
check("guide, dialog and Settings draw the boiler and store fields from the one field list",
      'fRow("setup.boiler_kw",{form:1' in code and 'fRow("setup.store_kg",{form:1' in code and "const FIELDS={" in html
      and 'fCheck("setup.store_kg"' in code)
check("typed values count as asked", "ci.asked=true" in code)
check("boiler size pre-filled from the board's KEDEL TYPE (033)", 'r["033"]' in code and "SU_KW={0:10,1:15,2:25}" in code)
check("place search reused", "wxCardHTML(true,true)" in code and "function wxRedraw()" in html)
check("connection shown live", "suLive()" in html and 'if(SU.open) suLive();' in html)
check("no hard-coded places or readings in the guide", not re.search(r"\b(lat|lon)\s*:\s*\d", code))
# the normal page
check("full page: the normal page is hidden while it shows", "html.su-on .wrap" in html and 'id="setup"' in html)
check("no flash of the normal page before the answer", "su-wait" in html)
check("'Run the setup again' in Settings (Service & relearn)", 'data-su="again"' in html and "su_again" in html)
check("Settings and guide never draw the shared inputs twice", "if(SU.open){ el.innerHTML=\"\"; return; }" in html)
# the service picture: not asked again when the guide stored the 3 numbers
ask = html[html.index("async function shareTech()"):html.index("// ============================================================ header / language")]
check("service picture skips the question when the 3 numbers are there", "if(!ctlInfo().asked&&!ctlFilled())" in ask)
check("ctlFilled needs all three", "!!c.sw&&c.min_pct!=null&&c.fan100_rpm!=null" in html)

# ---- the monitor's settings
sys.path.insert(0, os.path.join(here, ".."))
import burner_features as F
d = tempfile.mkdtemp()
try:
    st = F.Settings(d, lambda *a: None)
    check("default: not done, nothing set",
          st.get("setup") == {"done": False, "at": None, "lang": "", "boiler_kw": None, "store_kg": None})
    ok, _ = st.update({"setup": {"lang": "da", "boiler_kw": "15", "store_kg": "1000"}})
    s = st.get("setup")
    check("saves language, size and store", ok and s["lang"] == "da" and s["boiler_kw"] == 15 and s["store_kg"] == 1000 and not s["done"])
    ok, _ = st.update({"setup": {"done": True}})
    s = st.get("setup")
    check("done sets the time", ok and s["done"] and isinstance(s["at"], (int, float)) and s["at"] > 0)
    s2 = F.Settings(d, lambda *a: None).get("setup")
    check("kept after a restart", s2 == s)
    for bad in ({"boiler_kw": 12}, {"lang": "de"}, {"store_kg": 0}, {"store_kg": 99999}, {"store_kg": "x"}, "x"):
        ok, _ = st.update({"setup": bad})
        check(f"refuses {bad}", not ok)
    ok, _ = st.update({"setup": {"boiler_kw": "", "store_kg": None}})
    check("empty clears size and store", ok and st.get("setup")["boiler_kw"] is None and st.get("setup")["store_kg"] is None)
    check("still done after other changes", st.get("setup")["done"] is True)
    ok, _ = st.update({"setup": {"done": True}, "controller_info": {"sw": "1.2.3", "min_pct": 30, "fan100_rpm": 2000, "asked": True}})
    check("guide step saves both in one call", ok and st.get("controller_info")["min_pct"] == 30)
finally:
    shutil.rmtree(d, ignore_errors=True)

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
