"""Texts in English and Danish: every language has the same keys and the same {placeholders}.
Python tables: phone alerts (burner_notify), phone replies (burner_phone), Wi-Fi setup page, boiler state names.
Web pages: no inline LANG==="da" ? "..." : "..." text pairs left in home.html and dashboard.html (texts go in I18N;
the key check of I18N itself runs in _pages_test.py in a real browser). No boiler, no network."""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pi"))

FAILED = 0


def check(name, cond, extra=""):
    global FAILED
    print(("PASS " if cond else "FAIL ") + name + (("  " + extra) if extra and not cond else ""), flush=True)
    FAILED += 0 if cond else 1


def holes(s):
    return sorted(set(re.findall(r"\{(\w+)\}", str(s))))


def same(name, by_lang):
    """by_lang: {lang: {key: text or tuple}} -> same keys and placeholders in every language"""
    langs = sorted(by_lang)
    check(name + ": has en and da", {"en", "da"} <= set(langs))
    base = by_lang["en"]
    for lang in langs:
        if lang == "en":
            continue
        other = by_lang[lang]
        check(f"{name}: {lang} has the same keys as en", set(other) == set(base),
              f"missing {sorted(set(base) - set(other))} extra {sorted(set(other) - set(base))}")
        bad = [k for k in base if k in other and holes(base[k]) != holes(other[k])]
        check(f"{name}: {lang} has the same placeholders as en", not bad, str(bad))
        empty = [k for k in other if not other[k]]
        check(f"{name}: {lang} has no empty text", not empty, str(empty))


import burner_notify, burner_phone, burner_common, pelletpi_wifi_setup

for mod in (burner_notify, burner_phone):
    langs = {}
    for key, (_prio, _tags, texts) in mod.TEXTS.items():
        for lang, txt in texts.items():
            langs.setdefault(lang, {})[key] = txt
    same(mod.__name__ + ".TEXTS", langs)

same("wifi setup TEXT", {k: {kk: vv for kk, vv in v.items() if kk not in ("lang", "other_lang")}
                         for k, v in pelletpi_wifi_setup.TEXT.items()})
check("STATE_PAGE: en and da have the same number of states",
      len(burner_common.STATE_PAGE["en"]) == len(burner_common.STATE_PAGE["da"]))

# inline text pairs on the web pages: LANG==="da"?"..." (a number format such as "da-DK" or "," is fine)
PAIR = re.compile(r'LANG\s*===?\s*"(?:da|en)"\s*\?\s*"([^"]*)"')
ALLOWED = {"da-DK", ",", "DK"}
for page in ("home.html", "dashboard.html"):
    with open(os.path.join(HERE, page), encoding="utf-8") as f:
        text = f.read()
    left = [m for m in PAIR.findall(text) if m not in ALLOWED]
    check(f"{page}: no inline EN/DA text pairs", not left, str(left))

# server messages: the server sends a kind/code + numbers, the pages hold the words (EN and DA, so each key twice)
with open(os.path.join(ROOT, "burner_features.py"), encoding="utf-8") as f:
    feat = f.read()
pages = {}
for page in ("home.html", "dashboard.html"):
    with open(os.path.join(HERE, page), encoding="utf-8") as f:
        pages[page] = f.read()


def both(page, key):
    return len(re.findall(r"\b" + re.escape(key) + r':"', pages[page])) >= 2


def part(cls):
    a = feat.index("class " + cls)
    return feat[a:feat.index("\nclass ", a + 1)]


warns = {("schedule", k) for k in re.findall(r'self\._warn\("(\w+)"', part("Scheduler"))}
warns |= {("ash_plate", k) for k in re.findall(r'self\._warn\("(\w+)"', part("AshPlate"))}
warns |= {("lux", k) for k in re.findall(r'"kind": "(\w+)"', part("LuxMonitor"))}
for cls, src in (("AutoReset", "auto_reset"), ("VvbInput", "vvb"), ("WarmMode", "warm")):
    warns |= {(src, k) for k in re.findall(r'"type": "warning", "source": "' + src + r'", "kind": "(\w+)"', part(cls))}
check("server warning kinds found", len(warns) >= 14, str(sorted(warns)))
HOME_SPECIAL = {("schedule", "short_run"): "w_short_run", ("warm", "start_failed"): "w_warm_fail"}
for src, kind in sorted(warns):
    hk = HOME_SPECIAL.get((src, kind)) or ("w_" + src + "_" + kind)
    check(f"home.html: words for warning {src}/{kind} in EN and DA",
          both("home.html", hk) or (src == "ash_plate" and both("home.html", kind)))
    check(f"dashboard.html: words for warning {src}/{kind} in EN and DA", both("dashboard.html", f"wt_{src}_{kind}"))
codes = set(re.findall(r'self\._set\("(\w+)"', part("AutoReset")))
check("auto reset status codes found", {"waiting", "clearing", "gave_up"} <= codes, str(codes))
for c in sorted(codes):
    check(f"dashboard.html: auto reset status '{c}' in EN and DA", both("dashboard.html", "ars_" + c))
for c in ("waiting", "clearing", "restarted", "not_allowed", "not_burning"):   # the ones /home shows next to an alarm
    check(f"home.html: auto reset status '{c}' in EN and DA", both("home.html", "ar_" + c))
check("home.html: auto reset 'gave_up' in EN and DA", both("home.html", "ar_gave"))

print("ALL PASS" if not FAILED else f"{FAILED} FAILED")
sys.exit(1 if FAILED else 0)
