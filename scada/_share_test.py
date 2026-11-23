"""Offline test: the "Share my burner" picture on the home page (texts in both languages, button wired, nothing private)."""
import os, re, sys

FAILED = 0


def check(name, cond):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name)
    FAILED += 0 if cond else 1


html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "home.html"), encoding="utf-8").read()
keys = ["sh_btn", "sh_title", "sh_sub", "sh_sub1", "sh_light", "sh_light_s", "sh_burns", "sh_pel",
        "sh_alarms", "sh_alarms_s", "sh_ver", "sh_foot", "sh_none"]
for k in keys:
    check(f"text {k} in English and Danish", len(re.findall(r"\b" + k + r':"', html)) == 2)
check("button on History", 'data-act="share"' in html)
check("button wired", 'k==="share") shareBurner()' in html)
check("downloads one PNG", '"image/png"' in html and "a.download=" in html)
draw = html[html.index("function shareData()"):html.index("function shareBurner()")]
# the picture must not use the pellet brand, plan names, place or silo/ignition counters
for bad in ["batch", "warm", "plan", "weather", "silo", "ignitions", "place"]:
    check(f"picture does not use '{bad}'", bad not in draw.lower())
check("no upload from the share code", "fetch(" not in draw and "api(" not in draw)

# v2: the technical picture for service-minded people
keys2 = sorted(set(re.findall(r"\b(s2_[a-z0-9_]+):\"", html)))
check("v2 has texts", len(keys2) > 40)
for k in keys2:
    check(f"text {k} in English and Danish", len(re.findall(r"\b" + k + r':"', html)) == 2)
used = set(re.findall(r'"(s2_[a-z0-9_]+)"', html))
check("every v2 text used by the code is defined", not (used - set(keys2)))
check("v2 button on History", 'data-act="share2"' in html)
check("v2 button wired", 'k==="share2") shareTech()' in html)
code2 = html[html.index("const S2_FACTORY"):html.index("function shareTech()")]
for bad in ["batch", "warm", "plan", "weather", "silo", "ignitions", "place", "name:"]:
    check(f"v2 picture does not use '{bad}'", bad not in code2.lower())
check("v2 no upload", "fetch(" not in code2 and "api(" not in code2)
check("v2 is 1080 wide", "W=1080" in code2)
for need in ["IGN.records", "flame_power", "turn_hist", "transitions", "HIST24", ".runs"]:
    check(f"v2 uses {need}", need in code2)

# v3: the numbers the board does not send, asked once and kept in the monitor's settings
ask = html[html.index("async function shareTech()"):html.index("// ============================================================ header / language")]
check("asks only when not asked yet (and not all three typed in the guide)", "if(!ctlInfo().asked&&!ctlFilled())" in ask)
check("skip also counts as asked", "Object.assign({}, v||{}, {asked:true})" in ask)
check("saved in the monitor settings", 'api("api/settings",{controller_info:v})' in ask)
check("no browser popup in the dialog", "prompt(" not in ask[ask.index("function ctlAsk"):] and "confirm(" not in ask[ask.index("function ctlAsk"):])
check("dialog hints use the boiler menus", "MENU → FLERE → INFO" in html and "MONTØR → MIN YDELSE" in html and "MONTØR → SUGER INDSTILLING" in html)
check("Settings (My boiler) can change them: the same fields, from the one field list",
      all(f'"controller_info.{k}":' in html for k in ("sw", "min_pct", "fan100_rpm")) and 'fRow("controller_info.sw")' in html
      and 'fRow("controller_info."+k,{form:1' in ask)
check("picture uses the typed values", "ci.min_pct" in code2 and "ci.fan100_rpm" in code2 and "ci.sw" in code2)
check("picture no longer reads the board's broken version", 'num("038")' not in code2)
for bad in ["run_mode", "cfg_set", "set_values"]:
    check(f"save path does not touch the boiler ({bad})", bad not in ask)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import tempfile, shutil
import burner_features as F
d = tempfile.mkdtemp()
try:
    st = F.Settings(d, lambda *a: None)
    check("default: not asked, nothing set", st.get("controller_info") == {"asked": False, "sw": "", "min_pct": None, "fan100_rpm": None})
    ok, _ = st.update({"controller_info": {"sw": "V1.40.1", "min_pct": "20", "fan100_rpm": 1800, "asked": True}})
    check("saves the three numbers", ok and st.get("controller_info") == {"asked": True, "sw": "1.40.1", "min_pct": 20, "fan100_rpm": 1800})
    check("kept after a restart", F.Settings(d, lambda *a: None).get("controller_info")["min_pct"] == 20)
    for bad in ({"sw": "abc"}, {"min_pct": 0}, {"min_pct": 101}, {"fan100_rpm": 50}, {"fan100_rpm": "x"}):
        ok, _ = st.update({"controller_info": bad})
        check(f"refuses {bad}", not ok)
    ok, _ = st.update({"controller_info": {"min_pct": "", "sw": ""}})
    ci = st.get("controller_info")
    check("empty clears a value", ok and ci["min_pct"] is None and ci["sw"] == "" and ci["fan100_rpm"] == 1800)
finally:
    shutil.rmtree(d, ignore_errors=True)

print("FAILED:", FAILED)
sys.exit(1 if FAILED else 0)
